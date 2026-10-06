"""The MCP server: stateless, five tools, no consequences when killed.

There is no `mcp` SDK on Python 3.9, so the stdio transport is hand-rolled:
newline-delimited JSON-RPC 2.0. The server writes spec files, spawns detached
processes and reads state files. It holds nothing.

The load-bearing test here is the boring one: a spawned job's stdout must go to
log.txt, never to an inherited pipe. A job that inherits the server's pipes dies
of EPIPE when the server dies -- or worse, blocks forever once the 64 KB pipe
buffer fills and nobody is reading.
"""
import base64
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import carnet
import phases
import factory_mcp
from chat_store import ChatNotFound
from conftest import write_config
from factory_config import ConfigError
from factory_mcp import Factory, ToolError, detach_kwargs, handle, serve, spawn_detached
from gpu_lock import GpuLock
from job_store import JobStore
import project_map
import session_log
import turn_runner
import workspace_memory

TEST_SRC = "def test_x():\n    assert True\n"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "f@f.f")
    git(root, "config", "user.name", "factory")
    (root / "calc.py").write_text("x = 1\n")
    (root / "elo.py").write_text("K = 32\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "seed")
    return root


class StubManager:
    """Stands in for LlamaServerManager: no server, just the lifecycle state
    the Factory reads (current model) and drives (ensure/shutdown)."""
    def __init__(self):
        self.current = None
        self.url = "http://stub"
        self.shutdowns = 0

    def ensure(self, model):
        self.current = (model, 1234)
        return self.url

    def shutdown(self):
        self.shutdowns += 1
        self.current = None


class StubTimer:
    """Deterministic threading.Timer: never fires on its own; a test calls
    `.fn()` to simulate the idle timeout. `fired` collects every armed timer."""
    fired = []

    def __init__(self, delay, fn):
        self.delay, self.fn, self.cancelled = delay, fn, False
        StubTimer.fired.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


def _inject_runtime_stub(f):
    """Chat/agent ride an injected llama-server stub: what is under test is the
    lifecycle contract, not llama-server. StubTimer makes the idle release
    deterministic (fire it by hand from a test)."""
    StubTimer.fired = []
    f._llama = StubManager()
    f._llama_cfg = {"chat_idle_s": 600, "models": {}, "port": 8091}
    f.timer_fn = StubTimer
    return f


@pytest.fixture
def factory(tmp_path, project):
    cfg = write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                       paths={"demo": project})
    spawned = []
    f = Factory(tmp_path / "jobs", cfg, spawn_fn=lambda job_id: spawned.append(job_id) or 4242)
    f.spawned = spawned
    return _inject_runtime_stub(f)


def call(factory, tool, **args):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": tool, "arguments": args}}
    return handle(msg, factory)["result"]


def payload(result):
    return json.loads(result["content"][0]["text"])


def delegate(factory, **over):
    args = dict(project="demo", goal="make it work",
                tests={"test_x.py": TEST_SRC}, target_files=["calc.py"])
    args.update(over)
    return call(factory, "delegate", **args)


# --- protocol ---------------------------------------------------------------

def test_initialize_announces_the_tools_capability(factory):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2024-11-05"}}

    result = handle(msg, factory)["result"]

    assert result["protocolVersion"] == "2024-11-05"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "local-factory"


def test_a_notification_gets_no_reply(factory):
    msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}

    assert handle(msg, factory) is None


def test_tools_list_offers_exactly_the_five_tools(factory):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    tools = handle(msg, factory)["result"]["tools"]

    assert sorted(t["name"] for t in tools) == [
        "delegate", "job_cancel", "job_log", "job_result", "job_status"]
    assert all("inputSchema" in t for t in tools)


def test_an_unknown_method_is_a_json_rpc_error(factory):
    msg = {"jsonrpc": "2.0", "id": 7, "method": "sorcery"}

    reply = handle(msg, factory)

    assert reply["error"]["code"] == -32601
    assert reply["id"] == 7


def test_serve_answers_line_delimited_json_and_ignores_blanks(factory):
    stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
                        "\n"
                        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    stdout = io.StringIO()

    serve(stdin, stdout, factory)

    lines = [l for l in stdout.getvalue().splitlines() if l.strip()]
    assert len(lines) == 1, "the notification must not be answered"
    assert json.loads(lines[0])["id"] == 1


def test_malformed_json_does_not_kill_the_server(factory):
    stdin = io.StringIO("{not json\n" '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
    stdout = io.StringIO()

    serve(stdin, stdout, factory)

    ids = [json.loads(l)["id"] for l in stdout.getvalue().splitlines() if l.strip()]
    assert 2 in ids


# --- shutdown ---------------------------------------------------------------

FAKE_MCP = [sys.executable, str(Path(__file__).with_name("fake_mcp_server.py"))]


def _mcp_factory(tmp_path, project):
    """A Factory whose perimeter declares the fake MCP server."""
    cfg = write_config(
        tmp_path,
        '[projects.demo]\nrunner = "pytest"\n\n'
        '[mcp.servers.fake]\ncommand = {}\ntools = ["echo"]\n'.format(
            json.dumps(FAKE_MCP)),
        paths={"demo": project})
    return _inject_runtime_stub(Factory(tmp_path / "jobs", cfg))


def _spawned_server(factory, workspace):
    factory.mcp.tool_specs(["fake"], workspace)
    return factory.mcp._servers[("fake", workspace)].proc


def test_shutdown_closes_the_mcp_servers_holding_a_workspace(tmp_path, project):
    # An MCP server is spawned with cwd=workspace (mcp_client.py:64) and on
    # Windows a CWD is a locked handle: the directory cannot be deleted,
    # renamed or moved while the process lives. Nothing closed them, and
    # children survive their parent -- so the lock outlived the studio.
    # Measured 2026-07-23: 28 of 35 `web` bench runs died of PermissionError
    # at reseed.
    factory = _mcp_factory(tmp_path, project)
    proc = _spawned_server(factory, str(project))
    assert proc.poll() is None

    factory.shutdown()

    assert proc.wait(timeout=5) is not None
    assert factory.mcp._servers == {}


def test_serve_closes_the_factory_when_the_stream_ends(tmp_path, project):
    # The stdio server's own exit: Claude Code closes stdin and the process
    # leaves. Whatever it spawned has to leave with it.
    factory = _mcp_factory(tmp_path, project)
    proc = _spawned_server(factory, str(project))

    serve(io.StringIO(""), io.StringIO(), factory)

    assert proc.wait(timeout=5) is not None


# --- delegate ---------------------------------------------------------------

def test_delegate_returns_a_job_id_and_spawns_the_runner(factory):
    result = delegate(factory)

    job_id = payload(result)["job_id"]
    assert factory.spawned == [job_id]


def test_delegate_writes_the_spec_before_spawning(factory):
    job_id = payload(delegate(factory))["job_id"]

    spec = JobStore(factory.jobs_root).read_spec(job_id)
    assert spec["project"] == "demo"
    assert spec["tests"] == {"test_x.py": TEST_SRC}


def test_delegate_refuses_a_project_outside_the_allowlist(factory):
    result = delegate(factory, project="conformergpd")

    assert result["isError"] is True
    assert "not in factory.toml" in result["content"][0]["text"]
    assert factory.spawned == []


def test_delegate_refuses_a_delegation_with_no_tests(factory):
    result = delegate(factory, tests={})

    assert result["isError"] is True
    assert factory.spawned == []


def test_delegate_refuses_to_target_the_judge(factory):
    result = delegate(factory, target_files=["calc.py", "test_calc.py"])

    assert result["isError"] is True
    assert factory.spawned == []


def test_delegate_refuses_a_spec_supplied_conftest(factory):
    result = delegate(factory, tests={"test_x.py": TEST_SRC, "conftest.py": "import calc"})

    assert result["isError"] is True
    assert factory.spawned == []


def test_a_refused_delegation_leaves_no_job_behind(factory):
    delegate(factory, project="nope")

    assert JobStore(factory.jobs_root).list_jobs() == []


def test_delegate_validates_start_stage(factory):
    result = delegate(factory, start_stage=0)
    assert result["isError"] is True
    assert factory.spawned == []

    result = delegate(factory, start_stage=4)  # the 14b rung is gone: 1..3
    assert result["isError"] is True
    assert factory.spawned == []

    job_id = payload(delegate(factory, start_stage=3))["job_id"]
    spec = JobStore(factory.jobs_root).read_spec(job_id)
    assert spec["start_stage"] == 3


def test_delegate_without_start_stage_omits_it_from_the_spec(factory):
    job_id = payload(delegate(factory))["job_id"]

    assert "start_stage" not in JobStore(factory.jobs_root).read_spec(job_id)


def test_delegate_validates_edit_mode(factory):
    result = delegate(factory, edit_mode="patch")
    assert result["isError"] is True
    assert factory.spawned == []


def test_delegate_stores_edit_mode(factory):
    job_id = payload(delegate(factory, edit_mode="diff"))["job_id"]
    spec = JobStore(factory.jobs_root).read_spec(job_id)
    assert spec["edit_mode"] == "diff"


def test_delegate_without_edit_mode_omits_it_from_the_spec(factory):
    job_id = payload(delegate(factory))["job_id"]

    assert "edit_mode" not in JobStore(factory.jobs_root).read_spec(job_id)


def test_delegate_records_context_files_in_the_spec(factory):
    job_id = payload(delegate(factory, context_files=["elo.py"]))["job_id"]

    spec = JobStore(factory.jobs_root).read_spec(job_id)
    assert spec["context_files"] == ["elo.py"]


def test_delegate_without_context_files_records_an_empty_list(factory):
    job_id = payload(delegate(factory))["job_id"]

    assert JobStore(factory.jobs_root).read_spec(job_id)["context_files"] == []


def test_delegate_refuses_a_context_file_absent_from_head(factory):
    result = delegate(factory, context_files=["typo.py"])

    assert result["isError"] is True
    assert factory.spawned == []


def test_delegate_refuses_a_context_file_that_is_also_a_target(factory):
    result = delegate(factory, context_files=["calc.py"])

    assert result["isError"] is True
    assert factory.spawned == []


def test_delegate_refuses_context_files_over_the_budget(factory, project):
    """60 000 bytes is the cap (ADR-008 truncation guard); keep this just over it."""
    (project / "big.py").write_text("x = 1\n" * 10001)  # 60006 bytes
    git(project, "add", "-A")
    git(project, "commit", "-qm", "add oversized context file")

    result = delegate(factory, context_files=["big.py"])

    assert result["isError"] is True
    assert factory.spawned == []


def test_the_delegate_schema_advertises_context_files(factory):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    tools = handle(msg, factory)["result"]["tools"]

    schema = next(t for t in tools if t["name"] == "delegate")["inputSchema"]
    assert "context_files" in schema["properties"]
    assert "context_files" not in schema["required"]


# --- status / result / log --------------------------------------------------

def test_job_status_reports_a_queued_job(factory):
    job_id = payload(delegate(factory))["job_id"]

    assert payload(call(factory, "job_status", job_id=job_id))["status"] == "queued"


def test_job_status_on_an_unknown_job_is_an_error(factory):
    result = call(factory, "job_status", job_id="j_deadbeef")

    assert result["isError"] is True


def test_job_result_on_a_finished_job_returns_the_diff(factory):
    job_id = payload(delegate(factory))["job_id"]
    store = JobStore(factory.jobs_root)
    store.write_result(job_id, {"status": "succeeded", "diff": "--- a/calc.py"})

    assert payload(call(factory, "job_result", job_id=job_id))["diff"] == "--- a/calc.py"


def test_job_result_on_a_running_job_says_so_rather_than_failing(factory):
    job_id = payload(delegate(factory))["job_id"]
    store = JobStore(factory.jobs_root)
    store.update_state(job_id, status="running")

    body = payload(call(factory, "job_result", job_id=job_id))
    assert body["status"] == "running"
    assert body["ready"] is False


def test_job_result_on_a_job_that_died_without_a_verdict_explains_itself(factory):
    """No daemon reaps jobs. A stale heartbeat and no result.json is all we have."""
    job_id = payload(delegate(factory))["job_id"]
    store = JobStore(factory.jobs_root)
    store.update_state(job_id, status="running")
    store.heartbeat(job_id, now=time.time() - 10_000)
    store.append_log(job_id, "attempt 1 [7b]: 0 passed, 3 failed\n")

    body = payload(call(factory, "job_result", job_id=job_id))

    assert body["status"] == "dead"
    assert body["ready"] is False
    assert "no result" in body["error"]
    assert "attempt 1" in body["log_tail"]


def test_job_log_returns_the_tail(factory):
    job_id = payload(delegate(factory))["job_id"]
    store = JobStore(factory.jobs_root)
    for i in range(200):
        store.append_log(job_id, "line {}\n".format(i))

    body = payload(call(factory, "job_log", job_id=job_id, tail=5))

    assert body["log"].splitlines() == ["line {}".format(i) for i in range(195, 200)]


# --- spawning ---------------------------------------------------------------

def test_the_spawned_process_is_detached_from_our_process_group():
    kwargs = detach_kwargs()

    if sys.platform == "win32":
        assert kwargs["creationflags"] & subprocess.DETACHED_PROCESS
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True


def test_a_spawned_job_writes_its_output_to_log_txt_not_to_our_pipes(tmp_path, capsys):
    """Inheriting the server's stdout is how a 'detached' job silently dies."""
    job_dir = tmp_path / "j_1"
    job_dir.mkdir()
    argv = [sys.executable, "-c",
            "import sys; print('to stdout'); sys.stderr.write('to stderr\\n')"]

    spawn_detached(job_dir, argv)

    log = job_dir / "log.txt"
    deadline = time.time() + 15
    while time.time() < deadline and "to stderr" not in (
            log.read_text(encoding="utf-8") if log.exists() else ""):
        time.sleep(0.05)

    contents = log.read_text(encoding="utf-8")
    assert "to stdout" in contents
    assert "to stderr" in contents
    assert capsys.readouterr().out == ""


def test_final_print_does_not_overwrite_the_narrative_log(tmp_path):
    """The j_d85cd85b incident: the spawned runner inherits log.txt as a handle
    parked at byte 0 while its own append_log writes grow the file -- the final
    result print then lands on top of the narrative (the AnchorNotFound
    diagnostics were destroyed). seek_stdout_to_end() before the print is the
    fix; only the real spawn path reproduces this, subprocess.run keeps append
    semantics and hides it."""
    job_dir = tmp_path / "j_1"
    job_dir.mkdir()
    harness = str(Path(__file__).resolve().parent.parent)
    child = ("import sys; sys.path.insert(0, {!r}); "
             "from job_store import JobStore, seek_stdout_to_end; "
             "s = JobStore({!r}); "
             "s.append_log('j_1', 'attempt 1: anchor miss detail\\n'); "
             "seek_stdout_to_end(); "
             "print('{{\"status\": \"failed\"}}')").format(harness, str(tmp_path))

    spawn_detached(job_dir, [sys.executable, "-c", child])

    log = job_dir / "log.txt"
    deadline = time.time() + 15
    while time.time() < deadline and '"status"' not in (
            log.read_text(encoding="utf-8") if log.exists() else ""):
        time.sleep(0.05)

    contents = log.read_text(encoding="utf-8")
    assert "attempt 1: anchor miss detail" in contents
    assert '{"status": "failed"}' in contents


def test_spawn_does_not_wait_for_the_child(tmp_path):
    job_dir = tmp_path / "j_2"
    job_dir.mkdir()

    t0 = time.monotonic()
    pid = spawn_detached(job_dir, [sys.executable, "-c", "import time; time.sleep(30)"])
    elapsed = time.monotonic() - t0

    assert elapsed < 5, "delegate() must return immediately"
    assert pid > 0
    subprocess.run(["taskkill", "/F", "/PID", str(pid)] if sys.platform == "win32"
                   else ["kill", "-9", str(pid)], capture_output=True)


# --- list_jobs ---------------------------------------------------------------

def test_list_jobs_returns_newest_first_with_spec_fields(factory):
    now = time.time()
    a = factory.store.create({"project": "demo", "goal": "first", "tests": {},
                              "target_files": [], "context_files": []}, now=now - 1)
    b = factory.store.create({"project": "demo", "goal": "second", "tests": {},
                              "target_files": [], "context_files": []}, now=now)
    jobs = factory.list_jobs()["jobs"]
    assert [j["job_id"] for j in jobs] == [b, a]
    assert jobs[1]["goal"] == "first"
    assert jobs[1]["project"] == "demo"
    assert jobs[1]["status"] == "queued"
    assert jobs[1]["duration"] is None


def test_list_jobs_computes_duration_from_result_mtime(factory):
    job_id = factory.store.create({"project": "demo", "goal": "g", "tests": {},
                                   "target_files": [], "context_files": []}, now=100.0)
    factory.store.write_result(job_id, {"status": "succeeded", "diff": ""})
    entry = factory.list_jobs()["jobs"][0]
    assert entry["status"] == "succeeded"
    assert entry["duration"] is not None and entry["duration"] > 0


def test_list_jobs_skips_a_half_written_job_dir(factory):
    factory.store.create({"project": "demo", "goal": "ok", "tests": {},
                          "target_files": [], "context_files": []})
    (factory.jobs_root / "j_broken").mkdir()  # dir without state.json/spec.json
    jobs = factory.list_jobs()["jobs"]
    assert [j["goal"] for j in jobs] == ["ok"]


# ---- chat ----

def _fake_stream(chunks, metrics=None, fail_after=None):
    """Stands in for llama_client.chat_stream: a generator with a return value.

    `chunks` are (kind, text) tuples; bare strings mean ("content", text).
    """
    chunks = [c if isinstance(c, tuple) else ("content", c) for c in chunks]
    content = "".join(t for k, t in chunks if k == "content")

    def stream(url, messages, **kw):
        stream.calls.append({"url": url, "messages": messages, "kw": kw})
        for i, c in enumerate(chunks):
            if fail_after is not None and i == fail_after:
                raise OSError("server vanished")
            yield c
        return dict({"content": content, "tokens_per_s": 30.0,
                     "ttft_s": 1.0, "eval_count": 5, "prefill_count": 100,
                     "done_reason": "stop"}, **(metrics or {}))
    stream.calls = []
    return stream


def test_chat_create_validates_the_model_name(factory):
    with pytest.raises(ToolError):
        factory.chat_create("bad model name!!")

    s = factory.chat_create("qwen3:4b")
    assert factory.chat_get(s["session_id"])["model"] == "qwen3:4b"
    assert factory.chat_list()["sessions"][0]["session_id"] == s["session_id"]


def test_an_agent_session_refuses_a_model_that_cannot_call_tools(factory):
    """E2E audit §3: an agent session on qwen2.5-coder:7b answered ` ```" `
    and ended -- no tool call, no explanation, an empty bubble. The template
    carries the Hermes tools block and an isolated repro outside the factory
    returns the same garbage: the model simply cannot. Declared capability
    beats a silent empty turn; a plain chat on the same model is fine.
    """
    factory._llama_cfg = {"chat_idle_s": 600, "port": 8091, "models": {
        "toolless": {"path": "x", "args": [], "tools": False},
        "capable": {"path": "y", "args": [], "tools": True}}}

    with pytest.raises(ToolError, match="tool"):
        factory.chat_create("toolless", kind="agent")
    with pytest.raises(ToolError, match="tool"):
        sid = factory.chat_create("capable", kind="agent")["session_id"]
        factory.chat_set_model(sid, "toolless")

    assert factory.chat_create("toolless")["session_id"]  # plain chat: allowed


def test_runtime_models_reports_tool_capability(factory, tmp_path):
    factory._llama_cfg = {"chat_idle_s": 600, "port": 8091, "models": {
        "toolless": {"path": str(tmp_path / "a.gguf"), "args": [], "tools": False},
        "capable": {"path": str(tmp_path / "b.gguf"), "args": [], "tools": True}}}
    models = {m["name"]: m for m in factory.runtime_models()["models"]}
    assert models["toolless"]["tools"] is False
    assert models["capable"]["tools"] is True


def test_add_project_refuses_a_command_line_typed_as_one_argument(factory):
    """E2E audit §4: the field wants one argument per line. Typed on one line,
    "-m pytest tests -q" became a single argument that no runner can execute,
    accepted without a word -- the error only surfaced at the first job."""
    repo = factory.jobs_root.parent / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ConfigError, match="one argument per line"):
        factory.add_project("demo2", str(repo), ["-m pytest tests -q"])
    # a single argument that is a path, spaces and all, stays legal
    factory.add_project("demo3", str(repo), ["C:/some dir/run.py"])


# ---- runtime lifecycle (lock = VRAM) ----

def test_chat_keeps_the_gpu_lock_until_idle(factory, monkeypatch):
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", _fake_stream(["hi"]))
    sid = factory.chat_create("m")["session_id"]
    list(factory.chat_reply(sid, "hello"))
    # generation done: server still loaded, lock still held, idle timer armed
    assert factory._llama.current is not None
    outsider = GpuLock(factory.jobs_root / ".gpu.lock")
    assert not outsider.acquire(timeout=0)
    timer = StubTimer.fired[-1]
    assert timer.delay == 600 and not timer.cancelled
    timer.fn()  # idle fires
    assert factory._llama.shutdowns == 1
    assert outsider.acquire(timeout=0)
    outsider.release()


def test_chat_refuses_while_a_job_holds_the_gpu(factory, monkeypatch):
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", _fake_stream(["hi"]))
    job = GpuLock(factory.jobs_root / ".gpu.lock")
    assert job.acquire(timeout=0)
    sid = factory.chat_create("m")["session_id"]
    with pytest.raises(ToolError):
        next(factory.chat_reply(sid, "hello"))
    assert factory._llama.current is None  # never loaded
    job.release()


def test_runtime_endpoints(factory):
    factory._llama_cfg = {"chat_idle_s": 600, "models": {"m": {}}, "port": 8091}
    factory.runtime_load("m")
    assert factory.runtime_status() == {"loaded": "m", "held_by": "chat"}
    out = factory.runtime_unload()
    assert out["status"] == "unloaded"
    assert factory.runtime_status()["loaded"] is None
    assert factory._llama.shutdowns == 1


def test_runtime_load_rejects_a_model_absent_from_the_catalogue(factory):
    factory._llama_cfg = {"chat_idle_s": 600, "models": {"m": {}}, "port": 8091}
    with pytest.raises(ToolError, match="llama_server.models"):
        factory.runtime_load("ghost")


def test_runtime_models_lists_the_catalogue_with_gguf_state(factory, tmp_path):
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x" * 10)
    factory._llama_cfg = {"chat_idle_s": 600, "port": 8091, "models": {
        "present": {"path": str(gguf), "args": ["-ub", "2048"]},
        "absent": {"path": str(tmp_path / "nope.gguf"), "args": []}}}
    models = {m["name"]: m for m in factory.runtime_models()["models"]}
    assert models["present"]["size"] == 10 and models["present"]["missing"] is False
    assert models["present"]["args"] == ["-ub", "2048"]
    assert models["absent"]["missing"] is True and models["absent"]["size"] is None


# ---- byte-stable prompt (KV prefix reuse depends on it) ----

def test_agent_system_is_byte_stable():
    a = factory_mcp._agent_system("C:/ws", ["read_file", "run_command"])
    assert a == factory_mcp._agent_system("C:/ws", ["read_file", "run_command"])


def test_wire_messages_carry_no_timestamps(factory, monkeypatch):
    # le prefixe KV n'est reutilisable que si l'historique renvoye est
    # byte-identique d'un tour a l'autre : ts/metrics ne doivent jamais
    # entrer dans les messages envoyes au serveur
    seen = {}

    def spy(url, messages, **kw):
        seen["messages"] = messages
        return _fake_stream(["ok"])(url, messages, **kw)

    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", spy)
    sid = factory.chat_create("m")["session_id"]
    list(factory.chat_reply(sid, "hello"))
    for m in seen["messages"]:
        assert set(m) <= {"role", "content", "tool_calls", "tool_name"}


def test_chat_reply_streams_saves_and_holds_the_gpu_until_idle(factory, monkeypatch):
    fake = _fake_stream(["Bon", "jour"])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1")["session_id"]

    events = list(factory.chat_reply(sid, "salut"))

    assert events[0] == ("start", None)
    assert [e for e in events if e[0] == "chunk"] == [("chunk", "Bon"), ("chunk", "jour")]
    assert events[-1][0] == "done"
    assert events[-1][1]["tokens_per_s"] == 30.0
    # history sent to the model includes the fresh user message
    assert fake.calls[0]["messages"][-1] == {"role": "user", "content": "salut"}
    session = factory.chat_get(sid)
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]
    assert session["messages"][1]["content"] == "Bonjour"
    assert session["messages"][1]["metrics"]["tokens_per_s"] == 30.0
    # lock = VRAM: the GPU stays held after the turn, released on idle timeout
    outsider = GpuLock(factory.jobs_root / ".gpu.lock")
    assert not outsider.acquire(timeout=0)
    StubTimer.fired[-1].fn()  # idle fires
    assert outsider.acquire(timeout=0)
    outsider.release()


def test_chat_reply_requests_the_chat_context_and_saves_usage(factory, monkeypatch):
    fake = _fake_stream(["ok"])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1")["session_id"]

    done = list(factory.chat_reply(sid, "salut"))[-1][1]

    assert fake.calls[0]["kw"]["num_ctx"] == factory_mcp.CHAT_NUM_CTX
    assert done["prefill_count"] == 100
    assert done["done_reason"] == "stop"
    assert done["num_ctx"] == factory_mcp.CHAT_NUM_CTX
    saved = factory.chat_get(sid)["messages"][-1]["metrics"]
    assert saved["prefill_count"] == 100
    assert saved["done_reason"] == "stop"
    assert saved["num_ctx"] == factory_mcp.CHAT_NUM_CTX


def _fill_session(factory, sid, n_pairs=8, size=400):
    for i in range(n_pairs):
        factory.chats.append(sid, {"role": "user",
                                   "content": "q%d " % i + "x" * size, "ts": 1.0})
        factory.chats.append(sid, {"role": "assistant",
                                   "content": "a%d " % i + "y" * size, "ts": 1.0})


def _fake_summary_chat(content="Objectif : tester.", fail=False):
    def chat(url, system, user, **kw):
        chat.calls.append({"url": url, "system": system, "user": user})
        if fail:
            raise OSError("server vanished")
        return {"content": content}
    chat.calls = []
    return chat


def test_chat_reply_saves_calibration_and_context_info(factory, monkeypatch):
    fake = _fake_stream(["ok"])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1")["session_id"]

    done = list(factory.chat_reply(sid, "salut"))[-1][1]

    # usage_pct/used_tokens come from the real counters, not the estimate.
    assert done["context"] == {"evicted": 0, "dropped": 0, "compacted": False,
                               "usage_pct": 0, "used_tokens": 105,
                               "num_ctx": 65536}
    session = factory.chat_get(sid)
    assert session["calibration"]["tokens"] == 100  # prefill_count from fake
    assert session["calibration"]["chars"] > 0
    assert session["messages"][-1]["metrics"]["context"]["dropped"] == 0


def test_overflow_and_calibration_read_the_whole_prompt_not_the_evaluated_part(
        factory, monkeypatch):
    """The KV cache makes prefill_count a lie about window occupancy.

    Measured 2026-07-22 on the live server: a cached turn reports prompt_n 1
    for a 1103-token prompt. A session that is 95 % full therefore looked
    empty, so the backstop that forces compaction on the server's own counters
    (the guard against the 2026-07-14 silent truncation) could never fire
    after the first turn -- and the calibration stored chars-per-token against
    a token count of 1.
    """
    fake = _fake_stream(["ok"], metrics={"prefill_count": 1,
                                         "prompt_count": 62000,
                                         "eval_count": 500})
    summarizer = _fake_summary_chat("Objectif : compter juste.")
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    monkeypatch.setattr(factory_mcp.llama_client, "chat", summarizer)
    sid = factory.chat_create("m:1")["session_id"]
    _fill_session(factory, sid, n_pairs=4, size=10)  # estimates approve
    factory.chats.append(sid, {"role": "assistant", "content": "coupé",
                               "ts": 1.0,
                               "metrics": {"prefill_count": 1,
                                           "prompt_count": 60000,
                                           "eval_count": 5000}})

    done = list(factory.chat_reply(sid, "salut"))[-1][1]

    assert done["context"]["compacted"] is True   # backstop fired on the truth
    assert done["context"]["used_tokens"] == 62500
    assert factory.chat_get(sid)["calibration"]["tokens"] == 62000


def test_chat_reply_compacts_when_over_threshold(factory, monkeypatch):
    fake = _fake_stream(["ok"])
    summarizer = _fake_summary_chat("Objectif : tout retenir.")
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    monkeypatch.setattr(factory_mcp.llama_client, "chat", summarizer)
    monkeypatch.setattr(factory_mcp, "CHAT_NUM_CTX", 400)  # budget 100 tokens
    sid = factory.chat_create("m:1")["session_id"]
    _fill_session(factory, sid)  # 16 messages, far over 70 tokens

    events = list(factory.chat_reply(sid, "salut"))

    assert ("compacting", True) in events
    assert events[-1][0] == "done"
    assert events[-1][1]["context"]["compacted"] is True
    # one summary call, with the structured prompt, on the loaded server
    assert len(summarizer.calls) == 1
    assert summarizer.calls[0]["system"] == factory_mcp.chat_context.SUMMARY_PROMPT
    assert summarizer.calls[0]["url"] == "http://stub"
    # summary persisted: covers everything but the verbatim tail, tagged model
    summary = factory.chat_get(sid)["summary"]
    assert summary["model"] == "m:1"
    assert summary["content"] == "Objectif : tout retenir."
    assert summary["covers_until"] == 17 - factory_mcp.chat_context.VERBATIM_TAIL
    # the prompt actually sent starts with the summary as a system message
    sent = fake.calls[0]["messages"]
    assert sent[0]["role"] == "system" and "tout retenir" in sent[0]["content"]
    assert all("q0 " not in (m.get("content") or "") for m in sent)


def test_chat_reply_compacts_when_the_last_turn_really_overflowed(factory, monkeypatch):
    # Estimates said everything fit, but the server's own counters from the last
    # turn say >= 90 % of the window was used: real numbers overrule estimates
    # (the 2026-07-14 failure: 65536/65536, silent truncation, reply cut).
    fake = _fake_stream(["ok"])
    summarizer = _fake_summary_chat("Objectif : ne plus déborder.")
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    monkeypatch.setattr(factory_mcp.llama_client, "chat", summarizer)
    sid = factory.chat_create("m:1")["session_id"]
    _fill_session(factory, sid, n_pairs=4, size=10)  # tiny: estimates approve
    factory.chats.append(sid, {"role": "assistant", "content": "coupé",
                               "ts": 1.0,
                               "metrics": {"prefill_count": 60000,
                                           "eval_count": 5000}})

    events = list(factory.chat_reply(sid, "salut"))

    assert ("compacting", True) in events
    assert events[-1][0] == "done"
    assert events[-1][1]["context"]["compacted"] is True
    assert len(summarizer.calls) == 1


def test_chat_reply_survives_a_failed_compaction(factory, monkeypatch):
    fake = _fake_stream(["ok"])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    monkeypatch.setattr(factory_mcp.llama_client, "chat",
                        _fake_summary_chat(fail=True))
    monkeypatch.setattr(factory_mcp, "CHAT_NUM_CTX", 400)
    sid = factory.chat_create("m:1")["session_id"]
    _fill_session(factory, sid)

    events = list(factory.chat_reply(sid, "salut"))

    assert ("compacting", True) in events
    assert events[-1][0] == "done"                       # the turn still lands
    assert factory.chat_get(sid).get("summary") is None  # nothing persisted
    assert events[-1][1]["context"]["compacted"] is False
    assert events[-1][1]["context"]["dropped"] > 0       # trim covered it


def test_chat_reply_streams_thinking_and_keeps_it_out_of_content(factory, monkeypatch):
    fake = _fake_stream([("thinking", "Hmm, "), ("thinking", "voyons."),
                         ("content", "Bonjour")])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1")["session_id"]

    events = list(factory.chat_reply(sid, "salut"))

    assert [e for e in events if e[0] == "thinking"] == \
        [("thinking", "Hmm, "), ("thinking", "voyons.")]
    assert [e for e in events if e[0] == "chunk"] == [("chunk", "Bonjour")]
    saved = factory.chat_get(sid)["messages"][-1]
    assert saved["content"] == "Bonjour"
    assert saved["thinking"] == "Hmm, voyons."
    # thinking is display-only: the next turn's history must not include it
    events = list(factory.chat_reply(sid, "encore"))
    assert all("thinking" not in m for m in fake.calls[1]["messages"])


def test_chat_reply_refuses_when_a_job_holds_the_gpu(factory):
    sid = factory.chat_create("m:1")["session_id"]
    lock = GpuLock(factory.jobs_root / ".gpu.lock")
    assert lock.acquire(timeout=0)
    try:
        gen = factory.chat_reply(sid, "salut")
        with pytest.raises(ToolError):
            next(gen)
    finally:
        lock.release()
    # nothing was appended: the refusal happened before any write
    assert factory.chat_get(sid)["messages"] == []


def test_chat_reply_mid_stream_failure_keeps_the_partial(factory, monkeypatch):
    fake = _fake_stream(["par", "tiel", "never"], fail_after=2)
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1")["session_id"]

    events = list(factory.chat_reply(sid, "salut"))

    assert events[-1][0] == "error"
    saved = factory.chat_get(sid)["messages"][-1]
    assert saved["role"] == "assistant"
    assert saved["content"] == "partiel"
    assert "server vanished" in saved["error"]
    # even a failed turn leaves a releasable idle timer, no lock leak
    StubTimer.fired[-1].fn()
    lock = GpuLock(factory.jobs_root / ".gpu.lock")
    assert lock.acquire(timeout=0)
    lock.release()


def test_a_stop_caught_inside_the_lane_keeps_the_partial_and_gives_the_gpu_back(
        factory, monkeypatch):
    # Stop no longer only lands between two events: a lane blocked on a silent
    # server unwinds through here with TurnCancelled. That path must do what
    # the closed-generator path does -- keep what was said, arm the idle timer
    # -- and nothing more: it is a click, not a failure.
    cancel = turn_runner.Cancellation()

    def stopped_mid_stream(url, messages, **kw):
        yield "content", "par"
        yield "content", "tiel"
        cancel.cancel()
        kw["cancel"].check()

    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        stopped_mid_stream)
    sid = factory.chat_create("m:1")["session_id"]

    events = []
    with pytest.raises(turn_runner.TurnCancelled):
        for event in factory.chat_reply(sid, "salut", cancel=cancel):
            events.append(event)

    assert not [e for e in events if e[0] == "error"]
    saved = factory.chat_get(sid)["messages"][-1]
    assert saved["role"] == "assistant"
    assert saved["content"] == "partiel"
    assert saved["error"] == "interrupted"
    StubTimer.fired[-1].fn()            # the idle timer was armed on the way out
    lock = GpuLock(factory.jobs_root / ".gpu.lock")
    assert lock.acquire(timeout=0)
    lock.release()


def test_a_stop_before_the_first_token_saves_no_empty_answer(factory,
                                                             monkeypatch):
    # The wait the token exists for is the one BEFORE the first token, so this
    # is the common case: an empty assistant bubble in the transcript for every
    # turn the operator changes their mind about would be the visible bug.
    cancel = turn_runner.Cancellation()

    def never_speaks(url, messages, **kw):
        cancel.cancel()
        kw["cancel"].check()
        yield "content", "jamais"

    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", never_speaks)
    sid = factory.chat_create("m:1")["session_id"]

    with pytest.raises(turn_runner.TurnCancelled):
        list(factory.chat_reply(sid, "salut", cancel=cancel))

    assert [m["role"] for m in factory.chat_get(sid)["messages"]] == ["user"]


def test_chat_reply_unknown_session_raises_before_yielding(factory):
    with pytest.raises(ChatNotFound):
        next(factory.chat_reply("c_deadbeef", "x"))


def test_chat_reply_rejects_blank_content(factory):
    sid = factory.chat_create("m:1")["session_id"]
    with pytest.raises(ValueError):
        next(factory.chat_reply(sid, "   "))


def test_chat_delete_removes_the_session(factory):
    sid = factory.chat_create("m:1")["session_id"]
    assert factory.chat_delete(sid) == {"session_id": sid, "deleted": True}
    with pytest.raises(ChatNotFound):
        factory.chat_get(sid)


def test_chat_create_agent_defaults_workspace_to_cwd(factory):
    s = factory.chat_create("m:1", kind="agent", mode="auto")
    got = factory.chat_get(s["session_id"])
    assert got["kind"] == "agent" and got["mode"] == "auto"
    assert got["workspace"] == os.getcwd()


def test_chat_set_mode_validates(factory):
    sid = factory.chat_create("m:1", kind="agent", mode="approve")["session_id"]
    assert factory.chat_set_mode(sid, "auto")["mode"] == "auto"
    with pytest.raises(ToolError):
        factory.chat_set_mode(sid, "yolo")


# ---- agent ----

def _fake_agent_stream(turns):
    """Each turn: list of (kind, payload) tuples; content joined for metrics."""
    state = {"i": 0, "calls": []}

    def stream(url, messages, **kw):
        state["calls"].append({"messages": messages, "tools": kw.get("tools"),
                               "num_ctx": kw.get("num_ctx"),
                               "options": kw.get("options")})
        turn = turns[state["i"]]
        state["i"] += 1
        content = ""
        for kind, payload in turn:
            if kind == "content":
                content += payload
            yield kind, payload
        return {"content": content, "tokens_per_s": 30.0, "ttft_s": 1.0,
                "eval_count": 5}
    stream.state = state
    return stream


def _agent(factory, mode="auto", tmp=None):
    return factory.chat_create("m:1", kind="agent", mode=mode,
                               workspace=str(tmp))["session_id"]


def _seed_plan(factory, sid, steps, goal="faire le truc"):
    """A plan in the transcript without spending a scripted turn on it. Same
    message shape `_handle_carnet_tool` writes for `set_plan`, which is what
    `carnet.project` reads back."""
    factory.chats.append(sid, {
        "role": "tool", "tool_name": "plan", "ts": time.time(),
        "content": json.dumps({"goal": goal,
                               "steps": [{"step": s} for s in steps]})})


def test_agent_reply_executes_tools_and_reprompts(factory, tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("salut\n", encoding="utf-8")
    fake = _fake_agent_stream([
        [("tool_call", {"name": "read_file", "arguments": {"path": "a.txt"}})],
        [("content", "Le fichier dit salut.")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "lis a.txt"))

    kinds = [k for k, _ in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert "tool_call" in kinds and "tool_result" in kinds
    result = dict(events)["tool_result"]
    assert result["name"] == "read_file" and "salut" in result["output"]
    # the second model call saw the tool message and the tools list
    msgs = fake.state["calls"][1]["messages"]
    assert any(m["role"] == "tool" and "salut" in m["content"] for m in msgs)
    assert [t["function"]["name"] for t in fake.state["calls"][0]["tools"]] \
        == [t["function"]["name"] for t in factory_mcp.agent_tools.TOOLS] \
        + [t["function"]["name"] for t in factory_mcp.carnet.CARNET_TOOLS]
    assert fake.state["calls"][0]["num_ctx"] == factory_mcp.CHAT_NUM_CTX
    # system prompt is first and mentions the workspace
    assert msgs[0]["role"] == "system" and str(tmp_path) in msgs[0]["content"]


# ---- lessons: the first self-improvement loop ----

def test_agent_session_records_lessons_from_its_own_audit(factory, tmp_path,
                                                          monkeypatch):
    # A session that failed its tools over and over must leave something
    # behind. The agent's own version (de04bb4) never wrote a single lesson.
    fake = _fake_agent_stream([
        [("tool_call", {"name": "read_file", "arguments": {"path": "nope"}})],
        [("tool_call", {"name": "read_file", "arguments": {"path": "nope"}})],
        [("tool_call", {"name": "read_file", "arguments": {"path": "nope"}})],
        [("content", "j'abandonne")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "lis nope"))

    patterns = {l["pattern"] for l in factory.chats.load_lessons()}
    assert "tool_errors" in patterns


def test_lessons_reach_the_agent_system_prompt(factory, tmp_path, monkeypatch):
    factory.chats.record_lessons([{"pattern": "tool_errors", "count": 9,
                                   "suggestion": "read the error text",
                                   "last_seen": 1.0}])
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "salut"))

    system = fake.state["calls"][0]["messages"][0]["content"]
    assert "read the error text" in system


def test_the_lesson_block_is_frozen_for_the_life_of_a_session(
        factory, tmp_path, monkeypatch):
    # The system prompt IS the KV prefix. A block that changes between turns
    # kills --cache-reuse exactly when the session is going badly and
    # incidents pile up -- so a lesson learned mid-session lands on the NEXT
    # session, never this one.
    fake = _fake_agent_stream([[("content", "un")], [("content", "deux")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "premier"))
    factory.chats.record_lessons([{"pattern": "brand_new", "count": 50,
                                   "suggestion": "changed mid-session",
                                   "last_seen": 2.0}])
    list(factory.agent_reply(sid, "second"))

    first = fake.state["calls"][0]["messages"][0]["content"]
    second = fake.state["calls"][1]["messages"][0]["content"]
    assert first == second
    assert "changed mid-session" not in second


def test_a_clean_session_teaches_nothing(factory, tmp_path, monkeypatch):
    fake = _fake_agent_stream([[("content", "voilà")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "salut"))

    assert factory.chats.load_lessons() == []


# ---- workspace memory: the anti-redo half ----

_DEAD = "factory-no-such-command-xyz"


def _twice_failing(cmd=_DEAD):
    return _fake_agent_stream([
        [("tool_call", {"name": "run_command", "arguments": {"command": cmd}})],
        [("tool_call", {"name": "run_command", "arguments": {"command": cmd}})],
        [("content", "ça ne marche pas")],
    ])


def test_a_command_that_failed_twice_lands_in_workspace_memory(
        factory, tmp_path, monkeypatch):
    # The July 14 ask: the agent burned 19 near-identical scripts re-trying
    # what had already failed. What failed must be written down.
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _twice_failing())
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "lance le script"))

    memory = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert _DEAD in memory
    assert "failed 2x" in memory


def test_a_quiet_second_turn_does_not_inflate_the_count(
        factory, tmp_path, monkeypatch):
    # Learning runs at the end of EVERY user turn and audits the whole
    # transcript, so the same two failures were re-counted once per turn: a
    # command that failed twice read "failed 4x" after a single "merci", with
    # no new failure at all. The count is what auto-tuning would read, so an
    # inflated count is a lesson that lies.
    fake = _fake_agent_stream([
        [("tool_call", {"name": "run_command", "arguments": {"command": _DEAD}})],
        [("tool_call", {"name": "run_command", "arguments": {"command": _DEAD}})],
        [("content", "ça ne marche pas")],
        [("content", "de rien")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "lance le script"))
    list(factory.agent_reply(sid, "merci"))

    memory = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "failed 2x" in memory
    assert "failed 4x" not in memory


def test_one_failure_per_turn_is_remembered_once_it_repeats(
        factory, tmp_path, monkeypatch):
    # The gap the watermark opened (2026-07-22): learning audits only what is
    # new, and two failures spread over two turns are one failure per slice --
    # under the floor both times, so the workspace never learned a command that
    # fails every single turn.
    fake = _fake_agent_stream([
        [("tool_call", {"name": "run_command", "arguments": {"command": _DEAD}})],
        [("content", "raté")],
        [("tool_call", {"name": "run_command", "arguments": {"command": _DEAD}})],
        [("content", "encore raté")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "essaie"))
    assert not (tmp_path / "MEMORY.md").exists()  # once is still a typo

    list(factory.agent_reply(sid, "réessaie"))

    memory = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert _DEAD in memory
    assert "failed 2x" in memory


def test_a_quiet_second_turn_does_not_inflate_a_lesson(
        factory, tmp_path, monkeypatch):
    fake = _fake_agent_stream([
        [("tool_call", {"name": "read_file", "arguments": {"path": "nope"}})],
        [("tool_call", {"name": "read_file", "arguments": {"path": "nope"}})],
        [("tool_call", {"name": "read_file", "arguments": {"path": "nope"}})],
        [("content", "j'abandonne")],
        [("content", "de rien")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "lis nope"))
    after_one = {l["pattern"]: l["count"] for l in factory.chats.load_lessons()}
    list(factory.agent_reply(sid, "merci"))
    after_two = {l["pattern"]: l["count"] for l in factory.chats.load_lessons()}

    assert after_two == after_one


def test_workspace_memory_reaches_the_agent_system_prompt(
        factory, tmp_path, monkeypatch):
    (tmp_path / "MEMORY.md").write_text(
        "# MEMORY\n\nLe venv est dans .venv, toujours l'utiliser.\n",
        encoding="utf-8")
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "salut"))

    system = fake.state["calls"][0]["messages"][0]["content"]
    assert "Le venv est dans .venv" in system


def test_writing_a_memory_never_eats_what_the_operator_wrote(
        factory, tmp_path, monkeypatch):
    # MEMORY.md is a file in the operator's project, not a factory scratchpad.
    (tmp_path / "MEMORY.md").write_text("# MEMORY\n\nNe pas toucher.\n",
                                        encoding="utf-8")
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _twice_failing())
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "lance le script"))

    memory = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert memory.startswith("# MEMORY\n\nNe pas toucher.")
    assert _DEAD in memory


def test_the_memory_block_is_frozen_for_the_life_of_a_session(
        factory, tmp_path, monkeypatch):
    # Same KV-prefix rule as lessons: a memory recorded now lands on the NEXT
    # session, never rewrites this one's system prompt.
    fake = _fake_agent_stream([[("content", "un")], [("content", "deux")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "premier"))
    (tmp_path / "MEMORY.md").write_text("changé en cours de session\n",
                                        encoding="utf-8")
    list(factory.agent_reply(sid, "second"))

    first = fake.state["calls"][0]["messages"][0]["content"]
    second = fake.state["calls"][1]["messages"][0]["content"]
    assert first == second
    assert "changé en cours de session" not in second


def test_a_session_without_shell_failures_writes_no_memory(
        factory, tmp_path, monkeypatch):
    fake = _fake_agent_stream([[("content", "voilà")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "salut"))

    assert not (tmp_path / "MEMORY.md").exists()


def test_chat_upload_writes_into_the_workspace(factory, tmp_path):
    sid = _agent(factory, "auto", tmp_path)
    b64 = base64.b64encode("bonjour\n".encode("utf-8")).decode("ascii")

    res = factory.chat_upload(sid, "notes.txt", b64)

    assert res == {"path": "uploads/notes.txt", "bytes": 8}
    assert (tmp_path / "uploads" / "notes.txt").read_text(
        encoding="utf-8") == "bonjour\n"


def test_chat_upload_refuses_bad_names_chat_sessions_and_bad_payloads(
        factory, tmp_path):
    sid = _agent(factory, "auto", tmp_path)
    ok = base64.b64encode(b"x").decode("ascii")
    for bad in ("../evil.txt", "a/b.txt", "a\\b.txt", "..", "", ".env"):
        with pytest.raises(ToolError):
            factory.chat_upload(sid, bad, ok)
    chat_sid = factory.chat_create("m:1")["session_id"]
    with pytest.raises(ToolError):  # no workspace to receive the file
        factory.chat_upload(chat_sid, "a.txt", ok)
    with pytest.raises(ToolError):
        factory.chat_upload(sid, "a.txt", "pas du base64 !!")
    huge = base64.b64encode(b"z" * (factory_mcp.UPLOAD_MAX_BYTES + 1))
    with pytest.raises(ToolError):
        factory.chat_upload(sid, "big.bin", huge.decode("ascii"))
    assert not (tmp_path / "uploads").exists()  # nothing leaked to disk


def test_agent_recovers_a_tool_call_emitted_as_text(factory, tmp_path,
                                                     monkeypatch):
    # qwen3-coder:30b emits its tool call as XML content, not structured
    # tool_calls (llama-server can't parse its template). Before the fallback
    # the agent stopped silently on the raw XML; now it executes the call.
    (tmp_path / "a.txt").write_text("salut\n", encoding="utf-8")
    xml = ("Je lis le fichier.\n\n"
           "<function=read_file>\n"
           "<parameter=path>\na.txt\n</parameter>\n"
           "</function>\n</tool_call>")
    fake = _fake_agent_stream([
        [("content", xml)],
        [("content", "Le fichier dit salut.")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "lis a.txt"))

    kinds = [k for k, _ in events]
    assert "tool_result" in kinds
    result = dict(events)["tool_result"]
    assert result["name"] == "read_file" and "salut" in result["output"]
    # the saved assistant message carries the recovered call, not just text
    msgs = factory.chat_get(sid)["messages"]
    recovered = next(m for m in msgs
                     if m["role"] == "assistant" and m.get("tool_calls"))
    assert recovered["tool_calls"][0]["function"]["name"] == "read_file"
    assert "<function=" not in recovered["content"]  # raw XML stripped


def test_agent_turn_compacts_and_stubs_old_tool_outputs(factory, tmp_path,
                                                        monkeypatch):
    fake = _fake_agent_stream([[("content", "fini")]])
    summarizer = _fake_summary_chat("Objectif : agent.")
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    monkeypatch.setattr(factory_mcp.llama_client, "chat", summarizer)
    monkeypatch.setattr(factory_mcp, "CHAT_NUM_CTX", 400)
    sid = _agent(factory, "auto", tmp_path)
    for i in range(14):
        factory.chats.append(sid, {"role": "tool", "tool_name": "read_file",
                                   "content": "t%d " % i + "z" * 400, "ts": 1.0})

    events = list(factory.agent_reply(sid, "continue"))

    assert ("compacting", True) in events
    assert events[-1][0] == "done"
    assert events[-1][1]["context"]["compacted"] is True
    assert factory.chat_get(sid)["summary"]["content"] == "Objectif : agent."
    # the summarizer never sees raw tool dumps, only the stub
    assert "zzz" not in summarizer.calls[0]["user"]
    # the agent system prompt still leads, summary right after
    sent = fake.state["calls"][0]["messages"]
    assert str(tmp_path) in sent[0]["content"]
    assert "Objectif : agent." in sent[1]["content"]


def test_agent_reply_reads_instruction_file_into_system_prompt(
        factory, tmp_path, monkeypatch):
    (tmp_path / "AGENT.md").write_text("Toujours répondre en breton.",
                                       encoding="utf-8")
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "salut"))

    system = fake.state["calls"][0]["messages"][0]["content"]
    assert "breton" in system


def test_agent_reply_parks_write_calls_in_approve_mode(factory, tmp_path,
                                                       monkeypatch):
    fake = _fake_agent_stream([
        [("tool_call", {"name": "write_file",
                        "arguments": {"path": "b.txt", "content": "x"}})],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)

    events = list(factory.agent_reply(sid, "écris b.txt"))

    assert events[-1][0] == "approval_needed"
    assert events[-1][1]["name"] == "write_file"
    assert not (tmp_path / "b.txt").exists()
    assert factory.chat_get(sid)["pending_calls"][0]["name"] == "write_file"
    # lock = VRAM: the GPU stays warm across the approval gate, released on idle
    StubTimer.fired[-1].fn()
    lock = GpuLock(factory.jobs_root / ".gpu.lock")
    assert lock.acquire(timeout=0)
    lock.release()


def test_agent_reply_refuses_while_a_call_is_pending(factory, tmp_path,
                                                     monkeypatch):
    fake = _fake_agent_stream([
        [("tool_call", {"name": "run_command", "arguments": {"command": "x"}})],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)
    list(factory.agent_reply(sid, "vas-y"))

    with pytest.raises(ToolError):
        next(factory.agent_reply(sid, "encore"))


def test_agent_reply_resumes_queued_calls_in_auto_mode(factory, tmp_path,
                                                       monkeypatch):
    # Deadlock regression: the iteration cap leaves the last turn's calls in
    # pending_calls; in auto mode nothing awaits an operator, so a new user
    # message ("continue") must drain them instead of raising ToolError.
    (tmp_path / "a.txt").write_text("yo", encoding="utf-8")
    fake = _fake_agent_stream([[("content", "fini")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    factory.chats.set_pending_calls(
        sid, [{"name": "read_file", "arguments": {"path": "a.txt"}}])

    events = list(factory.agent_reply(sid, "continue"))

    kinds = [k for k, _ in events]
    assert "tool_result" in kinds and events[-1][0] == "done"
    assert factory.chat_get(sid)["pending_calls"] == []


def test_agent_reply_resumes_queued_read_calls_in_approve_mode(
        factory, tmp_path, monkeypatch):
    # Only a call that actually needs approval blocks a new message.
    (tmp_path / "a.txt").write_text("yo", encoding="utf-8")
    fake = _fake_agent_stream([[("content", "fini")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)
    factory.chats.set_pending_calls(
        sid, [{"name": "read_file", "arguments": {"path": "a.txt"}}])

    events = list(factory.agent_reply(sid, "continue"))

    assert events[-1][0] == "done"


def test_agent_reply_read_calls_run_even_in_approve_mode(factory, tmp_path,
                                                         monkeypatch):
    (tmp_path / "a.txt").write_text("yo", encoding="utf-8")
    fake = _fake_agent_stream([
        [("tool_call", {"name": "read_file", "arguments": {"path": "a.txt"}})],
        [("content", "fini")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)

    events = list(factory.agent_reply(sid, "lis"))

    assert events[-1][0] == "done"
    assert "approval_needed" not in [k for k, _ in events]


def test_agent_reply_stops_at_the_iteration_cap(factory, tmp_path, monkeypatch):
    # Distinct paths: every output differs, so only the cap can stop the turn.
    factory._llama_cfg["agent_max_iterations"] = 4
    turns = [[("tool_call", {"name": "read_file",
                             "arguments": {"path": "f%d.txt" % i}})]
             for i in range(5)]
    fake = _fake_agent_stream(turns)
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "boucle"))

    assert events[-1] == ("done", {"stopped": "iteration_limit"})
    assert len(fake.state["calls"]) == 4
    # the history says why and how to resume
    last = factory.chat_get(sid)["messages"][-1]
    assert last["tool_name"] == "system" and "continue" in last["content"]


def test_agent_reply_runs_unlimited_when_cap_is_zero(factory, tmp_path,
                                                      monkeypatch):
    # cap 0 (the default) = no iteration_limit: it runs past the old cap of 20
    # and stops only when the model gives a final answer.
    factory._llama_cfg["agent_max_iterations"] = 0
    turns = [[("tool_call", {"name": "read_file",
                             "arguments": {"path": "f%d.txt" % i}})]
             for i in range(factory_mcp.MAX_TOOL_ITERATIONS + 2)]
    turns.append([("content", "fini")])
    fake = _fake_agent_stream(turns)
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "boucle"))

    assert events[-1][0] == "done"
    assert events[-1][1].get("stopped") != "iteration_limit"
    assert len(fake.state["calls"]) == factory_mcp.MAX_TOOL_ITERATIONS + 3


def test_identical_repeats_are_surfaced_not_blocked(factory, tmp_path,
                                                    monkeypatch):
    """No hard block (Phase 1 spec): the old _LoopGuard killed the turn at 5
    identical results, which also killed legitimate work. The carnet counts
    the target and shows it instead; Stop and agent_max_iterations stay the
    only runaway brakes."""
    (tmp_path / "a.txt").write_text("yo", encoding="utf-8")
    turn = [("tool_call", {"name": "read_file", "arguments": {"path": "a.txt"}})]
    fake = _fake_agent_stream([turn] * 6 + [[("content", "fini")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "boucle"))

    assert events[-1][0] == "done" and "stopped" not in events[-1][1]
    assert len(fake.state["calls"]) == 7   # the turn ran to its own end
    sent = fake.state["calls"][-1]["messages"]
    assert "read_file:a.txt" in sent[-1]["content"]   # carnet, last message


def test_agent_system_prompt_names_the_shell_and_the_edit_rules(tmp_path):
    system = factory_mcp._agent_system(str(tmp_path))
    assert "0 matches" in system          # edit_file recovery rule
    assert "read_file" in system          # steer away from shell reads
    if os.name == "nt":
        assert "Windows" in system and "grep" in system
        # July 14 session: multi-line commands silently no-op under cmd.exe
        assert "first line" in system.lower()


def test_agent_system_prompt_always_includes_the_global_agent_md(
        tmp_path, monkeypatch):
    # The July 14 session ran in a fresh sandbox with no AGENT.md: the agent
    # worked without any of the operator's rules. The factory-level agent.md
    # must load regardless of the workspace.
    glob = tmp_path / "global_agent.md"
    glob.write_text("Règle globale : jamais de stash.", encoding="utf-8")
    monkeypatch.setattr(factory_mcp, "GLOBAL_AGENT_MD", glob)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENT.md").write_text("Règle locale : breton.", encoding="utf-8")

    system = factory_mcp._agent_system(str(ws))

    assert "jamais de stash" in system    # global rules present
    assert "breton" in system             # workspace rules still appended


def test_a_workspace_agents_md_is_read_too(tmp_path, monkeypatch):
    # AGENTS.md (plural) is the name the ecosystem settled on, and the factory
    # already has three on disk (experiments/crgpd-rerun, ComfyUI, a worktree).
    # AGENT.md and AGENTS.md are different filenames -- not a case difference
    # Windows would paper over -- so the plural was silently ignored and those
    # workspaces ran with no instructions at all.
    glob = tmp_path / "global_agent.md"
    glob.write_text("Regle globale.", encoding="utf-8")
    monkeypatch.setattr(factory_mcp, "GLOBAL_AGENT_MD", glob)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("Regle locale : breton.", encoding="utf-8")

    system = factory_mcp._agent_system(str(ws))

    assert "breton" in system


def test_agent_system_prompt_does_not_duplicate_the_global_file(
        tmp_path, monkeypatch):
    # Self-hosted sessions: the workspace IS the factory root, so the
    # workspace instruction file and the global one are the same file.
    glob = tmp_path / "agent.md"
    glob.write_text("Règle globale unique.", encoding="utf-8")
    monkeypatch.setattr(factory_mcp, "GLOBAL_AGENT_MD", glob)

    system = factory_mcp._agent_system(str(tmp_path))

    assert system.count("Règle globale unique.") == 1


def test_agent_turn_warns_the_model_before_the_iteration_cap(
        factory, tmp_path, monkeypatch):
    # Hitting the cap blind wastes the whole budget; at 5 remaining the
    # model must be told to wrap up. Only fires when a cap is configured.
    cap = 8
    factory._llama_cfg["agent_max_iterations"] = cap
    turns = [[("tool_call", {"name": "read_file",
                             "arguments": {"path": "f%d.txt" % i}})]
             for i in range(cap + 1)]
    fake = _fake_agent_stream(turns)
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "boucle"))

    warn_turn = cap - factory_mcp.ITERATION_WARNING_AT
    seen = fake.state["calls"][warn_turn]["messages"]
    assert any(m["role"] == "tool" and "5" in m["content"]
               and "iteration" in m["content"] for m in seen)
    before = fake.state["calls"][warn_turn - 1]["messages"]
    assert not any(m["role"] == "tool" and "iteration" in m["content"]
                   for m in before)


def test_usage_exposes_prefill_speed():
    # Agents are prefill-heavy (docs/audit-synthesis-2026-07.md); generation
    # tok/s alone hides half the cost.
    met = {"tokens_per_s": 50.0, "prefill_tokens_per_s": 900.0, "ttft_s": 1.2}
    out = factory_mcp._usage(met)
    assert out["prefill_tokens_per_s"] == 900.0


def test_chat_set_num_ctx_persists_and_validates(factory):
    sid = factory.chat_create("m:1")["session_id"]

    assert factory.chat_set_num_ctx(sid, 8192)["num_ctx"] == 8192
    for bad in (0, 2048, 262144, "16384", None):
        with pytest.raises(ToolError):
            factory.chat_set_num_ctx(sid, bad)


def test_agent_turn_uses_the_session_num_ctx(factory, tmp_path, monkeypatch):
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    factory.chat_set_num_ctx(sid, 8192)

    events = list(factory.agent_reply(sid, "salut"))

    assert fake.state["calls"][0]["num_ctx"] == 8192
    assert dict(events)["done"]["num_ctx"] == 8192


def test_agent_reply_saves_partial_on_mid_stream_failure(factory, tmp_path,
                                                         monkeypatch):
    def broken(url, messages, **kw):
        yield "content", "déb"
        raise OSError("server vanished")
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        lambda *a, **k: broken(*a, **k))
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "salut"))

    assert events[-1][0] == "error"
    saved = factory.chat_get(sid)["messages"][-1]
    assert saved["content"] == "déb" and "server vanished" in saved["error"]
    StubTimer.fired[-1].fn()
    lock = GpuLock(factory.jobs_root / ".gpu.lock")
    assert lock.acquire(timeout=0)
    lock.release()


def test_agent_reply_client_disconnect_saves_partial(factory, tmp_path,
                                                     monkeypatch):
    """Consumer walks away mid-stream (GeneratorExit): the partial content
    accumulated in the turn must land in the session, like chat_reply."""
    fake = _fake_agent_stream([
        [("thinking", "hmm"), ("content", "déb"), ("content", "ut")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    gen = factory.agent_reply(sid, "salut")
    assert next(gen) == ("start", None)
    assert next(gen) == ("thinking", "hmm")
    assert next(gen) == ("chunk", "déb")
    gen.close()

    saved = factory.chat_get(sid)["messages"][-1]
    assert saved["role"] == "assistant"
    assert saved["content"] == "déb"
    assert saved["thinking"] == "hmm"
    assert saved["error"] == "interrupted"
    StubTimer.fired[-1].fn()
    lock = GpuLock(factory.jobs_root / ".gpu.lock")
    assert lock.acquire(timeout=0)
    lock.release()


def test_chat_approve_executes_the_parked_call_and_resumes(factory, tmp_path,
                                                           monkeypatch):
    fake = _fake_agent_stream([
        [("tool_call", {"name": "write_file",
                        "arguments": {"path": "b.txt", "content": "x"}})],
        [("content", "écrit !")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)
    list(factory.agent_reply(sid, "écris b.txt"))

    events = list(factory.chat_approve(sid, True))

    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "x"
    assert events[-1][0] == "done"
    assert factory.chat_get(sid)["pending_calls"] == []


def test_chat_approve_refusal_feeds_the_model_a_tool_message(factory, tmp_path,
                                                             monkeypatch):
    fake = _fake_agent_stream([
        [("tool_call", {"name": "run_command", "arguments": {"command": "rm x"}})],
        [("content", "d'accord, j'arrête.")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)
    list(factory.agent_reply(sid, "supprime x"))

    events = list(factory.chat_approve(sid, False))

    assert events[-1][0] == "done"
    msgs = fake.state["calls"][1]["messages"]
    assert any(m["role"] == "tool" and "refused" in m["content"] for m in msgs)


def test_chat_approve_without_pending_call_is_a_tool_error(factory):
    sid = factory.chat_create("m:1", kind="agent")["session_id"]
    with pytest.raises(ToolError):
        next(factory.chat_approve(sid, True))


def test_chat_reply_surfaces_a_degenerate_generation(factory, monkeypatch):
    # the client's RepetitionGuard cut the stream: the partial is kept and
    # the done event says why, so the UI can tell the operator.
    fake = _fake_stream([("thinking", "Je vais lancer le test.\n"),
                         ("content", "début de rép")],
                        metrics={"stopped": "repetition"})
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1")["session_id"]

    events = list(factory.chat_reply(sid, "salut"))

    assert events[-1][0] == "done"
    assert events[-1][1]["stopped"] == "repetition"
    saved = factory.chat_get(sid)["messages"][-1]
    assert saved["role"] == "assistant"
    assert saved["metrics"]["stopped"] == "repetition"


def test_agent_reply_stops_the_turn_on_a_degenerate_generation(
        factory, tmp_path, monkeypatch):
    # A looping model must not be re-prompted for more iterations.
    def stream(url, messages, **kw):
        stream.calls += 1
        yield "thinking", "Let's go.\n"
        return {"content": "", "tokens_per_s": 30.0, "ttft_s": 1.0,
                "eval_count": 5, "stopped": "repetition"}
    stream.calls = 0
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", stream)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "fais un truc"))

    assert events[-1][0] == "done"
    assert events[-1][1]["stopped"] == "repetition"
    assert stream.calls == 1
    last = factory.chat_get(sid)["messages"][-1]
    assert last["tool_name"] == "system" and "repeating" in last["content"]


def test_agent_reprompts_a_malformed_tool_call_left_in_thinking(
        factory, tmp_path, monkeypatch):
    # qwen3.6:35b (thinking model) emitted a tool call into the reasoning
    # channel; only the closing tags survived as text -- no content, no
    # structured call. The agent used to mistake the empty turn for a final
    # answer and stop silently (session c_1a722191, msg#186). It must instead
    # re-prompt and keep going.
    orphan = ("Let me check if huggingface_hub is installed.\n"
              "</parameter>\n</function>\n</tool_call>")
    fake = _fake_agent_stream([
        [("thinking", orphan)],
        [("content", "C'est bon, installe.")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "verifie l'install"))

    assert events[-1][0] == "done" and "stopped" not in events[-1][1]
    assert len(fake.state["calls"]) == 2          # re-prompted, not stopped
    retry_msgs = fake.state["calls"][1]["messages"]
    assert any(m["role"] == "tool" and "malformed" in m["content"]
               for m in retry_msgs)


def test_agent_stops_after_persistent_malformed_tool_calls(
        factory, tmp_path, monkeypatch):
    # A model that can only emit broken calls must not loop forever (cap 0):
    # after MALFORMED_RETRY_AT turns the agent gives up like a degenerate run.
    orphan = "trying\n</function>\n</tool_call>"
    fake = _fake_agent_stream(
        [[("thinking", orphan)]] * factory_mcp.MALFORMED_RETRY_AT)
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "go"))

    assert events[-1][0] == "done"
    assert events[-1][1]["stopped"] == "malformed_call"
    assert len(fake.state["calls"]) == factory_mcp.MALFORMED_RETRY_AT
    last = factory.chat_get(sid)["messages"][-1]
    assert last["tool_name"] == "system" and "malformed" in last["content"]


def test_a_stalled_turn_reaches_the_model_neutralised(
        factory, tmp_path, monkeypatch):
    # The stall's own words are what reproduces it: the transcript is 100 % of
    # the ChatGPT lane's memory and is re-serialised whole every turn
    # (chatgpt_web.render_prompt), so "je n'ai pas les outils dans ce tour"
    # comes back as the model's own precedent. Kept on disk for the audits,
    # neutralised on the wire.
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    factory.chats.append(sid, {
        "role": "assistant", "ts": time.time(),
        "content": "Je n'ai pas les outils dans ce tour."})
    factory.chats.mark_stalled(sid)

    list(factory.agent_reply(sid, "continue"))

    sent = fake.state["calls"][0]["messages"]
    assert not any("pas les outils" in (m.get("content") or "") for m in sent)
    assert any(m.get("content") == factory_mcp.STALL_ON_WIRE for m in sent)
    kept = factory.chat_get(sid)["messages"]
    assert any("pas les outils" in (m.get("content") or "") for m in kept)


def test_agent_relances_a_turn_that_ends_without_the_call_it_announced(
        factory, tmp_path, monkeypatch):
    # Session c_14f20c89, msg 93/103/105/109: six clean calls, then a turn of
    # prose announcing the next edit and placing the tools in another turn
    # ("je n'ai pas les fonctions edit_file/run_command exposees dans ce
    # tour"). edit_file was offered the whole time. The loop used to end there
    # and wait for the operator to type "continue".
    stall = ("Je reprends. Je vais patcher Application.cpp.\n"
             "Je n'ai pas les fonctions edit_file exposees dans ce tour.")
    fake = _fake_agent_stream([
        [("content", stall)],
        [("tool_call", {"name": "step_done",
                        "arguments": {"step": 1,
                                      "evidence": "patch applique"}})],
        [("content", "C'est fait.")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    _seed_plan(factory, sid, ["patcher Application.cpp"])

    events = list(factory.agent_reply(sid, "vas-y"))

    assert len(fake.state["calls"]) == 3          # relanced, not handed back
    assert events[-1][0] == "done" and "stopped" not in events[-1][1]
    notices = [payload for kind, payload in events if kind == "notice"]
    assert len(notices) == 1 and "relance 1" in notices[0]
    relanced = fake.state["calls"][1]["messages"]
    assert any(m["role"] == "tool" and m["content"] == factory_mcp.STALL_RETRY
               for m in relanced)
    # ... and the stall does not come back as the model's own words
    assert not any("dans ce tour" in (m.get("content") or "")
                   for m in relanced)
    assert any(m.get("content") == factory_mcp.STALL_ON_WIRE
               for m in relanced)


def test_agent_does_not_relance_a_long_report(factory, tmp_path, monkeypatch):
    # c_594feb80 msg 38: a long status report (4295 chars) with an open plan was
    # relanced by the old `has_meaningful_content` gate. A long reply is an
    # answer, not an announcement.
    fake = _fake_agent_stream([[
        ("content", "Statut: le moteur est prêt. " + ("détail " * 120) + " Fin.")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    _seed_plan(factory, sid, ["finaliser la passe"])

    events = list(factory.agent_reply(sid, "où en est-on ?"))

    assert len(fake.state["calls"]) == 1
    assert events[-1][0] == "done"
    assert not [p for k, p in events if k == "notice"]


def test_agent_does_not_relance_a_turn_that_asks_a_question(
        factory, tmp_path, monkeypatch):
    # c_594feb80 msg 16/71: the reply ends with a question the operator must
    # answer. Relancing it asked the model again instead of handing back.
    fake = _fake_agent_stream([[
        ("content", "Souhaites-tu que je commence par les traductions "
                    "anglaises ou par intégrer le module ?")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    _seed_plan(factory, sid, ["faire la passe"])

    events = list(factory.agent_reply(sid, "vas-y"))

    assert len(fake.state["calls"]) == 1
    assert events[-1][0] == "done"
    assert not [p for k, p in events if k == "notice"]


def test_agent_does_not_relance_a_completion_note(factory, tmp_path, monkeypatch):
    # c_594feb80 msg 108: a short note stating the work is done. The plan's
    # steps were never ticked, but the reply is not an announcement either.
    fake = _fake_agent_stream(
        [[("content", "Aucune action supplémentaire n'est requise.")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    _seed_plan(factory, sid, ["finir"])

    events = list(factory.agent_reply(sid, "vas-y"))

    assert len(fake.state["calls"]) == 1
    assert events[-1][0] == "done"
    assert not [p for k, p in events if k == "notice"]


def test_a_bare_agent_workspace_becomes_a_git_repo(factory, tmp_path):
    ws = tmp_path / "fresh_ws"
    ws.mkdir()

    factory.chat_create("m:1", kind="agent", mode="auto", workspace=str(ws))

    assert (ws / ".git").is_dir()


def test_a_workspace_inside_a_repo_is_not_nested_with_a_second_one(
        factory, tmp_path):
    import subprocess
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    ws = tmp_path / "sub"
    ws.mkdir()

    factory.chat_create("m:1", kind="agent", mode="auto", workspace=str(ws))

    assert not (ws / ".git").exists()


def _factory_with_profiles(tmp_path, toml_extra=""):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[models."m:1"]\ntemperature = 0.6\ntop_p = 0.95\n'
                   + toml_extra, encoding="utf-8")
    return _inject_runtime_stub(factory_mcp.Factory(tmp_path / "jobs", cfg))


def test_agent_stream_gets_profile_and_num_predict(tmp_path, monkeypatch):
    factory = _factory_with_profiles(tmp_path)
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1", kind="agent", mode="auto",
                              workspace=str(tmp_path))["session_id"]

    list(factory.agent_reply(sid, "salut"))

    opts = fake.state["calls"][0]["options"]
    assert opts["num_predict"] == factory_mcp.chat_context.OUTPUT_RESERVE
    assert opts["temperature"] == 0.6 and opts["top_p"] == 0.95


def test_chat_stream_gets_num_predict_without_profile(tmp_path, monkeypatch):
    cfg = tmp_path / "factory.toml"
    cfg.write_text("", encoding="utf-8")
    factory = _inject_runtime_stub(factory_mcp.Factory(tmp_path / "jobs", cfg))
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1")["session_id"]

    list(factory.chat_reply(sid, "salut"))

    opts = fake.state["calls"][0]["options"]
    assert opts == {"num_predict": factory_mcp.chat_context.OUTPUT_RESERVE}


def test_agent_length_cut_stops_the_turn_with_its_own_notice(tmp_path, monkeypatch):
    factory = _factory_with_profiles(tmp_path)

    def stream(url, messages, **kw):
        yield "content", "cut mid-"
        return {"content": "cut mid-", "stopped": "length",
                "done_reason": "length"}
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", stream)
    sid = factory.chat_create("m:1", kind="agent", mode="auto",
                              workspace=str(tmp_path))["session_id"]

    events = list(factory.agent_reply(sid, "go"))

    assert events[-1][0] == "done" and events[-1][1]["stopped"] == "length"
    msgs = factory.chat_get(sid)["messages"]
    assert msgs[-1]["tool_name"] == "system"
    assert msgs[-1]["content"] == factory_mcp.LENGTH_STOPPED


def test_agent_length_cut_still_runs_a_call_that_was_already_complete(
        factory, tmp_path, monkeypatch):
    # The cap used to discard the turn's calls, so a thinking model whose
    # median run is ~6.5k tokens (banc A) routinely lost whole turns and waited
    # for a manual "continue". A parsed call is valid whatever happened after
    # it: run it, and say the reply was cut.
    (tmp_path / "a.txt").write_text("salut\n", encoding="utf-8")

    turn = {"n": 0}

    def stream(url, messages, **kw):
        turn["n"] += 1
        if turn["n"] == 1:
            yield "content", "je lis"
            yield "tool_call", {"name": "read_file",
                                "arguments": {"path": "a.txt"}}
            return {"content": "je lis", "stopped": "length",
                    "done_reason": "length", "eval_count": 5}
        yield "content", "le fichier dit salut."
        return {"content": "le fichier dit salut.", "eval_count": 5}
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", stream)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "lis a.txt"))

    kinds = [k for k, _ in events]
    assert "tool_result" in kinds, "the completed call must still run"
    notes = [m["content"] for m in factory.chat_get(sid)["messages"]
             if m.get("tool_name") == "system"]
    assert factory_mcp.LENGTH_STOPPED_WITH_CALLS in notes
    assert factory_mcp.LENGTH_STOPPED not in notes


def test_a_model_cut_on_every_turn_still_stops(factory, tmp_path, monkeypatch):
    # Running the calls of a cut turn removed an unconditional terminator, and
    # agent_max_iterations defaults to 0 (no cap). A model cut every turn while
    # emitting a call would loop forever; past a short streak, stop as before.
    (tmp_path / "a.txt").write_text("salut\n", encoding="utf-8")

    def stream(url, messages, **kw):
        yield "content", "je lis"
        yield "tool_call", {"name": "read_file",
                            "arguments": {"path": "a.txt"}}
        return {"content": "je lis", "stopped": "length",
                "done_reason": "length", "eval_count": 5}
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", stream)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "lis a.txt"))

    assert events[-1][0] == "done"
    notes = [m["content"] for m in factory.chat_get(sid)["messages"]
             if m.get("tool_name") == "system"]
    assert factory_mcp.LENGTH_STOPPED in notes, "the old terminator must return"


def _factory_with_mcp(tmp_path):
    fake = str(Path(__file__).with_name("fake_mcp_server.py"))
    cfg = tmp_path / "factory.toml"
    cfg.write_text(
        '[mcp.servers.fake]\n'
        'command = ["{}", "{}"]\n'
        'tools = ["echo", "boom"]\nreadonly = ["echo"]\n'
        '[toolsets.dev]\nmcp = ["fake"]\n'.format(
            sys.executable.replace("\\", "/"), fake.replace("\\", "/")),
        encoding="utf-8")
    return _inject_runtime_stub(factory_mcp.Factory(tmp_path / "jobs", cfg))


def test_chat_create_validates_the_toolset(tmp_path):
    factory = _factory_with_mcp(tmp_path)
    with pytest.raises(ToolError):
        factory.chat_create("m:1", kind="agent", toolset="nope",
                            workspace=str(tmp_path))
    s = factory.chat_create("m:1", kind="agent", toolset="dev",
                            workspace=str(tmp_path))
    assert factory.chat_get(s["session_id"])["toolset"] == "dev"


def test_agent_turn_exposes_native_plus_mcp_tools(tmp_path, monkeypatch):
    factory = _factory_with_mcp(tmp_path)
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1", kind="agent", mode="auto",
                              toolset="dev",
                              workspace=str(tmp_path))["session_id"]

    list(factory.agent_reply(sid, "salut"))

    names = [t["function"]["name"] for t in fake.state["calls"][0]["tools"]]
    assert "read_file" in names and "mcp__fake__echo" in names
    assert "mcp__fake__die" not in names


def test_agent_runs_an_mcp_tool_and_gates_non_readonly(tmp_path, monkeypatch):
    factory = _factory_with_mcp(tmp_path)
    fake = _fake_agent_stream([
        [("tool_call", {"name": "mcp__fake__echo",
                        "arguments": {"text": "hi"}})],
        [("tool_call", {"name": "mcp__fake__boom", "arguments": {}})],
        [("content", "done")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1", kind="agent", mode="approve",
                              toolset="dev",
                              workspace=str(tmp_path))["session_id"]

    events = list(factory.agent_reply(sid, "go"))

    results = [p for k, p in events if k == "tool_result"]
    assert results and results[0]["output"] == "hi"  # readonly: no gate
    assert events[-1][0] == "approval_needed"        # boom: gated


def test_agent_system_parts_join_back_to_the_prompt_byte_for_byte(tmp_path):
    """The joined string is the KV prefix: a byte that moves costs ~1 s per
    turn (measured 2026-07-22). Naming the parts must change nothing."""
    (tmp_path / "AGENTS.md").write_text("projet: regles locales\n",
                                        encoding="utf-8")
    ws = str(tmp_path)
    names = ["read_file", "write_file"]
    parts = factory_mcp._agent_system_parts(ws, names)
    assert "\n\n".join(text for _, text in parts) == \
        factory_mcp._agent_system(ws, names)


def test_agent_system_parts_are_named_in_prompt_order(tmp_path):
    (tmp_path / "AGENTS.md").write_text("projet: regles locales\n",
                                        encoding="utf-8")
    parts = factory_mcp._agent_system_parts(str(tmp_path), ["read_file"])
    assert [name for name, _ in parts] == [
        "system", "agent_md", "workspace_instructions", "tool_names"]


def test_agent_system_parts_omit_blocks_that_do_not_exist(tmp_path):
    """A workspace with no instruction file and a session with no tools are
    both normal; empty parts must not appear, or they would add separators."""
    parts = factory_mcp._agent_system_parts(str(tmp_path), None)
    assert [name for name, _ in parts] == ["system", "agent_md"]


def test_the_session_prompt_prefix_is_byte_stable_across_turns(factory, tmp_path):
    """The system prompt and the tool schemas are the KV prefix. If either
    serialises differently from one turn to the next, the prefix breaks and
    every turn repays the prefill (~1 s, measured 2026-07-22). Dict ordering
    is the classic way this happens."""
    ws = tmp_path / "ws"
    ws.mkdir()
    session = {"workspace": str(ws), "toolset": "base"}
    first = json.dumps(factory._session_tools(session), ensure_ascii=False)
    second = json.dumps(factory._session_tools(session), ensure_ascii=False)
    assert first == second
    names = [t["function"]["name"] for t in factory._session_tools(session)]
    assert factory_mcp._agent_system(str(ws), names) == \
        factory_mcp._agent_system(str(ws), names)


# ---- carnet: remember/recall/log_wall handled by the harness ---------------

WORKSPACE = "unused"  # remember/log_wall never touch the workspace


@pytest.fixture
def session_id(factory, tmp_path):
    return _agent(factory, "auto", tmp_path)


def test_remember_appends_note_message(factory, session_id):
    result = factory._handle_carnet_tool(
        session_id, "remember", {"note": "VAE gated"}, WORKSPACE)
    assert result == ("note", "VAE gated")


def test_remember_runs_even_in_approve_mode(factory, tmp_path, monkeypatch):
    """Carnet tools are read-tools: `approve` mode must never gate them."""
    fake = _fake_agent_stream([
        [("tool_call", {"name": "remember", "arguments": {"note": "ok"}})],
        [("content", "fini")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)

    events = list(factory.agent_reply(sid, "note ca"))

    assert "approval_needed" not in [k for k, _ in events]
    assert events[-1][0] == "done"


# ---- regressions: 7db19cdc glued the nudge into the stored output and had
# carnet tools append twice, corrupting detail_of/from_session -----------


def test_failure_nudge_is_a_separate_message_not_glued(
        factory, session_id, monkeypatch):
    monkeypatch.setattr(factory_mcp.agent_tools, "run",
                        lambda name, args, ws: "exit 1\n403 gated")
    call = {"name": "run_command", "arguments": {"command": "curl gated"}}

    list(factory._run_call(session_id, call, WORKSPACE))

    msgs = factory.chats.get(session_id)["messages"]
    tool_msg, nudge_msg = msgs[-2], msgs[-1]
    assert tool_msg["tool_name"] == "run_command"
    assert tool_msg["content"] == "exit 1\n403 gated"
    assert factory_mcp.REFLECT_ANNOTATION not in tool_msg["content"]
    assert workspace_memory.detail_of(tool_msg["content"]) == "403 gated"
    assert nudge_msg["tool_name"] == "system"
    assert factory_mcp.REFLECT_ANNOTATION in nudge_msg["content"]


def test_carnet_tool_appends_single_message(factory, session_id):
    before = len(factory.chats.get(session_id)["messages"])
    call = {"name": "remember", "arguments": {"note": "VAE gated"}}

    list(factory._run_call(session_id, call, WORKSPACE))

    msgs = factory.chats.get(session_id)["messages"]
    assert len(msgs) == before + 1
    assert msgs[-1]["tool_name"] == "note"
    assert msgs[-1]["content"] == "VAE gated"


def test_from_session_survives_a_carnet_message_in_the_turn(
        factory, tmp_path, monkeypatch):
    # One assistant turn calls remember then a failing run_command: the
    # carnet append used to be a second, unpaired tool message that drained
    # an unrelated pending call and dropped the run_command failure below.
    fake = _fake_agent_stream([
        [("tool_call", {"name": "remember", "arguments": {"note": "trying X"}}),
         ("tool_call", {"name": "run_command", "arguments": {"command": _DEAD}})],
        [("content", "ca ne marche pas")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "essaie"))

    session = factory.chats.get(sid)
    result = workspace_memory.from_session(session, now=0)
    assert len(result) == 1
    assert result[0]["command"] == _DEAD
    assert result[0]["count"] == 1


# ---- carnet: the live hot slice rides at the wire tail ---------------------


def _seed_repeat(factory, session_id, cmd="dir models", n=2):
    for _ in range(n):
        factory.chats.append(session_id, {
            "role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": "run_command",
                                         "arguments": {"command": cmd}}}]})
        factory.chats.append(session_id, {
            "role": "tool", "tool_name": "run_command",
            "content": "exit 1\nnot found"})


def test_carnet_tail_is_transient_and_never_persisted(factory, session_id):
    _seed_repeat(factory, session_id)
    session = factory.chats.get(session_id)

    tail = factory._carnet_tail(session)

    assert len(tail) == 1 and tail[0]["role"] == "system"
    assert carnet.HEADER in tail[0]["content"]
    assert "cmd:dir:models" in tail[0]["content"]
    assert all(carnet.HEADER not in (m.get("content") or "")
               for m in session["messages"])


def test_carnet_tail_is_empty_when_there_is_nothing_to_show(factory, session_id):
    assert factory._carnet_tail(factory.chats.get(session_id)) == []


def test_agent_turn_sends_the_hot_slice_as_the_last_message(
        factory, tmp_path, monkeypatch):
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    _seed_repeat(factory, sid)

    list(factory.agent_reply(sid, "et maintenant ?"))

    sent = fake.state["calls"][0]["messages"]
    assert carnet.HEADER in sent[-1]["content"]
    assert sent[-2]["role"] == "user"   # the carnet lands after everything


def test_walls_and_notes_reach_memory_md(factory, tmp_path, monkeypatch):
    """No failure in this session: the wall and the note must still persist,
    or a blocker found today is re-hit tomorrow."""
    fake = _fake_agent_stream([
        [("tool_call", {"name": "log_wall", "arguments": {
            "wall": "VAE gated", "cause": "auth",
            "tried": ["officiel"], "remaining": ["mirror"]}}),
         ("tool_call", {"name": "remember",
                        "arguments": {"note": "token HF requis"}})],
        [("content", "je passe par le mirror")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "telecharge le VAE"))

    text = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "MUR VAE gated" in text
    assert workspace_memory.parse_walls(text)[0]["remaining"] == ["mirror"]
    assert workspace_memory.parse_notes(text) == ["token HF requis"]


def test_system_head_is_stable_when_the_carnet_changes(factory, session_id):
    """The head is the KV prefix: a note or a failure must not touch it."""
    head1 = factory._agent_system_head(factory.chats.get(session_id))
    factory.chats.append(session_id, {"role": "tool", "tool_name": "note",
                                      "content": "VAE gated"})
    _seed_repeat(factory, session_id)

    head2 = factory._agent_system_head(factory.chats.get(session_id))

    assert head1 == head2


# ---- plan (phase 2): set_plan / step_done, and the ticks the harness owns ---


def _plan_call(factory, session_id, steps, goal="faire la chose"):
    list(factory._run_call(session_id, {
        "name": "set_plan",
        "arguments": {"goal": goal, "steps": steps}}, WORKSPACE))


def test_set_plan_appends_a_plan_message(factory, session_id):
    _plan_call(factory, session_id,
               [{"step": "ecrire go.py", "done_when": "file:go.py"}])

    msgs = factory.chats.get(session_id)["messages"]
    assert msgs[-1]["tool_name"] == "plan"
    plan = carnet.project(msgs)["plan"]
    assert plan["goal"] == "faire la chose"
    assert plan["steps"][0]["done_when"] == "file:go.py"


def test_set_plan_without_steps_is_an_error(factory, session_id):
    name, out = factory._handle_carnet_tool(
        session_id, "set_plan", {"goal": "x", "steps": []}, WORKSPACE)
    assert out.startswith("error:")
    assert name == "set_plan"   # not stored as a plan


def test_step_done_is_refused_when_the_file_is_absent(factory, session_id,
                                                     tmp_path):
    _plan_call(factory, session_id,
               [{"step": "ecrire go.py", "done_when": "file:go.py"}])

    name, out = factory._handle_carnet_tool(
        session_id, "step_done", {"step": 1}, str(tmp_path))

    assert out.startswith("error:") and "go.py" in out
    assert carnet.project(
        factory.chats.get(session_id)["messages"])["plan"]["steps"][0]["done"] \
        is False


def test_step_done_ticks_a_prose_step(factory, session_id, tmp_path):
    _plan_call(factory, session_id,
               [{"step": "comprendre le bug", "done_when": "j'ai la cause"}])

    list(factory._run_call(session_id, {
        "name": "step_done",
        "arguments": {"step": 1, "evidence": "c'est le num_ctx"}},
        str(tmp_path)))

    plan = carnet.project(factory.chats.get(session_id)["messages"])["plan"]
    assert plan["steps"][0]["done"] is True
    assert plan["steps"][0]["evidence"] == "c'est le num_ctx"


def test_harness_ticks_a_verifiable_step_by_itself(factory, session_id,
                                                   tmp_path, monkeypatch):
    _plan_call(factory, session_id,
               [{"step": "ecrire go.py", "done_when": "file:go.py"}])
    assert carnet.project(
        factory.chats.get(session_id)["messages"])["plan"]["steps"][0]["done"] \
        is False
    (tmp_path / "go.py").write_text("print(1)", encoding="utf-8")
    monkeypatch.setattr(factory_mcp.agent_tools, "run",
                        lambda name, args, ws: "ok")

    list(factory._run_call(session_id, {"name": "list_dir", "arguments": {}},
                           str(tmp_path)))

    msgs = factory.chats.get(session_id)["messages"]
    plan = carnet.project(msgs)["plan"]
    assert plan["steps"][0]["done"] is True
    assert sum(1 for m in msgs if m.get("tool_name") == "progress") == 1


def test_harness_tick_is_written_once(factory, session_id, tmp_path,
                                      monkeypatch):
    monkeypatch.setattr(factory_mcp.agent_tools, "run",
                        lambda name, args, ws: "ok")
    _plan_call(factory, session_id,
               [{"step": "ecrire go.py", "done_when": "file:go.py"}])
    # AFTER the plan on purpose: the harness ticks what the plan produced, and
    # a file that predates the plan proves nothing (see _changed_since).
    (tmp_path / "go.py").write_text("print(1)", encoding="utf-8")

    for _ in range(3):
        list(factory._run_call(session_id,
                               {"name": "list_dir", "arguments": {}},
                               str(tmp_path)))

    msgs = factory.chats.get(session_id)["messages"]
    assert sum(1 for m in msgs if m.get("tool_name") == "progress") == 1


def _age(path, offset):
    """Move a file's mtime `offset` seconds from now, so a test asserting on
    the A.2 gate reads its own ordering rather than the clock's granularity."""
    stamp = time.time() + offset
    os.utime(str(path), (stamp, stamp))


def _step_done_call(factory, session_id, step, workspace):
    """Tick through the real path: `_run_call` is what writes the message to
    the transcript, and the transcript is what the next projection reads."""
    list(factory._run_call(session_id,
                           {"name": "step_done",
                            "arguments": {"step": step, "evidence": "done"}},
                           str(workspace)))
    # Skip what the harness appends behind the result: a refusal says "error:",
    # which earns a reflection nudge, and a granted tick can be followed by a
    # `progress` the probe ticked on its own.
    msgs = [m for m in factory.chats.get(session_id)["messages"]
            if m.get("role") == "tool"
            and m.get("tool_name") in ("step", "step_done")]
    return msgs[-1].get("tool_name"), msgs[-1].get("content")


def test_a_claim_on_an_untouched_file_is_refused(factory, session_id, tmp_path):
    # The A.2 gate, live 2026-07-25: a session ticked 5/5 steps against an
    # EMPTY diff. Every step named a file that already existed, and existence
    # was the whole check -- so "modify server.js" passed without server.js
    # ever being opened for writing.
    existing = tmp_path / "server.js"
    existing.write_text("untouched", encoding="utf-8")
    # The gate compares `st_mtime > plan time`, and the Windows clock ticks
    # every ~15 ms: on a loaded machine the write and the plan call land inside
    # the same tick and the verdict flips (one red push, 25/07). Stamp the
    # ordering the test is actually about instead of racing the scheduler.
    _age(existing, -3600)
    _plan_call(factory, session_id,
               [{"step": "modifier server.js", "done_when": "file:server.js"}])

    kind, out = _step_done_call(factory, session_id, 1, tmp_path)

    assert kind == "step_done"
    assert "error:" in out and "stays open" in out
    assert carnet.project(
        factory.chats.get(session_id)["messages"])["plan"]["steps"][0]["done"] \
        is False

    existing.write_text("edited", encoding="utf-8")
    _age(existing, +1)

    kind, _ = _step_done_call(factory, session_id, 1, tmp_path)
    assert kind == "step"


def test_reticking_a_done_step_is_a_no_op_that_says_so(factory, session_id,
                                                       tmp_path):
    # Live 2026-07-25: the agent finished, then re-ticked step 5 with identical
    # arguments every 8 s for 10 minutes and would have run to the 1 h cap.
    _plan_call(factory, session_id,
               [{"step": "ecrire go.py", "done_when": "file:go.py"},
                {"step": "ecrire stop.py", "done_when": "file:stop.py"}])
    (tmp_path / "go.py").write_text("print(1)", encoding="utf-8")

    kind, out = _step_done_call(factory, session_id, 1, tmp_path)
    assert kind == "step"          # the real tick

    kind, out = _step_done_call(factory, session_id, 1, tmp_path)
    assert kind == "step_done"     # NOT a tick: countable as an action
    assert "already ticked" in out
    assert "2" in out              # names what is still open


def test_reticking_the_last_step_points_at_the_exit(factory, session_id,
                                                    tmp_path):
    _plan_call(factory, session_id,
               [{"step": "ecrire go.py", "done_when": "file:go.py"}])
    (tmp_path / "go.py").write_text("print(1)", encoding="utf-8")
    _step_done_call(factory, session_id, 1, tmp_path)

    kind, out = _step_done_call(factory, session_id, 1, tmp_path)

    assert kind == "step_done"
    assert "answer the operator" in out


def test_a_repeated_tick_counts_as_an_action_so_stagnation_can_see_it(
        factory, session_id, tmp_path, monkeypatch):
    # The loop was invisible because `step` is BOOKKEEPING. The no-op result is
    # stored under `step_done`, which is not -- that is what lets the alert fire.
    monkeypatch.setattr(factory_mcp.agent_tools, "run",
                        lambda name, args, ws: "ok")
    _plan_call(factory, session_id,
               [{"step": "ecrire go.py", "done_when": "file:go.py"}])
    (tmp_path / "go.py").write_text("print(1)", encoding="utf-8")
    list(factory._run_call(session_id,
                           {"name": "step_done",
                            "arguments": {"step": 1, "evidence": "x"}},
                           str(tmp_path)))
    before = carnet.stagnation(factory.chats.get(session_id)["messages"])

    for _ in range(10):
        list(factory._run_call(session_id,
                               {"name": "step_done",
                                "arguments": {"step": 1, "evidence": "x"}},
                               str(tmp_path)))

    after = carnet.stagnation(factory.chats.get(session_id)["messages"])
    assert before == 0             # a real tick is a checkpoint: counter reset
    assert after >= carnet.STAGNATION_AT


def test_changed_since_ignores_files_that_predate_the_plan(tmp_path):
    # Live 2026-07-25: the 30b planned "modify backend/matchHistory.js" with
    # `done_when: file:backend/matchHistory.js`. On an existence probe all three
    # such steps ticked on a clean tree -- work marked done before any was done.
    # mtimes are set explicitly: time.time() is ~15 ms granular on Windows, so
    # a file written in the same tick as the plan would make this flaky. Live
    # the gap is seconds -- here the comparison is what is under test.
    planned_at = time.time()
    old = tmp_path / "already.js"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (planned_at - 60, planned_at - 60))
    fresh = tmp_path / "written.js"
    fresh.write_text("y", encoding="utf-8")
    os.utime(fresh, (planned_at + 60, planned_at + 60))
    probe = factory_mcp._changed_since(str(tmp_path), planned_at)

    assert probe("already.js") is False   # existed before the plan: proves nothing
    assert probe("written.js") is True    # the plan produced it
    assert probe("nope.js") is False


def test_changed_since_ticks_a_file_the_agent_rewrites(tmp_path):
    # The other half: modifying a pre-existing file IS progress, and the probe
    # has to see it the moment the agent writes.
    target = tmp_path / "server.js"
    target.write_text("before", encoding="utf-8")
    planned_at = time.time()
    # Pin the "already there" mtime instead of assuming the write landed before
    # planned_at: under load the filesystem stamp can trail time.time() and the
    # probe then reads a pre-existing file as fresh progress (flake, 2026-07-25).
    os.utime(target, (planned_at - 10, planned_at - 10))
    probe = factory_mcp._changed_since(str(tmp_path), planned_at)
    assert probe("server.js") is False

    os.utime(target, (planned_at + 10, planned_at + 10))

    assert probe("server.js") is True


def test_changed_since_falls_back_to_existence_without_a_timestamp(tmp_path):
    (tmp_path / "in.txt").write_text("x", encoding="utf-8")
    probe = factory_mcp._changed_since(str(tmp_path), None)
    assert probe("in.txt") is True
    assert probe("../out.txt") is False


def test_exists_probe_stays_inside_the_workspace(tmp_path):
    (tmp_path / "in.txt").write_text("x", encoding="utf-8")
    outside = tmp_path.parent / "out.txt"
    outside.write_text("x", encoding="utf-8")
    probe = factory_mcp._exists(str(tmp_path))

    assert probe("in.txt") is True
    assert probe("*.txt") is True
    assert probe("../out.txt") is False
    assert probe(str(outside)) is False
    assert probe("nope.txt") is False


def test_carnet_tail_nudges_for_a_plan_in_auto_mode(factory, session_id):
    _seed_repeat(factory, session_id, n=carnet.PLAN_NUDGE_AT)

    tail = factory._carnet_tail(factory.chats.get(session_id))

    assert "PAS DE PLAN" in tail[0]["content"]


def test_carnet_tail_does_not_nudge_in_approve_mode(factory, tmp_path):
    sid = _agent(factory, "approve", tmp_path)
    _seed_repeat(factory, sid, n=carnet.PLAN_NUDGE_AT)

    tail = factory._carnet_tail(factory.chats.get(sid))

    assert "PAS DE PLAN" not in tail[0]["content"]


def test_carnet_tail_warns_when_the_plan_stops_advancing(factory, session_id):
    _plan_call(factory, session_id,
               [{"step": "ecrire go.py", "done_when": "file:go.py"}])
    _seed_repeat(factory, session_id, n=carnet.STAGNATION_AT)

    tail = factory._carnet_tail(factory.chats.get(session_id))

    assert "STAGNATION" in tail[0]["content"]
    assert "PLAN: faire la chose" in tail[0]["content"]


def test_system_head_states_the_plan_rule_and_stays_stable(factory, session_id):
    head1 = factory._agent_system_head(factory.chats.get(session_id))
    assert "set_plan" in head1

    _plan_call(factory, session_id, [{"step": "a", "done_when": "b"}])

    assert factory._agent_system_head(factory.chats.get(session_id)) == head1


# ---- phase 3: the autonomy contract (ask_operator / request_resource) --------


def test_ask_operator_needs_a_question(factory, session_id):
    name, out = factory._handle_carnet_tool(
        session_id, "ask_operator", {"question": "   "}, WORKSPACE)
    assert out.startswith("error:") and name == "ask_operator"


def test_ask_operator_hands_back_and_ends_the_turn(factory, tmp_path,
                                                   monkeypatch):
    fake = _fake_agent_stream([
        [("tool_call", {"name": "ask_operator",
                        "arguments": {"question": "fp16 ou fp8 ?",
                                      "options": ["fp16", "fp8"]}})],
        [("content", "unreachable")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "installe le stack"))

    kinds = [k for k, _ in events]
    assert "handback" in kinds and "done" not in kinds
    hb = next(v for k, v in events if k == "handback")
    assert hb["kind"] == "question" and hb["question"] == "fp16 ou fp8 ?"
    assert hb["options"] == ["fp16", "fp8"]
    assert fake.state["i"] == 1   # the turn stopped; the 2nd turn never streamed


def test_ask_operator_drops_calls_queued_behind_it(factory, tmp_path,
                                                    monkeypatch):
    monkeypatch.setattr(factory_mcp.agent_tools, "run",
                        lambda name, args, ws: "ran")
    sid = _agent(factory, "auto", tmp_path)
    factory.chats.set_pending_calls(sid, [
        {"name": "ask_operator", "arguments": {"question": "on fait quoi ?"}},
        {"name": "run_command", "arguments": {"command": "rm -rf /"}}])

    events = []
    gen = factory._drain_calls(sid)
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        status = stop.value

    assert status == "handback"
    assert factory.chats.get(sid)["pending_calls"] == []
    assert "run_command" not in [e[1].get("name") for e in events
                                 if e[0] == "tool_call"]


def test_request_resource_refused_without_a_logged_wall(factory, session_id):
    name, out = factory._handle_carnet_tool(
        session_id, "request_resource", {"resource": "HF token"}, WORKSPACE)
    assert out.startswith("error:") and "log_wall" in out
    assert name == "request_resource"   # not a handback, the turn goes on


def test_request_resource_accepted_after_a_dead_end_wall(factory, session_id):
    list(factory._run_call(session_id, {"name": "log_wall", "arguments": {
        "wall": "VAE gated", "cause": "auth required",
        "tried": ["anonymous download"], "remaining": []}}, WORKSPACE))

    name, out = factory._handle_carnet_tool(
        session_id, "request_resource",
        {"resource": "HF token", "why": "the VAE is gated"}, WORKSPACE)

    assert name == "handback"
    hb = json.loads(out)
    assert hb["kind"] == "resource" and hb["resource"] == "HF token"


def test_system_head_states_the_autonomy_contract(factory, session_id):
    head = factory._agent_system_head(factory.chats.get(session_id))
    assert "ask_operator" in head and "request_resource" in head


# ---- project map ----

def test_git_entries_reads_paths_docs_and_blobs(project):
    entries, sources, blobs = factory_mcp._git_entries(str(project))
    assert [e["path"] for e in entries] == ["calc.py", "elo.py"]
    assert entries[0]["lines"] == 2          # "x = 1\n"
    assert entries[0]["doc"] == ""           # no docstring in the fixture
    assert blobs["calc.py"] == entries[0]["blob"]
    assert "x = 1" in sources["calc.py"]


def test_git_entries_reads_the_module_docstring(project):
    (project / "documented.py").write_text('"""Does a thing."""\nY = 2\n')
    git(project, "add", "-A")
    git(project, "commit", "-qm", "doc")
    entries, _sources, _blobs = factory_mcp._git_entries(str(project))
    doc = {e["path"]: e["doc"] for e in entries}
    assert doc["documented.py"] == "Does a thing."


def test_git_entries_returns_none_outside_a_repo(tmp_path):
    plain = tmp_path / "nogit"
    plain.mkdir()
    assert factory_mcp._git_entries(str(plain)) is None


def test_walk_entries_is_bounded(tmp_path):
    plain = tmp_path / "nogit"
    plain.mkdir()
    for i in range(50):
        (plain / "f{}.py".format(i)).write_text("x = 1\n")
    entries, sources, blobs = factory_mcp._walk_entries(str(plain))
    assert len(entries) == 50
    assert len(entries) <= factory_mcp.MAX_WALK_FILES
    assert blobs == {}                       # no git, no anchor
    assert set(sources) == {e["path"] for e in entries}


def test_map_block_is_snapshotted_per_session(factory, project):
    first = factory._map_block("c_1", str(project))
    assert "calc.py" in first
    (project / "calc.py").write_text('"""Now documented."""\nx = 1\n')
    git(project, "add", "-A")
    git(project, "commit", "-qm", "change")
    assert factory._map_block("c_1", str(project)) == first
    assert factory._map_block("c_2", str(project)) != first


def test_map_block_appears_in_the_system_head(factory, project):
    head = factory._agent_system_head(
        {"session_id": "c_1", "workspace": str(project), "model": "m"},
        tools=[])
    assert project_map.HEADER in head
    assert "calc.py (2L)" in head


def test_map_block_survives_a_broken_workspace(factory):
    assert factory._map_block("c_3", "/does/not/exist") == ""


# ---- annotation cycle ----

def _tc(name, **args):
    return {"function": {"name": name, "arguments": json.dumps(args)}}


def _read_call(path):
    return {"role": "assistant", "tool_calls": [_tc("read_file", path=path)]}


class _Replies:
    """Stands in for llama_client.chat: one canned reply, counted calls."""
    def __init__(self, reply=""):
        self.reply, self.calls = reply, 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return {"content": self.reply}


def test_touched_files_takes_reads_and_edits_newest_first():
    messages = [_read_call("a.py"),
                {"role": "assistant",
                 "tool_calls": [_tc("edit_file", path="b.py")]}]
    assert factory_mcp._touched_files(messages) == ["b.py", "a.py"]


def test_touched_files_ignores_listing_and_searching():
    messages = [{"role": "assistant",
                 "tool_calls": [_tc("list_dir", path="harness"),
                                _tc("search", pattern="x")]}]
    assert factory_mcp._touched_files(messages) == []


def test_touched_files_deduplicates():
    assert factory_mcp._touched_files(
        [_read_call("a.py"), _read_call("a.py")]) == ["a.py"]


def test_should_annotate_only_on_a_step_boundary_or_a_full_batch():
    step = {"role": "assistant", "tool_calls": [_tc("step_done", step=1)]}
    assert factory_mcp._should_annotate([step], ["a.py"]) is True
    assert factory_mcp._should_annotate([_read_call("a.py")], ["a.py"]) is False
    many = ["f{}.py".format(i) for i in range(project_map.MAX_NOTES_PER_STEP)]
    assert factory_mcp._should_annotate([_read_call("a.py")], many) is True


def _annotating_session(project, reply, monkeypatch):
    """A session that read calc.py and closed a step, with the model stubbed."""
    stub = _Replies(reply)
    monkeypatch.setattr(factory_mcp.llama_client, "chat", stub)
    session = {
        "session_id": "c_ann", "workspace": str(project), "model": "m",
        "messages": [_read_call("calc.py"),
                     {"role": "assistant",
                      "tool_calls": [_tc("step_done", step=1)]}]}
    return session, stub


def test_annotate_persists_a_verified_note(factory, project, monkeypatch):
    session, stub = _annotating_session(
        project, "calc.py : x vaut 1, pas de configuration", monkeypatch)
    factory._annotate_from("c_ann", session)
    assert stub.calls == 1
    notes = project_map.parse(
        (project / "MEMORY.md").read_text(encoding="utf-8"))
    assert [n["path"] for n in notes] == ["calc.py"]
    assert notes[0]["text"] == "x vaut 1, pas de configuration"


def test_annotate_rejects_an_unevidenced_note_and_records_a_lesson(
        factory, project, monkeypatch):
    session, _stub = _annotating_session(
        project, "calc.py : Backend FastAPI", monkeypatch)
    factory._annotate_from("c_ann", session)
    memory = project / "MEMORY.md"
    assert not memory.exists() or project_map.parse(
        memory.read_text(encoding="utf-8")) == []
    patterns = [l["pattern"] for l in factory.chats.load_lessons()]
    assert "unevidenced_annotation" in patterns


def test_annotate_skips_a_file_that_already_has_a_fresh_note(
        factory, project, monkeypatch):
    _entries, _sources, blobs = factory_mcp._git_entries(str(project))
    (project / "MEMORY.md").write_text(project_map.update("", [
        {"path": "calc.py", "text": "deja note", "blob": blobs["calc.py"],
         "last_seen": 0.0}]), encoding="utf-8")
    session, stub = _annotating_session(project, "ignored", monkeypatch)
    factory._annotate_from("c_ann", session)
    assert stub.calls == 0          # nothing to ask, so no generation


def test_annotate_leaves_prose_above_the_markers_untouched(
        factory, project, monkeypatch):
    (project / "MEMORY.md").write_text("# MEMORY\n\nEcrit par Martin.\n",
                                       encoding="utf-8")
    session, _stub = _annotating_session(
        project, "calc.py : x vaut 1", monkeypatch)
    factory._annotate_from("c_ann", session)
    text = (project / "MEMORY.md").read_text(encoding="utf-8")
    assert text.startswith("# MEMORY\n\nEcrit par Martin.\n")


def test_system_prompt_points_at_the_map(factory, project):
    head = factory._agent_system_head(
        {"session_id": "c_9", "workspace": str(project), "model": "m"},
        tools=[])
    assert "authoritative for where things are" in head


# ---- session trace ----

def test_a_tool_call_lands_in_the_session_trace(factory, project):
    sid = _agent(factory, tmp=project)
    factory._ctx_running[sid] = 100
    list(factory._run_call(sid, {"name": "list_dir",
                                 "arguments": {"path": "."}},
                           str(project)))
    events = session_log.read(factory._log(sid).path)
    tools = [e for e in events if e["kind"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["name"] == "list_dir"
    assert tools[0]["ms"] >= 0
    assert tools[0]["out_chars"] > 0
    assert tools[0]["ctx_est"] > 100          # the read grew the estimate


def test_a_replayed_call_is_flagged_in_the_trace(factory, project):
    sid = _agent(factory, tmp=project)
    call = {"name": "list_dir", "arguments": {"path": "."}}
    for _ in range(2):
        list(factory._run_call(sid, call, str(project)))
    events = session_log.read(factory._log(sid).path)
    assert [e["what"] for e in events if e["kind"] == "anomaly"] == \
        ["replayed_call"]


def test_a_failing_tool_is_flagged_in_the_trace(factory, project):
    sid = _agent(factory, tmp=project)
    list(factory._run_call(sid, {"name": "read_file",
                                 "arguments": {"path": "nope.py"}},
                           str(project)))
    events = session_log.read(factory._log(sid).path)
    assert "tool_error" in [e.get("what") for e in events]


def test_log_turn_records_the_window_and_its_overrun(factory):
    factory._log_turn("c_t", num_ctx=1000,
                      metrics={"prompt_count": 1200, "eval_count": 30,
                               "done_reason": "length"},
                      info={"est_tokens": 900, "dropped": 2, "evicted": 4},
                      compacted=True)
    events = session_log.read(factory._log("c_t").path)
    end = [e for e in events if e["kind"] == "turn_end"][0]
    assert end["ctx_pct"] == 123
    assert end["dropped"] == 2 and end["evicted"] == 4
    what = {e.get("what") for e in events}
    assert {"context_overrun", "output_truncated", "messages_dropped",
            "compacted"} <= what
    # the running estimate is reset to what the prompt actually measured
    assert factory._ctx_running["c_t"] == 900


def test_chat_trace_exposes_the_plan_and_the_incidents(factory, project):
    sid = _agent(factory, tmp=project)
    list(factory._run_call(sid, {"name": "set_plan", "arguments": {
        "goal": "faire la chose",
        "steps": [{"step": "un", "done_when": "file:calc.py"},
                  {"step": "deux", "done_when": "file:elo.py"}]}},
        str(project)))
    list(factory._run_call(sid, {"name": "read_file",
                                 "arguments": {"path": "nope.py"}},
                           str(project)))
    trace = factory.chat_trace(sid)
    assert trace["plan"]["goal"] == "faire la chose"
    assert len(trace["plan"]["steps"]) == 2
    assert trace["summary"]["calls"] == 2      # set_plan counts too
    assert trace["summary"]["errors"] == 1
    assert trace["summary"]["tool_calls"]["set_plan"] == 1
    assert trace["anomalies"][0]["what"] == "tool_error"


def test_chat_trace_works_before_anything_has_happened(factory, project):
    sid = _agent(factory, tmp=project)
    trace = factory.chat_trace(sid)
    assert trace["plan"] is None
    assert trace["summary"]["calls"] == 0
    assert trace["anomalies"] == []


# ---- the trace teaches: what a session COST becomes a lesson ----

def test_a_session_that_replays_calls_learns_it_from_its_trace(
        factory, project, monkeypatch):
    # session_audit sees "wasted_calls: 3" and can price none of it. The trace
    # can, and the lesson names the file (audit 2026-07-27: app.js read 29x).
    read = ("tool_call", {"name": "read_file", "arguments": {"path": "calc.py"}})
    fake = _fake_agent_stream([[read], [read], [read], [read],
                               [("content", "fini")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", project)

    list(factory.agent_reply(sid, "lis calc.py"))

    learned = {l["pattern"]: l for l in factory.chats.load_lessons()}
    assert "replayed_calls" in learned
    assert "calc.py" in learned["hot_target"]["suggestion"]


def test_a_trace_lesson_is_banked_once_per_session(factory, project,
                                                   monkeypatch):
    # The analysis re-reads the WHOLE trace at every turn: without per-session
    # banking, one replayed call reads as ten sessions after ten quiet turns.
    read = ("tool_call", {"name": "read_file", "arguments": {"path": "calc.py"}})
    fake = _fake_agent_stream([[read], [read], [read], [read],
                               [("content", "fini")], [("content", "de rien")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", project)

    list(factory.agent_reply(sid, "lis calc.py"))
    after_one = {l["pattern"]: l["count"] for l in factory.chats.load_lessons()}
    list(factory.agent_reply(sid, "merci"))
    after_two = {l["pattern"]: l["count"] for l in factory.chats.load_lessons()}

    assert after_one["replayed_calls"] == 1
    assert after_two == after_one


def test_a_clean_session_learns_nothing_from_its_trace(factory, project,
                                                       monkeypatch):
    fake = _fake_agent_stream([
        [("tool_call", {"name": "read_file", "arguments": {"path": "calc.py"}})],
        [("content", "x vaut 1")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", project)

    list(factory.agent_reply(sid, "lis calc.py"))

    assert factory.chats.load_lessons() == []


def test_chat_trace_carries_the_priced_findings(factory, project):
    call = {"name": "read_file", "arguments": {"path": "calc.py"}}
    sid = _agent(factory, tmp=project)
    for _ in range(4):
        list(factory._run_call(sid, call, str(project)))

    trace = factory.chat_trace(sid)
    codes = [f["code"] for f in trace["findings"]]
    assert "replayed_calls" in codes and "hot_target" in codes
    assert trace["cost"]["tool_s"] >= 0.0


# ---- phases: where the session is, and the one thing it may not skip ----

def test_a_write_with_no_plan_is_refused_once_the_session_is_underway(
        factory, project):
    sid = _agent(factory, "auto", project)
    _seed_repeat(factory, sid, n=phases.BLOCK_AFTER_ACTIONS)

    events = list(factory._run_call(
        sid, {"name": "write_file",
              "arguments": {"path": "new.py", "content": "x = 1"}},
        str(project)))

    output = [e[1]["output"] for e in events if e[0] == "tool_result"][0]
    assert "set_plan" in output
    assert not (project / "new.py").exists()      # the write did not happen
    # A refusal is an incident: it must be visible in the trace, not only to
    # the model that read the refusal text.
    assert "gate_refused" in [e.get("what") for e in
                              session_log.read(factory._log(sid).path)]


def test_the_refusal_is_a_tool_result_the_agent_can_read(factory, project):
    sid = _agent(factory, "auto", project)
    _seed_repeat(factory, sid, n=phases.BLOCK_AFTER_ACTIONS)

    list(factory._run_call(sid, {"name": "write_file",
                                 "arguments": {"path": "new.py",
                                               "content": "x"}},
                           str(project)))

    last = factory.chats.get(sid)["messages"][-1]
    assert last["role"] == "tool" and last["tool_name"] == "write_file"
    assert "set_plan" in last["content"]


def test_a_write_after_a_plan_runs(factory, project):
    sid = _agent(factory, "auto", project)
    _seed_repeat(factory, sid, n=phases.BLOCK_AFTER_ACTIONS)
    list(factory._run_call(sid, {"name": "set_plan", "arguments": {
        "goal": "ecrire", "steps": [{"step": "un", "done_when": "file:new.py"}]}},
        str(project)))

    list(factory._run_call(sid, {"name": "write_file",
                                 "arguments": {"path": "new.py",
                                               "content": "x = 1"}},
                           str(project)))

    assert (project / "new.py").read_text() == "x = 1"


def test_the_carnet_tail_names_the_phase(factory, project):
    sid = _agent(factory, "auto", project)
    list(factory._run_call(sid, {"name": "set_plan", "arguments": {
        "goal": "ecrire", "steps": [{"step": "un", "done_when": "file:new.py"},
                                    {"step": "deux", "done_when": "file:b.py"}]}},
        str(project)))
    list(factory._run_call(sid, {"name": "write_file",
                                 "arguments": {"path": "new.py",
                                               "content": "x = 1"}},
                           str(project)))

    tail = factory._carnet_tail(factory.chats.get(sid))

    assert "PHASE implement" in tail[0]["content"]


def test_the_tail_quotes_back_what_this_session_already_wasted(factory,
                                                              project):
    # The system prompt is frozen for the session: without the tail, a session
    # cannot be told about its own waste until it is over.
    sid = _agent(factory, "auto", project)
    call = {"name": "read_file", "arguments": {"path": "calc.py"}}
    for _ in range(4):
        list(factory._run_call(sid, call, str(project)))

    tail = factory._carnet_tail(factory.chats.get(sid))

    assert "COUT DEJA PAYE" in tail[0]["content"]
    assert "calc.py" in tail[0]["content"]


def test_chat_trace_names_the_phase(factory, project):
    sid = _agent(factory, tmp=project)
    assert factory.chat_trace(sid)["phase"] == "explore"


# ---- the rules of engagement are a snapshot too ----

def test_the_workspace_instruction_file_is_snapshotted_for_the_session(
        factory, project):
    # The KV prefix must not move mid-session -- and this file lives in the very
    # directory the agent edits, so it is the one most likely to move. Lessons,
    # memory and the project map were snapshotted for this reason; the rules of
    # engagement were still read from disk every turn.
    (project / "CLAUDE.md").write_text("Regle: toujours le venv.\n",
                                       encoding="utf-8")
    sid = _agent(factory, tmp=project)
    head1 = factory._agent_system_head(factory.chats.get(sid))

    (project / "CLAUDE.md").write_text("Regle: plus jamais le venv.\n",
                                       encoding="utf-8")

    assert factory._agent_system_head(factory.chats.get(sid)) == head1
    assert "toujours le venv" in head1


def test_a_new_session_reads_the_new_instructions(factory, project):
    (project / "CLAUDE.md").write_text("Regle: A\n", encoding="utf-8")
    sid = _agent(factory, tmp=project)
    factory._agent_system_head(factory.chats.get(sid))

    (project / "CLAUDE.md").write_text("Regle: B\n", encoding="utf-8")
    other = _agent(factory, tmp=project)

    assert "Regle: B" in factory._agent_system_head(factory.chats.get(other))


def test_the_global_agent_md_is_snapshotted_for_the_session(
        factory, project, tmp_path, monkeypatch):
    # This is the file an agent.md optimiser would rewrite: rewriting it must
    # never change the rules under a session that is already running.
    rules = tmp_path / "agent.md"
    rules.write_text("Regle globale: commiter chaque etape.\n", encoding="utf-8")
    monkeypatch.setattr(factory_mcp, "GLOBAL_AGENT_MD", rules)
    sid = _agent(factory, tmp=project)
    head1 = factory._agent_system_head(factory.chats.get(sid))

    rules.write_text("Regle globale: ne rien commiter.\n", encoding="utf-8")

    assert factory._agent_system_head(factory.chats.get(sid)) == head1
    assert "commiter chaque etape" in head1


def test_agent_does_not_relance_when_the_plan_is_finished(
        factory, tmp_path, monkeypatch):
    # A finished plan and a turn of prose is a final answer. Ending there is
    # the behaviour, not the bug.
    fake = _fake_agent_stream([
        [("tool_call", {"name": "step_done",
                        "arguments": {"step": 1, "evidence": "fait"}})],
        [("content", "Tout est fait.")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    _seed_plan(factory, sid, ["une seule etape"])

    events = list(factory.agent_reply(sid, "vas-y"))

    assert len(fake.state["calls"]) == 2
    assert events[-1][0] == "done"
    assert not [p for k, p in events if k == "notice"]


def test_agent_does_not_relance_without_a_plan(factory, tmp_path, monkeypatch):
    # Progress is defined against the plan (spec C.2) and so is continuation:
    # relancing a planless session would have no exit at all.
    fake = _fake_agent_stream([[("content", "Voila ma reponse.")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "une question simple"))

    assert len(fake.state["calls"]) == 1
    assert events[-1][0] == "done"
    assert not [p for k, p in events if k == "notice"]


def test_agent_does_not_relance_in_approve_mode(factory, tmp_path, monkeypatch):
    # In `approve` the operator answers every call, so a turn handed back is
    # not a turn lost.
    fake = _fake_agent_stream([[("content", "Je vais patcher le fichier.")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)
    _seed_plan(factory, sid, ["patcher le fichier"])

    events = list(factory.agent_reply(sid, "vas-y"))

    assert len(fake.state["calls"]) == 1
    assert events[-1][0] == "done"
    assert not [p for k, p in events if k == "notice"]

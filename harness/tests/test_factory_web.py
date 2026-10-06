"""The web UI server: a third client of the Factory facade.

Boundary under test: HTTP in, JSON (or static bytes) out. The Factory behind it
is exercised through the same fake-spawn fixture the CLI tests use."""
import json
import socket
import struct
import subprocess
import pathlib
import threading
import time
import urllib.error
import urllib.request

import pytest

import factory_mcp
import factory_web
from conftest import write_config
from factory_mcp import Factory
from factory_web import FactoryWebServer
from gpu_lock import GpuLock

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
    git(root, "add", "-A")
    git(root, "commit", "-qm", "seed")
    return root


class _StubManager:
    """Minimal LlamaServerManager for the web boundary: no real server."""
    def __init__(self):
        self.current = None
        self.url = "http://stub"

    def ensure(self, model):
        self.current = (model, 1)
        return self.url

    def shutdown(self):
        self.current = None


class _NoFireTimer:
    """Idle timer that never fires on its own: the web tests never wait it out."""
    def __init__(self, delay, fn):
        self.fn = fn

    def start(self):
        pass

    def cancel(self):
        pass


@pytest.fixture
def factory(tmp_path, project):
    cfg = write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                       paths={"demo": project})
    spawned = []
    f = Factory(tmp_path / "jobs", cfg, spawn_fn=lambda job_id: spawned.append(job_id) or 4242)
    f.spawned = spawned
    # Chat/agent ride an injected llama-server stub; the web boundary is what
    # is under test here, not the runtime.
    f._llama = _StubManager()
    f._llama_cfg = {"chat_idle_s": 600, "models": {}, "port": 8091}
    f.timer_fn = _NoFireTimer
    return f


@pytest.fixture
def server(factory):
    srv = FactoryWebServer(("127.0.0.1", 0), factory, token="t0k3n")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, body=None, token=None, raw=False):
    url = "http://127.0.0.1:{}{}".format(srv.server_address[1], path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Factory-Token", token)
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = e.read()
        return e.code, payload if raw else json.loads(payload.decode("utf-8"))


def test_unknown_path_is_a_json_404(server):
    code, body = request(server, "GET", "/nope")
    assert code == 404
    assert body["error"] == "NotFound"


def test_index_embeds_the_token(server):
    code, body = request(server, "GET", "/", raw=True)
    assert code == 200
    assert b"t0k3n" in body
    assert b"{{TOKEN}}" not in body


def test_client_abort_mid_write_is_swallowed():
    # A browser refresh aborts the socket mid-response (WinError 10053); the
    # handler must not answer the dead socket with a 500, nor let it traceback.
    import types
    from factory_web import Handler
    h = Handler.__new__(Handler)
    h.headers = {"Host": "127.0.0.1"}
    h.path = "/"
    h.server = types.SimpleNamespace(token="t0k3n")
    h._bytes = lambda *a, **k: (_ for _ in ()).throw(
        ConnectionAbortedError(10053, "aborted"))
    h._route("GET")  # must return silently


def test_a_viewer_hanging_up_is_not_reported_as_a_server_error(server, capsys):
    # Since turns outlive connections, a client going away is routine -- it
    # must not print a traceback. Anything else still must.
    server.handle_error(None, ("127.0.0.1", 1234))  # no exception in flight
    capsys.readouterr()
    try:
        raise ConnectionResetError(10054, "aborted by the peer")
    except ConnectionResetError:
        server.handle_error(None, ("127.0.0.1", 1234))
    assert capsys.readouterr().err == ""

    try:
        raise ValueError("a real bug")
    except ValueError:
        server.handle_error(None, ("127.0.0.1", 1234))
    assert "a real bug" in capsys.readouterr().err


def test_static_serves_files_from_web_dir_only(server):
    code, _ = request(server, "GET", "/static/index.html", raw=True)
    assert code == 200
    code, _ = request(server, "GET", "/static/..%2Ffactory_web.py")
    assert code == 404


def test_post_without_token_is_403(server):
    code, body = request(server, "POST", "/api/jobs", body={})
    assert code == 403
    assert body["error"] == "Forbidden"
    code, _ = request(server, "POST", "/api/jobs", body={}, token="wrong")
    assert code == 403


def test_projects_lists_config_keys(server):
    code, body = request(server, "GET", "/api/projects")
    assert (code, body) == (200, {"projects": ["demo"]})


def test_delegate_queues_a_job_and_jobs_lists_it(server, factory):
    code, body = request(server, "POST", "/api/jobs", token="t0k3n", body={
        "project": "demo", "goal": "make it work",
        "tests": {"test_x.py": TEST_SRC}, "target_files": ["calc.py"]})
    assert code == 200
    job_id = body["job_id"]
    assert factory.spawned == [job_id]
    code, body = request(server, "GET", "/api/jobs")
    assert code == 200
    assert [j["job_id"] for j in body["jobs"]] == [job_id]
    assert body["jobs"][0]["goal"] == "make it work"


def test_delegate_refusal_is_a_400_with_the_reason(server):
    code, body = request(server, "POST", "/api/jobs", token="t0k3n", body={
        "project": "demo", "goal": "g", "tests": {}, "target_files": ["calc.py"]})
    assert code == 400
    assert body["error"] == "NoTests"


def test_job_lifecycle_endpoints(server, factory):
    _, body = request(server, "POST", "/api/jobs", token="t0k3n", body={
        "project": "demo", "goal": "g",
        "tests": {"test_x.py": TEST_SRC}, "target_files": ["calc.py"]})
    job_id = body["job_id"]

    code, body = request(server, "GET", "/api/jobs/{}".format(job_id))
    assert (code, body["status"]) == (200, "queued")

    code, body = request(server, "GET", "/api/jobs/{}/result".format(job_id))
    assert (code, body["ready"]) == (200, False)

    factory.store.append_log(job_id, "line1\nline2\n")
    code, body = request(server, "GET", "/api/jobs/{}/log?tail=1".format(job_id))
    assert (code, body["log"]) == (200, "line2")

    code, body = request(server, "POST", "/api/jobs/{}/cancel".format(job_id),
                         token="t0k3n", body={})
    assert (code, body["status"]) == (200, "cancelled")


def test_stats_answers_with_a_body(server):
    """Le handler `return`ait son dict au lieu de l'écrire : la connexion se
    fermait sans un octet (ERR_EMPTY_RESPONSE dans la console) et la barre de
    chiffres des Jobs n'a jamais rien affiché. Aucun test ne la couvrait."""
    request(server, "POST", "/api/jobs", token="t0k3n", body={
        "project": "demo", "goal": "g",
        "tests": {"test_x.py": TEST_SRC}, "target_files": ["calc.py"]})
    code, body = request(server, "GET", "/api/stats")
    assert code == 200
    assert body["total_jobs"] == 1
    for key in ("total_tokens", "total_cost", "total_time_ms", "status_counts",
                "model_stats", "throughput_tps"):
        assert key in body


def test_stats_counts_the_states_the_store_really_uses(server, factory):
    """« pending » et « done » n'existent nulle part dans `job_store` : compté
    sur ce vocabulaire inventé, chaque état réel tombait à zéro."""
    _, body = request(server, "POST", "/api/jobs", token="t0k3n", body={
        "project": "demo", "goal": "g",
        "tests": {"test_x.py": TEST_SRC}, "target_files": ["calc.py"]})
    request(server, "POST", "/api/jobs/{}/cancel".format(body["job_id"]),
            token="t0k3n", body={})
    _, stats = request(server, "GET", "/api/stats")
    assert stats["status_counts"] == {"cancelled": 1}


def test_job_metrics_read_the_shape_job_result_really_has():
    """Les compteurs sont à la racine (`attempts`, `total_seconds`) ; lus sous
    une clé `result`/`duration_s` inexistante, ils rendaient zéro sur TOUS les
    jobs — une barre de chiffres qui n'a jamais rien mesuré."""
    class FakeFactory:
        def job_result(self, job_id):
            return {"status": "succeeded", "total_seconds": 261.4, "attempts": [
                {"model": "qwen2.5-coder:7b", "eval_count": 209, "prompt_count": 800},
                {"model": "qwen2.5-coder:7b", "eval_count": 91},  # job d'avant
            ]}

    m = factory_web._compute_job_metrics("j_x", FakeFactory())
    assert m["attempt_count"] == 2
    assert m["total_tokens"] == 209 + 800 + 91
    assert m["total_time_ms"] == 261400


def test_unknown_job_is_404(server):
    code, body = request(server, "GET", "/api/jobs/j_00000000")
    assert (code, body["error"]) == (404, "JobNotFound")


def test_repo_files_lists_tracked_files(server):
    code, body = request(server, "GET", "/api/repo-files?project=demo")
    assert (code, body) == (200, {"files": ["calc.py"]})


def test_repo_files_unknown_project_is_400(server):
    code, body = request(server, "GET", "/api/repo-files?project=nope")
    assert (code, body["error"]) == (400, "ProjectNotAllowed")


def test_apply_route_applies_a_succeeded_job(server, factory, project):
    _, body = request(server, "POST", "/api/jobs", token="t0k3n", body={
        "project": "demo", "goal": "g",
        "tests": {"test_x.py": TEST_SRC}, "target_files": ["calc.py"]})
    job_id = body["job_id"]
    (project / "calc.py").write_text("x = 2\n")
    diff = subprocess.run(["git", "-C", str(project), "diff"],
                          capture_output=True, text=True, check=True).stdout
    git(project, "checkout", "--", "calc.py")
    factory.store.write_result(job_id, {"status": "succeeded", "diff": diff})

    code, body = request(server, "POST", "/api/jobs/{}/apply".format(job_id),
                         token="t0k3n", body={})

    assert (code, body["status"]) == (200, "applied")
    assert (project / "calc.py").read_text() == "x = 2\n"


def test_apply_refusal_is_a_409_with_the_reason(server, factory):
    _, body = request(server, "POST", "/api/jobs", token="t0k3n", body={
        "project": "demo", "goal": "g",
        "tests": {"test_x.py": TEST_SRC}, "target_files": ["calc.py"]})
    job_id = body["job_id"]
    factory.store.write_result(job_id, {"status": "failed", "diff": ""})

    code, body = request(server, "POST", "/api/jobs/{}/apply".format(job_id),
                         token="t0k3n", body={})

    assert code == 409
    assert body["error"] == "ToolError"


def test_runtime_status_route(server, factory):
    code, body = request(server, "GET", "/api/runtime")

    assert code == 200
    assert body == {"loaded": None, "held_by": None}


def test_runtime_models_route_lists_the_catalogue(server, factory, tmp_path):
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x" * 12)
    factory._llama_cfg = {"chat_idle_s": 600, "port": 8091, "models": {
        "m": {"path": str(gguf), "args": ["-ub", "2048"]}}}

    code, body = request(server, "GET", "/api/runtime/models")

    assert code == 200
    m = body["models"][0]
    assert m["name"] == "m" and m["size"] == 12 and m["missing"] is False
    assert m["args"] == ["-ub", "2048"]


def test_runtime_load_route_needs_the_token(server, factory):
    factory._llama_cfg = {"chat_idle_s": 600, "port": 8091, "models": {"m": {}}}

    code, _ = request(server, "POST", "/api/runtime/load", body={"model": "m"})
    assert code == 403

    code, body = request(server, "POST", "/api/runtime/load", token="t0k3n",
                         body={"model": "m"})
    assert (code, body["status"]) == (200, "loaded")
    assert factory._llama.current[0] == "m"


def test_runtime_unload_route_needs_the_token(server, factory):
    code, _ = request(server, "POST", "/api/runtime/unload", body={})
    assert code == 403

    code, body = request(server, "POST", "/api/runtime/unload", token="t0k3n",
                         body={})
    assert (code, body["status"]) == (200, "unloaded")


def test_draft_route_queues_a_draft_job(server, factory):
    code, body = request(server, "POST", "/api/drafts", token="t0k3n",
                         body={"project": "demo", "goal": "add slugify"})

    assert (code, body["status"]) == (200, "queued")
    assert factory.store.read_spec(body["job_id"])["kind"] == "draft"


def test_post_projects_adds_a_project_that_get_then_lists(server, tmp_path):
    newrepo = tmp_path / "newrepo"
    newrepo.mkdir()
    git(newrepo, "init", "-q", "-b", "main")

    code, body = request(server, "POST", "/api/projects", token="t0k3n",
                         body={"name": "newrepo", "path": str(newrepo)})

    assert (code, body["status"]) == (200, "added")
    code, body = request(server, "GET", "/api/projects")
    assert (code, body) == (200, {"projects": ["demo", "newrepo"]})


def test_foreign_host_header_is_rejected(server):
    # DNS rebinding reaches 127.0.0.1 carrying the attacker's Host header;
    # without this guard the page (and its token) would be readable.
    url = "http://127.0.0.1:{}/".format(server.server_address[1])
    req = urllib.request.Request(url, headers={"Host": "evil.example"})
    try:
        with urllib.request.urlopen(req) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 403


def test_index_is_not_cacheable_and_not_frameable(server):
    url = "http://127.0.0.1:{}/".format(server.server_address[1])
    with urllib.request.urlopen(url) as resp:
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["X-Frame-Options"] == "DENY"


# ---- chat ----

def _fake_chat_stream(chunks):
    """`chunks` are (kind, text) tuples; bare strings mean ("content", text)."""
    chunks = [c if isinstance(c, tuple) else ("content", c) for c in chunks]

    def stream(url, messages, **kw):
        for c in chunks:
            yield c
        return {"content": "".join(t for k, t in chunks if k == "content"),
                "tokens_per_s": 30.0, "ttft_s": 1.0, "eval_count": 5}
    return stream


def test_chat_session_crud(server, factory):
    code, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    assert code == 200 and s["session_id"].startswith("c_")

    code, listing = request(server, "GET", "/api/chats")
    assert code == 200 and listing["sessions"][0]["session_id"] == s["session_id"]

    code, got = request(server, "GET", "/api/chats/" + s["session_id"])
    assert code == 200 and got["messages"] == []

    code, upd = request(server, "POST", "/api/chats/" + s["session_id"] + "/model",
                        {"model": "m:2"}, token="t0k3n")
    assert code == 200 and upd["model"] == "m:2"

    code, gone = request(server, "POST", "/api/chats/" + s["session_id"] + "/delete",
                         {}, token="t0k3n")
    assert code == 200 and gone["deleted"] is True

    code, body = request(server, "GET", "/api/chats/" + s["session_id"])
    assert code == 404 and body["error"] == "ChatNotFound"


def test_a_session_can_be_opened_on_a_project_by_name(server, factory, project):
    """Le studio envoie un NOM, jamais un chemin : c'est ce qui empêche
    `../..` de vouloir dire quelque chose, et ça évite de faire descendre les
    chemins du serveur jusqu'au navigateur."""
    code, s = request(server, "POST", "/api/chats",
                      {"model": "m:1", "kind": "agent", "project": "demo"},
                      token="t0k3n")

    assert code == 200
    assert pathlib.Path(s["workspace"]).resolve() == project.resolve()

    _, listing = request(server, "GET", "/api/chats")
    row = next(x for x in listing["sessions"]
               if x["session_id"] == s["session_id"])
    assert row["project"] == "demo"


def test_an_unknown_project_is_refused_at_the_door(server, factory):
    code, body = request(server, "POST", "/api/chats",
                         {"model": "m:1", "kind": "agent",
                          "project": "../../etc"}, token="t0k3n")

    assert code >= 400
    assert body["error"] == "ProjectNotAllowed"
    assert "../../etc" in body["detail"]


def test_chat_rename_round_trips_through_the_api(server, factory):
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    sid = s["session_id"]

    code, upd = request(server, "POST", "/api/chats/" + sid + "/title",
                        {"title": "banc a codeur"}, token="t0k3n")
    assert code == 200 and upd["title"] == "banc a codeur"

    _, listing = request(server, "GET", "/api/chats")
    assert listing["sessions"][0]["title"] == "banc a codeur"


def test_the_listing_names_the_project_each_session_belongs_to(server, factory,
                                                               project):
    """Sans ça la bande de gauche ne peut que trier par date : le workspace est
    un chemin absolu, le nom du projet est ce dans quoi l'opérateur pense."""
    _, plain = request(server, "POST", "/api/chats", {"model": "m:1"},
                       token="t0k3n")
    _, listing = request(server, "GET", "/api/chats")
    row = next(s for s in listing["sessions"]
               if s["session_id"] == plain["session_id"])
    assert row["project"] is None  # un chat n'a pas de workspace

    # Seul factory.toml nomme un projet : un workspace hors périmètre reste
    # sans nom plutôt que d'ouvrir un groupe d'une session par bac à sable.
    assert factory._project_of(str(project)) == "demo"
    assert factory._project_of(str(project.parent / "ailleurs")) is None


def test_chat_send_streams_ndjson(server, factory, monkeypatch):
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _fake_chat_stream([("thinking", "Hmm."), "Bon", "jour"]))
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")

    code, raw = request(server, "POST", "/api/chats/" + s["session_id"] + "/messages",
                        {"content": "salut"}, token="t0k3n", raw=True)

    assert code == 200
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    assert lines[0] == {"thinking": "Hmm."}
    assert lines[1] == {"chunk": "Bon"}
    assert lines[2] == {"chunk": "jour"}
    assert lines[3]["done"] is True and lines[3]["tokens_per_s"] == 30.0
    # and the session was persisted
    _, got = request(server, "GET", "/api/chats/" + s["session_id"])
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    assert got["messages"][1]["content"] == "Bonjour"


def _gated_chat_stream(gate, before, after):
    """A reply the test can freeze mid-flight: `before` is streamed, then the
    model waits on `gate` before finishing with `after`."""
    def stream(url, messages, **kw):
        for c in before:
            yield ("content", c)
        assert gate.wait(5)
        for c in after:
            yield ("content", c)
        return {"content": "".join(before + after), "tokens_per_s": 30.0,
                "ttft_s": 1.0, "eval_count": 5}
    return stream


def _open_stream(srv, path, body, token="t0k3n"):
    """POST over a raw socket, so the test can hang up mid-stream the way a
    browser does on refresh. Returns (socket, first-bytes-reader)."""
    payload = json.dumps(body).encode("utf-8")
    head = ("POST {} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Content-Type: application/json\r\nX-Factory-Token: {}\r\n"
            "Content-Length: {}\r\n\r\n").format(path, token, len(payload))
    sock = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
    # SO_LINGER 0: close() sends a RST instead of a FIN, so the server's next
    # write really fails. A plain close leaves small writes landing in the
    # kernel buffer and the disconnect goes unnoticed -- which would make this
    # test pass for the wrong reason.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                    struct.pack("ii", 1, 0))
    sock.sendall(head.encode("utf-8") + payload)

    def read_until(needle, timeout=5):
        buf = b""
        end = time.time() + timeout
        while needle not in buf and time.time() < end:
            try:
                buf += sock.recv(65536)
            except socket.timeout:
                break
        assert needle in buf, buf
        return buf
    return sock, read_until


def _wait_for(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_a_viewer_that_hangs_up_does_not_kill_the_turn(server, factory, monkeypatch):
    # Measured 2026-07-22: a refresh mid-stream killed the turn, because the
    # turn WAS the response body. Chat lost its tail, an agent session lost its
    # task mid-way with its tool effects already applied.
    gate = threading.Event()
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _gated_chat_stream(gate, ["Bon"], ["jour"] + ["."] * 200))
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    sid = s["session_id"]

    sock, read_until = _open_stream(server, "/api/chats/{}/messages".format(sid),
                                    {"content": "salut"})
    read_until(b'"chunk": "Bon"')
    sock.close()  # the operator hits F5

    gate.set()  # the model keeps generating for nobody
    assert _wait_for(lambda: len(request(server, "GET", "/api/chats/" + sid)[1]
                               ["messages"]) == 2)
    _, got = request(server, "GET", "/api/chats/" + sid)
    assert got["messages"][1]["content"] == "Bonjour" + "." * 200
    assert "error" not in got["messages"][1]


def test_stream_reattaches_to_a_running_turn(server, factory, monkeypatch):
    # What the page does after a refresh: it re-attaches and the reply keeps
    # writing itself under the operator's eyes.
    gate = threading.Event()
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _gated_chat_stream(gate, ["Bon"], ["jour"]))
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    sid = s["session_id"]
    sock, read_until = _open_stream(server, "/api/chats/{}/messages".format(sid),
                                    {"content": "salut"})
    read_until(b'"chunk": "Bon"')
    sock.close()

    seen = []
    viewer = threading.Thread(target=lambda: seen.append(
        request(server, "GET", "/api/chats/{}/stream".format(sid), raw=True)))
    viewer.start()
    gate.set()
    viewer.join(5)
    code, raw = seen[0]
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    assert code == 200
    assert lines[0] == {"chunk": "Bon"}  # replayed from the buffer
    assert lines[1] == {"chunk": "jour"}  # then the live edge
    assert lines[-1]["done"] is True


def test_stream_takes_a_cursor(server, factory, monkeypatch):
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _fake_chat_stream(["Bon", "jour"]))
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    sid = s["session_id"]
    request(server, "POST", "/api/chats/{}/messages".format(sid),
            {"content": "salut"}, token="t0k3n", raw=True)

    _, raw = request(server, "GET", "/api/chats/{}/stream?cursor=1".format(sid),
                     raw=True)
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    assert lines[0] == {"chunk": "jour"}


def test_stream_without_a_turn_is_404(server):
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    code, body = request(server, "GET",
                         "/api/chats/{}/stream".format(s["session_id"]))
    assert (code, body["error"]) == (404, "NoTurn")


def test_a_second_send_while_a_turn_runs_is_refused(server, factory, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _gated_chat_stream(gate, ["Bon"], ["jour"]))
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    sid = s["session_id"]
    sock, read_until = _open_stream(server, "/api/chats/{}/messages".format(sid),
                                    {"content": "salut"})
    read_until(b'"chunk": "Bon"')

    code, body = request(server, "POST", "/api/chats/{}/messages".format(sid),
                         {"content": "encore"}, token="t0k3n")
    assert (code, body["error"]) == (409, "TurnBusy")
    gate.set()
    sock.close()


def test_stop_ends_the_turn_and_keeps_the_partial(server, factory, monkeypatch):
    # Stop used to work only as a side effect of dropping the connection.
    gate = threading.Event()
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _gated_chat_stream(gate, ["Bon"], ["jour"]))
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    sid = s["session_id"]
    sock, read_until = _open_stream(server, "/api/chats/{}/messages".format(sid),
                                    {"content": "salut"})
    read_until(b'"chunk": "Bon"')

    code, body = request(server, "POST", "/api/chats/{}/stop".format(sid), {},
                         token="t0k3n")
    assert (code, body) == (200, {"stopped": True})
    gate.set()  # the generator resumes, sees the stop, and closes
    assert _wait_for(lambda: not server.turns.running(sid))
    _, got = request(server, "GET", "/api/chats/" + sid)
    # One event of overshoot by design: the stop is read between two events, so
    # whatever the model was already emitting still lands. The turn then closes
    # through the facade's finally, which is what persists the partial.
    assert got["messages"][1]["content"] == "Bonjour"
    assert got["messages"][1]["error"] == "interrupted"
    assert server.turns.get(sid).stopped
    sock.close()


def test_a_session_says_whether_a_turn_is_running(server, factory, monkeypatch):
    # The page needs this on load to know it must re-attach instead of showing
    # a dead transcript.
    gate = threading.Event()
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _gated_chat_stream(gate, ["Bon"], ["jour"]))
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    sid = s["session_id"]
    assert request(server, "GET", "/api/chats/" + sid)[1]["running"] is False

    sock, read_until = _open_stream(server, "/api/chats/{}/messages".format(sid),
                                    {"content": "salut"})
    read_until(b'"chunk": "Bon"')
    assert request(server, "GET", "/api/chats/" + sid)[1]["running"] is True
    listed = request(server, "GET", "/api/chats")[1]["sessions"]
    assert [x["running"] for x in listed if x["session_id"] == sid] == [True]

    gate.set()
    assert _wait_for(lambda: not server.turns.running(sid))
    assert request(server, "GET", "/api/chats/" + sid)[1]["running"] is False
    sock.close()


def test_a_running_session_says_where_its_turn_starts(server, factory, monkeypatch):
    # The page draws the transcript up to turn_starts_at and lets the replay
    # rebuild the rest; without it a refresh mid-agent-turn would show the
    # already-persisted tool chips twice.
    gate = threading.Event()
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _gated_chat_stream(gate, ["Bon"], ["jour"]))
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    sid = s["session_id"]
    sock, read_until = _open_stream(server, "/api/chats/{}/messages".format(sid),
                                    {"content": "salut"})
    read_until(b'"chunk": "Bon"')

    _, got = request(server, "GET", "/api/chats/" + sid)
    # The user message is already stored; everything after it belongs to the
    # turn and comes from the replay.
    assert got["turn_starts_at"] == 1
    assert [m["role"] for m in got["messages"]] == ["user"]
    gate.set()
    sock.close()


def test_static_files_are_served_no_store(server):
    conn = urllib.request.urlopen(
        "http://127.0.0.1:{}/static/app.js".format(server.server_address[1]))
    assert conn.headers.get("Cache-Control") == "no-store"


def test_chat_create_forwards_the_agent_workspace(server, factory, tmp_path):
    # Regression: the route used to drop `workspace`, so every agent session
    # silently worked in the server's cwd.
    code, s = request(server, "POST", "/api/chats",
                      {"model": "m:1", "kind": "agent", "mode": "auto",
                       "workspace": str(tmp_path)}, token="t0k3n")
    assert code == 200 and s["workspace"] == str(tmp_path)


def test_chat_create_refuses_a_bad_workspace(server, factory):
    code, body = request(server, "POST", "/api/chats",
                         {"model": "m:1", "kind": "agent",
                          "workspace": "Z:\\nulle\\part"}, token="t0k3n")
    assert code == 409 and "workspace" in body["detail"]


def test_chat_send_mid_stream_failure_becomes_an_ndjson_error_line(
        server, factory, monkeypatch):
    # An exception after the headers went out used to just drop the socket:
    # the UI saw the stream end and showed nothing. It must arrive as a line.
    def boom(sid, content, cancel=None):
        yield "start", None
        yield "chunk", "déb"
        raise RuntimeError("le résumé a explosé")
    monkeypatch.setattr(server.factory, "chat_reply", boom)
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")

    code, raw = request(server, "POST", "/api/chats/" + s["session_id"] + "/messages",
                        {"content": "salut"}, token="t0k3n", raw=True)

    assert code == 200
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    assert lines[-1] == {"error": "le résumé a explosé"}


def test_chat_send_is_409_when_gpu_is_busy(server, factory):
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    lock = GpuLock(factory.jobs_root / ".gpu.lock")
    assert lock.acquire(timeout=0)
    try:
        code, body = request(server, "POST",
                             "/api/chats/" + s["session_id"] + "/messages",
                             {"content": "salut"}, token="t0k3n")
    finally:
        lock.release()
    assert code == 409 and "GPU" in body["detail"]


def test_chat_send_blank_content_is_400(server, factory):
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    code, body = request(server, "POST", "/api/chats/" + s["session_id"] + "/messages",
                         {"content": "  "}, token="t0k3n")
    assert code == 400


def test_chat_send_unknown_session_is_404(server):
    code, body = request(server, "POST", "/api/chats/c_deadbeef/messages",
                         {"content": "x"}, token="t0k3n")
    assert code == 404 and body["error"] == "ChatNotFound"


def test_chat_routes_need_the_token(server):
    code, _ = request(server, "POST", "/api/chats", {"model": "m:1"})
    assert code == 403


# ---- agent ----

def _fake_agent_stream(turns):
    state = {"i": 0}

    def stream(url, messages, **kw):
        turn = turns[state["i"]]
        state["i"] += 1
        content = ""
        for kind, payload in turn:
            if kind == "content":
                content += payload
            yield kind, payload
        return {"content": content, "tokens_per_s": 30.0, "ttft_s": 1.0,
                "eval_count": 5}
    return stream


def test_agent_session_streams_tool_events(server, factory, tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("yo", encoding="utf-8")
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _fake_agent_stream([
        [("tool_call", {"name": "read_file", "arguments": {"path": "a.txt"}})],
        [("content", "fini")]]))
    _, s = request(server, "POST", "/api/chats",
                   {"model": "m:1", "kind": "agent", "mode": "auto"},
                   token="t0k3n")
    factory.chats.get(s["session_id"])  # session exists
    factory.chats.set_pending_calls  # store API present
    # workspace must be controllable for the test: point it at tmp_path
    sess = factory.chats.get(s["session_id"])
    sess["workspace"] = str(tmp_path)
    factory.chats._save(sess, None)

    code, raw = request(server, "POST",
                        "/api/chats/" + s["session_id"] + "/messages",
                        {"content": "lis a.txt"}, token="t0k3n", raw=True)

    assert code == 200
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    kinds = [next(iter(l)) for l in lines]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert lines[-1]["done"] is True


def test_approve_route_streams_and_messages_409_while_pending(
        server, factory, tmp_path, monkeypatch):
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream",
                        _fake_agent_stream([
        [("tool_call", {"name": "write_file",
                        "arguments": {"path": "b.txt", "content": "x"}})],
        [("content", "écrit")]]))
    _, s = request(server, "POST", "/api/chats",
                   {"model": "m:1", "kind": "agent", "mode": "approve"},
                   token="t0k3n")
    sess = factory.chats.get(s["session_id"])
    sess["workspace"] = str(tmp_path)
    factory.chats._save(sess, None)
    code, raw = request(server, "POST",
                        "/api/chats/" + s["session_id"] + "/messages",
                        {"content": "écris b.txt"}, token="t0k3n", raw=True)
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    assert next(iter(lines[-1])) == "approval_needed"

    code, body = request(server, "POST",
                         "/api/chats/" + s["session_id"] + "/messages",
                         {"content": "autre"}, token="t0k3n")
    assert code == 409

    code, raw = request(server, "POST",
                        "/api/chats/" + s["session_id"] + "/approve",
                        {"approved": True}, token="t0k3n", raw=True)
    assert code == 200
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "x"
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    assert lines[-1]["done"] is True


def test_chat_mode_route(server, factory):
    _, s = request(server, "POST", "/api/chats",
                   {"model": "m:1", "kind": "agent"}, token="t0k3n")
    code, body = request(server, "POST",
                         "/api/chats/" + s["session_id"] + "/mode",
                         {"mode": "auto"}, token="t0k3n")
    assert code == 200 and body["mode"] == "auto"


def test_streaming_does_not_scroll_the_log_on_every_delta():
    """A source guard, not a behaviour test -- the frontend is deliberately
    Node-free so there is no JS runner to assert against.

    Writing `log.scrollTop` forces a synchronous layout of the whole chat log,
    and feed() did it once per streamed delta: measured 2026-07-22 through the
    real studio (Playwright, 4.3k-node session) at 11.5 ms per delta, i.e. ~66 %
    of the main thread at 60 tok/s -- the reason every click felt slow while a
    reply streamed. Batching the write into one requestAnimationFrame took it
    to 0.018 ms. Reading scrollHeight per delta was measured NOT to be the
    cost; the write is. A naive revert to the per-delta pattern must not pass
    silently.
    """
    import factory_web
    src = (factory_web.ROOT / "harness" / "web" / "app.js").read_text(
        encoding="utf-8")
    stream = src[src.index("async function streamChat"):]
    stream = stream[:stream.index("\n}\n")]
    assert "requestAnimationFrame" in stream, \
        "streamChat must batch its autoscroll into a frame"
    assert "const atBottom = log.scrollHeight" not in stream, \
        "per-delta layout read is back in the stream loop"
    # exactly one scrollTop write, and it lives inside the rAF callback
    body_after_raf = stream[stream.index("requestAnimationFrame"):]
    assert stream.count("log.scrollTop =") == 1, \
        "the only scrollTop write must be the batched one"
    assert "log.scrollTop =" in body_after_raf


def test_the_studio_reattaches_instead_of_owning_the_turn():
    """Source guards for the server-side turns (spec 2026-07-22). The frontend
    is Node-free on purpose, so these three invariants are checked in the
    source: the Stop button must be a server call (aborting the fetch now only
    blinds the viewer), a running session must attach to the live stream, and
    the transcript must stop at the turn's mark so the replay does not double
    what the store already holds."""
    import factory_web
    src = (factory_web.ROOT / "harness" / "web" / "app.js").read_text(
        encoding="utf-8")

    stop = src[src.index('text: "■ Stop"'):]
    stop = stop[:stop.index("stopBtn.hidden = true;")]
    assert '"/stop"' in stop.replace("' + \"", '"'), stop
    assert "STREAM_ABORT.abort()" not in stop, \
        "Stop must ask the server; aborting the fetch leaves the turn running"

    assert "if (session.running) {" in src
    assert '"/stream", null, log)' in src, \
        "a running session must attach to the live turn"
    assert "session.messages.slice(0, session.turn_starts_at)" in src


def test_the_studio_renders_markdown_without_ever_building_markup():
    """Source guards for the Markdown pass. Two invariants matter more than the
    look: model output must never be able to become markup, and the parse must
    not run per streamed delta -- that is the exact cost pattern that made the
    studio slow (11.5 ms/token, fixed 2026-07-22)."""
    import factory_web
    web = factory_web.ROOT / "harness" / "web"
    src = (web / "app.js").read_text(encoding="utf-8")
    md = (web / "markdown.js").read_text(encoding="utf-8")

    # innerHTML/insertAdjacentHTML would turn a model's reply into markup.
    # Comments are stripped: both files talk ABOUT innerHTML to say they avoid it.
    def code_only(text):
        return "\n".join(l for l in text.splitlines()
                         if not l.strip().startswith("//"))

    assert "innerHTML" not in code_only(src)
    assert "insertAdjacentHTML" not in code_only(src)
    assert "innerHTML" not in code_only(md)
    assert "document.createTextNode(s.v)" in src  # plain spans stay text nodes

    # The parse happens when a bubble stops growing, never inside the delta path.
    stream = src[src.index("async function streamChat"):]
    stream = stream[:stream.index("\n}\n")]
    chunk = stream[stream.index("} else if (msg.chunk) {"):]
    chunk = chunk[:chunk.index("} else if")]
    assert "mdRender" not in chunk and "mdParse" not in chunk
    assert "const seal = () =>" in stream

    # The parser must load before the app that calls it.
    html = (web / "index.html").read_text(encoding="utf-8")
    assert html.index("/static/markdown.js") < html.index("/static/app.js")


def test_the_studio_ui_carries_the_e2e_audit_fixes():
    """Source guards for three findings of audits/20260722-studio-e2e.md.
    Verified live in the real studio; these keep them from silently reverting
    (the frontend is Node-free on purpose, so there is no JS runner).
    """
    import factory_web
    src = (factory_web.ROOT / "harness" / "web" / "app.js").read_text(
        encoding="utf-8")

    # §3 an agent session must not default to, or offer, a toolless model --
    # the default used to be whatever came first in the catalogue, which is
    # alphabetically the one model that cannot call a tool. Since the picker
    # became provider-then-model (29/07) the same invariant lives in its
    # fallback: a barred model is never what the list lands on, in either the
    # creation form or an open session.
    assert "|| rows.find((m) => !barred(m));" in src
    assert "barred: (m) => kindSel.value === \"agent\" && m.tools === false" in src
    assert "barred: (m) => agentSession && m.tools === false" in src
    assert "sans outils" in src

    # §5 "waiting_gpu" must say who holds the GPU and where to release it.
    assert 'status.status === "waiting_gpu"' in src
    assert 'href: "#runtime"' in src

    # §6 the picker caps its rows, keeps ticked files visible, and says how
    # many it hid.
    picker = src[src.index("function filePicker"):]
    picker = picker[:picker.index("\n}\n")]
    assert "const SHOWN = 60;" in picker
    assert "autres fichiers correspondent" in picker
    assert "...chosen, ...rest.slice(" in picker


def test_the_chat_survives_its_own_refresh_poll():
    """Source guards, same reason as above (no JS runner for app.js).

    Two regressions found live on 2026-08-19. Every chat opened with
    "session is not defined": the défaut-3 fix referenced `session.kind` for
    the plan slot outside the `else` block that declared `session`, so the
    whole render died before inserting the transcript. And the 3 s poll can
    fire while a turn is still streaming: any re-render rebuilds the DOM and
    wipes the composer draft and the live bubble.
    """
    import factory_web
    web = factory_web.ROOT / "harness" / "web"
    src = (web / "app.js").read_text(encoding="utf-8")

    # session is hoisted (let session = null;) and assigned, never declared
    # with const inside the else -- the exact line that used to throw.
    assert "let session = null;" in src
    assert "session = await api(\"/api/chats/\" + sessionId);" in src
    assert "const session = await api(\"/api/chats/\"" not in src
    # The plan slot guard that reads it is null-safe.
    assert 'sessionId && session && session.kind === "agent"' in src
    assert '"(session && session.kind) || \\"chat\\""' in src or \
        '"data-kind": (session && session.kind) || "chat"' in src

    # render() must step aside while a stream mutates the chat DOM.
    render = src[src.index("async function render()"):]
    render = render[:render.index("\n}\n")]
    assert "if (STREAM_ABORT && isChat && wasChat) {" in render


def test_the_stream_narrates_on_one_line_and_never_forces_thinking_open():
    """Source guard, same reason as above (no JS runner for app.js).

    Two regressions to keep out. Forcing `think.open = true` on every thinking
    delta is the "all thoughts unfolded" line of the backlog, verbatim; and a
    per-delta repaint is the 11.5 ms/token cost already paid for once, so the
    line must stay throttled and flushed at the end of the turn.
    """
    import factory_web
    web = factory_web.ROOT / "harness" / "web"
    src = (web / "app.js").read_text(encoding="utf-8")

    stream = src[src.index("async function streamChat"):]
    stream = stream[:stream.index("\n}\n")]
    assert "think.open" not in stream       # neither forced open nor forced shut
    assert "activity.show(" in stream
    for end in ("} else if (msg.done) {", "} else if (msg.error) {"):
        branch = stream[stream.index(end):]
        assert branch[:branch.index("\n")+120].count("activity.flush()") == 1

    line = src[src.index("function activityLine"):]
    line = line[:line.index("\n}\n")]
    assert "100 - (Date.now() - lastPaint)" in line  # ten paints a second, at most

    # The table that names the icon and the verb must load before its caller.
    html = (web / "index.html").read_text(encoding="utf-8")
    assert html.index("/static/activity.js") < html.index("/static/app.js")


def test_job_panes_scroll_inside_themselves():
    """Source guard, same reason as above (no JS runner on purpose).

    Measured 2026-07-22 through the real studio: an unbounded log + diff made
    the job page 8416 px tall on a 620 px viewport, so reading the tail of a
    log scrolled the banner, the verdict and the Apply button out of sight.
    Bounding both panes took the page to 1271 px. The job view also re-renders
    whole every 2 s while a job is active, which discards the <pre>; without
    the restore below, a live tail reads from the top of the tail and an
    operator who scrolled up to read is thrown back on every poll.
    """
    import factory_web
    web = factory_web.ROOT / "harness" / "web"
    css = (web / "style.css").read_text(encoding="utf-8")
    pane = css[css.index("pre.log, pre.diff {"):]
    pane = pane[:pane.index("}")]
    assert "max-height" in pane and "overflow: auto" in pane, \
        "job log and diff must scroll inside themselves, not push the page"

    src = (web / "app.js").read_text(encoding="utf-8")
    job = src[src.index("async function renderJob(jobId)"):]
    job = job[:job.index("\n}\n")]
    assert "JOB_LOG_SCROLL.stick ? logPre.scrollHeight" in job, \
        "the live tail must pin to the bottom across re-renders"
    assert "JOB_LOG_SCROLL.top = logPre.scrollTop" in job, \
        "a reader who scrolled up must keep their position across re-renders"

"""Draft jobs: the local spec writer proposes tests and targets, a human edits,
delegate() judges. Validation here is advisory on purpose -- a flawed proposal
is a warning for the editor, never a dead job.
"""
import subprocess

import pytest

from conftest import write_config
from draft_job import build_draft_prompt, extract_json, run_draft
from factory_mcp import Factory, ToolError, runner_script
from gpu_lock import GpuLock
from job_store import JobStore

GOOD_REPLY = """Here is my proposal:
```json
{"tests": {"test_slug.py": "def test_slug():\\n    assert slugify('A b') == 'a-b'\\n"},
 "target_files": ["slug.py"],
 "context_files": ["calc.py"],
 "assumptions": ["ascii only"]}
```
"""


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


@pytest.fixture
def factory(tmp_path, project):
    cfg = write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                       paths={"demo": project})
    spawned = []
    f = Factory(tmp_path / "jobs", cfg, spawn_fn=lambda job_id: spawned.append(job_id) or 4242)
    f.spawned = spawned
    f.cfg = cfg
    return f


def make_draft(factory, goal="add a slugify function"):
    return factory.draft("demo", goal)["job_id"]


# --- facade -------------------------------------------------------------------

def test_draft_has_a_runner():
    assert runner_script("draft") == "draft_job.py"


def test_draft_creates_a_draft_job_and_spawns_it(factory):
    job_id = make_draft(factory)

    assert factory.spawned == [job_id]
    spec = factory.store.read_spec(job_id)
    assert spec == {"kind": "draft", "project": "demo",
                    "goal": "add a slugify function"}


def test_draft_refuses_an_unknown_project(factory):
    from factory_config import ConfigError
    with pytest.raises(ConfigError):
        factory.draft("ghost", "g")
    assert factory.spawned == []


# --- json extraction ----------------------------------------------------------

def test_extract_json_reads_a_fenced_block():
    assert extract_json('bla\n```json\n{"a": 1}\n```\nbla') == {"a": 1}


def test_extract_json_falls_back_to_the_outermost_braces():
    assert extract_json('Sure! {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}


def test_extract_json_refuses_prose():
    with pytest.raises(ValueError):
        extract_json("I cannot help with that.")


# --- prompt -------------------------------------------------------------------

def test_draft_prompt_names_the_goal_the_files_and_the_contract():
    p = build_draft_prompt("add slugify", ["calc.py", "elo.py"])

    assert "add slugify" in p
    assert "calc.py" in p and "elo.py" in p
    for key in ("tests", "target_files", "context_files", "assumptions"):
        assert key in p


# --- runner -------------------------------------------------------------------

def run(factory, job_id, reply, **over):
    calls = []

    def chat_fn(model, system, user, temperature=0.1, num_ctx=16384, timeout=900):
        calls.append({"model": model, "system": system, "user": user})
        return {"content": reply}

    kw = dict(chat_fn=chat_fn, unload_fn=lambda m: None, gpu_timeout=5)
    kw.update(over)
    result = run_draft(job_id, factory.jobs_root, factory.cfg, **kw)
    return result, calls


def test_run_draft_proposes_and_succeeds(factory):
    job_id = make_draft(factory)

    result, calls = run(factory, job_id, GOOD_REPLY)

    assert result["status"] == "succeeded"
    assert result["draft"]["target_files"] == ["slug.py"]
    assert result["draft"]["context_files"] == ["calc.py"]
    assert list(result["draft"]["tests"]) == ["test_slug.py"]
    assert result["warnings"] == []
    assert "add a slugify function" in calls[0]["user"]
    assert "calc.py" in calls[0]["user"]  # the repo listing reached the model
    assert factory.store.status(job_id)["status"] == "succeeded"


def test_without_the_section_and_without_a_chat_fn_the_run_errors(factory):
    # Zero-Ollama: no [llama_server] section and no injected chat_fn leaves a
    # verdict, never a bare exception.
    job_id = make_draft(factory)
    result = run_draft(job_id, factory.jobs_root, factory.cfg, gpu_timeout=5,
                       heartbeat_interval=0.05)
    assert result["status"] == "error"
    assert "llama_server" in result["error"]


def test_a_flawed_proposal_is_warnings_not_a_failure(factory):
    job_id = make_draft(factory)
    reply = ('```json\n{"tests": {"test_x.py": "def test_x(): pass"},\n'
             '"target_files": ["../evil.py"], "context_files": ["ghost.py"]}\n```')

    result, _ = run(factory, job_id, reply)

    assert result["status"] == "succeeded"
    assert len(result["warnings"]) == 2  # unsafe target + context not in HEAD


def test_an_unparseable_reply_fails_and_keeps_the_raw_text(factory):
    job_id = make_draft(factory)

    result, _ = run(factory, job_id, "I would suggest writing some tests.")

    assert result["status"] == "failed"
    assert "I would suggest" in result["raw"]
    assert factory.store.status(job_id)["status"] == "failed"


def test_run_draft_waits_for_the_gpu_and_gives_up_cleanly(factory):
    job_id = make_draft(factory)
    lock = GpuLock(factory.jobs_root / ".gpu.lock")
    assert lock.acquire(timeout=0)
    try:
        result, calls = run(factory, job_id, GOOD_REPLY, gpu_timeout=0)
    finally:
        lock.release()

    assert result["status"] == "failed"
    assert result["failure_reason"] == "gpu_timeout"
    assert calls == []


def test_run_draft_unloads_the_draft_model(factory):
    job_id = make_draft(factory)
    unloaded = []

    def chat_fn(model, system, user, **kw):
        return {"content": GOOD_REPLY}

    run_draft(job_id, factory.jobs_root, factory.cfg, chat_fn=chat_fn,
              unload_fn=unloaded.append, model="m-test", gpu_timeout=5)

    assert unloaded == ["m-test"]

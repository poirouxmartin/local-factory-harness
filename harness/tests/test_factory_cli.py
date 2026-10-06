"""The operator CLI: the factory without an MCP client.

The guarantee it exists for (docs/vision.md): running out of Claude credits must
never mean a stalled factory. Same Factory object as the MCP server -- argv in,
JSON out, and the diff savable to a file so `git apply` closes the loop by hand.
"""
import io
import json
import subprocess

import pytest

from conftest import write_config
from factory_cli import main
from factory_mcp import Factory

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


@pytest.fixture
def factory(tmp_path, project):
    cfg = write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                       paths={"demo": project})
    spawned = []
    f = Factory(tmp_path / "jobs", cfg, spawn_fn=lambda job_id: spawned.append(job_id) or 4242)
    f.spawned = spawned
    return f


def run_cli(factory, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), factory=factory, out=out, err=err)
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None), err.getvalue()


def delegate(factory, tmp_path, *extra):
    test_file = tmp_path / "test_x.py"
    test_file.write_text(TEST_SRC)
    return run_cli(factory, "delegate", "--project", "demo", "--goal", "make it work",
                   "--test", str(test_file), "--target", "calc.py", *extra)


def test_cli_delegate_passes_edit_mode(factory, tmp_path):
    code, payload, _ = delegate(factory, tmp_path, "--edit-mode", "diff")

    assert code == 0
    spec = factory.store.read_spec(payload["job_id"])
    assert spec["edit_mode"] == "diff"


def test_save_diff_falls_back_to_the_best_attempt(tmp_path):
    class FailedJob:
        def job_result(self, job_id):
            return {"ready": True, "status": "failed", "diff": "",
                    "best_attempt": {"tests_passed": 6, "diff": "BEST\n"}}

    code, payload, _ = run_cli(FailedJob(), "result", "j_x",
                               "--save-diff", str(tmp_path / "o.patch"))
    assert code == 0
    assert (tmp_path / "o.patch").read_text() == "BEST\n"
    assert payload["diff_saved_to"] == str(tmp_path / "o.patch")


def test_delegate_reads_test_files_from_disk_and_queues_a_job(factory, tmp_path):
    code, payload, _ = delegate(factory, tmp_path)

    assert code == 0
    assert payload["status"] == "queued"
    assert factory.spawned == [payload["job_id"]]
    spec = factory.store.read_spec(payload["job_id"])
    assert spec["tests"] == {"test_x.py": TEST_SRC}
    assert spec["target_files"] == ["calc.py"]


def test_a_missing_test_file_refuses_before_anything_exists(factory, tmp_path):
    code, payload, err = run_cli(factory, "delegate", "--project", "demo", "--goal", "g",
                                 "--test", str(tmp_path / "nope.py"), "--target", "calc.py")

    assert code == 1
    assert "nope.py" in err
    assert factory.spawned == []


def test_a_bad_project_key_is_a_clean_error_not_a_traceback(factory, tmp_path):
    test_file = tmp_path / "test_x.py"
    test_file.write_text(TEST_SRC)

    code, _, err = run_cli(factory, "delegate", "--project", "ghost", "--goal", "g",
                           "--test", str(test_file), "--target", "calc.py")

    assert code == 1
    assert "ghost" in err


def test_status_of_a_queued_job(factory, tmp_path):
    _, payload, _ = delegate(factory, tmp_path)

    code, status, _ = run_cli(factory, "status", payload["job_id"])

    assert code == 0
    assert status["status"] == "queued"


def test_result_of_a_finished_job_carries_the_verdict(factory, tmp_path):
    _, payload, _ = delegate(factory, tmp_path)
    job_id = payload["job_id"]
    factory.store.write_result(job_id, {"status": "succeeded", "diff": "the diff"})
    factory.store.update_state(job_id, status="succeeded")

    code, result, _ = run_cli(factory, "result", job_id)

    assert code == 0
    assert result["ready"] is True
    assert result["status"] == "succeeded"


def test_result_can_save_the_diff_to_a_file_for_git_apply(factory, tmp_path):
    _, payload, _ = delegate(factory, tmp_path)
    job_id = payload["job_id"]
    factory.store.write_result(job_id, {"status": "succeeded", "diff": "--- a/calc.py\n"})
    factory.store.update_state(job_id, status="succeeded")
    patch = tmp_path / "out.patch"

    code, result, _ = run_cli(factory, "result", job_id, "--save-diff", str(patch))

    assert code == 0
    assert patch.read_text(encoding="utf-8") == "--- a/calc.py\n"
    assert result["diff_saved_to"] == str(patch)


def test_an_unknown_job_is_a_clean_error(factory):
    code, _, err = run_cli(factory, "status", "j_nope")

    assert code == 1
    assert "j_nope" in err


def test_log_returns_the_tail(factory, tmp_path):
    _, payload, _ = delegate(factory, tmp_path)
    job_id = payload["job_id"]
    factory.store.append_log(job_id, "line one\nline two\n")

    code, log, _ = run_cli(factory, "log", job_id, "--tail", "1")

    assert code == 0
    assert log["log"] == "line two"


def test_apply_puts_the_diff_in_the_working_tree(factory, tmp_path, project):
    _, payload, _ = delegate(factory, tmp_path)
    job_id = payload["job_id"]
    (project / "calc.py").write_text("x = 2\n")
    diff = subprocess.run(["git", "-C", str(project), "diff"],
                          capture_output=True, text=True, check=True).stdout
    git(project, "checkout", "--", "calc.py")
    factory.store.write_result(job_id, {"status": "succeeded", "diff": diff})

    code, applied, _ = run_cli(factory, "apply", job_id)

    assert code == 0
    assert applied["status"] == "applied"
    assert (project / "calc.py").read_text() == "x = 2\n"


def test_apply_refusal_is_a_clean_error(factory, tmp_path):
    _, payload, _ = delegate(factory, tmp_path)
    job_id = payload["job_id"]

    code, _, err = run_cli(factory, "apply", job_id)

    assert code == 1
    assert "no result" in err


def test_cancel_marks_a_queued_job_cancelled(factory, tmp_path):
    _, payload, _ = delegate(factory, tmp_path)

    code, cancelled, _ = run_cli(factory, "cancel", payload["job_id"])

    assert code == 0
    assert cancelled["status"] == "cancelled"
    assert factory.store.status(payload["job_id"])["status"] == "cancelled"

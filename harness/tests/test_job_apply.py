"""Apply: the diff lands in the project's working tree, never in its history.

The check-then-apply pair is the whole contract: a diff that does not apply
cleanly must leave the tree byte-identical, and a clean apply must stage
nothing and commit nothing -- the human still owns the git story.
"""
import subprocess

import pytest

from conftest import write_config
from factory_mcp import Factory, ToolError

TEST_SRC = "def test_x():\n    assert True\n"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


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
    f = Factory(tmp_path / "jobs", cfg, spawn_fn=lambda job_id: 4242)
    f.project_path = project
    return f


def make_diff(repo, path, new_content):
    """A real diff from a real git, then the tree is put back."""
    (repo / path).write_text(new_content)
    diff = git(repo, "diff")
    git(repo, "checkout", "--", path)
    return diff


def finished_job(factory, diff, status="succeeded"):
    job_id = factory.delegate("demo", "g", {"test_x.py": TEST_SRC}, ["calc.py"])["job_id"]
    factory.store.write_result(job_id, {"status": status, "diff": diff})
    factory.store.update_state(job_id, status=status)
    return job_id


def test_apply_writes_the_diff_to_the_working_tree_without_committing(factory):
    repo = factory.project_path
    job_id = finished_job(factory, make_diff(repo, "calc.py", "x = 2\n"))

    out = factory.job_apply(job_id)

    assert out == {"job_id": job_id, "status": "applied", "project": "demo"}
    assert (repo / "calc.py").read_text() == "x = 2\n"
    assert git(repo, "diff", "--cached") == ""          # nothing staged
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "1"  # nothing committed
    assert factory.store.read_state(job_id)["applied_at"] > 0


def test_apply_refuses_a_job_that_has_no_result_yet(factory):
    job_id = factory.delegate("demo", "g", {"test_x.py": TEST_SRC}, ["calc.py"])["job_id"]

    with pytest.raises(ToolError, match="no result"):
        factory.job_apply(job_id)


def test_apply_refuses_a_job_that_did_not_succeed(factory):
    job_id = finished_job(factory, "some diff", status="failed")

    with pytest.raises(ToolError, match="failed"):
        factory.job_apply(job_id)


def test_apply_refuses_an_empty_diff(factory):
    job_id = finished_job(factory, "")

    with pytest.raises(ToolError, match="empty diff"):
        factory.job_apply(job_id)


def test_a_conflicting_diff_leaves_the_tree_untouched(factory):
    repo = factory.project_path
    job_id = finished_job(factory, make_diff(repo, "calc.py", "x = 2\n"))
    (repo / "calc.py").write_text("x = 999  # local edit\n")

    with pytest.raises(ToolError, match="does not apply"):
        factory.job_apply(job_id)

    assert (repo / "calc.py").read_text() == "x = 999  # local edit\n"
    assert "applied_at" not in factory.store.read_state(job_id)


def test_apply_twice_refuses_the_second_time(factory):
    repo = factory.project_path
    job_id = finished_job(factory, make_diff(repo, "calc.py", "x = 2\n"))

    factory.job_apply(job_id)
    with pytest.raises(ToolError, match="does not apply"):
        factory.job_apply(job_id)

    assert (repo / "calc.py").read_text() == "x = 2\n"

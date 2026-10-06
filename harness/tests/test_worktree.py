"""Worktree isolation and the shape of the deliverable.

The job never touches the source repo. It gets a worktree on branch factory/<id>,
and what comes back is a diff -- not a commit, not a push. Two traps are pinned here:

  - `git diff` alone does NOT see files the model created. A job asked to add a
    module would return an empty diff with green tests. Hence `git add -A` first.
  - the diff must never contain a test file. The tests live outside the worktree,
    so this is belt-and-braces -- but it is the last gate before a human sees it.
"""
import subprocess

import pytest

from worktree import WorkTree, forbidden_paths


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "f@f.f")
    git(root, "config", "user.name", "factory")
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "seed")
    return root


@pytest.fixture
def tree(repo, tmp_path):
    wt = WorkTree.create(repo, "j_00000001", tmp_path / "jobs" / "j_00000001" / "worktree")
    yield wt
    wt.remove()


def test_the_worktree_carries_the_repo_contents(tree):
    assert (tree.path / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"


def test_the_worktree_is_on_its_own_branch(tree, repo):
    assert tree.branch == "factory/j_00000001"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_the_base_sha_is_the_source_head(tree, repo):
    assert tree.base_sha == git(repo, "rev-parse", "HEAD").strip()


def test_editing_the_worktree_does_not_touch_the_source_repo(tree, repo):
    (tree.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    assert (repo / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"


def test_uncommitted_changes_in_the_source_repo_do_not_leak_in(repo, tmp_path):
    (repo / "calc.py").write_text("LOCAL SCRATCH\n")

    wt = WorkTree.create(repo, "j_00000002", tmp_path / "wt2")
    try:
        assert (wt.path / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"
    finally:
        wt.remove()


def test_capture_diff_is_empty_when_nothing_changed(tree):
    assert tree.capture_diff() == ""


def test_capture_diff_sees_a_modified_file(tree):
    (tree.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    diff = tree.capture_diff()
    assert "-    return a - b" in diff
    assert "+    return a + b" in diff


def test_capture_diff_sees_a_file_the_model_created(tree):
    """`git diff` without staging would silently return nothing here."""
    (tree.path / "elo.py").write_text("K = 32\n")

    diff = tree.capture_diff()
    assert "elo.py" in diff
    assert "+K = 32" in diff


def test_capture_diff_sees_a_deleted_file(tree):
    (tree.path / "calc.py").unlink()

    assert "deleted file" in tree.capture_diff()


def test_changed_paths_lists_what_the_job_touched(tree):
    (tree.path / "elo.py").write_text("K = 32\n")
    (tree.path / "calc.py").write_text("x\n")

    assert sorted(tree.changed_paths()) == ["calc.py", "elo.py"]


def test_no_commit_is_created_on_the_branch(tree, repo):
    (tree.path / "calc.py").write_text("changed\n")
    tree.capture_diff()

    assert git(repo, "rev-parse", "factory/j_00000001").strip() == tree.base_sha


def test_remove_deletes_the_worktree_and_its_branch(repo, tmp_path):
    wt = WorkTree.create(repo, "j_00000003", tmp_path / "wt3")

    wt.remove()

    assert not wt.path.exists()
    assert "factory/j_00000003" not in git(repo, "branch", "--list", "factory/*")
    assert "j_00000003" not in git(repo, "worktree", "list")


def test_remove_is_idempotent(repo, tmp_path):
    wt = WorkTree.create(repo, "j_00000004", tmp_path / "wt4")
    wt.remove()

    wt.remove()  # a crashed job may be pruned twice; this must not explode


@pytest.mark.parametrize("path", [
    "test_calc.py",
    "tests/test_calc.py",
    "calc_test.py",
    "conftest.py",
    "src/conftest.py",
    "pytest.ini",
    "pyproject.toml",
])
def test_a_diff_touching_the_judge_is_forbidden(path):
    assert forbidden_paths([path, "calc.py"]) == [path]


@pytest.mark.parametrize("path", ["calc.py", "elo.py", "src/contest.py", "latest.py"])
def test_ordinary_source_files_are_allowed(path):
    assert forbidden_paths([path]) == []


def test_restore_judge_reverts_a_modified_test_file(repo, tmp_path):
    (repo / "test_calc.py").write_text("def test_real():\n    assert False\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add tests")
    wt = WorkTree.create(repo, "j_j1", tmp_path / "wt_j1")
    try:
        (wt.path / "test_calc.py").write_text("def test_real():\n    pass\n")

        wt.restore_judge()

        assert (wt.path / "test_calc.py").read_text() == "def test_real():\n    assert False\n"
    finally:
        wt.remove()


def test_restore_judge_deletes_a_planted_conftest(tree):
    """`git checkout --` never removes an added file. This is the half that matters."""
    (tree.path / "conftest.py").write_text("import calc\ncalc.add = lambda a, b: a + b\n")

    report = tree.restore_judge()

    assert not (tree.path / "conftest.py").exists()
    assert report["deleted"] == ["conftest.py"]


def test_restore_judge_deletes_a_planted_pyproject_toml(tree):
    """A pyproject.toml with [tool.pytest.ini_options] can inject addopts just like a
    pytest.ini -- it must be a judge file too, or run_regression can be rigged."""
    (tree.path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "--collect-only"\n')

    report = tree.restore_judge()

    assert not (tree.path / "pyproject.toml").exists()
    assert report["deleted"] == ["pyproject.toml"]


def test_restore_judge_deletes_a_planted_test_in_a_subdirectory(tree):
    nested = tree.path / "harness" / "tests"
    nested.mkdir(parents=True)
    (nested / "conftest.py").write_text("# planted\n")

    tree.restore_judge()

    assert not (nested / "conftest.py").exists()


def test_restore_judge_leaves_a_legitimate_new_module_alone(tree):
    (tree.path / "elo.py").write_text("K = 32\n")

    tree.restore_judge()

    assert (tree.path / "elo.py").read_text() == "K = 32\n"


def test_restore_judge_leaves_the_models_source_edits_alone(tree):
    (tree.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    tree.restore_judge()

    assert (tree.path / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_restore_judge_is_a_no_op_on_a_clean_tree(tree):
    report = tree.restore_judge()

    assert report == {"restored": [], "deleted": []}


def test_restore_judge_reports_only_the_judge_files_that_actually_changed(repo, tmp_path):
    """A non-empty report now rejects the job (ADR-015), so listing every tracked
    judge file as 'restored' -- modified or not -- would reject every job in any
    project that has tests. Only real tampering may show up."""
    (repo / "test_calc.py").write_text("def test_real():\n    assert False\n")
    (repo / "test_other.py").write_text("def test_other():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add tests")
    wt = WorkTree.create(repo, "j_j2", tmp_path / "wt_j2")
    try:
        (wt.path / "test_calc.py").write_text("def test_real():\n    pass\n")

        report = wt.restore_judge()

        assert report["restored"] == ["test_calc.py"]
        assert (wt.path / "test_calc.py").read_text() == "def test_real():\n    assert False\n"
    finally:
        wt.remove()


def test_head_blob_size_returns_the_byte_size_of_a_tracked_file(repo):
    from worktree import head_blob_size

    assert head_blob_size(repo, "calc.py") == len("def add(a, b):\n    return a - b\n")


def test_head_blob_size_is_none_for_a_path_absent_from_head(repo):
    from worktree import head_blob_size

    assert head_blob_size(repo, "does_not_exist.py") is None


def test_create_at_an_explicit_base_sha(repo, tmp_path):
    first = git(repo, "rev-parse", "HEAD").strip()
    (repo / "calc.py").write_text("def add(a, b):\n    return a * b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "second")

    wt = WorkTree.create(repo, "j_00000002",
                         tmp_path / "jobs" / "j_00000002" / "worktree",
                         base_sha=first)
    try:
        assert wt.base_sha == first
        assert (wt.path / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"
    finally:
        wt.remove()


# ---- a stale branch must not score as a failed cell -------------------------
# 2026-07-28: two cells of a 12-job corpus replay came back `error` in 0.1 s on
# "a branch named factory/<job> already exists". An infrastructure collision
# counts in the denominator exactly like a model failure and reads like one, so
# the create has to tell a dead branch from a live one instead of dying.

def test_create_reclaims_a_branch_no_worktree_holds(repo, tmp_path):
    """A branch left behind by a killed run is garbage: take the name back."""
    git(repo, "branch", "factory/j_stale")
    tree = WorkTree.create(repo, "j_stale", tmp_path / "wt")
    assert tree.branch == "factory/j_stale"
    assert (tree.path / "calc.py").is_file()


def test_create_refuses_a_branch_a_live_worktree_holds(repo, tmp_path):
    """Two runs on one job id must not share a branch -- say so, out loud."""
    first = WorkTree.create(repo, "j_live", tmp_path / "wt1")
    with pytest.raises(Exception) as excinfo:
        WorkTree.create(repo, "j_live", tmp_path / "wt2")
    message = str(excinfo.value)
    assert "factory/j_live" in message
    assert "wt1" in message.replace("\\", "/")
    first.remove()

"""The judge lives outside the courtroom.

Tests are materialized in jobs/<id>/tests/, never inside the worktree. The model
cannot edit what is not in its tree -- that is the whole point. These tests take
the three escapes a model actually reaches for and prove each one fails:

  1. drop a conftest.py that empties the collected items
  2. drop a pytest config whose addopts deselect everything
  3. drop a sitecustomize.py, which Python imports before pytest exists

Plus the boring one that matters at 3am: a model writing an infinite loop must
not hang the job forever.
"""
import textwrap

import pytest

from pytest_runner import materialize_tests, run_tests

GOOD = "def add(a, b):\n    return a + b\n"
BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"


@pytest.fixture
def sandbox(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    tests_dir = tmp_path / "tests"
    materialize_tests(tests_dir, {"test_calc.py": TEST}, worktree)
    return worktree, tests_dir


def test_a_correct_implementation_passes(sandbox):
    worktree, tests_dir = sandbox
    (worktree / "calc.py").write_text(GOOD)

    result = run_tests(worktree, tests_dir)

    assert result.passed is True
    assert result.tests_passed == 1
    assert result.tests_failed == 0


def test_a_buggy_implementation_fails(sandbox):
    worktree, tests_dir = sandbox
    (worktree / "calc.py").write_text(BUGGY)

    result = run_tests(worktree, tests_dir)

    assert result.passed is False
    assert result.tests_failed == 1
    assert "test_add" in result.output


def test_a_conftest_dropped_in_the_worktree_cannot_empty_the_test_suite(sandbox):
    worktree, tests_dir = sandbox
    (worktree / "calc.py").write_text(BUGGY)
    (worktree / "conftest.py").write_text(textwrap.dedent("""
        def pytest_collection_modifyitems(config, items):
            items.clear()
    """))

    result = run_tests(worktree, tests_dir)

    assert result.passed is False, "the model just deleted every assertion"
    assert result.tests_failed == 1


def test_a_conftest_dropped_in_the_worktree_cannot_monkeypatch_its_way_to_green(sandbox):
    """The exploit that actually yields exit 0 with the bug intact.

    Against the naive layout (tests inside the tree) this prints "1 passed" while
    calc.add still returns a - b, and `git checkout -- tests/` does not even remove
    it, because an added file is untracked.
    """
    worktree, tests_dir = sandbox
    (worktree / "calc.py").write_text(BUGGY)
    (worktree / "conftest.py").write_text("import calc\ncalc.add = lambda a, b: a + b\n")

    result = run_tests(worktree, tests_dir)

    assert result.passed is False
    assert result.tests_failed == 1


def test_a_pytest_config_dropped_in_the_worktree_cannot_deselect_the_suite(sandbox):
    worktree, tests_dir = sandbox
    (worktree / "calc.py").write_text(BUGGY)
    (worktree / "pytest.ini").write_text("[pytest]\naddopts = -k nothing_matches\n")
    (worktree / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-k nothing_matches"\n')

    result = run_tests(worktree, tests_dir)

    assert result.passed is False
    assert result.tests_failed == 1


def test_a_sitecustomize_dropped_in_the_worktree_is_never_imported(sandbox):
    worktree, tests_dir = sandbox
    (worktree / "calc.py").write_text(GOOD)
    marker = worktree / "pwned.txt"
    (worktree / "sitecustomize.py").write_text(
        "import pathlib; pathlib.Path(r'{}').write_text('x')\n".format(marker))

    result = run_tests(worktree, tests_dir)

    assert result.passed is True
    assert not marker.exists(), "worktree code ran before pytest did"


def test_the_worktree_is_importable_by_the_tests(sandbox):
    worktree, tests_dir = sandbox
    (worktree / "calc.py").write_text(GOOD)

    assert run_tests(worktree, tests_dir).passed is True


def test_an_implementation_that_hangs_is_killed_and_reported(sandbox):
    worktree, tests_dir = sandbox
    (worktree / "calc.py").write_text("import time\n\ndef add(a, b):\n    time.sleep(60)\n")

    result = run_tests(worktree, tests_dir, timeout=3)

    assert result.passed is False
    assert result.timed_out is True


def test_materialize_writes_nested_test_paths(tmp_path):
    tests_dir = tmp_path / "tests"

    materialize_tests(tests_dir, {"unit/test_x.py": "def test_x(): pass"}, tmp_path / "wt")

    assert (tests_dir / "unit" / "test_x.py").read_text() == "def test_x(): pass"


def test_materialize_leaves_no_pytest_cache_inside_the_tests_dir(sandbox):
    worktree, tests_dir = sandbox
    (worktree / "calc.py").write_text(GOOD)

    run_tests(worktree, tests_dir)

    assert not (tests_dir / ".pytest_cache").exists()


def test_run_regression_passes_on_a_healthy_project_suite(tmp_path):
    from pytest_runner import run_regression

    worktree = tmp_path / "wt"
    (worktree / "suite").mkdir(parents=True)
    (worktree / "suite" / "test_ok.py").write_text("def test_ok():\n    assert True\n")

    result = run_regression(worktree, ["-m", "pytest", "suite", "-q"])

    assert result.passed is True
    assert result.tests_passed == 1


def test_run_regression_fails_when_the_project_suite_is_red(tmp_path):
    from pytest_runner import run_regression

    worktree = tmp_path / "wt"
    (worktree / "suite").mkdir(parents=True)
    (worktree / "suite" / "test_ko.py").write_text("def test_ko():\n    assert False\n")

    result = run_regression(worktree, ["-m", "pytest", "suite", "-q"])

    assert result.passed is False
    assert result.tests_failed == 1
    assert "test_ko" in result.output


def test_run_regression_runs_inside_the_worktree(tmp_path):
    """The suite must grade the worktree's code, not the harness that spawned it."""
    from pytest_runner import run_regression

    worktree = tmp_path / "wt"
    (worktree / "suite").mkdir(parents=True)
    (worktree / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (worktree / "suite" / "test_calc.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path.cwd()))\n"
        "from calc import add\n\n"
        "def test_add():\n    assert add(2, 2) == 4\n")

    assert run_regression(worktree, ["-m", "pytest", "suite", "-q"]).passed is True


def test_run_regression_that_hangs_is_killed(tmp_path):
    from pytest_runner import run_regression

    worktree = tmp_path / "wt"
    (worktree / "suite").mkdir(parents=True)
    (worktree / "suite" / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(60)\n")

    result = run_regression(worktree, ["-m", "pytest", "suite", "-q"], timeout=3)

    assert result.passed is False
    assert result.timed_out is True


def test_run_regression_surfaces_the_exit_code_of_a_misconfigured_command(tmp_path):
    """A typo'd regression_cmd must not look like a model that broke the project."""
    from pytest_runner import OPERATOR_ERROR_CODES, run_regression

    worktree = tmp_path / "wt"
    worktree.mkdir()

    result = run_regression(worktree, ["-m", "pytest", "no_such_dir", "-q"])

    assert result.passed is False
    assert result.returncode in OPERATOR_ERROR_CODES


def test_run_regression_treats_a_collect_only_suite_as_not_green(tmp_path):
    """A pyproject.toml addopts of --collect-only exits 0 with nothing actually run.
    Bare exit 0 must not read as green -- mirrors run_tests's own guard."""
    from pytest_runner import run_regression

    worktree = tmp_path / "wt"
    (worktree / "suite").mkdir(parents=True)
    (worktree / "suite" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (worktree / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "--collect-only"\n')

    result = run_regression(worktree, ["-m", "pytest", "suite", "-q"])

    assert result.returncode == 0, "collect-only itself must exit clean"
    assert result.passed is False, "nothing was actually run"


def test_run_tests_also_reports_its_exit_code(sandbox):
    worktree, tests_dir = sandbox
    (worktree / "calc.py").write_text(GOOD)

    assert run_tests(worktree, tests_dir).returncode == 0

"""Run the job's tests from outside the job's tree.

The classic failure of "the tests are the judge" is that, faced with a test it
cannot satisfy, a model rewrites the test. Restoring the test files after each
attempt only patches the obvious hole -- a model can still neutralize the suite
from a conftest.py, a pytest config, or a sitecustomize.py it drops in the tree.

So the tests do not live in the tree. They live in jobs/<id>/tests/, pytest is
pinned to that rootdir with our own config, and the worktree is added to sys.path
by a harness-owned conftest -- late, after the interpreter is done importing site.
The model cannot edit what it cannot see.
"""
import os
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

DEFAULT_TIMEOUT = 600  # a model can and will write an infinite loop

REGRESSION_TIMEOUT = 900  # the project's own suite; 143 tests take ~22 s today

# pytest: 3 internal error, 4 usage error, 5 nothing collected. All three mean the
# command is wrong, not the code. A model must never be asked to fix a typo in
# factory.toml.
OPERATOR_ERROR_CODES = (3, 4, 5)

Result = namedtuple("Result", "passed tests_passed tests_failed output timed_out returncode")

# Our own config, so a pytest.ini or [tool.pytest.ini_options] dropped in the
# worktree cannot inject addopts.
_PYTEST_INI = "[pytest]\naddopts =\n"

# Imported by pytest, i.e. long after `site` ran: nothing in the worktree gets a
# chance to execute before the suite is collected.
_CONFTEST = (
    "# Harness-owned. The job's spec cannot overwrite this file.\n"
    "import sys\n"
    "sys.path.insert(0, {worktree!r})\n"
)


def materialize_tests(tests_dir, tests, worktree, project_path=None):
    # project_path is the vitest runner's need (node_modules); unused here,
    # kept so loop_job can call every runner the same way.
    tests_dir = Path(tests_dir)
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "conftest.py").write_text(
        _CONFTEST.format(worktree=str(Path(worktree).resolve())), encoding="utf-8")
    (tests_dir / "pytest.ini").write_text(_PYTEST_INI, encoding="utf-8")
    for relpath, content in tests.items():
        dst = tests_dir / relpath
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
    return tests_dir


def cleanup(worktree, tests_dir):
    """Nothing to detach: pytest jobs plant no links. Runner-surface parity."""


def _counts(out):
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    return passed, failed


def failed_names(out):
    """Best-effort failing-test ids from a -q run ('FAILED path::name').

    Routing evidence (bottlenecks doc, item 1): the same names failing on
    successive rungs means escalate, shrinking counts mean progress.
    """
    names = []
    for m in re.finditer(r"^FAILED (\S+)", out, re.M):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def _clean_env():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_tests(worktree, tests_dir, timeout=DEFAULT_TIMEOUT):
    tests_dir = Path(tests_dir)
    cmd = [sys.executable, "-m", "pytest", str(tests_dir), "-q",
           "-p", "no:cacheprovider",
           "-c", str(tests_dir / "pytest.ini"),
           "--rootdir", str(tests_dir),
           "--confcutdir", str(tests_dir)]
    env = _clean_env()
    try:
        # cwd is the tests dir, never the worktree: with `python -m`, the working
        # directory lands on sys.path.
        p = subprocess.run(cmd, cwd=str(tests_dir), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + "\n" + (e.stderr or "")
        return Result(False, 0, 0, out + "\nTIMEOUT after {}s".format(timeout), True, -1)

    out = (p.stdout or "") + "\n" + (p.stderr or "")
    npassed, nfailed = _counts(out)
    # A suite that collected nothing is not a green suite, whatever the exit code.
    passed = p.returncode == 0 and npassed > 0
    return Result(passed, npassed, nfailed, out, False, p.returncode)


def run_regression(worktree, argv, timeout=REGRESSION_TIMEOUT):
    """Run the project's own test suite inside the worktree.

    Unlike the spec's tests, these live in the tree -- the model can reach them.
    Call WorkTree.restore_judge() immediately before this, every time.
    """
    cmd = [sys.executable, *argv]
    try:
        p = subprocess.run(cmd, cwd=str(worktree), env=_clean_env(), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + "\n" + (e.stderr or "")
        return Result(False, 0, 0, out + "\nTIMEOUT after {}s".format(timeout), True, -1)

    out = (p.stdout or "") + "\n" + (p.stderr or "")
    npassed, nfailed = _counts(out)
    # Same guard as run_tests: a suite that collected nothing is not green, whatever
    # the exit code (a collect-only addopts smuggled into the tree exits 0 too).
    passed = p.returncode == 0 and npassed > 0
    return Result(passed, npassed, nfailed, out, False, p.returncode)

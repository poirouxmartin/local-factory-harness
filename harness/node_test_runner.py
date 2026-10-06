"""node:test counterpart of vitest_runner, for projects on the built-in
runner (blitzvolley). Same threat model (ADR-010): spec tests live in
jobs/<id>/tests/, never in the worktree.

Resolution: spec tests are CJS and require worktree files through
NODE_PATH=<worktree> (repo-relative ids: require("backend/game/score")).
The project's node_modules is junctioned into both the worktree and the
tests dir, exactly like vitest_runner and for the same reasons.

Counts come from the TAP reporter: the human-facing default reporter is
unicode-decorated and unstable across node versions; TAP's trailing
"# pass N" / "# fail N" lines are not.
"""
import os
import re
import subprocess
from collections import namedtuple
from pathlib import Path

from vitest_runner import _link_node_modules, _unlink_node_modules

DEFAULT_TIMEOUT = 600
REGRESSION_TIMEOUT = 900

# node:test has no pytest-style "the command is wrong" exit codes: config errors
# and red tests all exit 1. Operator errors are caught by the collected-nothing
# guard instead.
OPERATOR_ERROR_CODES = ()

Result = namedtuple("Result", "passed tests_passed tests_failed output timed_out returncode")


def materialize_tests(tests_dir, tests, worktree, project_path=None):
    tests_dir = Path(tests_dir)
    tests_dir.mkdir(parents=True, exist_ok=True)
    for relpath, content in tests.items():
        dst = tests_dir / relpath
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
    if project_path:
        source = Path(project_path) / "node_modules"
        if not source.is_dir():
            raise FileNotFoundError(
                "{} has no node_modules: run npm install in the project "
                "before delegating to it".format(project_path))
        _link_node_modules(source, Path(worktree).resolve())
        _link_node_modules(source, tests_dir)
    return tests_dir


def cleanup(worktree, tests_dir):
    """Detach the node_modules links. Call BEFORE WorkTree.remove()."""
    _unlink_node_modules(worktree)
    _unlink_node_modules(tests_dir)


def _counts(out):
    passed = int(g.group(1)) if (g := re.search(r"^# pass (\d+)", out, re.M)) else 0
    failed = int(g.group(1)) if (g := re.search(r"^# fail (\d+)", out, re.M)) else 0
    return passed, failed


def failed_names(out):
    """Best-effort failing-test names from TAP output ('not ok N - name').

    Subtests are indented, so column-0 'not ok' catches top-level tests only;
    TAP directives after ' # ' are stripped.
    """
    names = []
    for m in re.finditer(r"^not ok \d+ - (.+)$", out, re.M):
        name = m.group(1).split(" # ")[0].strip()
        if name not in names:
            names.append(name)
    return names


def _env(worktree):
    env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}
    env["NODE_PATH"] = str(worktree)  # spec tests resolve worktree files
    env["CI"] = "1"
    env["FORCE_COLOR"] = "0"
    return env


def _clean_env():
    """Environment for regression runs: no injected NODE_PATH.

    The regression command (the project's own suite) must run in a clean
    environment to preserve the threat model (ADR-010): the judge's verdict
    must not be shaped by the model's diff.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("NODE_OPTIONS", "NODE_PATH")}
    env["CI"] = "1"
    env["FORCE_COLOR"] = "0"
    return env


def _run(cmd, cwd, env, timeout):
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + "\n" + (e.stderr or "")
        return Result(False, 0, 0, out + "\nTIMEOUT after {}s".format(timeout),
                      True, -1)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    npassed, nfailed = _counts(out)
    # A suite that collected nothing is not a green suite.
    passed = p.returncode == 0 and npassed > 0
    return Result(passed, npassed, nfailed, out, False, p.returncode)


def run_tests(worktree, tests_dir, timeout=DEFAULT_TIMEOUT):
    tests_dir = Path(tests_dir)
    # A bare directory positional arg does not recurse on this node build; it is
    # required as if it were the test file itself. A glob pattern does.
    pattern = tests_dir.resolve().as_posix() + "/**/*.test.?(c|m)js"
    cmd = ["node", "--test", "--test-reporter=tap", pattern]
    return _run(cmd, tests_dir, _env(Path(worktree).resolve()), timeout)


def run_regression(worktree, argv, timeout=REGRESSION_TIMEOUT):
    """The project's own suite, full command, inside the worktree (same
    contract as vitest_runner.run_regression)."""
    res = _run(list(argv), worktree, _clean_env(), timeout)
    # The project's own reporter may not be TAP: exit code decides green.
    if res.returncode == 0 and not res.timed_out:
        return res._replace(passed=True)
    return res

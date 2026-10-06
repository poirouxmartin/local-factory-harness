"""Vitest counterpart of pytest_runner: the judge lives outside the tree.

Same threat model (ADR-010): spec tests live in jobs/<id>/tests/, never in the
worktree, and the config is harness-owned so a vitest.config.ts in the tree is
never read. Two JS-specific twists:

- A git worktree has no node_modules (untracked), and ESM resolution cannot be
  redirected with env vars. materialize_tests() junctions the source project's
  node_modules into BOTH the worktree (so target files resolve their imports)
  and the tests dir (so specs can `import { test } from "vitest"`). The model
  only ever emits target-file contents, so nothing writes through the link.
  cleanup() removes the links (rmdir on a junction detaches it, never recurses)
  BEFORE the worktree is torn down -- neither git nor a later rm -rf may ever
  walk into the real node_modules.

- The harness config is a dependency-free plain-object default export: it must
  load from a directory that has no node_modules of its own at import time.
"""
import os
import re
import subprocess
from collections import namedtuple
from pathlib import Path

DEFAULT_TIMEOUT = 600
REGRESSION_TIMEOUT = 900

# Vitest has no pytest-style "the command is wrong" exit codes: config errors,
# no-tests and red tests all exit 1. Operator errors are caught by the
# collected-nothing guard instead.
OPERATOR_ERROR_CODES = ()

Result = namedtuple("Result", "passed tests_passed tests_failed output timed_out returncode")

# Aliases mirror the house Next.js convention (lucena, conformergpd):
# "@"     -> <worktree>/src   (tsconfig "@/*": ["./src/*"])
# "@work" -> <worktree>       (spec tests import targets through this)
_CONFIG = """\
// Harness-owned. The job's spec cannot overwrite this file.
export default {{
  cacheDir: "{tests_dir}/.vite",
  test: {{
    environment: "node",
    include: ["**/*.test.?(c|m)[jt]s?(x)"],
    exclude: ["**/node_modules/**"],
  }},
  resolve: {{ alias: {{ "@": "{worktree}/src", "@work": "{worktree}" }} }},
}};
"""


def _link_node_modules(source, link_dir):
    """Junction (Windows) or symlink `link_dir`/node_modules -> `source`."""
    link = Path(link_dir) / "node_modules"
    if link.exists():
        return
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(source), str(link))
    else:
        os.symlink(str(source), str(link))


def _unlink_node_modules(link_dir):
    link = Path(link_dir) / "node_modules"
    # Only ever detach a link: a real directory here is not ours to delete.
    if link.is_dir() and (os.path.islink(link) or _is_junction(link)):
        os.rmdir(link)


def _is_junction(path):
    try:
        return bool(os.stat(path, follow_symlinks=False).st_reparse_tag)
    except (OSError, AttributeError):
        return False


def materialize_tests(tests_dir, tests, worktree, project_path=None):
    tests_dir = Path(tests_dir)
    tests_dir.mkdir(parents=True, exist_ok=True)
    worktree = Path(worktree).resolve()
    (tests_dir / "vitest.config.mjs").write_text(
        _CONFIG.format(tests_dir=tests_dir.resolve().as_posix(),
                       worktree=worktree.as_posix()),
        encoding="utf-8")
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
        _link_node_modules(source, worktree)
        _link_node_modules(source, tests_dir)
    return tests_dir


def cleanup(worktree, tests_dir):
    """Detach the node_modules links. Call BEFORE WorkTree.remove()."""
    _unlink_node_modules(worktree)
    _unlink_node_modules(tests_dir)


def failed_names(out):
    """Best-effort failing-test ids from vitest output.

    The per-failure summary lines look like ' FAIL  tests/x.test.ts > name';
    'Test Files  1 failed' has no FAIL prefix and is not matched.
    """
    names = []
    for m in re.finditer(r"^\s*FAIL\s{2,}(.+?)\s*$", out, re.M):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _counts(out):
    """Vitest prints 'Test Files  1 passed (1)' before 'Tests  2 passed (2)';
    only the Tests line counts tests.

    The summary may carry a dim-escape preamble (vitest 4.x even under
    FORCE_COLOR=0), so it is stripped first: the numbers are what counts, and
    the escapes are decoration the regex would otherwise trip on.
    """
    out = _ANSI.sub("", out)
    m = re.search(r"^\s*Tests\s+([^\n]*)", out, re.M)
    line = m.group(1) if m else ""
    failed = int(g.group(1)) if (g := re.search(r"(\d+) failed", line)) else 0
    passed = int(g.group(1)) if (g := re.search(r"(\d+) passed", line)) else 0
    return passed, failed


def _clean_env():
    env = {k: v for k, v in os.environ.items()
           if k not in ("NODE_OPTIONS", "NODE_PATH")}
    env["CI"] = "1"          # never watch mode, never prompts
    env["FORCE_COLOR"] = "0"  # counts parsing wants plain text
    # vitest 4.x still decorates the summary with ANSI under FORCE_COLOR=0
    # (measured 2026-08-18 on 4.1.9), and `_counts` reads that summary.
    env["NO_COLOR"] = "1"
    return env


def _run(cmd, cwd, timeout):
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=_clean_env(), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + "\n" + (e.stderr or "")
        return Result(False, 0, 0, out + "\nTIMEOUT after {}s".format(timeout), True, -1)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    npassed, nfailed = _counts(out)
    # A suite that collected nothing is not a green suite, whatever the exit code.
    passed = p.returncode == 0 and npassed > 0
    return Result(passed, npassed, nfailed, out, False, p.returncode)


def run_tests(worktree, tests_dir, timeout=DEFAULT_TIMEOUT):
    tests_dir = Path(tests_dir)
    entry = tests_dir / "node_modules" / "vitest" / "vitest.mjs"
    cmd = ["node", str(entry), "run",
           "--config", str(tests_dir / "vitest.config.mjs"),
           "--root", str(tests_dir)]
    return _run(cmd, tests_dir, timeout)


def run_regression(worktree, argv, timeout=REGRESSION_TIMEOUT):
    """Run the project's own suite inside the worktree.

    Unlike pytest_runner, argv is the FULL command (the interpreter is not
    ours to choose), e.g. ["node", "node_modules/vitest/vitest.mjs", "run"].
    The worktree's node_modules junction makes it resolvable.
    """
    return _run(list(argv), worktree, timeout)

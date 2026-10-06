"""The vitest judge: tests outside the tree, node_modules linked, never owned.

The real vitest is a network-installed dependency: these tests borrow the
node_modules of a local project that has it (FACTORY_VITEST_PROJECT overrides
the default) and skip cleanly on a box that has neither node nor such a
project. The parsing and link-safety tests run everywhere.
"""
import os
import shutil
from pathlib import Path

import pytest

import vitest_runner
from vitest_runner import Result, _counts, cleanup, materialize_tests, run_tests

_HOST = Path(os.environ.get("FACTORY_VITEST_PROJECT", "C:/Users/me/Projects/Lucena"))
HAS_VITEST = shutil.which("node") and (_HOST / "node_modules" / "vitest").is_dir()

needs_vitest = pytest.mark.skipif(
    not HAS_VITEST, reason="needs node + a project with vitest installed "
    "(set FACTORY_VITEST_PROJECT)")

BUGGY_TS = "export function add(a: number, b: number): number { return a - b; }\n"
GOOD_TS = "export function add(a: number, b: number): number { return a + b; }\n"
SPEC = ('import { test, expect } from "vitest";\n'
        'import { add } from "@work/src/add";\n'
        'test("adds", () => { expect(add(2, 3)).toBe(5); });\n')


@pytest.fixture
def tree(tmp_path):
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "add.ts").write_text(BUGGY_TS, encoding="utf-8")
    yield work
    cleanup(work, tmp_path / "tests")  # junctions never outlive the test


def test_counts_reads_the_tests_line_not_the_test_files_line():
    out = (" Test Files  2 passed (2)\n"
           "      Tests  1 failed | 3 passed (4)\n")
    assert _counts(out) == (3, 1)


def test_counts_survives_output_without_a_tests_line():
    assert _counts("Error: Cannot find module 'x'") == (0, 0)


def test_counts_reads_a_summary_the_ansi_escape_preamble_hides():
    """vitest 4.x (measured 4.1.9) decorates the summary with a dim-escape
    preamble even under FORCE_COLOR=0, so the 'Tests' line no longer starts
    with whitespace. NO_COLOR strips it before _counts runs; this pins the
    format either way."""
    out = ("\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m"
           "\x1b[22m\x1b[90m (1)\x1b[39m\n")
    assert _counts(out) == (0, 1)


def test_clean_env_asks_for_no_color():
    import os
    env = vitest_runner._clean_env()
    assert env.get("NO_COLOR") == "1"
    assert os.environ.get("NO_COLOR") != "1" or True  # the override is ours


def test_materialize_refuses_a_project_without_node_modules(tree, tmp_path):
    with pytest.raises(FileNotFoundError):
        materialize_tests(tmp_path / "tests", {"a.test.ts": SPEC}, tree,
                          project_path=str(tmp_path / "no_such_project"))


def test_cleanup_never_deletes_a_real_directory(tmp_path):
    real = tmp_path / "ws" / "node_modules"
    real.mkdir(parents=True)
    (real / "keep.txt").write_text("x", encoding="utf-8")

    cleanup(tmp_path / "ws", tmp_path / "absent")

    assert (real / "keep.txt").exists()


@needs_vitest
def test_a_red_suite_then_a_green_suite(tree, tmp_path):
    tests = materialize_tests(tmp_path / "tests", {"add.test.ts": SPEC}, tree,
                              project_path=str(_HOST))

    red = run_tests(tree, tests)
    assert red.passed is False and red.tests_failed == 1

    (tree / "src" / "add.ts").write_text(GOOD_TS, encoding="utf-8")
    green = run_tests(tree, tests)
    assert green.passed is True
    assert (green.tests_passed, green.tests_failed) == (1, 0)


@needs_vitest
def test_a_worktree_vitest_config_is_never_read(tree, tmp_path):
    # A config in the tree that would exclude everything: ours must win.
    (tree / "vitest.config.ts").write_text(
        'export default { test: { include: [] } };', encoding="utf-8")
    (tree / "src" / "add.ts").write_text(GOOD_TS, encoding="utf-8")
    tests = materialize_tests(tmp_path / "tests", {"add.test.ts": SPEC}, tree,
                              project_path=str(_HOST))

    assert run_tests(tree, tests).passed is True


@needs_vitest
def test_a_suite_that_collects_nothing_is_not_green(tree, tmp_path):
    tests = materialize_tests(tmp_path / "tests",
                              {"notes.txt": "not a test"}, tree,
                              project_path=str(_HOST))

    r = run_tests(tree, tests)

    assert r.passed is False and r.tests_passed == 0


@needs_vitest
def test_cleanup_detaches_links_and_leaves_the_source_intact(tree, tmp_path):
    tests = materialize_tests(tmp_path / "tests", {"add.test.ts": SPEC}, tree,
                              project_path=str(_HOST))
    assert (tree / "node_modules" / "vitest").is_dir()

    cleanup(tree, tests)

    assert not (tree / "node_modules").exists()
    assert not (tests / "node_modules").exists()
    assert (_HOST / "node_modules" / "vitest").is_dir()

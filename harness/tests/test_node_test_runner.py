"""The node:test judge: tests outside the tree, TAP counts, never owned.

Mirrors test_vitest_runner.py's structure. node:test is built into the
runtime (no node_modules dependency needed for these core cases), so unlike
vitest these tests do not skip on a bare node install.
"""
import os

import node_test_runner
from node_test_runner import _counts, _clean_env


def test_run_tests_passes_a_green_suite(tmp_path):
    worktree = tmp_path / "wt"
    (worktree / "lib").mkdir(parents=True)
    (worktree / "lib" / "add.js").write_text(
        "module.exports = (a, b) => a + b;\n", encoding="utf-8")
    tests = {"spec.test.js":
             'const test = require("node:test");\n'
             'const assert = require("node:assert");\n'
             'const add = require("lib/add.js");\n'
             'test("adds", () => assert.strictEqual(add(1, 2), 3));\n'}
    tests_dir = node_test_runner.materialize_tests(
        tmp_path / "tests", tests, worktree)

    res = node_test_runner.run_tests(worktree, tests_dir)

    assert res.passed and res.tests_passed == 1 and res.tests_failed == 0


def test_run_tests_counts_failures(tmp_path):
    worktree = tmp_path / "wt"
    (worktree / "lib").mkdir(parents=True)
    (worktree / "lib" / "add.js").write_text(
        "module.exports = (a, b) => a + b;\n", encoding="utf-8")
    tests = {"spec.test.js":
             'const test = require("node:test");\n'
             'const assert = require("node:assert");\n'
             'const add = require("lib/add.js");\n'
             'test("adds", () => assert.strictEqual(add(1, 2), 4));\n'}
    tests_dir = node_test_runner.materialize_tests(
        tmp_path / "tests", tests, worktree)

    res = node_test_runner.run_tests(worktree, tests_dir)

    assert res.passed is False and res.tests_failed == 1


def test_an_empty_collection_is_not_green(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True)
    tests = {"notes.txt": "not a test"}
    tests_dir = node_test_runner.materialize_tests(
        tmp_path / "tests", tests, worktree)

    res = node_test_runner.run_tests(worktree, tests_dir)

    assert res.passed is False


def test_counts_reads_tap_pass_and_fail_lines():
    out = "TAP version 13\n1..1\nok 1 - adds\n# pass 1\n# fail 0\n"
    assert _counts(out) == (1, 0)


def test_counts_survives_output_without_tap_lines():
    assert _counts("Error: Cannot find module 'x'") == (0, 0)


def test_regression_env_does_not_include_node_path():
    """Regression runs must use a clean env to preserve ADR-010."""
    env = _clean_env()
    assert "NODE_PATH" not in env
    assert "NODE_OPTIONS" not in env
    assert env.get("CI") == "1"
    assert env.get("FORCE_COLOR") == "0"

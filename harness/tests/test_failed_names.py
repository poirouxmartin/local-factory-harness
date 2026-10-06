"""failed_names(): WHICH tests fail, per runner (bottlenecks doc, item 1).

Pure text parsers over real reporter output shapes; best-effort by design --
an empty list is always acceptable, a wrong name is not.
"""
import node_test_runner
import pytest_runner
import vitest_runner

PYTEST_OUT = """\
FF.                                                                      [100%]
=================================== FAILURES ===================================
FAILED tests/test_calc.py::test_add - assert 3 == 4
FAILED tests/test_calc.py::test_sub - assert 1 == 2
FAILED tests/test_calc.py::test_add - duplicated line
2 failed, 1 passed in 0.05s
"""

VITEST_OUT = """\
 RUN  v4.1.5 /jobs/j_x/tests

 ❯ tests/spec.test.ts (8 tests | 2 failed) 40ms
   × categoryScores > a clean scan scores 100 everywhere 12ms
 FAIL  tests/spec.test.ts > categoryScores > a clean scan scores 100 everywhere
 FAIL  tests/spec.test.ts > categoryScores > caps at 74
 Test Files  1 failed (1)
      Tests  2 failed | 6 passed (8)
"""

NODE_OUT = """\
TAP version 13
not ok 1 - very low / negative elo clamps to Bronze IV
    ---
    location: '/tests/ranks.test.js:33:1'
    ...
not ok 2 - Bronze divisions step every 50 points # TODO flaky
ok 3 - passing one
# tests 3
# pass 1
# fail 2
"""


def test_pytest_failed_names_dedupe_and_order():
    assert pytest_runner.failed_names(PYTEST_OUT) == [
        "tests/test_calc.py::test_add", "tests/test_calc.py::test_sub"]


def test_vitest_failed_names_skip_the_summary_lines():
    assert vitest_runner.failed_names(VITEST_OUT) == [
        "tests/spec.test.ts > categoryScores > a clean scan scores 100 everywhere",
        "tests/spec.test.ts > categoryScores > caps at 74"]


def test_node_failed_names_strip_tap_directives():
    assert node_test_runner.failed_names(NODE_OUT) == [
        "very low / negative elo clamps to Bronze IV",
        "Bronze divisions step every 50 points"]


def test_all_runners_return_empty_on_green_output():
    for mod, out in ((pytest_runner, "3 passed in 0.1s"),
                     (vitest_runner, " Tests  8 passed (8)"),
                     (node_test_runner, "# pass 8\n# fail 0")):
        assert mod.failed_names(out) == []

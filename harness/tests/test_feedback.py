"""condense(): the failure feedback a retry actually gets to reason about.

The bug this pins (banc A, 2026-07-25): `result.output[-3000:]` kept the TAP
tail, so on a 12k-char / 9-failure run the model was shown ONE failing test --
never a Bronze one, always a downstream consequence. Every model stalled
identically (30b 2->4->4, 35b 3->3, Haiku 4->4). Breadth before depth: naming
every failure costs ~40 chars each and is the part the model cannot infer.
"""
import feedback

TAP_OUT = """\
TAP version 13
# Subtest: very low / negative elo clamps to Bronze IV
not ok 2 - very low / negative elo clamps to Bronze IV
  ---
  duration_ms: 0.15
  type: 'test'
  location: 'C:\\\\Users\\\\me\\\\runs\\\\x\\\\tests\\\\ranks.test.js:30:1'
  failureType: 'testCodeFailure'
  error: |-
    Expected values to be strictly deep-equal:
    + actual - expected
      {
    +   division: 1,
    -   division: 4,
      }
  code: 'ERR_ASSERTION'
  name: 'AssertionError'
  operator: 'deepStrictEqual'
  stack: |-
    TestContext.<anonymous> (C:\\\\Users\\\\me\\\\runs\\\\x\\\\tests\\\\ranks.test.js:31:10)
    Test.runInAsyncScope (node:async_hooks:226:14)
    Test.run (node:internal/test_runner/test:1201:25)
    async Test.processPendingSubtests (node:internal/test_runner/test:831:7)
  ...
# Subtest: Bronze divisions step every 50 points
not ok 4 - Bronze divisions step every 50 points
  ---
  error: |-
    Expected values to be strictly equal:
    + actual - expected
    + 4
    - 3
  stack: |-
    Test.run (node:internal/test_runner/test:1258:12)
  ...
# Subtest: 1800 crosses into Master
not ok 11 - 1800 crosses into Master
  ---
  error: |-
    Expected values to be strictly deep-equal:
    + actual - expected
    +   tier: 'Diamond'
    -   tier: 'Master'
  stack: |-
    Test.run (node:internal/test_runner/test:1258:12)
  ...
# pass 4
# fail 3
"""

NAMES = ["very low / negative elo clamps to Bronze IV",
         "Bronze divisions step every 50 points",
         "1800 crosses into Master"]


def test_every_failing_name_survives_a_budget_that_fits_only_one_block():
    # The whole point: the old tail slice showed 1 of 9. Breadth is not optional.
    out = feedback.condense(TAP_OUT, NAMES, budget=400)
    for name in NAMES:
        assert name in out
    assert len(out) <= 400


def test_stack_frames_and_locations_are_dropped():
    out = feedback.condense(TAP_OUT, NAMES, budget=3000)
    for noise in ("node:internal", "runInAsyncScope", "location:", "duration_ms",
                  "operator:", "failureType:"):
        assert noise not in out


def test_expected_and_actual_keep_their_labels():
    # Dropping the key but keeping its children left two anonymous, mutually
    # contradictory blocks -- actively misleading, worse than the noise saved.
    tap = TAP_OUT.replace("  ...", "  expected:\n    division: 4\n"
                                   "  actual:\n    division: 1\n  ...", 1)
    out = feedback.condense(tap, NAMES[:1], budget=3000)
    assert "expected:" in out
    assert "actual:" in out


def test_assertion_evidence_is_kept():
    out = feedback.condense(TAP_OUT, NAMES, budget=3000)
    assert "+ actual - expected" in out
    assert "division: 4" in out
    assert "tier: 'Master'" in out


def test_output_never_exceeds_budget():
    for budget in (120, 400, 1000, 3000, 50000):
        assert len(feedback.condense(TAP_OUT, NAMES, budget=budget)) <= budget


def test_unrecognized_output_falls_back_to_the_tail():
    # A runner we have no parser for must degrade to the old behaviour, not to
    # an empty string: some evidence beats none.
    raw = "".join("line {}\n".format(i) for i in range(500))
    out = feedback.condense(raw, [], budget=200)
    assert out
    assert out.endswith("line 499\n") or "line 499" in out
    assert len(out) <= 200


def test_empty_output_stays_empty():
    assert feedback.condense("", [], budget=3000) == ""


def test_names_alone_fit_when_detail_cannot():
    many = ["failing test number {}".format(i) for i in range(40)]
    out = feedback.condense(TAP_OUT, many, budget=300)
    assert len(out) <= 300
    assert "failing test number 0" in out
    # Truncation of the list itself must be announced, never silent.
    assert "more" in out


def test_detail_is_cut_on_a_failure_boundary_not_mid_assertion():
    # A hard slice used to end the evidence on `+   label: '` -- a fragment the
    # model can only misread. Whole blocks or nothing.
    # 560: the names fit, the three blocks do not -- the case the cut is about.
    out = feedback.condense(TAP_OUT, NAMES, budget=560)
    assert len(out) <= 560
    body = out.split("\n\n", 1)[-1]
    assert "[..." in out, "dropped blocks must be announced"
    for line in body.splitlines():
        assert not line.rstrip().endswith(": '"), line


def test_subtest_headers_are_dropped_as_redundant():
    out = feedback.condense(TAP_OUT, NAMES, budget=3000)
    assert "# Subtest:" not in out
    # ...but the failure names themselves survive, they are the payload.
    for name in NAMES:
        assert name in out


def test_a_lone_failure_header_with_no_evidence_is_not_kept():
    out = feedback.condense(TAP_OUT, NAMES, budget=450)
    for line in out.strip().splitlines():
        assert not line.startswith("not ok") or "error" in out, line
    assert not out.strip().splitlines()[-1].startswith("not ok")

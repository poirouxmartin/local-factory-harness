"""What the factory learns about itself, across sessions.

The agent's own attempt (de04bb4) shipped inert: nothing wrote a lesson and
the reader raised NameError on three constants that do not exist. These tests
hold the replacement to a contract.
"""
import lessons
import session_analysis


def _lesson(pattern, count=1, seen=100.0, suggestion="do better"):
    return {"pattern": pattern, "count": count, "suggestion": suggestion,
            "last_seen": seen}


def test_merge_adds_an_unseen_pattern():
    out = lessons.merge([], [_lesson("wasted_tool_calls")])
    assert [l["pattern"] for l in out] == ["wasted_tool_calls"]
    assert out[0]["count"] == 1


def test_merge_bumps_the_count_of_a_known_pattern():
    known = [_lesson("wasted_tool_calls", count=2, seen=100.0)]
    out = lessons.merge(known, [_lesson("wasted_tool_calls", count=3,
                                        seen=200.0)])
    assert len(out) == 1
    assert out[0]["count"] == 5
    assert out[0]["last_seen"] == 200.0


def test_merge_never_mutates_its_input():
    # The store hands out its own list; a mutation there would change the
    # system prompt mid-session and break KV prefix reuse.
    known = [_lesson("errors", count=1)]
    lessons.merge(known, [_lesson("errors", count=1)])
    assert known[0]["count"] == 1


def test_from_audit_turns_a_flagged_session_into_lessons():
    report = {"session_id": "c_1", "wasted_calls": 27, "tool_errors": 5,
              "loops": 0, "verdict": "flagged"}
    out = lessons.from_audit(report, now=42.0)
    patterns = {l["pattern"] for l in out}
    assert "wasted_tool_calls" in patterns
    assert "tool_errors" in patterns
    assert all(l["last_seen"] == 42.0 for l in out)
    assert all(l["suggestion"] for l in out)  # a fact alone teaches nothing


def test_from_audit_stays_quiet_on_a_clean_session():
    report = {"session_id": "c_2", "wasted_calls": 0, "tool_errors": 0,
              "loops": 0, "verdict": "clean"}
    assert lessons.from_audit(report, now=42.0) == []


def test_from_audit_ignores_noise_below_the_threshold():
    # One duplicated call in a long session is not a lesson, it is a Tuesday.
    report = {"session_id": "c_3", "wasted_calls": 1, "tool_errors": 1,
              "loops": 0, "verdict": "flagged"}
    assert lessons.from_audit(report, now=42.0) == []


def test_render_orders_by_count_then_pattern():
    # Deterministic order: the block is a KV prefix, not a report.
    block = lessons.render([
        _lesson("bbb", count=1, suggestion="say bbb"),
        _lesson("aaa", count=1, suggestion="say aaa"),
        _lesson("zzz", count=9, suggestion="say zzz")])
    assert block.index("say zzz") < block.index("say aaa") < block.index("say bbb")


def test_render_is_byte_stable_for_the_same_lessons():
    ls = [_lesson("a", count=2), _lesson("b", count=1)]
    assert lessons.render(ls) == lessons.render(list(reversed(ls)))


def test_render_caps_the_number_of_lessons():
    ls = [_lesson("p%02d" % i, count=i) for i in range(30)]
    block = lessons.render(ls)
    assert block.count("\n- ") <= lessons.MAX_LESSONS


def test_render_honours_the_char_budget():
    ls = [_lesson("p%d" % i, count=i, suggestion="x" * 400) for i in range(10)]
    assert len(lessons.render(ls)) <= lessons.BUDGET_CHARS


def test_render_of_nothing_is_empty():
    # No lessons must add no bytes at all to the prompt, not an empty header.
    assert lessons.render([]) == ""


# ---- from_trace: lessons priced by the event trace -------------------------


def _report(*findings, **kw):
    """A session_analysis-shaped report carrying just these findings."""
    out = {"findings": list(findings), "models": kw.get("models", ["m"]),
           "verdict": "flagged" if findings else "clean"}
    return out


def _found(code, detail="d", count=1, cost_s=0.0, cost_tokens=0):
    return {"code": code, "detail": detail, "count": count, "cost_s": cost_s,
            "cost_tokens": cost_tokens,
            "weight_s": cost_s + cost_tokens / 800.0}


def test_from_trace_turns_a_replay_finding_into_a_priced_lesson():
    found = lessons.from_trace(
        _report(_found("replayed_calls", "9 identical calls replayed", count=9,
                       cost_s=41.0, cost_tokens=18000)), now=5.0)
    assert [l["pattern"] for l in found] == ["replayed_calls"]
    lesson = found[0]
    # The cost is what makes the lesson worth its place in the prompt.
    assert "41" in lesson["suggestion"] and "18000" in lesson["suggestion"]
    assert lesson["last_seen"] == 5.0


def test_from_trace_names_the_file_that_was_read_again():
    found = lessons.from_trace(
        _report(_found("hot_target", "read_file harness/web/app.js x29",
                       count=29)), now=1.0)
    assert "harness/web/app.js" in found[0]["suggestion"]


def test_from_trace_counts_one_per_session_not_one_per_occurrence():
    # `merge` adds counts, and the analysis re-runs over the whole trace every
    # turn: a count of 29 here would read as 29 sessions after one.
    found = lessons.from_trace(
        _report(_found("hot_target", "read_file a.js x29", count=29)), now=1.0)
    assert found[0]["count"] == 1


def test_from_trace_ignores_findings_a_model_cannot_act_on():
    found = lessons.from_trace(
        _report(_found("window_pressure", "window peaked at 99 %"),
                _found("slow_tool", "search took 12 s"),
                _found("map_degraded", "1x: fell back to directories")),
        now=1.0)
    assert found == []


def test_from_trace_scopes_a_lesson_to_the_model_that_earned_it():
    found = lessons.from_trace(
        _report(_found("failing_tool", "search failed 3x: MemoryError"),
                models=["qwen3-coder:30b"]), now=1.0)
    assert found[0]["scope"] == {"model": "qwen3-coder:30b"}


def test_from_trace_leaves_the_scope_open_when_two_models_ran():
    found = lessons.from_trace(
        _report(_found("failing_tool", "search failed 3x: boom"),
                models=["a", "b"]), now=1.0)
    assert "scope" not in found[0]


def test_from_trace_of_a_clean_session_teaches_nothing():
    assert lessons.from_trace(_report(), now=1.0) == []


def test_every_trace_lesson_pattern_is_a_finding_code():
    codes = [_found(c) for c in session_analysis.MODEL_FINDINGS]
    found = lessons.from_trace(_report(*codes), now=1.0)
    assert [l["pattern"] for l in found] == list(
        session_analysis.MODEL_FINDINGS)
    for lesson in found:
        assert lesson["suggestion"] and lesson["suggestion"] != lesson["pattern"]


def test_trace_lessons_fit_the_prompt_budget():
    codes = [_found(c, detail="x" * 200, cost_s=1.0) for c in
             session_analysis.MODEL_FINDINGS]
    block = lessons.render(lessons.from_trace(_report(*codes), now=1.0))
    assert len(block) <= lessons.BUDGET_CHARS

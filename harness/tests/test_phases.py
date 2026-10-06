"""Where the session is, and what it is not allowed to skip."""
import phases


def _plan(*steps, **kw):
    return {"goal": kw.get("goal", "faire la chose"),
            "steps": [{"step": s, "done_when": "file:x.py", "done": d,
                       "walls": []} for s, d in steps],
            "revisions": 0}


def _call(name, **args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def _result(tool_name, content="ok"):
    return {"role": "tool", "tool_name": tool_name, "content": content}


def _read(path="a.py"):
    return [_call("read_file", path=path), _result("read_file", "x = 1")]


def _set_plan():
    return [_call("set_plan", goal="g", steps=[]), _result("plan", "plan set")]


def _write(path="a.py"):
    return [_call("write_file", path=path, content="x"), _result("write_file")]


def _run(cmd):
    return [_call("run_command", command=cmd), _result("run_command", "out")]


# ---- phases ----

def test_a_session_without_a_plan_is_exploring():
    ev = phases.evidence(_read() + _read("b.py"))
    assert phases.phase_of(None, ev) == "explore"


def test_a_plan_with_nothing_written_yet_is_the_plan_phase():
    ev = phases.evidence(_read() + _set_plan())
    assert phases.phase_of(_plan(("un", False)), ev) == "plan"


def test_a_write_after_the_plan_is_implementation():
    ev = phases.evidence(_set_plan() + _write())
    assert phases.phase_of(_plan(("un", False)), ev) == "implement"


def test_a_write_before_the_plan_does_not_count_as_implementation():
    # The plan resets the clock: what was written while exploring was not
    # written against a step.
    ev = phases.evidence(_write() + _set_plan())
    assert phases.phase_of(_plan(("un", False)), ev) == "plan"


def test_a_fully_ticked_plan_is_the_verify_phase():
    ev = phases.evidence(_set_plan() + _write())
    assert phases.phase_of(_plan(("un", True), ("deux", True)), ev) == "verify"


def test_the_phase_of_nothing_is_explore():
    assert phases.phase_of(None, phases.evidence([])) == "explore"


# ---- what the transcript proves ----

def test_a_test_command_counts_as_a_verification():
    ev = phases.evidence(_run("cd /repo && python -m pytest tests -q"))
    assert ev["verified_at"] >= 0


def test_an_ordinary_command_is_not_a_verification():
    ev = phases.evidence(_run("cd /repo && git status"))
    assert ev["verified_at"] == -1


def test_a_verification_before_the_last_write_does_not_count():
    ev = phases.evidence(_run("npm test") + _write())
    assert phases.verified_after_write(ev) is False


def test_a_verification_after_the_last_write_counts():
    ev = phases.evidence(_write() + _run("npm test"))
    assert phases.verified_after_write(ev) is True


def test_arguments_arriving_as_json_text_are_still_read():
    # llama-server hands tool arguments back as a JSON string as often as a
    # dict; a phase engine that only reads dicts would see no writes at all.
    messages = [{"role": "assistant", "tool_calls": [{"function": {
        "name": "write_file",
        "arguments": '{"path": "a.py", "content": "x"}'}}]},
        _result("write_file")]
    assert phases.evidence(messages)["wrote_at"] >= 0


# ---- the one hard gate ----

def test_a_write_without_a_plan_is_refused_once_the_session_is_underway():
    ev = phases.evidence(_read() + _read("b.py") + _read("c.py"))
    ok, reason = phases.gate("write_file", None, ev, mode="auto")
    assert ok is False
    assert "set_plan" in reason


def test_the_first_write_of_a_short_session_is_never_refused():
    # "cree-moi ce fichier" writes on action 1 and owes nobody a plan.
    ev = phases.evidence(_read())
    assert phases.gate("write_file", None, ev, mode="auto")[0] is True


def test_a_write_with_a_plan_is_never_refused():
    ev = phases.evidence(_read() + _read("b.py") + _read("c.py") + _set_plan())
    assert phases.gate("write_file", _plan(("un", False)), ev,
                       mode="auto")[0] is True


def test_reading_is_never_gated():
    ev = phases.evidence(_read() + _read("b.py") + _read("c.py"))
    assert phases.gate("read_file", None, ev, mode="auto")[0] is True


def test_approve_mode_gates_nothing_the_operator_already_sees():
    ev = phases.evidence(_read() + _read("b.py") + _read("c.py"))
    assert phases.gate("write_file", None, ev, mode="approve")[0] is True


# ---- the signals ----

def test_a_complete_but_unverified_plan_is_signalled():
    ev = phases.evidence(_set_plan() + _write())
    lines = phases.alerts(_plan(("un", True)), ev, [])
    assert any("NON VERIFIE" in l for l in lines)


def test_a_complete_and_verified_plan_is_not_signalled():
    ev = phases.evidence(_set_plan() + _write() + _run("pytest -q"))
    lines = phases.alerts(_plan(("un", True)), ev, [])
    assert not any("NON VERIFIE" in l for l in lines)


def test_the_phase_itself_is_shown_with_the_step_count():
    ev = phases.evidence(_set_plan() + _write())
    lines = phases.alerts(_plan(("un", True), ("deux", False)), ev, [])
    assert any("PHASE implement" in l and "1/2" in l for l in lines)


def test_the_costliest_findings_are_quoted_back_into_the_turn():
    # The system prompt is frozen for the session, so a lesson learned now
    # lands on the NEXT one. The tail is transient: this is how the agent hears
    # about its own waste while it is still wasting it.
    findings = [{"code": "hot_target", "detail": "read_file app.js x29",
                 "cost_s": 12.0, "cost_tokens": 18000, "weight_s": 34.5},
                {"code": "replayed_calls", "detail": "9 identical calls "
                 "replayed", "cost_s": 41.0, "cost_tokens": 3000,
                 "weight_s": 44.8}]
    lines = phases.alerts(None, phases.evidence([]), findings)
    quoted = " ".join(lines)
    assert "app.js" in quoted and "9 identical" in quoted


def test_a_harness_finding_is_not_quoted_at_the_model():
    lines = phases.alerts(None, phases.evidence([]),
                          [{"code": "map_degraded", "detail": "directories",
                            "cost_s": 0.0, "cost_tokens": 0, "weight_s": 0.0}])
    assert not any("map_degraded" in l for l in lines)


def test_the_alerts_are_bounded():
    findings = [{"code": "hot_target", "detail": "x" * 400, "cost_s": 1.0,
                 "cost_tokens": 1, "weight_s": float(i)} for i in range(10)]
    lines = phases.alerts(_plan(("un", False)), phases.evidence([]), findings)
    assert len(lines) <= phases.MAX_ALERTS
    assert all(len(l) <= phases.LINE_CHARS for l in lines)


def test_a_refused_write_is_not_evidence_of_implementation():
    # The gate answers a refused write with a tool message under the same tool
    # name. Counting the REQUEST would flip the session to `implement` on a
    # write that never touched the disk.
    messages = [_call("write_file", path="a.py", content="x"),
                {"role": "tool", "tool_name": "write_file",
                 "content": "refused: 3 actions without a plan."}]
    assert phases.evidence(messages)["wrote_at"] == -1


def test_a_failed_write_is_not_evidence_of_implementation():
    messages = [_call("write_file", path="/nope/a.py", content="x"),
                {"role": "tool", "tool_name": "write_file",
                 "content": "error: outside the workspace"}]
    assert phases.evidence(messages)["wrote_at"] == -1


def test_a_write_that_ran_is_evidence_even_without_its_request():
    # The result is the proof; the request is only an intention.
    assert phases.evidence([_result("write_file", "wrote a.py")])[
        "wrote_at"] == 0

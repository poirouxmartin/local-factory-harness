import chat_context
from chat_context import (build_prompt, compaction_span, estimate,
                          evict_tool_outputs, needs_compaction, prompt_budget,
                          ratio, summary_message, EVICTED_STUB)


def test_ratio_defaults_and_calibrates_with_clamping():
    assert ratio({}) == 3.0
    assert ratio({"calibration": {"chars": 350, "tokens": 100}}) == 3.5
    assert ratio({"calibration": {"chars": 1000, "tokens": 100}}) == 5.0
    assert ratio({"calibration": {"chars": 100, "tokens": 100}}) == 2.0
    assert ratio({"calibration": {"chars": 0, "tokens": 0}}) == 3.0


def test_prompt_budget_reserves_output_room_with_a_floor():
    # The reserve is a tuning knob (raised to 16384 on 2026-07-25 so a thinking
    # model's turn stops being cut); what must hold is the relation, not the
    # number -- and that the chat window stays mostly prompt.
    assert prompt_budget(65536) == 65536 - chat_context.OUTPUT_RESERVE
    assert prompt_budget(65536) > 65536 // 2
    # tiny windows (tests, future overrides) must not go negative
    assert prompt_budget(400) == 100


def test_estimate_counts_chars_over_ratio_plus_message_overhead():
    assert estimate("x" * 300, 3.0) == 100
    msgs = [{"role": "user", "content": "x" * 300},
            {"role": "assistant", "content": ""}]
    assert estimate(msgs, 3.0) == 100 + 2 * chat_context._MSG_OVERHEAD


def test_estimate_counts_tool_call_payloads():
    # An agent write_file sends the whole file inside tool_calls; counting it
    # as zero is how the 2026-07-14 session overflowed 65536 without ever
    # triggering compaction.
    call = {"function": {"name": "write_file",
                         "arguments": {"path": "a.py", "content": "z" * 3000}}}
    bare = estimate([{"role": "assistant", "content": "j'écris"}], 3.0)
    with_call = estimate([{"role": "assistant", "content": "j'écris",
                           "tool_calls": [call]}], 3.0)
    assert with_call >= bare + 1000  # 3000 chars of arguments at ratio 3


def test_evict_stubs_old_tool_outputs_only():
    msgs = [{"role": "tool", "tool_name": "read_file", "content": "big" * 100},
            {"role": "assistant", "content": "vu"},
            {"role": "tool", "tool_name": "run_command", "content": "recent"},
            {"role": "user", "content": "ok"}]
    out, evicted = evict_tool_outputs(msgs, keep_last=2)
    assert evicted == 1
    assert out[0]["content"] == EVICTED_STUB
    assert out[0]["tool_name"] == "read_file"  # only content is stubbed
    assert out[1]["content"] == "vu"
    assert out[2]["content"] == "recent"       # within keep_last
    assert msgs[0]["content"] == "big" * 100   # input not mutated


def test_evict_keep_last_zero_stubs_everything():
    msgs = [{"role": "tool", "content": "a"}, {"role": "tool", "content": "b"}]
    out, evicted = evict_tool_outputs(msgs, keep_last=0)
    assert evicted == 2 and all(m["content"] == EVICTED_STUB for m in out)


def test_evict_strips_old_tool_call_arguments():
    old = {"function": {"name": "write_file",
                        "arguments": {"path": "a.py", "content": "z" * 500}}}
    recent = {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}
    msgs = [{"role": "assistant", "content": "j'écris", "tool_calls": [old]},
            {"role": "user", "content": "ok"},
            {"role": "assistant", "content": "", "tool_calls": [recent]}]
    out, evicted = evict_tool_outputs(msgs, keep_last=2)
    assert evicted == 1
    assert out[0]["tool_calls"][0]["function"]["name"] == "write_file"
    assert out[0]["tool_calls"][0]["function"]["arguments"] == {}
    assert out[2]["tool_calls"][0]["function"]["arguments"] == {"path": "a.py"}
    # input never mutated
    assert msgs[0]["tool_calls"][0]["function"]["arguments"]["content"]


def test_needs_compaction_can_be_forced_but_only_with_a_span():
    # force = the previous turn's REAL token counts overflowed; estimates are
    # overruled, but there must still be something beyond the verbatim tail.
    assert not needs_compaction(_msgs(8, size=30), None, 1000, 3.0)
    assert needs_compaction(_msgs(8, size=30), None, 1000, 3.0, force=True)
    # nothing beyond the verbatim tail -> still nothing to compact
    assert not needs_compaction(_msgs(3, size=30), None, 1000, 3.0, force=True)


def _msgs(n, size=300, role="user"):
    return [{"role": role, "content": "m%d " % i + "x" * size}
            for i in range(n)]


def test_needs_compaction_only_above_threshold_with_a_span_to_compact():
    # budget 1000, threshold 700 tokens; each msg ~105 tokens at ratio 3
    assert not needs_compaction(_msgs(4), None, 1000, 3.0)
    assert needs_compaction(_msgs(10), None, 1000, 3.0)
    # nothing beyond the verbatim tail -> nothing to compact
    assert not needs_compaction(_msgs(6, size=30000), None, 1000, 3.0)


def test_needs_compaction_measures_the_real_history_not_the_evicted_copy():
    # Session c_1a722191 (2026-07-20): 425 agent messages over 24 h, and
    # compaction fired exactly zero times. The bulk of an agent session is
    # tool output, which evict_tool_outputs stubs before the estimate was
    # taken -- the measured span plateaued around 16k against a ~40k
    # threshold, so the trigger was structurally unreachable and the history
    # was thrown away instead of summarized. Eviction is a transport
    # optimisation; it must not decide whether history is worth keeping.
    agent_session = [{"role": "tool", "content": "x" * 900} for _ in range(400)]
    assert needs_compaction(agent_session, None, 60000, 3.0)


def test_needs_compaction_ignores_messages_already_covered_by_summary():
    msgs = _msgs(20)
    summary = {"content": "résumé", "covers_until": 14}
    # only 6 live messages left -> nothing to compact
    assert not needs_compaction(msgs, summary, 1000, 3.0)


def test_compaction_span_is_everything_live_minus_the_tail():
    msgs = _msgs(10)
    span, covers = compaction_span(msgs, None)
    assert covers == 4 and span == msgs[:4]
    span, covers = compaction_span(msgs, {"content": "s", "covers_until": 2})
    assert covers == 4 and span == msgs[2:4]


def test_build_prompt_sends_everything_when_it_fits():
    msgs = _msgs(3)
    sent, info = build_prompt([], None, msgs, 10000, 3.0)
    assert sent == msgs
    assert info["evicted"] == 0 and info["dropped"] == 0
    assert info["used_summary"] is False and info["est_tokens"] > 0


def test_build_prompt_injects_summary_and_skips_covered_messages():
    msgs = _msgs(10)
    summary = {"content": "les faits", "covers_until": 6}
    sent, info = build_prompt(
        [{"role": "system", "content": "agent"}], summary, msgs, 10000, 3.0)
    assert sent[0] == {"role": "system", "content": "agent"}
    assert sent[1] == summary_message(summary)
    assert "les faits" in sent[1]["content"] and sent[1]["role"] == "system"
    assert sent[2:] == msgs[6:]
    assert info["used_summary"] is True and info["dropped"] == 0


def test_build_prompt_trims_oldest_first_and_reports_dropped():
    msgs = _msgs(10)  # ~105 tokens each
    sent, info = build_prompt([], None, msgs, 320, 3.0)
    # The dropped span is announced, then the surviving tail follows verbatim.
    assert sent[0]["role"] == "system" and "7 message(s)" in sent[0]["content"]
    assert sent[1:] == msgs[-3:]
    assert info["dropped"] == 7


def test_build_prompt_never_drops_the_newest_message():
    msgs = _msgs(2, size=30000)
    sent, info = build_prompt([], None, msgs, 100, 3.0)
    assert sent[-1] == msgs[-1]
    assert sent[0]["role"] == "system" and "1 message(s)" in sent[0]["content"]
    assert info["dropped"] == 1


def test_build_prompt_evicts_old_tool_outputs():
    msgs = ([{"role": "tool", "content": "z" * 500}] +
            _msgs(chat_context.EVICT_KEEP))
    sent, info = build_prompt([], None, msgs, 10000, 3.0)
    assert sent[0]["content"] == chat_context.EVICTED_STUB
    assert info["evicted"] == 1


# ---- what the model is told about its own elided history (2026-07-27).
# Sessions show the model reading its trimmed context as the USER withholding
# information, then re-reading the same file 29 times to rebuild it.

def test_the_eviction_stub_names_the_harness_and_the_recovery():
    stub = chat_context.EVICTED_STUB
    assert "harnais" in stub                      # who elided it
    assert "utilisateur" in stub                  # and who did not
    assert "relance l'outil" in stub              # what to do about it


def test_dropped_messages_are_announced_to_the_model():
    r = 3.0
    msgs = [{"role": "user", "content": "x" * 3000} for _ in range(12)]
    sent, info = chat_context.build_prompt([], None, msgs, budget=400, r=r)
    assert info["dropped"] > 0
    note = [m for m in sent if m["role"] == "system"
            and "message" in m.get("content", "")]
    assert note, "a dropped span must leave a visible trace in the prompt"
    assert str(info["dropped"]) in note[0]["content"]


def test_nothing_is_announced_when_nothing_was_dropped():
    sent, info = chat_context.build_prompt(
        [], None, [{"role": "user", "content": "hi"}], budget=10000, r=3.0)
    assert info["dropped"] == 0
    assert not [m for m in sent if m["role"] == "system"]

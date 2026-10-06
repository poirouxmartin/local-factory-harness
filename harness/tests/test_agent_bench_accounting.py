"""What a turn costs, line item by line item.

The rule the module exists to enforce: never estimate. Counts come from the
server's tokenizer (injected here as a fake), and the difference between the
blocks and the wire is REPORTED as overhead, never absorbed -- the chat
template's role wrappers are real tokens somebody has to own.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments"))

from agent_bench import accounting


def fake_tokenize(text):
    """One token per whitespace-separated word: deterministic, no server."""
    return len(text.split())


def test_segments_lay_out_the_prompt_in_wire_order():
    parts = [("system", "a b"), ("agent_md", "c")]
    tools = [{"function": {"name": "read_file"}}]
    messages = [{"role": "user", "content": "salut"}]
    names = [n for n, _ in accounting.segments(parts, tools, None, messages)]
    assert names == ["system", "agent_md", "tools", "messages"]


def test_segments_omit_the_summary_when_there_is_none():
    names = [n for n, _ in accounting.segments([("system", "a")], [], None, [])]
    assert "summary" not in names


def test_segments_place_the_summary_between_the_system_and_the_messages():
    parts = [("system", "a")]
    summary = {"content": "on parlait de X", "covers_until": 4}
    names = [n for n, _ in accounting.segments(parts, [], summary, [])]
    assert names == ["system", "tools", "summary"]


def test_segments_count_tool_call_arguments_in_the_messages():
    """An agent write_file carries the whole file in its arguments. Counting
    the message as its `content` alone is how a session filled 65536 tokens
    without anything noticing (2026-07-14)."""
    messages = [{"role": "assistant", "content": "",
                 "tool_calls": [{"function": {"name": "write_file",
                                              "arguments": {"text": "un deux trois"}}}]}]
    text = dict(accounting.segments([], [], None, messages))["messages"]
    assert "un deux trois" in text


def test_count_prices_every_segment_with_the_injected_tokenizer():
    segs = [("system", "un deux trois"), ("messages", "quatre")]
    assert accounting.count(segs, fake_tokenize) == [("system", 3),
                                                     ("messages", 1)]


def test_reconcile_reports_the_gap_between_the_blocks_and_the_wire():
    out = accounting.reconcile([("system", 90), ("messages", 10)], 120)
    assert out == {"blocks": 100, "wire": 120, "overhead": 20,
                   "overhead_pct": 16}


def test_reconcile_survives_a_turn_with_no_wire_counter():
    """A stopped turn yields no timings; the report must degrade, not crash."""
    out = accounting.reconcile([("system", 90)], 0)
    assert out["wire"] == 0 and out["overhead_pct"] == 0


def test_last_call_messages_drops_the_answer_written_after_the_prompt():
    """The bench prices the session once the turn is over, but the final
    answer was appended AFTER the last model call built its prompt. Charging
    it makes the blocks exceed the wire, i.e. a negative template overhead --
    which the plan says to read as a bug, not a finding."""
    messages = [{"role": "user", "content": "goal"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"function": {"name": "read_file"}}]},
                {"role": "tool", "content": "file body"},
                {"role": "assistant", "content": "voila, c'est fait"}]
    kept = accounting.last_call_messages(messages)
    assert [m["role"] for m in kept] == ["user", "assistant", "tool"]


def test_last_call_messages_keeps_an_assistant_that_still_holds_a_tool_call():
    """A parked or capped turn ends on a call the model made: that message WAS
    in the last prompt's history."""
    messages = [{"role": "user", "content": "goal"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"function": {"name": "run_command"}}]}]
    assert accounting.last_call_messages(messages) == messages


def test_cache_report_splits_what_was_repaid_from_what_was_reused():
    metrics = {"prompt_count": 3200, "prefill_count": 17}
    assert accounting.cache_report(metrics) == {
        "wire": 3200, "repaid": 17, "reused": 3183, "hit_pct": 99}


def test_cache_report_of_a_first_turn_is_all_repaid():
    metrics = {"prompt_count": 1103, "prefill_count": 1103}
    assert accounting.cache_report(metrics)["hit_pct"] == 0

"""Scoring a turn's events. The failure this guards: pass 1 of the 07-23 A/B
counted only outputs starting with "error:", so every failed run_command --
a wasted round-trip by any measure -- read as zero."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments"
                       / "agent_bench"))

import bench


def words(text):
    return len(text.split())


def test_a_failed_shell_command_counts_as_a_wasted_round_trip():
    events = [("tool_result", {"output": "exit 1: No module named pytest"})]
    assert bench.score_events(events)["tool_errors"] == 1


def test_thinking_and_answer_tokens_are_split():
    events = [("thinking", "je reflechis un peu"), ("chunk", "voila")]
    out = bench.score_events(events, tokenize=words)
    assert out["think_tokens"] == 4
    assert out["write_tokens"] == 1


def test_the_arguments_the_model_wrote_count_as_generation():
    """`eval_count` on the final `done` is the LAST model call's alone -- the
    agent loop yields `done` once per turn, not once per call (factory_mcp.py
    :1225). Measured 07-23: eval_tokens=41 against 228 written tokens. What
    the model generated in between is its tool calls, arguments included."""
    events = [("chunk", "voila"),
              ("tool_call", {"name": "write_file",
                             "arguments": {"text": "un deux trois"}})]
    out = bench.score_events(events, tokenize=words)
    assert out["call_tokens"] == words(
        '[{"name": "write_file", "arguments": {"text": "un deux trois"}}]')
    assert out["gen_tokens"] == out["think_tokens"] + out["write_tokens"] \
        + out["call_tokens"]


def test_compactions_are_counted_from_the_turn_events():
    """Line items 4/5 hinge on a session actually crossing the threshold; a
    zero here is the honest verdict that the corpus was too small."""
    events = [("compacting", True), ("tool_call", {"name": "read_file"}),
              ("compacting", True), ("done", {})]
    assert bench.score_events(events)["compactions"] == 2
    assert bench.score_events([("done", {})])["compactions"] == 0


def test_scoring_without_a_tokenizer_omits_the_split():
    out = bench.score_events([("chunk", "voila")])
    assert "think_tokens" not in out

"""The session event trace: what happened, what it cost, what went wrong."""
import json

import session_log


def _log(tmp_path):
    return session_log.SessionLog(tmp_path / "c_x.events.jsonl")


def test_events_are_appended_one_json_object_per_line(tmp_path):
    log = _log(tmp_path)
    log.turn_start(model="m", num_ctx=65536, prompt_est=1200)
    log.tool("read_file", {"path": "a.py"}, ms=12, out="x" * 30)
    lines = (tmp_path / "c_x.events.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["kind"] == "turn_start"
    assert json.loads(lines[1])["kind"] == "tool"


def test_a_tool_event_carries_its_cost(tmp_path):
    log = _log(tmp_path)
    log.tool("read_file", {"path": "a.py"}, ms=42, out="y" * 100)
    ev = session_log.read(tmp_path / "c_x.events.jsonl")[0]
    assert ev["name"] == "read_file"
    assert ev["ms"] == 42
    assert ev["out_chars"] == 100
    assert ev["error"] is False


def test_a_failed_tool_is_marked_and_its_kind_kept(tmp_path):
    log = _log(tmp_path)
    log.tool("search", {"pattern": "x"}, ms=3,
             out="error: search failed: MemoryError")
    ev = session_log.read(tmp_path / "c_x.events.jsonl")[0]
    assert ev["error"] is True
    assert "MemoryError" in ev["error_kind"]


def test_arguments_are_digested_not_stored_whole(tmp_path):
    # write_file carries an entire file in its arguments; the trace must not
    # become a second copy of the workspace.
    log = _log(tmp_path)
    log.tool("write_file", {"path": "big.py", "content": "z" * 100000},
             ms=5, out="wrote")
    raw = (tmp_path / "c_x.events.jsonl").read_text(encoding="utf-8")
    assert len(raw) < 1000
    assert "zzzz" not in raw


def test_a_replayed_call_raises_an_anomaly(tmp_path):
    log = _log(tmp_path)
    log.tool("read_file", {"path": "a.py"}, ms=1, out="x")
    log.tool("read_file", {"path": "a.py"}, ms=1, out="x")
    kinds = [e["what"] for e in session_log.read(
        tmp_path / "c_x.events.jsonl") if e["kind"] == "anomaly"]
    assert kinds == ["replayed_call"]


def test_a_different_argument_is_not_a_replay(tmp_path):
    log = _log(tmp_path)
    log.tool("read_file", {"path": "a.py"}, ms=1, out="x")
    log.tool("read_file", {"path": "b.py"}, ms=1, out="x")
    assert not [e for e in session_log.read(tmp_path / "c_x.events.jsonl")
                if e["kind"] == "anomaly"]


def test_turn_end_flags_a_context_overrun(tmp_path):
    log = _log(tmp_path)
    log.turn_end(num_ctx=1000, prompt_count=1200, eval_count=10,
                 done_reason="stop")
    events = session_log.read(tmp_path / "c_x.events.jsonl")
    assert [e["what"] for e in events if e["kind"] == "anomaly"] == \
        ["context_overrun"]


def test_turn_end_flags_a_truncated_generation(tmp_path):
    log = _log(tmp_path)
    log.turn_end(num_ctx=1000, prompt_count=10, eval_count=10,
                 done_reason="length")
    assert "output_truncated" in [e.get("what") for e in session_log.read(
        tmp_path / "c_x.events.jsonl")]


def test_turn_end_flags_dropped_and_evicted_context(tmp_path):
    log = _log(tmp_path)
    log.turn_end(num_ctx=1000, prompt_count=10, eval_count=10,
                 done_reason="stop", dropped=3, evicted=7, compacted=True)
    what = [e.get("what") for e in session_log.read(
        tmp_path / "c_x.events.jsonl")]
    assert "messages_dropped" in what and "compacted" in what


def test_a_clean_turn_raises_nothing(tmp_path):
    log = _log(tmp_path)
    log.turn_end(num_ctx=1000, prompt_count=10, eval_count=10,
                 done_reason="stop")
    assert not [e for e in session_log.read(tmp_path / "c_x.events.jsonl")
                if e["kind"] == "anomaly"]


def test_logging_never_raises_on_a_bad_path(tmp_path):
    log = session_log.SessionLog(tmp_path / "nope" / "deep" / "c.jsonl")
    log.tool("read_file", {"path": "a"}, ms=1, out="x")   # must not raise
    log.anomaly("something", "detail")


def test_read_survives_a_corrupt_line(tmp_path):
    p = tmp_path / "c_x.events.jsonl"
    p.write_text('{"kind": "tool"}\nnot json at all\n{"kind": "anomaly"}\n',
                 encoding="utf-8")
    assert [e["kind"] for e in session_log.read(p)] == ["tool", "anomaly"]


def test_summarize_totals_what_a_post_mortem_needs(tmp_path):
    log = _log(tmp_path)
    log.tool("read_file", {"path": "a.py"}, ms=100, out="x" * 10)
    log.tool("read_file", {"path": "a.py"}, ms=50, out="x" * 10)
    log.tool("search", {"pattern": "p"}, ms=900, out="error: boom")
    log.turn_end(num_ctx=1000, prompt_count=10, eval_count=5,
                 done_reason="stop")
    s = session_log.summarize(session_log.read(tmp_path / "c_x.events.jsonl"))
    assert s["calls"] == 3
    assert s["replayed"] == 1
    assert s["errors"] == 1
    assert s["tool_ms"]["read_file"] == 150
    assert s["tool_calls"]["read_file"] == 2
    assert s["anomalies"]["replayed_call"] == 1


def test_summarize_of_nothing_is_still_readable(tmp_path):
    s = session_log.summarize([])
    assert s["calls"] == 0 and s["replayed"] == 0 and s["anomalies"] == {}

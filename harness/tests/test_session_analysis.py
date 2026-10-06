"""What a session cost, and what the trace says to fix first."""
import session_analysis
import session_log


def _events(tmp_path, fill):
    log = session_log.SessionLog(tmp_path / "c_x.events.jsonl")
    fill(log)
    return session_log.read(tmp_path / "c_x.events.jsonl")


def _codes(report):
    return [f["code"] for f in report["findings"]]


def test_tool_time_and_generation_are_both_accounted(tmp_path):
    def fill(log):
        log.turn_start(model="m", num_ctx=65536, prompt_est=1000)
        log.tool("read_file", {"path": "a.py"}, ms=1500, out="x" * 300)
        log.tool("write_file", {"path": "b.py"}, ms=500, out="wrote")
        log.turn_end(num_ctx=65536, prompt_count=1200, eval_count=300,
                     done_reason="stop", tokens_per_s=30.0, ttft_s=0.5)
    report = session_analysis.analyze(_events(tmp_path, fill))
    assert report["turns"] == 1
    assert report["calls"] == 2
    assert report["cost"]["tool_s"] == 2.0
    assert report["cost"]["prompt_tokens"] == 1200
    assert report["cost"]["gen_tokens"] == 300
    # 300 tokens at 30 tok/s plus the 0.5 s of prefill wait
    assert report["cost"]["gen_s"] == 10.5


def test_a_clean_session_has_no_findings(tmp_path):
    def fill(log):
        log.tool("read_file", {"path": "a.py"}, ms=10, out="x" * 100)
        log.turn_end(num_ctx=65536, prompt_count=900, eval_count=50,
                     done_reason="stop", tokens_per_s=30.0)
    report = session_analysis.analyze(_events(tmp_path, fill))
    assert report["findings"] == []
    assert report["verdict"] == "clean"


def test_replayed_calls_are_priced_in_seconds_and_tokens(tmp_path):
    def fill(log):
        for _ in range(4):
            log.tool("read_file", {"path": "app.js"}, ms=1000, out="x" * 3000)
    report = session_analysis.analyze(_events(tmp_path, fill))
    waste = report["waste"]
    # Three of the four calls bought nothing: 3 s and ~3000 tokens.
    assert waste["replayed_calls"] == 3
    assert waste["replayed_s"] == 3.0
    assert waste["replayed_tokens"] == 3000
    found = [f for f in report["findings"] if f["code"] == "replayed_calls"][0]
    assert found["cost_s"] == 3.0
    assert found["cost_tokens"] == 3000
    assert "3" in found["detail"]


def test_the_repeated_target_is_named_not_just_counted(tmp_path):
    # c_d9e12d6a read harness/web/app.js 29 times. "wasted_calls: 257" never
    # said which file, so nothing could be fixed from it.
    def fill(log):
        for _ in range(5):
            log.tool("read_file", {"path": "harness/web/app.js"},
                     ms=100, out="x" * 1000)
        log.tool("list_dir", {"path": "harness"}, ms=10, out="y")
    report = session_analysis.analyze(_events(tmp_path, fill))
    hot = [f for f in report["findings"] if f["code"] == "hot_target"][0]
    assert "harness/web/app.js" in hot["detail"]
    assert hot["count"] == 5


def test_a_single_oversized_output_is_a_finding_of_its_own(tmp_path):
    def fill(log):
        log.tool("read_file", {"path": "big.js"}, ms=20, out="x" * 60000)
    report = session_analysis.analyze(_events(tmp_path, fill))
    big = [f for f in report["findings"] if f["code"] == "oversized_output"][0]
    assert "big.js" in big["detail"]
    assert big["cost_tokens"] == 20000
    assert big["cost_s"] == 0.0


def test_a_tool_that_eats_the_wall_clock_is_named(tmp_path):
    def fill(log):
        log.tool("search", {"pattern": "def "}, ms=12000, out="hit")
        log.tool("read_file", {"path": "a.py"}, ms=100, out="x")
    report = session_analysis.analyze(_events(tmp_path, fill))
    slow = [f for f in report["findings"] if f["code"] == "slow_tool"][0]
    assert "search" in slow["detail"]
    assert slow["cost_s"] == 12.0


def test_a_tool_failing_the_same_way_is_named_with_its_reason(tmp_path):
    def fill(log):
        for _ in range(3):
            log.tool("search", {"pattern": "x"}, ms=300,
                     out="error: search failed: MemoryError")
    report = session_analysis.analyze(_events(tmp_path, fill))
    bad = [f for f in report["findings"] if f["code"] == "failing_tool"][0]
    assert "search" in bad["detail"] and "MemoryError" in bad["detail"]
    assert bad["count"] == 3


def test_two_failures_do_not_reach_the_floor(tmp_path):
    def fill(log):
        for _ in range(2):
            log.tool("search", {"pattern": "x"}, ms=10, out="error: boom")
    report = session_analysis.analyze(_events(tmp_path, fill))
    assert "failing_tool" not in _codes(report)


def test_a_compaction_is_priced_at_the_prefill_it_repays(tmp_path):
    def fill(log):
        log.turn_end(num_ctx=65536, prompt_count=48000, eval_count=100,
                     done_reason="stop", compacted=True)
    report = session_analysis.analyze(_events(tmp_path, fill))
    pressure = [f for f in report["findings"]
                if f["code"] == "window_pressure"][0]
    assert pressure["cost_tokens"] == session_analysis.COMPACTION_REPAY_TOKENS
    assert report["window"]["compactions"] == 1


def test_a_full_window_is_flagged_without_a_compaction(tmp_path):
    def fill(log):
        log.turn_end(num_ctx=1000, prompt_count=900, eval_count=50,
                     done_reason="stop")
    report = session_analysis.analyze(_events(tmp_path, fill))
    assert "window_pressure" in _codes(report)
    assert report["window"]["peak_pct"] == 95


def test_a_truncated_turn_is_priced_at_the_generation_it_threw_away(tmp_path):
    # On done_reason=length the pending tool call is dropped
    # (factory_mcp:1404), so every token of that turn bought nothing.
    def fill(log):
        log.turn_end(num_ctx=65536, prompt_count=2000, eval_count=8192,
                     done_reason="length", tokens_per_s=40.0)
    report = session_analysis.analyze(_events(tmp_path, fill))
    cut = [f for f in report["findings"]
           if f["code"] == "output_truncated"][0]
    assert cut["cost_tokens"] == 8192
    assert report["window"]["truncations"] == 1


def test_findings_are_ordered_by_what_they_cost(tmp_path):
    def fill(log):
        # 20k chars of window (~6.7k tok, ~8 s of prefill) beats 2 s of tool
        # time, and must be read first.
        log.tool("read_file", {"path": "big.js"}, ms=5, out="x" * 60000)
        log.tool("search", {"pattern": "y"}, ms=2000, out="hit")
    report = session_analysis.analyze(_events(tmp_path, fill))
    assert _codes(report)[0] == "oversized_output"


def test_every_finding_code_belongs_to_the_closed_vocabulary(tmp_path):
    def fill(log):
        for _ in range(4):
            log.tool("read_file", {"path": "a.js"}, ms=2000, out="x" * 90000)
        log.tool("search", {"pattern": "x"}, ms=10, out="error: boom")
        log.anomaly("map_degraded", "fell back to directories")
        log.turn_end(num_ctx=1000, prompt_count=990, eval_count=10,
                     done_reason="length", compacted=True)
    report = session_analysis.analyze(_events(tmp_path, fill))
    assert _codes(report)
    for code in _codes(report):
        assert code in session_analysis.FINDINGS


def test_a_harness_anomaly_in_the_trace_reaches_the_findings(tmp_path):
    def fill(log):
        log.anomaly("toolset_over_budget", "15 tools exposed")
    report = session_analysis.analyze(_events(tmp_path, fill))
    assert "toolset_over_budget" in _codes(report)


def test_wall_clock_comes_from_the_event_timestamps(tmp_path):
    events = [{"kind": "turn_start", "t": 100.0, "model": "m", "num_ctx": 100},
              {"kind": "tool", "t": 130.5, "name": "read_file", "ms": 10,
               "out_chars": 5}]
    report = session_analysis.analyze(events)
    assert report["cost"]["wall_s"] == 30.5


def test_analysis_of_nothing_is_still_readable(tmp_path):
    report = session_analysis.analyze([])
    assert report["verdict"] == "clean"
    assert report["calls"] == 0
    assert report["cost"]["wall_s"] == 0.0
    assert session_analysis.to_markdown(report)


def test_a_corrupt_trace_file_yields_an_empty_analysis(tmp_path):
    p = tmp_path / "c_x.events.jsonl"
    p.write_text("not json\n", encoding="utf-8")
    assert session_analysis.analyze_file(p)["calls"] == 0


def test_the_markdown_report_names_the_costs(tmp_path):
    def fill(log):
        for _ in range(4):
            log.tool("read_file", {"path": "app.js"}, ms=1000, out="x" * 3000)
    report = session_analysis.analyze(_events(tmp_path, fill))
    md = session_analysis.to_markdown(report)
    assert "replayed_calls" in md
    assert "app.js" in md

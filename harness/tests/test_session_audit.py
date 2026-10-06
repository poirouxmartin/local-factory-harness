# Spec: ROADMAP "session-audit job" v1 — harness/session_audit.py replays a
# jobs/chats/*.json session dict and produces an audit report: message counts,
# duration, tool-call stats, wasted (duplicate) calls, thinking loops, error
# count, speed averages, flags and a verdict; plus a markdown rendering and a
# file entry point.
import json

import session_audit


def make_flagged_session():
    # 1 thinking loop, 1 duplicate tool call, 1 error, mixed str/float ts.
    looping = "\n".join(["I should check the file again."] * 11 + ["act now"])
    return {
        "session_id": "c_test1234",
        "title": "demo",
        "model": "coder",
        "messages": [
            {"role": "user", "content": "hi", "ts": 100.0},
            {"role": "assistant", "content": "look", "ts": "101.5",
             "thinking": looping,
             "metrics": {"tokens_per_s": 50.0, "ttft_s": 2.0},
             "tool_calls": [{"function": {"name": "list_dir",
                                          "arguments": {"path": "."}}}]},
            {"role": "tool", "tool_name": "list_dir", "content": "a b",
             "ts": 102.0},
            {"role": "assistant", "content": "again", "ts": 103.0,
             "metrics": {"tokens_per_s": 70.0, "ttft_s": 4.0},
             "tool_calls": [{"function": {"name": "list_dir",
                                          "arguments": {"path": "."}}}]},
            {"role": "tool", "tool_name": "list_dir", "content": "a b",
             "ts": 104.0, "error": "boom"},
            {"role": "assistant", "content": "done", "ts": "110.0"},
        ],
    }


def make_clean_session():
    return {
        "session_id": "c_clean007",
        "title": "clean",
        "model": "coder",
        "messages": [
            {"role": "user", "content": "hello", "ts": 10.0},
            {"role": "assistant", "content": "hi",
             "thinking": "greet the user", "ts": 11.0,
             "metrics": {"tokens_per_s": 80.0, "ttft_s": 1.0}},
        ],
    }


def test_counts_per_role():
    report = session_audit.analyze(make_flagged_session())
    assert report["counts"] == {"user": 1, "assistant": 3, "tool": 2}


def test_duration_coerces_string_timestamps():
    report = session_audit.analyze(make_flagged_session())
    assert report["duration_s"] == 10.0


def test_tool_call_totals_and_per_tool():
    report = session_audit.analyze(make_flagged_session())
    assert report["tool_calls"] == 2
    assert report["per_tool"] == {"list_dir": 2}


def test_wasted_calls_counts_duplicates_beyond_first():
    report = session_audit.analyze(make_flagged_session())
    assert report["wasted_calls"] == 1


def test_errors_counts_messages_with_error_key():
    report = session_audit.analyze(make_flagged_session())
    assert report["errors"] == 1


def test_tool_errors_counts_failed_tool_results():
    # `errors` counts messages carrying an error field (a stream that broke).
    # A tool that refused is a different failure and reads only in the text:
    # agent_tools returns "error: ..." as the tool output.
    session = {"session_id": "c_1", "messages": [
        {"role": "tool", "tool_name": "read_file", "content": "error: no such file"},
        {"role": "tool", "tool_name": "run_command", "content": "exit 0\nok"},
        {"role": "tool", "tool_name": "edit_file", "content": "error: 0 matches"},
    ]}
    assert session_audit.analyze(session)["tool_errors"] == 2


def test_loops_detects_degenerate_thinking():
    # >= 10 non-empty lines and unique/total <= 0.35 means one loop.
    report = session_audit.analyze(make_flagged_session())
    assert report["loops"] == 1


def test_short_or_varied_thinking_is_not_a_loop():
    report = session_audit.analyze(make_clean_session())
    assert report["loops"] == 0


def test_speed_averages_rounded_one_decimal():
    report = session_audit.analyze(make_flagged_session())
    assert report["speed"] == {"mean_tokens_per_s": 60.0, "mean_ttft_s": 3.0}


def test_flags_and_verdict_flagged():
    report = session_audit.analyze(make_flagged_session())
    assert report["flags"] == ["errors", "loops", "wasted_calls"]
    assert report["verdict"] == "flagged"


def test_clean_session_verdict():
    report = session_audit.analyze(make_clean_session())
    assert report["flags"] == []
    assert report["verdict"] == "clean"
    assert report["wasted_calls"] == 0
    assert report["errors"] == 0


def test_empty_session_does_not_crash():
    report = session_audit.analyze(
        {"session_id": "c_empty", "title": "", "model": "coder",
         "messages": []})
    assert report["duration_s"] == 0.0
    assert report["verdict"] == "clean"
    assert report["speed"] == {"mean_tokens_per_s": 0.0, "mean_ttft_s": 0.0}


def test_report_carries_identity():
    report = session_audit.analyze(make_flagged_session())
    assert report["session_id"] == "c_test1234"
    assert report["model"] == "coder"


def test_to_markdown_contains_key_facts():
    report = session_audit.analyze(make_flagged_session())
    md = session_audit.to_markdown(report)
    assert "# Session audit — c_test1234" in md
    assert "flagged" in md
    assert "list_dir" in md


def test_audit_file_writes_report(tmp_path):
    src = tmp_path / "c_test1234.json"
    src.write_text(json.dumps(make_flagged_session()), encoding="utf-8")
    out_dir = tmp_path / "audits"
    out = session_audit.audit_file(src, out_dir)
    assert out == out_dir / "c_test1234-audit.md"
    text = out.read_text(encoding="utf-8")
    assert "# Session audit — c_test1234" in text

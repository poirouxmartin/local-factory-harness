"""What a session cost, and what to change because of it.

`session_audit` reads the transcript, which holds no duration, no window figure
and no output size -- so it can count and nothing else. Its report on a session
that spent half its wall clock re-reading a 56 KB file reads exactly like its
report on a clean one, which is why the 2026-07-27 audit (854 replayed calls
over 256 sessions) had to be done by hand.

This reads the event trace (`session_log`), written for exactly that: `ms` and
`out_chars` per call, window figures per turn, a closed anomaly vocabulary.

Pure arithmetic over the event list, no state, no generation. Findings are a
closed vocabulary with floors: an analysis that can name anything names
nothing, and the lesson patterns downstream have to stay countable across
sessions.

Design: docs/design/specs/2026-07-27-session-self-analysis-design.md
"""
import session_log

# `chat_context.DEFAULT_RATIO`, kept local so a report never depends on the
# chat lane's configuration.
CHARS_PER_TOKEN = 3.0

# Time and window are both scarce, and a finding that costs no seconds but 20k
# tokens is not free: those tokens are prefilled again after every compaction.
# Prefill measures 600-1000 tok/s on this box (registry, 18/07), so 1000 tokens
# is priced at ~1 second. Only the RANKING uses this; both raw numbers stay.
PREFILL_TOK_PER_S = 800.0

# A compaction repaid ~13.6k tokens of prefill on both arms of the huge_chain
# A/B (experiments/results/20260723_compaction_cache.md). Measured on that
# bench, not universal -- but far closer to the truth than pricing it at zero.
COMPACTION_REPAY_TOKENS = 13600

REPLAY_MIN = 3
REPLAY_S_MIN = 5.0
HOT_TARGET_MIN = 4
OVERSIZED_CHARS = 20000        # ~6.7k tokens, a tenth of the chat window
SLOW_TOOL_S = 5.0
SLOW_TOOL_SHARE = 0.4
FAILING_TOOL_MIN = 3
WINDOW_PCT = 85

# Findings the trace reports as-is: the harness said it, and the wording is
# already the actionable one.
PASSTHROUGH = ("toolset_over_budget", "map_degraded")

FINDINGS = ("replayed_calls", "hot_target", "oversized_output", "slow_tool",
            "failing_tool", "window_pressure", "output_truncated") + PASSTHROUGH

# What a model can act on. The rest are harness facts: telling a model its
# project map degraded teaches it nothing.
MODEL_FINDINGS = ("replayed_calls", "hot_target", "oversized_output",
                  "failing_tool", "output_truncated")

MAX_FINDINGS = 8


def tokens(chars):
    """Window cost of a tool output, in tokens."""
    return int((chars or 0) / CHARS_PER_TOKEN)


def _weight(cost_s, cost_tokens):
    """Both costs in one unit, so findings can be ranked against each other."""
    return round((cost_s or 0.0) + (cost_tokens or 0) / PREFILL_TOK_PER_S, 1)


def _finding(code, detail, count=1, cost_s=0.0, cost_tokens=0):
    return {"code": code, "detail": detail, "count": count,
            "cost_s": round(cost_s, 1), "cost_tokens": int(cost_tokens),
            "weight_s": _weight(cost_s, cost_tokens)}


def _target(entry):
    """A call named the way a human would name it: tool plus what it hit."""
    return "{} {}".format(entry["name"], entry["hint"]).strip()


def _walk(events):
    """Everything the findings need, in one pass over the trace."""
    s = {"calls": 0, "errors": 0, "turns": 0, "models": [], "num_ctx": 0,
         "tools": {}, "hot": {}, "failures": {}, "anomalies": {},
         "anomaly_detail": {}, "biggest": None, "times": [],
         "replayed_calls": 0, "replayed_ms": 0, "replayed_chars": 0,
         "prompt_tokens": 0, "gen_tokens": 0, "gen_s": 0.0, "ttft_s": 0.0,
         "tool_ms": 0, "peak_pct": 0, "compactions": 0, "dropped": 0,
         "truncations": 0, "truncated_tokens": 0}
    for e in events:
        if e.get("t") is not None:
            try:
                s["times"].append(float(e["t"]))
            except (TypeError, ValueError):
                pass
        kind = e.get("kind")
        if kind == "tool":
            _walk_tool(s, e)
        elif kind == "turn_end":
            _walk_turn_end(s, e)
        elif kind == "turn_start":
            model = e.get("model")
            if model and model not in s["models"]:
                s["models"].append(model)
            s["num_ctx"] = max(s["num_ctx"], int(e.get("num_ctx") or 0))
        elif kind == "anomaly":
            what = e.get("what") or "?"
            s["anomalies"][what] = s["anomalies"].get(what, 0) + 1
            s["anomaly_detail"][what] = str(e.get("detail") or "")
    return s


def _walk_tool(s, e):
    name = e.get("name") or "?"
    ms = int(e.get("ms") or 0)
    chars = int(e.get("out_chars") or 0)
    s["calls"] += 1
    s["tool_ms"] += ms
    per = s["tools"].setdefault(name, {"calls": 0, "ms": 0, "chars": 0,
                                       "errors": 0})
    per["calls"] += 1
    per["ms"] += ms
    per["chars"] += chars
    if e.get("error"):
        s["errors"] += 1
        per["errors"] += 1
        key = (name, (e.get("error_kind") or "error"))
        fail = s["failures"].setdefault(key, {"count": 0, "ms": 0})
        fail["count"] += 1
        fail["ms"] += ms
    if s["biggest"] is None or chars > s["biggest"]["chars"]:
        s["biggest"] = {"chars": chars, "name": name,
                        "hint": e.get("args_hint") or ""}
    key = (name, e.get("args_digest"))
    hot = s["hot"].setdefault(key, {"name": name,
                                    "hint": e.get("args_hint") or "",
                                    "count": 0, "ms": 0, "chars": 0})
    hot["count"] += 1
    if hot["count"] > 1:
        # Only the repeats are waste; the first call did the work.
        s["replayed_calls"] += 1
        s["replayed_ms"] += ms
        s["replayed_chars"] += chars
        hot["ms"] += ms
        hot["chars"] += chars


def _walk_turn_end(s, e):
    s["turns"] += 1
    prompt = int(e.get("prompt_count") or 0)
    gen = int(e.get("eval_count") or 0)
    num_ctx = int(e.get("num_ctx") or 0)
    s["num_ctx"] = max(s["num_ctx"], num_ctx)
    s["prompt_tokens"] += prompt
    s["gen_tokens"] += gen
    tps = float(e.get("tokens_per_s") or 0.0)
    ttft = float(e.get("ttft_s") or 0.0)
    s["ttft_s"] += ttft
    s["gen_s"] += (gen / tps if tps else 0.0) + ttft
    pct = e.get("ctx_pct")
    if pct is None and num_ctx:
        pct = int(100 * (prompt + gen) / num_ctx)
    s["peak_pct"] = max(s["peak_pct"], int(pct or 0))
    s["dropped"] += int(e.get("dropped") or 0)
    if e.get("compacted"):
        s["compactions"] += 1
    if e.get("done_reason") == "length":
        s["truncations"] += 1
        # The pending tool call is dropped on a length stop
        # (factory_mcp:1404), so the whole turn's generation bought nothing.
        s["truncated_tokens"] += gen


def _findings(s):
    out = []
    if (s["replayed_calls"] >= REPLAY_MIN
            or s["replayed_ms"] / 1000.0 >= REPLAY_S_MIN):
        out.append(_finding(
            "replayed_calls",
            "{} identical calls replayed".format(s["replayed_calls"]),
            count=s["replayed_calls"], cost_s=s["replayed_ms"] / 1000.0,
            cost_tokens=tokens(s["replayed_chars"])))

    hot = max(s["hot"].values(), key=lambda h: h["count"]) if s["hot"] else None
    if hot and hot["count"] >= HOT_TARGET_MIN:
        out.append(_finding(
            "hot_target",
            "{} x{}".format(_target(hot), hot["count"]),
            count=hot["count"], cost_s=hot["ms"] / 1000.0,
            cost_tokens=tokens(hot["chars"])))

    big = s["biggest"]
    if big and big["chars"] >= OVERSIZED_CHARS:
        out.append(_finding(
            "oversized_output",
            "{} returned ~{} tokens in one call".format(
                _target(big), tokens(big["chars"])),
            cost_tokens=tokens(big["chars"])))

    for name, per in sorted(s["tools"].items()):
        share = per["ms"] / s["tool_ms"] if s["tool_ms"] else 0.0
        if per["ms"] / 1000.0 >= SLOW_TOOL_S and share >= SLOW_TOOL_SHARE:
            out.append(_finding(
                "slow_tool",
                "{} took {:.1f} s = {} % of tool time".format(
                    name, per["ms"] / 1000.0, int(100 * share)),
                count=per["calls"], cost_s=per["ms"] / 1000.0))

    for (name, kind), fail in sorted(s["failures"].items()):
        if fail["count"] >= FAILING_TOOL_MIN:
            out.append(_finding(
                "failing_tool",
                "{} failed {}x: {}".format(name, fail["count"], kind),
                count=fail["count"], cost_s=fail["ms"] / 1000.0))

    if s["peak_pct"] >= WINDOW_PCT or s["compactions"]:
        out.append(_finding(
            "window_pressure",
            "window peaked at {} %, {} compaction(s), {} message(s) "
            "dropped".format(s["peak_pct"], s["compactions"], s["dropped"]),
            count=s["compactions"],
            cost_tokens=s["compactions"] * COMPACTION_REPAY_TOKENS))

    if s["truncations"]:
        out.append(_finding(
            "output_truncated",
            "{} turn(s) stopped on the generation cap".format(
                s["truncations"]),
            count=s["truncations"], cost_tokens=s["truncated_tokens"]))

    for what in PASSTHROUGH:
        n = s["anomalies"].get(what, 0)
        if n:
            out.append(_finding(
                what, "{}x: {}".format(n, s["anomaly_detail"].get(what, "")),
                count=n))

    # Costliest first: the top line is the thing worth fixing. Code breaks ties
    # so the same trace always renders the same bytes.
    out.sort(key=lambda f: (-f["weight_s"], f["code"]))
    return out[:MAX_FINDINGS]


def analyze(events):
    """What this session cost and what to fix, from its event trace."""
    s = _walk(events)
    wall = (max(s["times"]) - min(s["times"])) if len(s["times"]) >= 2 else 0.0
    found = _findings(s)
    tool_s = s["tool_ms"] / 1000.0
    return {
        "turns": s["turns"],
        "calls": s["calls"],
        "errors": s["errors"],
        "models": s["models"],
        "cost": {
            "wall_s": round(wall, 1),
            "tool_s": round(tool_s, 1),
            "tool_share": round(tool_s / wall, 2) if wall else 0.0,
            "gen_s": round(s["gen_s"], 1),
            "ttft_s": round(s["ttft_s"], 1),
            "prompt_tokens": s["prompt_tokens"],
            "gen_tokens": s["gen_tokens"],
            "out_tokens": tokens(sum(t["chars"] for t in s["tools"].values())),
        },
        "window": {
            "num_ctx": s["num_ctx"],
            "peak_pct": s["peak_pct"],
            "compactions": s["compactions"],
            "dropped": s["dropped"],
            "truncations": s["truncations"],
        },
        "waste": {
            "replayed_calls": s["replayed_calls"],
            "replayed_s": round(s["replayed_ms"] / 1000.0, 1),
            "replayed_tokens": tokens(s["replayed_chars"]),
            "error_calls": s["errors"],
            "weight_s": round(sum(f["weight_s"] for f in found), 1),
        },
        "tools": s["tools"],
        "anomalies": s["anomalies"],
        "findings": found,
        "verdict": "flagged" if found else "clean",
    }


def analyze_file(path):
    """The analysis of a trace file. A missing or corrupt file reads as an
    empty session, never as a failure: post-mortems run over whatever is
    there."""
    return analyze(session_log.read(path))


def to_markdown(report):
    """The post-mortem a human reads, costliest finding first."""
    cost, window, waste = report["cost"], report["window"], report["waste"]
    lines = ["# Session analysis", ""]
    lines.append("**Verdict:** {} - {} finding(s), {} s of measured "
                 "waste".format(report["verdict"], len(report["findings"]),
                                waste["weight_s"]))
    lines.append("")
    lines.append("## Findings")
    if report["findings"]:
        for f in report["findings"]:
            lines.append("- **{}** ({} s / ~{} tok) - {}".format(
                f["code"], f["cost_s"], f["cost_tokens"], f["detail"]))
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Cost")
    lines.append("- wall {} s: tools {} s ({} % of it), generation {} s".format(
        cost["wall_s"], cost["tool_s"], int(100 * cost["tool_share"]),
        cost["gen_s"]))
    lines.append("- {} turns, {} calls, {} errors".format(
        report["turns"], report["calls"], report["errors"]))
    # Prompt tokens are what was SENT, not what was computed: the static head
    # is prefilled once per server (95-98 % KV reuse measured 2026-07-23), so
    # this figure is the window bill, not a wall-clock cost.
    lines.append("- {} prompt tok sent (mostly KV-reused), {} generated, "
                 "~{} tok of tool output".format(
                     cost["prompt_tokens"], cost["gen_tokens"],
                     cost["out_tokens"]))
    lines.append("- window peak {} % of {}, {} compaction(s), {} "
                 "truncation(s)".format(window["peak_pct"], window["num_ctx"],
                                        window["compactions"],
                                        window["truncations"]))
    lines.append("")
    lines.append("## Tools")
    for name, per in sorted(report["tools"].items(),
                            key=lambda kv: -kv[1]["ms"]):
        lines.append("- {}: {} calls, {:.1f} s, ~{} tok returned, {} "
                     "error(s)".format(name, per["calls"], per["ms"] / 1000.0,
                                       tokens(per["chars"]), per["errors"]))
    return "\n".join(lines) + "\n"

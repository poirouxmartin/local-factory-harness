import json
from pathlib import Path
from typing import Any, Dict, List, Union


def analyze(session: Dict[str, Any]) -> Dict[str, Any]:
    session_id = session.get("session_id", "unknown")
    model = session.get("model", "unknown")
    messages = session.get("messages", [])

    counts: Dict[str, int] = {}
    for msg in messages:
        role = msg.get("role", "unknown")
        counts[role] = counts.get(role, 0) + 1

    ts_values: List[float] = []
    for msg in messages:
        ts = msg.get("ts")
        if ts is not None:
            try:
                ts_values.append(float(ts))
            except (ValueError, TypeError):
                pass

    duration_s = 0.0
    if len(ts_values) >= 2:
        duration_s = max(ts_values) - min(ts_values)

    assistant_tool_calls_list: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            tc = msg.get("tool_calls", [])
            if tc:
                assistant_tool_calls_list.extend(tc)

    total_tool_calls = len(assistant_tool_calls_list)

    per_tool: Dict[str, int] = {}
    seen_calls: set = set()
    wasted_calls = 0

    for tc in assistant_tool_calls_list:
        func = tc.get("function", {})
        name = func.get("name", "unknown")
        args = func.get("arguments", "")

        if isinstance(args, (dict, list)):
            args_key = str(args)
        else:
            args_key = args

        call_key = (name, args_key)

        per_tool[name] = per_tool.get(name, 0) + 1

        if call_key in seen_calls:
            wasted_calls += 1
        else:
            seen_calls.add(call_key)

    errors = len([msg for msg in messages if "error" in msg])

    # A refused or failed tool reads only in the text: agent_tools returns
    # "error: ..." as the tool output, with no error field anywhere.
    tool_errors = len([msg for msg in messages
                       if msg.get("role") == "tool"
                       and str(msg.get("content", "")).lstrip().lower()
                       .startswith("error:")])

    loops = 0
    for msg in messages:
        if msg.get("role") == "assistant":
            thinking = msg.get("thinking")
            if thinking and isinstance(thinking, str):
                lines = [line.strip() for line in thinking.splitlines()]
                non_empty = [line for line in lines if line]
                total_non_empty = len(non_empty)
                if total_non_empty >= 10:
                    unique_count = len(set(non_empty))
                    if unique_count / total_non_empty <= 0.35:
                        loops += 1

    speed_tokens: List[float] = []
    speed_ttft: List[float] = []
    for msg in messages:
        metrics = msg.get("metrics")
        if isinstance(metrics, dict):
            if "tokens_per_s" in metrics:
                try:
                    speed_tokens.append(float(metrics["tokens_per_s"]))
                except (ValueError, TypeError):
                    pass
            if "ttft_s" in metrics:
                try:
                    speed_ttft.append(float(metrics["ttft_s"]))
                except (ValueError, TypeError):
                    pass

    mean_tokens = round(sum(speed_tokens) / len(speed_tokens), 1) if speed_tokens else 0.0
    mean_ttft = round(sum(speed_ttft) / len(speed_ttft), 1) if speed_ttft else 0.0

    flags: List[str] = []
    if errors:
        flags.append("errors")
    if loops:
        flags.append("loops")
    if wasted_calls > 0:
        flags.append("wasted_calls")
    flags.sort()

    verdict = "clean" if not flags else "flagged"

    return {
        "session_id": session_id,
        "model": model,
        "counts": counts,
        "duration_s": duration_s,
        "tool_calls": total_tool_calls,
        "per_tool": per_tool,
        "wasted_calls": wasted_calls,
        "errors": errors,
        "tool_errors": tool_errors,
        "loops": loops,
        "speed": {
            "mean_tokens_per_s": mean_tokens,
            "mean_ttft_s": mean_ttft,
        },
        "flags": flags,
        "verdict": verdict,
    }


def to_markdown(report: Dict[str, Any]) -> str:
    sid = report["session_id"]
    header = f"# Session audit \u2014 {sid}"

    lines = [header, ""]
    lines.append(f"**Verdict:** {report['verdict']}")
    lines.append("")

    lines.append("## Counts")
    for role, count in report["counts"].items():
        lines.append(f"- {role}: {count}")
    lines.append("")

    lines.append(f"## Duration: {report['duration_s']}s")
    lines.append("")

    lines.append("## Tool Calls")
    lines.append(f"- Total: {report['tool_calls']}")
    if report["per_tool"]:
        lines.append("- Per tool:")
        for name, count in report["per_tool"].items():
            lines.append(f"  - {name}: {count}")
    lines.append("")

    lines.append(f"## Wasted Calls: {report['wasted_calls']}")
    lines.append("")

    lines.append(f"## Errors: {report['errors']} message(s)")
    lines.append("")

    lines.append(f"## Loops: {report['loops']} degenerate block(s)")
    lines.append("")

    lines.append("## Speed")
    lines.append(f"- Mean tokens/s: {report['speed']['mean_tokens_per_s']}")
    lines.append(f"- Mean TTFT (s): {report['speed']['mean_ttft_s']}")
    lines.append("")

    flags_str = ", ".join(report["flags"]) if report["flags"] else "None"
    lines.append(f"## Flags: {flags_str}")

    return "\n".join(lines) + "\n"


def audit_file(path: Union[str, Path], out_dir: Union[str, Path]) -> Path:
    path = Path(path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with path.open("r", encoding="utf-8") as f:
        session = json.load(f)

    report = analyze(session)
    md_content = to_markdown(report)

    audit_path = out_dir / f"{report['session_id']}-audit.md"
    audit_path.write_text(md_content, encoding="utf-8")
    return audit_path

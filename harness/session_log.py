"""Append-only event trace, one file per chat/agent session.

Why a second file rather than more fields on the transcript: the transcript is
rewritten whole on every append (c_d9e12d6a reached 2.6 MB), so it cannot be
followed while a session runs, and it records what was SAID, not what things
cost. This records cost and incident -- how long each tool took, how much it
returned, how full the window got, and everything that went wrong.

The anomaly vocabulary is closed on purpose. A trace that only records the
happy path reads exactly like a trace of a session that went well; the
2026-07-27 audit found 854 replayed calls and 15 reasonless tool errors that
no log had ever mentioned.

Owns its file, like ChatStore and JobStore. Writing never raises: a session
that finishes is worth more than its telemetry.
"""
import hashlib
import json
import time
from pathlib import Path

# Args are digested, never stored: write_file carries a whole file in them, and
# the trace must not become a second copy of the workspace.
DIGEST_CHARS = 12
PREVIEW_CHARS = 60

ANOMALIES = (
    "replayed_call",        # same tool, same arguments, already seen
    "tool_error",           # the tool returned "error: ..."
    "context_overrun",      # the prompt did not fit the window
    "output_truncated",     # generation stopped on the length cap
    "messages_dropped",     # a span did not fit and was cut from the prompt
    "compacted",            # a summary replaced part of the history
    "generation_cut",       # RepetitionGuard stopped degenerate output
    "toolset_over_budget",  # more tools exposed than small models handle
    "map_degraded",         # the project map fell back to directories
    "gate_refused",         # a phase gate refused the call
)


def digest(args):
    """Short stable fingerprint of a call's arguments, plus a readable hint."""
    try:
        blob = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = repr(args)
    short = hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[
        :DIGEST_CHARS]
    hint = ""
    if isinstance(args, dict):
        for key in ("path", "pattern", "command"):
            if args.get(key):
                hint = str(args[key])[:PREVIEW_CHARS]
                break
    return short, hint


def error_kind(out):
    """The failure named by a tool's output, or "" when it succeeded."""
    text = (out or "").lstrip()
    if not text.lower().startswith("error:"):
        return ""
    return text.split("\n")[0][len("error:"):].strip()[:120]


class SessionLog:
    """One session's events. Append-only; never raises."""

    def __init__(self, path):
        self.path = Path(path)
        self._seen = set()

    def _write(self, event):
        event.setdefault("t", round(time.time(), 3))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except (OSError, ValueError, TypeError):
            pass          # telemetry must never cost a session its turn

    def anomaly(self, what, detail=""):
        self._write({"kind": "anomaly", "what": what, "detail": str(detail)})

    def turn_start(self, model, num_ctx, prompt_est=0):
        self._write({"kind": "turn_start", "model": model, "num_ctx": num_ctx,
                     "prompt_est": prompt_est})

    def tool(self, name, args, ms, out, ctx_est=None):
        """One tool call and what it cost.

        `ctx_est` is the running context estimate AFTER this call, which is
        what lets a viewer watch the window fill up as files are read instead
        of learning it once the turn is over.
        """
        short, hint = digest(args)
        kind = error_kind(out)
        self._write({"kind": "tool", "name": name, "args_digest": short,
                     "args_hint": hint, "ms": int(ms),
                     "out_chars": len(out or ""), "error": bool(kind),
                     "error_kind": kind, "ctx_est": ctx_est})
        if kind:
            self.anomaly("tool_error", "{}: {}".format(name, kind))
        key = (name, short)
        if key in self._seen:
            self.anomaly("replayed_call", "{} {}".format(name, hint or short))
        else:
            self._seen.add(key)

    def turn_end(self, num_ctx, prompt_count, eval_count, done_reason,
                 tokens_per_s=0.0, ttft_s=0.0, evicted=0, dropped=0,
                 compacted=False, stopped="", cost_usd=0.0):
        self._write({"kind": "turn_end", "num_ctx": num_ctx,
                     "prompt_count": prompt_count, "eval_count": eval_count,
                     "done_reason": done_reason,
                     "tokens_per_s": round(tokens_per_s or 0.0, 1),
                     "ttft_s": round(ttft_s or 0.0, 2),
                     # A local turn is free and records 0.0; a cloud turn puts
                     # its price here, and a session total is their sum.
                     "cost_usd": round(cost_usd or 0.0, 6),
                     "evicted": evicted, "dropped": dropped,
                     "compacted": bool(compacted),
                     "ctx_used": (prompt_count or 0) + (eval_count or 0),
                     "ctx_pct": int(100 * ((prompt_count or 0)
                                           + (eval_count or 0)) / num_ctx)
                     if num_ctx else 0})
        if num_ctx and (prompt_count or 0) > num_ctx:
            self.anomaly("context_overrun",
                         "prompt {} > num_ctx {}".format(prompt_count,
                                                         num_ctx))
        if done_reason == "length":
            self.anomaly("output_truncated", "done_reason=length")
        if dropped:
            self.anomaly("messages_dropped", "{} message(s)".format(dropped))
        if compacted:
            self.anomaly("compacted", "history summarised")
        if stopped:
            self.anomaly("generation_cut", str(stopped))


def read(path):
    """Events from a trace file. A corrupt line is skipped, never fatal: a
    half-written last line is the normal state of a live session."""
    out = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def summarize(events):
    """What a post-mortem asks first: how many calls, how much time, what
    failed, what was done twice."""
    tool_calls, tool_ms, tool_chars, anomalies = {}, {}, {}, {}
    calls = errors = 0
    turns = ctx_peak = 0
    for e in events:
        kind = e.get("kind")
        if kind == "tool":
            name = e.get("name", "?")
            calls += 1
            tool_calls[name] = tool_calls.get(name, 0) + 1
            tool_ms[name] = tool_ms.get(name, 0) + int(e.get("ms") or 0)
            tool_chars[name] = tool_chars.get(name, 0) + int(
                e.get("out_chars") or 0)
            if e.get("error"):
                errors += 1
        elif kind == "anomaly":
            what = e.get("what", "?")
            anomalies[what] = anomalies.get(what, 0) + 1
        elif kind == "turn_end":
            turns += 1
            ctx_peak = max(ctx_peak, int(e.get("ctx_pct") or 0))
    return {"calls": calls, "errors": errors, "turns": turns,
            "replayed": anomalies.get("replayed_call", 0),
            "ctx_peak_pct": ctx_peak,
            "tool_calls": tool_calls, "tool_ms": tool_ms,
            "tool_chars": tool_chars, "anomalies": anomalies}

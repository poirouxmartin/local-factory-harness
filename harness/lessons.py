"""What the factory learns about itself, across sessions.

Pure functions over plain dicts, no I/O: `chat_store` owns the file and
`factory_mcp` owns the wiring. A lesson is `{pattern, count, suggestion,
last_seen}` -- a fact plus what to do about it, because a fact alone teaches
nothing.

The rendered block goes into the agent system prompt, which is the KV prefix
(`--cache-reuse`, locked 2026-07-19). Rendering is therefore deterministic and
bounded: same lessons in, same bytes out.
"""

MAX_LESSONS = 10
BUDGET_CHARS = 1500

HEADER = "--- Lessons from previous sessions ---"
FOOTER = "--- End lessons ---"

# A pattern earns a lesson only above these; one duplicated call in a long
# session is noise, and a prompt full of noise teaches the model nothing.
WASTED_CALLS_MIN = 5
ERRORS_MIN = 3
LOOPS_MIN = 1

_SUGGESTIONS = {
    "wasted_tool_calls":
        "You re-ran identical tool calls ({count} wasted). Before repeating a "
        "command, re-read the result you already have.",
    "tool_errors":
        "{count} tool calls failed. Read the error text: it names the "
        "recovery move.",
    "generation_loops":
        "Your generation looped and had to be cut. When a reply stops making "
        "progress, change approach instead of rephrasing.",
}


def merge(known, new):
    """`known` plus `new`, deduplicated by pattern: counts add up, last_seen
    moves forward. Returns a new list -- the store hands out its own list and
    mutating it would change the system prompt mid-session."""
    out = [dict(l) for l in known]
    by_pattern = {l.get("pattern"): l for l in out}
    for lesson in new:
        seen = by_pattern.get(lesson.get("pattern"))
        if seen is None:
            out.append(dict(lesson))
            by_pattern[lesson.get("pattern")] = out[-1]
            continue
        seen["count"] = seen.get("count", 0) + lesson.get("count", 0)
        seen["last_seen"] = max(seen.get("last_seen", 0),
                                lesson.get("last_seen", 0))
        if lesson.get("suggestion"):
            seen["suggestion"] = lesson["suggestion"]
    return out


def from_audit(report, now):
    """Lessons implied by a `session_audit.analyze` report."""
    found = []
    thresholds = (("wasted_tool_calls", report.get("wasted_calls", 0),
                   WASTED_CALLS_MIN),
                  # `tool_errors`, not `errors`: the latter counts streams
                  # that broke, which teaches the model nothing about itself.
                  ("tool_errors", report.get("tool_errors", 0), ERRORS_MIN),
                  ("generation_loops", report.get("loops", 0), LOOPS_MIN))
    for pattern, count, floor in thresholds:
        if count >= floor:
            found.append({"pattern": pattern, "count": count,
                          "suggestion": _SUGGESTIONS[pattern].format(
                              count=count),
                          "last_seen": now})
    return found


_TRACE_SUGGESTIONS = {
    "replayed_calls":
        "You re-ran identical tool calls ({detail}), costing {cost_s} s and "
        "~{cost_tokens} tokens of window. Re-read the result you already have "
        "before repeating a call.",
    "hot_target":
        "You called {detail}. Once a file is read, keep its conclusion in "
        "your plan: repeating the call is what pushed the answer out of the "
        "window in the first place.",
    "oversized_output":
        "{detail}. Read the part you need or search the file instead of "
        "pulling all of it into the window.",
    "failing_tool":
        "{detail}. Read the error text: it names the recovery move.",
    "output_truncated":
        "{detail} and the tool call of that turn was thrown away. Keep a turn "
        "to one action, then stop.",
}


def from_trace(report, now):
    """Lessons implied by a `session_analysis.analyze` report.

    Only findings a model can act on (`MODEL_FINDINGS`) become lessons: these
    go into the agent's system prompt, and telling a model its project map
    degraded teaches it nothing.

    `count` is 1 per session, not per occurrence. The analysis re-runs over the
    whole trace at every turn and `merge` ADDS counts, so an occurrence count
    here would read as "seen in 29 sessions" after a single one. The occurrence
    figure lives in the suggestion text, where it is read instead of summed.
    """
    models = report.get("models") or []
    scope = {"model": models[0]} if len(models) == 1 else None
    found = []
    for f in report.get("findings") or []:
        template = _TRACE_SUGGESTIONS.get(f.get("code"))
        if not template:
            continue
        lesson = {"pattern": f["code"], "count": 1, "last_seen": now,
                  "suggestion": template.format(
                      detail=f.get("detail", ""),
                      cost_s=f.get("cost_s", 0),
                      cost_tokens=f.get("cost_tokens", 0))}
        if scope:
            lesson["scope"] = dict(scope)
        found.append(lesson)
    return found


def incident(pattern, now, suggestion=None):
    """A single live failure, ready for `merge`."""
    return {"pattern": pattern, "count": 1, "last_seen": now,
            "suggestion": suggestion or _SUGGESTIONS.get(pattern, pattern)}


def render(known):
    """The prompt block: the most frequent lessons, deterministic order,
    bounded. No lessons must add no bytes at all."""
    if not known:
        return ""
    ranked = sorted(known, key=lambda l: (-l.get("count", 0),
                                          l.get("pattern", "")))
    lines = []
    size = len(HEADER) + len(FOOTER) + 2
    for lesson in ranked[:MAX_LESSONS]:
        line = "\n- {}".format(lesson.get("suggestion") or lesson["pattern"])
        if size + len(line) > BUDGET_CHARS:
            break
        lines.append(line)
        size += len(line)
    if not lines:
        return ""
    return HEADER + "".join(lines) + "\n" + FOOTER


def _spans_rungs(attempts):
    """True when these attempts did NOT all come from the same ladder rung.

    An identical failure set inside one rung means that rung is stuck, and the
    ladder already knows the answer: escalate. It only means the prompt is
    missing a fact once a STRONGER model, handed the same prompt on a clean
    slate, reproduces the same set. The run that first shipped this function
    is the counter-example: the 30b repeated 11/12 twice, and the 35b then
    closed it from the identical prompt -- so the spec was fine and this
    lesson, unqualified, would have blamed it (2026-07-25).

    Attempts without a `stage` predate the distinction; treat them as spanning
    so the older, looser behaviour is preserved rather than silently dropped.
    """
    stages = {a.get("stage") for a in attempts}
    return len(stages) > 1 or stages == {None}


def from_job_outcome(outcome, now):
    """Lessons implied by a finished job outcome."""
    found = []

    attempts = outcome.get("attempts") or []
    if not attempts:
        return found

    final_model = outcome.get("final_model", "")
    edit_mode = outcome.get("edit_mode", "")
    success = outcome.get("success", False)
    failure_reason = outcome.get("failure_reason")

    # 1. output_wall
    length_attempts = [a for a in attempts if a.get("done_reason") == "length"]
    if length_attempts:
        max_eval = max(a.get("eval_count", 0) for a in length_attempts)
        found.append({
            "pattern": "output_wall",
            "count": len(length_attempts),
            "suggestion": "Set a token cap at %d to prevent truncation." % max_eval,
            "last_seen": now,
            "scope": {"model": final_model, "edit_mode": edit_mode},
        })

    # 2. no_code_in_diff
    if failure_reason == "no_code" and edit_mode == "diff":
        nocode_count = sum(1 for a in attempts if a.get("result") == "NoCode")
        found.append({
            "pattern": "no_code_in_diff",
            "count": nocode_count,
            "suggestion": "Diff mode requires SEARCH/REPLACE blocks. Ensure the model produces them.",
            "last_seen": now,
            "scope": {"model": final_model, "edit_mode": "diff"},
        })

    # 3. underspecified_spec
    if not success and failure_reason == "spec_tests" and len(attempts) >= 2:
        last_two = attempts[-2:]
        failing_sets = [set(a.get("failing") or []) for a in last_two]
        if failing_sets[0] and failing_sets[0] == failing_sets[1] \
                and _spans_rungs(last_two):
            found.append({
                "pattern": "underspecified_spec",
                "count": 1,
                "suggestion": "The model reproduced identical failures: the prompt never carried the necessary spec details.",
                "last_seen": now,
                "scope": {"model": final_model},
            })

    # 4. first_attempt_solve
    if success and len(attempts) == 1:
        found.append({
            "pattern": "first_attempt_solve",
            "count": 1,
            "suggestion": "The first attempt solved it on the first try. Keep this prompt style.",
            "last_seen": now,
            "scope": {"model": final_model, "edit_mode": edit_mode},
        })

    return found

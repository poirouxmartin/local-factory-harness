"""Condense a judge's output into the feedback a retry can act on.

`result.output[-3000:]` was chronological: it kept the tail of the reporter
stream, which on a multi-failure run is whichever tests happen to sit last in
the file. Banc A (2026-07-25) measured the cost -- 9 failures, 12k chars, ONE
failure named in the window, and never the causal one. Every model stalled the
same way, which is the signature of a harness defect, not a plateau.

Two rules, in this order:
  1. breadth -- every failing test is named, because a name the model never
     sees is a fix it cannot make;
  2. depth -- assertion evidence (expected/actual) fills what budget is left,
     head-first, since the first failure is usually the cause and the rest the
     consequences.

Stack frames, source locations and absolute paths are dropped outright: they
are a third of the window and say nothing a model can act on.
"""
import re

# Reporter bookkeeping: true across node:test TAP, pytest and vitest output.
# `expected:`/`actual:` are deliberately NOT here: dropping the key while
# keeping its children leaves two anonymous contradictory blocks, which is
# worse than the noise it saves (measured on j_3a6a7681, 2026-07-25).
_NOISE_KEY = re.compile(
    r"^\s*(location|stack|duration_ms|type|code|name|operator|failureType):"
    r"\s*(\|-)?\s*$")
_NOISE_INLINE = re.compile(
    r"^\s*(location|duration_ms|type|code|name|operator|failureType):\s*\S")
_STACK_FRAME = re.compile(
    r"node:internal|node:async_hooks|runInAsyncScope|^\s*at\s|"
    r"^\s*(Test|TestContext|async Test)\.")
_YAML_EDGE = re.compile(r"^\s*(---|\.\.\.)\s*$")
# TAP repeats every test name as a `# Subtest:` header before its result line;
# for a failure the `not ok` line already carries it.
_SUBTEST = re.compile(r"^\s*# Subtest:")
_FAIL_LINE = re.compile(r"^(not ok \d+ -|FAILED |\s*(×|✕|FAIL)\s)")
_TRUNCATED = "[... evidence for the remaining failures elided]"


def _is_noise(line):
    return bool(_NOISE_KEY.match(line) or _NOISE_INLINE.match(line)
                or _STACK_FRAME.search(line) or _YAML_EDGE.match(line)
                or _SUBTEST.match(line))


def _fit_blocks(detail, room):
    """`detail` cut on a failure boundary, never mid-assertion.

    A hard slice ends the model's last piece of evidence on something like
    `+   label: '` -- a fragment it can only misread. Whole blocks or nothing,
    and say so when blocks were dropped.
    """
    if len(detail) <= room:
        return detail
    lines, out, used = detail.split("\n"), [], 0
    marker = "\n" + _TRUNCATED
    for line in lines:
        cost = len(line) + 1
        if used + cost > room - len(marker):
            break
        # Never stop just before a block's first line: keep whole blocks only.
        out.append(line)
        used += cost
    while out and not _FAIL_LINE.match(out[-1]) and len(out) > 1:
        # Walk back to the end of the last COMPLETE block.
        if any(_FAIL_LINE.match(l) for l in out[:-1]):
            out.pop()
            continue
        break
    while out and _FAIL_LINE.match(out[-1]):
        out.pop()          # a bare header with no evidence teaches nothing
    return ("\n".join(out).rstrip() + marker) if out else ""


def _detail(output):
    """The reporter stream minus its bookkeeping, from the first failure on."""
    lines = output.replace("\r\n", "\n").split("\n")
    start = next((i for i, l in enumerate(lines) if _FAIL_LINE.match(l)), None)
    if start is None:
        return ""
    kept = [l.rstrip() for l in lines[start:] if not _is_noise(l)]
    # Blank runs left behind by dropped keys read as structure that is not there.
    out, blank = [], False
    for line in kept:
        if not line.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(line)
    return "\n".join(out).strip()


def _name_block(names, budget):
    """All failing names, or as many as fit with the remainder announced."""
    if not names:
        return ""
    head = "FAILING TESTS ({}):\n".format(len(names))
    lines, used = [], len(head)
    for i, name in enumerate(names):
        entry = "- {}\n".format(name)
        # Leave room for the "... N more" line so truncation is never silent.
        tail = "... {} more\n".format(len(names) - i)
        if used + len(entry) + (len(tail) if i < len(names) - 1 else 0) > budget:
            if used + len(tail) <= budget:
                lines.append(tail)
            break
        lines.append(entry)
        used += len(entry)
    return head + "".join(lines)


def condense(output, failed_names, budget=3000):
    """Failure-oriented view of `output`, at most `budget` characters."""
    if not output.strip():
        return ""
    names = _name_block(failed_names or [], budget)
    detail = _detail(output)
    if not names and not detail:
        return output[-budget:]          # unknown reporter: the old behaviour
    if not names:
        return detail[:budget]
    room = budget - len(names) - 1
    if detail and room > 0:
        return (names + "\n" + _fit_blocks(detail, room))[:budget]
    return names[:budget]

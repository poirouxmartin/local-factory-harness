"""What the factory learns about one workspace, across sessions.

A *lesson* (`lessons.py`) is about the agent and holds everywhere. A *memory*
is about this project only -- "the tests need the venv python", "pip install
torch fails here" -- so it lives with the project, in `<workspace>/MEMORY.md`,
and travels with it. This is the ROADMAP "Agent memory (anti-redo)" item: the
July 14 PDF phase burned 19 near-identical scripts re-attempting what had
already failed.

The file is the store, not a cache of one: humans read and edit it. Everything
above the `factory:auto` marker belongs to them and is never rewritten; the
block below is regenerated from what the agent actually observed failing. That
is why entries round-trip through markdown instead of a JSON sidecar -- a
memory nobody can read teaches nobody.

Pure functions over plain dicts, no I/O: `factory_mcp` owns the file. An entry
is `{command, detail, count, last_seen}` -- what was tried, how it failed, how
often, when.

The rendered block goes into the agent system prompt, which is the KV prefix
(`--cache-reuse`, locked 2026-07-19), so rendering is deterministic and
bounded: same entries in, same bytes out.
"""
import json
import re

MAX_ENTRIES = 10
BUDGET_CHARS = 2000
DETAIL_CHARS = 160

# Markers, not a whole-file rewrite: MEMORY.md is a project file a human owns.
BEGIN = "<!-- factory:auto -->"
END = "<!-- /factory:auto -->"
PREAMBLE = ("Recorded automatically from agent sessions. "
            "Edit above this marker; this block is regenerated.")

HEADER = "--- Workspace memory (MEMORY.md) ---"
FOOTER = "--- End workspace memory ---"

# Once is a typo, twice is the workspace telling the agent something it does
# not know. Below this floor MEMORY.md fills with noise and teaches nothing.
FAILURES_MIN = 2

_ENTRY_RE = re.compile(r"^- `(?P<command>.+?)` failed (?P<count>\d+)x: "
                       r"(?P<detail>.*)$")
_EXIT_RE = re.compile(r"^exit (-?\d+)\b")

# Second managed block: what the agent hit (`log_wall`) and what it chose to
# write down (`remember`). Failures are arithmetic over the transcript; these
# are the agent's own words, so they get their own block and their own floor
# (none -- the agent asked for them to be kept).
CARNET_BEGIN = "<!-- factory:carnet -->"
CARNET_END = "<!-- /factory:carnet -->"
CARNET_PREAMBLE = ("Walls and notes recorded by the agent itself. "
                   "Edit outside the markers; this block is regenerated.")
MAX_WALLS = 5
MAX_NOTES = 5
_WALL_RE = re.compile(r"^- MUR (?P<wall>.+?) \((?P<cause>.*?)\): "
                      r"tente (?P<tried>.*?); reste (?P<remaining>.*)$")
_NOTE_RE = re.compile(r"^- note: (?P<note>.*)$")
_LIST_SEP = " | "
_EMPTY_LIST = "-"


# ---- capture ---------------------------------------------------------------

def _calls(msg):
    """The (name, arguments) pairs of an assistant message, arguments always a
    dict -- llama-server hands them over as a JSON string or already parsed."""
    out = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        out.append((fn.get("name"), args if isinstance(args, dict) else {}))
    return out


def failed(output):
    """Did this run_command output report a failure? A refusal or a timeout
    says so in words; a real command says it in its exit code."""
    text = str(output or "").lstrip()
    if text.lower().startswith("error:"):
        return True
    m = _EXIT_RE.match(text)
    return bool(m) and m.group(1) != "0"


def _is_decoration(line):
    """A rule of '=' or a caret marker carries no reason: no letter, no digit."""
    return not any(ch.isalnum() for ch in line)


def _unwrap(lines):
    """Re-join what the shell wrapped. cmd.exe splits its commonest complaint
    over two lines ("'wc' n'est pas reconnu en tant que commande interne" /
    "ou externe, un programme executable...") and only the first half names
    the command, so choosing either line alone loses half the reason.

    A line continues the previous one when it opens in lowercase and the
    previous one did not close a sentence."""
    out = []
    for line in lines:
        head = line[:1]
        if out and head.islower() and not out[-1].endswith((".", "!", "?")):
            out[-1] = out[-1] + " " + line
        else:
            out.append(line)
    return out


def detail_of(output):
    """The one line worth remembering: what the shell complained about.

    The reason lives at the END. A Python traceback opens with its header and
    closes with the exception; a script that prints a banner opens with a rule
    of '='. Replaying the 44 archived sessions (2026-07-22) showed the first
    line was almost never the reason -- the entries read "failed 3x: ========"
    and "failed 4x: Traceback (most recent call last):", which is the failure
    this docstring exists to prevent.
    """
    text = str(output or "")
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    if not lines:
        return "no output"
    # When the runner separates the streams, the reason is on stderr and
    # nothing before the marker can compete with it.
    for i, line in enumerate(lines):
        if line.lower() == "stderr:" and i + 1 < len(lines):
            lines = lines[i + 1:]
            break
    # exit N is a verdict, not a reason; decoration is not a reason either.
    reasons = [l for l in lines
               if not _EXIT_RE.match(l) and not _is_decoration(l)]
    if reasons:
        return _clip(_unwrap(reasons)[-1])
    return _clip(lines[0])


def _clip(line):
    line = " ".join(line.split())
    if len(line) <= DETAIL_CHARS:
        return line
    return line[:DETAIL_CHARS - 3] + "..."


def from_session(session, now):
    """Every shell command this transcript saw fail, with its raw count.

    Raw on purpose: `FAILURES_MIN` is applied by `promote`, against the total a
    command has reached across the file AND the session. Thresholding here
    would lose a command that fails once per turn over three turns -- each
    slice sees one failure and forgets it (gap opened 2026-07-22 by the
    learning watermark).

    Pairing is positional -- `_drain_calls` appends tool results in call order
    -- and a mismatch drops the pair rather than inventing one."""
    counts = {}
    details = {}
    pending = []
    for msg in session.get("messages") or []:
        role = msg.get("role")
        if role == "assistant":
            pending = _calls(msg)
            continue
        if role != "tool":
            continue
        name = msg.get("tool_name")
        if name == "system":  # loop nudges, iteration warnings: not tool output
            continue
        if name in ("note", "wall", "plan", "step", "progress", "handback"):
            # remember/log_wall/set_plan/step_done/ask_operator/request_resource
            # persist under a canonical tool_name that differs from the call name
            # (and `progress` has no call at all): pairing by name equality would
            # never match, draining the pending queue of unrelated calls looking
            # for one (same skip as carnet.project).
            continue
        call = None
        while pending:
            candidate = pending.pop(0)
            if candidate[0] == name:
                call = candidate
                break
        if call is None or name != "run_command":
            continue
        command = " ".join(str(call[1].get("command") or "").split())
        if not command or not failed(msg.get("content")):
            continue
        counts[command] = counts.get(command, 0) + 1
        details[command] = detail_of(msg.get("content"))
    return [{"command": c, "detail": details[c], "count": n, "last_seen": now}
            for c, n in sorted(counts.items())]


def promote(raw, written, known, now):
    """What of `raw` belongs in MEMORY.md, and what this session has now
    contributed.

    `raw` is recounted over the WHOLE transcript every turn, so `written` --
    how much of each command this session already put in the file -- is what
    keeps a failure from being counted once per remaining turn. A command below
    `FAILURES_MIN` is simply not written; nothing is lost, because the next
    turn recounts the same transcript and it crosses the floor on its own.

    Returns `(entries, written)`: the merged entry list to write, and the new
    per-session contribution to persist.
    """
    by_command = {e.get("command"): e for e in known}
    written = dict(written or {})
    new = []
    for entry in raw:
        command = entry.get("command")
        delta = entry.get("count", 0) - written.get(command, 0)
        if delta <= 0:
            continue
        if by_command.get(command, {}).get("count", 0) + delta < FAILURES_MIN:
            continue
        new.append(dict(entry, count=delta, last_seen=now))
        written[command] = entry.get("count", 0)
    return merge(known, new), written


def merge(known, new):
    """`known` plus `new`, deduplicated by command: counts add up, the newest
    detail wins. Returns a new list -- the caller's is not ours to mutate."""
    out = [dict(e) for e in known]
    by_command = {e.get("command"): e for e in out}
    for entry in new:
        seen = by_command.get(entry.get("command"))
        if seen is None:
            out.append(dict(entry))
            by_command[entry.get("command")] = out[-1]
            continue
        seen["count"] = seen.get("count", 0) + entry.get("count", 0)
        seen["last_seen"] = max(seen.get("last_seen", 0),
                                entry.get("last_seen", 0))
        if entry.get("detail"):
            seen["detail"] = entry["detail"]
    return out


# ---- the file --------------------------------------------------------------

def _body(text, begin, end):
    """What sits between two markers, or "" when the block is absent."""
    text = text or ""
    if begin in text and end in text:
        return text.split(begin, 1)[1].split(end, 1)[0]
    return ""


def _replace_block(text, begin, end, block):
    """`text` with one managed block replaced or appended. The rest is
    returned byte-identical; an empty file gets a title."""
    if begin in text and end in text:
        head, rest = text.split(begin, 1)
        return head + block + rest.split(end, 1)[1]
    head = text.rstrip()
    if not head:
        head = "# MEMORY"
    return head + "\n\n" + block + "\n"


def parse(text):
    """The entries in the managed block. Anything unparseable is dropped, not
    guessed at: the human half of the file lives outside the markers."""
    body = _body(text, BEGIN, END)
    out = []
    for line in body.splitlines():
        m = _ENTRY_RE.match(line.strip())
        if m:
            out.append({"command": m.group("command"),
                        "detail": m.group("detail"),
                        "count": int(m.group("count")), "last_seen": 0})
    return out


def _ranked(entries):
    return sorted(entries, key=lambda e: (-e.get("count", 0),
                                          e.get("command", "")))[:MAX_ENTRIES]


def _line(entry):
    return "- `{}` failed {}x: {}".format(entry.get("command", ""),
                                          entry.get("count", 0),
                                          entry.get("detail", ""))


def update(text, entries):
    """`text` with the managed block replaced by `entries`. The human part is
    returned byte-identical; a file that has never seen the factory gets the
    block appended, with a title if it was empty."""
    block = "\n".join([BEGIN, PREAMBLE, ""] +
                      [_line(e) for e in _ranked(entries)] + [END])
    return _replace_block(text, BEGIN, END, block)


# ---- walls and notes -------------------------------------------------------

def _one_line(text):
    """A field is one line and fits: `|`, `(` and `)` are the separators of
    the line format, so they never survive inside a field."""
    return _clip(str(text or "").replace("|", "/")
                 .replace("(", "[").replace(")", "]"))


def _fmt_list(items):
    parts = [_one_line(i) for i in items or [] if str(i).strip()]
    return _LIST_SEP.join(parts) if parts else _EMPTY_LIST


def _parse_list(text):
    if not text or text.strip() == _EMPTY_LIST:
        return []
    return [p.strip() for p in text.split(_LIST_SEP) if p.strip()]


def _wall_entry(wall):
    """Normalized exactly as it will be rendered, so a wall read back from the
    file and the same wall coming from the session compare equal."""
    return {"wall": _one_line(wall.get("wall")),
            "cause": _one_line(wall.get("cause")),
            "tried": _parse_list(_fmt_list(wall.get("tried"))),
            "remaining": _parse_list(_fmt_list(wall.get("remaining")))}


def _wall_line(wall):
    return "- MUR {} ({}): tente {}; reste {}".format(
        wall["wall"], wall["cause"],
        _fmt_list(wall["tried"]), _fmt_list(wall["remaining"]))


def parse_walls(text):
    out = []
    for line in _body(text, CARNET_BEGIN, CARNET_END).splitlines():
        m = _WALL_RE.match(line.strip())
        if m:
            out.append({"wall": m.group("wall"), "cause": m.group("cause"),
                        "tried": _parse_list(m.group("tried")),
                        "remaining": _parse_list(m.group("remaining"))})
    return out


def parse_notes(text):
    out = []
    for line in _body(text, CARNET_BEGIN, CARNET_END).splitlines():
        m = _NOTE_RE.match(line.strip())
        if m and m.group("note").strip():
            out.append(m.group("note").strip())
    return out


def _merge_walls(known, new):
    """One entry per wall, the latest state of it winning: an agent that hits
    the same wall again has usually tried more alternatives since."""
    out, by_wall = [], {}
    for wall in list(known) + [_wall_entry(w) for w in new or []]:
        seen = by_wall.get(wall["wall"])
        if seen is None:
            out.append(dict(wall))
            by_wall[wall["wall"]] = out[-1]
        else:
            seen.update(wall)
    return out[-MAX_WALLS:]


def _merge_notes(known, new):
    out = list(known)
    for note in [_one_line(n) for n in new or []]:
        if note and note not in out:
            out.append(note)
    return out[-MAX_NOTES:]


def update_carnet(text, walls, notes):
    """`text` with the carnet block replaced by what the file already held
    plus this session's walls and notes.

    Merged, not appended: the projection recounts the whole transcript every
    turn, so writing blindly would stack the same note once per turn, and a
    second session would erase the first one's walls."""
    text = text or ""
    walls = _merge_walls(parse_walls(text), walls)
    notes = _merge_notes(parse_notes(text), notes)
    if not walls and not notes:
        return text
    block = "\n".join([CARNET_BEGIN, CARNET_PREAMBLE, ""] +
                      [_wall_line(w) for w in walls] +
                      ["- note: {}".format(n) for n in notes] + [CARNET_END])
    return _replace_block(text, CARNET_BEGIN, CARNET_END, block)


def render(text):
    """The prompt block: MEMORY.md as the human wrote it plus what the agent
    recorded, bounded. An empty or missing file must add no bytes at all."""
    body = (text or "").strip()
    if not body:
        return ""
    room = BUDGET_CHARS - len(HEADER) - len(FOOTER) - 2
    if len(body) > room:
        body = body[:room].rsplit("\n", 1)[0].rstrip()
        if not body:
            return ""
    return HEADER + "\n" + body + "\n" + FOOTER

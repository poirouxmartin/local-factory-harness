"""The agent's live working memory for one session.

A pure projection of the transcript -- never a separate store. Computed fresh
each turn, like workspace_memory.from_session, so it always reflects what just
happened and survives a refresh. remember/log_wall write tool-role messages
into the transcript; this module reads them back.
"""
import json
import re
import workspace_memory


def _norm_path(p):
    s = str(p or "").strip().strip('"').replace("\\", "/")
    if s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


_SEP_RE = re.compile(r"\s*(?:&&|\|\||;|&|\|)\s*")


def _action_of(cmd):
    """The segment that does the work. cmd.exe keeps no cwd between calls, so
    the agent prefixes nearly everything with `cd <workspace> &&`; keying on
    the head made every one of those the same target. What follows the last
    leading `cd` is the action; a trailing `|| echo missing` is a fallback and
    `| findstr x` a filter, not a second action.

    Mechanical, no shell parsing (Phase 1 rule: measure before adding
    smarts) -- so a `;` inside a quoted `python -c "..."` splits too, and
    distinct one-liners collapse onto their first word."""
    segments = [s for s in _SEP_RE.split(cmd) if s.strip()]
    for segment in segments:
        if segment.split()[0].lower() != "cd":
            return segment
    return segments[0] if segments else ""


def target_of(name, args):
    args = args or {}
    if name == "run_command":
        cmd = " ".join(str(args.get("command") or "").split())
        parts = _action_of(cmd).split()
        if not parts:
            return "cmd:"
        head = parts[0]
        path_like = ""      # first path-shaped token wins
        first_bare = ""     # else the first non-flag word
        for tok in parts[1:]:
            is_flag = (tok.startswith("-") or tok.startswith("/")) and \
                not ("/" in tok[1:] or "\\" in tok)
            if is_flag:
                continue
            if not first_bare:
                first_bare = tok
            if re.search(r"[\\/]", tok) or "." in tok:
                path_like = _norm_path(tok)
                break
        arg = path_like or _norm_path(first_bare)
        return "cmd:{}:{}".format(head, arg)
    if name in ("read_file", "write_file", "edit_file", "list_dir"):
        return "{}:{}".format(name, _norm_path(args.get("path")))
    if name == "search":
        return "search:{}".format(args.get("pattern") or "")
    return name


# The slice now carries the plan too (phase 2). The spec allows it ~800 tokens;
# 2400 chars is ~600, and the arithmetic below keeps the worst case under it
# (a clipped goal + MAX_PLAN_ROWS clipped rows + two alerts).
HOT_BUDGET = 2400
LINE_CHARS = 160
MAX_ROWS = 12
MAX_WALLS = 5
MAX_NOTES = 5
HEADER = "--- Carnet (live) ---"
FOOTER = "--- fin carnet ---"

# Tool messages that are bookkeeping, not actions: the harness's own nudges and
# everything the carnet writes about itself. They are never a target, they never
# pair with a pending call (their stored `tool_name` is not the call name), and
# they do not count as work done.
BOOKKEEPING = ("system", "note", "wall", "plan", "step", "progress",
               "handback")


def _calls(msg):
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


def project(messages):
    targets, notes, walls, pending = {}, [], [], []
    plans, ticks, acted = [], [], 0
    for msg in messages or []:
        role = msg.get("role")
        if role == "assistant":
            pending = _calls(msg)
            continue
        if role != "tool":
            continue
        name = msg.get("tool_name")
        if name == "note":
            notes.append(str(msg.get("content") or ""))
            continue
        if name == "wall":
            try:
                walls.append(json.loads(msg.get("content") or "{}"))
            except ValueError:
                pass
            continue
        if name == "plan":
            plans.append((str(msg.get("content") or ""), msg.get("ts")))
            continue
        if name in ("step", "progress"):
            ticks.append(_json(msg.get("content")))
            continue
        if name == "handback":
            continue
        if name == "system":
            continue
        acted += 1
        call = None
        while pending:
            cand = pending.pop(0)
            if cand[0] == name:
                call = cand
                break
        if call is None:
            continue
        key = target_of(name, call[1])
        content = str(msg.get("content") or "")
        t = targets.setdefault(key, {"n": 0, "fail": 0, "last": ""})
        t["n"] += 1
        if workspace_memory.failed(content):
            t["fail"] += 1
            t["last"] = workspace_memory.detail_of(content)
    return {"targets": targets, "notes": notes, "walls": walls,
            "plan": _plan_from(plans, ticks), "actions": acted}


# ---- the plan (phase 2) ----------------------------------------------------

MAX_PLAN_STEPS = 40
MAX_PLAN_ROWS = 10
# Spec C.2 puts the stagnation threshold at 6-10 actions. 8 is the middle: low
# enough that a thrash of 80 commands is called out early, high enough that one
# genuinely long step (a download, a build) does not trip it every turn.
STAGNATION_AT = 8
# A session that has done this much with no plan gets told to make one. Under
# it, "read this file and tell me X" is a legitimate plan-free task.
PLAN_NUDGE_AT = 3


def _json(content):
    try:
        data = json.loads(content or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


_STEP_SEP = re.compile(r"[\n;]+")
# The row already carries its number ("[ ] 1. ..."), so a step that numbers
# itself is displayed "1. 1. ...". Models number prose lists by reflex.
_ENUM = re.compile(r"^\d{1,2}\s*[.)\]-]\s*")


def _step_list(steps):
    """The `steps` payload as a list of steps.

    A text tool-call protocol carries no types, so the ChatGPT lane delivers
    `steps` as a STRING -- JSON when the model was tidy, a numbered sentence
    when it was not. A string is iterable, which is exactly why this went
    unseen: session c_14f20c89 (2026-08-06) stored four steps and the panel
    showed "0/36", one letter per row, four revisions in a row, because
    `set_plan` reported success every time.

    `llama_client` types the parameter at the boundary now; this is the layer
    that also covers a transcript already on disk, and any lane that parses a
    call without its schema."""
    if isinstance(steps, list):
        return steps
    text = str(steps or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        pass
    else:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    return [p.strip() for p in _STEP_SEP.split(text) if p.strip()]


def parse_plan(content):
    """One `set_plan` payload as a plan, or None when there is nothing usable.

    Tolerant on purpose: a small model writes a step as a bare string as often
    as as an object, and a plan with no step is not a plan."""
    data = _json(content)
    steps = []
    for raw in _step_list(data.get("steps"))[:MAX_PLAN_STEPS]:
        if isinstance(raw, str):
            raw = {"step": raw}
        if not isinstance(raw, dict):
            continue
        step = _ENUM.sub("", " ".join(str(raw.get("step") or "").split()), 1)
        if not step:
            continue
        steps.append({"step": step,
                      "done_when": " ".join(
                          str(raw.get("done_when") or "").split()),
                      "walls": [" ".join(str(w).split())
                                for w in (raw.get("walls") or []) if str(w).strip()],
                      "done": False, "evidence": ""})
    if not steps:
        return None
    return {"goal": " ".join(str(data.get("goal") or "").split()),
            "steps": steps, "revisions": 0}


def _plan_from(plans, ticks):
    """The session's plan: the LAST `set_plan` wins (that is how a plan is
    revised, spec A.3), with the checkpoints re-applied on top.

    A tick carries the step text it was granted for, so a revision that
    reorders or replaces steps does not inherit someone else's checkmark --
    the step has to still be the same step at that index.

    `ts` is when the winning plan was set. The harness needs it to tell a file
    the plan PRODUCED from one that was already lying there (see
    `factory_mcp._changed_since`)."""
    plan, ts = None, None
    for content, at in reversed(plans):
        plan = parse_plan(content)
        if plan:
            ts = at
            break
    if plan is None:
        return None
    plan["ts"] = ts
    plan["revisions"] = len(plans)
    for tick in ticks:
        try:
            i = int(tick.get("i"))
        except (TypeError, ValueError):
            continue
        if not 1 <= i <= len(plan["steps"]):
            continue
        step = plan["steps"][i - 1]
        claimed = " ".join(str(tick.get("step") or "").split())
        if claimed and claimed != step["step"]:
            continue
        step["done"] = True
        step["evidence"] = str(tick.get("evidence") or "") or step["evidence"]
    return plan


_FILE_COND_RE = re.compile(r"^file\s*:\s*(?P<pat>\S+)", re.IGNORECASE)
# A separator or a short extension: what tells a path from a word.
_PATH_SHAPED_RE = re.compile(r"[\\/]|\.\w{1,6}$")


def file_condition(done_when):
    """The path pattern this end condition can be checked against, or None when
    it is prose the harness cannot verify.

    Two accepted forms: the explicit `file:<pattern>` the tool description asks
    for, and a bare single path-shaped token -- models write
    `models/vae/ae.safetensors` far more readily than they follow a prefix
    convention, and refusing it would leave mechanical ticking unused.

    The `file:` form keeps only its first token: models glue prose after the
    path ("file:backend/matchHistory.js has been updated"), and anchoring the
    regex at the end classed that as prose -- no mechanical tick, and
    verify_claim waving the claim through (live run 2026-07-25).

    Either way the token must LOOK like a path. Promoting prose to a pattern
    would be worse than ignoring it: verify_claim refuses a step whose pattern
    is absent, so `file: it compiles` would lock that step shut forever, and
    A.2 is a signal, never a block."""
    text = " ".join(str(done_when or "").split())
    if not text:
        return None
    m = _FILE_COND_RE.match(text)
    token = m.group("pat") if m else (text if len(text.split()) == 1 else None)
    if token and _PATH_SHAPED_RE.search(token):
        return _norm_path(token)
    return None


def _steps(plan):
    return (plan or {}).get("steps") or []


def unfinished(plan):
    """How many steps the plan still owes. 0 without a plan, deliberately:
    progress is defined against the plan (spec C.2, and see `stagnation`), and
    so is the loop's decision to carry on rather than hand back."""
    return sum(1 for step in _steps(plan) if not step["done"])


def newly_satisfied(plan, exists):
    """The mechanically verifiable steps that hold now and are not ticked yet,
    as `(1-based index, done_when)`. `exists(pattern) -> bool` is injected: this
    module does no I/O."""
    out = []
    for i, step in enumerate(_steps(plan), 1):
        if step["done"]:
            continue
        pattern = file_condition(step["done_when"])
        if pattern and exists(pattern):
            out.append((i, step["done_when"]))
    return out


def verify_claim(plan, i, exists):
    """May the agent tick step `i`? Returns `(ok, reason)`.

    A prose condition is taken at its word -- there is nothing to check, and
    calling the agent a liar for free would be worse than a wrong tick. A
    condition naming a file is checked, because "etape 2 faite !" with no file
    behind it is exactly the self-delusion spec A.2 exists to catch.

    `exists` is injected, and what the caller injects decides how much this
    bites. The harness passes a "written since the plan was set" probe: a bare
    existence probe waves through every step that claims to MODIFY a file, and
    in a real repository those are most of them (live 2026-07-25: 5/5 steps
    ticked against an empty diff)."""
    steps = _steps(plan)
    if not steps:
        return False, "no plan yet: call set_plan first"
    try:
        idx = int(i)
    except (TypeError, ValueError):
        return False, "step must be the step number (1-based)"
    if not 1 <= idx <= len(steps):
        return False, "no step {} in the plan ({} steps)".format(idx, len(steps))
    pattern = file_condition(steps[idx - 1]["done_when"])
    if pattern and not exists(pattern):
        return False, ("nothing matching {} has been written since you set the "
                       "plan, so step {} is not done -- the step stays open. "
                       "Do the work, or revise the plan with set_plan if the "
                       "condition was wrong".format(pattern, idx))
    return True, ""


def resource_justified(walls):
    """May the agent ask the operator for an external resource? Only once it has
    proven a wall (spec E: a resource request is not an abandon, it is the last
    isolated input after all the soluble work is done). The mechanical proof is
    a logged wall with nothing left to try on its own -- empty `remaining`. The
    "all remaining are external" nuance is prose the prompt carries; what the
    harness can check is that a dead end was actually recorded."""
    for wall in walls or []:
        if not (wall.get("remaining") or []):
            return True
    return False


def actions(messages):
    """How many real tool results this transcript holds. Bookkeeping messages
    are not work."""
    return sum(1 for m in messages or []
               if m.get("role") == "tool"
               and m.get("tool_name") not in BOOKKEEPING)


def stagnation(messages):
    """Actions run since the last checkpoint (or since the plan was set), and 0
    when there is no plan -- progress is defined against the plan (spec C.2)."""
    started, n = False, 0
    for msg in messages or []:
        if msg.get("role") != "tool":
            continue
        name = msg.get("tool_name")
        if name == "plan":
            started, n = True, 0
        elif name in ("step", "progress"):
            started, n = True, 0
        elif started and name not in BOOKKEEPING:
            n += 1
    return n if started else 0


def _ranked(targets):
    """The targets worth showing -- tried more than once, or failed at all --
    worst first. A long session has dozens; taking them in insertion order
    spent the whole budget on what happened first and cut off the loop the
    agent is in right now (replay of c_d2a43fd5)."""
    rows = [(k, v) for k, v in targets.items() if v["n"] > 1 or v["fail"]]
    rows.sort(key=lambda kv: (-kv[1]["fail"], -kv[1]["n"], kv[0]))
    return rows[:MAX_ROWS]


def _clip(line):
    line = " ".join(str(line or "").split())
    return line if len(line) <= LINE_CHARS else line[:LINE_CHARS - 3] + "..."


def _row(key, stat):
    state = "echec {}x: {}".format(stat["fail"], stat["last"]) if stat["fail"] \
        else "{}x".format(stat["n"])
    return _clip("- {} -> {}".format(key, state))


STAGNATION_LINE = ("- STAGNATION: {n} actions sans qu'un jalon du plan soit "
                   "atteint. Relis ton carnet: change d'approche sur l'etape "
                   "ouverte, ou revise le plan avec set_plan.")
NO_PLAN_LINE = ("- PAS DE PLAN: {n} actions sans plan. Appelle set_plan avec, "
                "pour chaque etape, une condition de fin verifiable "
                "(prefere file:<chemin>) et ses murs potentiels.")


def _plan_lines(plan):
    """The plan block: the goal, how far it got, and the frontier.

    Pinned (spec A.1: never evicted) so it must be bounded by construction --
    a 40-step plan would eat the whole slice. Showing the window around the
    first open step keeps what the agent is doing NOW, which is the same reason
    `_ranked` stopped taking targets in insertion order."""
    steps = plan["steps"]
    done = sum(1 for s in steps if s["done"])
    lines = [_clip("PLAN: {} ({}/{} faits)".format(
        plan["goal"] or "(sans but declare)", done, len(steps)))]
    first_open = next((i for i, s in enumerate(steps) if not s["done"]),
                      len(steps) - 1)
    start = max(0, min(first_open, len(steps) - MAX_PLAN_ROWS))
    for i, step in enumerate(steps[start:start + MAX_PLAN_ROWS], start + 1):
        row = "[{}] {}. {}".format("x" if step["done"] else " ", i, step["step"])
        if step["done_when"]:
            row += " -> " + step["done_when"]
        if step["walls"] and not step["done"]:
            row += " (murs: {})".format(", ".join(step["walls"]))
        lines.append(_clip(row))
    left = len(steps) - (start + MAX_PLAN_ROWS)
    if left > 0:
        lines.append("... +{} etapes".format(left))
    return lines


def render(carnet, plan_expected=False, stalled=0, alerts=()):
    """The hot slice, bounded by whole lines. The plan and the alerts are
    pinned; walls and notes come next -- they are the agent's own words and cost
    a handful of lines -- while rows are regenerated every turn and the cheapest
    thing to drop."""
    plan = carnet.get("plan")
    pinned = _plan_lines(plan) if plan else []
    if plan and stalled >= STAGNATION_AT:
        pinned.append(_clip(STAGNATION_LINE.format(n=stalled)))
    if plan_expected and not plan and \
            carnet.get("actions", 0) >= PLAN_NUDGE_AT:
        pinned.append(_clip(NO_PLAN_LINE.format(n=carnet["actions"])))
    # `phases.alerts`: where the session is, and what it has already wasted.
    # Pinned like the rest -- an alert that gets dropped for a target row is an
    # alert that was not worth computing.
    pinned.extend(_clip(line) for line in alerts or ())
    walls = [_clip("- MUR {} ({}): reste {}".format(
        w.get("wall", ""), w.get("cause", ""), ", ".join(w.get("remaining") or [])))
        for w in carnet["walls"][-MAX_WALLS:]]
    notes = [_clip("- note: {}".format(n)) for n in carnet["notes"][-MAX_NOTES:]]
    rows = [_row(k, v) for k, v in _ranked(carnet["targets"])]
    if not rows and not walls and not notes and not pinned:
        return ""
    room = HOT_BUDGET - len(HEADER) - len(FOOTER) - 2
    room -= sum(len(l) + 1 for l in pinned)
    kept = {"walls": [], "notes": [], "rows": []}
    for kind, line in ([("walls", l) for l in walls] +
                       [("notes", l) for l in notes] +
                       [("rows", l) for l in rows]):
        if len(line) + 1 > room:
            continue   # skip, not stop: a shorter line further down still fits
        kept[kind].append(line)
        room -= len(line) + 1
    return "\n".join([HEADER] + pinned + kept["rows"] + kept["walls"]
                     + kept["notes"] + [FOOTER])


CARNET_TOOLS = [
    {"type": "function", "function": {
        "name": "remember",
        "description": "Record a durable note for THIS project: a discovery, a "
                       "decision, or something that worked. It stays visible in "
                       "your carnet and persists to MEMORY.md.",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string"}}, "required": ["note"]}}},
    {"type": "function", "function": {
        "name": "recall",
        "description": "Read this project's saved memory (MEMORY.md). Optional "
                       "query filters lines.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "log_wall",
        "description": "Record a blocker you hit: what blocks you, the root "
                       "cause, alternatives you already tried, and alternatives "
                       "left. You may not retry a tried alternative -- pick a "
                       "remaining one, or ask the operator only if all remaining "
                       "ones need something external (a token, a payment).",
        "parameters": {"type": "object", "properties": {
            "wall": {"type": "string"},
            "cause": {"type": "string"},
            "tried": {"type": "array", "items": {"type": "string"}},
            "remaining": {"type": "array", "items": {"type": "string"}}},
            "required": ["wall", "cause", "remaining"]}}},
    {"type": "function", "function": {
        "name": "set_plan",
        "description": "State the plan for THIS task up front: the goal and its "
                       "ordered steps. For each step give a verifiable end "
                       "condition in `done_when` -- prefer `file:<path>` so the "
                       "harness can tick it for you -- and, if you foresee one, "
                       "the walls that could block it. Call again to revise: the "
                       "last plan wins. The plan stays pinned in your carnet.",
        "parameters": {"type": "object", "properties": {
            "goal": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "step": {"type": "string"},
                "done_when": {"type": "string"},
                "walls": {"type": "array", "items": {"type": "string"}}},
                "required": ["step"]}}},
            "required": ["goal", "steps"]}}},
    {"type": "function", "function": {
        "name": "step_done",
        "description": "Mark a plan step finished, by its 1-based number, with the "
                       "evidence. If its `done_when` names a file that is not "
                       "there, the claim is refused and the step stays open -- do "
                       "the work, do not just say it is done.",
        "parameters": {"type": "object", "properties": {
            "step": {"type": "integer"},
            "evidence": {"type": "string"}},
            "required": ["step"]}}},
    {"type": "function", "function": {
        "name": "ask_operator",
        "description": "Ask the operator a decision that is THEIRS to make -- a "
                       "trade-off you cannot settle from the task (e.g. 'VAE "
                       "fp16 12GB or fp8 6GB?'). Use this ONLY for a genuine "
                       "operator choice: for everything else decide yourself and "
                       "state your assumption. This ends your turn until they "
                       "answer.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}}},
            "required": ["question"]}}},
    {"type": "function", "function": {
        "name": "request_resource",
        "description": "Request one external resource the operator alone can "
                       "provide (an API token, a payment, a credential). Legit "
                       "ONLY after you have exhausted every alternative and "
                       "logged the wall with log_wall (empty `remaining`): this "
                       "is not giving up, it is isolating the single external "
                       "input. Ask for exactly one thing, precisely. Ends your "
                       "turn until the operator provides it.",
        "parameters": {"type": "object", "properties": {
            "resource": {"type": "string"},
            "why": {"type": "string"}},
            "required": ["resource"]}}},
]
CARNET_TOOL_NAMES = {t["function"]["name"] for t in CARNET_TOOLS}

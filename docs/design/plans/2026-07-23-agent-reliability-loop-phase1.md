# Agent Reliability Loop — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status: DONE 2026-07-24** — 8 tasks shipped on `main` (`7c541d85` → `5c49a31d`), suite green under 3.9 and 3.12 (788 tests). Deviations from the plan as written, all deliberate:

- **Task 4** — the nudge is a separate `system` message, not a suffix on the tool output: gluing it on corrupted `workspace_memory.detail_of` and `carnet.project`, which read that string back. `_annotate_failure` therefore does not exist. `_handle_carnet_tool` returns `(tool_name, content)` and lets `_run_call` do the single append — appending itself desynced `from_session`'s positional pairing and dropped the next failure.
- **Task 5** — `_carnet_tail(session) -> list` instead of `_wire_with_carnet`: the slice is appended *after* the compaction decision, because `compaction_span` returns indexes into the persisted transcript and a phantom trailing message would shift `covers_until` by one on every compaction.
- **Task 7** — `update_carnet(text, walls, notes)` + `parse_walls` / `parse_notes` over a second `factory:carnet` block, rather than keyword args on `update`. Merged, not appended: the projection recounts the whole transcript every turn, and a second session must not erase the first one's walls.
- **Task 8** — the replay did its job and falsified two assumptions: `find_vae` does not collapse to one target (writing the script and running it are different targets, correctly), and `cd <ws> && <cmd>` collapsed 12 unrelated commands onto `cmd:cd`. `target_of` now keys on the first non-`cd` segment, and `render` ranks worst-first and drops whole lines.

**Goal:** Give the local agent a live working memory (carnet) that is always visible, tracks every action by target, records failures and walls, and forces a pivot on failure — with no hard blocks.

**Architecture:** The carnet is a **pure projection of the session transcript** (like the existing `workspace_memory.from_session`): computed fresh each turn, never a separate store. `remember` / `log_wall` append tool-role messages to the transcript, so notes and walls survive refresh and crashes for free. The rendered hot slice is injected at the **tail** of the wire each turn (never the system head, which is the KV prefix). MEMORY.md stays the cross-session promotion, unchanged, at turn end.

**Tech Stack:** Python 3.9 (`py -3.9`), stdlib only, pytest. Frontend untouched.

## Global Constraints

- Tests run under `py -3.9` (standard; hooks/studio). Suite must also stay green under 3.12.
- **KV prefix is byte-stable:** nothing that changes per turn may enter `system_msgs`. The hot slice and any live block go at the END of `wire`. A test must assert the system head is unchanged when the carnet changes.
- Tools return **strings**, never exceptions (`agent_tools` convention): failures are part of the string so the loop keeps going.
- **No hard blocks:** the harness never refuses a tool call for being a repeat. It annotates and lets the call proceed.
- Pure modules, no I/O in the projection layer (`carnet.py`): `factory_mcp` owns session state and file writes.
- Commits: conventional, imperative, ASCII, no AI attribution. Direct commits on `main` are the project convention for these sessions. `git commit -F -` via heredoc (never backticks in `-m`).

---

### Task 1: Target normalization (`carnet.target_of`)

Collapses different-looking actions onto the same "target" so a sprawl of renamed scripts and repeated listings reads as one loop.

**Files:**
- Create: `harness/carnet.py`
- Test: `harness/tests/test_carnet.py`

**Interfaces:**
- Produces: `target_of(name: str, args: dict) -> str` — a stable, human-readable target key for one tool call.

Normalization rules (mechanical only in Phase 1, per spec B.2 — measure before adding intent-based):
- `run_command`: `"cmd:" + <first shell word> + ":" + <first path-like arg or "">`. E.g. `python scripts/find_vae.py` → `cmd:python:scripts/find_vae.py`; `dir models\ /s` → `cmd:dir:models`.
- `read_file` / `write_file` / `edit_file` / `list_dir`: `"<name>:" + <normalized path>` (forward slashes, stripped quotes).
- `search`: `"search:" + <pattern>`.
- anything else: `name`.

- [x] **Step 1: Write the failing test**

```python
# harness/tests/test_carnet.py
import carnet

def test_target_collapses_command_by_script():
    a = carnet.target_of("run_command", {"command": "python scripts/find_vae.py --x"})
    b = carnet.target_of("run_command", {"command": "python  scripts/find_vae.py"})
    assert a == b == "cmd:python:scripts/find_vae.py"

def test_target_dir_listing_ignores_flags():
    t = carnet.target_of("run_command", {"command": r"dir models\ /s /b"})
    assert t == "cmd:dir:models"

def test_target_file_tools_key_on_path():
    assert carnet.target_of("read_file", {"path": "a/b.txt"}) == "read_file:a/b.txt"
    assert carnet.target_of("edit_file", {"path": r".\a\b.txt"}) == "edit_file:a/b.txt"
```

- [x] **Step 2: Run test to verify it fails**

Run: `py -3.9 -m pytest harness/tests/test_carnet.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'carnet'`)

- [x] **Step 3: Write minimal implementation**

```python
# harness/carnet.py
"""The agent's live working memory for one session.

A pure projection of the transcript -- never a separate store. Computed fresh
each turn, like workspace_memory.from_session, so it always reflects what just
happened and survives a refresh. remember/log_wall write tool-role messages
into the transcript; this module reads them back.
"""
import json
import re

_PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]*[/\\][A-Za-z0-9_./\\-]*|\S+\.\w+")


def _norm_path(p):
    return str(p or "").strip().strip('"').replace("\\", "/").lstrip("./")


def target_of(name, args):
    args = args or {}
    if name == "run_command":
        cmd = " ".join(str(args.get("command") or "").split())
        parts = cmd.split()
        if not parts:
            return "cmd:"
        head = parts[0]
        arg = ""
        for tok in parts[1:]:
            if tok.startswith("-") or tok.startswith("/"):
                if "/" in tok[1:] or "\\" in tok:
                    pass
                else:
                    continue
            if re.search(r"[\\/]", tok) or "." in tok:
                arg = _norm_path(tok)
                break
        return "cmd:{}:{}".format(head, arg)
    if name in ("read_file", "write_file", "edit_file", "list_dir"):
        return "{}:{}".format(name, _norm_path(args.get("path")))
    if name == "search":
        return "search:{}".format(args.get("pattern") or "")
    return name
```

- [x] **Step 4: Run test to verify it passes**

Run: `py -3.9 -m pytest harness/tests/test_carnet.py -v`
Expected: PASS (3 passed)

Note: `dir models\ /s /b` must yield `cmd:dir:models`. `/s` and `/b` are flags (no separator) → skipped; `models\` has a separator → taken. Verify the test passes; if `/s` is mis-taken, tighten the flag check.

- [x] **Step 5: Commit**

```bash
git add harness/carnet.py harness/tests/test_carnet.py
git commit -F - <<'EOF'
feat(carnet): target normalization for the live working memory
EOF
```

---

### Task 2: Carnet projection + hot-slice render (`carnet.project`, `carnet.render`)

Walks the transcript into a per-target summary plus notes and walls, and renders the always-visible hot slice.

**Files:**
- Modify: `harness/carnet.py`
- Test: `harness/tests/test_carnet.py`

**Interfaces:**
- Consumes: `target_of` (Task 1); `workspace_memory.failed(output)` (existing, `harness/workspace_memory.py:67`) to decide if a tool result is a failure.
- Produces:
  - `project(messages: list) -> dict` with keys `targets` (`{target: {"n": int, "fail": int, "last": str}}` in first-seen order), `notes` (`list[str]`), `walls` (`list[dict]`).
  - `render(carnet: dict) -> str` — the hot slice, or `""` when there is nothing worth showing. Bounded to `HOT_BUDGET` chars.
  - Message conventions: a note is `{"role": "tool", "tool_name": "note", "content": <text>}`; a wall is `{"role": "tool", "tool_name": "wall", "content": <json>}`.

- [x] **Step 1: Write the failing test**

```python
# add to harness/tests/test_carnet.py
def _assistant(name, args):
    return {"role": "assistant",
            "tool_calls": [{"function": {"name": name,
                                         "arguments": args}}]}

def _tool(name, content):
    return {"role": "tool", "tool_name": name, "content": content}

def test_project_collapses_repeated_failing_target():
    msgs = []
    for _ in range(5):
        msgs.append(_assistant("run_command", {"command": "python scripts/find_vae.py"}))
        msgs.append(_tool("run_command", "exit 1\nHTTPError: 403 gated"))
    c = carnet.project(msgs)
    t = c["targets"]["cmd:python:scripts/find_vae.py"]
    assert t["n"] == 5 and t["fail"] == 5
    assert "403" in t["last"]

def test_project_reads_notes_and_walls():
    msgs = [_tool("note", "VAE gated -> need HF token"),
            _tool("wall", '{"wall": "VAE gated", "cause": "auth",'
                          ' "tried": ["official"], "remaining": ["mirror"]}')]
    c = carnet.project(msgs)
    assert c["notes"] == ["VAE gated -> need HF token"]
    assert c["walls"][0]["wall"] == "VAE gated"

def test_render_empty_is_blank():
    assert carnet.render(carnet.project([])) == ""

def test_render_flags_repeated_target():
    msgs = []
    for _ in range(3):
        msgs.append(_assistant("run_command", {"command": "dir models"}))
        msgs.append(_tool("run_command", "exit 1\nnot found"))
    out = carnet.render(carnet.project(msgs))
    assert "cmd:dir:models" in out
    assert "3" in out  # attempt count surfaced
```

- [x] **Step 2: Run test to verify it fails**

Run: `py -3.9 -m pytest harness/tests/test_carnet.py -v`
Expected: FAIL (`AttributeError: module 'carnet' has no attribute 'project'`)

- [x] **Step 3: Write minimal implementation**

```python
# add to harness/carnet.py
import workspace_memory

HOT_BUDGET = 1200
HEADER = "--- Carnet (live) ---"
FOOTER = "--- fin carnet ---"


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
        if name == "system":
            continue
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
    return {"targets": targets, "notes": notes, "walls": walls}


def _repeated(targets):
    # A target worth showing: tried more than once, or failed at all.
    return [(k, v) for k, v in targets.items() if v["n"] > 1 or v["fail"]]


def render(carnet):
    rows = _repeated(carnet["targets"])
    walls = [w for w in carnet["walls"] if w.get("remaining") is not None]
    notes = carnet["notes"][-5:]
    if not rows and not walls and not notes:
        return ""
    lines = [HEADER]
    for k, v in rows:
        state = "echec {}x: {}".format(v["fail"], v["last"]) if v["fail"] \
            else "{}x".format(v["n"])
        lines.append("- {} -> {}".format(k, state))
    for w in walls:
        lines.append("- MUR {}: reste {}".format(
            w.get("wall", ""), w.get("remaining", [])))
    for n in notes:
        lines.append("- note: {}".format(n))
    lines.append(FOOTER)
    out = "\n".join(lines)
    return out[:HOT_BUDGET]
```

- [x] **Step 4: Run test to verify it passes**

Run: `py -3.9 -m pytest harness/tests/test_carnet.py -v`
Expected: PASS (7 passed)

- [x] **Step 5: Commit**

```bash
git add harness/carnet.py harness/tests/test_carnet.py
git commit -F - <<'EOF'
feat(carnet): transcript projection and live hot-slice render
EOF
```

---

### Task 3: Carnet tool schemas exposed to the model

`remember`, `recall`, `log_wall` must appear in the agent's tool list (base toolset, all sessions) as read-tools (no approval gate). Schemas live in `carnet.py`; handlers come in Task 4.

**Files:**
- Modify: `harness/carnet.py` (add `CARNET_TOOLS`, `CARNET_TOOL_NAMES`)
- Modify: `harness/factory_mcp.py` — `_session_tools` (`factory_mcp.py:509`) appends `CARNET_TOOLS`
- Test: `harness/tests/test_carnet.py`, `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Produces: `carnet.CARNET_TOOLS: list` (OpenAI tool schema dicts), `carnet.CARNET_TOOL_NAMES: set`.
- Consumes in `_session_tools`: the existing native+MCP tool list it already builds.

- [x] **Step 1: Write the failing test**

```python
# add to harness/tests/test_carnet.py
def test_carnet_tools_are_read_tools_named():
    names = {t["function"]["name"] for t in carnet.CARNET_TOOLS}
    assert names == {"remember", "recall", "log_wall"} == carnet.CARNET_TOOL_NAMES
```

- [x] **Step 2: Run test to verify it fails**

Run: `py -3.9 -m pytest harness/tests/test_carnet.py::test_carnet_tools_are_read_tools_named -v`
Expected: FAIL (`AttributeError: ... 'CARNET_TOOLS'`)

- [x] **Step 3: Write minimal implementation**

```python
# add to harness/carnet.py
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
]
CARNET_TOOL_NAMES = {t["function"]["name"] for t in CARNET_TOOLS}
```

Then in `harness/factory_mcp.py`, `_session_tools` (append before returning the list):

```python
        tools = tools + carnet.CARNET_TOOLS   # exposed in every toolset
        return tools
```

(Verify the exact return site at `factory_mcp.py:509-518`; add `import carnet` at the top with the other harness imports.)

- [x] **Step 4: Run tests**

Run: `py -3.9 -m pytest harness/tests/test_carnet.py harness/tests/test_factory_mcp.py -v`
Expected: PASS. If an existing `_session_tools` test asserts an exact tool count, update it to include the 3 carnet tools.

- [x] **Step 5: Commit**

```bash
git add harness/carnet.py harness/factory_mcp.py harness/tests/
git commit -F - <<'EOF'
feat(carnet): expose remember/recall/log_wall to the agent
EOF
```

---

### Task 4: Carnet tool handlers + first-failure annotation in `_run_call`

The three carnet tools are handled by the harness (they need session + workspace), not by `agent_tools.run`. On any failing tool result, the returned string is annotated to force a pivot.

**Files:**
- Modify: `harness/factory_mcp.py` — `_run_call` (`factory_mcp.py:1029`)
- Test: `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Consumes: `carnet.CARNET_TOOL_NAMES` (Task 3), `carnet.project` (Task 2), `workspace_memory.failed`/`detail_of`, `agent_tools.run`, `_read_memory`, `workspace_memory.render`.
- Produces: unchanged external behavior of `_run_call` except (a) carnet tool names are handled, (b) failing results carry a `REFLECT_ANNOTATION` suffix.

- [x] **Step 1: Write the failing test**

```python
# add to harness/tests/test_factory_mcp.py (follow existing fixtures in file)
def test_remember_appends_note_message(factory, session_id):
    factory._handle_carnet_tool(session_id, "remember", {"note": "VAE gated"}, WORKSPACE)
    msgs = factory.chats.get(session_id)["messages"]
    assert msgs[-1]["tool_name"] == "note" and msgs[-1]["content"] == "VAE gated"

def test_failing_result_is_annotated(factory):
    out = factory._annotate_failure("run_command", "exit 1\n403 gated")
    assert "403 gated" in out
    assert factory_mcp.REFLECT_ANNOTATION.split(".")[0] in out

def test_ok_result_not_annotated(factory):
    assert factory._annotate_failure("read_file", "1\thello") == "1\thello"
```

- [x] **Step 2: Run test to verify it fails**

Run: `py -3.9 -m pytest harness/tests/test_factory_mcp.py -k "remember or annotat" -v`
Expected: FAIL (`AttributeError: ... '_handle_carnet_tool'`)

- [x] **Step 3: Write minimal implementation**

Add module-level constant near the other agent prompts in `factory_mcp.py`:

```python
REFLECT_ANNOTATION = (
    "\n\n[carnet] Note pour toi: avant de retenter sur cette cible, dis en une "
    "ligne ton hypothese sur la cause et en quoi ta prochaine action differe de "
    "celle qui vient d'echouer. Si tu as tente plusieurs approches sur ce but, "
    "enregistre un log_wall.")
```

Add methods to the `Factory` class:

```python
    def _annotate_failure(self, name, output):
        if name in ("note", "wall") or not workspace_memory.failed(output):
            return output
        return output + REFLECT_ANNOTATION

    def _handle_carnet_tool(self, session_id, name, args, workspace):
        args = args or {}
        if name == "remember":
            note = str(args.get("note") or "").strip()
            if not note:
                return "error: remember needs a note"
            self.chats.append(session_id, {
                "role": "tool", "tool_name": "note",
                "content": note, "ts": time.time()})
            return "noted"
        if name == "log_wall":
            wall = {"wall": args.get("wall", ""), "cause": args.get("cause", ""),
                    "tried": list(args.get("tried") or []),
                    "remaining": list(args.get("remaining") or [])}
            self.chats.append(session_id, {
                "role": "tool", "tool_name": "wall",
                "content": json.dumps(wall), "ts": time.time()})
            return "wall logged; pick a remaining alternative"
        if name == "recall":
            text = _read_memory(workspace)
            q = str(args.get("query") or "").strip().lower()
            if q:
                text = "\n".join(l for l in text.splitlines() if q in l.lower())
            return text or "(memory empty)"
        return "error: unknown carnet tool: {}".format(name)
```

Wire into `_run_call`: at the top, when `call["name"] in carnet.CARNET_TOOL_NAMES`, return the result of `_handle_carnet_tool` instead of delegating to `agent_tools.run`; and wherever `_run_call` currently obtains the tool `output` from `agent_tools.run`, wrap it: `output = self._annotate_failure(call["name"], output)` before it is appended as the tool message and before the guard sees it. Read `factory_mcp.py:1029-1074` to place these precisely; keep the existing approval-gate and loop-guard flow intact. Note the carnet tools are read-tools: add their names to the read set so `approve` mode never gates them (`agent_tools.READ_TOOLS` or the `_needs_approval` path — verify which `_needs_approval` consults and extend it to treat `CARNET_TOOL_NAMES` as read).

- [x] **Step 4: Run tests**

Run: `py -3.9 -m pytest harness/tests/test_factory_mcp.py -k "remember or annotat or carnet" -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -F - <<'EOF'
feat(carnet): handle remember/recall/log_wall and annotate failures
EOF
```

---

### Task 5: Inject the hot slice at the wire tail (KV-safe)

The carnet hot slice is appended to `wire` as the last message before `build_prompt`, so the model always sees its recent history — without touching the system head.

**Files:**
- Modify: `harness/factory_mcp.py` — `_agent_turn` (`factory_mcp.py:1110-1134`)
- Test: `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Consumes: `carnet.project` (Task 2), `carnet.render` (Task 2), the `wire` list built at `factory_mcp.py:1111-1117`.
- Produces: no signature change. `wire` gains one transient trailing message when the hot slice is non-empty; it is NEVER persisted to `session["messages"]`.

- [x] **Step 1: Write the failing test**

```python
# add to harness/tests/test_factory_mcp.py
def test_hot_slice_is_appended_to_wire_not_persisted(factory, session_id):
    # seed a repeated failing target into the transcript
    for _ in range(2):
        factory.chats.append(session_id, {"role": "assistant",
            "tool_calls": [{"function": {"name": "run_command",
                                         "arguments": {"command": "dir models"}}}]})
        factory.chats.append(session_id, {"role": "tool", "tool_name": "run_command",
                                          "content": "exit 1\nnot found"})
    session = factory.chats.get(session_id)
    wire = factory._wire_with_carnet(session)   # extracted helper (below)
    assert wire[-1]["content"].startswith(carnet.HEADER) or carnet.HEADER in wire[-1]["content"]
    # transcript itself is untouched
    assert all(m.get("tool_name") not in ("carnet",) for m in session["messages"])

def test_system_head_stable_across_carnet_change(factory, session_id):
    head1 = factory._agent_system_head(factory.chats.get(session_id))
    factory.chats.append(session_id, {"role": "tool", "tool_name": "note",
                                      "content": "x"})
    head2 = factory._agent_system_head(factory.chats.get(session_id))
    assert head1 == head2   # notes/carnet never enter the KV-prefixed head
```

- [x] **Step 2: Run test to verify it fails**

Run: `py -3.9 -m pytest harness/tests/test_factory_mcp.py -k "hot_slice or head_stable" -v`
Expected: FAIL (`AttributeError: ... '_wire_with_carnet'`)

- [x] **Step 3: Write minimal implementation**

Extract the wire build (currently inline at `factory_mcp.py:1110-1117`) into a helper and append the hot slice:

```python
    def _wire_with_carnet(self, session):
        wire = []
        for m in session["messages"]:
            msg = {"role": m["role"], "content": m["content"]}
            if m.get("tool_calls"):
                msg["tool_calls"] = m["tool_calls"]
            if m.get("tool_name"):
                msg["tool_name"] = m["tool_name"]
            wire.append(msg)
        hot = carnet.render(carnet.project(session["messages"]))
        if hot:
            wire.append({"role": "system", "content": hot})
        return wire
```

Replace the inline wire build in `_agent_turn` with `wire = self._wire_with_carnet(session)`. Add a thin `_agent_system_head(session)` that returns exactly the `system_msgs` content string built at `factory_mcp.py:1101-1109` (agent_sys + lesson + memory blocks) so the stability test has a single source; use it inside `_agent_turn` too. Confirm the carnet tail message rides in `wire`, which `build_prompt` places AFTER the system head and summary — read `chat_context.build_prompt` to confirm ordering; if it reorders roles, inject the hot slice as a trailing `user`/`tool` message instead so it lands last.

- [x] **Step 4: Run tests**

Run: `py -3.9 -m pytest harness/tests/test_factory_mcp.py -k "hot_slice or head_stable" -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -F - <<'EOF'
feat(carnet): inject the live hot slice at the wire tail, head stays byte-stable
EOF
```

---

### Task 6: Retire the hard `_LoopGuard` stop; keep the carnet as the signal

The design forbids hard blocks. The `_LoopGuard` "stop" at 5 killed the turn; remove that behavior. The carnet (Task 5) + failure annotation (Task 4) are now the loop signal. The Stop button and the optional `agent_max_iterations` cap remain the only runaway brakes.

**Files:**
- Modify: `harness/factory_mcp.py` — `_LoopGuard` (`factory_mcp.py:193-209`), `_drain_calls` (`factory_mcp.py:1049-1073`), `_agent_turn` loop-status handling (`factory_mcp.py:1086-1091`)
- Test: `harness/tests/test_factory_mcp.py` (`test_factory_mcp.py:1562` and the loop-guard tests)

**Interfaces:**
- Consumes: nothing new.
- Produces: `_drain_calls` no longer returns `"loop"`; identical-repeat detection no longer ends the turn. `_LoopGuard` is deleted (its job is subsumed by the carnet).

- [x] **Step 1: Update the failing test**

Replace the existing loop-guard stop test with one asserting no hard stop and that the carnet surfaces the repeat instead:

```python
def test_identical_repeats_do_not_hard_stop(factory, session_id):
    # three identical failing calls in one turn must NOT end the turn as "loop"
    for _ in range(3):
        factory.chats.append(session_id, {"role": "assistant",
            "tool_calls": [{"function": {"name": "run_command",
                                         "arguments": {"command": "dir models"}}}]})
        factory.chats.append(session_id, {"role": "tool", "tool_name": "run_command",
                                          "content": "exit 1\nnot found"})
    c = carnet.project(factory.chats.get(session_id)["messages"])
    assert c["targets"]["cmd:dir:models"]["n"] == 3   # visible, not blocked
```

Remove/replace `harness/tests/test_factory_mcp.py:1562` (`_LoopGuard` unit test).

- [x] **Step 2: Run test to verify current behavior conflicts**

Run: `py -3.9 -m pytest harness/tests/test_factory_mcp.py -k "loop or repeat" -v`
Expected: the old loop-guard test still asserting a hard stop FAILS or is now removed.

- [x] **Step 3: Implement**

Delete class `_LoopGuard` (`factory_mcp.py:193-209`) and the constants `LOOP_NUDGE_AT`, `LOOP_STOP_AT`, `LOOP_NUDGE`, `LOOP_STOPPED` if unused elsewhere (grep first). In `_drain_calls`, drop the `guard` parameter and the `verdict == "stop"` branch (`factory_mcp.py:1064-1073`) — just run each queued call. In `_agent_turn`, remove `guard = _LoopGuard()` and the `status == "loop"` branch (`factory_mcp.py:1080,1089-1091`). Keep `agent_max_iterations` and the Stop path exactly as they are.

- [x] **Step 4: Run the full suite**

Run: `py -3.9 -m pytest harness/tests/ -q`
Expected: PASS (adjust any test still referencing the removed guard/constants).

- [x] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -F - <<'EOF'
refactor(agent): retire the hard loop-stop; carnet is the loop signal
EOF
```

---

### Task 7: Persist walls to MEMORY.md at turn end (cross-session durability)

Failures already promote to MEMORY.md via `_remember`. Walls are semantic and should persist too, so a wall hit in one session warns the next. Notes already persist because `remember` will also append to MEMORY.md's managed block through the same path.

**Files:**
- Modify: `harness/workspace_memory.py` (add wall + note lines to the managed block, round-trippable)
- Modify: `harness/factory_mcp.py` — `_remember` (`factory_mcp.py:474-494`) to include walls/notes from the transcript projection
- Test: `harness/tests/test_workspace_memory.py`, `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Consumes: `carnet.project` (Task 2).
- Produces: `workspace_memory.parse`/`update` also round-trip `note`/`wall` lines; `_remember` writes them.

- [x] **Step 1: Write the failing test**

```python
# add to harness/tests/test_workspace_memory.py
import workspace_memory as wm

def test_wall_line_round_trips():
    text = wm.update("# MEMORY\n", [], walls=[{"wall": "VAE gated",
        "cause": "auth", "remaining": ["mirror"]}])
    assert "MUR VAE gated" in text
    back = wm.parse_walls(text)
    assert back[0]["wall"] == "VAE gated"
```

(If extending `update`/`parse` with keyword args breaks their pure signature contract, add sibling functions `render_walls`/`parse_walls` and a second managed sub-block `<!-- factory:walls -->` instead — keep the failed-command block byte-identical to today.)

- [x] **Step 2: Run test to verify it fails**

Run: `py -3.9 -m pytest harness/tests/test_workspace_memory.py -k wall -v`
Expected: FAIL

- [x] **Step 3: Implement**

Add a `<!-- factory:walls -->` managed sub-block with `render_walls(walls) -> str` and `parse_walls(text) -> list` mirroring the existing `_ENTRY_RE`/`update` pattern (deterministic, bounded, human-readable line `- MUR <wall> (<cause>): reste <remaining>`). In `_remember`, after the existing failed-command promotion, project the session and write `walls` (and any `note` lines) through the new sub-block. Keep the failed-command block unchanged so existing tests and the byte-stable head hold.

- [x] **Step 4: Run tests**

Run: `py -3.9 -m pytest harness/tests/test_workspace_memory.py harness/tests/test_factory_mcp.py -q`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add harness/workspace_memory.py harness/factory_mcp.py harness/tests/
git commit -F - <<'EOF'
feat(memory): persist walls and notes to MEMORY.md across sessions
EOF
```

---

### Task 8: Offline replay guardrail on `c_d2a43fd5`

Prove the whole Phase-1 loop on the real session that motivated it, with no GPU — the same offline-replay method used on 2026-07-22.

**Files:**
- Create: `harness/tests/test_carnet_replay.py`
- Uses: `jobs/chats/c_d2a43fd5.json` (in-repo)

**Interfaces:**
- Consumes: `carnet.project`, `carnet.render`.

- [x] **Step 1: Write the test**

```python
# harness/tests/test_carnet_replay.py
import json, os, carnet

def _load():
    p = os.path.join(os.path.dirname(__file__), "..", "..",
                     "jobs", "chats", "c_d2a43fd5.json")
    return json.load(open(p, encoding="utf-8"))["messages"]

def test_replay_collapses_find_vae_loop():
    c = carnet.project(_load())
    # find_vae.py was run 5x back-to-back; the carnet shows ONE target
    hits = [k for k in c["targets"] if "find_vae" in k]
    assert len(hits) == 1
    assert c["targets"][hits[0]]["n"] >= 5

def test_replay_hot_slice_nonempty_and_bounded():
    out = carnet.render(carnet.project(_load()))
    assert out and len(out) <= carnet.HOT_BUDGET
```

- [x] **Step 2: Run**

Run: `py -3.9 -m pytest harness/tests/test_carnet_replay.py -v`
Expected: PASS (if `find_vae` collapses to 2 targets because a flag varied, tighten `target_of` per Task 1's note, then re-run).

- [x] **Step 3: Full suite**

Run: `py -3.9 -m pytest harness/tests/ -q`
Expected: PASS.

- [x] **Step 4: Commit**

```bash
git add harness/tests/test_carnet_replay.py
git commit -F - <<'EOF'
test(carnet): offline replay proves the loop collapses on c_d2a43fd5
EOF
```

- [x] **Step 5: Push** (detached, the pre-push hook runs the suite ~2-3 min)

```bash
git push
```

---

## Self-Review

**Spec coverage (Phase 1 = B + C.1 + C.3 + target collapse):**
- B.1 store / B.2 target collapse → Tasks 1, 2, 7. B.3 injection (tail, KV-safe) → Task 5. B.4 tools → Tasks 3, 4. B.5 persistence → Task 7.
- C.1 first-failure reflection → Task 4. C.3 wall log → Tasks 3, 4, 7. Target-collapse anti-loop → Tasks 2, 6. No-hard-block → Task 6.
- **Deferred to the A plan (needs the plan object):** C.2 stagnation detection, A (plan/checkpoints), C.4 escalation accroche, E (autonomy contract). Called out in the spec's implementation order.

**Placeholders:** none — every code step carries runnable code. The two "verify the exact site" notes (Tasks 3, 4, 5) point at exact line ranges already read; they are placement instructions, not missing content.

**Type consistency:** `target_of(name, args) -> str`, `project(messages) -> {"targets","notes","walls"}`, `render(carnet) -> str`, `CARNET_TOOL_NAMES: set`, `_handle_carnet_tool(session_id, name, args, workspace) -> str`, `_annotate_failure(name, output) -> str`, `_wire_with_carnet(session) -> list` — used consistently across tasks.

## Notes for the executor

- Read the exact insertion sites before editing (line numbers cited are from 2026-07-23 and may drift): `_session_tools` ~509, `_run_call` ~1029, `_agent_turn` wire build ~1110, `_remember` ~474.
- `import carnet` alongside the other harness imports in `factory_mcp.py`.
- If `chat_context.build_prompt` reorders/merges system messages, inject the hot slice as a trailing `tool`/`user` message so it lands last (Task 5, Step 3).

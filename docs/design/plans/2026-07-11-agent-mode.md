# Agent Mode in the Chat Tab — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A claude-code-like agent in the Studio chat tab: native Ollama tool calling, read/write/shell tools confined to a workspace, per-session approve/auto mode, streamed NDJSON events, approval flow across separate HTTP requests.

**Architecture:** New `agent_tools.py` module (tool schemas + sandboxed executors). `factory_mcp.Factory` gains `agent_reply` / `chat_approve` generators sharing the GpuLock pattern of `chat_reply`; the tool loop re-prompts Ollama with tool results until a final answer, an approval gate (stream ends, `pending_calls` parked in the session), or an iteration cap. `factory_web` streams the new event kinds and adds `/mode` + `/approve` routes. The chat UI renders tool chips, diffs and an inline approval panel.

**Tech Stack:** Python 3.9 stdlib only (project rule), Ollama `/api/chat` with `tools`, vanilla JS UI.

**Spec:** `docs/design/specs/2026-07-11-agent-mode-design.md`

## Global Constraints

- Stdlib only, no new dependencies.
- Python 3.9 compatible (no `match`, no `X | Y` type syntax).
- Every path a tool touches resolves under the session workspace; escapes are refused as tool errors the model sees, never exceptions.
- Tool output truncated to 8000 chars with a `\n[truncated]` marker.
- Existing plain-chat sessions must keep working unchanged.
- GPU lock invariant: held while the model generates; released while waiting for operator approval.
- All tests run from `harness/`: `python -m pytest -q`.
- Conventional commits, no AI attribution.

---

### Task 1: `ollama_client.chat_stream` — `tools` parameter and tool_call deltas

**Files:**
- Modify: `harness/ollama_client.py:95-129` (`chat_stream`)
- Test: `harness/tests/test_ollama_client.py`

**Interfaces:**
- Produces: `chat_stream(model, messages, temperature=0.7, num_ctx=16384, timeout=900, tools=None)` — generator yielding `("thinking", str)`, `("content", str)`, and `("tool_call", {"name": str, "arguments": dict})`; returns metrics dict via StopIteration.value.

- [ ] **Step 1: Write the failing tests** (append to `test_ollama_client.py`)

```python
def test_chat_stream_forwards_tools_and_yields_tool_calls(monkeypatch):
    seen = {}

    def urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(
            b'{"message": {"content": "", "tool_calls": [{"function": '
            b'{"name": "read_file", "arguments": {"path": "a.py"}}}]}, '
            b'"done": true}\n')
    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", urlopen)
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    chunks, metrics = _drain(chat_stream("m", [], tools=tools))

    assert seen["body"]["tools"] == tools
    assert chunks == [("tool_call", {"name": "read_file",
                                     "arguments": {"path": "a.py"}})]


def test_chat_stream_omits_tools_key_when_not_given(monkeypatch):
    seen = {}

    def urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(b'{"message": {"content": "ok"}, "done": true}\n')
    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", urlopen)

    _drain(chat_stream("m", []))

    assert "tools" not in seen["body"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd harness && python -m pytest tests/test_ollama_client.py -q -k tools`
Expected: FAIL (`chat_stream() got an unexpected keyword argument 'tools'`)

- [ ] **Step 3: Implement**

In `chat_stream`, change the signature to
`def chat_stream(model, messages, temperature=0.7, num_ctx=16384, timeout=900, tools=None):`
after building `payload`, add:

```python
    if tools:
        payload["tools"] = tools
```

In the read loop, after the `thinking` block and before the `content` block, add:

```python
            for call in message.get("tool_calls") or []:
                fn = call.get("function", {})
                yield "tool_call", {"name": fn.get("name", ""),
                                    "arguments": fn.get("arguments") or {}}
```

- [ ] **Step 4: Run the file's tests**

Run: `cd harness && python -m pytest tests/test_ollama_client.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/ollama_client.py harness/tests/test_ollama_client.py
git commit -m "feat: chat_stream forwards tools and yields tool_call deltas"
```

---

### Task 2: `agent_tools.py` — read tools (list_dir, read_file, search)

**Files:**
- Create: `harness/agent_tools.py`
- Create: `harness/tests/test_agent_tools.py`

**Interfaces:**
- Produces: `run(name, arguments, workspace) -> str` (never raises for user-level failures), `needs_approval(name) -> bool`, `TOOLS` (list of Ollama function schemas), `MAX_OUTPUT = 8000`.

- [ ] **Step 1: Write the failing tests**

```python
"""Workspace-confined tools for the chat agent."""
import pytest

from agent_tools import needs_approval, run


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("K = 32\nname = 'elo'\n",
                                             encoding="utf-8")
    (tmp_path / "top.txt").write_text("hello\nworld\n", encoding="utf-8")
    return tmp_path


def test_list_dir_shows_entries(ws):
    out = run("list_dir", {"path": "."}, ws)
    assert "pkg/" in out and "top.txt" in out


def test_read_file_numbers_lines(ws):
    out = run("read_file", {"path": "pkg/mod.py"}, ws)
    assert "1\tK = 32" in out and "2\tname = 'elo'" in out


def test_read_file_offset_and_limit(ws):
    out = run("read_file", {"path": "top.txt", "offset": 2, "limit": 1}, ws)
    assert "2\tworld" in out and "hello" not in out


def test_search_finds_matches_across_files(ws):
    out = run("search", {"pattern": "K = \\d+"}, ws)
    assert "pkg/mod.py:1" in out and "K = 32" in out


def test_search_reports_zero_matches(ws):
    assert "no matches" in run("search", {"pattern": "zzz_nothing"}, ws)


def test_paths_may_not_escape_the_workspace(ws):
    out = run("read_file", {"path": "../outside.txt"}, ws)
    assert "error" in out.lower() and "workspace" in out


def test_absolute_paths_outside_are_refused(ws):
    out = run("list_dir", {"path": "C:\\Windows"}, ws)
    assert "error" in out.lower()


def test_missing_file_is_a_tool_error_not_an_exception(ws):
    out = run("read_file", {"path": "nope.py"}, ws)
    assert "error" in out.lower()


def test_unknown_tool_is_a_tool_error(ws):
    assert "error" in run("frobnicate", {}, ws).lower()


def test_read_tools_need_no_approval():
    for name in ("list_dir", "read_file", "search"):
        assert needs_approval(name) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `cd harness && python -m pytest tests/test_agent_tools.py -q`
Expected: FAIL (`ModuleNotFoundError: agent_tools`)

- [ ] **Step 3: Implement `harness/agent_tools.py`**

```python
"""Workspace-confined tools for the chat agent.

Every tool returns a string the model reads. Failures (missing file, escaped
path, bad regex) are part of that string, never exceptions: the loop must
keep going so the model can correct itself. Outputs are truncated: num_ctx
is 16k and a fat `git log` must not evict the conversation.
"""
import re
import subprocess
from pathlib import Path

MAX_OUTPUT = 8000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv"}
READ_TOOLS = {"list_dir", "read_file", "search"}

TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List entries of a directory (relative to the workspace).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "directory, '.' for the root"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file with line numbers.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "1-based first line"},
            "limit": {"type": "integer", "description": "max lines"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search",
        "description": "Regex search across workspace text files; returns path:line matches.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Python regex"},
            "glob": {"type": "string", "description": "filename filter like *.py"}},
            "required": ["pattern"]}}},
]


def needs_approval(name):
    return name not in READ_TOOLS


def _truncate(text):
    if len(text) > MAX_OUTPUT:
        return text[:MAX_OUTPUT] + "\n[truncated]"
    return text


def _resolve(workspace, path):
    """Path under the workspace root, or None when it escapes."""
    root = Path(workspace).resolve()
    p = Path(path)
    p = (p if p.is_absolute() else root / p).resolve()
    if p == root or root in p.parents:
        return p
    return None


def _list_dir(workspace, path="."):
    p = _resolve(workspace, path)
    if p is None:
        return "error: path escapes the workspace"
    if not p.is_dir():
        return "error: not a directory: {}".format(path)
    lines = []
    for child in sorted(p.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
        if child.is_dir():
            lines.append(child.name + "/")
        else:
            lines.append("{}  ({} bytes)".format(child.name, child.stat().st_size))
    return "\n".join(lines) or "(empty)"


def _read_file(workspace, path, offset=1, limit=2000):
    p = _resolve(workspace, path)
    if p is None:
        return "error: path escapes the workspace"
    if not p.is_file():
        return "error: no such file: {}".format(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = max(1, int(offset or 1))
    limit = max(1, int(limit or 2000))
    window = lines[offset - 1:offset - 1 + limit]
    return "\n".join("{}\t{}".format(offset + i, l) for i, l in enumerate(window)) \
        or "(empty)"


def _search(workspace, pattern, glob="*"):
    root = Path(workspace).resolve()
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return "error: bad regex: {}".format(e)
    hits = []
    for p in sorted(root.rglob(glob or "*")):
        if not p.is_file() or set(p.relative_to(root).parts) & SKIP_DIRS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append("{}:{}: {}".format(
                    p.relative_to(root).as_posix(), i, line.strip()))
                if len(hits) >= 50:
                    return _truncate("\n".join(hits) + "\n[more matches elided]")
    return "\n".join(hits) or "no matches"


_HANDLERS = {"list_dir": _list_dir, "read_file": _read_file, "search": _search}


def run(name, arguments, workspace):
    fn = _HANDLERS.get(name)
    if fn is None:
        return "error: unknown tool: {}".format(name)
    try:
        return _truncate(fn(workspace, **dict(arguments or {})))
    except TypeError as e:
        return "error: bad arguments for {}: {}".format(name, e)
```

- [ ] **Step 4: Run the tests**

Run: `cd harness && python -m pytest tests/test_agent_tools.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/agent_tools.py harness/tests/test_agent_tools.py
git commit -m "feat: agent read tools (list_dir, read_file, search)"
```

---

### Task 3: `agent_tools.py` — write and shell tools

**Files:**
- Modify: `harness/agent_tools.py`
- Test: `harness/tests/test_agent_tools.py`

**Interfaces:**
- Produces: tools `write_file(path, content)`, `edit_file(path, old, new)`, `run_command(command, timeout?)` reachable through the same `run()`; `needs_approval` returns True for all three; `TOOLS` includes their schemas.

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_write_file_creates_parents(ws):
    out = run("write_file", {"path": "new/dir/f.txt", "content": "yo"}, ws)
    assert "wrote" in out
    assert (ws / "new" / "dir" / "f.txt").read_text(encoding="utf-8") == "yo"


def test_write_file_refuses_escape(ws):
    out = run("write_file", {"path": "../evil.txt", "content": "x"}, ws)
    assert "error" in out.lower()


def test_edit_file_replaces_exactly_one_match(ws):
    out = run("edit_file", {"path": "pkg/mod.py", "old": "K = 32",
                            "new": "K = 24"}, ws)
    assert "edited" in out
    assert "K = 24" in (ws / "pkg" / "mod.py").read_text(encoding="utf-8")


def test_edit_file_errors_on_zero_and_many_matches(ws):
    assert "0 matches" in run("edit_file", {"path": "pkg/mod.py",
                                            "old": "absent", "new": "x"}, ws)
    (ws / "dup.txt").write_text("a\na\n", encoding="utf-8")
    assert "2 matches" in run("edit_file", {"path": "dup.txt",
                                            "old": "a", "new": "b"}, ws)


def test_run_command_captures_exit_and_output(ws):
    out = run("run_command", {"command": "echo hello"}, ws)
    assert "exit 0" in out and "hello" in out


def test_run_command_nonzero_exit_is_reported_not_raised(ws):
    out = run("run_command", {"command": "exit 3"}, ws)
    assert "exit 3" in out


def test_run_command_timeout_is_a_tool_error(ws):
    out = run("run_command", {"command": "ping -n 30 127.0.0.1",
                              "timeout": 1}, ws)
    assert "timed out" in out


def test_long_output_is_truncated(ws):
    (ws / "big.txt").write_text("x" * 20000, encoding="utf-8")
    out = run("read_file", {"path": "big.txt"}, ws)
    assert len(out) <= 8100 and out.endswith("[truncated]")


def test_write_tools_need_approval():
    for name in ("write_file", "edit_file", "run_command"):
        assert needs_approval(name) is True
```

- [ ] **Step 2: Run to verify failure**

Run: `cd harness && python -m pytest tests/test_agent_tools.py -q`
Expected: new tests FAIL (`error: unknown tool: write_file` breaks the asserts)

- [ ] **Step 3: Implement** (append to `agent_tools.py`, extend `TOOLS` and `_HANDLERS`)

Append to the `TOOLS` list:

```python
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a text file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace one exact occurrence of `old` with `new` in a file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"},
            "new": {"type": "string"}},
            "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command in the workspace; returns exit code and output.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "seconds, default 60"}},
            "required": ["command"]}}},
```

New handlers (before `_HANDLERS`, then add the three entries to `_HANDLERS`):

```python
def _write_file(workspace, path, content):
    p = _resolve(workspace, path)
    if p is None:
        return "error: path escapes the workspace"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return "wrote {} bytes to {}".format(len(content.encode("utf-8")), path)


def _edit_file(workspace, path, old, new):
    p = _resolve(workspace, path)
    if p is None:
        return "error: path escapes the workspace"
    if not p.is_file():
        return "error: no such file: {}".format(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    n = text.count(old)
    if n != 1:
        return "error: {} matches for old text in {} (need exactly 1)".format(n, path)
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return "edited {}".format(path)


def _run_command(workspace, command, timeout=60):
    # shell=True is the point of this tool: the model composes a full shell
    # line (pipes, redirects) and the approve mode / operator gates it.
    try:
        proc = subprocess.run(command, shell=True, cwd=str(workspace),
                              capture_output=True, text=True,
                              timeout=int(timeout or 60))
    except subprocess.TimeoutExpired:
        return "error: command timed out after {}s".format(timeout)
    out = "exit {}\n{}".format(proc.returncode, proc.stdout or "")
    if proc.stderr:
        out += "\nstderr:\n" + proc.stderr
    return out


_HANDLERS = {"list_dir": _list_dir, "read_file": _read_file, "search": _search,
             "write_file": _write_file, "edit_file": _edit_file,
             "run_command": _run_command}
```

- [ ] **Step 4: Run the tests**

Run: `cd harness && python -m pytest tests/test_agent_tools.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/agent_tools.py harness/tests/test_agent_tools.py
git commit -m "feat: agent write and shell tools with approval flags"
```

---

### Task 4: `chat_store` — agent session fields

**Files:**
- Modify: `harness/chat_store.py`
- Test: `harness/tests/test_chat_store.py`

**Interfaces:**
- Produces: `create(model, kind=None, mode=None, workspace=None)` — agent sessions carry `kind`, `mode`, `workspace`, `pending_calls: []`; `set_mode(session_id, mode)`; `set_pending_calls(session_id, calls)`; `list()` rows include `"kind"`.

- [ ] **Step 1: Write the failing tests** (append to `test_chat_store.py`, reuse its existing store fixture — check its name at the top of the file and match it)

```python
def test_create_agent_session_carries_kind_mode_workspace(store):
    s = store.create("m:1", kind="agent", mode="approve", workspace="C:/ws")
    got = store.get(s["session_id"])
    assert got["kind"] == "agent" and got["mode"] == "approve"
    assert got["workspace"] == "C:/ws" and got["pending_calls"] == []
    assert store.list()[0]["kind"] == "agent"


def test_plain_session_has_no_agent_fields(store):
    s = store.create("m:1")
    got = store.get(s["session_id"])
    assert "mode" not in got and "workspace" not in got
    assert store.list()[0]["kind"] is None


def test_set_mode_and_pending_calls(store):
    sid = store.create("m:1", kind="agent", mode="approve", workspace=".")["session_id"]
    store.set_mode(sid, "auto")
    call = {"name": "write_file", "arguments": {"path": "a", "content": "b"}}
    store.set_pending_calls(sid, [call])
    got = store.get(sid)
    assert got["mode"] == "auto" and got["pending_calls"] == [call]
```

- [ ] **Step 2: Run to verify failure**

Run: `cd harness && python -m pytest tests/test_chat_store.py -q`
Expected: new tests FAIL (unexpected keyword `kind`)

- [ ] **Step 3: Implement** in `chat_store.py`

Replace `create` with:

```python
    def create(self, model, now=None, kind=None, mode=None, workspace=None):
        now = time.time() if now is None else now
        session = {"session_id": "c_" + uuid.uuid4().hex[:8], "title": "",
                   "model": model, "created_at": now, "updated_at": now,
                   "messages": []}
        if kind == "agent":
            session.update(kind="agent", mode=mode or "approve",
                           workspace=workspace, pending_calls=[])
        atomic_write_json(self._path(session["session_id"]), session)
        return session
```

After `set_model`, add:

```python
    def set_mode(self, session_id, mode, now=None):
        session = self.get(session_id)
        session["mode"] = mode
        return self._save(session, now)

    def set_pending_calls(self, session_id, calls, now=None):
        session = self.get(session_id)
        session["pending_calls"] = calls
        return self._save(session, now)
```

In `list()`, add `"kind": s.get("kind")` to the row dict.

- [ ] **Step 4: Run the tests**

Run: `cd harness && python -m pytest tests/test_chat_store.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/chat_store.py harness/tests/test_chat_store.py
git commit -m "feat: chat store carries agent session fields"
```

---

### Task 5: Facade — agent session creation and mode switch

**Files:**
- Modify: `harness/factory_mcp.py` (`chat_create`, new `chat_set_mode`)
- Test: `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Produces: `Factory.chat_create(model, kind=None, mode=None, workspace=None)` (workspace defaults to `os.getcwd()` for agent sessions); `Factory.chat_set_mode(session_id, mode)` validating mode in `{"approve", "auto"}`.

- [ ] **Step 1: Write the failing tests** (append to the chat section of `test_factory_mcp.py`)

```python
def test_chat_create_agent_defaults_workspace_to_cwd(factory):
    s = factory.chat_create("m:1", kind="agent", mode="auto")
    got = factory.chat_get(s["session_id"])
    assert got["kind"] == "agent" and got["mode"] == "auto"
    assert got["workspace"] == os.getcwd()


def test_chat_set_mode_validates(factory):
    sid = factory.chat_create("m:1", kind="agent", mode="approve")["session_id"]
    assert factory.chat_set_mode(sid, "auto")["mode"] == "auto"
    with pytest.raises(ToolError):
        factory.chat_set_mode(sid, "yolo")
```

Add `import os` to the test file imports if absent.

- [ ] **Step 2: Run to verify failure**

Run: `cd harness && python -m pytest tests/test_factory_mcp.py -q -k "agent or set_mode"`
Expected: FAIL

- [ ] **Step 3: Implement** in `factory_mcp.py` (add `import os` to the imports if absent)

Replace `chat_create` and add `chat_set_mode` after `chat_set_model`:

```python
    def chat_create(self, model, kind=None, mode=None, workspace=None):
        self._check_model(model)
        if kind not in (None, "agent"):
            raise ToolError("unknown session kind: {!r}".format(kind))
        if kind == "agent":
            if mode not in (None, "approve", "auto"):
                raise ToolError("mode must be approve or auto")
            return self.chats.create(model, kind="agent", mode=mode or "approve",
                                     workspace=workspace or os.getcwd())
        return self.chats.create(model)

    def chat_set_mode(self, session_id, mode):
        if mode not in ("approve", "auto"):
            raise ToolError("mode must be approve or auto")
        return self.chats.set_mode(session_id, mode)
```

- [ ] **Step 4: Run the tests**

Run: `cd harness && python -m pytest tests/test_factory_mcp.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -m "feat: facade creates agent sessions and switches mode"
```

---

### Task 6: Facade — the agent loop (`agent_reply`)

**Files:**
- Modify: `harness/factory_mcp.py` (new module constants + `Factory` methods)
- Test: `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Consumes: `agent_tools.TOOLS`, `agent_tools.run`, `agent_tools.needs_approval`; `ollama_client.chat_stream(..., tools=...)` from Task 1; store methods from Task 4.
- Produces: `Factory.agent_reply(session_id, content)` — generator: `("start", None)`, then `("thinking", str)` / `("chunk", str)` / `("tool_call", dict)` / `("tool_result", {"name", "output"})` / `("approval_needed", dict)`, terminal `("done", dict)` or `("error", str)`. Raises `ToolError` before the first yield when `pending_calls` is non-empty or the GPU is busy. Internal helpers `_agent_turn`, `_drain_calls`, `_run_call` reused by Task 7.

- [ ] **Step 1: Write the failing tests**

The fake needs to script several successive model turns. Append to `test_factory_mcp.py`:

```python
def _fake_agent_stream(turns):
    """Each turn: list of (kind, payload) tuples; content joined for metrics."""
    state = {"i": 0, "calls": []}

    def stream(model, messages, **kw):
        state["calls"].append({"messages": messages, "tools": kw.get("tools")})
        turn = turns[state["i"]]
        state["i"] += 1
        content = ""
        for kind, payload in turn:
            if kind == "content":
                content += payload
            yield kind, payload
        return {"content": content, "tokens_per_s": 30.0, "ttft_s": 1.0,
                "eval_count": 5}
    stream.state = state
    return stream


def _agent(factory, mode="auto", tmp=None):
    return factory.chat_create("m:1", kind="agent", mode=mode,
                               workspace=str(tmp))["session_id"]


def test_agent_reply_executes_tools_and_reprompts(factory, tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("salut\n", encoding="utf-8")
    fake = _fake_agent_stream([
        [("tool_call", {"name": "read_file", "arguments": {"path": "a.txt"}})],
        [("content", "Le fichier dit salut.")],
    ])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "lis a.txt"))

    kinds = [k for k, _ in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert "tool_call" in kinds and "tool_result" in kinds
    result = dict(events)["tool_result"]
    assert result["name"] == "read_file" and "salut" in result["output"]
    # the second model call saw the tool message and the tools list
    msgs = fake.state["calls"][1]["messages"]
    assert any(m["role"] == "tool" and "salut" in m["content"] for m in msgs)
    assert fake.state["calls"][0]["tools"] is factory_mcp.agent_tools.TOOLS
    # system prompt is first and mentions the workspace
    assert msgs[0]["role"] == "system" and str(tmp_path) in msgs[0]["content"]


def test_agent_reply_reads_instruction_file_into_system_prompt(
        factory, tmp_path, monkeypatch):
    (tmp_path / "AGENT.md").write_text("Toujours répondre en breton.",
                                       encoding="utf-8")
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    list(factory.agent_reply(sid, "salut"))

    system = fake.state["calls"][0]["messages"][0]["content"]
    assert "breton" in system


def test_agent_reply_parks_write_calls_in_approve_mode(factory, tmp_path,
                                                       monkeypatch):
    fake = _fake_agent_stream([
        [("tool_call", {"name": "write_file",
                        "arguments": {"path": "b.txt", "content": "x"}})],
    ])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)

    events = list(factory.agent_reply(sid, "écris b.txt"))

    assert events[-1][0] == "approval_needed"
    assert events[-1][1]["name"] == "write_file"
    assert not (tmp_path / "b.txt").exists()
    assert factory.chat_get(sid)["pending_calls"][0]["name"] == "write_file"
    # the GPU lock is free while the operator thinks
    lock = GpuLock(factory.jobs_root / ".gpu.lock")
    assert lock.acquire(timeout=0)
    lock.release()


def test_agent_reply_refuses_while_a_call_is_pending(factory, tmp_path,
                                                     monkeypatch):
    fake = _fake_agent_stream([
        [("tool_call", {"name": "run_command", "arguments": {"command": "x"}})],
    ])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)
    list(factory.agent_reply(sid, "vas-y"))

    with pytest.raises(ToolError):
        next(factory.agent_reply(sid, "encore"))


def test_agent_reply_read_calls_run_even_in_approve_mode(factory, tmp_path,
                                                         monkeypatch):
    (tmp_path / "a.txt").write_text("yo", encoding="utf-8")
    fake = _fake_agent_stream([
        [("tool_call", {"name": "read_file", "arguments": {"path": "a.txt"}})],
        [("content", "fini")],
    ])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)

    events = list(factory.agent_reply(sid, "lis"))

    assert events[-1][0] == "done"
    assert "approval_needed" not in [k for k, _ in events]


def test_agent_reply_stops_at_the_iteration_cap(factory, tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("yo", encoding="utf-8")
    turn = [("tool_call", {"name": "read_file", "arguments": {"path": "a.txt"}})]
    fake = _fake_agent_stream([turn] * (factory_mcp.MAX_TOOL_ITERATIONS + 1))
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "boucle"))

    assert events[-1] == ("done", {"stopped": "iteration_limit"})
    assert len(fake.state["calls"]) == factory_mcp.MAX_TOOL_ITERATIONS


def test_agent_reply_saves_partial_on_mid_stream_failure(factory, tmp_path,
                                                         monkeypatch):
    def broken(model, messages, **kw):
        yield "content", "déb"
        raise OSError("ollama vanished")
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream",
                        lambda *a, **k: broken(*a, **k))
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "salut"))

    assert events[-1][0] == "error"
    saved = factory.chat_get(sid)["messages"][-1]
    assert saved["content"] == "déb" and "ollama vanished" in saved["error"]
    lock = GpuLock(factory.jobs_root / ".gpu.lock")
    assert lock.acquire(timeout=0)
    lock.release()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd harness && python -m pytest tests/test_factory_mcp.py -q -k agent`
Expected: FAIL (`Factory` has no attribute `agent_reply` / `MAX_TOOL_ITERATIONS`)

- [ ] **Step 3: Implement** in `factory_mcp.py`

Imports: add `import agent_tools` next to `import ollama_client`.

Module constants (near `RUNNERS`):

```python
MAX_TOOL_ITERATIONS = 20
INSTRUCTION_FILES = ("AGENT.md", "agent.md", "CLAUDE.md")
AGENT_SYSTEM = """You are a coding agent working inside the workspace {workspace}.
Use the tools to inspect and modify the project; keep answers short and factual.
Call tools instead of guessing file contents. When the task is done, answer
the user directly without further tool calls. Reply in the user's language."""


def _agent_system(workspace):
    parts = [AGENT_SYSTEM.format(workspace=workspace)]
    for name in INSTRUCTION_FILES:
        p = Path(workspace) / name
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8", errors="replace")[:8000])
            break
    return "\n\n".join(parts)
```

`Factory` methods (after `chat_reply`):

```python
    def _run_call(self, session_id, call, workspace):
        yield "tool_call", call
        output = agent_tools.run(call["name"], call.get("arguments") or {},
                                 workspace)
        self.chats.append(session_id, {"role": "tool", "tool_name": call["name"],
                                       "content": output, "ts": time.time()})
        yield "tool_result", {"name": call["name"], "output": output}

    def _drain_calls(self, session_id):
        """Run queued calls; False when parked on an approval gate."""
        while True:
            session = self.chats.get(session_id)
            calls = session.get("pending_calls") or []
            if not calls:
                return True
            call = calls[0]
            if session["mode"] == "approve" and \
                    agent_tools.needs_approval(call["name"]):
                yield "approval_needed", call
                return False
            # pop before running: a crash must not replay a shell command
            self.chats.set_pending_calls(session_id, calls[1:])
            yield from self._run_call(session_id, call, session["workspace"])

    def _agent_turn(self, session_id):
        """Model turns + tool executions until a final answer, an approval
        gate, or the iteration cap. The caller owns the GPU lock."""
        for _ in range(MAX_TOOL_ITERATIONS):
            if not (yield from self._drain_calls(session_id)):
                return
            session = self.chats.get(session_id)
            history = [{"role": "system",
                        "content": _agent_system(session["workspace"])}]
            for m in session["messages"]:
                msg = {"role": m["role"], "content": m["content"]}
                if m.get("tool_calls"):
                    msg["tool_calls"] = m["tool_calls"]
                if m.get("tool_name"):
                    msg["tool_name"] = m["tool_name"]
                history.append(msg)
            stream = ollama_client.chat_stream(session["model"], history,
                                               tools=agent_tools.TOOLS)
            parts, thoughts, calls = [], [], []
            while True:
                try:
                    kind, payload = next(stream)
                except StopIteration as stop:
                    metrics = stop.value or {}
                    break
                except Exception as e:
                    msg = {"role": "assistant", "content": "".join(parts),
                           "ts": time.time(), "error": str(e)}
                    if thoughts:
                        msg["thinking"] = "".join(thoughts)
                    self.chats.append(session_id, msg)
                    yield "error", str(e)
                    return
                if kind == "thinking":
                    thoughts.append(payload)
                    yield "thinking", payload
                elif kind == "content":
                    parts.append(payload)
                    yield "chunk", payload
                else:
                    calls.append(payload)
            msg = {"role": "assistant", "content": "".join(parts),
                   "ts": time.time(),
                   "metrics": {k: metrics.get(k) for k in
                               ("tokens_per_s", "ttft_s", "eval_count")}}
            if thoughts:
                msg["thinking"] = "".join(thoughts)
            if calls:
                msg["tool_calls"] = [{"function": c} for c in calls]
            self.chats.append(session_id, msg)
            if not calls:
                yield "done", {"tokens_per_s": metrics.get("tokens_per_s"),
                               "ttft_s": metrics.get("ttft_s"),
                               "eval_count": metrics.get("eval_count")}
                return
            self.chats.set_pending_calls(session_id, calls)
        self.chats.append(session_id, {
            "role": "tool", "tool_name": "system",
            "content": "tool iteration limit reached", "ts": time.time()})
        yield "done", {"stopped": "iteration_limit"}

    def agent_reply(self, session_id, content):
        """Agent counterpart of chat_reply; same refusal-before-first-yield
        contract, plus: refuses while a call awaits approval."""
        session = self.chats.get(session_id)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty message")
        if session.get("pending_calls"):
            raise ToolError("a tool call awaits approval: answer it first")
        lock = self._gpu_guard()
        try:
            self.chats.append(session_id, {"role": "user", "content": content,
                                           "ts": time.time()})
            yield "start", None
            yield from self._agent_turn(session_id)
        finally:
            lock.release()
```

Note: `Path` is already imported in `factory_mcp.py`; verify, add if missing.

- [ ] **Step 4: Run the tests**

Run: `cd harness && python -m pytest tests/test_factory_mcp.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -m "feat: agent reply loop with native tool calling and approval gate"
```

---

### Task 7: Facade — `chat_approve` (resume after the operator decides)

**Files:**
- Modify: `harness/factory_mcp.py`
- Test: `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Consumes: `_run_call`, `_agent_turn`, store `pending_calls` from Task 6.
- Produces: `Factory.chat_approve(session_id, approved)` — generator with the same event vocabulary as `agent_reply`; raises `ToolError` before the first yield when nothing is pending or the GPU is busy.

- [ ] **Step 1: Write the failing tests** (append; reuses `_fake_agent_stream` and `_agent` from Task 6)

```python
def test_chat_approve_executes_the_parked_call_and_resumes(factory, tmp_path,
                                                           monkeypatch):
    fake = _fake_agent_stream([
        [("tool_call", {"name": "write_file",
                        "arguments": {"path": "b.txt", "content": "x"}})],
        [("content", "écrit !")],
    ])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)
    list(factory.agent_reply(sid, "écris b.txt"))

    events = list(factory.chat_approve(sid, True))

    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "x"
    assert events[-1][0] == "done"
    assert factory.chat_get(sid)["pending_calls"] == []


def test_chat_approve_refusal_feeds_the_model_a_tool_message(factory, tmp_path,
                                                             monkeypatch):
    fake = _fake_agent_stream([
        [("tool_call", {"name": "run_command", "arguments": {"command": "rm x"}})],
        [("content", "d'accord, j'arrête.")],
    ])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)
    list(factory.agent_reply(sid, "supprime x"))

    events = list(factory.chat_approve(sid, False))

    assert events[-1][0] == "done"
    msgs = fake.state["calls"][1]["messages"]
    assert any(m["role"] == "tool" and "refused" in m["content"] for m in msgs)


def test_chat_approve_without_pending_call_is_a_tool_error(factory):
    sid = factory.chat_create("m:1", kind="agent")["session_id"]
    with pytest.raises(ToolError):
        next(factory.chat_approve(sid, True))
```

- [ ] **Step 2: Run to verify failure**

Run: `cd harness && python -m pytest tests/test_factory_mcp.py -q -k approve`
Expected: FAIL (`no attribute chat_approve`)

- [ ] **Step 3: Implement** (after `agent_reply`)

```python
    def chat_approve(self, session_id, approved):
        """Resolve the first pending call, then resume the agent loop."""
        session = self.chats.get(session_id)
        calls = session.get("pending_calls") or []
        if not calls:
            raise ToolError("nothing awaits approval")
        lock = self._gpu_guard()
        try:
            yield "start", None
            call = calls[0]
            self.chats.set_pending_calls(session_id, calls[1:])
            if approved:
                yield from self._run_call(session_id, call, session["workspace"])
            else:
                output = "refused by operator"
                self.chats.append(session_id, {
                    "role": "tool", "tool_name": call["name"],
                    "content": output, "ts": time.time()})
                yield "tool_result", {"name": call["name"], "output": output}
            yield from self._agent_turn(session_id)
        finally:
            lock.release()
```

- [ ] **Step 4: Run the tests**

Run: `cd harness && python -m pytest tests/test_factory_mcp.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -m "feat: chat_approve resolves parked tool calls and resumes"
```

---

### Task 8: Web layer — agent routes and event streaming

**Files:**
- Modify: `harness/factory_web.py` (`_chat_create`, `_chat_send`, new `_chat_mode` + `_chat_approve`, `ROUTES`)
- Test: `harness/tests/test_factory_web.py`

**Interfaces:**
- Consumes: `Factory.chat_create(model, kind, mode)`, `agent_reply`, `chat_approve`, `chat_set_mode`.
- Produces routes: `POST /api/chats {model, kind?, mode?}`; `POST /api/chats/(id)/mode {mode}`; `POST /api/chats/(id)/approve {approved}` (NDJSON stream); `/messages` dispatches agent sessions to `agent_reply` and streams `{"tool_call": ...}`, `{"tool_result": ...}`, `{"approval_needed": ...}` lines.

- [ ] **Step 1: Write the failing tests** (append to `test_factory_web.py`)

```python
def _fake_agent_stream(turns):
    state = {"i": 0}

    def stream(model, messages, **kw):
        turn = turns[state["i"]]
        state["i"] += 1
        content = ""
        for kind, payload in turn:
            if kind == "content":
                content += payload
            yield kind, payload
        return {"content": content, "tokens_per_s": 30.0, "ttft_s": 1.0,
                "eval_count": 5}
    return stream


def test_agent_session_streams_tool_events(server, factory, tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("yo", encoding="utf-8")
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream",
                        _fake_agent_stream([
        [("tool_call", {"name": "read_file", "arguments": {"path": "a.txt"}})],
        [("content", "fini")]]))
    _, s = request(server, "POST", "/api/chats",
                   {"model": "m:1", "kind": "agent", "mode": "auto"},
                   token="t0k3n")
    factory.chats.get(s["session_id"])  # session exists
    factory.chats.set_pending_calls  # store API present
    # workspace must be controllable for the test: point it at tmp_path
    sess = factory.chats.get(s["session_id"])
    sess["workspace"] = str(tmp_path)
    factory.chats._save(sess, None)

    code, raw = request(server, "POST",
                        "/api/chats/" + s["session_id"] + "/messages",
                        {"content": "lis a.txt"}, token="t0k3n", raw=True)

    assert code == 200
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    kinds = [next(iter(l)) for l in lines]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert lines[-1]["done"] is True


def test_approve_route_streams_and_messages_409_while_pending(
        server, factory, tmp_path, monkeypatch):
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream",
                        _fake_agent_stream([
        [("tool_call", {"name": "write_file",
                        "arguments": {"path": "b.txt", "content": "x"}})],
        [("content", "écrit")]]))
    _, s = request(server, "POST", "/api/chats",
                   {"model": "m:1", "kind": "agent", "mode": "approve"},
                   token="t0k3n")
    sess = factory.chats.get(s["session_id"])
    sess["workspace"] = str(tmp_path)
    factory.chats._save(sess, None)
    code, raw = request(server, "POST",
                        "/api/chats/" + s["session_id"] + "/messages",
                        {"content": "écris b.txt"}, token="t0k3n", raw=True)
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    assert next(iter(lines[-1])) == "approval_needed"

    code, body = request(server, "POST",
                         "/api/chats/" + s["session_id"] + "/messages",
                         {"content": "autre"}, token="t0k3n")
    assert code == 409

    code, raw = request(server, "POST",
                        "/api/chats/" + s["session_id"] + "/approve",
                        {"approved": True}, token="t0k3n", raw=True)
    assert code == 200
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "x"
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    assert lines[-1]["done"] is True


def test_chat_mode_route(server, factory):
    _, s = request(server, "POST", "/api/chats",
                   {"model": "m:1", "kind": "agent"}, token="t0k3n")
    code, body = request(server, "POST",
                         "/api/chats/" + s["session_id"] + "/mode",
                         {"mode": "auto"}, token="t0k3n")
    assert code == 200 and body["mode"] == "auto"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd harness && python -m pytest tests/test_factory_web.py -q -k "agent or approve or mode"`
Expected: FAIL

- [ ] **Step 3: Implement** in `factory_web.py`

Replace `_chat_create`:

```python
    def _chat_create(self, query=None):
        b = self._body()
        self._json(200, self.server.factory.chat_create(
            b["model"], kind=b.get("kind"), mode=b.get("mode")))
```

Replace `_chat_send` with a shared streaming helper + dispatch, and add the two new handlers:

```python
    def _stream_events(self, gen):
        # Refusals (404/409/400) raise on the first next(), before any byte
        # of the stream: _route still owns the response and answers JSON.
        kind, _ = next(gen)
        assert kind == "start"
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            for kind, payload in gen:
                if kind == "done":
                    line = dict(payload, done=True)
                elif kind == "error":
                    line = {"error": payload}
                else:  # chunk, thinking, tool_call, tool_result, approval_needed
                    line = {kind: payload}
                self.wfile.write((json.dumps(line) + "\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError):
            gen.close()  # the facade saves the partial and frees the GPU

    def _chat_send(self, session_id, query=None):
        factory = self.server.factory
        content = self._body()["content"]
        if factory.chat_get(session_id).get("kind") == "agent":
            gen = factory.agent_reply(session_id, content)
        else:
            gen = factory.chat_reply(session_id, content)
        self._stream_events(gen)

    def _chat_mode(self, session_id, query=None):
        self._json(200, self.server.factory.chat_set_mode(
            session_id, self._body()["mode"]))

    def _chat_approve(self, session_id, query=None):
        self._stream_events(self.server.factory.chat_approve(
            session_id, bool(self._body().get("approved"))))
```

Add to `ROUTES` next to the other chat routes:

```python
    (r"/api/chats/(c_\w+)/mode", "POST", Handler._chat_mode),
    (r"/api/chats/(c_\w+)/approve", "POST", Handler._chat_approve),
```

- [ ] **Step 4: Run the tests**

Run: `cd harness && python -m pytest tests/test_factory_web.py -q`
Expected: all PASS (including the pre-existing chat tests — `_chat_send` behavior for plain sessions is unchanged)

- [ ] **Step 5: Commit**

```bash
git add harness/factory_web.py harness/tests/test_factory_web.py
git commit -m "feat: web routes for agent sessions, mode switch and approval"
```

---

### Task 9: UI — agent session creation, badges, replayed tool messages

**Files:**
- Modify: `harness/web/app.js` (chat section), `harness/web/style.css`

No JS test harness exists; verification is the Playwright smoke in Task 11. Keep changes reviewable.

- [ ] **Step 1: Session creation controls**

In `renderChat`, replace the `newBtn` block with a small form: kind select, mode select (visible only for agent), create button.

```js
  const kindSel = h("select", { class: "chat-new-kind" },
    h("option", { value: "chat", text: "chat" }),
    h("option", { value: "agent", text: "agent" }));
  const modeSel = h("select", { class: "chat-new-mode", hidden: "" },
    h("option", { value: "approve", text: "approbation" }),
    h("option", { value: "auto", text: "auto" }));
  kindSel.addEventListener("change", () => {
    modeSel.hidden = kindSel.value !== "agent";
  });
  const newBtn = h("button", { text: "+ Nouvelle session", onclick: async () => {
    const model = installed[0];
    if (!model) {
      showError(new Error("Aucun modèle installé (voir l'onglet Ollama)."));
      return;
    }
    const body = { model };
    if (kindSel.value === "agent") { body.kind = "agent"; body.mode = modeSel.value; }
    try {
      const s = await api("/api/chats", { method: "POST",
        body: JSON.stringify(body) });
      location.hash = "chat/" + s.session_id;
    } catch (e) { showError(e); }
  } });
  const newForm = h("div", { class: "chat-new" }, newBtn, kindSel, modeSel);
```

Use `newForm` instead of `newBtn` in the `list` container. In the session rows, add a badge when `s.kind === "agent"`:

```js
      h("span", { class: "chat-title", text: (s.kind === "agent" ? "⚙ " : "") + (s.title || "(vide)") }),
```

Note: `h()` sets attributes via `setAttribute`, so `hidden: ""` works; clearing needs the property (`modeSel.hidden = ...`), which the change listener does.

- [ ] **Step 2: Mode dropdown in the session header**

In the `sessionId` branch, after the model selector wiring, add:

```js
    let modeCtl = "";
    if (session.kind === "agent") {
      modeCtl = h("select", { class: "chat-mode" },
        h("option", { value: "approve", text: "approbation" }),
        h("option", { value: "auto", text: "auto" }));
      modeCtl.value = session.mode;
      modeCtl.addEventListener("change", async () => {
        try {
          await api("/api/chats/" + sessionId + "/mode", { method: "POST",
            body: JSON.stringify({ mode: modeCtl.value }) });
        } catch (e) { showError(e); }
      });
    }
```

and include it in the banner:

```js
      h("div", { class: "banner" }, h("label", { text: "Modèle" }), model,
        modeCtl ? h("label", { text: "Mode" }) : "", modeCtl),
```

- [ ] **Step 3: Replay tool messages and tool_calls**

Add a chip renderer near `chatBubble`:

```js
function toolChip(name, args, output) {
  const summaryText = "▸ " + name + " " +
    JSON.stringify(args || {}).slice(0, 80);
  const chip = h("details", { class: "chat-tool" },
    h("summary", { text: summaryText }),
    h("pre", { class: "chat-tool-out", text: output || "…" }));
  return chip;
}
```

In the history rendering (`session.messages.map`), branch on role:

```js
    const log = h("div", { class: "chat-log" }, ...session.messages.map((m) => {
      if (m.role === "tool")
        return toolChip(m.tool_name || "tool", null, m.content);
      const b = chatBubble(m.role, m.content, m.thinking);
      ...
```

(keep the existing metrics/error lines, return `b.bubble` as today; assistant messages that only carried tool_calls have empty content and render as an empty bubble — hide those: `if (m.role === "assistant" && !m.content && !m.thinking) return "";` before creating the bubble. `h()` ignores empty-string children safely since `el.append("")` appends an empty text node.)

- [ ] **Step 4: CSS** (append near the chat styles)

```css
.chat-new { display: flex; flex-direction: column; gap: 6px; }
.chat-tool { font-size: 12px; color: var(--dim); align-self: flex-start;
  max-width: 46rem; }
.chat-tool summary { cursor: pointer; user-select: none; font-family: monospace; }
.chat-tool-out { white-space: pre-wrap; overflow-wrap: break-word; margin: 4px 0 0;
  padding-left: 8px; border-left: 2px solid var(--line);
  max-height: 14rem; overflow-y: auto; }
.chat-approval { border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 13px; align-self: flex-start; max-width: 46rem; }
.chat-approval pre { white-space: pre-wrap; overflow-wrap: break-word;
  max-height: 14rem; overflow-y: auto; }
.chat-approval .buttons { display: flex; gap: 8px; margin-top: 8px; }
```

- [ ] **Step 5: Manual smoke + commit**

Run: `cd harness && python -m pytest -q` (nothing broken server-side)
Open the Studio, create an agent session, check the badge, mode dropdown, and that plain chat still works.

```bash
git add harness/web/app.js harness/web/style.css
git commit -m "feat: chat ui creates agent sessions and replays tool messages"
```

---

### Task 10: UI — streamed tool events and the approval panel

**Files:**
- Modify: `harness/web/app.js` (`sendChat`, send flow)

**Interfaces:**
- Consumes: NDJSON lines `{"thinking"}`, `{"chunk"}`, `{"tool_call"}`, `{"tool_result"}`, `{"approval_needed"}`, `{"done"...}`, `{"error"}`; routes `/messages` and `/approve`.

- [ ] **Step 1: Generalize `sendChat` into a log-driven stream renderer**

Replace `sendChat` with `streamChat(url, body, log)`: it owns bubble creation (an agent turn interleaves several assistant bubbles and tool chips), so the caller no longer pre-creates the reply bubble.

```js
async function streamChat(url, body, log) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Factory-Token": TOKEN },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const data = await resp.json();
    throw new Error(data.error + ": " + data.detail);
  }
  let reply = null;       // current assistant bubble, opened lazily
  let chip = null;        // current tool chip awaiting its result
  const bubble = () => {
    if (!reply) { reply = chatBubble("assistant", ""); log.append(reply.bubble); }
    return reply;
  };
  const feed = (line) => {
    if (!line.trim()) return;
    const msg = JSON.parse(line);
    if (msg.thinking) {
      const r = bubble();
      r.think.hidden = false;
      r.think.open = true;
      r.thinkText.textContent += msg.thinking;
    } else if (msg.chunk) {
      const r = bubble();
      r.think.open = false;
      r.text.textContent += msg.chunk;
    } else if (msg.tool_call) {
      chip = toolChip(msg.tool_call.name, msg.tool_call.arguments, "");
      chip.open = false;
      log.append(chip);
      reply = null;       // next content starts a fresh bubble
    } else if (msg.tool_result) {
      if (chip) chip.querySelector(".chat-tool-out").textContent =
        msg.tool_result.output;
      chip = null;
    } else if (msg.approval_needed) {
      log.append(approvalPanel(msg.approval_needed, log));
      reply = null;
    } else if (msg.done) {
      if (reply) reply.meta.textContent =
        Math.round(msg.tokens_per_s || 0) + " tok/s · ttft " +
        (msg.ttft_s || 0).toFixed(1) + " s";
    } else if (msg.error) {
      const r = bubble();
      r.meta.textContent = "erreur : " + msg.error;
      r.meta.classList.add("chat-error");
    }
    log.scrollTop = log.scrollHeight;
  };
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    lines.forEach(feed);
  }
  feed(buf);
}
```

- [ ] **Step 2: The approval panel**

```js
function approvalPanel(call, log) {
  const args = call.arguments || {};
  let detail;
  if (call.name === "run_command") detail = "$ " + (args.command || "");
  else if (call.name === "edit_file")
    detail = args.path + "\n--- old\n" + (args.old || "") +
             "\n+++ new\n" + (args.new || "");
  else if (call.name === "write_file")
    detail = args.path + "\n" + (args.content || "");
  else detail = JSON.stringify(args, null, 2);
  const panel = h("div", { class: "chat-approval" },
    h("b", { text: "L'agent veut exécuter : " + call.name }),
    h("pre", { text: detail }));
  const sessionId = document.querySelector(".chat-root").dataset.session;
  const decide = (approved) => async () => {
    panel.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      await streamChat("/api/chats/" + sessionId + "/approve",
                       { approved }, log);
      panel.remove();
    } catch (e) { showError(e); }
  };
  panel.append(h("div", { class: "buttons" },
    h("button", { text: "Approuver", onclick: decide(true) }),
    h("button", { class: "ghost", text: "Refuser", onclick: decide(false) })));
  return panel;
}
```

- [ ] **Step 3: Rewire the send flow**

In `renderChat`'s `send` function, replace the bubble pre-creation and `sendChat` call:

```js
    const send = async () => {
      const content = input.value.trim();
      if (!content || sendBtn.disabled) return;
      input.value = "";
      sendBtn.disabled = true;
      log.append(chatBubble("user", content).bubble);
      log.scrollTop = log.scrollHeight;
      try {
        await streamChat("/api/chats/" + sessionId + "/messages",
                         { content }, log);
      } catch (e) { showError(e); }
      sendBtn.disabled = false;
      log.scrollTop = log.scrollHeight;
      input.focus();
    };
```

- [ ] **Step 4: Replay a still-pending approval on reload**

After building `log` from `session.messages`, add:

```js
    if ((session.pending_calls || []).length)
      log.append(approvalPanel(session.pending_calls[0], log));
```

- [ ] **Step 5: Verify + commit**

Run: `cd harness && python -m pytest -q`
Expected: all PASS (UI is exercised in Task 11).

```bash
git add harness/web/app.js
git commit -m "feat: chat ui streams tool events and inline approval panel"
```

---

### Task 11: End-to-end smoke on the live Studio + docs

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Restart the Studio with the new code**

Find the running `factory_web.py` PID, stop it, relaunch:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match "factory_web" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

```bash
cd C:/Users/me/local-factory && start //b python harness/factory_web.py
```

- [ ] **Step 2: Scripted NDJSON smoke against the real model**

Read the token from the served page (`<meta ... token" content="...">`), then: create an agent session in **auto** mode on model `qwen3.6:35b`, POST "liste les fichiers à la racine et lis README.md, puis résume-le en une phrase", and assert the stream contains `thinking`, `tool_call`, `tool_result` and `done` lines. This measures real qwen3.6 tool-calling quality — record tokens/s and whether the calls were well-formed.

- [ ] **Step 3: Browser smoke (Playwright)**

In the Studio: create an agent session in **approbation** mode, ask for a small file write, see the approval panel render with the content, click Approuver, watch the resume stream, confirm the file exists. Then reload the page and confirm the conversation (chips included) replays.

- [ ] **Step 4: Changelog + commit**

Add a CHANGELOG entry describing the agent mode (tools, modes, approval flow). Update the project memory file per the operator's standing instruction.

```bash
git add CHANGELOG.md
git commit -m "docs: changelog entry for chat agent mode"
```

---

## Self-Review Notes

- Spec coverage: session fields (T4/T5), tools + sandbox + truncation (T2/T3), native tool calling (T1), loop + gate + cap (T6), resume (T7), routes (T8), UI create/replay (T9), UI stream/approval (T10), smoke + real-model risk check (T11). AGENT.md instructions covered in T6 (`_agent_system`).
- The web test in T8 pokes `workspace` into the session file directly (`factory.chats._save`) because the route defaults workspace to the server's cwd; acceptable in a boundary test.
- Type consistency: events are `(kind, payload)` tuples end-to-end; `pending_calls` is always a list of `{"name", "arguments"}` dicts; tool messages carry `tool_name`.
- Security posture: `run_command` uses `shell=True` deliberately — arbitrary shell is the tool's contract (spec: "Risk accepted"); the guardrails are the approve mode, the localhost-only token-gated server, and the workspace cwd. Not an injection bug: there is no unwitting interpolation, the whole string is the payload.

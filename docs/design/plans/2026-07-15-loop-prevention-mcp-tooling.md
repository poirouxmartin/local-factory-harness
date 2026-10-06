# Loop Prevention + MCP Tooling + Blitzvolley Pilot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make degenerate generation loops structurally impossible (generation cap + model-card sampling), give the chat agent curated MCP tools, and validate delegation on blitzvolley.

**Architecture:** Chantier A threads an `options` dict through `ollama_client` so every call carries `num_predict` (the `max_tokens` equivalent) and per-model sampling profiles from `factory.toml`. Chantier B adds a stdlib MCP stdio client + a registry that namespaces allowlisted server tools into the existing agent tool loop, selected per session via toolsets. Chantier C adds a `node --test` runner and puts blitzvolley in the delegation perimeter.

**Tech Stack:** Python 3.9, stdlib only (no pip deps in harness/). pytest for tests. External MCP servers run via `uvx`/`npx`.

**Spec:** `docs/design/specs/2026-07-15-loop-prevention-mcp-tooling-design.md`

## Global Constraints

- Python 3.9 compatible; stdlib only in `harness/` (the `tomli` fallback import pattern in `factory_config.py` is the model).
- House style: `str.format()` (no f-strings), module docstrings explain the *why*, errors returned to the model are strings, never exceptions (see `agent_tools.py`).
- Full suite must stay green after every task: `python -m pytest harness/tests -q` from repo root (449 tests before this plan).
- Commits: conventional (`feat:`/`fix:`/`test:`/`docs:`), imperative, English, ASCII only, **no AI attribution of any kind**, committed directly on `main` (project convention).
- Tool budget: warn past 15 exposed tools per session (`TOOL_BUDGET = 15`).
- `num_predict` values: chat/agent = `chat_context.OUTPUT_RESERVE` (8192); ladder = `LADDER_NUM_PREDICT = 8192`.

---

# Phase A — Loop prevention

### Task 1: `options` passthrough + "length" stop in ollama_client

**Files:**
- Modify: `harness/ollama_client.py:115-136` (chat), `:177-254` (chat_stream)
- Test: `harness/tests/test_ollama_client.py`

**Interfaces:**
- Produces: `chat(model, system, user, temperature=0.1, num_ctx=16384, timeout=900, options=None)` and `chat_stream(model, messages, temperature=0.7, num_ctx=16384, timeout=900, tools=None, options=None)`. `options` is a dict merged OVER `{temperature, num_ctx}` in the request payload. `chat_stream` metrics carry `stopped: "length"` when `done_reason == "length"`.

- [ ] **Step 1: Write the failing tests** (append to `harness/tests/test_ollama_client.py`; `_FakeResp` and `_fake_urlopen` already exist there)

```python
def _capturing_urlopen(lines):
    payload = b"".join(json.dumps(l).encode("utf-8") + b"\n" for l in lines)

    def urlopen(req, timeout=None):
        urlopen.req = req
        return _FakeResp(payload)
    return urlopen


def test_chat_merges_extra_options_into_the_payload(monkeypatch):
    fake = _capturing_urlopen([{"message": {"content": "ok"}, "done": True}])
    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", fake)

    ollama_client.chat("m:1", "sys", "hi", temperature=0.2,
                       options={"num_predict": 8192, "top_p": 0.95})

    opts = json.loads(fake.req.data)["options"]
    assert opts == {"temperature": 0.2, "num_ctx": 16384,
                    "num_predict": 8192, "top_p": 0.95}


def test_chat_stream_merges_extra_options_into_the_payload(monkeypatch):
    fake = _capturing_urlopen([{"message": {"content": "ok"}, "done": True,
                                "done_reason": "stop"}])
    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", fake)

    gen = chat_stream("m:1", [{"role": "user", "content": "hi"}],
                      options={"num_predict": 4096, "top_k": 20})
    list(gen)

    opts = json.loads(fake.req.data)["options"]
    assert opts["num_predict"] == 4096 and opts["top_k"] == 20
    assert opts["num_ctx"] == 16384  # defaults survive the merge


def test_stream_marks_a_length_cut_as_stopped(monkeypatch):
    lines = [{"message": {"content": "partial answer"}},
             {"done": True, "done_reason": "length",
              "eval_count": 10, "eval_duration": 1_000_000_000}]
    monkeypatch.setattr(ollama_client.urllib.request, "urlopen",
                        _fake_urlopen(lines))

    gen = chat_stream("m:1", [{"role": "user", "content": "hi"}])
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            metrics = stop.value
            break

    assert metrics["stopped"] == "length"
    assert metrics["done_reason"] == "length"
    assert metrics["content"] == "partial answer"


def test_stream_does_not_mark_a_normal_stop(monkeypatch):
    lines = [{"message": {"content": "fin"}},
             {"done": True, "done_reason": "stop"}]
    monkeypatch.setattr(ollama_client.urllib.request, "urlopen",
                        _fake_urlopen(lines))

    gen = chat_stream("m:1", [{"role": "user", "content": "hi"}])
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            metrics = stop.value
            break

    assert "stopped" not in metrics
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest harness/tests/test_ollama_client.py -q`
Expected: 4 failures (`TypeError: chat() got an unexpected keyword argument 'options'`, and the length test fails on missing `stopped`).

- [ ] **Step 3: Implement**

In `chat()` (line 115), replace the signature and options construction:

```python
def chat(model, system, user, temperature=0.1, num_ctx=16384, timeout=900,
         options=None):
    opts = {"temperature": temperature, "num_ctx": num_ctx}
    opts.update(options or {})
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": opts,
    }
```

In `chat_stream()` (line 177), same treatment:

```python
def chat_stream(model, messages, temperature=0.7, num_ctx=16384, timeout=900,
                tools=None, options=None):
```
```python
    opts = {"temperature": temperature, "num_ctx": num_ctx}
    opts.update(options or {})
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": opts,
    }
```

And at the end of `chat_stream()` (currently lines 247-251), mark a length cut. Extend the docstring's `stopped` sentence with: `"length" means Ollama hit num_predict -- the turn was cut, not finished.`

```python
    metrics = _metrics(final, time.time() - t0)
    metrics["content"] = "".join(parts)
    metrics.update(phase)
    if not stopped and metrics["done_reason"] == "length":
        # Ollama hit num_predict: same contract as the RepetitionGuard cut --
        # the caller must not treat this reply as a finished turn.
        stopped = "length"
    if stopped:
        metrics["stopped"] = stopped
```

- [ ] **Step 4: Run the module tests, then the full suite**

Run: `python -m pytest harness/tests/test_ollama_client.py -q` → all pass.
Run: `python -m pytest harness/tests -q` → green (existing callers pass no `options`; behavior unchanged).

- [ ] **Step 5: Commit**

```bash
git add harness/ollama_client.py harness/tests/test_ollama_client.py
git commit -m "feat: options passthrough and length-stop detection in ollama client"
```

---

### Task 2: Sampling profiles `[models]` in factory.toml + factory_config

**Files:**
- Modify: `harness/factory_config.py` (add after `load_projects`, line 66), `factory.toml`
- Test: `harness/tests/test_factory_config.py`

**Interfaces:**
- Produces: `factory_config.load_models(config_path) -> dict` (model name → options dict, validated), `factory_config.model_options(profiles, model) -> dict` (copy, `{}` when absent), `factory_config.SAMPLING_KEYS`.

- [ ] **Step 1: Write the failing tests** (append to `harness/tests/test_factory_config.py`; use the file's existing config-writing pattern — it writes TOML strings to `tmp_path`)

```python
def test_load_models_returns_profiles(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[models."qwen3.6:35b"]\n'
                   'temperature = 0.6\ntop_p = 0.95\ntop_k = 20\n'
                   'presence_penalty = 1.0\n', encoding="utf-8")

    models = factory_config.load_models(cfg)

    assert models == {"qwen3.6:35b": {"temperature": 0.6, "top_p": 0.95,
                                      "top_k": 20, "presence_penalty": 1.0}}


def test_load_models_refuses_unknown_keys(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[models."m:1"]\ntop-p = 0.9\n', encoding="utf-8")

    with pytest.raises(factory_config.ConfigError):
        factory_config.load_models(cfg)


def test_load_models_refuses_non_numeric_values(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[models."m:1"]\ntemperature = "hot"\n', encoding="utf-8")

    with pytest.raises(factory_config.ConfigError):
        factory_config.load_models(cfg)


def test_model_options_copies_and_defaults_empty():
    profiles = {"m:1": {"temperature": 0.6}}

    opts = factory_config.model_options(profiles, "m:1")
    opts["temperature"] = 999  # mutating the copy must not touch the profile

    assert profiles["m:1"]["temperature"] == 0.6
    assert factory_config.model_options(profiles, "unknown") == {}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest harness/tests/test_factory_config.py -q`
Expected: `AttributeError: module 'factory_config' has no attribute 'load_models'`.

- [ ] **Step 3: Implement** (in `harness/factory_config.py`, after `load_projects`)

```python
# Ollama sampling options a [models] profile may set. A typo like `top-p`
# must fail loudly, not silently sample greedy (the July 14 failure mode).
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p",
                 "repeat_penalty", "presence_penalty")


def load_models(config_path):
    """Per-model sampling profiles from [models."name"] tables.

    Quasi-greedy decoding on thinking models is documented (Qwen model
    cards) to cause infinite repetition; profiles carry the card's values
    to every call site instead of hard-coded temperatures.
    """
    with open(config_path, "rb") as f:
        raw = tomllib.load(f).get("models", {})
    profiles = {}
    for name, entry in raw.items():
        bad = set(entry) - set(SAMPLING_KEYS)
        if bad:
            raise ConfigError("[models.{!r}]: unknown option(s): {}".format(
                name, ", ".join(sorted(bad))))
        for key, value in entry.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError("[models.{!r}].{} must be a number".format(
                    name, key))
        profiles[name] = dict(entry)
    return profiles


def model_options(profiles, model):
    """Copy of the model's sampling profile; {} when it has none."""
    return dict(profiles.get(model, {}))
```

- [ ] **Step 4: Add the profiles to `factory.toml`** (append at the end)

```toml
# Sampling profiles (spec 2026-07-15, A2). Applied as Ollama options wherever
# the model is invoked; an explicit per-call temperature override still wins.
# Values come from the Qwen model cards: quasi-greedy decoding on thinking
# models is documented to cause infinite repetition (the July 14 incident).
[models."qwen3.6:35b"]
temperature = 0.6
top_p = 0.95
top_k = 20
presence_penalty = 1.0

[models."qwen3-coder:30b"]
temperature = 0.7
top_p = 0.8
top_k = 20
repeat_penalty = 1.05
```

- [ ] **Step 5: Run the tests, then the full suite**

Run: `python -m pytest harness/tests/test_factory_config.py -q` → pass.
Run: `python -m pytest harness/tests -q` → green.

- [ ] **Step 6: Commit**

```bash
git add harness/factory_config.py harness/tests/test_factory_config.py factory.toml
git commit -m "feat: per-model sampling profiles in factory.toml"
```

---

### Task 3: Chat + agent use profiles and num_predict

**Files:**
- Modify: `harness/factory_mcp.py` — imports (line 29), constants (after `DEGENERATE_STOPPED`, line 153), `chat_reply` (line 617), `_compact` (line 663), `_agent_turn` (lines 770, 818-827)
- Test: `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Consumes: `ollama_client.chat/chat_stream(options=...)` (Task 1), `factory_config.load_models/model_options` (Task 2), `chat_context.OUTPUT_RESERVE`.
- Produces: `Factory._model_options(model) -> dict` (profile + `num_predict`), `factory_mcp.LENGTH_STOPPED` message constant.

- [ ] **Step 1: Write the failing tests** (append to `harness/tests/test_factory_mcp.py`, agent section; `_fake_agent_stream` and `_agent` helpers exist at lines 634-656 — extend `_fake_agent_stream`'s recorded kw with `"options": kw.get("options")` inside its `stream()`)

```python
def _factory_with_profiles(tmp_path, toml_extra=""):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[models."m:1"]\ntemperature = 0.6\ntop_p = 0.95\n'
                   + toml_extra, encoding="utf-8")
    return factory_mcp.Factory(tmp_path / "jobs", cfg)


def test_agent_stream_gets_profile_and_num_predict(tmp_path, monkeypatch):
    factory = _factory_with_profiles(tmp_path)
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1", kind="agent", mode="auto",
                              workspace=str(tmp_path))["session_id"]

    list(factory.agent_reply(sid, "salut"))

    opts = fake.state["calls"][0]["options"]
    assert opts["num_predict"] == factory_mcp.chat_context.OUTPUT_RESERVE
    assert opts["temperature"] == 0.6 and opts["top_p"] == 0.95


def test_chat_stream_gets_num_predict_without_profile(tmp_path, monkeypatch):
    cfg = tmp_path / "factory.toml"
    cfg.write_text("", encoding="utf-8")
    factory = factory_mcp.Factory(tmp_path / "jobs", cfg)
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1")["session_id"]

    list(factory.chat_reply(sid, "salut"))

    opts = fake.state["calls"][0]["options"]
    assert opts == {"num_predict": factory_mcp.chat_context.OUTPUT_RESERVE}


def test_agent_length_cut_stops_the_turn_with_its_own_notice(tmp_path, monkeypatch):
    factory = _factory_with_profiles(tmp_path)

    def stream(model, messages, **kw):
        yield "content", "cut mid-"
        return {"content": "cut mid-", "stopped": "length",
                "done_reason": "length"}
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", stream)
    sid = factory.chat_create("m:1", kind="agent", mode="auto",
                              workspace=str(tmp_path))["session_id"]

    events = list(factory.agent_reply(sid, "go"))

    assert events[-1][0] == "done" and events[-1][1]["stopped"] == "length"
    msgs = factory.chat_get(sid)["messages"]
    assert msgs[-1]["tool_name"] == "system"
    assert msgs[-1]["content"] == factory_mcp.LENGTH_STOPPED
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest harness/tests/test_factory_mcp.py -q -k "profile or num_predict or length_cut"`
Expected: KeyError on `"options"` / AttributeError on `LENGTH_STOPPED`.

- [ ] **Step 3: Implement**

Import additions (line 29): add `load_models, model_options` to the `factory_config` import list.

Constant (after `DEGENERATE_STOPPED`, line 153):

```python
LENGTH_STOPPED = ("generation stopped: the reply hit the per-turn generation "
                  "cap (num_predict). The partial reply is kept; send "
                  "'continue' to let the agent resume.")
```

`Factory` method (put it next to `_gpu_guard`):

```python
    def _model_options(self, model):
        """Sampling profile from factory.toml plus the per-turn generation
        cap. num_predict == chat_context.OUTPUT_RESERVE: the prompt budget
        already reserves that many tokens, the cap turns the reserve into a
        guarantee -- a single turn can no longer overflow the window
        mid-stream (the July 14 root cause)."""
        try:
            opts = model_options(load_models(self.config_path), model)
        except (OSError, ConfigError) as e:
            print("model profiles unavailable: {}".format(e), file=sys.stderr)
            opts = {}
        opts["num_predict"] = chat_context.OUTPUT_RESERVE
        return opts
```

Call sites:

- `chat_reply` (line 617): `stream = ollama_client.chat_stream(session["model"], history, num_ctx=num_ctx, options=self._model_options(session["model"]))`
- `_agent_turn` (line 770): `stream = ollama_client.chat_stream(session["model"], history, num_ctx=num_ctx, tools=agent_tools.TOOLS, options=self._model_options(session["model"]))`
- `_compact` (line 663): add `options=self._model_options(model)` to the `ollama_client.chat(...)` call (keep `temperature=0.1` — the profile's temperature, when present, wins via the merge; the summary must also be capped).

`_agent_turn` stopped branch (lines 818-827) — pick the notice by reason:

```python
            if metrics.get("stopped"):
                # The stream layer cut the turn (degenerate output) or Ollama
                # hit num_predict: either way, do not run this turn's tool
                # calls or iterate on it -- surface why and hand back.
                note = (LENGTH_STOPPED if metrics["stopped"] == "length"
                        else DEGENERATE_STOPPED)
                self.chats.set_pending_calls(session_id, [])
                self.chats.append(session_id, {
                    "role": "tool", "tool_name": "system",
                    "content": note, "ts": time.time()})
                yield "done", _usage(metrics, info, compacted, num_ctx)
                return
```

- [ ] **Step 4: Run tests, full suite**

Run: `python -m pytest harness/tests/test_factory_mcp.py -q` → pass (existing tests keep passing: they assert on `num_ctx`/`tools` kw, which are untouched).
Run: `python -m pytest harness/tests -q` → green.

- [ ] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -m "feat: chat and agent turns are capped by num_predict and use model profiles"
```

---

### Task 4: Ladder adopts profiles and num_predict

**Files:**
- Modify: `harness/loop_job.py` (DEFAULT_LADDER ~line 45, `_review` line 112, `_run_ladder` lines 134-169, `_execute` lines 288-318), `harness/loop.py` (DEFAULT_LADDER lines 39-42, `run_task` chat call line 140)
- Test: `harness/tests/test_loop_job.py`

**Interfaces:**
- Consumes: `chat(..., options=...)` (Task 1), `factory_config.load_models/model_options` (Task 2).
- Produces: `_run_ladder(..., models=None)` kwarg; `LADDER_NUM_PREDICT = 8192` in `loop_job.py`.

- [ ] **Step 1: Write the failing test** (append to `harness/tests/test_loop_job.py`; mirror the file's existing fake-`chat_fn` pattern — fakes there accept `**kw`, check and adapt if one uses a fixed signature)

```python
def test_ladder_passes_profile_options_and_num_predict(tmp_path, ...):
    # Build on the file's existing minimal _run_ladder harness (fake store,
    # worktree, chat_fn). Assert on the kw the fake chat_fn records:
    #   opts = recorded_kw["options"]
    #   assert opts["num_predict"] == loop_job.LADDER_NUM_PREDICT
    #   assert opts["top_p"] == 0.8            # from the profile passed in
    #   assert "temperature" not in opts       # the stage temperature wins
    #   assert recorded_kw["temperature"] == stage_temperature
    ...
```

Write it concretely against the fixtures that exist in `test_loop_job.py` (read the top of that file first; it already fakes `chat_fn` for `_run_ladder`). The assertions above are the contract.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest harness/tests/test_loop_job.py -q -k options`
Expected: FAIL (`options` not passed / `LADDER_NUM_PREDICT` missing).

- [ ] **Step 3: Implement**

`loop_job.py`:

```python
# Same role as chat_context.OUTPUT_RESERVE for the chat: one attempt's reply
# is bounded, so a looping model costs at most this many tokens.
LADDER_NUM_PREDICT = 8192
```

DEFAULT_LADDER (line 45-48): change the 30b stage to `{"model": "qwen3-coder:30b", "temperature": 0.7, "attempts": 3}` (model-card value; 0.1 was the quasi-greedy trap). Same edit in `loop.py` lines 39-42.

`_run_ladder` signature (line 134): add `models=None` after `runner=pytest_runner`. In the attempt loop (line 168), replace the `chat_fn` call:

```python
            options = model_options(models or {}, model)
            options.pop("temperature", None)  # stage escalation temp wins
            options["num_predict"] = LADDER_NUM_PREDICT
            resp = chat_fn(model, system,
                           build_prompt(goal, current, context, last_output, lang),
                           temperature=stage.get("temperature", 0.1),
                           num_ctx=num_ctx, options=options)
```

`_review` (line 112): add a `models=None` kwarg and the same options treatment (profile minus temperature, plus `num_predict`), keeping `temperature=0.1`.

`_execute` (line 288): after `project = resolve_project(...)`, add `models = load_models(config_path)`; pass `models=models` to `_run_ladder` (line 315) and `models=models` to `_review` (line 329). Import `load_models, model_options` from `factory_config` (the module already imports from it).

`loop.py` `run_task` (line 140): add `options={"num_predict": 8192}` to the `chat()` call. No profile plumbing here — `loop.py` is the standalone task runner, `loop_job.py` is the production path (YAGNI).

- [ ] **Step 4: Run tests, full suite**

Run: `python -m pytest harness/tests/test_loop_job.py -q` → pass. Existing fakes that reject the new `options` kw: extend them with `**kw`, do not weaken assertions.
Run: `python -m pytest harness/tests -q` → green.

- [ ] **Step 5: Commit**

```bash
git add harness/loop_job.py harness/loop.py harness/tests/test_loop_job.py
git commit -m "feat: ladder attempts use sampling profiles and a generation cap"
```

---

# Phase B — MCP client + toolsets (chat agent only)

### Task 5: MCP stdio client + fake server fixture

**Files:**
- Create: `harness/mcp_client.py`, `harness/tests/fake_mcp_server.py`
- Test: `harness/tests/test_mcp_client.py`

**Interfaces:**
- Produces: `McpServer(name, command, cwd=None, env=None)` with `.start()`, `.alive()`, `.close()`, `.tools` (list of `{name, description, inputSchema}` after start), `.call(tool, arguments, timeout=60) -> str` (string result, string `"error: ..."` on any failure, lazy restart of a dead server). `McpError` exception (internal).

- [ ] **Step 1: Write the fake server** (`harness/tests/fake_mcp_server.py` — a real subprocess target, so the client is tested over actual pipes)

```python
"""Minimal MCP stdio server for tests: initialize, tools/list, tools/call.

Tools: echo (returns its text argument), sleepy (sleeps `seconds`, then
answers "woke"), boom (isError result), die (exits mid-call).
"""
import json
import sys
import time

TOOLS = [
    {"name": "echo", "description": "echo text back",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "sleepy", "description": "sleep then answer",
     "inputSchema": {"type": "object",
                     "properties": {"seconds": {"type": "number"}}}},
    {"name": "boom", "description": "always fails",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "die", "description": "kill the server",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _reply(req_id, result):
    sys.stdout.write(json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method, req_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            _reply(req_id, {"protocolVersion":
                            msg["params"]["protocolVersion"],
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake", "version": "0"}})
        elif method == "tools/list":
            _reply(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments") or {}
            if name == "die":
                sys.exit(1)
            if name == "sleepy":
                time.sleep(float(args.get("seconds", 0)))
                _reply(req_id, {"content": [{"type": "text", "text": "woke"}]})
            elif name == "boom":
                _reply(req_id, {"content": [{"type": "text", "text": "kaputt"}],
                                "isError": True})
            else:
                _reply(req_id, {"content":
                                [{"type": "text", "text": args.get("text", "")}]})
        # notifications (no id) are consumed silently


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the failing tests** (`harness/tests/test_mcp_client.py`)

```python
import sys
from pathlib import Path

import pytest

from mcp_client import McpServer

FAKE = [sys.executable, str(Path(__file__).with_name("fake_mcp_server.py"))]


@pytest.fixture
def server():
    srv = McpServer("fake", FAKE)
    yield srv
    srv.close()


def test_start_lists_the_tools(server):
    server.start()
    assert [t["name"] for t in server.tools] == ["echo", "sleepy", "boom", "die"]


def test_call_returns_the_text_content(server):
    assert server.call("echo", {"text": "bonjour"}) == "bonjour"


def test_call_starts_the_server_lazily(server):
    # no explicit start(): the first call spawns and handshakes
    assert server.call("echo", {"text": "lazy"}) == "lazy"
    assert server.alive()


def test_is_error_results_become_error_strings(server):
    out = server.call("boom", {})
    assert out.startswith("error: ") and "kaputt" in out


def test_a_timeout_is_a_string_not_an_exception(server):
    out = server.call("sleepy", {"seconds": 5}, timeout=1)
    assert out.startswith("error: ") and "timeout" in out


def test_a_dead_server_restarts_on_the_next_call(server):
    assert server.call("echo", {"text": "a"}) == "a"
    out = server.call("die", {})
    assert out.startswith("error: ")
    assert server.call("echo", {"text": "b"}) == "b"  # restarted


def test_a_missing_command_is_a_string_error():
    srv = McpServer("ghost", ["definitely-not-a-command-xyz"])
    out = srv.call("anything", {})
    assert out.startswith("error: ")
```

- [ ] **Step 3: Run to verify they fail**

Run: `python -m pytest harness/tests/test_mcp_client.py -q`
Expected: `ModuleNotFoundError: No module named 'mcp_client'`.

- [ ] **Step 4: Implement `harness/mcp_client.py`**

```python
"""Stdlib-only MCP client over stdio (JSON-RPC 2.0, one message per line).

The factory now speaks MCP in both directions: factory_mcp.py SERVES the
factory to Claude; this module lets the local chat agent CONSUME external
MCP servers (git, fetch, playwright...). Same hand-rolled transport, same
reason: there is no `mcp` SDK for Python 3.9.

Contract mirrors agent_tools: call() returns a STRING the model reads, and
every failure -- missing command, crash, timeout, isError result -- is
folded into that string, never raised. The agent loop must keep going.

A reader thread drains stdout into a queue: Windows pipes cannot be
select()ed, and a blocking readline would turn a slow server into a hung
agent turn.
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import time

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 60
SPAWN_TIMEOUT = 30


class McpError(Exception):
    pass


def _text(result):
    """Flatten a tools/call result into the string the model reads."""
    parts = [c.get("text", "") for c in result.get("content") or []
             if c.get("type") == "text"]
    text = "\n".join(p for p in parts if p) or "(no output)"
    if result.get("isError"):
        return "error: " + text
    return text


class McpServer:
    """One spawned MCP server process and its handshake state."""

    def __init__(self, name, command, cwd=None, env=None):
        self.name = name
        self.command = list(command)
        self.cwd = cwd
        self.env = env
        self.proc = None
        self.tools = []      # tools/list result, set by start()
        self._id = 0
        self._replies = None
        self._lock = threading.Lock()

    def start(self):
        exe = shutil.which(self.command[0])  # resolves npx -> npx.cmd on nt
        if exe is None:
            raise McpError("command not found: {}".format(self.command[0]))
        env = dict(os.environ, **(self.env or {}))
        self._replies = queue.Queue()
        self.proc = subprocess.Popen(
            [exe] + self.command[1:], cwd=self.cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        threading.Thread(target=self._read_forever, args=(self.proc.stdout,),
                         daemon=True).start()
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "local-factory", "version": "1.0"},
        }, timeout=SPAWN_TIMEOUT)
        self._notify("notifications/initialized")
        self.tools = self._rpc("tools/list", {},
                               timeout=SPAWN_TIMEOUT).get("tools", [])

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def close(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        self.tools = []

    def call(self, tool, arguments, timeout=DEFAULT_TIMEOUT):
        """String result, string errors; a dead server restarts once."""
        try:
            if not self.alive():
                self.close()
                self.start()
            return _text(self._rpc(
                "tools/call", {"name": tool, "arguments": arguments or {}},
                timeout=timeout))
        except (McpError, OSError, ValueError) as e:
            return "error: mcp server {}: {}".format(self.name, e)

    # ---- transport ----

    def _read_forever(self, stdout):
        replies = self._replies
        for line in stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                continue  # some servers log to stdout; skip the junk
            if "id" in msg and "method" not in msg:
                replies.put(msg)
            # server-initiated requests/notifications are ignored in v1

    def _send(self, msg):
        data = (json.dumps(msg) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def _rpc(self, method, params, timeout=DEFAULT_TIMEOUT):
        with self._lock:
            self._id += 1
            req_id = self._id
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method,
                        "params": params})
            return self._read_until(req_id, timeout)

    def _read_until(self, req_id, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError("timeout after {}s".format(timeout))
            try:
                msg = self._replies.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if not self.alive():
                    raise McpError("server exited")
                continue
            if msg.get("id") != req_id:
                continue  # stale reply from a timed-out earlier call
            if "error" in msg:
                err = msg["error"] or {}
                raise McpError("{} (code {})".format(
                    err.get("message", "?"), err.get("code")))
            return msg.get("result") or {}
```

- [ ] **Step 5: Run tests, full suite**

Run: `python -m pytest harness/tests/test_mcp_client.py -q` → all pass (the timeout test takes ~1 s, the sleepy server dies on `close()`).
Run: `python -m pytest harness/tests -q` → green.

- [ ] **Step 6: Commit**

```bash
git add harness/mcp_client.py harness/tests/fake_mcp_server.py harness/tests/test_mcp_client.py
git commit -m "feat: stdlib MCP stdio client with string-error contract"
```

---

### Task 6: MCP server config + registry

**Files:**
- Modify: `harness/factory_config.py`
- Create: `harness/mcp_registry.py`
- Test: `harness/tests/test_mcp_registry.py`, additions to `harness/tests/test_factory_config.py`

**Interfaces:**
- Consumes: `McpServer` (Task 5).
- Produces: `factory_config.load_mcp_servers(config_path) -> dict` (name → `McpServerConfig(name, command, tools, readonly, env)` namedtuple); `mcp_registry.McpRegistry(configs)` with `.tool_specs(server_names, workspace) -> list` (Ollama tool defs, namespaced), `.needs_approval(name) -> bool`, `.run(name, arguments, workspace) -> str`, `.close()`; `mcp_registry.PREFIX = "mcp__"`, `mcp_registry.tool_name(server, tool)`, `mcp_registry.split(name)`, `mcp_registry.TOOL_BUDGET = 15`.

- [ ] **Step 1: Write the failing config tests** (append to `test_factory_config.py`)

```python
def test_load_mcp_servers_parses_and_validates(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[mcp.servers.git]\n'
                   'command = ["uvx", "mcp-server-git"]\n'
                   'tools = ["git_status", "git_commit"]\n'
                   'readonly = ["git_status"]\n', encoding="utf-8")

    servers = factory_config.load_mcp_servers(cfg)

    assert servers["git"].command == ["uvx", "mcp-server-git"]
    assert servers["git"].tools == ["git_status", "git_commit"]
    assert servers["git"].readonly == ["git_status"]


def test_load_mcp_servers_refuses_readonly_outside_tools(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[mcp.servers.x]\ncommand = ["x"]\ntools = ["a"]\n'
                   'readonly = ["b"]\n', encoding="utf-8")
    with pytest.raises(factory_config.ConfigError):
        factory_config.load_mcp_servers(cfg)


def test_load_mcp_servers_refuses_a_missing_command(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[mcp.servers.x]\ntools = ["a"]\n', encoding="utf-8")
    with pytest.raises(factory_config.ConfigError):
        factory_config.load_mcp_servers(cfg)
```

- [ ] **Step 2: Write the failing registry tests** (`harness/tests/test_mcp_registry.py` — uses the real fake server from Task 5)

```python
import sys
from pathlib import Path

import pytest

from factory_config import McpServerConfig
from mcp_registry import McpRegistry, PREFIX, split, tool_name

FAKE = [sys.executable, str(Path(__file__).with_name("fake_mcp_server.py"))]


@pytest.fixture
def registry():
    cfg = McpServerConfig("fake", FAKE, ["echo", "boom"], ["echo"], {})
    reg = McpRegistry({"fake": cfg})
    yield reg
    reg.close()


def test_names_roundtrip():
    assert tool_name("git", "git_status") == "mcp__git__git_status"
    assert split("mcp__git__git_status") == ("git", "git_status")
    assert split("read_file") is None


def test_tool_specs_expose_only_the_allowlist(registry, tmp_path):
    specs = registry.tool_specs(["fake"], str(tmp_path))

    names = [s["function"]["name"] for s in specs]
    assert names == ["mcp__fake__echo", "mcp__fake__boom"]  # not sleepy/die
    echo = specs[0]["function"]
    assert echo["description"] == "echo text back"
    assert echo["parameters"]["required"] == ["text"]


def test_run_routes_to_the_server(registry, tmp_path):
    out = registry.run("mcp__fake__echo", {"text": "hi"}, str(tmp_path))
    assert out == "hi"


def test_run_refuses_tools_outside_the_allowlist(registry, tmp_path):
    out = registry.run("mcp__fake__die", {}, str(tmp_path))
    assert out.startswith("error: tool not allowed")


def test_needs_approval_honors_readonly(registry):
    assert registry.needs_approval("mcp__fake__echo") is False
    assert registry.needs_approval("mcp__fake__boom") is True
    assert registry.needs_approval("mcp__nope__x") is True


def test_a_broken_server_costs_nothing_at_spec_time(tmp_path):
    cfg = McpServerConfig("ghost", ["no-such-command-xyz"], ["a"], [], {})
    reg = McpRegistry({"ghost": cfg})
    assert reg.tool_specs(["ghost"], str(tmp_path)) == []
```

- [ ] **Step 3: Run to verify they fail**

Run: `python -m pytest harness/tests/test_mcp_registry.py harness/tests/test_factory_config.py -q`
Expected: import errors on `McpServerConfig` / `mcp_registry`.

- [ ] **Step 4: Implement**

`factory_config.py` additions:

```python
McpServerConfig = namedtuple("McpServerConfig", "name command tools readonly env")


def load_mcp_servers(config_path):
    """[mcp.servers.<name>] tables: the perimeter of external tools, same
    philosophy as [projects] -- only what is named here can ever be spawned."""
    with open(config_path, "rb") as f:
        raw = tomllib.load(f).get("mcp", {}).get("servers", {})
    out = {}
    for name, entry in raw.items():
        command = entry.get("command")
        if not isinstance(command, list) or not command:
            raise ConfigError("[mcp.servers.{}]: command must be a non-empty "
                              "array".format(name))
        tools = list(entry.get("tools") or [])
        readonly = list(entry.get("readonly") or [])
        stray = set(readonly) - set(tools)
        if stray:
            raise ConfigError("[mcp.servers.{}]: readonly lists tools outside "
                              "the allowlist: {}".format(
                                  name, ", ".join(sorted(stray))))
        out[name] = McpServerConfig(name, list(command), tools, readonly,
                                    dict(entry.get("env") or {}))
    return out
```

`harness/mcp_registry.py`:

```python
"""Bridges configured MCP servers into the agent tool loop.

One McpServer per (server, workspace): git and playwright act on the
session's workspace, so two sessions must never share a process. Tool
names are namespaced mcp__<server>__<tool>; the factory.toml allowlist
decides what the model ever sees, and `readonly` decides what skips the
approval gate.
"""
import sys
import threading

from mcp_client import McpError, McpServer

PREFIX = "mcp__"
# Past this many exposed tools, <=35b models pick tools measurably worse;
# the caller warns, it does not refuse (the operator may know better).
TOOL_BUDGET = 15


def tool_name(server, tool):
    return PREFIX + server + "__" + tool


def split(name):
    """("server", "tool") from a namespaced name, else None."""
    if not name.startswith(PREFIX):
        return None
    rest = name[len(PREFIX):]
    if "__" not in rest:
        return None
    server, tool = rest.split("__", 1)
    return server, tool


class McpRegistry:
    def __init__(self, configs):
        self.configs = configs  # name -> McpServerConfig
        self._servers = {}      # (name, workspace) -> McpServer
        self._lock = threading.Lock()

    def _server(self, name, workspace):
        key = (name, workspace)
        with self._lock:
            srv = self._servers.get(key)
            if srv is None:
                cfg = self.configs[name]
                srv = McpServer(name, cfg.command, cwd=workspace, env=cfg.env)
                self._servers[key] = srv
            return srv

    def tool_specs(self, names, workspace):
        """Ollama tool definitions for the allowlisted tools of `names`.
        tools/list carries the schemas, so this spawns lazily; a server
        that will not start costs a stderr line, never the session."""
        specs = []
        for name in names:
            cfg = self.configs.get(name)
            if cfg is None:
                continue
            srv = self._server(name, workspace)
            if not srv.alive():
                try:
                    srv.close()
                    srv.start()
                except (McpError, OSError) as e:
                    print("mcp server {} unavailable: {}".format(name, e),
                          file=sys.stderr)
                    continue
            by_name = {t.get("name"): t for t in srv.tools}
            for tool in cfg.tools:
                spec = by_name.get(tool)
                if spec is None:
                    continue
                specs.append({"type": "function", "function": {
                    "name": tool_name(name, tool),
                    "description": spec.get("description", ""),
                    "parameters": spec.get("inputSchema") or
                                  {"type": "object", "properties": {}},
                }})
        return specs

    def needs_approval(self, name):
        parts = split(name)
        if parts is None:
            return True
        cfg = self.configs.get(parts[0])
        return cfg is None or parts[1] not in cfg.readonly

    def run(self, name, arguments, workspace):
        parts = split(name)
        if parts is None:
            return "error: unknown tool: {}".format(name)
        server, tool = parts
        cfg = self.configs.get(server)
        if cfg is None or tool not in cfg.tools:
            return "error: tool not allowed: {}".format(name)
        return self._server(server, workspace).call(tool, arguments)

    def close(self):
        with self._lock:
            for srv in self._servers.values():
                srv.close()
            self._servers.clear()
```

- [ ] **Step 5: Run tests, full suite**

Run: `python -m pytest harness/tests/test_mcp_registry.py harness/tests/test_factory_config.py -q` → pass.
Run: `python -m pytest harness/tests -q` → green.

- [ ] **Step 6: Commit**

```bash
git add harness/factory_config.py harness/mcp_registry.py harness/tests/test_mcp_registry.py harness/tests/test_factory_config.py
git commit -m "feat: mcp server perimeter in factory.toml and namespaced tool registry"
```

---

### Task 7: Toolsets per session, wired into the agent turn

**Files:**
- Modify: `harness/factory_config.py`, `harness/factory_mcp.py` (`Factory.__init__` line 297, `chat_create` line 507, `_agent_system` line 268, `_run_call` line 683, `_drain_calls` line 708, `_agent_turn` line 770, `agent_reply` line 849), `harness/chat_store.py:28-37`, `harness/factory_web.py` (`_chat_create` line 183, routes line 280), `harness/web/app.js` (new-session form, lines ~685-710)
- Test: `harness/tests/test_factory_mcp.py`, `harness/tests/test_chat_store.py`, `harness/tests/test_factory_config.py`

**Interfaces:**
- Consumes: `McpRegistry` (Task 6), `agent_tools.TOOLS/needs_approval/run`.
- Produces: `factory_config.load_toolsets(config_path, mcp_servers=None) -> dict` (name → `Toolset(name, native, mcp)`; always contains `"base"` = the 6 natives, no MCP); `Factory.chat_create(..., toolset=None)`; sessions store `"toolset"`; `Factory.toolsets()` (list for the UI); GET `/api/toolsets`.

- [ ] **Step 1: Write the failing config test** (append to `test_factory_config.py`)

```python
def test_load_toolsets_always_has_base_and_validates(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[mcp.servers.git]\ncommand = ["uvx", "mcp-server-git"]\n'
                   'tools = ["git_status"]\n'
                   '[toolsets.dev]\nmcp = ["git"]\n', encoding="utf-8")

    ts = factory_config.load_toolsets(
        cfg, mcp_servers=factory_config.load_mcp_servers(cfg))

    assert ts["base"].native == list(factory_config.NATIVE_TOOLS)
    assert ts["base"].mcp == []
    assert ts["dev"].native == list(factory_config.NATIVE_TOOLS)  # default
    assert ts["dev"].mcp == ["git"]


def test_load_toolsets_refuses_unknown_native_or_server(tmp_path):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[toolsets.bad]\nnative = ["rm_rf"]\n', encoding="utf-8")
    with pytest.raises(factory_config.ConfigError):
        factory_config.load_toolsets(cfg)

    cfg.write_text('[toolsets.bad]\nmcp = ["ghost"]\n', encoding="utf-8")
    with pytest.raises(factory_config.ConfigError):
        factory_config.load_toolsets(cfg, mcp_servers={})
```

- [ ] **Step 2: Write the failing factory tests** (append to `test_factory_mcp.py`; reuse `_factory_with_profiles`-style local config)

```python
def _factory_with_mcp(tmp_path):
    fake = str(Path(__file__).with_name("fake_mcp_server.py"))
    cfg = tmp_path / "factory.toml"
    cfg.write_text(
        '[mcp.servers.fake]\n'
        'command = ["{}", "{}"]\n'
        'tools = ["echo", "boom"]\nreadonly = ["echo"]\n'
        '[toolsets.dev]\nmcp = ["fake"]\n'.format(
            sys.executable.replace("\\", "/"), fake.replace("\\", "/")),
        encoding="utf-8")
    return factory_mcp.Factory(tmp_path / "jobs", cfg)


def test_chat_create_validates_the_toolset(tmp_path):
    factory = _factory_with_mcp(tmp_path)
    with pytest.raises(ToolError):
        factory.chat_create("m:1", kind="agent", toolset="nope",
                            workspace=str(tmp_path))
    s = factory.chat_create("m:1", kind="agent", toolset="dev",
                            workspace=str(tmp_path))
    assert factory.chat_get(s["session_id"])["toolset"] == "dev"


def test_agent_turn_exposes_native_plus_mcp_tools(tmp_path, monkeypatch):
    factory = _factory_with_mcp(tmp_path)
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1", kind="agent", mode="auto",
                              toolset="dev",
                              workspace=str(tmp_path))["session_id"]

    list(factory.agent_reply(sid, "salut"))

    names = [t["function"]["name"] for t in fake.state["calls"][0]["tools"]]
    assert "read_file" in names and "mcp__fake__echo" in names
    assert "mcp__fake__die" not in names


def test_agent_runs_an_mcp_tool_and_gates_non_readonly(tmp_path, monkeypatch):
    factory = _factory_with_mcp(tmp_path)
    fake = _fake_agent_stream([
        [("tool_call", {"name": "mcp__fake__echo",
                        "arguments": {"text": "hi"}})],
        [("tool_call", {"name": "mcp__fake__boom", "arguments": {}})],
        [("content", "done")],
    ])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1", kind="agent", mode="approve",
                              toolset="dev",
                              workspace=str(tmp_path))["session_id"]

    events = list(factory.agent_reply(sid, "go"))

    results = [p for k, p in events if k == "tool_result"]
    assert results and results[0]["output"] == "hi"  # readonly: no gate
    assert events[-1][0] == "approval_needed"        # boom: gated
```

- [ ] **Step 3: Run to verify they fail**

Run: `python -m pytest harness/tests/test_factory_mcp.py -q -k "toolset or mcp"`
Expected: `TypeError: chat_create() got an unexpected keyword argument 'toolset'`.

- [ ] **Step 4: Implement**

`factory_config.py`:

```python
NATIVE_TOOLS = ("list_dir", "read_file", "search", "write_file", "edit_file",
                "run_command")

Toolset = namedtuple("Toolset", "name native mcp")


def load_toolsets(config_path, mcp_servers=None):
    """[toolsets.<name>]: which native tools and which MCP servers a session
    exposes. "base" (all natives, no MCP) always exists -- it is the current
    behavior and the default."""
    with open(config_path, "rb") as f:
        raw = tomllib.load(f).get("toolsets", {})
    out = {"base": Toolset("base", list(NATIVE_TOOLS), [])}
    for name, entry in raw.items():
        native = list(entry.get("native", NATIVE_TOOLS))
        bad = set(native) - set(NATIVE_TOOLS)
        if bad:
            raise ConfigError("[toolsets.{}]: unknown native tool(s): {}".format(
                name, ", ".join(sorted(bad))))
        mcp = list(entry.get("mcp") or [])
        if mcp_servers is not None:
            missing = set(mcp) - set(mcp_servers)
            if missing:
                raise ConfigError("[toolsets.{}]: unknown mcp server(s): {}".format(
                    name, ", ".join(sorted(missing))))
        out[name] = Toolset(name, native, mcp)
    return out
```

`chat_store.py` `create()` (line 28): add `toolset=None` kwarg; inside the `kind == "agent"` branch add `toolset=toolset or "base"` to the `session.update(...)` call.

`factory_mcp.py`:

- Imports: add `load_mcp_servers, load_toolsets` to the `factory_config` import; add `import mcp_registry`.
- `Factory.__init__`: add

```python
        try:
            self.mcp = mcp_registry.McpRegistry(
                load_mcp_servers(self.config_path))
        except (OSError, ConfigError) as e:
            print("mcp config unavailable: {}".format(e), file=sys.stderr)
            self.mcp = mcp_registry.McpRegistry({})
```

- New helpers on `Factory`:

```python
    def _toolsets(self):
        try:
            return load_toolsets(self.config_path,
                                 mcp_servers=self.mcp.configs)
        except (OSError, ConfigError) as e:
            print("toolsets unavailable: {}".format(e), file=sys.stderr)
            return load_toolsets(os.devnull)  # just "base"

    def toolsets(self):
        return {"toolsets": sorted(self._toolsets())}

    def _session_tools(self, session):
        """(ollama tool defs, names) for this session's toolset."""
        ts = self._toolsets().get(session.get("toolset") or "base")
        if ts is None:
            ts = self._toolsets()["base"]
        tools = [t for t in agent_tools.TOOLS
                 if t["function"]["name"] in ts.native]
        tools += self.mcp.tool_specs(ts.mcp, session["workspace"])
        if len(tools) > mcp_registry.TOOL_BUDGET:
            print("toolset {}: {} tools exposed (budget {}) -- small models "
                  "degrade past it".format(ts.name, len(tools),
                                           mcp_registry.TOOL_BUDGET),
                  file=sys.stderr)
        return tools

    def _needs_approval(self, name):
        if name.startswith(mcp_registry.PREFIX):
            return self.mcp.needs_approval(name)
        return agent_tools.needs_approval(name)
```

- `chat_create` (line 507): add `toolset=None` param; in the agent branch, before creating:

```python
            if toolset is not None and toolset not in self._toolsets():
                raise ToolError("unknown toolset: {!r}".format(toolset))
```

and pass `toolset=toolset` to `self.chats.create(...)`.

- `_run_call` (line 685): route by prefix:

```python
        if call["name"].startswith(mcp_registry.PREFIX):
            output = self.mcp.run(call["name"], call.get("arguments") or {},
                                  workspace)
        else:
            output = agent_tools.run(call["name"],
                                     call.get("arguments") or {}, workspace)
```

- `_drain_calls` (line 708-709) and `agent_reply` (line 849-850): replace `agent_tools.needs_approval(...)` with `self._needs_approval(...)`.
- `_agent_turn` (line 770-772): compute `tools = self._session_tools(session)` once before the stream call and pass `tools=tools`. Update the existing test assertion `fake.state["calls"][0]["tools"] is factory_mcp.agent_tools.TOOLS` (test_factory_mcp.py:678) to compare names instead:

```python
    assert [t["function"]["name"] for t in fake.state["calls"][0]["tools"]] \
        == [t["function"]["name"] for t in factory_mcp.agent_tools.TOOLS]
```

- `_agent_system` (line 268): add a `tool_names=None` kwarg; when given, append `"Tools available this session: " + ", ".join(tool_names)` as a final part. In `_agent_turn`, call `_agent_system(session["workspace"], [t["function"]["name"] for t in tools])`. The doc the model reads is generated from the real toolset — never a stale list.

`factory_web.py` `_chat_create` (line 183): forward `toolset` from the body like `mode`/`workspace`. Add route + handler:

```python
    def _toolsets(self, query=None):
        self._json(200, self.server.factory.toolsets())
```
```python
    (r"/api/toolsets", "GET", Handler._toolsets),
```

`web/app.js` (~line 692): mirror the `modeSel` pattern — add a `toolsetSel` `<select>` shown only when `kindSel.value === "agent"`, populated from `GET /api/toolsets` when the new-session form opens, defaulting to `"base"`; include `body.toolset = toolsetSel.value` next to `body.kind = "agent"` (line 703). Read the surrounding form code first and copy its style exactly.

- [ ] **Step 5: Run tests, full suite**

Run: `python -m pytest harness/tests/test_factory_mcp.py harness/tests/test_chat_store.py harness/tests/test_factory_config.py harness/tests/test_factory_web.py -q` → pass.
Run: `python -m pytest harness/tests -q` → green.

- [ ] **Step 6: Commit**

```bash
git add harness/factory_config.py harness/factory_mcp.py harness/chat_store.py harness/factory_web.py harness/web/app.js harness/tests
git commit -m "feat: per-session toolsets expose curated mcp tools to the chat agent"
```

---

### Task 8: v1 servers in factory.toml + manual smoke

**Files:**
- Modify: `factory.toml`, `agent.md` (note on MCP tools if it documents tools)

- [ ] **Step 1: Check the prerequisites**

Run: `npx --version` and `uvx --version`.
If `uvx` is missing: `pip install uv` (or winget) — or swap the two `uvx` entries below for `python -m` equivalents after `pip install mcp-server-fetch mcp-server-git`.

- [ ] **Step 2: Append the server + toolset config to `factory.toml`**

```toml
# External MCP servers (spec 2026-07-15, B5). `tools` is the allowlist --
# playwright alone ships ~25 tools, exposing them all would bust the ~15-tool
# budget small models can route reliably. `readonly` skips the approval gate.
[mcp.servers.fetch]
command = ["uvx", "mcp-server-fetch"]
tools = ["fetch"]
readonly = ["fetch"]

[mcp.servers.search]
command = ["npx", "-y", "duckduckgo-mcp-server"]
tools = ["search", "fetch_content"]
readonly = ["search", "fetch_content"]

[mcp.servers.git]
command = ["uvx", "mcp-server-git"]
tools = ["git_status", "git_diff_unstaged", "git_diff_staged", "git_log",
         "git_add", "git_commit"]
readonly = ["git_status", "git_diff_unstaged", "git_diff_staged", "git_log"]

[mcp.servers.playwright]
command = ["npx", "-y", "@playwright/mcp@latest"]
tools = ["browser_navigate", "browser_snapshot", "browser_click",
         "browser_type", "browser_press_key", "browser_take_screenshot",
         "browser_console_messages", "browser_wait_for"]
readonly = ["browser_snapshot", "browser_take_screenshot",
            "browser_console_messages"]

[toolsets.dev]
mcp = ["git", "fetch"]

[toolsets.web]
mcp = ["fetch", "search", "playwright"]
```

Verify each server's real tool names on first spawn (`tools/list` is authoritative): run a throwaway
`python -c "import sys; sys.path.insert(0, 'harness'); from mcp_client import McpServer; s = McpServer('x', ['uvx', 'mcp-server-git']); s.start(); print([t['name'] for t in s.tools])"`
for each server and fix the allowlists if a name differs. `duckduckgo-mcp-server` is unvetted npm — if its tool names or behavior disappoint, substitute another keyless search MCP server and record the choice in the commit message.

- [ ] **Step 3: Manual smoke via the Studio**

1. Start the web studio (existing command, see README).
2. Create an agent session, toolset `web`, mode `approve`.
3. Ask: `récupère le titre de https://example.com`. Expect a `mcp__fetch__fetch` call (readonly, no gate) and a correct answer.
4. Create a session with toolset `dev` in a git repo; ask for `git status`; expect `mcp__git__git_status` with no approval gate; then ask it to commit something and expect the approval gate.

- [ ] **Step 4: Commit**

```bash
git add factory.toml agent.md
git commit -m "feat: fetch, search, git and playwright mcp servers in the perimeter"
```

---

# Phase C — Blitzvolley pilot

### Task 9: `node` runner (node --test)

**Files:**
- Create: `harness/node_test_runner.py`
- Modify: `harness/factory_config.py:17` (SUPPORTED_RUNNERS), `:84-87` (_COLLECTABLE), `harness/loop_job.py` (RUNNERS module map near its imports)
- Test: `harness/tests/test_node_test_runner.py`

**Interfaces:**
- Consumes: node >= 18 on PATH.
- Produces: module with the same surface as `vitest_runner`: `materialize_tests(tests_dir, tests, worktree, project_path=None)`, `run_tests(worktree, tests_dir, timeout=600)`, `run_regression(worktree, argv, timeout=900)`, `cleanup(worktree, tests_dir)`, `Result` namedtuple, `OPERATOR_ERROR_CODES = ()`.

- [ ] **Step 1: Write the failing tests** (`harness/tests/test_node_test_runner.py`; mirror the structure of `test_vitest_runner.py` — read it first and copy its fixtures where they apply. Core cases:)

```python
def test_run_tests_passes_a_green_suite(tmp_path):
    # worktree with a module + spec test that requires it via NODE_PATH
    worktree = tmp_path / "wt"
    (worktree / "lib").mkdir(parents=True)
    (worktree / "lib" / "add.js").write_text(
        "module.exports = (a, b) => a + b;\n", encoding="utf-8")
    tests = {"spec.test.js":
             'const test = require("node:test");\n'
             'const assert = require("node:assert");\n'
             'const add = require("lib/add.js");\n'
             'test("adds", () => assert.strictEqual(add(1, 2), 3));\n'}
    tests_dir = node_test_runner.materialize_tests(
        tmp_path / "tests", tests, worktree)

    res = node_test_runner.run_tests(worktree, tests_dir)

    assert res.passed and res.tests_passed == 1 and res.tests_failed == 0


def test_run_tests_counts_failures(tmp_path):
    # same skeleton, assert.strictEqual(add(1, 2), 4) -> passed False,
    # tests_failed == 1
    ...


def test_an_empty_collection_is_not_green(tmp_path):
    # materialize no *.test.js -> res.passed is False even on exit 0
    ...
```

Write the two elided tests in full (copy the first one's skeleton). 

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest harness/tests/test_node_test_runner.py -q`
Expected: `ModuleNotFoundError: No module named 'node_test_runner'`.

- [ ] **Step 3: Implement `harness/node_test_runner.py`**

```python
"""node:test counterpart of vitest_runner, for projects on the built-in
runner (blitzvolley). Same threat model (ADR-010): spec tests live in
jobs/<id>/tests/, never in the worktree.

Resolution: spec tests are CJS and require worktree files through
NODE_PATH=<worktree> (repo-relative ids: require("backend/game/score")).
The project's node_modules is junctioned into both the worktree and the
tests dir, exactly like vitest_runner and for the same reasons.

Counts come from the TAP reporter: the human-facing default reporter is
unicode-decorated and unstable across node versions; TAP's trailing
"# pass N" / "# fail N" lines are not.
"""
import os
import re
import subprocess
from collections import namedtuple
from pathlib import Path

from vitest_runner import _link_node_modules, _unlink_node_modules

DEFAULT_TIMEOUT = 600
REGRESSION_TIMEOUT = 900
OPERATOR_ERROR_CODES = ()

Result = namedtuple("Result", "passed tests_passed tests_failed output timed_out returncode")


def materialize_tests(tests_dir, tests, worktree, project_path=None):
    tests_dir = Path(tests_dir)
    tests_dir.mkdir(parents=True, exist_ok=True)
    for relpath, content in tests.items():
        dst = tests_dir / relpath
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
    if project_path:
        source = Path(project_path) / "node_modules"
        if not source.is_dir():
            raise FileNotFoundError(
                "{} has no node_modules: run npm install in the project "
                "before delegating to it".format(project_path))
        _link_node_modules(source, Path(worktree).resolve())
        _link_node_modules(source, tests_dir)
    return tests_dir


def cleanup(worktree, tests_dir):
    """Detach the node_modules links. Call BEFORE WorkTree.remove()."""
    _unlink_node_modules(worktree)
    _unlink_node_modules(tests_dir)


def _counts(out):
    passed = int(g.group(1)) if (g := re.search(r"^# pass (\d+)", out, re.M)) else 0
    failed = int(g.group(1)) if (g := re.search(r"^# fail (\d+)", out, re.M)) else 0
    return passed, failed


def _env(worktree):
    env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}
    env["NODE_PATH"] = str(worktree)  # spec tests resolve worktree files
    env["CI"] = "1"
    env["FORCE_COLOR"] = "0"
    return env


def _run(cmd, cwd, env, timeout):
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + "\n" + (e.stderr or "")
        return Result(False, 0, 0, out + "\nTIMEOUT after {}s".format(timeout),
                      True, -1)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    npassed, nfailed = _counts(out)
    # A suite that collected nothing is not a green suite.
    passed = p.returncode == 0 and npassed > 0
    return Result(passed, npassed, nfailed, out, False, p.returncode)


def run_tests(worktree, tests_dir, timeout=DEFAULT_TIMEOUT):
    tests_dir = Path(tests_dir)
    cmd = ["node", "--test", "--test-reporter=tap", str(tests_dir)]
    return _run(cmd, tests_dir, _env(Path(worktree).resolve()), timeout)


def run_regression(worktree, argv, timeout=REGRESSION_TIMEOUT):
    """The project's own suite, full command, inside the worktree (same
    contract as vitest_runner.run_regression)."""
    return _run(list(argv), worktree, _env(Path(worktree).resolve()), timeout)
```

Note: `run_regression` reuses `_counts`; a regression command that is not TAP simply reports 0/0 and the exit code decides — but `_run` requires `npassed > 0` for green. For regression, exit code alone must decide (the project's reporter is not ours). Split it:

```python
def run_regression(worktree, argv, timeout=REGRESSION_TIMEOUT):
    res = _run(list(argv), worktree, _env(Path(worktree).resolve()), timeout)
    # The project's own reporter may not be TAP: exit code decides green.
    if res.returncode == 0 and not res.timed_out:
        return res._replace(passed=True)
    return res
```

Check how `pytest_runner.run_regression` and `vitest_runner.run_regression` results are consumed in `loop_job.py` before finalizing this; match their green criterion exactly.

- [ ] **Step 4: Register the runner**

`factory_config.py:17`: `SUPPORTED_RUNNERS = ("pytest", "vitest", "node")`. `_COLLECTABLE` (line 84): add `"node": r".+\.test\.[cm]?js"`. Update the `validate_tests` error hint (line 108) to mention `*.test.js` for node.

`loop_job.py`: find the `RUNNERS` dict that maps runner names to modules (near the imports; `_execute` line 293 uses it), add `import node_test_runner` and `"node": node_test_runner`.

Add a config test (append to `test_factory_config.py`):

```python
def test_node_runner_is_supported_and_collects_test_js(tmp_path):
    assert "node" in factory_config.SUPPORTED_RUNNERS
    factory_config.validate_tests(["spec.test.js"], "node")  # no raise
    with pytest.raises(factory_config.NoTests):
        factory_config.validate_tests(["spec.js"], "node")
```

- [ ] **Step 5: Run tests, full suite**

Run: `python -m pytest harness/tests/test_node_test_runner.py harness/tests/test_factory_config.py -q` → pass.
Run: `python -m pytest harness/tests -q` → green.

- [ ] **Step 6: Commit**

```bash
git add harness/node_test_runner.py harness/factory_config.py harness/loop_job.py harness/tests
git commit -m "feat: node --test runner joins the delegation perimeter"
```

---

### Task 10: `start_stage` — enter the ladder at rung N

**Files:**
- Modify: `harness/factory_mcp.py` (`delegate` line 313 + the `delegate` inputSchema line 49), `harness/loop_job.py` (`_execute` line 315)
- Test: `harness/tests/test_loop_job.py`, `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Produces: `delegate(..., start_stage=None)` (int, 1..4); spec.json carries `"start_stage"`; `_execute` slices the ladder: `ladder[start_stage - 1:]`.

- [ ] **Step 1: Write the failing tests**

`test_loop_job.py` (against the existing `_execute`/`run_job` fixtures — read how they inject `ladder` and fake `chat_fn`):

```python
def test_start_stage_skips_the_lower_rungs(...):
    # spec with "start_stage": 3, DEFAULT_LADDER injected -> the first model
    # the fake chat_fn sees is "qwen3-coder:30b", never the 7b/14b.
    ...
```

`test_factory_mcp.py`:

```python
def test_delegate_validates_start_stage(factory, ...):
    # start_stage=0 and start_stage=5 raise ToolError;
    # start_stage=3 lands in the written spec.json.
    ...
```

Write both in full against the real fixtures in those files (they exist for `delegate` and `_execute`; the elision here is fixture naming only — the assertions are the contract).

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest harness/tests/test_loop_job.py harness/tests/test_factory_mcp.py -q -k start_stage`
Expected: FAIL.

- [ ] **Step 3: Implement**

`factory_mcp.py` `delegate()`: add `start_stage=None` param; validate:

```python
        if start_stage is not None and start_stage not in (1, 2, 3, 4):
            raise ToolError("start_stage must be 1..4 (ladder rung)")
```

include `"start_stage": start_stage` in the spec dict when not None. Add to the `delegate` tool inputSchema properties:

```python
                "start_stage": {"type": "integer",
                                "description": "Enter the ladder at this rung "
                                "(1=7b .. 4=35b). Module-scale contracts "
                                "start at 3; small fixes at 1."},
```

`loop_job.py` `_execute` (line 315): before calling `_run_ladder`:

```python
        stages = ladder or DEFAULT_LADDER
        start = spec.get("start_stage")
        if start:
            stages = stages[start - 1:] or stages[-1:]
```

and pass `stages` instead of `ladder or DEFAULT_LADDER`.

- [ ] **Step 4: Run tests, full suite**

Run: `python -m pytest harness/tests -q` → green.

- [ ] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/loop_job.py harness/tests
git commit -m "feat: delegate can enter the ladder at a given rung"
```

---

### Task 11: Blitzvolley in the perimeter + pilot run + docs

**Files:**
- Modify: `factory.toml`, `CHANGELOG.md`, `ROADMAP.md`, `docs/model-configs.md`
- Output: `audits/20260715-blitzvolley-pilot.md`

- [ ] **Step 1: Verify the project's suite runs standalone**

Run in `C:/Users/me/Documents/Info/VibeCoding/Blobby/BlobVolley`:
`node --test backend/test/` — expect the same pass count as `npm test`. If the directory form does not collect (older node), use `node --test backend/test/*.test.js` in `regression_cmd` below.

- [ ] **Step 2: Add the project**

```toml
[projects.blitzvolley]
path = "C:/Users/me/Documents/Info/VibeCoding/Blobby/BlobVolley"
runner = "node"
regression_cmd = ["node", "--test", "backend/test/"]
```

- [ ] **Step 3: Pick the pilot task**

Read `BlobVolley/TODO.md`; pick ONE module-scale task (new self-contained backend module + clear contract, e.g. a rating/quest/shop calculation — NOT a UI/netcode change). Confirm the pick with the operator before delegating (real project, real diff).

- [ ] **Step 4: Delegate and watch**

Delegate with: spec tests (`*.test.js`, CJS, requiring targets repo-relative via NODE_PATH), `target_files` = the new/changed module only, `start_stage=3`. Track with `job_status`/`job_log`. The run exercises: profiles (30b at 0.7), `num_predict` cap, node runner, start_stage.

- [ ] **Step 5: Write the pilot audit + update docs**

`audits/20260715-blitzvolley-pilot.md`: what was delegated, rungs used, attempts, wall time, regression verdict, whether any guard fired (it should not), review verdict, and the apply/discard decision. Update `CHANGELOG.md` (one entry per shipped feature of this plan), `ROADMAP.md` (tick the items), `docs/model-configs.md` (sampling profile table + source note).

- [ ] **Step 6: Commit**

```bash
git add factory.toml CHANGELOG.md ROADMAP.md docs/model-configs.md audits/20260715-blitzvolley-pilot.md
git commit -m "feat: blitzvolley joins the delegation perimeter; pilot audit"
```

---

## Self-Review Notes (already applied)

- Spec A1..A4 → Tasks 1-4; B1..B5 → Tasks 5-8; C → Tasks 9-11. `start_stage` (Task 10) implements the spec's "démarrage direct barreau 3".
- Types consistent: `options` dict everywhere; `Toolset.native/mcp`; `McpServerConfig.tools/readonly`; `stopped: "length"` end-to-end.
- Known intentional gaps: Task 4/10 test bodies reference existing fixtures by contract (the exact fixture names live in `test_loop_job.py` — read them before writing); Task 7 app.js edit mirrors an existing pattern rather than shipping blind JS; Task 8 tool-name allowlists must be checked against each server's real `tools/list` on first spawn.

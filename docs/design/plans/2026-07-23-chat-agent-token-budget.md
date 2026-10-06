# Chat/agent token budget — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure, to the token, what a chat turn and an agent turn cost, line item by line item, then act on the fattest ones.

**Architecture:** Three layers. (1) `llama_client.tokenize()` asks the server for a real token count. (2) `accounting.py` — pure functions, no I/O — turns named prompt blocks plus the server's counters into a decomposition, and reconciles it against what the wire actually carried. (3) `experiments/agent_bench/` drives real turns through the real `Factory` and records the decomposition per turn.

**Tech Stack:** Python 3.9 (stdlib only — `urllib`, `json`, `http.server` for fakes), pytest, llama-server (`/v1/chat/completions`, `/tokenize`), git.

## Global Constraints

- Interpreter: `py -3.9` is the documented standard for the suite and the studio. The 3.12 on PATH now has pytest (2026-07-23) but is not the reference.
- The pytest suite is 734 green at the start of this plan. It stays green at every commit: `py -3.9 -m pytest harness/tests -q`.
- Host is Windows; `run_command` uses cmd.exe. Long GPU runs launch detached with `Start-Process` plus file logs — **never** through Claude Code background tasks (an 07-18 smoke run was killed that way).
- Forbidden in this repo for AI sessions: `git stash`, `git reset --hard`, `git push --force`.
- Commits go directly on `main` (project convention), conventional-commit prefixes, English, ASCII only, no AI attribution.
- **The agent system prompt is the KV prefix.** A byte change costs ~1 s per turn (measured 2026-07-22, ×11 on TTFT). Any refactor of prompt assembly must be provably byte-identical.
- Harness modules import each other by bare name; `harness/tests/conftest.py` already puts `harness/` on `sys.path`.

---

### Task 1: A real token count from the server

`accounting` must not estimate. llama.cpp's server exposes `POST /tokenize`; this task adds the client call and proves the endpoint exists on our build.

**Files:**
- Modify: `harness/llama_client.py` (add `tokenize`, after `chat` which ends at line 131)
- Test: `harness/tests/test_llama_client.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `llama_client.tokenize(base_url: str, text: str) -> int` — number of tokens the server's tokenizer makes of `text`. Raises `RuntimeError` with the server's body on HTTP failure (via the existing `_raise_readable`).

- [ ] **Step 1: Write the failing test**

Append to `harness/tests/test_llama_client.py`. The existing `FakeLlamaServer` fixture (`server`, line 33) records the request and replies with whatever is in `FakeLlamaServer.reply`, so it serves `/tokenize` without changes.

```python
def test_tokenize_returns_the_server_token_count(server):
    FakeLlamaServer.reply = {"tokens": [100, 200, 300, 400]}
    assert llama_client.tokenize(server, "quatre tokens ici") == 4
    assert FakeLlamaServer.seen["path"] == "/tokenize"
    assert FakeLlamaServer.seen["body"] == {"content": "quatre tokens ici"}


def test_tokenize_of_the_empty_string_is_zero_without_a_call(server):
    FakeLlamaServer.seen = None
    assert llama_client.tokenize(server, "") == 0
    assert FakeLlamaServer.seen is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.9 -m pytest harness/tests/test_llama_client.py -k tokenize -q`
Expected: FAIL, `AttributeError: module 'llama_client' has no attribute 'tokenize'`

- [ ] **Step 3: Write the implementation**

Add to `harness/llama_client.py`, after the `chat` function:

```python
def tokenize(base_url, text):
    """How many tokens the server's own tokenizer makes of `text`.

    The accounting of a prompt's line items has to be exact, and the only
    tokenizer that agrees with the server is the server's. An empty block
    costs nothing and is not worth a round-trip.
    """
    if not text:
        return 0
    req = urllib.request.Request(
        base_url.rstrip("/") + "/tokenize",
        data=json.dumps({"content": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
            out = json.loads(f.read().decode("utf-8"))
    except Exception as e:
        _raise_readable(e)
    return len(out.get("tokens") or [])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.9 -m pytest harness/tests/test_llama_client.py -k tokenize -q`
Expected: `2 passed`

- [ ] **Step 5: Probe the real server**

This is the 30-second check the spec makes step 1: does our llama.cpp build serve `/tokenize`? Start the studio (or any llama-server on the chat lane) so port 8091 is up, then:

```bash
py -3.9 -c "import sys; sys.path.insert(0,'harness'); import llama_client; print(llama_client.tokenize('http://127.0.0.1:8091', 'bonjour le monde'))"
```

Expected: a small integer (3-5).

**If instead it raises `llama-server HTTP 404`:** the endpoint is absent on this build. Do not improvise — stop and report. The spec's documented fallback is differential measurement (send a prompt with and without a block, subtract the `prompt_count` values), which changes Task 3's interface and needs its own decision.

- [ ] **Step 6: Commit**

```bash
git add harness/llama_client.py harness/tests/test_llama_client.py
git commit -m "feat(llama): ask the server for a real token count"
```

---

### Task 2: Name the parts of the agent system prompt

`_agent_system` returns one joined string (`factory_mcp.py:339-357`), so there is no way to say what each block costs. Split the build into named parts and have the old function join them — **byte for byte**, because this string is the KV prefix.

**Files:**
- Modify: `harness/factory_mcp.py:339-357` (`_agent_system`)
- Test: `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `factory_mcp._agent_system_parts(workspace, tool_names=None) -> list[tuple[str, str]]` — ordered `(name, text)` pairs. Names are exactly `"system"`, `"agent_md"`, `"workspace_instructions"`, `"tool_names"`. `_agent_system(workspace, tool_names=None) -> str` keeps its current signature and return value.

- [ ] **Step 1: Write the failing test**

Append to `harness/tests/test_factory_mcp.py`:

```python
def test_agent_system_parts_join_back_to_the_prompt_byte_for_byte(tmp_path):
    """The joined string is the KV prefix: a byte that moves costs ~1 s per
    turn (measured 2026-07-22). Naming the parts must change nothing."""
    (tmp_path / "AGENTS.md").write_text("projet: regles locales\n",
                                        encoding="utf-8")
    ws = str(tmp_path)
    names = ["read_file", "write_file"]
    parts = factory_mcp._agent_system_parts(ws, names)
    assert "\n\n".join(text for _, text in parts) == \
        factory_mcp._agent_system(ws, names)


def test_agent_system_parts_are_named_in_prompt_order(tmp_path):
    (tmp_path / "AGENTS.md").write_text("projet: regles locales\n",
                                        encoding="utf-8")
    parts = factory_mcp._agent_system_parts(str(tmp_path), ["read_file"])
    assert [name for name, _ in parts] == [
        "system", "agent_md", "workspace_instructions", "tool_names"]


def test_agent_system_parts_omit_blocks_that_do_not_exist(tmp_path):
    """A workspace with no instruction file and a session with no tools are
    both normal; empty parts must not appear, or they would add separators."""
    parts = factory_mcp._agent_system_parts(str(tmp_path), None)
    assert [name for name, _ in parts] == ["system", "agent_md"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.9 -m pytest harness/tests/test_factory_mcp.py -k agent_system_parts -q`
Expected: FAIL, `AttributeError: module 'factory_mcp' has no attribute '_agent_system_parts'`

- [ ] **Step 3: Write the implementation**

Replace `_agent_system` in `harness/factory_mcp.py` (currently lines 339-357) with:

```python
def _agent_system_parts(workspace, tool_names=None):
    """The system prompt as named blocks, in wire order. `_agent_system`
    joins them; the bench prices them one by one. Same sources, same order,
    same bytes -- this string is the KV prefix."""
    shell_note = SHELL_NOTE_WINDOWS if os.name == "nt" else SHELL_NOTE_POSIX
    parts = [("system", AGENT_SYSTEM.format(workspace=workspace,
                                            shell_note=shell_note))]
    if GLOBAL_AGENT_MD.is_file():
        parts.append(("agent_md", GLOBAL_AGENT_MD.read_text(
            encoding="utf-8", errors="replace")[:8000]))
    for name in INSTRUCTION_FILES:
        p = Path(workspace) / name
        if not p.is_file():
            continue
        try:
            # self-hosted sessions: the workspace file IS the global one
            same = GLOBAL_AGENT_MD.is_file() and p.samefile(GLOBAL_AGENT_MD)
        except OSError:
            same = False
        if not same:
            parts.append(("workspace_instructions", p.read_text(
                encoding="utf-8", errors="replace")[:8000]))
        break
    if tool_names:
        # Generated from the real toolset, never a stale hard-coded list --
        # a session with a curated toolset must not see phantom tools.
        parts.append(("tool_names",
                      "Tools available this session: " + ", ".join(tool_names)))
    return parts


def _agent_system(workspace, tool_names=None):
    return "\n\n".join(text for _, text in
                       _agent_system_parts(workspace, tool_names))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.9 -m pytest harness/tests/test_factory_mcp.py -k agent_system -q`
Expected: all pass, including the pre-existing `_agent_system` tests.

- [ ] **Step 5: Run the whole suite — this file is load-bearing**

Run: `py -3.9 -m pytest harness/tests -q`
Expected: `734 passed` (plus the 3 new ones = 737)

- [ ] **Step 6: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -m "refactor(agent): name the parts of the system prompt

Byte-identical by construction and by test: the joined string is the KV
prefix. Naming the blocks is what lets the bench price them."
```

---

### Task 3: The accounting module

Pure functions. In: named blocks plus the server's counters. Out: the decomposition, and — this is the honest part — the **gap** between the blocks and what the wire really carried. The chat template wraps every message in role tokens and serialises tool schemas its own way; a decomposition that pretends to add up exactly would be lying.

**Files:**
- Create: `experiments/agent_bench/__init__.py` (empty)
- Create: `experiments/agent_bench/accounting.py`
- Test: `harness/tests/test_agent_bench_accounting.py`

**Interfaces:**
- Consumes: `llama_client.tokenize` (Task 1) — but only as an injected callable, never imported here. `factory_mcp._agent_system_parts` (Task 2) supplies the system blocks.
- Produces:
  - `segments(system_parts, tools, summary, messages) -> list[tuple[str, str]]`
  - `count(segments, tokenize) -> list[tuple[str, int]]`
  - `reconcile(counted, prompt_count) -> dict` with keys `blocks`, `wire`, `overhead`, `overhead_pct`
  - `cache_report(metrics) -> dict` with keys `wire`, `repaid`, `reused`, `hit_pct`

- [ ] **Step 1: Create the package marker**

```bash
py -3.9 -c "open('experiments/agent_bench/__init__.py','w').close()" 2>NUL || mkdir experiments\agent_bench
```

On Windows cmd.exe, if the directory does not exist yet:

```bash
mkdir experiments\agent_bench
py -3.9 -c "open(r'experiments/agent_bench/__init__.py','w').close()"
```

- [ ] **Step 2: Write the failing test**

Create `harness/tests/test_agent_bench_accounting.py`:

```python
"""What a turn costs, line item by line item.

The rule the module exists to enforce: never estimate. Counts come from the
server's tokenizer (injected here as a fake), and the difference between the
blocks and the wire is REPORTED as overhead, never absorbed -- the chat
template's role wrappers are real tokens somebody has to own.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments"))

from agent_bench import accounting


def fake_tokenize(text):
    """One token per whitespace-separated word: deterministic, no server."""
    return len(text.split())


def test_segments_lay_out_the_prompt_in_wire_order():
    parts = [("system", "a b"), ("agent_md", "c")]
    tools = [{"function": {"name": "read_file"}}]
    messages = [{"role": "user", "content": "salut"}]
    names = [n for n, _ in accounting.segments(parts, tools, None, messages)]
    assert names == ["system", "agent_md", "tools", "messages"]


def test_segments_omit_the_summary_when_there_is_none():
    names = [n for n, _ in accounting.segments([("system", "a")], [], None, [])]
    assert "summary" not in names


def test_segments_place_the_summary_between_the_system_and_the_messages():
    parts = [("system", "a")]
    summary = {"content": "on parlait de X", "covers_until": 4}
    names = [n for n, _ in accounting.segments(parts, [], summary, [])]
    assert names == ["system", "tools", "summary"]


def test_segments_count_tool_call_arguments_in_the_messages():
    """An agent write_file carries the whole file in its arguments. Counting
    the message as its `content` alone is how a session filled 65536 tokens
    without anything noticing (2026-07-14)."""
    messages = [{"role": "assistant", "content": "",
                 "tool_calls": [{"function": {"name": "write_file",
                                              "arguments": {"text": "un deux trois"}}}]}]
    text = dict(accounting.segments([], [], None, messages))["messages"]
    assert "un deux trois" in text


def test_count_prices_every_segment_with_the_injected_tokenizer():
    segs = [("system", "un deux trois"), ("messages", "quatre")]
    assert accounting.count(segs, fake_tokenize) == [("system", 3),
                                                     ("messages", 1)]


def test_reconcile_reports_the_gap_between_the_blocks_and_the_wire():
    out = accounting.reconcile([("system", 90), ("messages", 10)], 120)
    assert out == {"blocks": 100, "wire": 120, "overhead": 20,
                   "overhead_pct": 16}


def test_reconcile_survives_a_turn_with_no_wire_counter():
    """A stopped turn yields no timings; the report must degrade, not crash."""
    out = accounting.reconcile([("system", 90)], 0)
    assert out["wire"] == 0 and out["overhead_pct"] == 0


def test_cache_report_splits_what_was_repaid_from_what_was_reused():
    metrics = {"prompt_count": 3200, "prefill_count": 17}
    assert accounting.cache_report(metrics) == {
        "wire": 3200, "repaid": 17, "reused": 3183, "hit_pct": 99}


def test_cache_report_of_a_first_turn_is_all_repaid():
    metrics = {"prompt_count": 1103, "prefill_count": 1103}
    assert accounting.cache_report(metrics)["hit_pct"] == 0
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `py -3.9 -m pytest harness/tests/test_agent_bench_accounting.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'agent_bench'`

- [ ] **Step 4: Write the implementation**

Create `experiments/agent_bench/accounting.py`:

```python
"""What one turn's prompt costs, line item by line item.

Pure functions, no I/O: the tokenizer arrives as a callable so this module is
testable without a GPU, and the counters arrive as plain dicts.

The rule: never estimate. `chat_context.estimate` is chars/ratio and exists to
budget transport conservatively; it is not a measurement. Counts here come
from the server's own tokenizer, and what the blocks fail to explain is
reported as `overhead` rather than absorbed -- the chat template's role
wrappers and its own tool serialisation are real tokens, and pretending the
sum is exact would hide them.
"""
import json


def _message_text(m):
    """What a message really carries: content plus tool_call arguments. An
    agent write_file puts the whole file in the arguments (2026-07-14)."""
    text = m.get("content") or ""
    if m.get("tool_calls"):
        text += json.dumps(m["tool_calls"], ensure_ascii=False)
    return text


def segments(system_parts, tools, summary, messages):
    """The prompt in wire order as [(name, text)].

    `tools` is priced from its JSON: the template serialises schemas its own
    way, so this is an approximation, and `reconcile` is what keeps it honest.
    """
    segs = list(system_parts)
    segs.append(("tools", json.dumps(tools, ensure_ascii=False) if tools else ""))
    if summary:
        segs.append(("summary", summary.get("content") or ""))
    if messages:
        segs.append(("messages",
                     "\n".join(_message_text(m) for m in messages)))
    return segs


def count(segments_, tokenize):
    """[(name, tokens)] -- `tokenize` is str -> int, injected."""
    return [(name, tokenize(text)) for name, text in segments_]


def reconcile(counted, prompt_count):
    """The blocks against the wire. `overhead` is what the chat template adds
    and the blocks cannot explain; it is a number to look at, not to bury."""
    blocks = sum(n for _, n in counted)
    wire = prompt_count or 0
    overhead = wire - blocks
    return {"blocks": blocks, "wire": wire, "overhead": overhead,
            "overhead_pct": int(100 * overhead / wire) if wire else 0}


def cache_report(metrics):
    """What this turn repaid versus what the KV prefix gave back for free.
    `prompt_count` is prompt_n + cache_n; `prefill_count` is prompt_n alone
    (llama_client.py:107-111)."""
    wire = metrics.get("prompt_count") or 0
    repaid = metrics.get("prefill_count") or 0
    reused = wire - repaid
    return {"wire": wire, "repaid": repaid, "reused": reused,
            "hit_pct": int(100 * reused / wire) if wire else 0}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -3.9 -m pytest harness/tests/test_agent_bench_accounting.py -q`
Expected: `9 passed`

- [ ] **Step 6: Run the whole suite**

Run: `py -3.9 -m pytest harness/tests -q`
Expected: `746 passed`

- [ ] **Step 7: Commit**

```bash
git add experiments/agent_bench/ harness/tests/test_agent_bench_accounting.py
git commit -m "feat(bench): price a turn's prompt line item by line item

Counts come from the server's tokenizer, never from chars/3, and the gap
between the blocks and the wire is reported instead of absorbed."
```

---

### Task 4: Prove the prefix is byte-stable (line item 3)

The spec runs this one first of the seven: it is binary, it costs nothing, and the risk is real. If the tool schemas serialise differently from one turn to the next, the KV prefix breaks every turn and every other measurement in this plan is polluted.

**Files:**
- Test: `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Consumes: `factory_mcp._agent_system_parts` (Task 2), `Factory._session_tools` (`factory_mcp.py:495`).
- Produces: no new API. A guard test.

- [ ] **Step 1: Write the test**

Append to `harness/tests/test_factory_mcp.py`. Reuse whatever `Factory` fixture the file already uses for session tests; if it builds one inline, follow that pattern.

```python
def test_the_session_prompt_prefix_is_byte_stable_across_turns(tmp_path):
    """The system prompt and the tool schemas are the KV prefix. If either
    serialises differently from one turn to the next, the prefix breaks and
    every turn repays the prefill (~1 s, measured 2026-07-22). Dict ordering
    is the classic way this happens."""
    f = factory_mcp.Factory(tmp_path / "jobs", ROOT / "factory.toml")
    session = {"workspace": str(tmp_path), "toolset": "base"}
    first = json.dumps(f._session_tools(session), ensure_ascii=False)
    second = json.dumps(f._session_tools(session), ensure_ascii=False)
    assert first == second
    names = [t["function"]["name"] for t in f._session_tools(session)]
    assert factory_mcp._agent_system(str(tmp_path), names) == \
        factory_mcp._agent_system(str(tmp_path), names)
```

- [ ] **Step 2: Run it**

Run: `py -3.9 -m pytest harness/tests/test_factory_mcp.py -k byte_stable -q`

Expected: PASS. **If it fails, stop and report** — that is a shipped bug worth its own fix and its own registry row, not something to paper over inside this task.

- [ ] **Step 3: Commit**

```bash
git add harness/tests/test_factory_mcp.py
git commit -m "test(agent): guard the byte-stability of the KV prefix"
```

---

### Task 5: Promote the bench to `experiments/agent_bench/`

`experiments/agent_md_variants/bench.py` (375 lines) already drives the real `Factory.agent_reply` against a real llama-server, seeds a byte-identical git workspace per run, and scores the result. It is welded to one experiment. Split it by responsibility and keep the 07-23 results reproducible.

**Files:**
- Create: `experiments/agent_bench/tasks.py` (the corpus + `read`, `eval_ok`, `seed`, `git`, `_rmtree`)
- Create: `experiments/agent_bench/bench.py` (orchestrator + `score_events` + `summarize`)
- Create: `experiments/agent_bench/README.md`
- Keep: `experiments/agent_md_variants/` untouched — it holds the 07-23 raw results and its own README. Do not move or delete it.

**Interfaces:**
- Consumes: `accounting` (Task 3), `factory_mcp.Factory`, `workspace_memory.failed`.
- Produces:
  - `tasks.TASKS` — list of dicts `{"name": str, "files": dict[str, str], "goal": str, "ok": callable(ws) -> bool}`
  - `tasks.seed(ws: Path, files: dict) -> None`
  - `bench.score_events(events, tokenize=None) -> dict` — the existing keys (`tool_calls`, `tool_errors`, `edit_misses`, `eval_tokens`, `stopped`, `call_names`) plus `think_tokens` and `write_tokens` when `tokenize` is given.

- [ ] **Step 1: Copy the mechanical parts across**

Copy `_rmtree`, `git`, `seed`, `read`, `eval_ok` and the `TASKS` list from `experiments/agent_md_variants/bench.py` into `experiments/agent_bench/tasks.py` verbatim. Copy `run_one`, `score_events`, `summarize`, `main` into `experiments/agent_bench/bench.py`. Change only the imports: `bench.py` does `from tasks import TASKS, seed` after the same `sys.path.insert(0, str(ROOT / "harness"))` preamble.

Drop from `bench.py`: the `VARIANTS` dict and the `factory_mcp.GLOBAL_AGENT_MD = path` line. This bench is not the agent.md A/B; variants come back in Task 8 as a generic hook.

- [ ] **Step 2: Write the failing test for the new scoring**

Create `harness/tests/test_agent_bench_scoring.py`:

```python
"""Scoring a turn's events. The failure this guards: pass 1 of the 07-23 A/B
counted only outputs starting with "error:", so every failed run_command --
a wasted round-trip by any measure -- read as zero."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments"
                       / "agent_bench"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bench


def words(text):
    return len(text.split())


def test_a_failed_shell_command_counts_as_a_wasted_round_trip():
    events = [("tool_result", {"output": "exit 1: No module named pytest"})]
    assert bench.score_events(events)["tool_errors"] == 1


def test_thinking_and_answer_tokens_are_split():
    events = [("thinking", "je reflechis un peu"), ("chunk", "voila")]
    out = bench.score_events(events, tokenize=words)
    assert out["think_tokens"] == 4
    assert out["write_tokens"] == 1


def test_scoring_without_a_tokenizer_omits_the_split():
    out = bench.score_events([("chunk", "voila")])
    assert "think_tokens" not in out
```

- [ ] **Step 3: Run it to verify it fails**

Run: `py -3.9 -m pytest harness/tests/test_agent_bench_scoring.py -q`
Expected: FAIL — `think_tokens` is not produced yet.

- [ ] **Step 4: Extend `score_events`**

In `experiments/agent_bench/bench.py`, change the signature and add the split. Keep every existing key and the existing `workspace_memory.failed` rule untouched.

```python
def score_events(events, tokenize=None):
    calls = [p for k, p in events if k == "tool_call"]
    results = [p for k, p in events if k == "tool_result"]
    # A tool that refused says "error:"; a shell command that failed says it in
    # its exit code, and that round-trip is just as wasted (07-23, bug 2).
    tool_errors = sum(1 for r in results
                      if workspace_memory.failed(r.get("output")))
    edit_misses = sum(1 for r in results
                      if "0 matches" in (r.get("output") or ""))
    done_payloads = [p for k, p in events if k == "done"]
    eval_tokens = sum(p.get("eval_count") or 0 for p in done_payloads) or None
    out = {
        "tool_calls": len(calls),
        "tool_errors": tool_errors,
        "edit_misses": edit_misses,
        "eval_tokens": eval_tokens,
        "stopped": (done_payloads[-1].get("stopped") if done_payloads else None),
        "call_names": [c.get("name") for c in calls],
    }
    if tokenize:
        # Line item 6: the share of generation that never reaches the user.
        out["think_tokens"] = tokenize(
            "".join(p for k, p in events if k == "thinking"))
        out["write_tokens"] = tokenize(
            "".join(p for k, p in events if k == "chunk"))
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -3.9 -m pytest harness/tests/test_agent_bench_scoring.py -q`
Expected: `3 passed`

- [ ] **Step 6: Write the README**

Create `experiments/agent_bench/README.md`:

```markdown
# agent_bench

Drives real agent turns through the real `Factory` against a real llama-server
and records what each turn cost, line item by line item.

- `tasks.py` — the corpus. A task seeds a byte-identical git workspace, states
  a goal in the operator's language, and asserts on the resulting workspace by
  RUNNING the code, never by matching literal text (07-23, bug 1: a model that
  rewrote function-style tests as a TestCase scored as a failure).
- `accounting.py` — pure functions, no I/O. Prices the prompt's blocks with the
  server's tokenizer and reports the gap against the wire.
- `bench.py` — the orchestrator. Writes `results.jsonl` and a `.md`.

Long GPU run: launch it DETACHED, never through a task runner.

    Start-Process py -ArgumentList '-3.9','experiments/agent_bench/bench.py' `
      -RedirectStandardOutput run.out -RedirectStandardError run.err

Environment: `BENCH_MODEL` (default `qwen3-coder:30b`), `BENCH_RUNS` (5),
`BENCH_ONLY` (substring filter on task name, for smoke runs).
```

- [ ] **Step 7: Run the whole suite and commit**

Run: `py -3.9 -m pytest harness/tests -q`
Expected: `749 passed`

```bash
git add experiments/agent_bench/ harness/tests/test_agent_bench_scoring.py
git commit -m "feat(bench): promote the agent bench to its own package

tasks/accounting/bench, one responsibility each. Scoring now splits the
generated tokens the user never sees from the ones they do."
```

---

### Task 6: Record the per-turn budget in the bench

Wire Tasks 2, 3 and 5 together: every run records its prompt decomposition and its cache hit rate alongside the existing scores.

**Files:**
- Modify: `experiments/agent_bench/bench.py` (`run_one`)

**Interfaces:**
- Consumes: `accounting.segments/count/reconcile/cache_report` (Task 3), `factory_mcp._agent_system_parts` (Task 2), `llama_client.tokenize` (Task 1).
- Produces: each row of `results.jsonl` gains `budget` (the `[(name, tokens)]` list), `recon` (the `reconcile` dict) and `cache` (the `cache_report` dict).

- [ ] **Step 1: Extend `run_one`**

In `experiments/agent_bench/bench.py`:

```python
def run_one(factory, task, ws):
    seed(ws, task["files"])
    sid = factory.chat_create(MODEL, kind="agent", mode="auto",
                              workspace=str(ws))["session_id"]
    t0 = time.time()
    events = list(factory.agent_reply(sid, task["goal"]))
    dt = time.time() - t0

    # The turn is over and the runtime is still up: price the prompt with the
    # server's own tokenizer, on the same blocks the turn actually sent.
    url = factory._acquire_runtime(MODEL)
    tok = lambda text: llama_client.tokenize(url, text)  # noqa: E731
    session = factory.chats.get(sid)
    tools = factory._session_tools(session)
    parts = factory_mcp._agent_system_parts(
        session["workspace"], [t["function"]["name"] for t in tools])
    wire = [{"role": m["role"], "content": m["content"],
             "tool_calls": m.get("tool_calls")} for m in session["messages"]]
    segs = accounting.segments(parts, tools, session.get("summary"), wire)
    budget = accounting.count(segs, tok)

    done = [p for k, p in events if k == "done"]
    metrics = done[-1] if done else {}

    s = score_events(events, tokenize=tok)
    s["budget"] = budget
    s["recon"] = accounting.reconcile(budget, metrics.get("prompt_count"))
    s["cache"] = accounting.cache_report(metrics)
    s["seconds"] = round(dt, 1)
    try:
        s["ok"] = bool(task["ok"](ws))
    except Exception as e:
        s["ok"] = False
        s["ok_error"] = str(e)
    s["session"] = sid
    return s
```

Add to the imports at the top of `bench.py`, after the `sys.path.insert`
preamble. `accounting` sits in the same directory as `bench.py`, so it is a
bare import like `tasks` — the package form is only for the test file, which
runs from elsewhere:

```python
import llama_client  # noqa: E402
import accounting  # noqa: E402
```

- [ ] **Step 2: Extend the log line so a run is readable live**

In `main`, replace the `say("    ok=...")` call with:

```python
                say("    ok={ok} calls={calls} err={err} miss={miss} "
                    "prompt={wire} (repaid {repaid}, +{oh} tmpl) "
                    "think/write={th}/{wr} {sec}s".format(
                        ok=s.get("ok"), calls=s.get("tool_calls"),
                        err=s.get("tool_errors"), miss=s.get("edit_misses"),
                        wire=s.get("cache", {}).get("wire"),
                        repaid=s.get("cache", {}).get("repaid"),
                        oh=s.get("recon", {}).get("overhead"),
                        th=s.get("think_tokens"), wr=s.get("write_tokens"),
                        sec=s.get("seconds")))
```

- [ ] **Step 3: Smoke it on one task**

The suite cannot cover this — it needs a GPU. Start the studio so a chat-lane server is up, then run one task once, detached:

```bash
Start-Process py -ArgumentList '-3.9','experiments/agent_bench/bench.py' `
  -RedirectStandardOutput smoke.out -RedirectStandardError smoke.err
```

with `BENCH_ONLY=fix_exact_value` and `BENCH_RUNS=1` set in the environment first.

Expected in `smoke.out`: one line with a non-zero `prompt=`, a `repaid` **below** it on any turn after the first, and a `+N tmpl` overhead that is positive and under ~15 % of `prompt`.

**If `+N tmpl` is negative,** the blocks are being double-counted somewhere — stop and report; a decomposition that exceeds the wire is a bug, not a finding.

- [ ] **Step 4: Commit**

```bash
git add experiments/agent_bench/bench.py
git commit -m "feat(bench): record the prompt budget and cache hit per run"
```

---

### Task 7: The measurement run and its report (line items 1, 2, 6)

The first three line items of the spec are answered by one run of what Tasks 1-6 built.

**Files:**
- Create: `experiments/results/20260723_prompt_budget.md`
- Modify: `docs/backlog-optimisations.md` (section "Chat / agent")

- [ ] **Step 1: Run the full corpus, both toolsets, detached**

Two runs: `base` toolset, then `web` (15 MCP schemas — the suspected fat).

Add the switch at the top of `bench.py`, beside the other environment knobs:

```python
TOOLSET = os.environ.get("BENCH_TOOLSET", "base")
```

and pass it in `run_one`:

```python
    sid = factory.chat_create(MODEL, kind="agent", mode="auto",
                              workspace=str(ws), toolset=TOOLSET)["session_id"]
```

Then, once per toolset:

```bash
Start-Process py -ArgumentList '-3.9','experiments/agent_bench/bench.py' `
  -RedirectStandardOutput run_base.out -RedirectStandardError run_base.err
```

Expected wall clock: roughly 7 tasks × 5 runs × ~20 s ≈ 12 min per toolset on the 30b.

- [ ] **Step 2: Write the report**

`experiments/results/20260723_prompt_budget.md` states, with numbers:
- tokens per block (`system`, `agent_md`, `workspace_instructions`, `tool_names`, `tools`, `summary`, `messages`), mean per turn, per toolset
- the template overhead `reconcile` reports, as an absolute and a percentage
- the cache hit rate per turn index (turn 1 repays everything; turn 2+ should repay almost nothing)
- think vs write tokens per model
- what the numbers say to do next, and what they say NOT to do

- [ ] **Step 3: File the rows in the registry**

One row each in the "Chat / agent" table of `docs/backlog-optimisations.md`, statuses per the file's own legend (SHIPPED / FALSIFIÉ / APPROUVÉ / À TESTER):
- "Décomposition du budget de prompt agent" — MESURÉ, with the report path
- "Coût des schémas d'outils (toolset web, 15 tools)" — the measured number, and a decision only if the number justifies one
- "Tokens de thinking non vus par l'utilisateur" — the measured share
- "Prefix KV byte-stable" — SHIPPED (guard test, Task 4)

- [ ] **Step 4: Commit**

```bash
git add experiments/results/20260723_prompt_budget.md docs/backlog-optimisations.md
git commit -m "docs(bench): the measured prompt budget of a chat/agent turn"
```

---

### Task 8: Long tasks, so compaction and eviction can happen (line items 4 and 5)

The current corpus finishes in ~3.4 tool calls. Compaction fires at 70 % of the prompt budget and eviction keeps the last 12 messages (`chat_context.py:14-16`); neither engages on a 3-call session. **This is why the 07-23 A/B saw `0 matches` and wasted round-trips fire zero times in 80 sessions.** Without this task, line items 4 and 5 are unreachable.

**Files:**
- Modify: `experiments/agent_bench/tasks.py` (append to `TASKS`)

**Interfaces:**
- Consumes: `tasks.seed` and the task dict shape from Task 5.
- Produces: two new entries in `tasks.TASKS`, named `long_refactor_chain` and `long_survey`.

- [ ] **Step 1: Add the tasks**

Append to `TASKS` in `experiments/agent_bench/tasks.py`. Both are built to force many tool round-trips on a large surface — that is what pushes a session past the compaction threshold.

```python
    {
        "name": "long_refactor_chain",
        # Eight modules, one shared helper: the agent must read most of them
        # before it can safely rename. Enough round-trips, and enough tool
        # output, to cross the 70 % compaction threshold.
        "files": dict(
            {"helper.py": "def fmt(x):\n    return str(x)\n"},
            **{"mod{}.py".format(i):
               "from helper import fmt\n\n\ndef show{}(v):\n"
               "    return fmt(v) + ' #{}'\n".format(i, i)
               for i in range(8)}),
        "goal": ("Renomme la fonction `fmt` de helper.py en `format_value`, "
                 "puis mets a jour TOUS ses appelants dans les modules mod0 a "
                 "mod7. Ne change rien d'autre."),
        "ok": lambda ws: (
            "def format_value(" in read(ws, "helper.py")
            and all("format_value(v)" in read(ws, "mod{}.py".format(i))
                    and "fmt(" not in read(ws, "mod{}.py".format(i))
                    for i in range(8))),
    },
    {
        "name": "long_survey",
        # Twelve files, one planted constant. The agent must search and read
        # widely; the tool OUTPUT is the bulk here, which is exactly what
        # eviction stubs.
        "files": dict(
            {"notes/target.py": "# la valeur de prod\nSEUIL = 4271\n"},
            **{"notes/f{}.py".format(i):
               "# fichier {}\nVALEUR_{} = {}\n".format(i, i, 1000 + i) * 12
               for i in range(12)}),
        "goal": ("Trouve dans notes/ la constante SEUIL, puis ecris dans "
                 "resume.md une ligne `SEUIL=<valeur>` avec sa valeur reelle."),
        "ok": lambda ws: "SEUIL=4271" in read(ws, "resume.md"),
    },
```

- [ ] **Step 2: Verify the tasks are well-formed without a GPU**

Run: `py -3.9 -c "import sys; sys.path.insert(0,'experiments/agent_bench'); import tasks; print([t['name'] for t in tasks.TASKS]); print(len(tasks.TASKS[-2]['files']), len(tasks.TASKS[-1]['files']))"`
Expected: the 9 names, then `9 13`.

- [ ] **Step 3: Confirm they actually trigger compaction**

Run the two new tasks once each, detached, with `BENCH_ONLY=long_`. In the log, look for a turn whose `prompt=` approaches 70 % of the 64k window, and check the session file for a `summary` key.

**If neither task triggers a compaction,** they are still too small — say so and stop. Do not silently lower `COMPACT_THRESHOLD` to force it: that would change the system under measurement.

- [ ] **Step 4: Commit**

```bash
git add experiments/agent_bench/tasks.py
git commit -m "test(bench): tasks long enough to reach compaction and eviction

The 07-23 A/B never saw its target failure modes because every task
finished in ~3.4 tool calls. These two cross the threshold."
```

---

### Task 9: A/B the compaction against `--cache-reuse` (line item 4)

The registry filed `--cache-reuse 256` FALSIFIED on 07-22 against a stable prefix and a head mutation — and named the untested form explicitly: **mid-prompt divergence**, which is exactly what compaction produces. This closes that hole.

**Files:**
- Modify: `experiments/agent_bench/bench.py` (a `BENCH_VARIANTS` hook)
- Create: `experiments/results/20260723_compaction_cache.md`
- Modify: `docs/backlog-optimisations.md`

- [ ] **Step 1: Add a generic variant hook**

Replace the deleted `VARIANTS`/`GLOBAL_AGENT_MD` mechanism from Task 5 with one that does not know what it is switching:

```python
# A variant is a name plus a callable that mutates the world before a run.
# The bench does not know what it switches -- agent.md, a server flag, a
# toolset. Keep the arms to two: one variable at a time.
VARIANTS = {"A": lambda: None}
```

The compaction A/B sets the two arms by restarting llama-server with and without `--cache-reuse 256`, which is a server start-up flag in `llama_server_manager.py`, not a request field. Arms therefore run as **two separate bench invocations**, not two arms in one process — record which is which in the results file name.

- [ ] **Step 2: Run both arms on the long tasks**

`BENCH_ONLY=long_`, `BENCH_RUNS=5`, detached, once with the flag on the server and once without.

- [ ] **Step 3: Read the answer off `prompt_n`**

The measurement is the `repaid` figure of `cache_report` on the turn **immediately after** a compaction. Flag ON should repay materially less than flag OFF. If it repays the same, the flag is falsified in its own target form and the registry row moves from "the untested form" to FALSIFIÉ outright.

- [ ] **Step 4: Write the report and update the registry row, then commit**

```bash
git add experiments/results/20260723_compaction_cache.md docs/backlog-optimisations.md experiments/agent_bench/bench.py
git commit -m "exp: A/B --cache-reuse on the form it was built for"
```

---

### Task 10: Put tool-output eviction on trial (line item 5)

The spec states this as a charge, not an assumption: `EVICT_KEEP = 12` mutates the **middle** of the prompt, which invalidates the KV cache downstream of the edit. It may be spending more re-prefill than the window it saves.

**Files:**
- Modify: `harness/chat_context.py` (only if the verdict says so)
- Create: `experiments/results/20260723_eviction_trial.md`
- Modify: `docs/backlog-optimisations.md`

- [ ] **Step 1: Run the long tasks with eviction on and off**

Arm A: `EVICT_KEEP = 12` (current). Arm B: eviction disabled, by running the bench with `chat_context.EVICT_KEEP` set high enough that `evict_tool_outputs` never fires — set it in `bench.py` at start-up, one line, so no shipped code changes for the measurement:

```python
if os.environ.get("BENCH_NO_EVICT"):
    chat_context.EVICT_KEEP = 10 ** 6
```

- [ ] **Step 2: Compare the right two numbers**

Not the window used — the **tokens repaid**. Arm A saves window (`wire` is smaller) but should repay more (`repaid` is larger, because the prefix broke mid-prompt). The verdict is the sum over a session, plus wall clock.

- [ ] **Step 3: Act on the verdict**

- If eviction costs more than it saves: remove it from `chat_context.build_prompt`, with the measurement in the commit message, the suite green, and `regress run` passed.
- If it pays for itself: the row goes SHIPPED with its proof, and `EVICT_KEEP` gets the number the data supports.
- Either way the row exists in the registry. A falsified lever stays in the table.

- [ ] **Step 4: Run the regression suite before adopting any harness change**

Run: `py -3.9 harness/factory_cli.py regress run` (detached, long GPU run)
Expected: no correctness regression against the baselines.

- [ ] **Step 5: Commit**

```bash
git add experiments/results/20260723_eviction_trial.md docs/backlog-optimisations.md
git commit -m "exp: put tool-output eviction on trial against the KV cache"
```

---

### Task 11: Hunt the systematic environment traps (line item 7)

The 07-23 A/B's one real finding was not about prose: **every single run of
both arms** burned two round-trips discovering that `python` on this box was a
3.12 without pytest. 24 failed tool results in 10 sessions, 20 of them from
that one trap. Fixing it at the source beat any wording change by a mile
(2.4 → 0 failed results per session, 28.7 → 15.9 s). The question this task
answers: **what else is like that?**

**Files:**
- Create: `experiments/results/20260723_env_traps.md`
- Modify: `docs/backlog-optimisations.md`, `ROADMAP.md`

**Interfaces:**
- Consumes: the `results.jsonl` written by Tasks 7-10 — no new run needed.
- Produces: no code. A ranked list of traps and a fix decision per trap.

- [ ] **Step 1: Rank the failure reasons across every session run so far**

A script rather than a one-liner, because it gets run again after every future
bench. Create `experiments/agent_bench/traps.py`:

```python
"""Rank the failure reasons across bench sessions.

The 07-23 A/B's one actionable finding came from this cut: 20 of 24 failed
tool results in 10 sessions were the same missing interpreter. Wording never
fixes those; the environment does.
"""
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "harness"))
import workspace_memory  # noqa: E402

HERE = Path(__file__).resolve().parent


def reasons(chats_dir):
    """(first line of the failure, count) over every tool result that failed."""
    counts = collections.Counter()
    for path in Path(chats_dir).glob("*.json"):
        session = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        for m in session.get("messages", []):
            if m.get("role") != "tool":
                continue
            out = m.get("content") or ""
            if workspace_memory.failed(out):
                counts[out.strip().splitlines()[0][:80]] += 1
    return counts.most_common()


if __name__ == "__main__":
    for line, n in reasons(sys.argv[1]):
        print("{:4d}  {}".format(n, line))
```

Run: `py -3.9 experiments/agent_bench/traps.py jobs/chats`
Expected: a ranked list, most frequent first.

- [ ] **Step 2: Decide the fix location for each trap in the top five**

For each, the choice is the one the 07-23 note already framed: fix it **at the
source** (install the thing, configure the box) or **name it in the prompt**.
Prefer the source. Naming it in `AGENT_SYSTEM` teaches the agent to route
around a broken box *and* costs KV-prefix bytes on every turn forever — that
price is now measured, in Task 7's table, so quote it in the decision.

- [ ] **Step 3: Apply the source fixes and re-measure**

For each fix applied, re-run the affected task 4 times and record the before/after
on failed results per session, shell calls per session, and wall clock — the
same three columns the 07-23 note used.

**Before trusting any source fix, check it does not convert a loud failure
into a quiet wrong one.** Installing pytest on the 3.12 was only safe because
the suite was verified 734 green under 3.12 too; had it been red, the fix would
have swapped a clear error for a silent regression on a second interpreter.

- [ ] **Step 4: Commit**

```bash
git add experiments/agent_bench/traps.py experiments/results/20260723_env_traps.md docs/backlog-optimisations.md ROADMAP.md
git commit -m "exp: rank the environment traps that burn tool calls

The 07-23 bench's one real finding was a missing interpreter, not prose.
This is the cut that found it, kept."
```

---

### Task 12: Decide what goes in the prompt (first half of E)

Now, and only now — with items 1-2 measured — the asymmetry gets settled with arithmetic instead of taste.

**Files:**
- Modify: `harness/factory_mcp.py:901` (only if the verdict says so)
- Create: `experiments/results/20260723_chat_system_prompt.md`
- Modify: `docs/backlog-optimisations.md`, `ROADMAP.md`

- [ ] **Step 1: State the three candidates and their measured price**

From Task 7's table: what would giving plain chat a system prompt cost, in tokens of window, per turn? The blocks are already priced individually — no new run needed for the cost side.

- [ ] **Step 2: Measure the benefit side, or decline to**

The benefit of a system prompt in plain chat is a **quality** claim, and quality is block C, which has no corpus yet. Two honest outcomes, and picking one is the deliverable:
- the cost is negligible and the change is cheap to reverse → ship it, file the row as APPROUVÉ-on-cost, and flag it for C to confirm
- the cost is material → the row waits for C, explicitly, in the registry

**Do not invent a quality measurement here.** That is what block C is for, and inventing one is how the 07-23 bench nearly published a wrong answer twice.

- [ ] **Step 3: Record the skills/global-memory decision rule**

Both are the same arithmetic, and it is already measurable: content that is **stable** is nearly free after turn 1 (KV cache) but permanently occupies window; content loaded **on demand** costs a re-prefill each time it changes. Write the rule with the numbers from Task 7 into the results file, and file the rows as À TESTER with the rule attached, so block E inherits a decision procedure rather than a debate.

- [ ] **Step 4: Commit**

```bash
git add experiments/results/20260723_chat_system_prompt.md docs/backlog-optimisations.md ROADMAP.md
git commit -m "docs: settle what belongs in the chat prompt, on the numbers"
```

---

## Closing the block

- [ ] For every A/B run (Tasks 9, 10): **at least one raw session per arm was
      autopsied** before its result was believed. This is what caught both
      measurement bugs on 07-23; skipping it is how a confident wrong answer
      ships.
- [ ] Full suite green: `py -3.9 -m pytest harness/tests -q`
- [ ] `regress run` green if anything under `harness/` changed
- [ ] Every one of the seven line items has a dated row in `docs/backlog-optimisations.md`, including the falsified ones
- [ ] `ROADMAP.md` — block B checked off, anything the bench uncovered filed under block D
- [ ] `git push` (the hook runs the suite; a red suite blocks the push by design)

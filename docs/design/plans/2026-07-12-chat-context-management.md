# Chat Context Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chat/agent sessions never silently degrade at context-full: old tool outputs are stubbed, the oldest span is replaced by a stored model-written summary, and a trim-with-warning is the safety net — Ollama never truncates on its own.

**Architecture:** A new pure module `harness/chat_context.py` owns all budget math and prompt assembly (no I/O, fully unit-testable). `factory_mcp.py` calls it at prompt-build time in `chat_reply` and `_agent_turn`, runs the one-shot compaction call via the existing `ollama_client.chat()`, and persists `summary`/`calibration` through two new `ChatStore` setters. The web UI renders a compaction chip, a history divider, and context info in the per-reply meta line.

**Tech Stack:** Python 3.9 stdlib only (repo constraint), pytest, vanilla JS frontend.

## Global Constraints

- Python 3.9 compatible, stdlib only — no new dependencies.
- The session JSON on disk always keeps the full record; only what is *sent* changes.
- A compaction failure must never fail the user's turn.
- Constants (from spec): `OUTPUT_RESERVE = 8192`, `COMPACT_THRESHOLD = 0.7`, `VERBATIM_TAIL = 6`, `EVICT_KEEP = 12`, `DEFAULT_RATIO = 3.0`, ratio clamped to [2.0, 5.0].
- Commit messages: conventional commits, ASCII, no AI attribution.
- Run tests from `C:\Users\me\local-factory` with `python -m pytest harness/tests/<file> -q`.

---

### Task 1: `chat_context` — estimation, budget, eviction

**Files:**
- Create: `harness/chat_context.py`
- Create: `harness/tests/test_chat_context.py`

**Interfaces:**
- Produces: `ratio(session) -> float`, `prompt_budget(num_ctx) -> int`, `estimate(messages_or_str, r) -> int`, `evict_tool_outputs(messages, keep_last=EVICT_KEEP) -> (list, int)`, constants `OUTPUT_RESERVE, COMPACT_THRESHOLD, VERBATIM_TAIL, EVICT_KEEP, DEFAULT_RATIO, EVICTED_STUB`.

- [ ] **Step 1: Write the failing tests**

`harness/tests/test_chat_context.py`:

```python
import chat_context
from chat_context import (estimate, evict_tool_outputs, prompt_budget, ratio,
                          EVICTED_STUB)


def test_ratio_defaults_and_calibrates_with_clamping():
    assert ratio({}) == 3.0
    assert ratio({"calibration": {"chars": 350, "tokens": 100}}) == 3.5
    assert ratio({"calibration": {"chars": 1000, "tokens": 100}}) == 5.0
    assert ratio({"calibration": {"chars": 100, "tokens": 100}}) == 2.0
    assert ratio({"calibration": {"chars": 0, "tokens": 0}}) == 3.0


def test_prompt_budget_reserves_output_room_with_a_floor():
    assert prompt_budget(32768) == 32768 - 8192
    # tiny windows (tests, future overrides) must not go negative
    assert prompt_budget(400) == 100


def test_estimate_counts_chars_over_ratio_plus_message_overhead():
    assert estimate("x" * 300, 3.0) == 100
    msgs = [{"role": "user", "content": "x" * 300},
            {"role": "assistant", "content": ""}]
    # 100 + overhead + 0 + overhead
    assert estimate(msgs, 3.0) == 100 + 2 * chat_context._MSG_OVERHEAD


def test_evict_stubs_old_tool_outputs_only():
    msgs = [{"role": "tool", "tool_name": "read_file", "content": "big" * 100},
            {"role": "assistant", "content": "vu"},
            {"role": "tool", "tool_name": "run_command", "content": "recent"},
            {"role": "user", "content": "ok"}]
    out, evicted = evict_tool_outputs(msgs, keep_last=2)
    assert evicted == 1
    assert out[0]["content"] == EVICTED_STUB
    assert out[0]["tool_name"] == "read_file"  # only content is stubbed
    assert out[1]["content"] == "vu"
    assert out[2]["content"] == "recent"       # within keep_last
    assert msgs[0]["content"] == "big" * 100   # input not mutated


def test_evict_keep_last_zero_stubs_everything():
    msgs = [{"role": "tool", "content": "a"}, {"role": "tool", "content": "b"}]
    out, evicted = evict_tool_outputs(msgs, keep_last=0)
    assert evicted == 2 and all(m["content"] == EVICTED_STUB for m in out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest harness/tests/test_chat_context.py -q`
Expected: FAIL / error `ModuleNotFoundError: No module named 'chat_context'`

- [ ] **Step 3: Write the implementation**

`harness/chat_context.py`:

```python
"""Prompt-side context management for chat/agent sessions (spec 2026-07-12).

Three layers, cheapest first, all at prompt-build time -- the session file on
disk always keeps the full record: (1) stub old tool outputs, (2) replace the
oldest span with a stored summary, (3) trim to the budget and report what was
dropped. Ollama must never be the one truncating: it does so silently.

Pure functions, no I/O. Token counts are estimates (stdlib only, no
tokenizer): chars / ratio, where ratio is calibrated per session from the
previous turn's prompt_eval_count.
"""

OUTPUT_RESERVE = 8192      # room for the reply; thinking models run long
COMPACT_THRESHOLD = 0.7    # compaction fires above this share of the budget
VERBATIM_TAIL = 6          # newest messages are always sent as-is
EVICT_KEEP = 12            # tool outputs within the last N messages survive
DEFAULT_RATIO = 3.0        # chars per token, conservative for French + code
_MSG_OVERHEAD = 4          # rough per-message token cost of role/formatting

EVICTED_STUB = "[résultat d'outil effacé]"

SUMMARY_PROMPT = """Tu résumes une conversation pour qu'elle puisse continuer \
avec moins de contexte. Réponds UNIQUEMENT avec le résumé, dans la langue de \
la conversation, structuré ainsi :
Objectif : ce que l'utilisateur veut obtenir.
Décisions : les choix actés et leurs raisons.
Faits : informations précises à retenir (noms, chemins, valeurs, préférences).
État : où en est le travail.
Suite : la prochaine étape attendue."""


def ratio(session):
    """Chars-per-token from the last turn's real prefill_count, clamped:
    a wild estimate must degrade to caution, not to a broken budget."""
    cal = session.get("calibration") or {}
    chars, tokens = cal.get("chars", 0), cal.get("tokens", 0)
    if chars and tokens:
        return min(5.0, max(2.0, chars / tokens))
    return DEFAULT_RATIO


def prompt_budget(num_ctx):
    """Tokens available for the prompt; the floor keeps small windows usable."""
    return max(num_ctx - OUTPUT_RESERVE, num_ctx // 4)


def estimate(messages, r):
    """Token estimate for a list of wire messages, or a bare string."""
    if isinstance(messages, str):
        return int(len(messages) / r)
    return sum(_MSG_OVERHEAD + int(len(m.get("content") or "") / r)
               for m in messages)


def evict_tool_outputs(messages, keep_last=EVICT_KEEP):
    """Copy of `messages` with tool outputs outside the last `keep_last`
    stubbed. Old tool results are bulky and perishable: the assistant message
    that reacted to them carries the conclusion. Returns (copy, count)."""
    cut = max(0, len(messages) - keep_last)
    out, evicted = [], 0
    for i, m in enumerate(messages):
        if i < cut and m.get("role") == "tool":
            m = dict(m, content=EVICTED_STUB)
            evicted += 1
        out.append(m)
    return out, evicted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest harness/tests/test_chat_context.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add harness/chat_context.py harness/tests/test_chat_context.py
git commit -m "feat: chat_context module - token estimation, budget, tool-output eviction"
```

---

### Task 2: `chat_context` — compaction span and prompt assembly

**Files:**
- Modify: `harness/chat_context.py` (append)
- Modify: `harness/tests/test_chat_context.py` (append)

**Interfaces:**
- Consumes: Task 1 functions.
- Produces: `summary_message(summary) -> dict`, `needs_compaction(messages, summary, budget, r) -> bool`, `compaction_span(messages, summary) -> (list, int)`, `build_prompt(system_msgs, summary, messages, budget, r) -> (list, dict)`. `summary` is `None` or `{"content": str, "covers_until": int, ...}`; `info` is `{"evicted": int, "dropped": int, "used_summary": bool, "est_tokens": int}`.

- [ ] **Step 1: Write the failing tests**

Append to `harness/tests/test_chat_context.py`:

```python
from chat_context import (build_prompt, compaction_span, needs_compaction,
                          summary_message)


def _msgs(n, size=300, role="user"):
    return [{"role": role, "content": "m%d " % i + "x" * size}
            for i in range(n)]


def test_needs_compaction_only_above_threshold_with_a_span_to_compact():
    # budget 1000, threshold 700 tokens; each msg ~104 tokens at ratio 3
    assert not needs_compaction(_msgs(4), None, 1000, 3.0)
    assert needs_compaction(_msgs(10), None, 1000, 3.0)
    # nothing beyond the verbatim tail -> nothing to compact
    assert not needs_compaction(_msgs(6, size=30000), None, 1000, 3.0)


def test_needs_compaction_ignores_messages_already_covered_by_summary():
    msgs = _msgs(20)
    summary = {"content": "résumé", "covers_until": 14}
    # only 6 live messages left -> nothing to compact
    assert not needs_compaction(msgs, summary, 1000, 3.0)


def test_compaction_span_is_everything_live_minus_the_tail():
    msgs = _msgs(10)
    span, covers = compaction_span(msgs, None)
    assert covers == 4 and span == msgs[:4]
    span, covers = compaction_span(msgs, {"content": "s", "covers_until": 2})
    assert covers == 4 and span == msgs[2:4]


def test_build_prompt_sends_everything_when_it_fits():
    msgs = _msgs(3)
    sent, info = build_prompt([], None, msgs, 10000, 3.0)
    assert sent == msgs
    assert info == {"evicted": 0, "dropped": 0, "used_summary": False,
                    "est_tokens": info["est_tokens"]}
    assert info["est_tokens"] > 0


def test_build_prompt_injects_summary_and_skips_covered_messages():
    msgs = _msgs(10)
    summary = {"content": "les faits", "covers_until": 6}
    sent, info = build_prompt(
        [{"role": "system", "content": "agent"}], summary, msgs, 10000, 3.0)
    assert sent[0] == {"role": "system", "content": "agent"}
    assert sent[1] == summary_message(summary)
    assert "les faits" in sent[1]["content"] and sent[1]["role"] == "system"
    assert sent[2:] == msgs[6:]
    assert info["used_summary"] is True and info["dropped"] == 0


def test_build_prompt_trims_oldest_first_and_reports_dropped():
    msgs = _msgs(10)  # ~104 tokens each
    sent, info = build_prompt([], None, msgs, 320, 3.0)
    assert sent == msgs[-3:]
    assert info["dropped"] == 7


def test_build_prompt_never_drops_the_newest_message():
    msgs = _msgs(2, size=30000)
    sent, info = build_prompt([], None, msgs, 100, 3.0)
    assert sent == [msgs[-1]]
    assert info["dropped"] == 1


def test_build_prompt_evicts_old_tool_outputs():
    msgs = ([{"role": "tool", "content": "z" * 500}] +
            _msgs(chat_context.EVICT_KEEP))
    sent, info = build_prompt([], None, msgs, 10000, 3.0)
    assert sent[0]["content"] == chat_context.EVICTED_STUB
    assert info["evicted"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest harness/tests/test_chat_context.py -q`
Expected: FAIL with `ImportError: cannot import name 'build_prompt'`

- [ ] **Step 3: Write the implementation**

Append to `harness/chat_context.py`:

```python
def _live(messages, summary):
    return messages[summary["covers_until"]:] if summary else list(messages)


def summary_message(summary):
    return {"role": "system",
            "content": "Résumé de la conversation antérieure :\n"
                       + summary["content"]}


def needs_compaction(messages, summary, budget, r):
    """True when the live span (after eviction) would still crowd the window."""
    live, _ = evict_tool_outputs(_live(messages, summary))
    if len(live) <= VERBATIM_TAIL:
        return False
    return estimate(live, r) > COMPACT_THRESHOLD * budget


def compaction_span(messages, summary):
    """(messages to summarize, new covers_until): everything not already
    covered, minus the verbatim tail. Indexes are into `messages`."""
    start = summary["covers_until"] if summary else 0
    covers_until = max(start, len(messages) - VERBATIM_TAIL)
    return messages[start:covers_until], covers_until


def build_prompt(system_msgs, summary, messages, budget, r):
    """Assemble what is actually sent: system + summary + the newest live
    messages that fit. Messages covered by the summary are never sent. The
    newest message goes out even over budget -- sending nothing is worse."""
    live, evicted = evict_tool_outputs(_live(messages, summary))
    base = list(system_msgs)
    if summary:
        base.append(summary_message(summary))
    left = budget - estimate(base, r)
    tail, dropped = [], 0
    for i in range(len(live) - 1, -1, -1):
        cost = estimate([live[i]], r)
        if left - cost < 0 and tail:
            dropped = i + 1
            break
        tail.insert(0, live[i])
        left -= cost
    sent = base + tail
    return sent, {"evicted": evicted, "dropped": dropped,
                  "used_summary": bool(summary),
                  "est_tokens": estimate(sent, r)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest harness/tests/test_chat_context.py -q`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add harness/chat_context.py harness/tests/test_chat_context.py
git commit -m "feat: chat_context compaction span and budgeted prompt assembly"
```

---

### Task 3: `ChatStore.set_summary` / `set_calibration`

**Files:**
- Modify: `harness/chat_store.py` (after `set_pending_calls`, line ~70)
- Modify: `harness/tests/test_chat_store.py` (append)

**Interfaces:**
- Produces: `ChatStore.set_summary(session_id, summary, now=None)` and `ChatStore.set_calibration(session_id, cal, now=None)`, both returning the saved session dict, both raising `ChatNotFound` on a missing session (via the existing `get`).

- [ ] **Step 1: Write the failing tests**

Append to `harness/tests/test_chat_store.py` (the file has a `store` fixture built on `tmp_path`):

```python
def test_set_summary_and_calibration_persist(store):
    sid = store.create("m:1")["session_id"]
    summary = {"content": "résumé", "covers_until": 4, "model": "m:1", "ts": 1.0}
    store.set_summary(sid, summary)
    store.set_calibration(sid, {"chars": 3000, "tokens": 900})
    s = store.get(sid)
    assert s["summary"] == summary
    assert s["calibration"] == {"chars": 3000, "tokens": 900}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest harness/tests/test_chat_store.py -q`
Expected: FAIL with `AttributeError: 'ChatStore' object has no attribute 'set_summary'`

- [ ] **Step 3: Write the implementation**

In `harness/chat_store.py`, after `set_pending_calls`:

```python
    def set_summary(self, session_id, summary, now=None):
        session = self.get(session_id)
        session["summary"] = summary
        return self._save(session, now)

    def set_calibration(self, session_id, cal, now=None):
        session = self.get(session_id)
        session["calibration"] = cal
        return self._save(session, now)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest harness/tests/test_chat_store.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add harness/chat_store.py harness/tests/test_chat_store.py
git commit -m "feat: chat_store setters for summary and token calibration"
```

---

### Task 4: `factory_mcp.chat_reply` — compaction, budgeted prompt, calibration

**Files:**
- Modify: `harness/factory_mcp.py` (`chat_reply`, `_usage` area, new `_compact` method; `import chat_context` at top with the other harness imports)
- Modify: `harness/tests/test_factory_mcp.py` (append to the chat section)

**Interfaces:**
- Consumes: everything from Tasks 1–3; existing `ollama_client.chat(model, system, user, temperature, num_ctx, timeout)` and `chat_stream`.
- Produces: `chat_reply` may yield `("compacting", True)` before streaming; `done` payload and stored `metrics` gain `"context": {"evicted": int, "dropped": int, "compacted": bool}`; `Factory._compact(session_id, model, wire, summary) -> summary_dict` (raises on failure); calibration persisted after every reply with a nonzero `prefill_count`.

- [ ] **Step 1: Write the failing tests**

Append to `harness/tests/test_factory_mcp.py` after the existing chat tests. Note `_fake_stream` (already in the file) returns metrics with `prefill_count: 100`.

```python
def _fill_session(factory, sid, n_pairs=8, size=400):
    for i in range(n_pairs):
        factory.chats.append(sid, {"role": "user",
                                   "content": "q%d " % i + "x" * size, "ts": 1.0})
        factory.chats.append(sid, {"role": "assistant",
                                   "content": "a%d " % i + "y" * size, "ts": 1.0})


def _fake_summary_chat(content="Objectif : tester.", fail=False):
    def chat(model, system, user, **kw):
        chat.calls.append({"model": model, "system": system, "user": user})
        if fail:
            raise OSError("ollama vanished")
        return {"content": content}
    chat.calls = []
    return chat


def test_chat_reply_saves_calibration_and_context_info(factory, monkeypatch):
    fake = _fake_stream(["ok"])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    sid = factory.chat_create("m:1")["session_id"]

    done = list(factory.chat_reply(sid, "salut"))[-1][1]

    assert done["context"] == {"evicted": 0, "dropped": 0, "compacted": False}
    session = factory.chat_get(sid)
    assert session["calibration"]["tokens"] == 100  # prefill_count from fake
    assert session["calibration"]["chars"] > 0
    assert session["messages"][-1]["metrics"]["context"]["dropped"] == 0


def test_chat_reply_compacts_when_over_threshold(factory, monkeypatch):
    fake = _fake_stream(["ok"])
    summarizer = _fake_summary_chat("Objectif : tout retenir.")
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    monkeypatch.setattr(factory_mcp.ollama_client, "chat", summarizer)
    monkeypatch.setattr(factory_mcp, "CHAT_NUM_CTX", 400)  # budget 100 tokens
    sid = factory.chat_create("m:1")["session_id"]
    _fill_session(factory, sid)  # 16 messages, far over 70 tokens

    events = list(factory.chat_reply(sid, "salut"))

    assert ("compacting", True) in events
    assert events[-1][0] == "done"
    assert events[-1][1]["context"]["compacted"] is True
    # one summary call, with the structured prompt, on the session's model
    assert len(summarizer.calls) == 1
    assert summarizer.calls[0]["system"] == factory_mcp.chat_context.SUMMARY_PROMPT
    assert summarizer.calls[0]["model"] == "m:1"
    # summary persisted: covers everything but the verbatim tail
    summary = factory.chat_get(sid)["summary"]
    assert summary["content"] == "Objectif : tout retenir."
    assert summary["covers_until"] == 17 - factory_mcp.chat_context.VERBATIM_TAIL
    # the prompt actually sent starts with the summary as a system message
    sent = fake.calls[0]["messages"]
    assert sent[0]["role"] == "system" and "tout retenir" in sent[0]["content"]
    assert all("q0 " not in (m.get("content") or "") for m in sent)


def test_chat_reply_survives_a_failed_compaction(factory, monkeypatch):
    fake = _fake_stream(["ok"])
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    monkeypatch.setattr(factory_mcp.ollama_client, "chat",
                        _fake_summary_chat(fail=True))
    monkeypatch.setattr(factory_mcp, "CHAT_NUM_CTX", 400)
    sid = factory.chat_create("m:1")["session_id"]
    _fill_session(factory, sid)

    events = list(factory.chat_reply(sid, "salut"))

    assert ("compacting", True) in events
    assert events[-1][0] == "done"                      # the turn still lands
    assert factory.chat_get(sid).get("summary") is None  # nothing persisted
    assert events[-1][1]["context"]["compacted"] is False
    assert events[-1][1]["context"]["dropped"] > 0       # trim covered it
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest harness/tests/test_factory_mcp.py -q -k "calibration or compacts or failed_compaction"`
Expected: 3 FAIL (`KeyError: 'context'`, no `compacting` event)

- [ ] **Step 3: Write the implementation**

In `harness/factory_mcp.py`:

(a) add `import chat_context` to the harness imports (after `import agent_tools`).

(b) extend `_usage` to accept the build info:

```python
def _usage(metrics, info=None, compacted=False):
    """What the UI needs to see *why* a reply looks the way it does:
    token counts against the window, how generation ended ("length" means
    cut, not finished), and what context management did this turn."""
    out = {k: metrics.get(k) for k in
           ("tokens_per_s", "ttft_s", "eval_count", "prefill_count",
            "done_reason")}
    out["num_ctx"] = CHAT_NUM_CTX
    out["context"] = {"evicted": (info or {}).get("evicted", 0),
                      "dropped": (info or {}).get("dropped", 0),
                      "compacted": compacted}
    return out
```

(c) add the `_compact` method to `Factory` (next to `chat_reply`):

```python
    def _compact(self, session_id, model, wire, summary):
        """One summary generation over the span about to leave the window;
        persisted so it is paid once. Caller already holds the GPU lock.
        Raises on any failure -- the caller falls back to trimming."""
        span, covers_until = chat_context.compaction_span(wire, summary)
        span, _ = chat_context.evict_tool_outputs(span, keep_last=0)
        parts = []
        if summary:
            parts.append("Résumé précédent :\n" + summary["content"])
        parts.extend("{}: {}".format(m["role"], m.get("content") or "")
                     for m in span)
        res = ollama_client.chat(model, chat_context.SUMMARY_PROMPT,
                                 "\n\n".join(parts), temperature=0.1,
                                 num_ctx=CHAT_NUM_CTX)
        content = (res.get("content") or "").strip()
        if not content:
            raise ValueError("summarizer returned nothing")
        new = {"content": content, "covers_until": covers_until,
               "model": model, "ts": time.time()}
        self.chats.set_summary(session_id, new)
        return new

    def _save_calibration(self, session_id, history, metrics):
        if metrics.get("prefill_count"):
            self.chats.set_calibration(session_id, {
                "chars": sum(len(m.get("content") or "") for m in history),
                "tokens": metrics["prefill_count"]})
```

(d) in `chat_reply`, replace the history build and stream start:

```python
            yield "start", None
            fresh = self.chats.get(session_id)
            wire = [{"role": m["role"], "content": m["content"]}
                    for m in fresh["messages"]]
            r = chat_context.ratio(fresh)
            budget = chat_context.prompt_budget(CHAT_NUM_CTX)
            summary = fresh.get("summary")
            compacted = False
            if chat_context.needs_compaction(wire, summary, budget, r):
                yield "compacting", True
                try:
                    summary = self._compact(session_id, session["model"],
                                            wire, summary)
                    compacted = True
                except Exception as e:
                    print("compaction failed: {}".format(e), file=sys.stderr)
            history, info = chat_context.build_prompt([], summary, wire,
                                                      budget, r)
            stream = ollama_client.chat_stream(session["model"], history,
                                               num_ctx=CHAT_NUM_CTX)
```

and replace the two `_usage(metrics)` calls with `_usage(metrics, info, compacted)`, then right before `finished = True` / after `self.chats.append(session_id, msg)` add:

```python
            self._save_calibration(session_id, history, metrics)
```

(e) `factory_web._stream_events` already serializes unknown event kinds generically (`line = {kind: payload}`), so `compacting` flows to the browser with no server change.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest harness/tests/test_factory_mcp.py harness/tests/test_factory_web.py -q`
Expected: all pass (existing chat tests keep passing: `_usage` extras are additive).

- [ ] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -m "feat: chat_reply compacts, trims and calibrates against the context budget"
```

---

### Task 5: `factory_mcp._agent_turn` — same machinery for agent sessions

**Files:**
- Modify: `harness/factory_mcp.py` (`_agent_turn`)
- Modify: `harness/tests/test_factory_mcp.py` (append to the agent section)

**Interfaces:**
- Consumes: `chat_context.*`, `Factory._compact`, `Factory._save_calibration` from Task 4.
- Produces: `_agent_turn` yields `("compacting", True)` when it compacts; its `done` payload and stored metrics carry the same `"context"` dict.

- [ ] **Step 1: Write the failing test**

Append to `harness/tests/test_factory_mcp.py` (agent section; `_agent` and `_fake_agent_stream` already exist there, `_fake_summary_chat` comes from Task 4):

```python
def test_agent_turn_compacts_and_stubs_old_tool_outputs(factory, tmp_path,
                                                        monkeypatch):
    fake = _fake_agent_stream([[("content", "fini")]])
    summarizer = _fake_summary_chat("Objectif : agent.")
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream", fake)
    monkeypatch.setattr(factory_mcp.ollama_client, "chat", summarizer)
    monkeypatch.setattr(factory_mcp, "CHAT_NUM_CTX", 400)
    sid = _agent(factory, "auto", tmp_path)
    for i in range(14):
        factory.chats.append(sid, {"role": "tool", "tool_name": "read_file",
                                   "content": "t%d " % i + "z" * 400, "ts": 1.0})

    events = list(factory.agent_reply(sid, "continue"))

    assert ("compacting", True) in events
    assert events[-1][0] == "done"
    assert events[-1][1]["context"]["compacted"] is True
    assert factory.chat_get(sid)["summary"]["content"] == "Objectif : agent."
    # the summarizer never sees raw tool dumps, only the stub
    assert "zzz" not in summarizer.calls[0]["user"]
    # the agent system prompt still leads, summary right after
    sent = fake.state["calls"][0]["messages"]
    assert str(tmp_path) in sent[0]["content"]
    assert "Objectif : agent." in sent[1]["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest harness/tests/test_factory_mcp.py -q -k stubs_old_tool`
Expected: FAIL (no `compacting` event)

- [ ] **Step 3: Write the implementation**

In `_agent_turn`, replace the history build:

```python
            session = self.chats.get(session_id)
            system_msgs = [{"role": "system",
                            "content": _agent_system(session["workspace"])}]
            wire = []
            for m in session["messages"]:
                msg = {"role": m["role"], "content": m["content"]}
                if m.get("tool_calls"):
                    msg["tool_calls"] = m["tool_calls"]
                if m.get("tool_name"):
                    msg["tool_name"] = m["tool_name"]
                wire.append(msg)
            r = chat_context.ratio(session)
            budget = chat_context.prompt_budget(CHAT_NUM_CTX)
            summary = session.get("summary")
            compacted = False
            if chat_context.needs_compaction(wire, summary, budget, r):
                yield "compacting", True
                try:
                    summary = self._compact(session_id, session["model"],
                                            wire, summary)
                    compacted = True
                except Exception as e:
                    print("compaction failed: {}".format(e), file=sys.stderr)
            history, info = chat_context.build_prompt(system_msgs, summary,
                                                      wire, budget, r)
            stream = ollama_client.chat_stream(session["model"], history,
                                               num_ctx=CHAT_NUM_CTX,
                                               tools=agent_tools.TOOLS)
```

Replace both `_usage(metrics)` calls in `_agent_turn` with `_usage(metrics, info, compacted)`, and after `self.chats.append(session_id, msg)` / `saved = True` add:

```python
                self._save_calibration(session_id, history, metrics)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest harness/tests/test_factory_mcp.py -q`
Expected: all pass (the existing `num_ctx`/tools assertions in agent tests are unchanged).

- [ ] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -m "feat: agent turns compact, evict tool outputs and trim like chat"
```

---

### Task 6: Web UI — compaction chip, divider, context in the meta line

**Files:**
- Modify: `harness/web/app.js` (`setChatMeta`, `streamChat` feed, `sendChat` feed, both history render loops)
- Modify: `harness/web/style.css` (one rule)
- Modify: `harness/tests/test_factory_web.py` (one NDJSON test)
- Modify: `CHANGELOG.md` (Unreleased entry)

**Interfaces:**
- Consumes: `done.context` / `metrics.context` `{evicted, dropped, compacted}`, `session.summary.covers_until`, NDJSON line `{"compacting": true}` from Tasks 4–5.

- [ ] **Step 1: Write the failing web test**

Append to `harness/tests/test_factory_web.py` (chat section; reuses `_fake_chat_stream`):

```python
def test_chat_send_streams_compacting_event(server, factory, monkeypatch):
    monkeypatch.setattr(factory_mcp.ollama_client, "chat_stream",
                        _fake_chat_stream(["ok"]))
    monkeypatch.setattr(factory_mcp.ollama_client, "chat",
                        lambda *a, **kw: {"content": "résumé"})
    monkeypatch.setattr(factory_mcp, "CHAT_NUM_CTX", 400)
    _, s = request(server, "POST", "/api/chats", {"model": "m:1"}, token="t0k3n")
    for i in range(16):
        factory.chats.append(s["session_id"],
                             {"role": "user", "content": "x" * 400, "ts": 1.0})

    code, raw = request(server, "POST",
                        "/api/chats/" + s["session_id"] + "/messages",
                        {"content": "salut"}, token="t0k3n", raw=True)

    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    assert {"compacting": True} in lines
    assert lines[-1]["done"] is True and "context" in lines[-1]
```

Run: `python -m pytest harness/tests/test_factory_web.py -q -k compacting`
Expected: FAIL (no compacting line — wait, Tasks 4–5 already emit it; this test should PASS if Task 4 landed. Run it: if it passes, treat this step as regression cover and move on.)

- [ ] **Step 2: Update `setChatMeta` in `app.js`**

Replace the `done_reason` block at the end of `setChatMeta` with:

```js
  const ctx = met.context || {};
  if (ctx.compacted) s += " · historique compacté";
  if (ctx.dropped) {
    s += " · ⚠ " + ctx.dropped + " messages hors contexte";
    warn = true;
  }
  if (met.done_reason === "length") {
    s += " · ⚠ réponse coupée (limite de génération)";
    warn = true;
  }
  el.textContent = s;
  el.classList.toggle("chat-warn", warn);
```

- [ ] **Step 3: Render the compacting chip in both stream readers**

In `streamChat`'s `feed`, before the `msg.done` branch:

```js
    } else if (msg.compacting) {
      log.append(h("div", { class: "chat-compact",
                            text: "⏳ compactage de l'historique…" }));
```

In `sendChat`'s `feed`, before the `msg.done` branch:

```js
    } else if (msg.compacting) {
      reply.meta.textContent = "⏳ compactage de l'historique…";
```

(`setChatMeta` overwrites it at `done`.)

- [ ] **Step 4: Divider in the history renders**

In the agent history loop, convert `for (const m of session.messages)` to `session.messages.forEach((m, i) => { ... })` and as the first statement inside:

```js
        if (session.summary && i === session.summary.covers_until)
          log.append(h("div", { class: "chat-compact",
                                text: "— messages au-dessus compactés —" }));
```

In the plain-chat branch, replace the `.map(...)` construction with the same `forEach`-append pattern including the divider check, i.e.:

```js
      log = h("div", { class: "chat-log" });
      session.messages.forEach((m, i) => {
        if (session.summary && i === session.summary.covers_until)
          log.append(h("div", { class: "chat-compact",
                                text: "— messages au-dessus compactés —" }));
        const b = chatBubble(m.role, m.content, m.thinking);
        setChatMeta(b.meta, m.metrics);
        if (m.error) {
          b.meta.textContent = "erreur : " + m.error;
          b.meta.classList.add("chat-error");
        }
        log.append(b.bubble);
      });
```

- [ ] **Step 5: Style + syntax check**

Append to `harness/web/style.css` next to the chat rules:

```css
.chat-compact { text-align: center; font-size: 11px; color: var(--dim); margin: 8px 0; }
```

Run: `node --check harness/web/app.js`
Expected: no output (syntax OK)

- [ ] **Step 6: CHANGELOG**

Add at the top of `## Unreleased` in `CHANGELOG.md`:

```markdown
- **Chat context management** — at prompt-build time (session files keep the
  full record): old tool outputs stubbed (`[résultat d'outil effacé]`, last 12
  messages spared), same-model structured compaction above 70 % of the budget
  (summary persisted in the session, pinned as a system message, verbatim tail
  of 6 kept), trim-with-warning as the safety net so Ollama never truncates
  silently. Token budget = `num_ctx − 8192`, chars-per-token calibrated per
  session from real `prefill_count`. UI: "compactage…" chip, "— messages
  au-dessus compactés —" divider, "N messages hors contexte" in the meta line.
  Spec: `docs/design/specs/2026-07-12-chat-context-management-design.md`.
```

- [ ] **Step 7: Full suite + commit**

Run: `python -m pytest harness/tests -q`
Expected: all pass

```bash
git add harness/web/app.js harness/web/style.css harness/tests/test_factory_web.py CHANGELOG.md
git commit -m "feat: chat UI shows compaction, divider and out-of-context warnings"
```

---

### Task 7: Real smoke test

**Files:** none (verification only)

- [ ] **Step 1: Restart the web server** (old process predates this code): kill any running `factory_web.py`, then `py -3 harness/factory_web.py` from the repo root.

- [ ] **Step 2: Drive a compaction end-to-end.** In an existing long session (e.g. `c_b1ef3328`, already past any threshold) or a fresh one padded with a few big pasted messages, send a message. Expected: "⏳ compactage de l'historique…" chip, then a normal streamed reply; the session JSON in `jobs/chats/` gains a `summary` with `covers_until`; the reply's meta line shows `ctx N/32768` with N far below the session's raw size; a reload of the session shows the divider.

- [ ] **Step 3: Confirm no silent truncation.** The meta `ctx used/32768` must stay < 100 % on the compacted turn, and the next turn must reuse the stored summary without a second compaction chip (check: no new summary `ts` in the JSON).

---

## Self-review notes

- Spec coverage: eviction (T1), compaction + span + assembly (T2), persistence (T3), chat integration + failure fallback + calibration (T4), agent integration (T5), UI + changelog (T6), real smoke (T7). Non-goals untouched.
- Type consistency: `summary = {content, covers_until, model, ts}` everywhere; `info = {evicted, dropped, used_summary, est_tokens}`; `context = {evicted, dropped, compacted}`.
- `test_chat_reply_compacts_when_over_threshold` covers_until math: 16 filled + 1 new user = 17 wire messages, tail 6 → 11.

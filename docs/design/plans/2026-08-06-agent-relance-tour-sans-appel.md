# Relancer un tour sans appel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an agent turn ends in prose while its plan is unfinished, the harness asks for the announced call again instead of handing back to the operator — and the stalled turn stops teaching the model that its tools live in another turn.

**Architecture:** Two independent changes in the agent loop (`Factory._agent_turn`, `harness/factory_mcp.py`). First, a stalled assistant message is flagged in the session file and replaced by a neutral placeholder when the transcript is serialised into a prompt — disk keeps the truth, the model does not re-read the refusal. Second, a relance branch beside the existing `MALFORMED_CALL_RETRY` branch: no parsed call, no call markers, `mode == "auto"`, and a plan with at least one unfinished step ⇒ append a system message asking for the call, flag the stalled turn, emit a `notice`, and loop.

**Tech Stack:** Python 3.12, stdlib only in `harness/` (plus `tomli` and `pytest`). Tests in `harness/tests/`, run with `py -3 -m pytest harness/tests -q -m "not browser"` from the repo root (`C:\Users\me\Projects\local-factory`).

**Spec:** `docs/design/specs/2026-08-06-agent-relance-tour-sans-appel-design.md`

## Global Constraints

- Stdlib only in `harness/` except `tomli` and `pytest`. No new dependency.
- Repo root for every command: `C:\Users\me\Projects\local-factory`. Interpreter: `py -3` (3.12 — the pre-push hook and `pytest_runner.py` both judge with it).
- Commit directly on `main` (project convention, `CLAUDE.md`). `git stash`, `reset --hard` and `push --force` are forbidden.
- Every commit must leave `py -3 -m pytest harness/tests -q -m "not browser"` green — the pre-push hook refuses a red push.
- Prompt constants sent to the model are written in English, like `MALFORMED_CALL_RETRY` and `TOOL_PROTOCOL`, whatever language the model answers in.
- Wording of anything the model reads follows the rules `harness/chatgpt_web.py` states and proved live: name the tools, say the call block is a request, claim nothing about what the model may not do, never tell it to suppress its own words.
- Do not add the new anomaly code to `session_analysis.PASSTHROUGH`. `PASSTHROUGH` findings are fed back to the model as advice; the relance message already tells it. This one is a trace fact for the operator and the post-mortems.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `harness/chat_store.py` | sessions on disk, one `set_*`/mutator per field | add `mark_stalled` |
| `harness/factory_mcp.py` | the agent loop and its prompts | add `STALL_ON_WIRE`, `STALL_RETRY`, `STALL_NOTICE`; neutralise flagged messages when building `wire` (~line 1975); add the relance branch in `_agent_turn` (~line 2091) |
| `harness/carnet.py` | plan projection, no I/O | add `unfinished(plan)` |
| `harness/tests/test_chat_store.py` | store unit tests | one test for `mark_stalled` |
| `harness/tests/test_carnet.py` | carnet unit tests | one test for `unfinished` |
| `harness/tests/test_factory_mcp.py` | agent-loop tests on the fake transport | `_seed_plan` helper + five tests |

---

### Task 1: The stalled turn stops being precedent

A message flagged `stalled` reaches the model as a neutral line instead of its own words. Nothing sets the flag yet — Task 2 does. This task is the mechanism and its proof.

**Files:**
- Modify: `harness/chat_store.py` (new method after `set_pending_calls`, line 115-118)
- Modify: `harness/factory_mcp.py` (new constant near `MALFORMED_CALL_RETRY`, line 196; wire loop at line 1975-1981)
- Test: `harness/tests/test_chat_store.py`, `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Consumes: `ChatStore.append`, `ChatStore.get`, `ChatStore._save` (existing); `_fake_agent_stream(turns)` and `_agent(factory, mode, tmp)` (existing test helpers, `harness/tests/test_factory_mcp.py:972` and `:993`).
- Produces: `ChatStore.mark_stalled(session_id, now=None) -> dict` — flags the LAST message whose `role` is `"assistant"` with `"stalled": True` and returns the saved session; a session with no assistant message is left untouched. `factory_mcp.STALL_ON_WIRE` — the placeholder string. Task 2 calls `mark_stalled`.

- [ ] **Step 1: Write the failing store test**

Append to `harness/tests/test_chat_store.py`:

```python
def test_mark_stalled_flags_the_last_assistant_message(tmp_path):
    store = ChatStore(tmp_path)
    sid = store.create("m:1", kind="agent", mode="auto")["session_id"]
    store.append(sid, {"role": "assistant", "content": "premier"})
    store.append(sid, {"role": "user", "content": "continue"})
    store.append(sid, {"role": "assistant", "content": "je n'ai pas les outils"})

    store.mark_stalled(sid)

    messages = store.get(sid)["messages"]
    assert messages[2].get("stalled") is True
    assert not messages[0].get("stalled")      # only the last one
    assert messages[2]["content"] == "je n'ai pas les outils"   # text kept


def test_mark_stalled_is_harmless_without_an_assistant_message(tmp_path):
    store = ChatStore(tmp_path)
    sid = store.create("m:1", kind="agent", mode="auto")["session_id"]
    store.append(sid, {"role": "user", "content": "salut"})

    store.mark_stalled(sid)

    assert not any(m.get("stalled") for m in store.get(sid)["messages"])
```

Check the top of `harness/tests/test_chat_store.py` for how `ChatStore` is imported; if the file imports it differently (e.g. `from chat_store import ChatStore`), match what is already there rather than adding an import.

- [ ] **Step 2: Run it to make sure it fails**

Run: `py -3 -m pytest harness/tests/test_chat_store.py -q -k mark_stalled`
Expected: FAIL, `AttributeError: 'ChatStore' object has no attribute 'mark_stalled'`

- [ ] **Step 3: Implement `mark_stalled`**

In `harness/chat_store.py`, directly after `set_pending_calls` (line 118):

```python
    def mark_stalled(self, session_id, now=None):
        """Flag the last assistant message as a turn that ended without the
        call it announced.

        The transcript is re-serialised into a prompt every turn, so a stall
        written once is replayed as `[assistant]` precedent for the rest of the
        session (session c_14f20c89: four "je n'ai pas les outils dans ce tour"
        in thirteen minutes, each one read again by the turn after it). The flag
        is what lets the loop send a neutral line in its place -- here, on disk,
        the words are kept: the audits and the replays read the real text.
        """
        session = self.get(session_id)
        for msg in reversed(session["messages"]):
            if msg.get("role") == "assistant":
                msg["stalled"] = True
                break
        return self._save(session, now)
```

- [ ] **Step 4: Run the store tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_chat_store.py -q`
Expected: PASS (whole file, not just the two new tests)

- [ ] **Step 5: Write the failing wire test**

Append to `harness/tests/test_factory_mcp.py`, next to the other agent tests (after `test_agent_stops_after_persistent_malformed_tool_calls`, around line 1775):

```python
def test_a_stalled_turn_reaches_the_model_neutralised(
        factory, tmp_path, monkeypatch):
    # The stall's own words are what reproduces it: the transcript is 100 % of
    # the ChatGPT lane's memory and is re-serialised whole every turn
    # (chatgpt_web.render_prompt), so "je n'ai pas les outils dans ce tour"
    # comes back as the model's own precedent. Kept on disk for the audits,
    # neutralised on the wire.
    fake = _fake_agent_stream([[("content", "ok")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    factory.chats.append(sid, {
        "role": "assistant", "ts": time.time(),
        "content": "Je n'ai pas les outils dans ce tour."})
    factory.chats.mark_stalled(sid)

    list(factory.agent_reply(sid, "continue"))

    sent = fake.state["calls"][0]["messages"]
    assert not any("pas les outils" in (m.get("content") or "") for m in sent)
    assert any(m.get("content") == factory_mcp.STALL_ON_WIRE for m in sent)
    kept = factory.chat_get(sid)["messages"]
    assert any("pas les outils" in (m.get("content") or "") for m in kept)
```

- [ ] **Step 6: Run it to make sure it fails**

Run: `py -3 -m pytest harness/tests/test_factory_mcp.py -q -k neutralised`
Expected: FAIL, `AttributeError: module 'factory_mcp' has no attribute 'STALL_ON_WIRE'`

- [ ] **Step 7: Add the constant**

In `harness/factory_mcp.py`, right after `MALFORMED_RETRY_AT = 3` (line 203):

```python
# What a stalled turn says to the model on every later turn, in place of what
# it actually wrote. The words stay on disk; only the wire is rewritten. See
# ChatStore.mark_stalled for why, and STALL_RETRY for what is done about it.
STALL_ON_WIRE = ("(this turn ended without the call it announced; the program "
                 "asked for the call again)")
```

- [ ] **Step 8: Neutralise flagged messages on the wire**

In `harness/factory_mcp.py`, the loop at line 1975 currently reads:

```python
            wire = []
            for m in session["messages"]:
                msg = {"role": m["role"], "content": m["content"]}
                if m.get("tool_calls"):
```

Make it:

```python
            wire = []
            for m in session["messages"]:
                # A stalled turn goes out as a neutral line: its own words are
                # the precedent that reproduces it (see mark_stalled).
                content = STALL_ON_WIRE if m.get("stalled") else m["content"]
                msg = {"role": m["role"], "content": content}
                if m.get("tool_calls"):
```

Leave the rest of the loop untouched.

- [ ] **Step 9: Run the new test, then the whole suite**

Run: `py -3 -m pytest harness/tests/test_factory_mcp.py -q -k neutralised`
Expected: PASS

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: PASS, no test newly red.

- [ ] **Step 10: Commit**

```bash
git add harness/chat_store.py harness/factory_mcp.py harness/tests/test_chat_store.py harness/tests/test_factory_mcp.py
git commit -F- <<'EOF'
feat(agent): a stalled turn stops being its own precedent

A turn that ends in prose is re-read by every turn after it, and for the
ChatGPT lane that is the whole memory: render_prompt re-serialises the
transcript each time. Session c_14f20c89 wrote "je n'ai pas les outils dans ce
tour" four times in thirteen minutes, each one read again by the next turn.

The message is flagged in the session file and leaves the wire as a neutral
line. Disk is untouched -- the audits, the replays and session_analysis read
what was really written. Nothing sets the flag yet.
EOF
```

---

### Task 2: The relance

**Files:**
- Modify: `harness/carnet.py` (new function after `_steps`, line 306-307)
- Modify: `harness/factory_mcp.py` (two constants after `STALL_ON_WIRE`; the `if not calls:` branch at line 2091-2113; the streak reset at line 2114)
- Test: `harness/tests/test_carnet.py`, `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Consumes: `factory_mcp.STALL_ON_WIRE` and `ChatStore.mark_stalled` (Task 1); `carnet.project(messages) -> dict` with a `"plan"` key (existing); `self._log(session_id).anomaly(what, detail)` (existing, `factory_mcp.py:1833`).
- Produces: `carnet.unfinished(plan) -> int` — how many steps the plan still owes; `0` when `plan` is `None`. `factory_mcp.STALL_RETRY` (what the model reads) and `factory_mcp.STALL_NOTICE` (a `{n}` format string, what the operator reads).

- [ ] **Step 1: Write the failing carnet test**

Append to `harness/tests/test_carnet.py`:

```python
def test_unfinished_counts_the_steps_the_plan_still_owes():
    plan = carnet.parse_plan(json.dumps({
        "goal": "g", "steps": [{"step": "un"}, {"step": "deux"}]}))

    assert carnet.unfinished(plan) == 2
    plan["steps"][0]["done"] = True
    assert carnet.unfinished(plan) == 1
    plan["steps"][1]["done"] = True
    assert carnet.unfinished(plan) == 0


def test_unfinished_is_zero_without_a_plan():
    # Progress is defined against the plan (spec C.2), like carnet.stagnation.
    # So is continuation: a planless session is not owed a relance.
    assert carnet.unfinished(None) == 0
```

Check the imports at the top of `harness/tests/test_carnet.py` — it needs `carnet` and `json`; add only what is missing.

- [ ] **Step 2: Run it to make sure it fails**

Run: `py -3 -m pytest harness/tests/test_carnet.py -q -k unfinished`
Expected: FAIL, `AttributeError: module 'carnet' has no attribute 'unfinished'`

- [ ] **Step 3: Implement `unfinished`**

In `harness/carnet.py`, directly after `_steps` (line 306-307):

```python
def unfinished(plan):
    """How many steps the plan still owes. 0 without a plan, deliberately:
    progress is defined against the plan (spec C.2, and see `stagnation`), and
    so is the loop's decision to carry on rather than hand back."""
    return sum(1 for step in _steps(plan) if not step["done"])
```

- [ ] **Step 4: Run the carnet tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_carnet.py -q`
Expected: PASS (whole file)

- [ ] **Step 5: Add the plan-seeding helper and write the failing relance test**

In `harness/tests/test_factory_mcp.py`, next to `_agent` (line 993), add:

```python
def _seed_plan(factory, sid, steps, goal="faire le truc"):
    """A plan in the transcript without spending a scripted turn on it. Same
    message shape `_handle_carnet_tool` writes for `set_plan`, which is what
    `carnet.project` reads back."""
    factory.chats.append(sid, {
        "role": "tool", "tool_name": "plan", "ts": time.time(),
        "content": json.dumps({"goal": goal,
                               "steps": [{"step": s} for s in steps]})})
```

Then append the test, after `test_a_stalled_turn_reaches_the_model_neutralised`:

```python
def test_agent_relances_a_turn_that_ends_without_the_call_it_announced(
        factory, tmp_path, monkeypatch):
    # Session c_14f20c89, msg 93/103/105/109: six clean calls, then a turn of
    # prose announcing the next edit and placing the tools in another turn
    # ("je n'ai pas les fonctions edit_file/run_command exposees dans ce
    # tour"). edit_file was offered the whole time. The loop used to end there
    # and wait for the operator to type "continue".
    stall = ("Je reprends. Je vais patcher Application.cpp.\n"
             "Je n'ai pas les fonctions edit_file exposees dans ce tour.")
    fake = _fake_agent_stream([
        [("content", stall)],
        [("tool_call", {"name": "step_done",
                        "arguments": {"step": 1,
                                      "evidence": "patch applique"}})],
        [("content", "C'est fait.")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    _seed_plan(factory, sid, ["patcher Application.cpp"])

    events = list(factory.agent_reply(sid, "vas-y"))

    assert len(fake.state["calls"]) == 3          # relanced, not handed back
    assert events[-1][0] == "done" and "stopped" not in events[-1][1]
    notices = [payload for kind, payload in events if kind == "notice"]
    assert len(notices) == 1 and "relance 1" in notices[0]
    relanced = fake.state["calls"][1]["messages"]
    assert any(m["role"] == "tool" and m["content"] == factory_mcp.STALL_RETRY
               for m in relanced)
    # ... and the stall does not come back as the model's own words
    assert not any("dans ce tour" in (m.get("content") or "")
                   for m in relanced)
    assert any(m.get("content") == factory_mcp.STALL_ON_WIRE
               for m in relanced)
```

Why turn 2 is `step_done` and not, say, `read_file`: the third turn is prose, and it must end the loop rather than be relanced in its turn — otherwise the fake runs out of scripted turns and the test dies of `IndexError` instead of telling you anything. Closing the only plan step in turn 2 is what makes turn 3 a legitimate final answer. `step_done` on a step whose `done_when` is empty is granted on the model's word (`carnet.verify_claim`: a prose condition has nothing to check), so no file needs to exist for this test.

- [ ] **Step 6: Run it to make sure it fails**

Run: `py -3 -m pytest harness/tests/test_factory_mcp.py -q -k relances`
Expected: FAIL, `AttributeError: module 'factory_mcp' has no attribute 'STALL_RETRY'`

- [ ] **Step 7: Add the two constants**

In `harness/factory_mcp.py`, right after `STALL_ON_WIRE` (added in Task 1):

```python
# The turn ended in prose, announcing an action it never requested, while the
# plan still owes steps. Measured on session c_14f20c89 (2026-08-06, four times
# in thirteen minutes, every one of them AFTER the tail reminder shipped): the
# belief is not "there are no tools" -- all four stalls name edit_file and
# run_command correctly and put them in another turn ("dans ce tour", "au
# prochain tour ou les appels sont effectivement disponibles"). The head block
# and the reminder both answer the July sentence, "I have no access to a tool",
# and nothing answers this one. The second sentence below is what does.
#
# Wording under the rules chatgpt_web.py proved live: name the tools, say the
# block is a request, claim nothing about what the model may not do, and never
# ask it to suppress its own words -- told to emit only the block, it refused.
STALL_RETRY = ("your last reply announced the next action but did not request "
               "it. The turn you were waiting for is this one: the program is "
               "executing tools right now, and the call block is how you ask "
               "for one. Emit the call you just described, as the last thing "
               "in your reply.")
# What the operator sees. There is no cap on the relance (agent_max_iterations
# is 0 by default and the plan is the exit), so the count is the instrument:
# a relance that repeats means the neutralisation is not holding, and Stop is
# the escape. A ChatGPT turn is a message off the account's allowance.
STALL_NOTICE = ("relance {n}: the reply announced an action without requesting "
                "it and the plan is unfinished, so the agent was asked again. "
                "Stop ends this; nothing else caps it.")
```

- [ ] **Step 8: Add the relance branch**

In `harness/factory_mcp.py`, `_agent_turn`. First, next to `malformed_streak = 0` (line 1957), add a counter:

```python
        malformed_streak = 0
        stall_streak = 0
        cut_streak = 0
```

Then the `if not calls:` branch (line 2091). It currently ends with:

```python
                    yield "notice", MALFORMED_CALL_RETRY
                    i += 1
                    continue
                yield "done", _usage(metrics, info, compacted, num_ctx)
                return
            malformed_streak = 0
```

Make it:

```python
                    yield "notice", MALFORMED_CALL_RETRY
                    i += 1
                    continue
                # Prose, with no call scaffolding at all: either a real final
                # answer, or a turn that announced the next action and never
                # requested it. The plan is what tells them apart -- and only
                # in `auto`, the mode that means "work by yourself": in
                # `approve` the operator is already in the loop at every call.
                plan = carnet.project(
                    self.chats.get(session_id)["messages"]).get("plan")
                if session.get("mode") == "auto" and carnet.unfinished(plan):
                    stall_streak += 1
                    self.chats.mark_stalled(session_id)
                    self.chats.append(session_id, {
                        "role": "tool", "tool_name": "system",
                        "content": STALL_RETRY, "ts": time.time()})
                    self._log(session_id).anomaly(
                        "stall_relance",
                        "{} step(s) left".format(carnet.unfinished(plan)))
                    yield "notice", STALL_NOTICE.format(n=stall_streak)
                    i += 1
                    continue
                yield "done", _usage(metrics, info, compacted, num_ctx)
                return
            malformed_streak = 0
            stall_streak = 0
```

`session` is the snapshot read at the top of this iteration, which is why the plan is re-read from `self.chats.get(session_id)` — tool results appended during the turn are not in that snapshot, and a `step_done` from this very turn must count. `session["mode"]` does not change mid-turn, so reading it from the snapshot is fine.

- [ ] **Step 9: Run the relance test**

Run: `py -3 -m pytest harness/tests/test_factory_mcp.py -q -k relances`
Expected: PASS

- [ ] **Step 10: Write the three tests that prove the relance stays in its lane**

Append to `harness/tests/test_factory_mcp.py`:

```python
def test_agent_does_not_relance_when_the_plan_is_finished(
        factory, tmp_path, monkeypatch):
    # A finished plan and a turn of prose is a final answer. Ending there is
    # the behaviour, not the bug.
    fake = _fake_agent_stream([
        [("tool_call", {"name": "step_done",
                        "arguments": {"step": 1, "evidence": "fait"}})],
        [("content", "Tout est fait.")],
    ])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)
    _seed_plan(factory, sid, ["une seule etape"])

    events = list(factory.agent_reply(sid, "vas-y"))

    assert len(fake.state["calls"]) == 2
    assert events[-1][0] == "done"
    assert not [p for k, p in events if k == "notice"]


def test_agent_does_not_relance_without_a_plan(factory, tmp_path, monkeypatch):
    # Progress is defined against the plan (spec C.2) and so is continuation:
    # relancing a planless session would have no exit at all.
    fake = _fake_agent_stream([[("content", "Voila ma reponse.")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "auto", tmp_path)

    events = list(factory.agent_reply(sid, "une question simple"))

    assert len(fake.state["calls"]) == 1
    assert events[-1][0] == "done"
    assert not [p for k, p in events if k == "notice"]


def test_agent_does_not_relance_in_approve_mode(factory, tmp_path, monkeypatch):
    # In `approve` the operator answers every call, so a turn handed back is
    # not a turn lost.
    fake = _fake_agent_stream([[("content", "Je vais patcher le fichier.")]])
    monkeypatch.setattr(factory_mcp.llama_client, "chat_stream", fake)
    sid = _agent(factory, "approve", tmp_path)
    _seed_plan(factory, sid, ["patcher le fichier"])

    events = list(factory.agent_reply(sid, "vas-y"))

    assert len(fake.state["calls"]) == 1
    assert events[-1][0] == "done"
    assert not [p for k, p in events if k == "notice"]
```

- [ ] **Step 11: Run the three, then the whole suite**

Run: `py -3 -m pytest harness/tests/test_factory_mcp.py -q -k "relance or neutralised"`
Expected: PASS

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: PASS. If `test_agent_reply_executes_tools_and_reprompts` or `test_agent_reprompts_a_malformed_tool_call_left_in_thinking` turn red with an `IndexError` out of `_fake_agent_stream`, the relance is firing on a planless session — re-read Step 8, the `carnet.unfinished(plan)` guard is what keeps them green.

- [ ] **Step 12: Commit**

```bash
git add harness/carnet.py harness/factory_mcp.py harness/tests/test_carnet.py harness/tests/test_factory_mcp.py
git commit -F- <<'EOF'
feat(agent): the turn you were waiting for is this one

Session c_14f20c89, messages 80-109, all after the tail reminder shipped: six
clean calls, then a turn of prose announcing the next edit and placing the
tools elsewhere in time -- "je n'ai pas les fonctions edit_file/run_command
exposees dans ce tour". Operator types "continue", four clean calls, same
stall, nudge, stall in the very next turn, nudge, one call, stall. Four in
thirteen minutes, three of them ending a turn by hand. edit_file was offered
throughout, proved by messages 95 and 99 calling it.

So a branch beside the malformed one, with the sentence nothing in the prompt
was answering: the turn it is waiting for is this one. It fires only in `auto`
and only while the plan still owes a step -- progress is defined against the
plan (spec C.2) and so is continuation; a planless session would have no exit.
No cap, like agent_max_iterations: the plan is the exit, Stop is the escape,
and the relance count rides on the notice so the operator can see it burn.

Proposed, not confirmed: the falsifier is a long live session on the account.
EOF
```

---

### Task 3: Say it in the changelog

**Files:**
- Modify: `CHANGELOG.md` (a bullet at the top of `## Unreleased`)

**Interfaces:**
- Consumes: nothing. Runs after Tasks 1 and 2 are committed.
- Produces: nothing code-side.

- [ ] **Step 1: Add the entry**

At the top of the `## Unreleased` list in `CHANGELOG.md`, above the `**Stop reaches a turn asleep in its lane (2026-08-05)**` bullet, matching that entry's shape — bold title with a date, then prose that leads with the measurement:

```markdown
- **Le tour où il dit que les outils sont dans un autre tour (2026-08-06)** —
  session `c_14f20c89`, messages 80–109, tous postérieurs au rappel d'outils de
  `6f7e6f0` : six appels propres, puis un tour de prose qui annonce le patch et
  place les outils ailleurs dans le temps (« je n'ai pas les fonctions
  `edit_file`/`run_command` exposées dans ce tour »). Relance manuelle, quatre
  appels propres, même stall, relance, stall au tour suivant, relance, un appel,
  stall. Quatre en treize minutes, trois qui ont fini un tour à la main — et
  `edit_file` était offert du début à la fin, prouvé par les messages 95 et 99
  qui l'appellent. Le prompt répondait à la phrase de juillet (« je n'ai pas
  accès à cet outil ») et pas à celle-ci. Deux pièces : une branche de relance à
  côté de celle des appels malformés, dont la deuxième phrase dit que le tour
  attendu est celui-ci ; et le tour stallé qui part sur le fil sous une ligne
  neutre, parce que le transcrit est 100 % de la mémoire de la lane ChatGPT et
  que ses quatre refus étaient devenus ce qu'il lisait de plus récent sur ses
  propres outils (disque intact : les audits lisent le vrai texte). La relance
  ne part qu'en mode `auto` et tant que le plan doit une étape — le progrès se
  définit contre le plan (spec C.2), la continuation aussi. Pas de plafond : le
  plan est la sortie, Stop est l'échappement, et le compteur de relances passe
  dans la notice pour que l'opérateur voie brûler l'abonnement. Proposé, pas
  confirmé : le falsificateur est une longue session live sur le compte.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): the relance, and what it was measured against"
```

- [ ] **Step 3: Hand back**

Do not push. Report to the operator: the suite is green, three commits are on `main` ahead of `origin/main`, and the live falsification (a long ChatGPT session reaching the fourth or fifth burst) has not been run.

---

## What this plan does not do

Stated so a reviewer does not look for it: nothing here proves the relance changes the model's behaviour. The tests prove the loop asks again instead of handing back, and that it asks only where it should. Whether `STALL_RETRY` flips a stalled GPT-5.2 into emitting the call is a live measurement on the operator's account — the same falsification the tail reminder in `6f7e6f0` is still waiting for, and which this session has now shown it half-failed.

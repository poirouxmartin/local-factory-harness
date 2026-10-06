# Agent Reliability Loop — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** complete 2026-07-24 (commits 0f38fb86 tasks 1-4, fe04f1fa tasks
5-7, 071aeb7d + follow-up task 8). Full suite green 3.9 + 3.12, pushed. Remaining:
a live GPU run where an auto agent actually calls set_plan/step_done on a real
task. Phase 3 (section E, autonomy/handback) not started.

**Scope:** spec `docs/design/specs/2026-07-23-agent-reliability-loop-design.md`
sections **A** (plan up front, falsifiable checkpoints, potential walls) and
**C.2** (stagnation detection). Phase 1 shipped B + C.1 + C.3. Section **E**
(autonomy contract) stays for Phase 3.

**Goal:** the agent states a plan whose steps have *verifiable* end conditions,
the **harness** ticks the ones it can check itself, and a run that stops
advancing that plan says so out loud — with no hard block anywhere.

**Architecture (inherited, non-negotiable):** the plan is not a new store. It is
a **tool message in the transcript** (`tool_name: "plan"`), so `carnet.project`
reads it back like notes and walls, and it survives a refresh, a crash and a
server-side turn for free. Ticks are transcript messages too (`"step"` when the
agent claims one, `"progress"` when the harness verifies one), which makes
"progress happened here" a fact anyone can read instead of hidden counter state.
The rendered slice still rides at the **tail** of the wire; the system head stays
byte-stable.

**Tech Stack:** Python 3.9 (`py -3.9`), stdlib only, pytest. Frontend untouched
(a tool message already renders as a chip: `harness/web/app.js:915`).

## Global Constraints

- Tests under `py -3.9` (standard) and green under 3.12.
- **KV prefix byte-stable:** the plan, the ticks and the alerts go at the END of
  `wire`. Only *static* text may join `system_msgs` (the plan-first instructions
  of Task 7 are static per session — a test asserts the head does not move when
  the plan changes).
- `carnet.py` stays **pure, no I/O**. Mechanical verification needs the
  filesystem, so it is injected as an `exists(pattern) -> bool` callable owned by
  `factory_mcp`.
- **No hard blocks.** A false claim of completion is contradicted, never
  punished; stagnation is a sentence, never a stop.
- Tools return strings, never raise (`agent_tools` convention).
- Commits: conventional, imperative, ASCII, no AI attribution, direct on `main`,
  `git commit -F -` heredoc.

---

### Task 1: Plan projection (`carnet.parse_plan`, `project()["plan"]`)

The plan and its ticks become part of the carnet projection.

**Files:**
- Modify: `harness/carnet.py`
- Test: `harness/tests/test_carnet.py`

**Interfaces:**
- Produces `parse_plan(content) -> dict|None` and `project(messages)["plan"]`:
  `{"goal": str, "steps": [{"step","done_when","walls","done":bool,"evidence":str}], "revisions": int}`, or `None` when the session has no plan.
- Message conventions: plan = `{"role":"tool","tool_name":"plan","content":<json>}`;
  agent tick = `tool_name:"step"`; harness tick = `tool_name:"progress"`.
- The **last** plan message wins (a revision replaces, never appends), and ticks
  are re-applied on top of it by 1-based index.

- [x] **Step 1: failing test**

```python
def test_plan_projected_with_ticks():
    msgs = [_tool("plan", json.dumps({"goal": "generate an image", "steps": [
                {"step": "install ComfyUI", "done_when": "file:ComfyUI/main.py"},
                {"step": "fetch the VAE", "done_when": "file:models/vae/ae.safetensors",
                 "walls": ["may be gated"]}]})),
            _tool("progress", json.dumps({"i": 1, "evidence": "file:ComfyUI/main.py"}))]
    plan = carnet.project(msgs)["plan"]
    assert plan["goal"] == "generate an image"
    assert plan["steps"][0]["done"] is True
    assert plan["steps"][1]["done"] is False
    assert plan["steps"][1]["walls"] == ["may be gated"]

def test_plan_revision_replaces_and_keeps_ticks():
    ...  # two plan messages: the second is the plan; a tick on step 1 survives

def test_no_plan_is_none():
    assert carnet.project([])["plan"] is None
```

- [x] **Step 2: run, expect fail** (`KeyError: 'plan'`)
- [x] **Step 3: implement** — in `project`, treat `plan` / `step` / `progress`
  tool names like `note`/`wall` (collected, and **skipped from the target
  pairing**: their stored `tool_name` differs from the call name, so the pending
  queue must not be drained looking for them — same bug the `note`/`wall` skip
  documents). Apply ticks after the walk.
- [x] **Step 4: run, expect pass**
- [x] **Step 5: commit** `feat(carnet): project the plan and its checkpoints`

---

### Task 2: Mechanical end conditions (`file_condition`, `newly_satisfied`, `verify_claim`)

What the harness can check without the model's word for it.

**Files:** `harness/carnet.py` · Test: `harness/tests/test_carnet.py`

**Interfaces:**
- `file_condition(done_when) -> str|None` — the path pattern of a machine-checkable
  condition: an explicit `file:<pattern>` (globs allowed), or a bare single token
  that looks like a path (has a separator or an extension, no whitespace).
  Anything else is prose → `None` (agent-declared only).
- `newly_satisfied(plan, exists) -> [(i, done_when)]` — 1-based indexes of
  mechanical steps that hold **now** and are not already ticked.
- `verify_claim(plan, i, exists) -> (ok: bool, reason: str)` — a claim on a
  mechanical step whose file is absent is refused with the reason; a claim on a
  prose step is always accepted (nothing to check); an out-of-range index is
  refused.

- [x] **Step 1: failing test** — `file:` and bare-path detection, prose → None,
  `newly_satisfied` skips ticked steps, `verify_claim` refuses a missing file and
  accepts prose.
- [x] **Step 2: run, expect fail**
- [x] **Step 3: implement** (pure; `exists` is the injected probe)
- [x] **Step 4: run, expect pass**
- [x] **Step 5: commit** `feat(carnet): machine-checkable end conditions`

---

### Task 3: Stagnation (`carnet.stagnation`)

**Files:** `harness/carnet.py` · Test: `harness/tests/test_carnet.py`

**Interfaces:**
- `stagnation(messages) -> int` — how many real actions ran since the last
  progress event (`step` or `progress` message) or, failing that, since the plan
  was set. Bookkeeping messages (`system`, `plan`, `step`, `progress`, `note`,
  `wall`) are not actions.
- `STAGNATION_AT = 8` (spec C.2 says 6-10; the replay measures whether it fires
  where a human would say "it is going in circles").
- `actions(messages) -> int` — the same count over the whole transcript, used by
  the no-plan nudge in Task 4.

- [x] **Step 1: failing test** — 9 failing actions after a plan → `>= STAGNATION_AT`;
  a `progress` message resets the count to what follows it.
- [x] **Step 2: run, expect fail**
- [x] **Step 3: implement**
- [x] **Step 4: run, expect pass**
- [x] **Step 5: commit** `feat(carnet): count actions since the last checkpoint`

---

### Task 4: Render the plan, pinned, with the alerts

**Files:** `harness/carnet.py` · Test: `harness/tests/test_carnet.py`

**Interfaces:**
- `render(carnet, plan_expected=False) -> str`. The plan block and the alert
  lines are **pinned**: charged against the budget first and never dropped
  (spec A.1). Order: plan → alerts → walls → notes → target rows.
- `HOT_BUDGET` rises from 1200 to 2400 chars (~600 tokens, still under the ~800
  the spec allows for the slice) because the plan now shares the space.
- Alerts: stagnation (only with a plan) and, when `plan_expected` and there is no
  plan after `PLAN_NUDGE_AT` actions, a line telling the agent to set one.

- [x] **Step 1: failing test** — a done step renders `[x]`, an open one `[ ]`;
  the stagnation line appears past the threshold; the no-plan nudge only with
  `plan_expected=True`; a 40-step plan still fits `HOT_BUDGET`.
- [x] **Step 2: run, expect fail**
- [x] **Step 3: implement**
- [x] **Step 4: run, expect pass**
- [x] **Step 5: commit** `feat(carnet): pin the plan and the alerts in the hot slice`

---

### Task 5: `set_plan` / `step_done` exposed and handled

**Files:**
- Modify: `harness/carnet.py` (schemas), `harness/factory_mcp.py`
  (`_handle_carnet_tool`), `harness/workspace_memory.py` (`from_session` skip list)
- Test: `harness/tests/test_carnet.py`, `harness/tests/test_factory_mcp.py`,
  `harness/tests/test_workspace_memory.py`

**Interfaces:**
- `set_plan(goal, steps)` — `steps` is an array of `{step, done_when, walls?}`.
  Stored as one `plan` message. Re-callable: that is how the plan is revised.
- `step_done(step, evidence)` — `step` is the 1-based index. Verified through
  `verify_claim`: accepted → a `step` message; refused → `error: ...` and the
  step stays open (the anti-self-delusion of spec A.2, without a block).
- `CARNET_TOOL_NAMES` grows to 5 → they stay read-tools (never gated) via the
  existing `_needs_approval` branch, and `_session_tools` needs no change.
- `workspace_memory.from_session` must skip `plan`/`step`/`progress` for the same
  reason it skips `note`/`wall`.

- [x] **Step 1: failing test** — schema names; `set_plan` appends a `plan`
  message; a `step_done` on a missing file returns `error:` and does not tick; on
  a prose step it ticks.
- [x] **Step 2: run, expect fail**
- [x] **Step 3: implement** — `_handle_carnet_tool` gains the two names; it reads
  the session for the plan and uses the workspace-bound `exists` probe.
- [x] **Step 4: run, expect pass**
- [x] **Step 5: commit** `feat(carnet): set_plan and step_done, verified claims`

---

### Task 6: The harness ticks what it can verify

**Files:** `harness/factory_mcp.py` (`_run_call`) · Test: `harness/tests/test_factory_mcp.py`

**Interfaces:**
- `_exists(workspace) -> callable` — a glob probe confined to the workspace
  (a pattern that escapes it is `False`, never an exception).
- `_tick_plan(session_id, workspace)` — after every tool result, append a
  `progress` message for each newly satisfied mechanical step. Never fatal, never
  duplicated (a satisfied step is ticked once; the transcript is the ledger).

- [x] **Step 1: failing test** — a plan whose file appears gets a `progress`
  message on the next tool call, exactly one, and `stagnation` resets.
- [x] **Step 2: run, expect fail**
- [x] **Step 3: implement**
- [x] **Step 4: run, expect pass**
- [x] **Step 5: commit** `feat(agent): tick plan checkpoints the harness can verify`

---

### Task 7: Plan-first instructions (static head) + tail wiring

**Files:** `harness/factory_mcp.py` (`AGENT_SYSTEM`, `_carnet_tail`) ·
Test: `harness/tests/test_factory_mcp.py`

**Interfaces:**
- `AGENT_SYSTEM` gains the plan-first paragraph (A.1), the falsifiable-condition
  rule (prefer `file:<path>`), the wall-vs-plan rule (A.3: adapt the blocked
  step, revise the plan only if the approach is dead, **never drop the goal on
  your own — ask**). Static text → the KV prefix stays stable.
- `_carnet_tail(session)` passes `plan_expected=(session["mode"] == "auto")`.

- [x] **Step 1: failing test** — head contains the plan rule and is unchanged
  when a plan/tick is appended; the tail nudges in auto mode and not in approve.
- [x] **Step 2: run, expect fail**
- [x] **Step 3: implement**
- [x] **Step 4: run, expect pass**
- [x] **Step 5: commit** `feat(agent): plan-first rules of engagement`

---

### Task 8: Offline replay guardrail + suite + push

**Files:** `harness/tests/test_carnet_replay.py`

- [x] **Step 1:** on `c_d2a43fd5` (the session that motivated the spec, 150 calls,
  no plan): assert the projection finds no plan, that `actions` is far past
  `PLAN_NUDGE_AT`, and that the rendered slice in auto mode carries the no-plan
  nudge — i.e. the run that thrashed for 80 commands would have been told to
  plan. Then a synthetic tick sequence proves stagnation fires within the same
  transcript.
- [x] **Step 2:** `py -3.9 -m pytest harness/tests/ -q` green, and under 3.12.
- [x] **Step 3:** commit + **detached** push (the pre-push hook runs the suite).

---

## Self-Review

**Spec coverage:** A.1 plan-first → Tasks 1, 5, 7. A.1 potential walls → the
`walls` field of a step (Tasks 1, 4). A.2 harness ticks / anti-self-delusion →
Tasks 2, 5, 6. A.3 wall vs plan → Task 7 (prompt) + `set_plan` revision (Task 5).
C.2 stagnation → Tasks 3, 4. Pinned plan, KV safety → Tasks 4, 7.

**Deliberately not in Phase 2:** section E (handback contract), toolset escalation
(C.4), auto-tuning of `STAGNATION_AT` (a threshold earns its value from the
replay, not from a guess).

**Risks:** (1) the model may write prose end conditions and never use `file:`, so
mechanical ticking stays rare — measured, not assumed, and the bare-path
heuristic covers the common case; (2) a pinned plan costs tail tokens every turn
— bounded and priced against the 5 wasted calls it aims to prevent; (3)
`STAGNATION_AT = 8` may fire during legitimately long single steps (a big
download) — it is a sentence, not a stop, which is exactly why it is allowed to
be wrong sometimes.

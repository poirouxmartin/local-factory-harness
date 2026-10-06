# Agent Reliability Loop — Phase 3 Implementation Plan

**Status:** complete 2026-07-24 (commit follows). Full suite green 3.9 + 3.12.

**Scope:** spec `docs/design/specs/2026-07-23-agent-reliability-loop-design.md`
section **E** (autonomy contract). Phases 1-2 shipped B + C + A. This is the
last section of the design.

**Goal:** the agent exhausts everything it can solve itself, and when it must
reach the operator it does so through one of two clearly distinct, precise
handbacks — never a vague "I'm stuck" that ends a turn by accident.

**Architecture (inherited):** a handback is a transcript message
(`tool_name: "handback"`, content = json with a `kind`), like the plan and the
walls. `carnet.project` and `workspace_memory.from_session` skip it (its stored
name differs from the call name). The two tools are carnet read-tools (never
gated). The turn-ending is the only new control flow: `_drain_calls` returns
`"handback"` and `_agent_turn` treats it like `"parked"`.

**No hard blocks:** `request_resource` is *softly* gated — refused with a nudge
(log the wall first) when no dead-end wall is recorded, accepted otherwise. The
"all remaining are external" nuance is prose the prompt carries; the harness
only checks that a wall with empty `remaining` exists.

---

### Task 1: Two handback tools + the wall-justification check (`carnet`)

- `CARNET_TOOLS` grows to 7: `ask_operator(question, options?)` and
  `request_resource(resource, why?)`. They stay read-tools (never gated).
- `resource_justified(walls) -> bool` — pure: a logged wall with empty
  `remaining` proves a dead end.
- `handback` joins `BOOKKEEPING` and the `project()` skip branch (not a target,
  not an action, never paired).
- Tests: naming set of 7, `resource_justified` truth table.

- [x] Step 1-4: tests, implement (pure), run green.

### Task 2: Handle the tools + end the turn (`factory_mcp`)

- `_handle_carnet_tool`: `ask_operator` → `("handback", {kind:"question",...})`,
  refused on a blank question; `request_resource` → checks `resource_justified`
  over the session walls, refused with a log-wall-first nudge when unproven,
  else `("handback", {kind:"resource",...})`.
- `_drain_calls`: after a call runs, if a `handback` message appeared, drop the
  queued calls, yield `("handback", payload)`, return `"handback"`.
- `_agent_turn`: return on `status in ("parked", "handback")`.
- `workspace_memory.from_session` skips `handback`.
- Tests: blank question refused; ask_operator ends the turn e2e (2nd stream turn
  never runs) and drops calls queued behind it; request_resource refused without
  a wall, accepted after a dead-end wall; from_session skips handback.

- [x] Step 1-4: tests, implement, run green.

### Task 3: The autonomy contract in the static head (`AGENT_SYSTEM`)

- The plan-first paragraph gains the contract: exhaust the soluble; decide alone
  with a stated assumption; `ask_operator` only for an operator's decision;
  `request_resource` only after a logged wall, for the single external thing;
  both end the turn, so the ask is immediate and precise. Static → KV stable.
- Test: head contains `ask_operator` and `request_resource` (stability already
  covered by the Phase 2 head-stability test).

- [x] Step 1-4.

### Task 4: Suite + push

- [x] `py -3.9 -m pytest harness/tests -q` green, and under 3.12. Commit + push.

---

## Deliberately not in Phase 3

Credential pre-provisioning (token in env) — spec E marks it a deferred last
resort; robustness first. A live GPU run where an auto agent actually reaches a
real external wall and hands back cleanly — the only thing left to validate for
the whole reliability loop (Phases 1-3).

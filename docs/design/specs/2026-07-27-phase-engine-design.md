# Phase engine and quality gates — design (2026-07-27)

## Why

The agent has a plan (carnet, phases 1-3) and now a priced analysis of what it
wastes. What it does not have is a notion of *where it is*: exploration,
planning, implementation and verification all look the same to the harness, so
nothing can say "you have read 40 files and written nothing" or "you ticked the
last step without running a test".

Everything the carnet added is advisory, by operator decision (phase 2: signal,
never block) — and the measured result is that advisory alone gets ignored: the
live run of 2026-07-25 ticked 5/5 steps against an empty diff, which is what
made `verify_claim` a real gate. That gate works. This generalises it, with the
same restraint.

## Enforcement: hybrid (operator decision, 2026-07-27)

- **Hard refusal only where the check is cheap and the verdict unambiguous.**
  One new gate: a write with no plan, after 3 actions, in `auto` mode. Three
  actions is the floor at which the carnet has already asked for a plan
  (`PLAN_NUDGE_AT`) and been ignored. A one-shot "write this file" task writes
  on action 1 and is never touched.
- **Everything else is a signal** in the carnet tail: the phase itself, a
  complete-but-unverified plan, and — new — what the session has already wasted.

Two gates already exist and stay as they are: `verify_claim` (no tick without
the file the step named) and `resource_justified` (no resource request without a
logged wall). The engine adds a third and names the phases the three of them
guard.

## The phases (evidence, never declaration)

| phase | holds when | exit evidence |
|---|---|---|
| `explore` | no plan | a plan is set |
| `plan` | a plan exists, nothing written since | a write lands |
| `implement` | something written, steps still open | every step ticked |
| `verify` | every step ticked | a verification command ran since the last write |

Derived from the transcript, never from what the model says it is doing: the
model that ticked 5/5 steps against an empty diff would have declared `verify`
just as confidently. Writes and commands come from the assistant's own
`tool_calls` (the tool message holds the output, not the arguments); the plan
and the ticks come from the carnet bookkeeping messages, which are the
authoritative results.

A verification is a command that runs a suite or a checker (pytest, vitest,
npm test, tox, ruff, tsc, cargo test, go test, make test…). Mechanical matching
on the action segment, the same restraint as `carnet._action_of`: no shell
parsing.

## Why the tail, not the system prompt

The findings ride the **carnet tail**, a transient trailing system message. The
system prompt is the KV prefix and is frozen for the life of the session
(measured: a mutated prefix costs ~1 s/turn, 22/07), which is why lessons only
reach the NEXT session. A trailing message invalidates nothing before it, so the
agent can be told what it is wasting *in the session that is wasting it* — at
the cost of its own few lines.

This is the loop the registry has wanted since « analyse post-session auto »:
the analysis already runs at the end of every turn, and now its two costliest
model-actionable findings are quoted back into the next turn.

## Not in scope

- Blocking on a missing verification: a workspace with no runner would lock
  shut. Signal only.
- Phases for the jobs lane: `loop_job` has its ladder and its reviewer gate.
- New anomaly beyond `gate_refused`: a refusal is an incident and belongs in the
  trace, but the findings vocabulary stays as it shipped this morning.

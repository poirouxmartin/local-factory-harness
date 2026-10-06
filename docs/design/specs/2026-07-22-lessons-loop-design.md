# Lessons — the first self-improvement loop (2026-07-22)

## Why

The agent's own attempt at this (`de04bb4`, 2026-07-20) shipped inert and
armed: nothing ever wrote a lesson, `lessons.json` never existed, and
`_apply_lessons` mutated three module-level constants that are not defined
anywhere — `Factory.__init__` would have raised `NameError` the first time a
lesson existed. Verified:

    CRASH: NameError name 'PROACTIVE_CTX_THRESHOLD' is not defined

It also appended lesson text to the agent system prompt, the byte-stable
prefix the 2026-07-19 llama-server migration locked for KV reuse.

The idea is right and the operator wants it. This is the version that works.

## Two stores, because scope differs

A **lesson** is about the agent itself and holds everywhere: "you waste calls
re-running identical commands", "taskkill on python kills the server". Lives
in `jobs/chats/lessons.json`, loads into every session.

A **memory** is about one workspace: "pip install torch into the system python
fails here, use the venv". Lives in `<workspace>/MEMORY.md`, loads only for
that workspace. This is the ROADMAP "Agent memory (anti-redo)" item (operator
ask 2026-07-14, the PDF phase that burned 19 near-identical scripts).

Build order: lessons end to end first, then MEMORY.md on the same funnel.

**MEMORY.md, as shipped (same day, `harness/workspace_memory.py`).** The
capture is narrower than "the same funnel" implied, on purpose: only
`run_command` failures become memories, and only above two occurrences in one
session. A failed `edit_file` ("0 matches") is the agent misreading a file —
that is a lesson about the agent and already has a home. What a shell refuses
to do is a fact about the project.

The file is the store, not a projection of one: entries round-trip through
markdown (`` - `cmd` failed Nx: detail ``) instead of a JSON sidecar, because
MEMORY.md exists to be read by a human. Only the block between the
`factory:auto` markers is regenerated; everything above it is the operator's
and comes back byte-identical. Anything unparseable inside the block is
dropped rather than guessed at.

Not covered by the arithmetic: semantic dead ends ("tried the ONNX export, the
opset is too old"). Those need a hand or an LLM pass over the transcript, and
the operator can simply write them above the marker.

## Capture — two levels

**Live, per incident.** One funnel, `_record_incident(kind, detail)`, wired
where the harness already knows something failed: a tool result starting with
`error:` (including the guardrail refusals shipped today), a malformed tool
call, an empty reply, RepetitionGuard, LoopGuard, length stop, iteration cap.

**End of the agent loop, aggregate.** `session_audit.analyze()` — pure, tested,
already in the repo — turns the whole session into counts: wasted calls,
errors, loops, verdict. Patterns above threshold become lessons.

Both paths write through the same merge: dedup by `pattern`, bump `count`,
refresh `last_seen`. A lesson is
`{pattern, count, suggestion, last_seen}` — a fact plus what to do about it.

## Effect

The rendered block goes into the agent system prompt, **snapshotted once per
session** and cached for the life of the process. Never rebuilt mid-session.

This is the load-bearing constraint: the system prompt is the KV prefix. A
block that changes between turns kills `--cache-reuse` exactly when the
session is going badly and incidents pile up. Lessons recorded during a
session therefore take effect in the *next* one. In-session correction already
has a channel — the tool error string the model reads immediately.

Bounded: 10 lessons max, ordered by count then pattern (deterministic),
truncated at 1500 chars. `MEMORY.md` at 2000.

## Explicitly not doing

**Auto-tuning of runtime thresholds** — the agent's original intent, deferred
to ROADMAP by operator decision. Silent global mutation is unreviewable, and
it is what broke main this week. Lessons must first prove they say true
things; then we let them turn knobs.

## Deleted

`harness/apply_lessons.py` (a one-shot string-surgery patcher that targets
`class ChatStore` in the wrong file and calls `json.dump` without importing
json — it cannot run) and `Factory._apply_lessons` with its three phantom
constants.

## Testing

- `merge`: dedup by pattern, count bumps, `last_seen` refresh.
- `from_audit`: thresholds, and a clean session produces nothing.
- `render`: ordering is deterministic, budget is honoured, empty renders empty.
- Wiring: an incident reaches the store; the injected block is identical
  across two turns of one session even after a new lesson lands.

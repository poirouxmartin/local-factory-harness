# Diff-based edits (SEARCH/REPLACE) — design

Date: 2026-07-17. Status: approved.

## Problem

Whole-file re-emission starves the thinking budget on multi-file jobs. Measured
on j_c32e89fd (3 targets, ~900 lines): ~7-9k output tokens of code per attempt;
the 35b died mid-deliberation (`done_reason: length`, ~6.3k thinking tokens)
without emitting one line of code. A failed job also leaves nothing behind: the
30b's best attempt (6/8) was thrown away by the escalation `reset()`.

Two changes, one chantier:

1. **SEARCH/REPLACE edit mode** for large/multi-file jobs — output drops from
   ~7-9k tokens to a few hundred, returning the thinking budget to the model.
2. **Best-attempt diff persistence** — a failed job carries its best diff.

## Decision 1 — format: SEARCH/REPLACE blocks

Aider-style blocks under the existing `### FILE:` headers:

```
### FILE: harness/loop.py
<<<<<<< SEARCH
def run(self, spec):
    result = self._attempt(spec)
=======
def run(self, spec):
    self.log("start")
    result = self._attempt(spec)
>>>>>>> REPLACE
```

- Several blocks per file allowed; blocks apply in order of appearance.
- Empty SEARCH → new file (REPLACE is the whole content). Only valid when the
  target does not exist yet.
- Empty REPLACE → deletion of the SEARCH span.
- No fences required around the blocks; a fenced variant is tolerated by the
  parser (models love fences).

Unified diffs were rejected: they demand line-number and context arithmetic
that ≤35b local models notoriously fail, and a rejected hunk is opaque
feedback. SEARCH/REPLACE anchors are mechanically checkable and a failure
message ("anchor not found, closest region was …") is directly actionable.

## Decision 2 — routing: same rule as start-stage 3

`edit_mode` is decided per job, never per file:

- Spec override: `edit_mode: "whole" | "diff"` (optional `delegate` argument,
  validated).
- Auto rule otherwise: `len(target_files) > 1 or any target > 300 lines`
  (measured at job start, worktree state) → `"diff"`; else `"whole"`.

Same threshold as the start-stage routing rule adopted 2026-07-17 — one body of
evidence, two knobs. The whole-file path is untouched: small single-target jobs
keep the prompt and parser that already close jobs.

## Decision 3 — anchor failures cost like a NoCode

Application is **all-or-nothing per attempt**: edits are applied to in-memory
copies; any failure discards all of them (the worktree is not touched).

- Each SEARCH must match **exactly once** in the current file text.
  - 0 matches → `AnchorNotFound`, feedback includes the closest region
    (difflib over line windows) and "recopie la région EXACTEMENT".
  - ≥2 matches → `AmbiguousAnchor`, feedback says "add more context lines".
- Exact match only. No whitespace-fuzzy matching, no partial application —
  the model must reason about a file state it can predict.
- Both errors subclass `PatchError`, so the existing NoCode economics apply
  unchanged: reminder retry (does not burn an attempt), `nocode_streak`,
  two in a row → leave the rung.
- The `compile()` syntax gate is kept, applied to the **resulting** `.py`
  content after edits — same safety net as whole-file mode.

## Components

### `harness/patch.py` (extended)

- `extract_edits(reply, target_files) -> {path: [(search, replace), ...]}` —
  parses `### FILE:` sections into ordered S/R blocks. Reuses the header regex
  and the path allowlist / `is_unsafe_path` checks.
- `apply_edits(current: {path: text}, edits) -> {path: new_text}` — pure
  function, raises `AnchorNotFound` / `AmbiguousAnchor` / `NoCode` (no valid
  block at all). Runs the `compile()` gate for `.py` results.
- Whole-file `extract_files` is untouched.

### `harness/loop_job.py`

- `resolve_edit_mode(spec, worktree) -> "whole" | "diff"` implementing
  Decision 2.
- `build_prompt(..., edit_mode)` — diff mode keeps current file contents in
  the prompt (the win is on OUTPUT tokens) and swaps the instruction block:
  format rules + one mini-example. Reminder texts for anchor failures.
- In `_run_ladder`: diff mode calls `extract_edits` + `apply_edits`, writes
  the resulting files exactly where whole-file mode writes today. The
  `except PatchError` branch is shared.
- Best-diff persistence: whenever an attempt strictly improves `tests_passed`
  over the job's best so far, capture `worktree.capture_diff()` and keep
  `best_attempt = {attempt, model, tests_passed, tests_failed, failing, diff}`.
  Included in the result dict on ALL terminal statuses; on `succeeded` the
  final `diff` field stays authoritative. Captured at attempt time, so the
  escalation `reset()` can no longer discard it.

### `harness/factory_mcp.py` / `factory_cli.py`

- `delegate` gains optional `edit_mode`; validation rejects anything but
  `whole`/`diff`. CLI flag `--edit-mode`. `job_result` passes `best_attempt`
  through (it is just part of result.json).

## Testing (TDD, existing suites' style)

- Parser: single/multiple blocks, several per file, fenced and unfenced,
  empty SEARCH (new file, and error when file exists), empty REPLACE
  (deletion), unknown target, unsafe path.
- Applier: exact match, 0-match → AnchorNotFound (closest-region text),
  2-match → AmbiguousAnchor, all-or-nothing on partial failure, compile gate
  on broken result, blocks applied in order.
- Routing: auto rule both sides of the threshold, spec override wins.
- Prompt: diff-mode instruction block present, current files still included.
- Ladder integration (fake `chat_fn`): S/R attempt that goes green; anchor
  failure → reminder retry, streak of 2 leaves the rung; best_attempt present
  on a failed job and survives escalation reset.

## Out of scope

Fuzzy/whitespace-tolerant matching, mid-rung whole-file fallback, prompt-cache
prefix stability (bottlenecks item 4), keep-alive, llama-server.

## Docs

ADR-017 (SEARCH/REPLACE over unified diff, anchor economics) in DECISIONS.md;
update docs/delegation.md (edit_mode, best_attempt) and
docs/bottlenecks-2026-07.md once measured.

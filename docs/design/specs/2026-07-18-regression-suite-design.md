# Job regression suite — design (2026-07-18)

Validated with Martin 2026-07-18. Rationale: every harness/model change so far
(temp, caps, num_ctx, llama-server) was validated on 1-3 jobs. The suite makes
"validate on 10-20 jobs" a one-command operation, which is the foundation for
every other optimisation decision in `docs/backlog-optimisations.md`.

## Decisions (Q&A with Martin)

- **Regression =** green job turning red, AND cost degradation (attempts,
  rungs, duration) vs a stored baseline. Not binary-only, not an aggregate
  score.
- **Corpus =** frozen past jobs from `jobs/j_*/` (real specs, pinned
  `base_sha`). No synthetic specs in v1; add coverage gaps later if needed.
- **Trigger =** manual CLI, two tiers: `smoke` (4-5 jobs, ~30 min, around any
  harness change) and `full` (10-14 jobs, 2-3 h, overnight). No nightly, no
  pre-push hook.
- **Variance =** 1 run per job; on detected regression, auto-replay the failing
  job up to 2 more times — red 2-of-3 = confirmed REGRESSION, else FLAKY
  (reported, non-blocking). Prod temperatures are kept (no temp-0 determinism:
  the sampling config is part of the system under test).

## Architecture

New module `harness/regress.py` + CLI verbs in `harness/factory_cli.py`.
Replays go through the REAL `loop_job` with prod config (llama-server ladder,
temps, caps): the pipeline under test is the pipeline in production.

### Files

- `regression/corpus.toml` — committed manifest. One entry per retained job:

  ```toml
  [thresholds]
  attempts_slack = 1        # DEGRADED if attempts > baseline + slack
  seconds_factor = 1.6      # DEGRADED if total_seconds > factor * baseline
  confirm_replays = 2       # max extra replays to confirm a regression

  [jobs.j_bcf15a3c]
  tier = "smoke"            # smoke | full  (smoke is a subset of full)
  expected = "green"        # green | probe
  note = "diff multi-file crgpd, closer via rung 2"
  ```

  `project`, `base_sha` and the spec itself are READ from
  `jobs/<id>/spec.json` + `jobs/<id>/result.json` — the archived job dir stays
  the single source of truth; the manifest only carries suite metadata.

- `regression/baselines/<job_id>.json` — per-entry baseline:
  `{success, attempts, stages_used, final_model, total_seconds}`.
  Seeded from the archived `result.json` (free — no seed runs needed).
  Refreshed only explicitly (`regress baseline`), never silently.

- `regression/runs/<YYYYMMDD-HHMMSS>/` — one dir per suite run:
  `report.md`, `report.json`, and each replay's `result.json`
  (`<job_id>.json`, plus `<job_id>.confirm1.json` etc. for confirmation
  replays).

### Pipeline change (the only one)

`Worktree.create(repo, job_id, path)` gains an optional `base_sha=None`
parameter (worktree.py:53 currently hardcodes `rev-parse HEAD`). Default
unchanged. `loop_job` threads it through from an optional replay field so a
replay builds its worktree at the ARCHIVED sha — required because merged diffs
would otherwise make the spec tests pass before the model writes anything.
Note: archived base_shas were `main` HEADs at delegation time, so they stay
reachable from history (verified for crgpd-rerun and lucena); `regress check`
verifies resolvability per entry. (The `factory/j_*` branches do NOT pin them —
`worktree.remove()` deletes the branch at job end.)

Replays are throwaway: no new entry under `jobs/` (no history pollution),
worktree torn down in `finally` as usual, GPU serialized via `gpu_lock`.

## CLI

- `factory_cli.py regress check` — no-GPU validation: corpus parses, projects
  reachable, `base_sha` resolvable in each project repo, baselines present.
  Entries that fail are listed and excluded from runs (warning, not crash).
- `factory_cli.py regress run --tier smoke|full [--only j_a,j_b]` — implicit
  check, then sequential replays, then report. Exit code non-zero iff ≥1
  confirmed REGRESSION.
- `factory_cli.py regress baseline --from-run <ts> [--only ...]` — promote an
  accepted run's results to new baselines.

## Verdict rules (per entry, vs baseline)

| Situation | Verdict | Blocking |
|---|---|---|
| expected=green, replay red, red 2-of-3 after confirmation | REGRESSION | yes (exit code) |
| expected=green, replay red, then green on confirmation | FLAKY | no |
| green but attempts > baseline+slack OR seconds > factor×baseline OR final rung above baseline's | DEGRADED | no (warning) |
| green and cheaper than baseline | IMPROVED | no (info) |
| expected=probe, red | expected | no |
| expected=probe, green | PROGRESS | no (candidate to flip to green) |

## Initial curation (~10-14 of the 29 archived jobs)

Coverage axes: whole + diff edit modes, single + multi-file, all 3 rungs
(7b/30b/35b), pytest + vitest + node runners, the 5 projects where still
replayable. 4-5 entries tagged `smoke` (fast, one per axis). 1-2 historically
red hard jobs tagged `probe`. Jobs whose project/base_sha no longer resolves
are dropped by `regress check`.

## Testing

Pytest on the pure logic (corpus parsing, verdict rules, baseline comparison,
report generation) with fixture `result.json`s — zero GPU, same style as the
rest of `harness/tests/`. First real `regress run --tier smoke` doubles as the
e2e validation.

## Out of scope (v1)

Studio UI integration, automatic nightly, systematic N-runs, automatic corpus
growth (new jobs are added by hand: one TOML entry), cross-machine result
sharing.

# Delegation v2 — context files and the regression gate

**Date:** 2026-07-09
**Status:** approved, not implemented
**Goal:** make it possible, and safe, to delegate real work on `local-factory` itself.

## Problem

Delegation works. The end-to-end run of 2026-07-09 had `qwen2.5-coder:7b` create
`factory_demo.py` from a four-test spec in 6.8 s, and returned a clean diff. But
only trivial work can be delegated today, for two reasons.

**The model sees one file.** `build_prompt` gives it the goal, the current contents
of its target, and the last pytest output. Nothing else. Asked to change
`worktree.py`, it cannot see `job_store.py`, so it invents the API. No change
touching more than one module is reachable.

**Nothing protects the 143 existing tests.** A job's judge is only the tests in its
own spec. A model can make those green while breaking every other test in the
project, and the job reports `succeeded`. For a factory that modifies itself, this
is the most dangerous hole in the current design.

## Non-goals

- **No apply path.** `result.json` carries the diff and its `base_sha`; a human runs
  `git apply`. A tool that writes to the real repo is the one thing ADR-012 exists
  to prevent. Not built.
- **No Reviewer gate.** ADR-003's ACCEPT/REJECT pass over the diff stays on the
  ROADMAP. It is an independent quality improvement, not on the critical path.
- **No tool-calling.** Letting the model explore the repo with `read_file`/`grep` is
  the long-term answer and already on the ROADMAP. The small rungs of the ladder are
  not reliable at tool use; a static context list is the cheap 80%.

## Design

### 1. `context_files`

`spec.json` gains an optional `context_files: [str]`, repo-relative. The runner
reads each from the worktree and injects it into the prompt under a header that
says, in words, that these files are reference and must not be rewritten.

No new enforcement is needed. `extract_files` already refuses to write any path
absent from `target_files`, so a context file is read-only by construction.

Validation happens in `delegate`, against the project's `HEAD`, so a typo fails in
milliseconds rather than after the GPU lock is taken:

- `is_unsafe_path` → `UnsafePath` (traversal, absolute, drive letter)
- `forbidden_paths` → `UnsafePath` (a judge file is not reference material)
- a path also present in `target_files` → `UnsafePath`; a file is readable or
  writable, not both
- missing from `HEAD` (`git cat-file -s HEAD:<path>` fails) → `UnsafePath`
- total size over `CONTEXT_BUDGET` (60 000 bytes) → `ContextTooLarge`

The budget is a real constraint, not decoration: the 7b runs at `num_ctx` 16384,
and silently overflowing it reproduces the truncation bug of ADR-008.

Prompt order becomes: goal, instruction, **reference files**, current target files,
last failure. Reference before target, so the model reads the API before the call
site.

### 2. The regression gate

`factory.toml` gains an optional per-project `regression_cmd`: an argv list passed
to the job's Python interpreter, run with `cwd` set to the worktree.

```toml
[projects.local-factory]
path = "C:/Users/me/local-factory"
runner = "pytest"
regression_cmd = ["-m", "pytest", "harness/tests", "-q"]
```

It runs **only when the spec's tests pass.** A failing attempt has already told the
model what to fix; paying 22 s for the project suite on every miss buys nothing.

Immediately before each run, the runner restores the worktree's judge files, since
these ones necessarily live inside the tree:

- tracked files matching `_JUDGE` (via `git ls-files`) → `git checkout --`
- untracked files matching `_JUDGE` (via `git ls-files --others --exclude-standard`)
  → deleted

Deleting the untracked ones is the half that matters. `git checkout --` does not
remove an *added* file, and an added `harness/tests/conftest.py` is exactly how a
model fakes a green suite. Targeting only judge paths means a legitimate new module
the model created is never touched.

If the regression suite fails, its output becomes the failure signal fed back to the
model, and the loop continues — the model gets a chance to fix what it broke. A job
is `succeeded` only when both suites are green.

Plateau detection (ADR-009) keeps watching the *spec* suite's failure count, which is
`0` on every attempt that reaches the gate. A model that repeatedly satisfies its
spec and repeatedly breaks the project therefore looks like a plateau at zero and
escalates to the next rung — which is the right move: it is stuck, and a bigger model
is the only thing left to try. `state.json`'s `tests_passed` / `tests_failed` continue
to describe the spec suite only; the regression verdict is a separate field.

A pleasant property falls out for free: a job that breaks the harness breaks it in
the worktree, not in the harness executing the job. The factory can modify itself
without sabotaging itself mid-flight.

### 3. Verdict

`result.json` gains:

| field | meaning |
|---|---|
| `regression_passed` | `true` / `false` / `null` when the project declares no `regression_cmd` |
| `regression_output` | last 3000 chars of the suite's output |
| `failure_reason` | `spec_tests` \| `regression` \| `no_code` \| `gpu_timeout` \| `null` |

`succeeded` now requires both suites green. `rejected` (the diff grew a judge file)
is unchanged and still outranks everything. `null` for a project without a
regression command is deliberate: absence of a safety net should be visible in the
result, not silent.

### 4. Error handling

- `delegate` validates paths, `HEAD` membership and budget, and refuses before any
  job directory exists. A refused delegation leaves no trace on disk.
- `loop_job` re-validates, so a hand-written `spec.json` cannot smuggle a context
  file past the runner.
- A context file that vanishes between `delegate` and the worktree checkout is
  impossible — the worktree is built from the same `HEAD` the validation read.
- The regression command gets its own timeout (`REGRESSION_TIMEOUT`, 900 s). A
  timeout counts as a regression failure, with `timed_out` recorded.
- A `regression_cmd` that fails to *start* (bad argv) is a job `error`, not a
  regression failure. The distinction matters: one is the model's fault, the other
  is the operator's.

### 5. Files touched

| File | Change |
|---|---|
| `harness/factory_config.py` | `validate_context_files`, `ContextTooLarge`, `regression_cmd` in `Project` |
| `harness/worktree.py` | `restore_judge()` |
| `harness/pytest_runner.py` | `run_regression(worktree, argv, timeout)` |
| `harness/loop_job.py` | context in `build_prompt`; gate after a green spec suite; new result fields |
| `harness/factory_mcp.py` | `delegate` accepts `context_files`; schema and description |
| `factory.toml` | `regression_cmd` for `local-factory` |
| `DECISIONS.md` | ADR-014 |
| `docs/delegation.md` | context files, the gate, and the limitation below |

### 6. Testing

TDD, as the rest of the harness. Real git repos, real pytest, the model faked.
The tests that carry the design:

- a model that makes its spec green while breaking the project suite ends `failed`
  with `failure_reason == "regression"`, not `succeeded`
- a judge file planted in the worktree is gone before the regression suite runs
- a legitimate new module created by the model survives `restore_judge()`
- the regression suite does not run while the spec's tests are red
- a context file appears in the prompt, and a model that tries to write one is
  refused by `extract_files`
- `context_files` over budget is refused by `delegate`, and no job directory is created
- a project with no `regression_cmd` yields `regression_passed is None` and can still
  succeed

## Known limitation

The model may never touch a test. So a delegation can only make changes that keep
the existing suite green **as written**. A behaviour-changing refactor — one whose
correctness requires updating `harness/tests/` — cannot be delegated: its updated
tests must ship in the spec, and the old ones would still fail.

In practice this bounds delegation to additive work and to bug fixes. That is an
acceptable price for a self-modifying system, and it is better named than
discovered.

# Delegation — handing a task to the local ladder

Claude (cloud) writes a spec and walks away. A detached local process takes the GPU,
runs the escalation ladder against a test suite it cannot touch, and leaves a diff.
Claude can die at any point; the job does not notice.

## The four pieces

| File | Role |
|---|---|
| `harness/job_store.py` | The filesystem is the database. One directory per job. |
| `harness/gpu_lock.py` | One GPU, one job. Released by the OS when the holder dies. |
| `harness/loop_job.py` | The executant. Detached. Worktree in, diff out. |
| `harness/factory_mcp.py` | Stdio JSON-RPC MCP server. Five tools. Stateless. |
| `factory.toml` | The allowlist. `delegate` refuses any project not keyed here. |

## Flow

```
Claude (cloud)                    disk                       loop_job.py (detached)
──────────────                    ────                       ─────────────────────
delegate(project, goal,  ──► jobs/j_7f3a/spec.json
        tests, targets)          state.json {queued}
   ◄── "j_7f3a"                  heartbeat            ──►  spawn (stdio → log.txt)
   [Claude may die here]                                   msvcrt lock on .gpu.lock
                                                           git worktree add factory/j_7f3a
                                 state.json {running,       tests → jobs/j_7f3a/tests/
job_status("j_7f3a") ◄──          stage, attempt,           ladder 7b→30b→35b
   "running 11/14"                tests_passed/failed}      heartbeat thread, every 30 s
                                 result.json {diff,         git add -A && git diff --cached
job_result("j_7f3a") ◄──          base_sha, branch}         worktree remove
   diff + test report
```

## The two guarantees

**The loop cannot rewrite its own judge.** The tests never enter the worktree. They
are written to `jobs/<id>/tests/`, pytest is pinned there with a harness-owned
`pytest.ini` and `--confcutdir`, and the worktree only reaches `sys.path` through a
harness-owned `conftest.py` — imported long after `site` ran, so a `sitecustomize.py`
dropped in the tree never executes. `delegate` refuses to make a judge file a target,
and the final diff is rejected if it grew one anyway. See ADR-010.

Restoring the tests after each attempt is *not* equivalent. `git checkout -- tests/`
does not delete files the model added, and this is enough to fake a green run:

```python
# tests/conftest.py, added by the model
import calc
calc.add = lambda a, b: a + b     # 1 passed, exit 0, calc.py still returns a - b
```

**Nothing reaches master.** The job runs on branch `factory/<job_id>` in a throwaway
worktree. Nothing is committed, so nothing can be pushed. `result.json` carries the
diff and the base SHA it applies to. A human, or Opus, reads it. See ADR-012.

**A green spec is not a green job.** The project's own suite (`regression_cmd` in
`factory.toml`) runs inside the worktree once the spec's tests pass, and must pass
too. Before each run the worktree's judge files are checked: if any was modified or
planted, the tree is restored and the job is `rejected` on the spot with
`failure_reason: judge_tampered` — the suite never runs on a tampered tree, and the
stronger rungs never get a turn at the same exploit. The model may never write a
test, so a delegation can only make changes that keep the existing suite green as
written: additive work and bug fixes. Behaviour-changing refactors cannot be
delegated. See ADR-014 and ADR-015.

## Registering the server

```json
{
  "mcpServers": {
    "local-factory": {
      "command": "py",
      "args": ["-3", "C:/Users/me/Projects/local-factory/harness/factory_mcp.py"]
    }
  }
}
```

## Tools

- `delegate(project, goal, tests, target_files, context_files=[])` → `{job_id, status}`.
  Validates and returns immediately. `tests` is `{path: source}` and needs at least one
  `test_*.py`. `target_files` is the only thing the model may write. `context_files` are
  read-only reference files injected into the prompt, capped at 60 000 bytes total and
  validated against the project's HEAD.
  `edit_mode` (`"whole"|"diff"`, optional) forces the output format; without it
  a multi-file job or a >300-line target gets SEARCH/REPLACE edit mode
  (ADR-017), small single-file jobs keep whole-file re-emission.
- `job_status(job_id)` → stage, attempt, model, tests passing. `dead` means the
  heartbeat is older than 90 s and no result was written.
- `job_result(job_id)` → the verdict. `succeeded` | `failed` | `rejected` (the diff
  touched a judge file, or an attempt tampered with one — see `judge_tampered`) |
  `cancelled` | `dead` | `error`. Carries `diff` and `base_sha`. `best_attempt`
  carries the best-scoring attempt (model, tests passed, its diff) even when
  the job failed -- `factory_cli.py result --save-diff` falls back to it when
  the final diff is empty. On a green job,
  `review` holds the advisory Reviewer verdict on the diff (`ACCEPT`/`REJECT` +
  notes) — recorded, never enforced, until it is benchmarked (ADR-016).
- `job_log(job_id, tail)` → tail of `log.txt`.
- `job_cancel(job_id)` → kills the runner; the OS drops the GPU lock.

## Running one by hand — no MCP client, no Claude

`factory_cli.py` is the operator's guarantee: everything the MCP tools do, from a
plain terminal. The full loop by hand:

```
# 1. queue a job (the test file's basename becomes its spec path)
py -3 harness/factory_cli.py delegate --project local-factory \
    --goal "fix add()" --test path/to/test_add.py --target harness/calc.py

# 2. watch it
py -3 harness/factory_cli.py status j_7f3a
py -3 harness/factory_cli.py log j_7f3a --tail 50

# 3. collect the verdict and apply the diff yourself
py -3 harness/factory_cli.py result j_7f3a --save-diff out.patch
git -C <project> apply out.patch        # after reading it

py -3 harness/factory_cli.py cancel j_7f3a   # if it needs killing
```

A runner can also be (re)attached to an existing spec directly:

```
py -3 harness/loop_job.py j_7f3a --jobs jobs --config factory.toml
```

## Limits

- Three runners: pytest, vitest (lucena, conformergpd) and node:test
  (blitzvolley), all built on the same judge-outside-the-tree principle.
- The ladder tops out at `qwen3.6:35b`, ~20 of the machine's 31 GB. Rungs are
  unloaded before the next one loads; two jobs never run at once.

# Decisions (ADR log)

Lightweight architecture decision records. Append; don't rewrite history. Each
entry: context → decision → why. This is where we track what works and what doesn't.

## ADR-001 — Roles are personas, not separate models
**Context:** Single 12 GB GPU. Each distinct model is ~18 GB and swapping thrashes.
**Decision:** A role = a system prompt (`personas/`) + per-request temperature/ctx
via the Ollama API. One base model serves many roles. Compile a separate model only
when a role genuinely needs a different resident config.
**Why:** Avoids VRAM swap cost; lets us have 9 roles without 9 model loads.

## ADR-002 — Version the Modelfiles
**Context:** v0.1.0 deleted the Modelfiles after `ollama create` (per initial spec).
**Decision:** Keep them under `models/Modelfiles/`.
**Why:** A self-improving, reproducible project must be able to rebuild its models
from source. Deleting them was a mistake for a versioned project.

## ADR-003 — Generator→verifier is the core engine
**Context:** A lone coder self-asserts success; that's where agentic loops fail.
**Decision:** The standing loop is Coder + Reviewer + Tester. Reviewer gates the
diff before accept (ROADMAP: wire into loop.py next).
**Why:** The verifier catches "confidently wrong" fixes — the biggest quality lever.

## ADR-004 — Prompt → skills → fine-tune, in that order
**Context:** "Should roles be fine-tuned models or skills?"
**Decision:** Prompt first (personas). Add skills/tools/RAG next for capability &
process. Fine-tune (QLoRA, 7B only on 12 GB) last, and only once run logs provide a
dataset. Never fine-tune 30B locally — VRAM won't allow it.
**Why:** Cheapest, most reversible lever first. The factory's own successful runs
become the future fine-tuning dataset.

## ADR-005 — Bounded + git-safe loop
**Context:** An autonomous loop edits files and runs code.
**Decision:** Cap at 5 attempts (the break); `git checkout` restores the seeded bug
after every run.
**Why:** Prevents thrashing and prevents a bad edit from persisting; keeps tasks reusable.

## ADR-009 — Escalation ladder beats retrying a stuck model
**Context:** On the hard `expr` task (parser w/ precedence, parens, unary minus):
30b MoE solved it in 1 attempt (14/14); 14b plateaued at 11/14 and 7b at 10/14 —
and BOTH produced the *same* failing output on all 5 attempts. At temp 0.1 a model
that can't solve it just regenerates the same wrong answer; feeding the failure back
doesn't help. The 5-attempt break correctly stopped the thrash.
**Decision:** When a model plateaus (same tests_failed N attempts running), don't
keep retrying identically — ESCALATE: bump temperature, then switch to a bigger
model (7b→14b→30b). Add this ladder to loop.py (ROADMAP).
**Why:** Retrying a deterministic stuck model wastes cycles; escalation is where the
extra attempts actually buy something. This is the real value of a model hierarchy.

## ADR-007 — qwen3-coder:30b (MoE) is the default workhorse
**Context:** Measured tok/s: 30b MoE = 64, 7b dense = 84, 27b dense = 8.6. The
30b is `qwen3moe` (~3B active/token); the 27b is dense `qwen35` and emits no
`<think>` — its slowness is architecture + VRAM spill, not deliberation.
**Decision:** Default Coder/Architect/Planner/Reviewer to qwen3-coder:30b (MoE).
Use 7b/14b for throughput; keep qwen3.6:27b only as a rare, opt-in "second opinion".
**Why:** MoE gives near-7b speed with higher quality ceiling. Dense 27b costs 7x
latency for unproven quality gain — benchmark before trusting it.

## ADR-008 — Always set num_ctx explicitly (context gotcha)
**Context:** OpenCode hit "how can I help you?" mid-task — Ollama's OpenAI-compat
`/v1` endpoint defaults raw models to ~4k context, truncating the task away.
**Decision:** Never rely on the default context. The harness sends num_ctx on every
native `/api/chat` call; external tools should point at the ctx-baked `coder`/
`architect` models (or set `OLLAMA_CONTEXT_LENGTH`).
**Why:** Silent truncation looks like "the model forgets"; explicit num_ctx prevents it.

## ADR-010 — The tests live outside the worktree
**Context:** "Tests are the judge" fails the moment a model can reach the judge.
Restoring test files after each attempt (`git checkout -- tests/`) is not enough:
`checkout` does not delete files the model *added*, and an added
`tests/conftest.py` containing `import calc; calc.add = lambda a, b: a + b` prints
"1 passed", exits 0, and leaves the bug in place. Measured, not theorized.
**Decision:** The job's tests are materialized in `jobs/<id>/tests/`, never inside
the worktree. pytest is pinned to that rootdir with a harness-owned `pytest.ini`
and `--confcutdir`, and the worktree is added to `sys.path` by a harness-owned
`conftest.py` — late, after the interpreter finished importing `site`. The model
cannot edit what is not in its tree. `delegate` additionally refuses to name any
judge file as a target, and the final diff is rejected if it touches one.
**Why:** Restoring the judge after the fact is a race you eventually lose. Not
handing it over is not.

## ADR-011 — The filesystem is the job database; liveness is derived
**Context:** No daemon, and Claude may die mid-job. Nothing is left to mark a
killed job as dead, so a `status` field cannot be trusted to say "running".
**Decision:** A job is a directory: `spec.json` (immutable, guarded by an
exclusive `mkdir`), `state.json` (rewritten), `result.json` (write-once),
`heartbeat`, `log.txt`. `job_status` computes liveness from the heartbeat's age
(TTL 90 s), and a present `result.json` always outranks a stale `state.json`.
The heartbeat is beaten by a background *thread*, not by the loop: one 35b call
runs up to 900 s, ten times the TTL. It lives in its own file so that thread and
main loop never read-modify-write the same document.
**Why:** Derived liveness needs no reaper. Two files, two writers, no lock.
**Windows gotcha (measured):** `os.replace` raises `PermissionError [WinError 5]`
when the destination is open by anyone — a concurrent `job_status` read is enough
to break a heartbeat write. Every atomic write retries. And `flock` does not
exist here: the GPU lock is `msvcrt.locking(LK_NBLCK)`, polled rather than
blocking, so a job queued behind the GPU keeps beating instead of looking dead.

## ADR-012 — The deliverable is a diff with a base SHA, never a commit
**Context:** An autonomous loop that can commit is an autonomous loop that can
eventually push.
**Decision:** Jobs run in `git worktree` on branch `factory/<job_id>` under the
job directory. Nothing is committed. `capture_diff` runs `git add -A` *first* —
plain `git diff` cannot see files the model created, so a "write me a new module"
job would return an empty diff with green tests. `result.json` records the base
SHA; a diff without a point of application is a dead deliverable.
**Why:** Nothing reaches master without a human, or Opus, reading it.

## ADR-013 — The MCP server is hand-rolled stdio JSON-RPC
**Context:** The factory runs on Python 3.9; the official `mcp` SDK needs 3.10+.
**Decision:** ~90 lines of newline-delimited JSON-RPC 2.0 in `factory_mcp.py`,
stdlib only, matching `ollama_client.py`'s no-dependency style. Spawned jobs get
stdout/stderr redirected into `log.txt`.
**Why:** The redirect is what makes "detached" true: a job that inherits the
server's pipes dies of EPIPE with it, or blocks forever once the 64 KB pipe
buffer fills and nobody drains it. Revisit when ROADMAP's Python upgrade lands.

## ADR-014 — A job is green only when the project's suite is green
**Context:** A job's judge was only the tests in its own spec. A model could make
those pass while breaking the other 143 tests, and the job reported `succeeded`.
For a factory that modifies itself, that is the sharpest edge in the design.
**Decision:** `factory.toml` declares a per-project `regression_cmd`, run inside the
worktree, but only once the spec's tests pass — a red spec has already told the model
what to fix. Its failure output is fed back to the model, which gets to repair what it
broke. Judge files inside the worktree are restored (tracked) and deleted (added)
before every run: `git checkout --` does not remove an added `conftest.py`, and an
added `conftest.py` is how a model fakes a green suite. The spec also gains
`context_files`, read-only reference material, so a change can span modules.
**Why:** Tests are the judge only if all of them are. The cost is that a delegation
can never touch a test, so it can only make changes that keep the existing suite green
as written — additive work and bug fixes. A behaviour-changing refactor is out of
reach. That is the price of a self-modifying system, and it is better named than
discovered.

## ADR-015 — Touching a judge file rejects the job
**Context:** `restore_judge` put tampered judge files back and logged the fact, then
let the job run on. A tamper that fires only during the spec run (keyed on pytest's
argv, for instance) leaves a clean final diff — the diff gate sees nothing and the
job reads `succeeded`. Reproduced in
`test_a_one_shot_tamper_is_rejected_even_when_the_final_diff_is_clean`.
**Decision:** A non-empty `restore_judge` report is disqualifying: the ladder stops,
the regression suite never runs on the tampered tree, and the job's status is
`rejected` with `failure_reason: judge_tampered` and the offending paths in the
result. The report itself now lists only files that actually differed from HEAD —
reporting every tracked judge file as "restored" would reject every job in any
project that has tests.
**Why:** The tree can be restored; the intent cannot. A model that rewrites its own
judge has forfeited the attempt, and giving a stronger rung a turn at the same
exploit is escalation in the wrong direction.

## ADR-016 — The reviewer's verdict is advisory until benchmarked
**Context:** ADR-003 wants the Reviewer gating diffs. But a local model judging
diffs is an unmeasured judge: enforced blindly, it can reject good work (cost:
a wasted ladder run) or rubber-stamp bad work (cost: false confidence). The same
question will return for every judge the studio staffs (docs/vision.md).
**Decision:** On a green job, the final rung's model (already resident — no swap
cost) reviews the diff with the Reviewer persona; `result.json` carries
`review: {verdict: ACCEPT|REJECT|unparseable|error, notes}`. The verdict never
changes the job's status, and a reviewer crash never costs a job that earned
green. Promotion to a blocking gate requires measuring its agreement rate with
Martin/Claude on real diffs first.
**Why:** Judges earn trust the way models do: by benchmark, not by title. The
advisory phase generates exactly the labeled data the promotion decision needs.

## ADR-006 — Roles staffed now vs deferred
**Context:** Temptation to add CEO / marketing / artist immediately.
**Decision:** Staff Coder, Architect(+Planner), Reviewer, Tester now; Client, Spec
Writer, Creative, Designer as on-demand personas. Defer CEO/marketing (no product yet)
and pixel-art (needs an image model, separate stack).
**Why:** Add a role only when it changes loop output quality; avoid token/VRAM bloat.

## ADR-017 — Large jobs edit by SEARCH/REPLACE, and every job keeps its best diff
**Context:** Whole-file re-emission starved the thinking budget on multi-file jobs:
j_c32e89fd re-emitted ~7-9k tokens of code per attempt and the 35b died
mid-deliberation (`done_reason: length`) without one line of code. The same job's
best attempt (6/8 on the 30b) was thrown away by the escalation reset.
**Decision:** Jobs that are multi-target or touch a >300-line file (the same
threshold as the start-stage-3 routing rule) ask for aider-style SEARCH/REPLACE
blocks. Anchors are exact and must be unique; a miss costs a reminder retry
(NoCode economics) and feeds back the closest region. Application is
all-or-nothing per attempt and the compile() gate runs on the result.
`edit_mode` in the spec overrides the auto rule. Separately, the best-scoring
attempt's diff is captured at attempt time into `best_attempt`, so a failed job
leaves its best work behind.
**Why:** Unified diffs demand line-number arithmetic that <=35b local models
notoriously fail, and a rejected hunk is opaque feedback; an exact anchor is
mechanically checkable and its failure message is actionable. Output tokens are
the scarce resource on this hardware -- a few hundred beats 7-9k every attempt.

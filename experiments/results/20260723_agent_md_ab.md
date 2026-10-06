# agent.md content A/B — 2026-07-23 — FALSIFIED at this scale

ROADMAP item "agent.md content pass": *A/B the content — shorter, imperative,
examples of good/bad tool calls*. The claim under test was never "shorter is
prettier": it was that variant B **lowers wasted and failed tool calls without
losing completion**.

Rig: `experiments/agent_md_variants/bench.py`, qwen3-coder:30b, real
`Factory.agent_reply` (real llama-server, real tools, auto mode), a fresh git
workspace per run seeded byte-identically. The ONLY thing that changes between
arms is `factory_mcp.GLOBAL_AGENT_MD`:

- **A** = `a_current.md`, the repo's current `agent.md` (2915 chars).
- **B** = `b_short.md` (2496 chars): imperative headings, and a "Tool calls"
  section with a good/bad example pair and the "0 matches ⇒ re-read" rule.

7 tasks × 5 runs × 2 variants = 70 sessions (pass 1), plus 10 sessions
re-running one task after its assertion was corrected. Raw:
`results_pass1_20260723.jsonl` / `bench_pass1_20260723.log`, then
`results.jsonl` / `bench.log`.

## The result

| | completion | edit `0 matches` | failed tool results | calls/run | tokens/run |
|---|---|---|---|---|---|
| **A** | **35/35** | 0 | 9 | 3.43 | 71 |
| **B** | **35/35** | 0 | 9 | 3.93 | 75 |

(calls/tokens over the six light tasks; the seventh is broken out below.)

**No measurable difference.** Identical completion, identical failure count,
means separated by less than run-to-run variance. The metrics B exists to move
— `edit_file` returning "0 matches", wasted round-trips — did not fire **once
in 80 sessions**, on either arm.

The most likely reason is that the operative rules are already in
`AGENT_SYSTEM` (`factory_mcp.py:308`): read before you edit, "0 matches" ⇒
re-read, prefer `search`/`read_file` over the shell, and the Windows shell
note. B's "Tool calls" section restates what the system prompt already says, so
there is nothing left for it to move. **Filed as falsified, not deleted**: the
lever may still exist on long multi-step sessions, where these failure modes
actually occur. Re-open it there, not here.

Smoke pass the night before (2026-07-22, 3 tasks × 2 runs,
`results_smoke_20260722.jsonl`) said the same thing with `err=0 miss=0`
everywhere; the four hard tasks were added precisely to give the failure modes
a chance to happen, and they still did not.

## Two measurement bugs found on the way

Both would have produced a confident wrong answer.

**1. The assertion scored the test's shape, not the result.** Pass 1 had A at
33/35, all of it on `add_function_and_test`. Autopsy of the two sessions
(`c_f310560c`, `c_c145f4d7`): the model made the right edits, ran
`python -m pytest` → *No module named pytest*, fell back to `unittest`, got
"Ran 0 tests" on function-style tests, and **rewrote the test file as a
`TestCase`** — right answer, different bytes, and my check was looking for the
literal `puissance(2, 3)`. The task now runs the code (`eval_ok`). Re-run:
**A 5/5, B 5/5**. Had I stopped at pass 1 I would have shipped "B is better",
on two runs of one task, for a reason that was mine.

**2. `tool_errors` only counted outputs starting with `error:`.** A shell
command that fails says so in its exit code, and that round-trip is just as
wasted — so every failed `run_command` of pass 1 read as zero. `score_events`
now uses `workspace_memory.failed`, the rule that already gets this right.
The corrected counts are the ones in the table above.

## The finding that is actually worth acting on

Re-running the hard task with the exit-code rule: **24 failed tool results in
10 sessions**, and the reasons are not about agent.md at all —

| count | reason |
|---|---|
| 10 | `...Python312\python.exe: No module named pytest` |
| 10 | `NO TESTS RAN` (the unittest fallback on function-style tests) |
| 1 | `FAILED (errors=1)` |
| 1 | `error: path escapes the workspace` |

**Every single run** of both arms burned two round-trips discovering that
`python` on this box is a 3.12 without pytest (the project's suite runs under
`py -3.9`). That is the largest single source of wasted tool calls in this
bench, it is systematic, and no wording of `agent.md` fixes it. Options: install
pytest for the 3.12 on PATH, or name the interpreter in the shell note of
`AGENT_SYSTEM` (which is the KV prefix — a byte change to weigh, see
`20260722_ttft_cache_reuse.md`). Filed in ROADMAP.

Third-order note: it is also the exact shape workspace memory exists for, and
as of today a failure once per turn does accumulate across turns
(`workspace_memory.promote`), so a real session in a real workspace would now
write this one down by itself.

### Fixed the same day, and measured

`pip install pytest` on the 3.12 (9.1.1) — at the source rather than in the
prompt, which would only have taught the agent to route around it, and at a
price, since `AGENT_SYSTEM` is the KV prefix. Same task, same model, 4 sessions:

| | failed tool results / session | shell calls / session | calls / run | wall clock |
|---|---|---|---|---|
| before (10 sessions) | 2.4 | 5.8 | A 13.0 · B 11.6 | A 28.7 s · B 21.5 s |
| after (4 sessions) | **0** | **1.0** | A 5.0 · B 7.0 | A 15.9 s · B 11.8 s |

The agent now runs `pytest`, it works, and it stops there — the whole
unittest-fallback detour (which is what rewrote the test file, bug 1 above)
is gone.

Checked before trusting the fix: **the suite is 734 green under 3.12 too**. Had
it been red, installing pytest would have converted a loud failure ("no module
named pytest", which the 07-22 audit taught the judge to classify as an
operator error) into a quiet wrong one — a red regression on a second
interpreter. `py -3.9` remains the documented standard for the hooks and the
studio; this only removes the trap for anything reaching for a bare `python`.

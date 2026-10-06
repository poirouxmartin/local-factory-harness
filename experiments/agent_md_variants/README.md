# agent.md content A/B

Rig for the ROADMAP "agent.md content pass" item. Verdict and full write-up:
`experiments/results/20260723_agent_md_ab.md` (falsified at this task scale).

    python experiments/agent_md_variants/bench.py        # 7 tasks x 5 runs x 2 variants
    BENCH_RUNS=1 BENCH_ONLY=no_scope_creep ...           # smoke one task
    BENCH_VARIANT=A ...                                  # one arm

Long GPU run: launch detached (`Start-Process`), never through a task runner.
`_work/` is scratch, `results.jsonl` and `bench.log` are rewritten per run --
copy them before re-running if the pass is worth keeping.

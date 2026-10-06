# agent_bench

Drives real agent turns through the real `Factory` against a real llama-server
and records what each turn cost, line item by line item.

- `tasks.py` — the corpus. A task seeds a byte-identical git workspace, states
  a goal in the operator's language, and asserts on the resulting workspace by
  RUNNING the code, never by matching literal text (07-23, bug 1: a model that
  rewrote function-style tests as a TestCase scored as a failure).
- `accounting.py` — pure functions, no I/O. Prices the prompt's blocks with the
  server's tokenizer and reports the gap against the wire.
- `bench.py` — the orchestrator. Writes `results.jsonl` and `bench.log`.
- `traps.py` — ranks the failure reasons across bench sessions. The 07-23
  bench's one actionable finding came from this cut.

Long GPU run: launch it DETACHED, never through a task runner.

    Start-Process py -ArgumentList '-3.9','experiments/agent_bench/bench.py' `
      -RedirectStandardOutput run.out -RedirectStandardError run.err

Environment: `BENCH_MODEL` (default `qwen3-coder:30b`), `BENCH_RUNS` (5),
`BENCH_ONLY` (substring filter on task name, for smoke runs), `BENCH_TOOLSET`
(default `base`).

The predecessor, `experiments/agent_md_variants/`, is kept as-is: it holds the
raw results of the 2026-07-23 agent.md A/B.

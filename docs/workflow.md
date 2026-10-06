# Workflow — how the loop runs and how we measure it

## The loop (harness/loop.py)

```
governance (agent.md) + persona  ─┐
task goal + current file + last  ─┼─►  model  ─►  extract code  ─►  write file
pytest output                    ─┘                                     │
        ▲                                                               ▼
        └──────────────  pytest fails (feed output back)  ◄──────  run pytest
                                                                        │ pass
                                                                        ▼
                                                          success → git restore seed
```

- **Governance is injected as the system prompt** — `agent.md` is no longer
  decorative; every call carries it.
- **Bounded:** at most `max_attempts` (default 5) cycles — the 5-error break.
- **Safe:** the seeded bug is restored via `git checkout` after each run, so a
  task is reusable and a bad edit never persists.

## Run it

```bash
# one task, one model (fast smoke)
py -3 harness/loop.py roman --model qwen2.5-coder:7b --save

# full benchmark: models x tasks x N runs, aggregated
py -3 harness/benchmark.py --models qwen2.5-coder:7b qwen2.5-coder:14b \
    --tasks roman calculator --runs 3
```
Results land in `experiments/results/` as JSON + a markdown table (commit them —
that history is how the project sees its own progress).

## Metrics (what we optimize)

| Metric | Meaning | Want |
|--------|---------|------|
| success_rate | % of runs driven to all-green | ↑ |
| mean_attempts | cycles used (cap 5) | ↓ |
| mean_seconds | wall-clock to green | ↓ |
| tokens_per_s | model throughput (per attempt) | ↑ |
| stub_violations | forbidden `pass`/`TODO`/… emitted | 0 |
| tests_failed | remaining failures at stop | 0 |

Because model output is nondeterministic, **always run N≥3** and read the mean +
spread, never a single run. That is the difference between a benchmark and an anecdote.

## Next capabilities (see ROADMAP)
- Add the Reviewer gate between "write file" and "accept".
- Multi-file tasks; a task generator (Client → Spec Writer → Tester).
- Keep-alive / model preloading to kill swap latency.

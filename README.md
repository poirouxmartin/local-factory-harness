# local-factory

A Python harness that drives language models through a bounded
`spec -> diff -> regression -> review` pipeline: quantised open-weight models on a
single 12 GB consumer GPU (llama.cpp / Ollama), and cloud routes when a job needs
more (OpenRouter, Anthropic, OpenAI, Mistral, DeepSeek, Gemini).

Project page: [martinpoiroux.com/en/projects/local-factory](https://martinpoiroux.com/en/projects/local-factory/)

> This repository is a curated snapshot of a private working repository: a single
> commit, without session transcripts, audits or machine-specific files. The code,
> tests, design documents and measurement history are the real ones.

## What it does well

- **A job is green only when the project's own suite is green**, not when the model
  says it is done (ADR-014). Touching a test or judge file rejects the job (ADR-015).
- **The deliverable is a diff against a base SHA**, produced in a git worktree, never
  a commit on the operator's branch (ADR-012). Git hooks in `.githooks/` stop the
  agent from committing on `main` or pushing with a red suite.
- **Escalation ladder**: a stuck model hands over to a stronger one instead of
  retrying the same failure (ADR-009).
- **Measured, including what failed**: every optimisation is A/B tested and written
  down in `experiments/results/` and `DECISIONS.md`; ideas the data killed are filed
  as falsified rather than deleted.
- About 1 300 tests (`py -3 -m pytest harness/tests -q`).

## Why it exists
To turn "an AI that fixes code" into something **measurable and improvable**: a
bounded edit->test loop, a benchmark suite, and a decision log, so it is possible to
see what works, what doesn't, and get better over time.

## Layout
```
local-factory/
├── agent.md                 # governance: rules every agent obeys (system prompt)
├── README / ROADMAP / CHANGELOG / DECISIONS
├── docs/                    # architecture, workflow, roles (+ the org chart)
├── personas/                # one system prompt per role ("the employees")
├── models/Modelfiles/       # versioned, reproducible Ollama model defs
├── tasks/                   # benchmark suite (seeded-bug mini-projects)
│   ├── roman/  └── calculator/
├── harness/                 # loop.py (runner) · benchmark.py · ollama_client.py
└── experiments/results/     # benchmark history (JSON + markdown)
```

## Quickstart
```bash
git config core.hooksPath .githooks                            # once per clone
py -3 harness/loop.py roman --model qwen2.5-coder:7b --save   # one loop
py -3 harness/benchmark.py --runs 3                            # measure
```

`core.hooksPath` arms the two guardrails in `.githooks`: the local agent
cannot commit on `main` (it branches, you merge), and no push leaves with a
red suite. Escape hatch when you know why: `git push --no-verify`.

## Web UI

The studio in a browser -- same `Factory` facade as the MCP server and the CLI:

    py -3 harness/factory_web.py            # binds 127.0.0.1:8787, opens the browser
    py -3 harness/factory_web.py --port 9000 --no-browser

Active sections: **Jobs** (list, live log, verdict + diff), **Déléguer**
(write the tests in the page, pick targets/context from the repo),
**Dashboard** (success rate per model, durations, latest failures).
Goals / Benchmarks / Agents are greyed out until their backend lands.

Every POST carries a per-run random token embedded in the page, so a
drive-by website cannot reach the API. The server holds no state: jobs are
detached processes, and stopping the UI mid-job loses nothing.

## Core ideas
- **Roles are prompts, models are hardware.** One loaded model serves many
  personas via per-request temperature/context — see `docs/roles.md`.
- **Generator → verifier.** Coder + Reviewer + Tester is the quality engine.
- **Measure everything.** N runs, mean + spread; anecdotes don't count.
- **The loop is bounded and safe.** 5-attempt break; git restores after each run.

Start with `docs/workflow.md`, then `docs/roles.md`, then `DECISIONS.md`.

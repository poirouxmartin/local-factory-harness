# --cache-reuse on the form it was built for — 2026-07-23 — FALSIFIED

Line item 4 of the chat/agent token-budget spec. The registry filed
`--cache-reuse 256` FALSIFIED on 2026-07-22 against a stable prefix and a
head mutation, and named the one form it had **not** tested: **mid-prompt
divergence**, which is exactly what compaction produces when it replaces the
oldest span with a summary. This closes that hole.

Rig: `experiments/agent_bench/`, `huge_chain` (a read-gated 18-file chain that
forces ~19 full-file reads, the only task shape the 30b cannot shortcut — see
its docstring), qwen3-coder:30b, `noshell` toolset, chat lane 64k / KV q4.
Every run compacts once (`compact=1`, raw history ~48.7k tokens > the ~40k
threshold). Two arms, two bench invocations: the server spawned with
`--cache-reuse 256` (shipped default) and without (`LLAMA_NO_CACHE_REUSE=1`).
5 runs each. Raw: `results_ab_baseline_20260723.jsonl`,
`results_ab_cacheoff_20260723.jsonl`.

## The measurement

`repaid` = `prefill_count` = tokens the server actually evaluated on the
**last** model call of the turn, which runs on the post-compaction prompt.

| | repaid, per run | mean | reuse % |
|---|---|---|---|
| cache-reuse **ON** | 13663, 11104, 13669, 13710, 13622 | 13154 | 13 |
| cache-reuse **OFF** | 13827, 13711, 2656, 13626, 13676 | 11499 | 27 |

## The result: no benefit

Four of five runs on **both** arms repay ~13.6k tokens. The flag does not
lower the repaid figure — the OFF arm's mean is actually a shade *lower*, on
the strength of one run (2656) that happened to land its last call on a prefix
the native cache already held. That variance is the story of this metric: the
last call repays either ~13.6k or ~2.7k depending only on where the single
compaction fell relative to it, and the flag moves neither number.

Why the flag is powerless here is the same reason it was on 07-22:
**compaction injects a brand-new summary** — tokens the server has never seen —
at the head of the live span. Those must be prefilled no matter what, and the
verbatim tail after them is not recovered by `--cache-reuse` across the
position shift. The lever the registry hoped compaction would vindicate does
nothing on the compaction form either.

**Verdict: FALSIFIED outright**, no longer "the untested form". Kept (falsified
≠ removed), measured cost still nil.

## Honest limits

- **The metric is high-variance run to run.** The bench sees the last model
  call of the turn, not each call; `repaid` therefore depends on where the one
  compaction landed. The signal (no difference between arms) is visible through
  the noise, but a per-call instrument would state it with less hand-waving.
- **The task is flaky at completion** (ok 5/5 ON, 3/5 OFF): the 30b reads all
  18 files but sometimes writes the wrong secret. Completion is not the
  measurement here — every run still compacted at ~19 calls — but it is why the
  arms differ in `ok`.

# Tool-output eviction on trial — 2026-07-23 — KEPT (pays for itself)

Line item 5. The spec put eviction on trial as a charge, not an assumption:
`EVICT_KEEP = 12` stubs tool outputs older than the last 12 messages, which
mutates the **middle** of the prompt and could spend more re-prefill than the
window it saves.

Rig as in `20260723_compaction_cache.md`: `huge_chain`, qwen3-coder:30b,
`noshell`, 64k / KV q4, 5 runs per arm, every run compacts once. Arm A =
`EVICT_KEEP = 12` (shipped). Arm B = eviction off (`BENCH_NO_EVICT=1` sets
`chat_context.EVICT_KEEP` past any session length; no shipped code changes).
Raw: `results_ab_baseline_20260723.jsonl`, `results_ab_evictoff_20260723.jsonl`.

## The result

| | last-call wire | repaid | seconds, per run | mean s |
|---|---|---|---|---|
| eviction **ON** | 15154 | 13154 | 122, 118, 105, 108, 105 | **111** |
| eviction **OFF** | 14753 | 9991 | 116, 126, 129, 217, 210 | **160** |

**Eviction off is ~43 % slower**, and the cost is lumpy: two of five runs
ballooned to ~210 s while the ON arm never passed 122 s. That is eviction
paying for itself — on the mid-turn calls, the un-stubbed arm carries the full
tool outputs of every prior read, and prefilling them is what the extra
minute-and-a-half buys.

**Verdict: KEPT / SHIPPED.** Eviction is not shown to cost more than it saves;
on wall clock it clearly saves. `EVICT_KEEP` stays at **12** — nothing in the
data argues for a different number, and changing a shipped constant on this
evidence would be guessing. No harness change adopted, so no `regress run`
needed for this trial.

## Honest limits — and a metric to fix

- **The last-call wire barely moves (15154 vs 14753), and it should not be read
  as "eviction did nothing".** By the last call, compaction has already
  replaced the bulk with a summary, and the recent tail that remains is inside
  the 12-message window eviction keeps *either way*. Eviction's work happens on
  the **middle** calls of the turn, which the end-of-turn measurement never
  sees. The wall clock, which integrates every call, is where it shows.
- **`evicted_tokens` in the bench conflates compaction and eviction.** It is
  `raw_history − live_sent`, and after compaction the live span is already
  short because the summary covers the rest — so it reads ~36k on **both**
  arms, eviction on or off. It is not a clean eviction meter. Isolating
  eviction properly needs per-call prompt metrics (the bench currently yields
  one `done` per turn); filed as the next instrument if line item 5 is
  re-opened for a precise number.
- **Small, flaky N** (ok 5/5 vs 2/5): the direction (eviction saves time) is
  robust across the five runs; the magnitude is not precise.

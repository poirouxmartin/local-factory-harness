# TTFT / `--cache-reuse` A/B — 2026-07-22

The last unmeasured claim of the llama-server migration (ROADMAP
"Prompt-cache stability"). Raw data: `20260722-114653_ttft_cache_reuse.{json,md}`,
rig `experiments/ttft_cache_reuse_bench.py`.

Rig: qwen3-coder:30b (UD-Q4_K_XL, ncmoe 24, `-ub 2048`), **chat-lane settings**
(ctx 65536, KV q4_0) because that is the lane the byte-stability rule protects.
~3.2k-token system prefix, 5 turns, history resent every turn, `num_predict 16`
(we are measuring prefill, not generation). Two regimes: a byte-identical
prefix across turns, and a prefix whose head changes every turn — which is
exactly what an un-snapshotted lessons/MEMORY.md block would do.

| arm | regime | prompt_n (tokens evaluated) | mean TTFT after turn 0 |
|---|---|---|---|
| cache-reuse **on** | stable prefix | 16-17 | **0.106 s** |
| cache-reuse **off** | stable prefix | 16-17 | **0.098 s** |
| cache-reuse **on** | mutated prefix | 3150-3191 | **1.171 s** |
| cache-reuse **off** | mutated prefix | 3150-3191 | **1.129 s** |

## 1. `--cache-reuse 256` — FALSIFIED on these shapes

The flag changes **nothing**: `prompt_n` is identical to the token between the
on and off arms in both regimes (16/17 stable, 3150→3191 mutated), and the
TTFT difference is noise in the *wrong* direction (off is marginally faster).
Per the standing rule, a lever measured without effect is recorded FALSIFIED,
not deleted — the flag stays in `llama_server_manager.ensure()` at zero
measured cost.

**Untested shape, and it is the one the flag was designed for:** reuse *past a
mid-prompt divergence*. Chat compaction evicts old tool outputs from the middle
of the history, which is precisely that shape, and this bench never produced
it. So the honest verdict is "no effect on prefix-stable and head-mutated
prompts", not "the flag is useless". Measure it against a real compaction turn
before removing it.

## 2. The byte-stable prefix — CONFIRMED, and worth more than expected

A stable prefix evaluates **17 tokens**; a mutated one evaluates **~3170** —
99.5 % of the prefill saved, **~11x on TTFT** (0.10 s vs 1.13 s per turn).

This is the measurement that justifies a constraint already being paid for:
lessons (`0a277d6`) and workspace MEMORY.md (`8121bac`) each snapshot their
block **once per session** specifically so the system prompt stays
byte-identical across turns. Had this come back flat, that design should have
been revisited. It comes back at ~1 s per turn, so keep it — and treat any
future "just append this to the system prompt mid-session" idea as costing a
second per turn from then on.

Note the attribution: the gain comes from llama-server's **native
longest-common-prefix slot caching**, not from `--cache-reuse`. The ROADMAP
item conflated the two. The rule is right; the flag it was filed under is
inert.

## Reading the numbers

`prompt_n` is the honest metric here — it counts tokens actually evaluated, so
a cache hit shows up as a drop. `llama_client.chat` did not return it before
today (`chat_stream` always did); it was added for this bench, because
otherwise the rig would have silently recorded `eval_count` and the whole
measurement would have lied.

The high "prefill tok/s" in the mutated arm (~2800) versus the low one in the
stable arm (~180) is an artefact, not a finding: with 17 tokens to evaluate,
the per-request overhead dominates the rate. Read TTFT and `prompt_n`.

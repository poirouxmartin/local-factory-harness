# Bottlenecks — where the next real gains are (2026-07-17)

Companion to `audit-synthesis-2026-07.md`, but ranked by OUR measured data, not
by what talks recommend. Hardware: RTX 5070 12 GB + 31 GB RAM. Baselines:
MoE 30b/35b ≈ 55-67 tok/s gen, 1084-1332 tok/s prefill, warm TTFT < 2 s;
dense 27b = 9.4 tok/s (never again). FA + KV-q8 flags: falsified (no measurable
gain; the ceiling is CPU-offloaded MoE experts). 64k ctx costs ~7 % gen vs 32k.

The ranking principle, proven three times in July: **a failed job costs minutes;
a slow token costs milliseconds.** Attempt economics dominate throughput.

## 1. Attempt economics (harness) — the proven 10x

Evidence: the ranks.js pilot died twice "outside the envelope of the whole
ladder", then closed 13/13 in 260 s once the harness stopped starving the
thinking budget (8192 cap vs ~6k thinking tokens) and stopped throwing away
`done_reason`. The model was never the bottleneck; our feedback loop was.

Open items, cheapest first:
- **log WHICH tests fail per attempt** — DONE 2026-07-17: `failed_names()` in
  all three runners; names land in log.txt, state.json and each attempt record
  in result.json. Next: use them (routing rule below, richer retry prompts).
- **structured failure taxonomy in result.json** (NoCode / truncation /
  judge_tampered / structural already exist — keep extending; every new
  failure mode gets a name and a counter, so the next fix is data-driven).
- **spec quality is a harness input**: the 2-attempt close had an exact
  contract (mapping table + formula). Vague goal = burned rungs. The /goal
  brief format (docs/goal.md) is the lever, not more model.

## 2. Rung routing (start_stage) — stop burning the small rungs

Today's multi-file job (j_c32e89fd: 3 targets, ~900 lines, 8 spec tests) shows
it live: 7b burned 3 attempts (0/8, then twice tried to write the judge),
14b was still at 0/8 attempts later. On a task this size the low rungs are
noise generators — minutes of GPU plus 2 model swaps before the first
plausible attempt.

`start_stage` exists (T1-T11). What is missing is the ROUTING RULE. Proposed
v0, to falsify against job history:
- >1 target file, or any target > 300 lines, or spec > 6 tests → start at 30b.
- Single small pure function → 7b as today.
Measure: minutes-to-first-plausible-attempt before/after.

*Falsified same day on j_c32e89fd (final numbers): rungs 1-2 burned ~15.5 of
27.5 min for 0/8 plus two judge-write attempts; the dense 14b alone cost 11.5
min at 22.6 tok/s. Rule adopted for the manager; 14b's place in the ladder is
now an open ROADMAP question. Second finding, bigger: whole-file re-emission
(~7-9k tokens of code per attempt on a 629-line target) starves the 35b's
thinking budget — diff-based edits are the real multi-file unlock (ROADMAP).
SHIPPED 2026-07-17 (ADR-017): SEARCH/REPLACE edit mode auto-routed on the same
threshold, plus best_attempt diff persistence. Validation pending: re-run a
j_c32e89fd-class multi-file job in diff mode with --start-stage 3.*

## 3. Model swaps — pay 18 GB less often

Every rung change unloads/reloads up to ~18 GB through a 12 GB card. A full
4-rung ladder = 3 swaps; with routing (item 2) most jobs should see 1-2 models.
Remaining lever: `OLLAMA_KEEP_ALIVE` tuned so the workhorse (30b) stays
resident between jobs and chat turns (ROADMAP "Model preload / keep-alive",
still unmeasured — time a cold vs warm rung start, then decide).

## 4. Prefill, not generation, is the agent cost

An agent attempt re-sends: system + persona + spec + targets (~900 lines
today) + failing-test feedback. At 1084-1332 tok/s prefill, a 12k-token prompt
is ~10 s BEFORE thinking starts, every attempt. Two levers:
- **Prompt-cache stability** (ROADMAP): byte-identical prefix across attempts
  within a run → the server reuses prefix KV instead of re-prefilling.
  Verify with the prefill timings we now record.
- **Context hygiene**: targeted files only (audit consensus: "20k relevant
  beats 100k useless"); the 60 KB context_files cap is the right instinct.

## 5. The throughput ceiling itself — llama-server, time-boxed

FA/KV-q8 falsification localised the ceiling: CPU-offloaded MoE experts.
Ollama cannot steer WHICH experts spill; `llama-server --n-cpu-moe` can
(attention + hot experts on GPU, cold experts on CPU), plus `--ubatch` for
prefill. 5/5 audits converge. Bounded experiment, same bench suite, keep only
measured gains. This is the only tok/s item worth hours.

## 6. The hard wall — 12 GB VRAM

Everything above optimises within the wall. The wall itself: a used 24 GB card
(3090-class) doubles residency, ends most expert spill for 30b-class MoE, and
would raise the "reliable envelope" a full rung. Larger models (70b-class MoE)
become drivable. It is the single biggest jump available (audit consensus:
VRAM > RAM > SSD > CPU) — and a purchase decision, not an engineering one.
Everything in items 1-5 remains valid after it.

## 7. What is NOT a bottleneck

- Generation tok/s of the MoE roster (55-67 tok/s is fine for bounded jobs).
- KV cache quant / flash attention flags (measured: noise).
- The cloud/local split itself: the Claude-Fable audit's strategic point
  stands — cloud plans and reviews, local executes bounded specs. /goal v1
  makes that loop durable; harness quality > model size on this hardware.

## Order of work

1 (test-name logging) → 2 (routing rule) → 3 (keep-alive timing) →
4 (prompt-cache check) → 5 (llama-server time-box) → 6 (hardware, user call).
Items 1-4 are hours each and compound; 5 is a day; 6 is money.

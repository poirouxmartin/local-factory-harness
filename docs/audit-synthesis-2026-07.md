# Audit synthesis — local-LLM workflow (July 2026)

Five independent AI audits (`audits/`: GPT, Gemini, Mistral, Copilot, Claude-Fable)
compiled from YouTube talks on running LLMs locally, most without knowledge of this
project. This doc records where they converge, what we had already validated, and
what actually changes. Roadmap items updated accordingly.

## Convergence (5/5 audits agree)

1. **MoE over dense on 12 GB VRAM** — active-params cost dominates; dense 30B+
   with CPU offload is catastrophic.
2. **Multi-model roster** — small fast model (7–14B) + MoE workhorse (20–40B),
   never one universal model.
3. **Q4_K_M as default quant**; Q5_K_M if it fits, IQ4_XS under pressure, never Q2/Q3.
4. **llama.cpp / `llama-server` as the backend**, running permanently, with expert-level
   offload control (`--n-cpu-moe`), `--ubatch`, `--mlock`, `--no-mmap`, KV cache quant.
5. **Benchmark every change, one variable at a time**; keep a table of
   model / quant / VRAM / **prefill tok/s** / gen tok/s / TTFT.
6. **Context hygiene beats model size** — "20k relevant tokens > 100k useless ones";
   targeted files only, project memory file (CLAUDE.md/agent.md), stable system prompt.
7. **Realistic context ceiling ~32k** (64k max) on 12 GB; KV cache Q8.
8. **Agent pipeline with roles** (architect → coder → tester/reviewer) and human/test
   validation gates.

## Already validated here (audits confirm, no action)

| Audit point | Our artifact |
|---|---|
| MoE > dense, measured | `docs/model-configs.md` table (56.9 vs 9.4 tok/s), ADR-007 |
| Multi-model roster + escalation | ladder 7b→14b→30b→35b, ADR-009 |
| Generator→verifier pipeline | ADR-003, personas |
| Explicit num_ctx, 32k realistic | ADR-008 |
| KV cache Q8, flash attention | `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_FLASH_ATTENTION=1` |
| Project memory file | `agent.md` + personas |
| Bounded loop, tests as gate, diff not commit | ADR-005, ADR-010, ADR-012 |
| Benchmark culture, N runs | `harness/benchmark.py`, `experiments/results/` |
| Speculative decoding / MTP | investigated, N/A for Qwen3.6 (`model-configs.md`) |

## Genuinely new — actions taken or queued

1. **llama-server evaluation promoted Later → Next.** The single biggest convergent
   recommendation. Ollama hides the two levers that matter most on a 12 GB MoE box:
   `--n-cpu-moe` (keep attention + hot experts on GPU, spill only cold experts —
   finer than Ollama's per-layer split) and `--ubatch` (prefill throughput, which is
   what an agent workload is mostly made of). Falsifiable: same model, same bench
   suite, Ollama vs llama-server tuned; keep only measured gains.
2. **Prefill tok/s + time-to-first-token in the metrics.** `ollama_client.py` only
   computes generation tok/s; `prompt_eval_count`/`prompt_eval_duration` are in the
   same response body. Agents are prefill-heavy; we are blind on half the cost.
3. **Prompt-cache stability rule.** Keep the system prompt (agent.md + persona)
   byte-identical across attempts within a run so the server reuses the prefix KV
   cache instead of re-prefilling. Cheap, structural, easy to break by accident.
4. **Strategic validation of delegation-v2.** The only audit that knew the project
   (Claude-Fable) is blunt: fully-local autonomous agents on large projects is
   unrealistic on this hardware — the local 30B drifts on long agentic sessions.
   The profitable split is exactly our architecture: cloud orchestrator
   (plan/review/debug) + local executor of well-specified, bounded tasks. This is
   confirmation, not change — but it re-ranks priorities: harness quality
   (task decomposition, context injection, verification gates) > squeezing tok/s.
5. **Context hygiene for real projects.** Benchmark tasks are small; when delegation
   targets lucena/conformergpd, inject targeted files + their tests, never the repo.
   Queued as a constraint on the vitest-runner item.
6. **Tailscale** for remote access from the laptop — QoL, Later.
7. **Hardware ladder** — consensus: VRAM > RAM > SSD > CPU; a used RTX 3090 24 GB
   is the best single jump. Noted for Later; no purchase implied.

## Explicitly rejected or deferred

- **Switching agents to Cline/Continue/Roo** — audits assume an IDE-assistant
  use case; our harness is headless and already talks to the API directly with
  no intermediate layers, which is the actual point of that advice.
- **Local vector RAG** — premature while tasks fit in 32k with targeted injection;
  revisit when multi-file tasks on real repos land.
- **Weeks of llama.cpp flag-tuning** — Claude-Fable's warning stands: structure and
  context beat flags. The llama-server test gets a bounded time-box, then we decide
  on numbers.

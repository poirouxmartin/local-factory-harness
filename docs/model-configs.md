# Model configs — one tuned model per agent role

Hardware: RTX 5070 (12 GB VRAM) + 31 GB RAM. `OLLAMA_FLASH_ATTENTION=1`,
`OLLAMA_KV_CACHE_TYPE=q8_0` already set globally.

## The rule that governs everything here

On 12 GB, **architecture beats size**. Measured on this machine (4.7k-token prompt):

| Model | Arch | Prompt ingest | Generation |
|---|---|---|---|
| `qwen3.6:35b` | MoE, 3B active | 1332 tok/s | 56.7 tok/s |
| `qwen3-coder:30b` | MoE, 3B active | 1084 tok/s | 56.9 tok/s |
| `qwen3.6:27b` | **dense 27.8B** | 800 tok/s | **9.4 tok/s** |

A dense model pays its full parameter count per generated token, so RAM spill is
catastrophic for it. A MoE pays only for its active experts. Never ship a dense
model above ~14B on this box.

## Sampling — the part that is easy to get wrong

Qwen3.6 models are **thinking models**. Near-greedy sampling (`temperature 0.1`)
makes them fall into repetition loops. The old `coder:latest` (temp 0.1, built on
the non-thinking qwen3-coder) was correct for its base and is wrong for qwen3.6.

Official Qwen3.6-35B-A3B presets:

| Mode | temp | top_p | top_k | min_p | presence_penalty |
|---|---|---|---|---|---|
| Precise coding (thinking) | 0.6 | 0.95 | 20 | 0 | 0.0 |
| General reasoning (thinking) | 1.0 | 0.95 | 20 | 0 | 1.5 |
| Instruct / non-thinking | 0.7 | 0.80 | 20 | 0 | 1.5 |

`qwen3.6:35b`'s Ollama defaults are the *general reasoning* preset — wrong for code.
That is what the tuned models below fix.

### Sampling profiles actually applied (`factory.toml`, spec 2026-07-15 A2)

`[models."<name>"]` in `factory.toml` is loaded by `model_options()` and merged
into every Ollama call for that model — chat turns, agent turns, and ladder
attempts (`harness/loop.py`) — with an explicit per-call `temperature` still
winning. Boolean values are rejected: a `true`/`false` typo silently reads as
Ollama's `1`/`0`.

| Model | temperature | top_p | top_k | other |
|---|---|---|---|---|
| `qwen3.6:35b` | 0.6 | 0.95 | 20 | `presence_penalty` 1.0 |
| `qwen3-coder:30b` | 0.7 | 0.8 | 20 | `repeat_penalty` 1.05 |

Source: the Qwen model cards' documented presets (precise-coding for the
thinking `qwen3.6:35b`; `qwen3-coder:30b` is a non-thinking coder model, so it
keeps its own card's instruct-style preset rather than a thinking preset).
Every ladder/chat/agent call additionally carries a `num_predict` cap
(`harness/ollama_client.py`), and a reply cut short by the cap is reported as
`stopped: "length"` rather than looking like the model chose to stop.

**Modelfile caveat:** `qwen3.6:35b` declares `RENDERER qwen3.5` / `PARSER qwen3.5`.
A derived Modelfile must not override `TEMPLATE`, or the chat format breaks.

## Target roster

| Role | Tuned model | Base | Sampling preset | num_ctx |
|---|---|---|---|---|
| coder (hard) | `coder36` | `qwen3.6:35b` | precise coding | 32768 |
| coder (fast) | `coder-fast` | `qwen2.5-coder:7b` | greedy (non-thinking) | 16384 |
| architect / planner | `architect36` | `qwen3.6:35b` | general reasoning | 65536 |
| reviewer | `reviewer36` | `qwen3.6:35b` | precise coding | 32768 |
| creative / client | `qwen2.5:14b` (untuned) | — | default | 16384 |

`keep_alive` should be short on the 35b models — they occupy ~20 GB of RAM while
resident, which is most of the machine.

## Escalation ladder (`harness/loop_job.py`)

`qwen2.5-coder:7b` → `qwen3-coder:30b` → `qwen3.6:35b`. The 14b rung was
dropped 2026-07-18 (07/2026 audits: it never rescued a 7b failure; the model
stays installed for the Reviewer/Spec-Writer roles). `start_stage` is now
1=7b, 2=30b, 3=35b.
Clean slate on each escalation (ADR-009): handing the stronger model the weaker
one's broken file measurably hurts it.

## Purged

Dense models that cannot run at usable speed here, plus the OpenCode/aider-era
custom models (`coder`, `architect`, `quiz-*`) superseded by this roster:

`qwen3.6:27b`, `deepseek-coder:33b`, `coder:latest`, `architect:latest`,
`quiz-coder`, `quiz-architect`, `quiz-pm`.

Nothing in `harness/` referenced the custom models — the ladder names base models
directly.

## Not available: multi-token prediction

Ollama 0.31.2 parses the `DRAFT` Modelfile directive and ships `--draft-quantize`,
but a draft model must share the target's tokenizer, and no Qwen3.6-tiny exists.
MTP is a Gemma 4 feature (native prediction heads, ~3x). Revisit if we test Gemma 4.

## Not applicable to Claude cloud models

Sampling parameters (`temperature`, `top_p`, `top_k`) are **removed** on Opus 4.8/4.7,
Sonnet 5, and Fable 5 — sending them returns a 400. Cloud-side specialization happens
through system prompts, `output_config.effort`, adaptive thinking, skills, and
subagents. The `personas/*.md` files are the artifact that carries across both sides.

# Roles — the Local Factory org chart

Local Factory behaves like a small company staffed by local LLM agents. But on a
**single 12 GB GPU**, a "role" is almost never a separate model — it's a **persona**
(system prompt) plus **per-request options** (temperature / context) sent through
the Ollama API. One loaded base model can act as many employees. (ADR-001.)

## The staff

| Persona | Base model (suggested) | temp | ctx | Standing? | Job |
|---------|------------------------|------|-----|-----------|-----|
| Coder | qwen2.5-coder:7b or :14b | 0.1 | 32k | ✅ loop | Implement / patch |
| Architect | qwen3-coder:30b | 0.3 | 64–128k | on call | Design across repo |
| Planner | = Architect | 0.3 | 64k | on call | Decompose goal → tasks |
| Reviewer | qwen2.5-coder:14b | 0.1 | 32k | ✅ loop | Verify Coder's diff (ACCEPT/REJECT) |
| Tester | qwen2.5-coder:7b | 0.1 | 16k | ✅ loop | Write/extend tests |
| Client | any | 0.3 | 8k | on demand | Seed a task with a brief + acceptance |
| Spec Writer | qwen2.5-coder:14b | 0.2 | 32k | on demand | Brief → spec + acceptance tests |
| Creative | any | 0.9–1.0 | 16k | on demand | Divergent ideas |
| Designer | any | 0.4 | 16k | on demand | Interface/UX (text), not pixels |

**Standing = runs every loop.** The core quality engine is the generator→verifier
pair: **Coder + Reviewer + Tester**. Everything else is invoked when a new task
needs shaping. Don't add CEO/marketing until the factory ships a real product.

## Model selection — measured (v0.2.0)

Architecture matters more than parameter count on 12 GB:

| Model | Arch | Active/token | tok/s (measured) | Use for |
|-------|------|--------------|------------------|---------|
| qwen3-coder:30b | **MoE** (qwen3moe) | ~3B of 30.5B | **64** | **Workhorse — nearly every role.** Fast *and* strong. |
| qwen2.5-coder:7b | dense | 7B | **84** | Max throughput; simple bounded tasks (aced roman+calc 1-shot). |
| qwen2.5-coder:14b | dense | 14B | ~mid (100% GPU) | Balanced fast coder. |
| qwen3.6:27b | **dense** (qwen35) | 27.8B | **8.6** | Slow specialist. Rare high-stakes decisions only — never in a loop. |

**Key insight:** the 30B *feels* as fast as the 7B because it's **MoE** (sparse) —
only ~3B params fire per token. So you get 30B quality at ~75% of 7B speed. The
27B "thinking" model is slow because it's **dense**, not because it deliberates
(it emitted no `<think>` block). Dense ≠ smarter; MoE 30B likely matches or beats it.

**Default recommendation:** `qwen3-coder:30b` (as baked `coder`/`architect`) for
Coder, Architect, Planner, and Reviewer. Drop to 7b/14b when you want raw speed or
to free VRAM. Reserve qwen3.6:27b as an experimental "second opinion" on a gnarly
design call you're willing to wait ~2 min for — and only adopt it if an A/B shows
it's actually better. (ADR-007.)

## How do you specialize a role: prompt, skill, or fine-tune?

Three tiers, cheapest first. Climb only when the tier below plateaus.

1. **Prompting (personas).** ~90% of role behavior. Instant, free, reversible.
   This is what the files in `personas/` do. Start here for *every* role.

2. **Skills / tools / scaffolding (context augmentation).** Give a model
   *capabilities and knowledge* without touching weights: tool/function calling
   (Ollama supports it), retrieval of project docs (RAG), and a control loop that
   enforces process (our `harness/`). This is the answer to "do we need skills to
   complement the models?" — **yes, and it beats fine-tuning for most role needs.**
   A skill = a repeatable procedure + the tools to execute it. Use skills when the
   model needs to *do* something (run tests, read the repo, call an API) or follow
   a strict workflow — not when it just needs a different tone.

3. **Fine-tuning (LoRA / QLoRA).** Actually train weights for a role's style,
   format, or domain jargon. **Reality on a 12 GB card:** feasible for **7B**
   models with QLoRA; **not** for 30B (needs far more VRAM even quantized). Only
   worth it once (a) prompting + skills clearly plateau AND (b) you have a dataset
   of good examples. Good news: this project *generates its own dataset* — every
   successful loop run (prompt → accepted fix) is a training pair. So the honest
   sequence is: **prompt now → add skills next → harvest run logs → optionally
   QLoRA a 7B "house coder" later.** Don't fine-tune first; it's the slowest,
   most brittle lever.

> Visual roles (artist/designer-with-pixels) need an **image model** (SDXL/Flux),
> a separate stack from the text LLMs. Tracked in ROADMAP, not staffed here.

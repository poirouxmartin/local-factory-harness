# Multi-Agent AI Factory — Architecture Blueprint

## 1. Hardware Envelope

| Component | Spec | Constraint |
|-----------|------|-----------|
| GPU | NVIDIA RTX 5070 | 12 GB VRAM |
| Base model | Qwen 3 Coder MoE 30B (`qwen3-coder:30b`) | ~18 GB on disk, MoE active params fit partial offload |

> ⚠️ VRAM reality check: 12 GB cannot hold the full 30B weights + a 128k KV cache
> resident. The architect's 131072 context is a *capability ceiling*, not a claim
> that every request runs fully GPU-resident. llama-server spills experts / KV
> cache to system RAM as needed (`--n-cpu-moe`). Keep concurrent large-context
> calls serialized.

## 2. Model Roles & Context Splitting

Dedicated context windows are compiled per role so each agent is sized to its job:

| Agent | Model tag | `num_ctx` | `temperature` | Purpose |
|-------|-----------|-----------|---------------|---------|
| Coder | `coder` | 32 768 | 0.1 | Deterministic implementation, patches, tests. Small context = fast, focused. |
| Architect | `architect` | 131 072 | 0.3 | Whole-repo reasoning, design, planning. Large context = holds many files. |

Rationale:
- **Coders** work on a bounded task surface (a file, a function, a failing test).
  32k is plenty and keeps latency + VRAM pressure low. Low temp = reproducible.
- **Architects** must reason over the whole codebase at once. 128k lets them
  ingest many files. Slightly higher temp = room for design exploration.

## 3. Loop Engineering Protocol

The factory runs an autonomous edit → test → observe loop. Rules are strict:

1. **Zero stubs.** No `pass`, no `TODO`, no `NotImplementedError`, no placeholder
   returns. Every emitted unit of code must be complete and runnable.
2. **Internal thinking phase.** Every agent reasons before it acts (plan, then patch).
3. **5-error maximum threshold.** The loop may attempt a fix at most 5 times against
   the same failing signal. On the 5th consecutive failure it **breaks hard**,
   preserves state, and escalates to a human instead of thrashing.
4. **Break on core-choice ambiguity.** If a decision that shapes the architecture
   (framework, data model, protocol) is unresolved, the agent stops and asks —
   it does not guess a foundation.
5. **Test-anchored.** Progress is measured by tests going green, never by the
   agent asserting success. Evidence before claims.

## 4. Toolchain

- **Runtime:** llama.cpp `llama-server` (OpenAI `/v1/chat/completions`); one
  server per model, managed by `LlamaServerManager`. Ollama retired 2026-07-19.
- **VCS:** git (workspace-local repo, `main` protected by convention)
- **Test harness:** pytest (Python testbed for loop validation)
- **Governance:** `agent.md` (rules of engagement, loaded by every agent)

## 5. Directory Layout

```
ai-factory/
├── factory_architecture.md   # this file
├── agent.md                  # global rules of engagement
├── todo.md                   # active feature tracker
├── calculator.py             # testbed subject (contains an intentional bug)
└── test_calculator.py        # pytest spec that pins correct behavior
```

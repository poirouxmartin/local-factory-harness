# Chat/agent token budget — design (2026-07-23)

## Why

Two days of measurement have settled the *speed* of the chat lane (64k ctx,
KV q4, ~59 tok/s on the 30b) and proved that a stable prompt prefix is worth
×11 on TTFT. What none of it settled is **how many tokens a turn actually
costs, and what they buy**.

The evidence that the question is open:

- The `agent.md` content A/B (2026-07-23, 80 sessions) moved nothing. The one
  thing that did move the numbers was an *environment* fix — `pip install
  pytest` on the 3.12 — worth 2.4 → 0 failed tool results per session and
  28.7 → 15.9 s wall clock. We were tuning prose while the waste was
  elsewhere, because we had no map of where the tokens go.
- Plain chat sends **no system prompt at all** (`factory_mcp.py:901`,
  `build_prompt([], ...)`), while an agent turn stacks four blocks
  (`AGENT_SYSTEM` + global `agent.md` + workspace instruction file + lessons +
  workspace memory, `factory_mcp.py:339` and `:1087`). Nobody decided this
  asymmetry; it accreted.
- The `web` toolset ships **15 MCP tool schemas on every single turn**
  (`factory.toml:111`). Their token cost has never been measured once.
- `chat_context.estimate()` is chars/ratio. It is a *transport budget*, and a
  deliberately conservative one. It has never been reconciled against what the
  server reports.

Meanwhile `llama_client.py:107-111` has been returning the ground truth all
along: `prompt_n` (tokens actually evaluated this turn) and `cache_n` (tokens
reused from the KV prefix). Their sum is the true prompt size, to the token.
This design stops estimating and starts reading that.

Scope note: this is block **B (+ the prompt-assets half of E)** of the plan
agreed with Martin on 2026-07-23. The remaining blocks — **D** (end-to-end
chat/agent audit), **A** (speed: 128k, keep-alive/swap, MTP on the chat lane),
**C** (quality/completion corpus), **E** (the learning loop: automatic
post-session analysis) — each get their own spec, in that order, after this
one.

The operational target is not "zero wasted tokens", which is neither
achievable nor measurable. It is: **every line item of a turn's prompt is
counted and justified, or it goes.**

## What we build

### B0 — the chat/agent bench

`experiments/agent_md_variants/bench.py` (375 lines) already does the hard
part: it drives the real `Factory.agent_reply` against a real llama-server
with real tools, seeds a byte-identical git workspace per run, and scores the
result. It is currently welded to one experiment (the `GLOBAL_AGENT_MD` swap).

Promote it to `experiments/agent_bench/`, split by responsibility:

**`tasks.py`** — the corpus. The 7 existing tasks, plus **long multi-step
tasks**. This addition is the point, not padding: the 07-23 A/B found `0
matches` and wasted round-trips firing *zero times in 80 sessions* because
every task was short enough to finish in ~3.4 tool calls. Compaction and tool
eviction never engage on a 3-call session, so the two mechanisms this design
most wants to measure are currently unreachable by the bench.

**`accounting.py`** — pure functions, no I/O. In: the prompt blocks and the
server's counters. Out: the per-line-item decomposition. No GPU needed, so it
is covered by the pytest suite like any other harness module.

**`bench.py`** — the orchestrator: variants × tasks × runs, writing
`results.jsonl` and a committed `.md`. Run **detached** (`Start-Process` plus
file logs), never through Claude Code's background tasks — that infrastructure
killed a smoke run on 07-18.

**Tokenizer.** Per-block counts come from llama-server's `POST /tokenize`.
Step 1 of implementation is a 30-second probe confirming the endpoint exists on
our build. If it does not, fall back to differential measurement (send the
prompt with and without the block, read `prompt_count`) — slower, same
exactness. The design does not depend on which one we get.

### B — the line items

Each becomes one row in `docs/backlog-optimisations.md` with its dated proof,
**including the ones that come back falsified**.

**1. Agent system prompt decomposition.** `AGENT_SYSTEM` + global `agent.md`
(capped at 8000 chars) + workspace instruction file (8000) + lessons block
(1500 chars) + `MEMORY.md`. Deliverable: a table of tokens per block, per
model. This is the map everything else is read against.

**2. Tool schema cost.** 15 MCP schemas per turn on the `web` toolset. Prime
suspect for the largest unmeasured line item. If confirmed, the levers are:
shorter descriptions, narrower toolsets, or on-demand tool loading (the
`ToolSearch` pattern Claude Code itself uses). Measure first, choose after.

**3. Prefix determinism.** Are tool schemas serialised byte-stable from one
turn to the next? A dict whose ordering shifts breaks the KV prefix, and a
broken prefix costs ~1 s per turn (measured 2026-07-22). This is a binary
check at zero cost against a real risk, so it runs first of the seven.

**4. Compaction.** The hole named in the registry: compaction produces exactly
the *mid-prompt divergence* that `--cache-reuse 256` exists to serve, and that
form has never been tested. The flag was filed FALSIFIED on 07-22 against
stable and head-mutated prefixes only. Measure `prompt_n` on the turn *after*
a compaction, flag ON vs OFF.

**5. Tool-output eviction.** `EVICT_KEEP = 12` (`chat_context.py:16`).
Hypothesis stated as a charge against the current behaviour: eviction mutates
the *middle* of the prompt, so it invalidates the KV cache downstream of the
edit. It may be spending 3k tokens of re-prefill to save 2k tokens of window.
If that is what the numbers say, a shipped mechanism is a net negative and
comes out.

**6. Thinking tokens.** The share of `predicted_n` that never reaches the
user, per model. Feeds the `no_think` row already sitting at EN ATTENTE.

**7. Systematic environment traps.** Wasted round-trips are already scored
(`score_events`). The 07-23 bench proved the environment, not the prompt, is
where they come from. Hunt for the remaining traps of the same shape.

### The prompt-assets question (first half of E, merged here)

Decided **with the numbers from items 1-2 in hand**, not before:

- **The chat/agent asymmetry.** Should plain chat carry `agent.md`, a memory
  block, a persona? What does it buy, and at what window cost?
- **A global memory** in the claude-code style, above the existing per-workspace
  `MEMORY.md`.
- **Skills.** The decision rule is already measurable: content that is *stable*
  is nearly free after turn 1 (KV cache) but permanently occupies window;
  content loaded *on demand* costs a re-prefill each time it changes. That is
  an arithmetic trade-off, not a preference.

## Measurement discipline

Non-negotiable, and it is what stopped the 07-23 bench from publishing a
confident wrong answer twice:

- One variable at a time. A/B, N ≥ 5 runs — run-to-run variance here is real.
- A hypothesis that does not move is **FALSIFIED in the registry, not
  deleted**. Conditions change; we do not re-decide from memory.
- Before trusting any result, autopsy at least one raw session per arm.
- Nothing in `harness/` is adopted without the pytest suite green and
  `regress run` passed.
- Long GPU runs detached, file logs, throwaway watcher for notification.

## Deliverables

- `experiments/agent_bench/` with pytest coverage on `accounting.py`; the
  suite stays green (734 at the time of writing).
- `experiments/results/20260723_prompt_budget.md` — the decomposition table.
- One `.md` per A/B that follows.
- `docs/backlog-optimisations.md`, "Chat / agent" section — one dated row per
  line item.
- `ROADMAP.md` — anything the bench uncovers that belongs to block D.
- Direct commits on `main` (project convention), one per validated step.

## Testing

`accounting.py` is pure, so it is unit-tested without a GPU: given known blocks
and known counters, it produces a known decomposition. `score_events` and the
task checks are tested the same way. The bench itself is not unit-tested — it
*is* the test — but every function it leans on is.

The corpus additions carry one specific risk the existing tasks taught us: a
check that scores the *shape* of an answer rather than its result will report a
difference that is the author's, not the model's (07-23, bug 1 — a model that
rewrote function-style tests as a `TestCase` scored as a failure). New tasks
verify by **running the code**, never by matching literal text.

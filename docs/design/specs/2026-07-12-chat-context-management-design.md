# Chat context management: eviction, compaction, trim

**Date:** 2026-07-12
**Status:** approved (Martin, 12/07)
**Prereq:** chat context fix of 12/07 (`CHAT_NUM_CTX = 32768`, per-message
`prefill_count`/`done_reason`/`num_ctx` metrics, UI warnings) — commit `9a25895`.

## Problem

Chat and agent sessions send the full message history on every turn. When the
history outgrows `num_ctx`, Ollama silently drops the oldest tokens: replies
degrade (shorter answers, thinking with no content) and nothing tells the model
or the user what was lost. The 12/07 fix made the overflow *visible*; this
design makes the behavior at context-full *correct*. It is also the
precondition for raising `num_ctx` later: a big window plus this machinery
means effectively unbounded sessions; a big window alone just moves the cliff.

## Approach (mirrors Claude Code)

Three layers, cheapest first, applied at prompt-build time. The session file on
disk always keeps the full record — only what is *sent* to the model changes.

1. **Tool-output eviction** (agent sessions; free). Old tool results are bulky
   and perishable: an 8 000-char `read_file` from 20 turns ago is dead weight,
   but the assistant message that reacted to it carries the conclusion. In the
   prompt, tool-role messages outside the verbatim tail are stubbed to
   `[résultat d'outil effacé]`.
2. **Compaction** (one model call, occasionally). When the estimated prompt
   still exceeds 70 % of the budget, the session's own model summarizes the
   older span with a structured prompt (goal, decisions, facts, state, next
   step). The summary is persisted in the session file and pinned into every
   future prompt: `system + summary + verbatim tail`. Recompaction feeds the
   old summary into the new call.
3. **Trim + warn** (safety net; free). If the prompt still does not fit — or
   the compaction call failed — oldest messages are dropped from the prompt
   until it fits, and the UI says how many. Ollama must never be the one
   truncating.

## Token estimation

No tokenizer is available (stdlib-only; Ollama has no tokenize endpoint).
Estimate `tokens = chars / ratio` with `ratio` defaulting to 3.0 (conservative
for French + code) and calibrated per session: after each reply we know
`prefill_count` (actual prompt tokens) and the chars we sent, so store
`calibration: {chars, tokens}` in the session and use `ratio = chars/tokens`
clamped to [2.0, 5.0]. Estimation errors are safe: the budget keeps a reserve,
and layer 3 plus the existing UI warnings catch the rest.

## Budget math

```
prompt_budget = CHAT_NUM_CTX − OUTPUT_RESERVE − est(system prompt + summary)
OUTPUT_RESERVE = 8192        # thinking models: observed replies up to ~3.4k tok
COMPACT_THRESHOLD = 0.7      # compaction fires above 70 % of prompt_budget
VERBATIM_TAIL = 6            # messages always sent verbatim, never summarized
EVICT_KEEP = 12              # tool outputs within the last 12 messages survive
```

## Components

### `harness/chat_context.py` (new, pure functions, no I/O)

- `ratio(session)` → calibrated chars-per-token.
- `estimate(text_or_messages, ratio)` → token estimate.
- `evict_tool_outputs(messages, keep_last)` → copy with old tool contents
  stubbed (message count unchanged; `tool_calls` on assistant messages kept —
  they are small and carry intent).
- `needs_compaction(messages, summary, budget, ratio)` → bool.
- `compaction_span(messages, summary)` → (messages to summarize, tail) —
  everything not already covered by the stored summary, minus the tail.
- `build_prompt(system_msgs, summary, messages, budget, ratio)` →
  `(messages_to_send, info)` where `info = {evicted, dropped, est_tokens,
  used_summary}`. Applies eviction → summary insertion → trim, in that order.
  Messages at indexes below `summary.covers_until` are never sent — the
  summary replaces them; `dropped` counts only uncovered messages trimmed on
  top of that.
- `SUMMARY_PROMPT` — structured instructions (user's goal and intent, decisions
  taken, facts to remember, current state, next step; same language as the
  conversation).

### `factory_mcp.py`

- In `chat_reply` and `_agent_turn`, replace the inline history build with
  `chat_context.build_prompt(...)`.
- Before the main generation, if `needs_compaction`: yield a `("compacting",
  None)` event, call `ollama_client.chat()` (non-stream, temp 0.1, same model,
  same held GPU lock) with `SUMMARY_PROMPT` over the span, persist
  `session["summary"] = {content, covers_until, model, ts}` via a new
  `ChatStore.set_summary`. On any failure (exception, empty content): log to
  stderr, skip — layer 3 covers it. A compaction failure never fails the turn.
- After a successful reply, persist `session["calibration"]` from
  `prefill_count` and the chars actually sent.
- `done` payload and stored metrics gain `context: {evicted, dropped,
  compacted: bool}`.

### Prompt assembly

`[agent system prompt (agent only)] + [summary as a system message:
"Résumé de la conversation antérieure : …"] + [verbatim tail and whatever
older messages still fit]`. Plain chat sessions gain their first system
message this way; qwen-family models handle a system message at position 0/1
fine.

### `chat_store.py`

- `set_summary(session_id, summary)` and `set_calibration(session_id, cal)` —
  same atomic-write pattern as `set_mode`.

### Web UI (`app.js`, `style.css`)

- New NDJSON event `{"compacting": true}` → transient chip
  "⏳ compactage de l'historique…" in the log.
- `done.context`: meta line appends "· N messages hors contexte" (warn style)
  when `dropped > 0`.
- Session render: a divider "— historique compacté ici —" above the message at
  `summary.covers_until`; tooltip/details shows the stored summary text.

## Error handling

| Failure | Behavior |
|---|---|
| Compaction call raises / times out | stderr log, no summary update, trim covers it, turn proceeds |
| Summary content empty | same as above |
| Estimation badly off (prompt still overflows num_ctx) | existing 12/07 UI warning shows `ctx used/num_ctx` ≥ 100 % — visible, and calibration self-corrects next turn |
| Old sessions without `summary`/`calibration` keys | all code paths default (`summary=None`, ratio 3.0) |

## Testing

- Unit tests on `chat_context` pure functions: eviction keeps counts/stubs the
  right messages, budget math, trim drops oldest-first and never the tail/
  system, calibration clamping, `compaction_span` respects `covers_until`.
- `factory_mcp` tests with fake `ollama_client.chat`/`chat_stream`: compaction
  triggers at threshold, summary persisted and injected on the *next* turn,
  compaction failure falls back to trim without failing the turn, `done`
  carries `context`, calibration saved.
- Web test: `compacting` event serialized in the NDJSON stream.
- Real smoke: long session past 70 % budget → chip appears, summary lands in
  the JSON, next reply's prompt is small again, no Ollama-side truncation.

## Non-goals

- Raising `CHAT_NUM_CTX` (separate step: measure KV-cache RAM and prefill
  latency first — at ~1 GB per 16k tokens observed, 260k is ~16 GB of KV on a
  12 GB GPU / 31 GB RAM box and minutes of prefill; 64k is the realistic
  ceiling to benchmark, per-session override can come with it).
- Compacting delegation jobs (they are single-shot, budgeted by ADR-008).
- Retroactive compaction of existing session files (they pick up the machinery
  on their next message).

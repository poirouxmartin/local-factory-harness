# Session self-analysis — design (2026-07-27)

## Why

`session_audit.analyze` reads the transcript. The transcript holds no duration,
no window figure and no output size, so the audit can count things and nothing
else: `tool_calls`, `wasted_calls`, `tool_errors`, `loops`. Its report on a
session that spent half its wall clock re-reading a 56 KB file reads exactly
like its report on a clean one.

That is why the 2026-07-27 audit (854 replayed calls over 256 sessions, 24 % on
the last 40) had to be done by hand, and why the registry entry
« Lessons sans contenu (auto-amélioration en panne) » stayed open: the only
writer of lessons is `lessons.from_audit`, fed by a report that cannot see
cost. Three hardcoded process-hygiene patterns, no *what*, no *how much*.

`session_log` (shipped today) records exactly what was missing: `ms` and
`out_chars` per call, `prompt_count` / `eval_count` / `ctx_pct` / `ttft_s` per
turn, and a closed anomaly vocabulary. Nothing reads it yet beyond
`summarize()`, which totals but does not judge.

## What this adds

`harness/session_analysis.py` — pure arithmetic over the event list:

1. **Cost.** Where the wall clock went (tools vs generation), how many tokens
   were prefilled and generated, how full the window got.
2. **Waste, priced.** Not "5 wasted calls" but "9 replayed calls = 41 s and
   ~18k tokens of window".
3. **Findings.** A bounded, ordered list from a closed vocabulary, each with a
   cost. Ordering is by cost, so the top line is the thing worth fixing.

### One unit: seconds

Time and window are both scarce, and a finding that costs 0 s but 20k tokens is
not free — those tokens are prefilled again after every compaction. Prefill on
this box measures 600–1000 tok/s (registry, 18/07), so the analysis prices
1000 tokens at 1 second and ranks findings by `weight_s = seconds +
tokens / 800`. Both raw numbers stay in the report; only the ranking is derived.

### The findings vocabulary (closed, with floors)

| code | fires when | priced at |
|---|---|---|
| `replayed_calls` | ≥ 3 byte-identical replays, or ≥ 5 s of them | their `ms` + `out_chars` |
| `hot_target` | one (tool, args) pair ≥ 4× | the repeats, not the first call |
| `oversized_output` | one call returned ≥ 20 000 chars | its tokens |
| `slow_tool` | one tool ≥ 5 s **and** ≥ 40 % of tool time | its `ms` |
| `failing_tool` | same (tool, error kind) ≥ 3× | its `ms` |
| `window_pressure` | peak ≥ 85 % or ≥ 1 compaction | 13 600 tok per compaction (measured, `20260723_compaction_cache.md`) |
| `output_truncated` | ≥ 1 turn stopped on `length` | that turn's `eval_count` — the tool call is dropped (`factory_mcp:1404`), so the generation bought nothing |
| `toolset_over_budget`, `map_degraded` | the trace said so | — |

A closed vocabulary for the same reason the anomaly list is closed: an analysis
that can name anything names nothing, and the lesson patterns downstream must
stay countable across sessions.

### Which findings become lessons

Lessons land in the agent's system prompt (the KV prefix). Only findings about
behaviour **the model controls** may go there:

- `replayed_calls`, `hot_target`, `oversized_output`, `failing_tool`,
  `output_truncated` → `lessons.from_trace`.
- `slow_tool`, `window_pressure`, `map_degraded`, `toolset_over_budget` are
  harness facts. Telling a model its project map degraded teaches it nothing;
  those stay in the report and the studio panel.

`count` on a trace lesson means *sessions in which this fired*, not
occurrences: the analysis is re-run over the whole trace at every turn, so
per-session banking (`traced_codes` on the session) is what keeps `merge` from
adding the same finding ten times. Occurrence counts live in the suggestion
text, where they are read rather than summed.

## Where it runs

- `_learn_from` (end of every user turn) also runs the trace analysis. Cheap:
  the trace is a small append-only file, and the analysis is arithmetic. No
  generation, no GPU.
- `chat_trace` gains `findings` and `cost`, so the studio panel shows what the
  session is wasting **while it runs**, next to the incidents already there.
- `analyze_file` + `to_markdown` give post-mortems the same numbers by hand,
  the way `session_audit.audit_file` does.

## Not in scope

- Changing configuration from the findings (lessons auto-tuning) — deferred by
  operator decision 2026-07-22, and still deferred.
- Rewriting `agent.md` from the findings — that is the third item of this lane
  (snapshot first).
- Retiring `session_audit`: it still owns thinking-loop detection over the
  transcript, which the trace cannot see.

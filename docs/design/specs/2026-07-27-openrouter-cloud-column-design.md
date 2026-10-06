# OpenRouter cloud column for banc A — design (2026-07-27)

## Why

`docs/backlog-optimisations.md:20` records the harness-tax measurement as
MEASURED (25/07) and then immediately qualifies it:

> **Réserve : le proxy Haiku n'est pas une cellule `cloud_client`** (runtime
> d'agent, system prompt CC en plus, aucun cap de sortie) — la vraie colonne
> cloud attend `ANTHROPIC_API_KEY`.

That reservation is not cosmetic. Two other leviers in the same registry —
"donner des yeux au job" and "mémoire intra-barreau" — are both
`EN ATTENTE — arbitré par la taxe de harnais`. They are waiting on a
measurement whose only cloud cell was run through an advantaged proxy.

`experiments/coder_bench/cloud_client.py` is already written for exactly this:
a cloud rung taxed identically to the local ones, wired into `run_bench.py:149-155`
through the `chat_fn` seam. It has never run, because it needs an API key.

An OpenRouter key unblocks it. OpenRouter exposes only the OpenAI-shaped
`/v1/chat/completions` (no Anthropic `/v1/messages`), so `ANTHROPIC_BASE_URL`
cannot redirect the existing SDK path — the client needs a second backend.

`ROADMAP.md:674` ("OpenRouter as the ladder's top rung") describes a different,
production-facing feature and cites `ollama_client.py`, which no longer exists.
It is out of scope here, and should be re-decided on the numbers this
measurement produces.

## What we build

An OpenRouter backend inside `experiments/coder_bench/cloud_client.py`, plus
per-cell cost and provider logging in `run_bench.py`.

This is a measurement instrument. Nothing in the production jobs lane or the
chat lane changes.

### The seam does not move

`run_bench.chat_fn` (`run_bench.py:152-155`) already routes on
`cloud_client.is_cloud(model)`. Only what `is_cloud` accepts, and what `chat`
does behind it, changes.

- `is_cloud(model)` — true for `claude-…` (existing Anthropic SDK path) and for
  any identifier containing `/` (OpenRouter). The provider prefix makes the
  routing legible in `rows.jsonl`; `slug()` (`run_bench.py:72`) already maps
  `/` to `_`.
- `chat()` dispatches on the same distinction and returns the **already
  normalized** dict (`content`, `eval_count`, `tokens_per_s`, `done_reason`, …).
  The return contract is unchanged — that is what lets `loop_job` stay untouched.

### The OpenRouter request

Body as `llama_client` builds it (`llama_client.py:82-91`), plus:

- header `Authorization: Bearer $OPENROUTER_API_KEY`
- `provider: {"order": ["anthropic"], "allow_fallbacks": false}`

The pinning is methodological, not incidental. `cloud_client.py:6-7` justifies
the whole bench with "the horizontal axis only means something if the harness
is the constant". OpenRouter arbitrates between providers for a single model
id; left to default routing we would close one reservation by opening another.

Sampling follows the existing rule (`cloud_client.py:14-17`): temperature is
sent only for models documented to accept it.

### The cost ledger

`loop_job.py:298-303` builds each attempt record from an explicit whitelist, so
any extra field `chat()` returns is dropped. Widening that whitelist would mean
editing the path every production job takes, for a bench-only need — and under
`CLAUDE.md` that mandates a `regress run` before adoption.

Instead: a module-level ledger in `cloud_client` accumulates
`{cost, provider, prompt_tokens, completion_tokens}` per call. `run_bench` reads
the cumulative total before and after each cell and writes the delta onto the
row. `loop_job.py` is not touched.

OpenRouter returns `usage.cost` on every response (no opt-in field required).

### What lands in rows.jsonl

Per cell, in addition to the existing fields: `cost_usd`, `provider`,
`prompt_tokens`, `completion_tokens`. `summarize()` gains `total_cost_usd` per
model. This is the dated evidence the registry asks for.

### Accepted degradation

`ttft_s` and `prefill_tokens_per_s` stay at zero, as the Anthropic path already
does (`cloud_client.py:66-67`): they measure a local GPU and mean nothing across
a network. `tokens_per_s` stays wall-clock — comparable between cloud cells,
never against a local rung. `num_ctx` stays ignored: the context window is the
model's.

### Errors

- Missing key fails at startup, in the shape of `cloud_client.py:32-34` — not at
  cell 7 of 12.
- A cell that dies on the network must not cost the run. `run_bench` already
  appends each row as it lands (`run_bench.py:184`); failures stay scoped to
  their cell.
- No automatic retry. A silent retry would corrupt `attempts`, which is one of
  the measurements.

### Cost guard

The credit ceiling is set on the OpenRouter key, outside the code: a hard
guarantee that a crash cannot bypass. The code only records what was spent.

Estimated 5–10 USD for the 12-case corpus, consistent with the audit's "quelques
dollars d'API". Confirm on the first cell before launching the rest.

## Testing

Transport is testable without network: well-formed request (auth header,
provider pinning, sampling), parsing of an OpenRouter response fixture, `usage`
→ ledger mapping, missing-key behaviour.

The live call is validated on a single cell (`--only <job_id>`) before the full
corpus runs.

Per `CLAUDE.md`, the full run goes detached (`Start-Process` + a log file), never
through a task runner.

## Not in scope

- An OpenRouter rung in the production ladder (`ROADMAP.md:674`) — re-decide on
  the numbers this produces.
- Streaming SSE: the bench displays nothing live.
- An in-code spend counter with abort: the ceiling is on the key.
- Non-Claude models in the cloud column: one new variable at a time.

## Success criteria

An `anthropic/claude-…` column in `report.json`, on the same corpus and the same
bride as the three local rungs, with its cost — and the reservation at
`docs/backlog-optimisations.md:20` rewritten as a verdict.

## Open points, to settle at implementation

Settled on live cells, 2026-07-27 (`experiments/coder_bench/runs/or-smoke`,
`or-smoke2`):

- **How to resolve a model id and its provider slug.** `GET
  /api/v1/models` lists every id; `GET /api/v1/models/<id>/endpoints` returns
  the slug to pin in its `tag` field. Neither call needs authentication. The
  specific model for the measurement column is an open product decision, not a
  technical unknown.
- **The response does expose the serving provider.** We pinned `darkbloom` and
  the row came back `provider: "Darkbloom"` — the display name, not the tag.
  So the pin is not merely *sent*, it is **verifiable per row after the fact**,
  which is what the methodological reservation asked for. `_record` still
  falls back to `"unknown"` when the field is absent.
- **A wrong slug fails loudly.** With `allow_fallbacks: false`, an unavailable
  provider returns HTTP 429/4xx with a readable body rather than silently
  rerouting (`or-smoke`: Gemma refused upstream by Google AI Studio).

Still open, and now the deciding factor:

- **Latency on the free tier makes the full corpus impractical.** One cell took
  1072 s (3 calls, 2 attempts), almost all of it network wait. Twelve cells is
  ~3.5 h for a single model, against a 50-calls-per-day cap for accounts that
  have never purchased credit. Either the corpus is reduced for free-tier runs,
  or the column runs on a paid model. This is a protocol decision, not a code
  defect.

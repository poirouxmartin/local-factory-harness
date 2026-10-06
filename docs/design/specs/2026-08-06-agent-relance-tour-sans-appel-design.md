# Relancer un tour sans appel — design (2026-08-06)

## Why

The agent loop ends a turn the moment the model replies without a tool call
(`factory_mcp.py:2091`): `yield "done"`, hand back, wait for the operator. That
is the right reading of a final answer. It is the wrong reading of what the
ChatGPT lane actually does every four to six calls.

Measured on session `c_14f20c89` (mode `auto`, `@chatgpt/gpt-5.2`, workspace
GPT_code), messages 80–109, **all of them after the tail reminder shipped**
(`6f7e6f0`, 09:35; the messages below run 10:17→10:30):

| # | what happened |
|---|---|
| 81–92 | six clean calls: `read_file` ×3, `step_done` ×2, `search` |
| **93** | no call. Recap, then: *« Je n'ai toutefois pas de canal d'édition/exécution actif **dans ce tour** »* |
| 94 | operator: « tu peux continuer ? » |
| 95–102 | four clean calls, including two `edit_file` |
| **103** | no call: *« Je n'ai pas les fonctions `edit_file`/`run_command` exposées **dans ce tour** »* |
| 104 | operator: « je te laisse reprendre » |
| **105** | no call, immediately again: *« dès que l'outil d'édition workspace est disponible **dans le tour** »* |
| 106 | operator: « parfait, continue » |
| 107–108 | one call: `read_file` |
| **109** | no call: *« Je n'ai pas les appels workspace exécutables **dans ce tour** »* |

Four stalls in thirteen minutes, three of them ending a turn that had to be
restarted by hand. Nothing changed between message 92 and message 93 — no config
edit, no model switch, and `edit_file` was in the offered list the whole time,
proved by messages 95 and 99 calling it minutes later.

Three readings follow from the transcript.

**The belief is specific, and it is not "there are no tools".** All four stalls
name the tools correctly and place them elsewhere in time: *dans ce tour*, *dans
le tour*, *au prochain tour où les appels sont effectivement disponibles*. The
head block and the tail reminder both answer "you have no access to a tool" —
which is the sentence from the July probes, and no longer the one being written.
Nothing in the prompt answers "not in this turn".

**A generic nudge is not reliably enough.** Message 104 ("je te laisse
reprendre") produced message 105, another stall, in one turn. Two of the three
manual restarts worked; one did not.

**The stall costs more than the turn it wastes.** The event log records
`replayed_call read_file src/app/Application.cpp` three times (10:20, 10:25,
10:29): after each stall the model re-reads the file it had already read to
rebuild "l'état réel". On a subscription metered per message, one stall bills a
wasted turn, a nudge, and a replayed read.

The context is healthy throughout: `num_ctx` 128000, `dropped 0`, never
compacted, `evicted` rising 45→55 as the oldest messages fall off the budget.
The protocol and the reminder are both system-side and survive eviction. This is
not a context wall.

The mechanism is the one `6f7e6f0` already named and only half fixed: the
transcript is 100 % of this lane's memory and is re-serialised whole each turn,
so a refusal written once returns as `[assistant]` precedent for the rest of the
session. Four of them now sit in the tail of this session, and they are the most
recent thing the model reads about its own tools.

## What changes

Two pieces, both in `_agent_turn`.

### 1. The relance

The loop already knows one way to refuse to end a turn: `MALFORMED_CALL_RETRY`
(`factory_mcp.py:2096`), for a reply whose text carries broken call scaffolding.
The stall carries none — it is prose. This is its sibling branch.

**Condition.** No parsed call, and:

- `llama_client.has_tool_call_markers(...)` is false (with markers, the existing
  malformed path owns the turn);
- `session["mode"] == "auto"` — the mode that means "work by yourself". In
  `approve` the operator is already in the loop at every call;
- no `metrics["stopped"]` (length and degenerate stops return before this point);
- the plan exists and has at least one unfinished step — read from
  `carnet.project(session["messages"])["plan"]`, a step being finished when
  `step["done"]` is set (by `step_done`, or by the harness ticking a `file:`
  condition it can verify).

**No plan, no relance.** An earlier draft of this design relanced a planless
session too, on the grounds that the agent owes a plan. That is wrong twice
over. It has no exit: an ordinary final answer in `auto` mode — the model
answering the operator's question and stopping, correctly — would be relanced
forever, and three tests in the current suite are exactly that shape
(`test_agent_reply_executes_tools_and_reprompts`,
`test_agent_reprompts_a_malformed_tool_call_left_in_thinking`, and the tool-error
tests all end on a prose turn with no plan). And it contradicts the rule the
carnet already states for the same question: `carnet.stagnation` returns 0 when
there is no plan, because "progress is defined against the plan (spec C.2)". So
is continuation. A planless session keeps today's behaviour: the turn ends, and
the carnet's `PAS DE PLAN` line asks for a plan on the next message.

**Action.** Append `{"role": "tool", "tool_name": "system", "content":
STALL_RETRY}`, `yield "notice"` with the running relance count so the studio
shows it happening, and loop.

**No cap**, consistent with `agent_max_iterations = 0`: the exits are a complete
plan, `ask_operator` / `request_resource` (the existing `handback` path), a
`mode` that is not `auto`, and the Stop button. This is a deliberate trade: an
unbounded relance on a wedged model spends the account's allowance at the
account's pace, and the visible per-relance notice is what lets the operator see
it and stop it. A relance that repeats verbatim also means piece 2 failed, which
is the reading that notice buys.

**The wording**, under the rules `chatgpt_web.py` proved live and states in its
own comments — name the tools, say the block is a request, claim nothing about
what the model may not do, never tell it to suppress its own words:

> your last reply announced the next action but did not request it. The turn you
> were waiting for is this one: the program is executing tools right now, and the
> call block is how you ask for one. Emit the call you just described, as the
> last thing in your reply.

The second sentence is the whole point of the new constant: it answers *dans ce
tour*, which is the sentence actually on the page, and which neither existing
block addresses.

### 2. The precedent

When the loop relances a turn, it marks the assistant message it is relancing:
`msg["stalled"] = True`.

The `wire` list built at `factory_mcp.py:1975` is where the transcript becomes a
prompt. A marked message goes onto the wire with a neutral placeholder instead of
its own content. The message on disk is untouched — the audits, the replays and
`session_analysis` read the real text; only what is sent changes.

This drops the recap the stall also contains ("il reste : includes, build,
test"). Deliberate: the carnet and the tool results are the session's ledger, and
that recap is precisely what the model writes *instead of* acting. Keeping the
useful half would mean detecting the refusal sentence by regex, in whatever
language the model answered — a fragile surface added to reduce a loss the carnet
already covers.

This is lane-agnostic and lands for all three, but it is the lane with no memory
of its own that needed it: `render_prompt` re-serialises the whole transcript
every turn, so for ChatGPT the wire *is* the model's past.

## Proof

Unit tests on the fake transport, mirroring
`test_agent_reprompts_a_malformed_tool_call_left_in_thinking`:

- a prose turn with no markers, in `auto`, with an unfinished plan → a relance is
  appended and the loop continues;
- the same turn with a complete plan → `done`, as today;
- the same turn with no plan at all → `done`, as today;
- the same turn in `approve` mode → `done`, as today;
- a marked message leaves `wire` neutralised and leaves the session file intact;
- the existing malformed and length paths keep their current behaviour.

What the tests cannot settle is whether the relance actually flips the model. The
falsifier is a live session on the account, long enough to reach the fourth or
fifth burst: the pass condition is a `notice` followed by a real call, and the
failure condition is the same stall re-emitted after a relance. Proposed, not
confirmed — like the tail reminder it extends, which this session has now shown
to be a partial fix.

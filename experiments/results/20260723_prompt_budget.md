# What a chat/agent turn's prompt costs — 2026-07-23 — MEASURED

Block B, line items 1, 2 and 6 of
`docs/design/specs/2026-07-23-chat-agent-token-budget-design.md`.

Rig: `experiments/agent_bench/`, qwen3-coder:30b on llama-server (chat lane,
64k ctx, KV q4), real `Factory.agent_reply`, real tools, auto mode, a fresh git
workspace per run. 7 tasks × 5 runs × 2 toolsets = **70 sessions, 70/70
completed**. Raw: `results_base.jsonl`, `results_web.jsonl`.

Every number below comes from the **server's own tokenizer** (`/tokenize`) and
from its counters (`prompt_n`, `cache_n`). Nothing here is `chars / 3`.

## The budget of one turn

Mean tokens per turn, on the prompt of the turn's **last** model call.

| block | base (6 tools) | share | web (15 tools) | share |
|---|---|---|---|---|
| `system` (AGENT_SYSTEM + shell note) | 309 | 13.4 % | 309 | 6.6 % |
| `agent_md` (global `agent.md`) | **705** | **30.6 %** | 705 | 15.1 % |
| `tool_names` | 21 | 0.9 % | 100 | 2.1 % |
| `tools` (the JSON schemas) | 513 | 22.2 % | **2692** | **57.7 %** |
| `messages` (history + call arguments) | 400 | 17.4 % | 402 | 8.6 % |
| template overhead (`reconcile`) | 357 | 15.5 % | 460 | 9.8 % |
| **wire (`prompt_count`)** | **2306** | | **4668** | |

`workspace_instructions` is absent: the seeded workspaces carry no
`AGENTS.md`/`CLAUDE.md`. A session in a real repo adds that block on top —
local-factory's own is ~1.5 k chars.

**Template overhead is 357-460 tokens and it is real**: role wrappers, the
`<tools>` serialisation, the per-message scaffolding. It is reported, never
absorbed into the blocks. It sits at 15.5 % of a base turn and 9.8 % of a web
turn — the absolute number barely moves, so it is a per-message cost, not a
per-token one.

## Line item 2: what the tool schemas cost

Priced one schema at a time, same tokenizer:

| tool | tokens | | tool | tokens |
|---|---|---|---|---|
| `mcp__search__fetch_content` | 407 | | `run_command` | 99 |
| `mcp__search__search` | 348 | | `mcp__playwright__browser_navigate` | 98 |
| `mcp__fetch__fetch` | 308 | | `read_file` | 91 |
| `mcp__playwright__browser_click` | 243 | | `edit_file` | 89 |
| `mcp__playwright__browser_type` | 222 | | `search` | 87 |
| `mcp__playwright__browser_console_messages` | 204 | | `list_dir` | 72 |
| `mcp__playwright__browser_snapshot` | 199 | | `write_file` | 68 |
| `mcp__playwright__browser_wait_for` | 144 | | | |

- 6 native tools = **506 tokens** (84 each).
- 9 MCP tools = **2179 tokens** (242 each) — **2.9× the price of a native
  schema**, because MCP schemas ship the server author's prose.
- The `web` toolset **doubles the prompt of every turn**: 2306 → 4668.
- The three fetch/search schemas alone (1063) cost more than the entire native
  toolset.

What that bought, on this corpus: **nothing**. 35/35 both arms, same 4 tool
calls per run, and the web arm spent **11 `search` calls across 35 sessions**
reaching for the web on tasks that are pure local code. It also cost **+40 %
wall clock** (7.2 s → 10.1 s per turn), which at 98 % cache reuse is not
prefill — it is 2.4 k more tokens of attention on every generated token.

## Line item 1: the KV prefix is doing its job

| | wire | repaid (`prompt_n`) | reused (`cache_n`) |
|---|---|---|---|
| base | 2306 | **124** | 95 % |
| web | 4668 | **92** | 98 % |

A turn's last model call repays ~100 tokens out of 2.3-4.7 k. The static head
of the prompt — `system`, `agent_md`, `tool_names`, `tools`, 1548 tokens on
base and 3806 on web — is **prefilled once per server lifetime**, not once per
turn, and the byte-stability guard
(`test_the_session_prompt_prefix_is_byte_stable_across_turns`)
is what keeps it that way.

Caveat, and it is the honest limit of this measurement: the agent loop yields
`done` **once per turn**, not once per model call (`factory_mcp.py:1225`), so
what is measured is the last call of each session. The first call of a session
repays only what the previous session did not already leave in the cache —
which, since the prefix is byte-identical across sessions, is close to
nothing too.

## Line item 6: generation the user never sees

| | think | written answer | tool-call arguments | total |
|---|---|---|---|---|
| base | **0** | 181 | 135 | 316 |
| web | **0** | 205 | 132 | 337 |

`qwen3-coder:30b` emits no thinking channel at all: **0 tokens, 0 %**, on 70
sessions. The line item as written ("thinking tokens the user never sees") is
**empty for the model the factory actually runs on**. Re-open it if the chat
lane moves to the 35b, which does think.

What *is* invisible is different: **43 % of everything generated is tool-call
arguments** (135 of 316). That is the block a `write_file` fills with a whole
file, and it lands back in `messages` on the next turn.

## What the numbers say to do

1. **Cut the `web` toolset down, or stop shipping it by default.** 2179 tokens
   for 9 schemas that changed no outcome and were called 11 times in 35
   sessions, all off-task. Ranked by price: `fetch_content` (407),
   `search` (348), `fetch` (308) — three schemas, 1063 tokens, and `fetch` +
   `fetch_content` overlap. A curated toolset per session already exists; this
   says use it.
2. **Trim MCP schema descriptions, not our own.** Our native schemas are 84
   tokens each and there is nothing left to win there.

## What the numbers say NOT to do

1. **Do not trim `agent.md` for speed.** It is the fattest single block (705
   tokens, 30.6 % of a base turn) and the reflex is to cut it — but it is at
   the *head* of the prompt, so it is prefilled once and reused at 95-98 %.
   Its per-turn cost in time is ~zero. Its real cost is context window and the
   fact that the 07-23 A/B found **no effect on outcomes**
   (`20260723_agent_md_ab.md`). Trim it on the quality argument if at all,
   never on the token count — and never in a way that moves a byte mid-session
   (that would repay the whole prefix, ~1 s per turn, measured 07-22).
2. **Do not chase the template overhead.** 357-460 tokens is the chat
   template's own scaffolding; the only lever on it is sending fewer messages,
   which is block C's problem, not a token-budget one.
3. **Do not trust `eval_count` as a turn's generation.** See below.

## Three defects found on the way

**1. `eval_count` counts one model call, not the turn.** The agent loop yields
`done` once per turn, so the bench's `eval_tokens` read **76** where the turn
had actually generated **316**. A 4× undercount on the cost side of every
future comparison. Fixed: `gen_tokens = think + write + call arguments`, all
three from the tokenizer (`bench.py:score_events`).

**2. An MCP server holds the workspace hostage.** `McpServer` is spawned with
`cwd=workspace` (`mcp_client.py:64`) and the registry keeps it alive per
(server, workspace) with nothing closing it at session end. On Windows a CWD is
a locked handle: **28 of the first 35 web runs died on `PermissionError`** when
the next run tried to reseed the directory. The bench now closes the registry
after each run; the product bug — a workspace that cannot be deleted, renamed
or moved after an agent session touched it — is filed in the registry.

**3. A dead MCP server silently shortens the KV prefix.** `tool_specs` skips a
server that will not start with a stderr line, by design ("never the session")
— but the tool list *is* in the prefix. A server that dies between two turns
shrinks the head of the prompt: the whole prefix is repaid, and the model
loses tools without being told. The byte-stability guard does not catch it (it
compares two consecutive calls, not a server death). Every run now records
`n_tools`, so a `web` session measured at 9 tools instead of 15 reads as a dead
server rather than a cheap toolset. Filed in the registry, undecided between
failing loudly and keeping a placeholder schema.

# Agent mode in the chat tab (v1) — design

Date: 2026-07-11
Status: approved

## Goal

A claude-code-like agent inside the Studio chat tab: the model can read files,
search, write/edit files and run shell commands in a workspace, in a streamed
multi-turn conversation. Per-session mode dropdown: **approve** (writes and
commands wait for operator approval) or **auto** (everything executes).

## Decisions (validated with the operator)

- **Tool calling**: native Ollama `tools` parameter on `/api/chat` (approach A)
  — structured `tool_calls`, no text-protocol parsing, streams like today.
- **Workspace v1**: the server's cwd (launch directory), maximum freedom;
  assigning a registered project comes later.
- **Instructions**: at session start the system prompt embeds the first of
  `AGENT.md` / `agent.md` / `CLAUDE.md` found at the workspace root.
- **Tools v1**: read + write + shell from day one.
- **Approval flow**: read tools always auto-execute; write/shell tools in
  approve mode end the stream with an `approval_needed` event; the decision
  comes back on a separate POST that resumes the loop with a fresh stream
  (an HTTP response cannot pause mid-stream waiting for a click).
- **Risk accepted**: qwen3.6 tool-calling quality is unknown; the first real
  smoke test measures it (that is the factory's job anyway).

## Session (chat_store.py)

Existing sessions are untouched (plain chat keeps working). Agent sessions add
optional fields:

```json
{
  "kind": "agent",
  "mode": "approve",
  "workspace": "C:\\Users\\me\\local-factory",
  "pending_call": {"name": "write_file", "arguments": {"path": "...", "content": "..."}}
}
```

- `mode` is editable per session (dropdown, like the model selector).
- `pending_call` holds the tool call awaiting approval, null otherwise.
- Messages gain two roles beyond user/assistant: `{"role": "tool", ...}` for
  tool results, and assistant messages may carry `"tool_calls": [...]` — both
  are replayed verbatim in the history sent to the model.

## Tools (agent_tools.py, new module)

| Tool | Kind | Effect |
|---|---|---|
| `list_dir(path)` | read | entries + sizes, workspace-relative |
| `read_file(path, offset?, limit?)` | read | text content, line-numbered |
| `search(pattern, glob?)` | read | regex search across files (paths + matching lines) |
| `write_file(path, content)` | write | create/overwrite |
| `edit_file(path, old, new)` | write | exact-match single replacement, error if 0 or >1 matches |
| `run_command(command, timeout?)` | shell | via shell, cwd = workspace, default 60 s timeout |

Rules for every tool:
- Paths resolve under the workspace root; anything escaping it
  (absolute outside, `..`) is refused with a tool error the model sees.
- Outputs truncated (~8 KB) with an explicit `[truncated]` marker: num_ctx is
  16k, a fat `git log` must not evict the conversation.
- Tool errors (file not found, non-zero exit, timeout) are returned to the
  model as the tool result, never raised: the loop continues.
- One JSON schema list `TOOLS` (Ollama format) + `run(name, arguments, workspace)`
  dispatcher + `needs_approval(name)` (write/shell true, read false).

## Agent loop (factory_mcp.agent_reply)

Same generator + GpuLock pattern as `chat_reply`. Events:
`("start", None)`, `("thinking", text)`, `("chunk", text)`,
`("tool_call", {name, arguments})`, `("tool_result", {name, output})`,
`("approval_needed", {name, arguments})`, terminal `("done", metrics)` /
`("error", message)`.

Loop per user turn (max 20 tool iterations):

1. Build history (system prompt with AGENT.md content + messages, thinking
   stripped) and call `ollama_client.chat_stream(..., tools=TOOLS)`.
2. Stream thinking/content deltas as today. Ollama streams `tool_calls`
   in the message; collect them.
3. No tool calls → save assistant message, `done`, release lock.
4. Tool calls → save the assistant message with its `tool_calls`, then for
   each call: read tool or auto mode → execute, save
   `{"role": "tool", "content": result}`, emit `tool_call` + `tool_result`,
   loop back to 1. Approve mode + write/shell → save it in `pending_call`,
   emit `approval_needed`, end the stream (lock released — the operator may
   take minutes; a job grabbing the GPU meanwhile just means the resume
   returns 409 and the UI says retry).
5. Iteration 20 → save a tool message "iteration limit reached", `done`.

Resume endpoint: `POST /api/chats/(id)/approve {"approved": true|false}` —
approved: execute `pending_call`, clear it, save the tool message, re-enter
the loop (new NDJSON stream, same event vocabulary). Refused: save a tool
message "refused by operator", clear `pending_call`, re-enter the loop so the
model can react.

## Routes (factory_web.py)

| Route | Verb | Effect |
|---|---|---|
| `/api/chats` | POST `{model, kind?, mode?}` | create (agent when `kind: "agent"`) |
| `/api/chats/(id)/mode` | POST `{mode}` | switch approve/auto |
| `/api/chats/(id)/approve` | POST `{approved}` | resolve pending call, resume loop (streams) |

`/messages` on an agent session streams the new event kinds as NDJSON lines:
`{"tool_call": ...}`, `{"tool_result": ...}`, `{"approval_needed": ...}`.
A `/messages` POST while `pending_call` is set → 409 (resolve it first).

## UI (harness/web)

- New-session form gains a kind toggle (chat / agent) and, for agent, the
  mode dropdown; agent sessions show a badge and the mode dropdown next to
  the model selector.
- Event rendering in the log: collapsed tool chips
  (`▸ read_file harness/app.js`) expandable to show the output; write_file /
  edit_file render old→new as a small diff block; run_command shows the
  command + output.
- `approval_needed` renders an inline panel (diff or command) with
  Approuver / Refuser buttons wired to `/approve`, which streams like send.
- Session reload replays messages including tool messages (chips) and a
  still-pending `pending_call` re-renders its approval panel.

## Tests (harness/tests)

- `agent_tools`: each tool happy path; path traversal refused; truncation;
  edit_file 0/2-match errors; command timeout and non-zero exit as results.
- `factory_mcp.agent_reply` (mocked `chat_stream`): tool loop executes and
  re-prompts; approve mode parks the call + `approval_needed`; approve/refuse
  resume paths; iteration cap; GPU 409 on resume; plain chat sessions
  unaffected.
- Web: create agent session; NDJSON contains tool events; `/approve` streams;
  `/messages` 409 when a call is pending.
- `ollama_client.chat_stream`: `tools` forwarded; streamed `tool_calls`
  collected and yielded.

## Out of scope (v1)

Project assignment (workspace picker), git worktree isolation, multi-file
diffs/patch format, allow-lists for commands, sub-agents, markdown rendering,
context compaction when the conversation outgrows num_ctx.

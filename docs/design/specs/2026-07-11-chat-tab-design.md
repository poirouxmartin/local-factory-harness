# Chat tab (v1) — design

Date: 2026-07-11
Status: approved

## Goal

A "Chat" tab in the Studio: a Claude Code desktop-like discussion space. Pick a
local Ollama model, hold a multi-turn conversation inside a session, streamed
responses. Pure chat — no tools, no factory actions from the model. Existing
Studio views are untouched.

## Decisions (validated with the operator)

- **Scope**: pure chat, no agent tools, no project-file context (later).
- **Streaming**: yes — tokens render as they arrive (~30 tok/s locally makes
  block responses unusable).
- **Persistence**: sessions on disk, survive server restarts.
- **Placement**: a tab in the existing Studio (same server, same port).
- **Architecture**: dedicated `chat_store.py` + direct streaming from the web
  handler (NOT jobs with `kind="chat"` — polling would kill streaming and the
  job data model does not fit a conversation).

## Server

### chat_store.py (new module, mirrors job_store.py spirit)

One session = one JSON file `jobs/chats/c_<hex>.json`:

```json
{
  "session_id": "c_ab12...",
  "title": "first user message truncated",
  "model": "qwen3-coder:30b",
  "created_at": 1234.0,
  "updated_at": 1234.0,
  "messages": [
    {"role": "user", "content": "...", "ts": 1234.0},
    {"role": "assistant", "content": "...", "ts": 1234.0,
     "metrics": {"tokens_per_s": 30.1, "ttft_s": 1.2}}
  ]
}
```

API: `create(model)`, `list()` (id, title, model, updated_at, message count),
`get(id)`, `append(id, message)`, `delete(id)`, `set_model(id, model)`.
Title = first user message truncated to ~60 chars, set on first append.

### ollama_client.chat_stream (new function)

`chat_stream(model, messages, on_chunk, temperature=0.7, num_ctx=16384, timeout=900)`
— POST `/api/chat` with `stream: True` and the full message history; each
parsed line's content delta goes to `on_chunk(text)`; returns final metrics
(same shape as `chat()`: tokens_per_s, prefill, ttft_s, content).

### Routes (factory_web.py)

| Route | Verb | Effect |
|---|---|---|
| `/api/chats` | GET | list sessions |
| `/api/chats` | POST `{model}` | create session |
| `/api/chats/(c_\w+)` | GET | full session |
| `/api/chats/(c_\w+)/messages` | POST `{content}` | send + stream reply |
| `/api/chats/(c_\w+)/delete` | POST | delete session |
| `/api/chats/(c_\w+)/model` | POST `{model}` | change session model |

`POST .../messages` flow: append user message → acquire `GpuLock(timeout=0)`
(409 "GPU is busy" if a job holds it) → stream the Ollama reply to the browser
as chunked NDJSON (`{"chunk": "..."}` lines, then `{"done": true, ...metrics}`)
→ append assistant message → release lock. The lock is held for the whole
generation: a job cannot swap VRAM under an in-flight chat reply, and vice
versa (the existing invariant).

Mid-stream failure: keep the partial content, save the assistant message with
an `"error"` field, emit `{"error": "..."}` as the last NDJSON line.

## UI (harness/web)

- Nav link `Chat` (`#chat`, `#chat/<session_id>`).
- Two-column layout: session list left (new-session button, delete per row),
  conversation right — model selector on top (installed models from
  `/api/ollama`), user/assistant bubbles, input box at the bottom
  (Enter = send, Shift+Enter = newline).
- Client streaming: `fetch` + `ReadableStream` reader over NDJSON; the
  assistant bubble fills as chunks arrive; small metrics line (tok/s) under
  each reply.
- The router's 10 s polling must NOT re-render the chat view (same guard idea
  as the Déléguer view) — a re-render would wipe typed input or an in-flight
  generation.
- Errors: GPU busy → inline message; Ollama down → banner with the existing
  start button behavior; stream error → partial kept, marked as error.

## Tests (harness/tests)

- `chat_store`: CRUD, title derivation, updated_at ordering.
- `ollama_client.chat_stream`: NDJSON parsing + metrics from a mocked stream.
- Web routes: create/list/get/delete session; send message with mocked Ollama
  streaming; 409 when the gpu lock is held.

## Out of scope (v1)

Markdown rendering, message editing/regeneration, project-file context,
tool calling, session search, renaming sessions.

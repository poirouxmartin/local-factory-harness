# Sessions survive the UI — design (2026-07-22)

## Why

Measured on the real studio (port 8791, coupures de socket = ce que fait un F5)
before writing a line of code, per the staleness rule of the chat lane.

A chat/agent turn is the body of the `POST /api/chats/<id>/messages` response.
`factory_web.py:_stream_events` writes each event to `self.wfile`; when the
browser goes away the write raises `BrokenPipeError` and the handler calls
`gen.close()`. `GeneratorExit` lands in the `finally` of `chat_reply`
(`factory_mcp.py:961`) and `_agent_turn` (`:1161`): the partial is persisted
with `error: "interrupted"` and the GPU idle timer is armed.

So the turn's lifetime **is** the HTTP connection's lifetime. Three measured
consequences:

| Mesure | Résultat |
|---|---|
| Chat, modèle froid, coupure à 6 s | 3 caractères persistés, `error: interrupted` |
| Chat, modèle chaud, coupure à 20 s | 4050 car. streamés → 4050 car. persistés, suite perdue |
| Agent auto (30b), coupure juste après le 1er `tool_result` | l'outil a tourné et son résultat est écrit, la boucle meurt : `rapport.md` jamais créé, session figée sur `error: interrupted` |

What is already healthy, and must stay healthy: nothing leaks. No runaway
generation, no orphaned lock, the partial is kept, tool effects are kept. The
failure mode is "the turn dies", not "the turn escapes".

The agent case is the damaging one: side effects have landed, the model never
sees the last tool result, and there is no resume — neither server-side nor in
the UI. A refresh, a navigation or a closed tab mid-run silently abandons a
half-done task.

Second-order finding: today's **Stop** button works *because* of this coupling.
`app.js:918` only calls `STREAM_ABORT.abort()`; the kill is a side effect of
dropping the connection. Any fix that decouples the turn from the connection
must give Stop a real server-side signal, or the button silently stops
stopping.

## What we build

The turn runs in a server-side thread and publishes its events into an
in-memory buffer. HTTP connections are *viewers* over that buffer: they can
come and go, replay from a cursor, and multiply, without ever touching the
turn.

### 1. `harness/turn_runner.py` — the runner

- `Turn`: one running generator per session. Holds the event list
  (`[(kind, payload), ...]`, append-only), a `threading.Condition`, a
  `done` flag, and a `stop_requested` flag.
- `TurnRunner.start(session_id, gen)`: refuses if a turn is already running for
  that session (`ToolError`, surfaced as 409), then spawns a daemon thread that
  drains `gen` and appends every event. The generator is **unchanged**:
  `chat_reply` and `agent_reply` keep their contract (refusals raise before the
  first yield, so `start()` consumes the initial `("start", None)` on the
  caller's thread and a refusal is still an HTTP status code, not a stream).
- `Turn.read(cursor)`: generator yielding buffered events from `cursor`, then
  blocking on the condition until new ones arrive, ending when `done` is set.
  Any number of concurrent readers, each with its own cursor.
- `TurnRunner.stop(session_id)`: sets `stop_requested`. The runner loop checks
  it between two events and calls `gen.close()` **from the runner thread**,
  while the generator is suspended at a `yield` — the existing `GeneratorExit`
  path, partial saved. Calling `close()` from the HTTP thread would race
  ("generator already executing"); it is not done.
  Accepted limit, identical to today: a Stop during a long tool call only takes
  effect when the tool returns.
- Retention: the finished turn's buffer stays until the next turn starts on that
  session, so a viewer attaching late still replays the tail and the terminal
  `done`.

### 2. HTTP surface

- `POST /api/chats/<id>/messages` — same contract as today (NDJSON stream, same
  event kinds), but the bytes are read from the buffer through `Turn.read(0)`.
  A disconnect closes the reader; the turn continues.
- `GET /api/chats/<id>/stream?cursor=N` — the re-attach. Same NDJSON, same
  renderer. Default `cursor=0`: a turn is bounded by the context window, so
  replaying it whole is cheap and reconstructs the bubble exactly.
- `POST /api/chats/<id>/stop` — the real Stop.
- `GET /api/chats/<id>` gains `running: bool`; `GET /api/chats` carries it per
  session so the list can badge a live session.

### 3. Client

- `streamChat` splits into `renderStream(resp, log)` (the existing feed loop —
  rAF autoscroll, magnet stickiness, tool chips, approval panel — untouched)
  and two thin callers: the POST send, and `attachStream(sessionId)`.
- Opening a session whose `running` is true attaches instead of showing a dead
  transcript.
- Stop posts `/stop` and lets the stream end on its own, so the operator sees
  the same partial the server persisted instead of a client-side guess.

## Decisions taken by default

- **Second message while a turn runs → 409.** The GPU lock already serialised
  this in practice; an explicit refusal beats a silent queue.
- **Buffer in RAM, results on disk.** `ChatStore` stays the durable record; the
  event buffer is per-turn and volatile. Zero I/O per token.
- **A studio restart still kills the turn.** It would kill the llama-server
  request anyway; persisting the event stream would buy nothing real.
- **Multi-tab is free** and therefore supported: fan-out is just several
  cursors.

## Testing

- `turn_runner`: buffering and cursor replay, two concurrent readers, a reader
  that leaves mid-turn (the turn still completes), stop between events, an
  exception inside the generator surfacing as a terminal `error` event,
  retention of the last finished turn, refusal of a concurrent start.
- Web level: POST still streams the same NDJSON; a disconnect mid-POST leaves a
  *complete* assistant message in the store (this is the regression test for the
  measured bug); `GET /stream` replays it; second POST answers 409; `/stop`
  ends the turn with a saved partial.
- Frontend: source-level guard in `test_factory_web.py`, per the Node-free rule.

## Risks

- **Threads and the GPU lock.** `_acquire_runtime` is called inside the
  generator, so the lock is now taken and released on the runner thread. It must
  not be thread-affine (it is not — it is a lock plus an idle timer), and the
  idle timer arming in the `finally` keeps working unchanged.
- **A turn with no viewer at all.** It runs to completion and persists; that is
  the point. The runaway brakes stay the LoopGuard, the RepetitionGuard and the
  iteration cap — a decoupled agent turn makes the Stop button matter more, which
  is why it becomes a real endpoint here.

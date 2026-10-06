# Changelog

Newest first. Versions are lightweight tags on meaningful states.

## Unreleased
- **Les deux bugs MCP que le banc avait trouvés en passant (2026-08-06)** —
  ouverts depuis le 23/07, corrigés sans attendre l'audit E2E qui les portait.
  *Le verrou* : un serveur MCP tourne avec `cwd=workspace`, et sous Windows un
  CWD est un handle verrouillé — le dossier ne peut plus être supprimé, renommé
  ni déplacé tant que le process vit. `McpRegistry.close()` existait **sans
  aucun appelant dans `harness/`**, et les enfants survivent à leur parent : le
  verrou survivait au studio qui l'avait posé. Mesuré à l'époque : 28 des 35
  runs `web` du banc morts en `PermissionError` au reseed, le banc fermant le
  registre lui-même pour finir. `Factory.shutdown()` rend maintenant les deux
  choses que le process tient — le runtime GPU et les serveurs MCP —, appelé
  par le `finally` du studio et par `serve()` à la fermeture de stdin. Le cwd
  neutre est écarté : `git` et `playwright` agissent sur le workspace, et c'est
  le CWD qui le leur dit. *Le préfixe* : `tool_specs` sautait un serveur
  impossible à redémarrer pour une ligne stderr, « never the session ». Vrai au
  spawn, faux ensuite — la liste d'outils est la tête du préfixe KV, donc un
  serveur qui meurt entre deux tours raccourcissait le prompt par le haut : tout
  le préfixe à repayer, et le modèle qui perd des outils sans être prévenu. Le
  garde-fou octet-stable ne le voyait pas (il compare deux appels consécutifs,
  pas une mort). Trouvé le 23/07 en instrumentant le banc, qui affichait un
  toolset `web` à 9 outils au lieu de 15 — lu à l'époque comme un toolset léger,
  pas comme un cadavre. Le registre retient désormais les derniers schémas
  servis et les ressert quand le redémarrage échoue : le préfixe garde ses
  octets, l'outil reste offert, et `run()` respawnant à la demande, une mort
  transitoire est réparée au prochain appel. Un serveur qui n'a jamais démarré
  ne coûte toujours rien : pas de forme à tenir, le préfixe ne l'a jamais
  portée.
- **Le tour où il dit que les outils sont dans un autre tour (2026-08-06)** —
  session `c_14f20c89`, messages 80–109, tous postérieurs au rappel d'outils de
  `6f7e6f0` : six appels propres, puis un tour de prose qui annonce le patch et
  place les outils ailleurs dans le temps (« je n'ai pas les fonctions
  `edit_file`/`run_command` exposées dans ce tour »). Relance manuelle, quatre
  appels propres, même stall, relance, stall au tour suivant, relance, un appel,
  stall. Quatre en treize minutes, trois qui ont fini un tour à la main — et
  `edit_file` était offert du début à la fin, prouvé par les messages 95 et 99
  qui l'appellent. Le prompt répondait à la phrase de juillet (« je n'ai pas
  accès à cet outil ») et pas à celle-ci. Deux pièces : une branche de relance à
  côté de celle des appels malformés, dont la deuxième phrase dit que le tour
  attendu est celui-ci ; et le tour stallé qui part sur le fil sous une ligne
  neutre, parce que le transcrit est 100 % de la mémoire de la lane ChatGPT et
  que ses quatre refus étaient devenus ce qu'il lisait de plus récent sur ses
  propres outils (disque intact : les audits lisent le vrai texte). La relance
  ne part qu'en mode `auto` et tant que le plan doit une étape — le progrès se
  définit contre le plan (spec C.2), la continuation aussi. Un plafond de 3
  relances a été ajouté (2026-08-15) pour éviter les boucles infinies observées
  avec ChatGPT quand il annonce des actions sans émettre d'appels d'outils : après
  trois tentatives, la session rend la main à l'opérateur avec un message explicite.
  Le compteur de relances passe dans la notice pour que l'opérateur voie le
  problème.
- **Stop reaches a turn asleep in its lane (2026-08-05)** — since turns moved
  server-side (07-22) Stop has been a flag read between two events, and a turn
  spends its longest waits producing none: llama-server prefilling a large
  prompt, OpenRouter serving out a `Retry-After`, ChatGPT web before its first
  delta. Clicked there, the button did nothing until the server spoke —
  potentially the whole 1800 s timeout away. `turn_runner.Cancellation` carries
  the same stop down to those waits: the lane polls it where it loops, and
  registers what to close where it is already blocked, because no flag
  interrupts a socket inside `recv` (the socket is shut down, then closed —
  private attribute on purpose, there is no public way to abort a read someone
  else is inside of). It comes back up as `TurnCancelled` through the facade,
  so the partial is persisted and the idle timer armed by the paths that
  already did both for a closed generator, and the runner ends the turn on it
  *without* an `("error", ...)` event — the operator's own click is not a
  failure to paint red. Two blocking points rather than three lanes: the cloud
  lane reads its SSE through `llama_client.consume_sse`, so llama-server and
  OpenRouter were fixed in one place; the browser lane needed its own, and the
  token is also read inside the worker's decode loop, which is what actually
  gives the browser back instead of leaving the next turn queued behind an
  answer nobody will read. Guardrails are real waits, not mocks: an HTTP server
  that sends its headers and then says nothing (the thread must be dead 5 s
  after the click, against a 900 s timeout), a silent queue, and a 60 s
  `Retry-After` the turn must not sit through. Still NOT interruptible, and
  unchanged: a stop during a tool call waits for the tool, and `_compact`'s
  summary call is a blocking `chat()`.
- **Carnet: the agent sees what it has already tried (2026-07-24)** — phase 1
  of the reliability loop (`harness/carnet.py`, plan
  `docs/design/plans/2026-07-23-agent-reliability-loop-phase1.md`). A pure
  projection of the transcript, recomputed each turn and injected at the wire
  tail (never the system head: that is the KV prefix): every tool call is
  collapsed onto a normalized *target*, with its attempt count and its last
  failure. Three tools go with it — `remember`, `recall`, `log_wall` — and
  their walls and notes persist to a `factory:carnet` block in MEMORY.md, so a
  blocker hit today warns tomorrow. Any failing result now carries a
  reflection nudge as a separate system message (never glued into the output,
  which `detail_of` reads back). The hard `_LoopGuard` stop is retired: it
  killed the turn at 5 identical results, legitimate work included. Repetition
  is shown, not punished — `RepetitionGuard`, `agent_max_iterations` and Stop
  remain the only brakes. Offline replay on `c_d2a43fd5` is the guardrail, and
  it caught two defects the unit tests could not: `cd <ws> && <cmd>` (cmd.exe
  has no persistent cwd) collapsed 12 unrelated commands onto one target, and
  the slice was cut mid-line at 1200 chars, spending its whole budget on what
  happened first. The slice now leads with the real loop of that session:
  `generate.py`, 5 failures, ComfyUI never started.
- **Workspace memory counts failures across turns (2026-07-23)** — the
  learning watermark (07-22) stopped counts from inflating, and opened the
  opposite hole: memory audited only the new slice, so a command failing once
  per turn was one failure per slice, under the two-failure floor every time,
  and a command that failed at every single turn was never written down.
  `workspace_memory.from_session` now returns raw counts and the floor moved to
  a new `promote()`, which applies it to the TOTAL (what MEMORY.md already
  knows + what this session adds). Memory reads the whole transcript again;
  what keeps a failure from being banked twice is `memory_written` on the
  session — how much of each command this session already put in the file.
  Lessons keep the watermark: they are about the agent, and re-auditing a
  transcript really does re-count them.
- **A refresh no longer kills the turn (2026-07-22, `db49f1c`, `5f9464e`)** —
  chat and agent turns run in `harness/turn_runner.py`, a server-side thread
  that buffers its events; HTTP connections became viewers reading by cursor,
  so hanging up (refresh, navigation, closed tab) ends the read, not the turn.
  `GET /api/chats/<id>/stream?cursor=N` re-attaches and the studio does it
  automatically when the server reports `running`, drawing the stored
  transcript only up to `turn_starts_at` so the replay does not double the
  turn's tool chips. Stop became `POST /api/chats/<id>/stop` — it used to work
  only as a side effect of dropping the connection. Measured before and after
  on the real studio: a cut mid-reply used to persist a truncated message
  flagged `interrupted`; it now completes (3704 chars, no flag), and an agent
  session finishes its remaining tool calls instead of abandoning the task
  after its side effects had landed.
- **A stale llama-server no longer hijacks the port (2026-07-22, `ce1d816`)** —
  a studio that died stranded its server on port 8091 with the model in VRAM;
  the next studio spawned a child that could not bind and, because the health
  check only asked the URL, talked to the ghost with the ghost's ctx/KV while
  its own child held ~8.8 GB for nothing. `ensure()` now reclaims the port
  first (only from a `llama-server` image) and the studio unloads its runtime
  in a `finally`.
- **Chat/studio migrated to llama-server; Ollama retired (2026-07-19,
  `77c3de0`..`61307ba`)** — the studio's chat and agent modes now stream from
  llama.cpp `llama-server` (OpenAI `/v1/chat/completions`, SSE) instead of
  Ollama, so the whole factory rides a single runtime. `llama_client` gained a
  `chat_stream` at the exact contract the UI consumes (thinking/content/
  tool_call deltas, metrics via `StopIteration.value`); the server runs with
  `--jinja --reasoning-format deepseek --cache-reuse 256`. `factory_mcp` owns
  the runtime with a "lock = VRAM" lifecycle: the GPU lock is taken on load and
  held while the model is resident, an idle timer (`chat_idle_s`, default 600 s)
  releases both so a queued job can run. The `/api/ollama*` panel became
  `/api/runtime*` and the Ollama tab became the Modèles tab reading the
  `[llama_server.models]` catalogue. Draft/ladder runners now require a
  `[llama_server]` section (no Ollama fallback). `ollama_client.py`,
  `ollama_pull.py` and their tests are gone; `RepetitionGuard` lives in its own
  module. Byte-stable prompt invariants are locked by tests for KV prefix
  reuse. 596 tests green. Real-GPU validation (regression parity, studio smoke,
  TTFT/tok-s) pending, to run detached.
- **ranks.js contract CLOSED on the third run (2026-07-17, `j_3a6a7681`)** —
  with the two fixes below in place: 13/13 spec + 219/219 project regression
  + review ACCEPT, 2 attempts (4/13 then 13/13, both `done_reason: stop`),
  260 s at rung 4. The contract that killed two whole jobs was never outside
  the 35b's envelope — the harness was starving its thinking budget and
  throwing away its retries. Diff applied to the blitzvolley working tree;
  `ranks.test.js` installed in the project suite.
- **Ladder attempts record done_reason/eval_count; num_predict cap 8192 -> 16384
  (2026-07-17)** — the ranks.js re-run (j_7e7c0bbb) failed on two codeless 35b
  replies that were undiagnosable: the ladder dropped the response metadata, so
  a reply truncated by num_predict looked identical to a model drifting into
  prose. Probes showed qwen3.6:35b spends ~6k thinking tokens on a module-scale
  contract before any code — 8192 left no variance headroom. Each attempt now
  records `done_reason` and `eval_count`; a codeless reply with
  `done_reason: "length"` gets a truncation-specific reminder (lead with the
  code block) instead of the format one; the cap doubles to 16384 (worst case
  ~4 min/attempt at 67 tok/s, still bounded).
- **NoCode replies burn a reminder retry, not an attempt (2026-07-17)** —
  direct follow-up of the blitzvolley pilot, where the job died on a codeless
  reply while holding its best diff (10/13). A `PatchError` reply now costs a
  retry-with-reminder instead of a ladder attempt; the retry prompt keeps the
  previous failing-test output (it used to be overwritten by the rejection
  message, so the model lost sight of which specs were red). Two codeless
  replies in a row leave the rung — the free retry must not become a prose
  loop with a model that has drifted out of code mode.
- **Blitzvolley in the perimeter + first real-project pilot (2026-07-16)** —
  `[projects.blitzvolley]` in `factory.toml` (node runner, regression glob
  excluding the two Windows-incompatible ELF-loading test files). Pilot
  delegation of `backend/ranks.js` (13-spec ELO->division contract) FAILED
  honestly at both rungs — 30b plateaued 3/13 (guard escalated correctly),
  35b peaked 10/13 then died on a NoCode reply; diff discarded, blitzvolley
  master untouched. Full analysis: `audits/20260715-blitzvolley-pilot.md`.
- **node --test runner — blitzvolley-shaped projects join the perimeter
  (2026-07-16)** — `harness/node_test_runner.py` mirrors vitest_runner's
  contract (ADR-010): spec tests are CJS, resolve worktree files via
  `NODE_PATH` (`require("backend/mod")`), TAP reporter for pass/fail counts
  stable across node versions (the default human reporter is
  unicode-decorated and unstable). `run_regression` uses a clean env (no
  injected `NODE_PATH`) so the model's own diff cannot shape the judge's
  verdict. `RUNNERS["node"]` wired into `loop_job.py` alongside pytest/vitest.
- **MCP servers in the perimeter — fetch, search, git, playwright
  (2026-07-16)** — `factory.toml` gains `[mcp.servers.*]` with a per-server
  `tools` allowlist and a `readonly` skip-approval list; search uses uvx
  `duckduckgo-mcp-server` (nickclyde), not the brief's npx package, because
  only it exposes both `search` and `fetch_content` per a real `tools/list`.
  Playwright trimmed to 6 tools (no screenshots — useless to text-only local
  models) to keep the web toolset inside the tool budget.
- **MCP registry + per-session toolsets (2026-07-16)** —
  `harness/mcp_registry.py` namespaces MCP tools and enforces a ~15-tool
  budget (small models degrade past it); `[toolsets.dev]` / `[toolsets.web]`
  in `factory.toml` curate which servers a chat/agent session sees, mixed
  with native tools per session.
- **Stdlib MCP client (2026-07-15)** — `harness/mcp_client.py`: hand-rolled
  stdio JSON-RPC client (no `mcp` SDK on Python 3.9), string-error contract
  so a dead or misbehaving server degrades one tool call instead of crashing
  the session. MCP call restarts are serialized under the server lock — a
  race could otherwise interleave two calls on one stdio pipe.
- **num_predict caps reach chat, agent and the ladder (2026-07-15)** —
  `harness/ollama_client.py` callers (chat turns, agent turns, and now
  `harness/loop.py` escalation attempts) pass a `num_predict` cap, closing
  the loop from the July 14 repetition incident at the point that actually
  burns GPU-hours: ladder attempts, not just chat turns.
- **Per-model sampling profiles (2026-07-15)** —
  `[models."qwen3.6:35b"]` / `[models."qwen3-coder:30b"]` in `factory.toml`:
  temperature/top_p/top_k/penalties per the Qwen model cards' documented
  presets, loaded via `model_options()` and applied wherever a model is
  invoked (an explicit per-call temperature still wins). Boolean sampling
  values are rejected — a `true`/`false` typo silently becoming Ollama's
  `1`/`0` was the near-miss that prompted the test.
- **Options passthrough + length-stop detection (2026-07-15)** —
  `harness/ollama_client.py`: arbitrary Ollama `options` now pass through
  instead of being silently dropped, and a `stopped: "length"` field
  distinguishes a reply cut by `num_predict` from one the model actually
  finished — the foundation the profiles and caps above are built on.
- **start_stage — delegate can enter the ladder at a given rung
  (2026-07-16)** — `delegate(..., start_stage=1..4)` lets a module-scale
  contract (self-contained module, tightly pinned spec) skip the rungs
  known to plateau on it instead of burning 7b/14b attempts first.
  Implements the spec's "démarrage direct barreau 3".
- **session-audit v1 + the 7b ceiling found (2026-07-15)** —
  `harness/session_audit.py` (analyze / to_markdown / audit_file over a
  `jobs/chats/*.json`: loops, wasted calls, errors, tok/s, verdict) shipped by
  the ladder itself (j_5a1e2ee8, 14 spec tests validated against a hidden
  reference first). The session's real result is the routing datum: 7b, 14b
  and 30b ALL plateaued at 9-10/14 on this ~180-line/3-function contract;
  qwen3.6:35b closed it (14/14 + regression) in 2 attempts, 704 s total.
  Precision on exact pins (keys, rounding, sorted flags, unicode header) is
  the gap, not capability — lesson 6 of session 1 confirmed at every rung.
  Reviewer label #4: correct ACCEPT with a genuinely useful note (~2.5/4).
  Full report: `audits/20260715-hybrid-session-2.md`.
- **vitest runner — lucena is inside the perimeter (2026-07-14)** —
  `harness/vitest_runner.py` mirrors pytest_runner's contract (ADR-010: the
  judge lives outside the tree): spec tests + a harness-owned dependency-free
  config in jobs/<id>/tests/, the source project's node_modules junctioned
  into worktree and tests dir (a git worktree has none) and detached before
  any teardown, `@`/`@work` aliases matching the house Next.js convention.
  `loop_job` dispatches on factory.toml's `runner` key (materialize/run/
  regression/cleanup), prompts ask for the target's language, `patch.py`
  accepts ts/tsx/js fences, `validate_tests` knows `*.test.ts`. Proven the
  same evening by a REAL delegation on lucena (j_1e75691f): 7b first rung,
  2 attempts, 14.8 s, spec 4/4, lucena's own suite green, repo untouched.
  13 new tests (real vitest included, skips cleanly without node).
- **Degenerate generations are cut, not watched (2026-07-14)** — post-mortem
  of chat session c_5a2a73ed (`audits/20260714-agent-chat-postmortem.md`):
  134 KB of thinking holding 35 unique lines, minutes of GPU per occurrence,
  stopped only by a human. `RepetitionGuard` in `chat_stream` drops the
  connection when the last 40 streamed lines hold ≤ 8 unique (plus a 120 k
  thinking-char cap); the partial is kept, the done event carries
  `stopped: "repetition"`, the UI says why, and an agent turn ends instead
  of re-prompting the looping model. 8 new tests.
- **Ladder aborts on structurally identical failures (2026-07-14)** — the
  ROADMAP item from j_bc119315 (10 attempts / 4 rungs / 688 s on one
  collection error): 3 identical abnormal-exit signatures running abort the
  run as `failure_reason: "structural"` in both `loop_job.py` and `loop.py`;
  an honest red run breaks the streak. Escalation is for capability, not for
  a broken contract. 2 new tests.
- **Honest live ctx % (2026-07-14)** — the status line estimated tokens at
  chars/3 while the session's own calibration measured 2.65 chars/token: the
  famous "ctx ~102 %" was real overflow, displayed too late. The estimate now
  uses the session calibration and flags "⚠ saturé" from 95 %.
- **Agent workspaces are git repos (2026-07-14)** — agent.md rule 7 (commit
  after every validated step) was unenforceable in a bare directory: session
  creation now `git init`s the workspace unless it is already inside a repo
  (never nested). `sandbox/` joined .gitignore — 13 GB of generated assets
  do not belong in this repo.
- **First real hybrid session — Claude manages, the ladder ships (2026-07-14)**
  — the "Hybrid controller" direction ran for real on ROADMAP items, not toys
  (full log: `audits/20260714-hybrid-session-1.md`):
  - *Fence-bearing targets survive extraction* — the first delegation on real
    harness code (target `loop.py`, which contains ``` in a regex) exposed
    `patch.py`'s non-greedy fence match truncating the file on all 10 attempts
    (job j_bc119315). Fixed cloud-side: greedy first-to-last-fence span joins
    the candidates, the largest that `compile()`s wins, and a python target
    with no compiling candidate is a `NoCode` the model can act on instead of
    a downstream collection error.
  - *Prefill + TTFT metrics (ROADMAP Next) shipped by the local ladder* —
    `loop.py` attempt records carry `prefill_tokens_per_s`/`ttft_s` (job
    j_9631eba1) and benchmark rows/tables aggregate them via new pure
    `summarize()`/`to_markdown()` (job j_0e2bf531). Both: qwen2.5-coder:7b,
    first rung, first attempt, ~2 min wall, regression 410/410.
  - *Reviewer agreement data (ADR-016)* — 2 labelled diffs: one false REJECT
    (imaginary indentation problem), one correct ACCEPT.
- **Models info page (2026-07-14)** — the Ollama tab's "Installés" table now
  shows family, parameter count, quantization, max context and capabilities
  (tools/thinking/vision) per model: `ollama_client.show()` (/api/show,
  stable subset), `GET /api/ollama/models` (status enriched with cards; a
  failing card degrades to name+size, never kills the listing). Verified in
  a real browser against 12 installed models.
- **Chat quick wins (2026-07-14)** — two operator-comfort items from the
  07-15 feedback list:
  - *Timestamps everywhere* — every bubble and tool chip shows its
    wall-clock time (stored `ts`, rendered top-right / in the chip summary);
    live lines use now. A session reads as a timeline, which session-audit
    will need anyway.
  - *Drag-n-drop files* — dropping a file on an agent session saves it to
    `<workspace>/uploads/<name>` (`POST /api/chats/<id>/upload`, bare
    filename enforced, 5 MB cap, base64 validated) and references it in the
    composer; on a plain chat (no workspace) the file's text is inlined
    into the composer instead. Verified in a real browser: upload through
    the UI token path lands on disk, zero console errors.
- **Context compaction actually fires (2026-07-14)** — a real agent session
  reached `ctx 65536/65536 (100 %)`, Ollama truncated the history silently
  and the reply was cut, while compaction never triggered. Root cause: the
  token estimator counted message `content` only, and agent messages carry
  their bulk in `tool_calls` (a write_file holds the whole file in its
  arguments) — so the estimate approved prompts that really overflowed.
  - *wire_chars* — estimates and per-session calibration now count
    tool_calls payloads (json size) on top of content.
  - *Eviction covers tool_calls* — outside the keep-last window, old
    tool_call arguments are emptied (name kept), mirroring the existing
    tool-output stubbing; the paired result was already a stub.
  - *Real-counter backstop* — if the previous turn's own Ollama counters
    (prompt_eval + eval) reached 90 % of num_ctx, the next turn forces a
    compaction regardless of what the estimate claims. Estimates can lie;
    the server's counters cannot.
- **Session c_5a2a73ed post-mortem fixes (2026-07-14)** — a "simple" agent
  session burned its 20-iteration budget twice; root cause: cmd.exe executes
  only the FIRST line of a multi-line command, so every multi-line
  `python -c` returned `exit 0` with no output (a fake success the model
  retried ~15 times, loop guard blind because each variant differed).
  - *run_command rejects multi-line commands on Windows* with the recovery
    move in the error (write a script with write_file, then run it); the
    tool description and the Windows shell note say it up front.
  - *OEM decoding* — child output is decoded utf-8 first, OEM codepage
    (cp850) as fallback; `dir` no longer floods the context with U+FFFD
    mojibake. Empty output is labeled `(no output)` instead of a bare
    `exit 0` that reads like success.
  - *Global agent.md always loaded* — the session ran in a fresh sandbox
    with no instruction file, so none of the operator's rules applied.
    `_agent_system` now loads the factory-root agent.md in every session,
    plus the workspace file when present (deduplicated when they are the
    same file).
  - *Iteration budget warning* — at 5 remaining the model gets a system
    note to finish or summarize, instead of hitting the cap mid-plan.
  - *Prefill visibility* — `prefill_tokens_per_s` exposed in the usage
    payload and shown in the chat meta line (agents are prefill-heavy;
    generation tok/s alone hides half the cost).
  - *CHAT_NUM_CTX 32768 → 65536* — per the 07-15 bench, 64k costs ~7 % gen
    speed on this box; 128k stays per-session until benched.
- **Operator feedback pass (2026-07-15)** — everything actionable from
  Martin's field notes after real use:
  - *Approval deadlock chain fixed* — panels bound to their session id at
    creation (was: querySelector on a not-yet-attached root → crash on
    refresh, stale/empty id → `POST /api/chats//approve` 404, session
    unrecoverable) and the iteration cap no longer deadlocks auto mode
    (queued calls resume on the next message; only a call genuinely awaiting
    approval blocks).
  - *"I cannot modify files"* — the agent system prompt now states write
    access outright (write_file/edit_file/run_command incl. git); instruct
    models kept denying capabilities nothing told them they had.
  - *Viewport layout* — the page itself never scrolls in the chat view; the
    sessions column and the log scroll in their own blocks, which finally
    lets the log's magnet auto-scroll (stick when at bottom, release on
    manual scroll up) do its job.
  - *Per-click latency* — parallel fetches + 30 s client cache for the
    Ollama status (it pings the Ollama server and lagged whenever a model
    was loading/generating).
  - *Honest phases* — status line starts at "chargement modèle + prompt"
    until the first delta (was billed as "réflexion"); banner shows whether
    the session's model sits in VRAM (● chargé / ○ non chargé).
  - *Live context + per-session num_ctx* — status line estimates window
    usage while streaming; each session picks its num_ctx (8k–128k select,
    validated server-side); meta line splits réflexion/rédaction with
    durations and ~token counts (chat_stream measures both phases).
  - *agent.md rule 7 (git discipline)* — commit+push each validated step,
    never stash (a stash destroyed local work on 07-13), no history
    rewrites; dangling AGENT_RULES.md reference removed.
  - *Perf falsification* — OLLAMA_FLASH_ATTENTION + KV q8: zero effect on
    this box (bottleneck = CPU-offloaded MoE experts); 64k ctx costs only
    ~7 % gen speed. Cleanup killed 4 zombie llama-servers and reset the
    Ollama app's own context_length (262144 → 32768, another 07-13
    leftover). `experiments/results/20260715_ollama_fa_kvq8_bench.md`.
  - Studio favicon; chat/agent lane backlog (DA overhaul, structured
    missions, personas, global memory, models info page, session analytics,
    git policy, run_command confinement) consigned in ROADMAP.md.
- **Agent hardening after the 2026-07-13 in-tool sessions** — main was first
  restored to `a848a10` (the sessions run from the factory's own agent mode
  had committed a broken `read_file`, `CHAT_NUM_CTX`/Modelfiles at 260k that
  overflowed the 12 GB GPU and dropped generation from ~60 to ~10 tok/s,
  `plateau_k=1000000`, a dozen one-shot analysis scripts and a
  simulated-data log). The Ollama models `coder`/`coder36` were rebuilt at
  `num_ctx 32768` (measured back at ~63 tok/s). Then, fixes for the real
  failure modes those sessions exposed:
  - *Loop guard* — identical (tool, args) → identical output 3 times injects
    a system nudge the model reads; 5 times ends the turn with
    `stopped: loop_detected`. Output changes reset the count, so re-running
    pytest after each edit never triggers.
  - *Self-correcting tool errors* — `read_file` past EOF is an explicit error
    (was `(empty)`, re-read forever); `edit_file` 0-match/N-match errors say
    how to recover. The agent system prompt names the OS and shell (no grep
    on Windows), steers toward the built-in tools and states the
    copy-verbatim edit rule.
  - *Failures reach the UI* — urllib errors carry Ollama's own body (model
    not found, OOM); an exception after the NDJSON headers went out is
    written as a final `{"error": …}` line instead of silently dropping the
    socket.
  - *Chat status & stop states* (TODO_chat_improvements) — live status line
    (state, elapsed, ≈tok/s), explicit end states (✓ terminé + real tok/s,
    ⛔ stopped + reason + how to resume, ⏸ approval, ⏹ operator abort), Stop
    button (abort saves the partial), loop notices in the log, auto-scroll
    that respects a manual scroll up, full-width chat, taller input.
  - *Agent workspace* — the web route forwarded no workspace: every agent
    session silently worked in the server's cwd. The new-session form takes
    a path (validated as a directory) and the banner shows it.
  - *Statics no-store* — a stale cached app.js made fresh server code look
    broken; `[hidden] { display: none !important }` restores the hidden
    contract against author display rules.
- **Chat context management** — at prompt-build time (session files keep the
  full record): old tool outputs stubbed (`[résultat d'outil effacé]`, last 12
  messages spared), same-model structured compaction above 70 % of the budget
  (summary persisted in the session, pinned as a system message, verbatim tail
  of 6 kept), trim-with-warning as the safety net so Ollama never truncates
  silently. Token budget = `num_ctx − 8192`, chars-per-token calibrated per
  session from real `prefill_count`. UI: "compactage…" chip, "— messages
  au-dessus compactés —" divider, "N messages hors contexte" in the meta line.
  Spec: `docs/design/specs/2026-07-12-chat-context-management-design.md`.
- **Chat context window + usage display** — chat/agent sessions now request
  `num_ctx=32768` (`factory_mcp.CHAT_NUM_CTX`) instead of the client default
  16384, which long sessions silently overflowed: Ollama dropped the oldest
  tokens and replies degraded (shorter, thinking without an answer — observed
  on qwen3.6:35b, same failure class as ADR-008). Each assistant message now
  stores and displays `prefill_count`, `eval_count`, `done_reason` and the
  window: the meta line reads `61 tok/s · ttft 1.2 s · ctx 8500/32768 (26 %)`
  with an orange warning when the context is ≥85 % full, overflowed, or the
  reply was cut (`done_reason: length`).
- **Chat tab** — a Claude-Code-desktop-like discussion space in the Studio:
  streamed multi-turn conversations with any installed Ollama model, sessions
  persisted on disk (`jobs/chats/`, one JSON per session via `chat_store.py`),
  per-session model switch. `ollama_client.chat_stream` yields deltas over
  `/api/chat`; `Factory.chat_reply` holds the GPU lock for the whole
  generation (a busy GPU answers 409, a mid-stream failure keeps the partial
  reply). `/api/chats*` routes stream NDJSON to a vanilla `fetch` reader.
- **Studio v2: apply, Ollama, pulls, drafts, projets** — the browser closes the
  loop end to end. `job_apply` puts a succeeded diff in the project's working
  tree (`git apply --check` first; never a commit or a stage). Ollama page:
  status via `/api/tags` + `/api/ps`, auto-start of `ollama serve` at web
  boot, preload/unload guarded by a non-blocking take of the GPU lock. Jobs
  gained a `kind` (`delegate`/`pull`/`draft`) and `Factory._spawn_runner`
  dispatches the runner from it. `ollama_pull.py` streams `/api/pull` progress
  into `state.json`. `draft_job.py` is the spec-writer wizard: goal in, JSON
  proposal out (tests/targets/context/assumptions), advisory validation,
  "Utiliser ce brouillon" prefills the delegate form. `add_project` appends to
  factory.toml (re-parsed before an atomic replace; comments survive). CLI:
  `apply`, `draft`.
- **Web UI** (`harness/factory_web.py` + `harness/web/`): studio shell over the
  Factory facade. v1 = Jobs / Déléguer / Dashboard; Goals, Benchmarks and
  Agents greyed out pending their backend. Stdlib HTTP server, 127.0.0.1
  only, per-run CSRF token on every POST.
- **Advisory reviewer on green jobs** — the final rung's model reviews the diff
  with the Reviewer persona; `result.json` carries `review: {verdict, notes}`.
  Advisory until its agreement rate is benchmarked (ADR-016); a reviewer crash
  never costs a green job.
- **Operator CLI** (`harness/factory_cli.py`) — delegate/status/result/log/cancel
  from a plain terminal; `result --save-diff` writes the patch for a manual
  `git apply`. The manual-takeover guarantee: no Claude credits never means a
  stalled factory (docs/vision.md).
- **Vision captured** — `docs/vision.md`: the full-studio north star (creative
  workers, agent judges, PO-owned acceptance criteria, automatic end-of-project
  debriefs) with its challenge log. ROADMAP reprioritized around it.
- **Judge tampering rejects the job** — a non-empty `restore_judge` report stops the
  ladder and returns `rejected` with `failure_reason: judge_tampered`; the regression
  suite never runs on a tampered tree. Closes the one-shot-tamper hole where a plant
  that fires only during the spec run left a clean diff and read `succeeded`.
  `restore_judge` now reports only files that actually differed from HEAD. ADR-015.
- **Delegation v2** — `context_files` (read-only reference material in the prompt) and
  a per-project `regression_cmd` that must also pass before a job is `succeeded`.
  ADR-014.
- **Delegation harness** — Claude (cloud) hands a task to the local ladder and walks
  away. Four pieces, no daemon: `harness/job_store.py` (filesystem-as-database, atomic
  writes, liveness derived from a heartbeat), `harness/gpu_lock.py` (non-blocking,
  OS-released on death), `harness/loop_job.py` (detached runner: worktree in, diff out),
  `harness/factory_mcp.py` (stdio JSON-RPC MCP server, five tools), `factory.toml`
  (project allowlist). ADR-010..013. 143 tests.
  - Tests are materialized *outside* the worktree, so the loop cannot rewrite its own
    judge. The `tests/conftest.py` monkeypatch escape was reproduced against the naive
    layout (exit 0, "1 passed", bug intact) and is closed here.
  - `capture_diff` stages with `git add -A`: plain `git diff` is blind to files the
    model created.
  - Multi-file jobs via `### FILE: <path>` headers in the reply.
  - `ollama_client.unload()` evicts the previous rung before the next one loads —
    30b + 35b resident at once is ~38 GB on a 31 GB machine.
- Escalation ladder in `loop.py`: plateau detection → climb 7b→14b→30b with a
  clean slate on escalation (ADR-009). Validated on the hard `expr` task.
- Added hard `expr` evaluator task (parser: precedence/parens/unary) that
  discriminates models — 30b MoE solves it, 7b/14b plateau.
- Installed + configured **aider 0.82.3** for local Ollama (num_ctx set to avoid the
  context-truncation "forgetting" bug); see `docs/aider.md`.
- Model research: Onyx leaderboard vetted (reliable but its top tier needs 100s of GB);
  12 GB roster shortlisted in `docs/roles.md`/response.

## v0.2.0 — Project restructure + working harness
- Renamed `ai-factory` → `local-factory`; reorganized into docs/personas/models/
  tasks/harness/experiments.
- Added working loop runner (`harness/loop.py`) that injects `agent.md` as the
  system prompt, runs the bounded edit→test loop, counts stub violations, and
  restores the seeded bug via git.
- Added `harness/benchmark.py` (N-run aggregation → JSON + markdown) and a
  stdlib-only `ollama_client.py`.
- Added 9 role personas and `docs/roles.md` (org chart + prompt/skill/fine-tune guidance).
- Versioned the Modelfiles under `models/Modelfiles/` (previously deleted).
- Moved the calculator + roman testbeds into a `tasks/` benchmark suite with task.json.

## v0.1.0 — Scaffold
- Compiled `coder` (32k, 0.1) and `architect` (128k, 0.3) from `qwen3-coder:30b`.
- Governance (`agent.md`) + architecture blueprint.
- Calculator + Roman testbeds with intentional bugs and pytest specs.
- Fixed the broken Windows `python`/`pip` PATH (Store stubs → real interpreter).
# Agent Mode in Chat Tab

## 2026-07-12 — agent mode (T9+T10+T11)

### Agent chat sessions
- New-session form: **chat** / **agent** toggle + **approbation**/ **auto** dropdown
- Agent sessions show `⚙` badge in session list + mode selector per session
- Write/shell tools require operator approval (approve mode); reads always auto-execute

### Streamed tool events
- NDJSON stream yields `tool_call`, `tool_result`, `thinking`, `chunk`, `approval_needed`, `done`
- Tool chips expand to show output inline
- Approval panel shows diff/command detail with Approuver/Refuser buttons (auto-resumes after)

### Engine core
- `agent_tools.py`: 6 tools (list_dir, read_file, search, write_file, edit_file, run_command)
  with workspace confinement, truncation, sandbox escaping refused
- `agent_reply()`: tool loop (max 20 iterations), GPU lock held during generation, released on approval gate
- `chat_approve()`: resolves parked calls, resumes agent loop

### E2E smoke
- NDJSON stream verified against `qwen-agent2:latest` (list_dir returned correctly)
- Approval flow + Playwright browser test to verify manually

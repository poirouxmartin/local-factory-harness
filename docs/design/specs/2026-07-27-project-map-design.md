# Project map — design (2026-07-27)

## Why

An agent that cannot find things reads everything, and an agent that reads
everything forgets what it read.

`session_audit.analyze` run over all 256 stored chat sessions
(`jobs/chats/c_*.json`), 2026-07-27:

| | all 256 | last 40 |
|---|---|---|
| tool calls | 4937 | 1938 |
| **byte-identical calls replayed** | 854 (**17%**) | 469 (**24%**) |
| tool errors | 282 (6%) | 113 (6%) |
| thinking loops detected | 6 | 0 |

The worst session, `c_d9e12d6a` (qwen3.6:35b, 1754 messages, 814 tool calls,
257 of them duplicates):

```
x29  read_file  harness/web/app.js
x23  read_file  TODO_chat_improvements.md
x22  list_dir   harness
x20  list_dir   .
x16  list_dir   harness/web
```

`harness/web/app.js` is 56 KB — roughly 19k tokens, about 30% of the chat
lane's `CHAT_NUM_CTX` (65536). One read crosses `COMPACT_THRESHOLD`,
compaction evicts old tool output (`chat_context.evict_tool_outputs`,
`EVICT_KEEP = 12`), the file the model just read is replaced by
`[résultat d'outil effacé]`, and it reads it again. Twenty-nine times.

`loops` is 0 on that session: `RepetitionGuard` judges lines of generated
text, not tool calls. The loop is real and the existing detector cannot see
it, because it happens one level up.

Searching does not rescue it. `agent_tools._search` calls
`sorted(root.rglob(glob or "*"))`, which materialises the entire tree —
over 200,000 paths here, including 2.7 GB of `.git` — before the first match,
since `SKIP_DIRS` is only applied afterwards. It then calls `read_text()` on
every file found, including `models/gguf/*.gguf`, which raises `MemoryError`,
whose `str(e)` is empty. That is the `error: search failed:` with no reason,
15 times in that one session. (Repairing `_search` is separate work; this
design aims to make most searches unnecessary in the first place.)

And the one map that exists is poisoned. `MEMORY.md`, in a section the agent
wrote on 2026-07-25 and which ships in every system prompt since, states:

> **Backend** : FastAPI (`harness/factory_web.py`), port 8787

`harness/factory_web.py:1` reads *"Stdlib only, like everything here"* and
uses `BaseHTTPRequestHandler`. FastAPI does not appear anywhere in the
repository. An agent told to look for route decorators that do not exist will
search until it is stopped.

The failure is therefore two-sided: there is no map of where things are, and
the prose the agent wrote instead is wrong. A design that only adds a place to
write would reproduce the second half.

## What we build

One generated map in the system prompt, carrying verified per-file
annotations. Generated content states *where*; annotations state *why*; the
generator can never be contradicted by an annotation, and an annotation whose
anchor moved says so instead of lying.

### 1. `harness/project_map.py` — pure functions, no I/O

Follows the convention of `lessons.py`, `workspace_memory.py` and
`chat_context.py`: pure functions over plain dicts, `factory_mcp` owns the
files and the wiring.

```
skeleton(entries)                -> deterministic index lines
render(skeleton, notes, budget)  -> the prompt block, bounded
vocabulary(sources)              -> the set of tokens the repo evidences
verify(note, vocabulary)         -> reject claims with no evidence in the repo
stale(notes, blobs)              -> flag notes whose anchor blob changed
```

`entries` is `[{path, lines, doc, blob}]`; `notes` is
`[{path, text, blob, last_seen}]`; `sources` is `{path: text}`, which
`factory_mcp` has already read to build `entries`. None of these functions
opens a file.

`vocabulary` is split out rather than folded into `verify` so that the token
set is built once per session and reused across every note checked, instead of
being rebuilt per line.

### 2. Scope comes from git

`git ls-files` in the workspace decides what belongs to the project: 253
files here, 20546 chars, ~6848 tokens (measured 2026-07-27 — 99 `.py`, 92
`.md`, 55 `.json`; an earlier estimate of 9393 chars counted code files only
and was wrong about the real repo).

This is not a stylistic preference. The first prototype of this map used a
hand-maintained blacklist, forgot `venv/`, and produced 22,122 files and
839k tokens — the identical failure mode as `_search`. A blacklist is a
promise to remember every vendor directory forever; git already knows.

Workspaces without git fall back to a **bounded** walk (depth and count
capped, vendor names skipped). Never an unbounded walk: that is the bug being
removed elsewhere in the same pass.

### 3. Storage: a third marked block in `MEMORY.md`

`MEMORY.md` already carries `<!-- factory:auto -->` (observed command
failures) and `<!-- factory:carnet -->` (agent walls and notes). Annotations
get `<!-- factory:map -->`, same round-trip-through-markdown discipline, for
the reason already recorded in `workspace_memory.py:14` — *a memory nobody
can read teaches nobody*.

```
<!-- factory:map -->
- `harness/chat_context.py` @a1b2c3d : le carnet est reprojeté à chaque tour,
  il n'existe sur aucun disque
- `harness/factory_web.py` @9f4e21c : stdlib pur, BaseHTTPRequestHandler
<!-- /factory:map -->
```

Only annotations persist. The skeleton is regenerated every session, so it
cannot go stale by construction. `@<sha>` is the git blob of the anchor file:
when it differs from the current blob, the annotation renders `(périmé)`. It
is never silently deleted — a note the agent can no longer trust is
information, a note that vanishes is not.

### 4. What reaches the prompt

One block, not two. Annotated files carry their note inline, so there is a
single place to look.

```
--- Project map (git ls-files) ---
harness/agent_tools.py (328L) Workspace-confined tools for the chat agent.
harness/chat_context.py (148L) Prompt-side context management.
    ^ le carnet est reprojeté à chaque tour, il n'existe sur aucun disque
harness/factory_web.py (517L) Web UI server: the factory in a browser.
--- End project map ---
```

Deterministic sort, bounded render: this is the KV prefix under
`--cache-reuse` (locked 2026-07-19). Same entries in, same bytes out, or the
cache breaks every turn and the block costs more than it saves.

`MAX_MAP_CHARS = 21000` (~6800 tokens, ~10% of a 65536 window). Above it, the
render degrades to directory level plus the largest files per directory. It
degrades; it does not truncate mid-list, which would silently hide whatever
sorts last.

The budget is paid for: removing the git MCP server from `[toolsets.dev]`
frees ~1900 tokens of tool schemas (8 schemas at a measured 242 tokens each,
`docs/backlog-optimisations.md:90`) that served 46 of 4937 calls (0.9%), all
of them reachable through `run_command`.

### 5. The annotation cycle

**Trigger — the harness, never the model.** Two existing hooks: `step_done`
when a plan is live, and end of session, where `lessons.from_audit` already
runs (`factory_mcp.py:532`). The model is not asked to remember to write
notes; the thing it is worst at is not made a prerequisite.

**Selection.** Files actually read, written or edited during the step, taken
from the transcript's `tool_calls` — the pattern already exists in
`workspace_memory._calls` (line 67). Files already carrying a fresh
annotation are skipped: what has been recorded is never asked for twice.
At most `MAX_NOTES_PER_STEP = 8` files per cycle, newest first.

**Production.** One bounded call: *for each of these files, one line — what
you understood that is not readable from the skeleton*. One line per file,
`MAX_NOTE_CHARS = 160`. No free-form prose, no architecture essays: the
architecture essay is the artifact that produced "FastAPI".

**Verification.** The rule that would have caught it: *an annotation may
assert only what the repository can evidence.* Each note is scanned for three
token shapes, and every token found must appear literally in `vocabulary`:

- anything inside backticks,
- identifiers — `snake_case`, `CamelCase`, or containing `.` or `/`,
- capitalised words that are not the first word of a sentence.

Everything else — ordinary prose, French connective words, numbers — is not
checked, because it asserts nothing checkable. `FastAPI` is caught by the
third rule and by the second. A line that fails is rejected whole, naming the
token that had no evidence.

This catches invented names, which is the observed failure. It does not catch
a false causal claim built entirely from real identifiers; nothing mechanical
would, and pretending otherwise would be the same overreach as trusting the
prose in the first place.

A rejection is not swallowed. It becomes a `lessons.incident`, so it surfaces
in the audit. Silent rejection would rebuild exactly the blind spot that let
`error: search failed:` pass 15 times.

### 6. One system-prompt rule

Added to `AGENT_SYSTEM`. It sits in the KV prefix, so every word is paid for
the whole session:

> The project map above is authoritative for where things are. Do not use
> `list_dir` or `search` to locate a file it already names.

## Testing

`harness/tests/test_project_map.py`, over the pure functions, in the style of
`test_from_job_outcome.py`.

- **Determinism** — same entries, identical bytes. This is the KV prefix
  contract, and the only test whose failure degrades performance silently.
- **The FastAPI case as a regression fixture** — `verify()` must reject
  "Backend : FastAPI (`harness/factory_web.py`)" against an entry set in which
  FastAPI appears nowhere. Today's bug becomes tomorrow's test.
- **Staleness** — anchor blob changed, note renders `(périmé)`, and is still
  present.
- **Budget** — an oversized entry set degrades to directory level rather than
  truncating.
- **No git** — the fallback walk is bounded in depth and count.
- **Selection** — a file merely listed by `list_dir` is not a candidate; a
  file read or edited is; a file with a fresh note is skipped.

## What this does not do

- It does not order the workflow phases (sub-project A).
- It does not display the plan in the UI (sub-project B) — nothing is exposed
  over HTTP for it today: `carnet` and `plan` appear in neither
  `harness/factory_web.py` nor `harness/web/app.js`.
- It does not repair `_search` or the eviction stub (sub-project F, already
  agreed, independent). The map should make most searches unnecessary; it does
  not make the broken ones work.

## Decided

The false `FastAPI` line in the human-owned part of `MEMORY.md` is corrected
as part of this work. The rest of that hand-written section is left untouched:
above the markers is the operator's territory, and the harness must never
rewrite it.

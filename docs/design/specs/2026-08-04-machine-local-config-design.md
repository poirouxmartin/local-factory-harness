# Machine-local config — design (2026-08-04)

## Why

`factory.toml` is tracked in git and holds absolute paths to this operator's
machine. Cloning the repo onto a second computer therefore produces a factory
that loads, reports no error, and cannot delegate to anything.

Measured on the second machine, 2026-08-04 — every path in the tracked file,
tested with `test -e`:

| key | value | exists |
|---|---|---|
| `projects.local-factory.path` | `C:/Users/me/local-factory` | no |
| `projects.lucena.path` | `C:/Users/me/lucena` | no |
| `projects.conformergpd.path` | `C:/Users/me/side-projects/conforme-rgpd` | no |
| `projects.crgpd-rerun.path` | `.../experiments/crgpd-rerun` | no |
| `projects.blitzvolley.path` | `.../Blobby/BlobVolley` | no |
| `projects.factory-demo.path` | `C:/Users/me/factory-demo` | no |
| `projects.opti_chess.path` | `.../Documents/Info/Chess/C++` | no |
| `projects.GPT_code.path` | `.../Documents/Info/Projects/GPT_online_test` | no |
| `llama_server.exe` | `C:/Users/me/tools/llama.cpp/llama-server.exe` | no |

9 of 9. The projects themselves are all present on the second machine, under
`C:/Users/me/Projects/<name>` — only the recorded coordinates are wrong.

Two further machine facts sit in the same file and fail more quietly. The
`llama_server.models.*.path` entries are Ollama blobs pinned by sha256, and the
file's own comment states the trap: "re-pulling a model changes the sha and must
be reflected here" — so reinstalling Ollama does not repair them. And
`--n-cpu-moe 24|26` plus `ctx = 32768` are tuned for a 12 GB card
(bench 2026-07-19: 31 vs 59 tok/s once KV spills to system RAM); on different
hardware they are wrong with no error at all, only bad throughput.

The mechanism that put the paths there is still live. `add_project`
(`harness/factory_mcp.py:1032`) does `Path(path).resolve()` and appends the
absolute result to the tracked file. It is an MCP tool: the local agent calls it
mid-session. The application mutates a versioned file with machine state.

`[projects.factory-demo]` is the proof. It is the only entry in the file with no
comment, because no human wrote it — it was added from the studio UI during the
E2E audit (`audits/20260722-studio-e2e.md:23`) as a disposable target, and
`audits/20260725-delegation-economics.md:34` later lists it among the "toy
targets" polluting the measurement corpus.

## What does not change

`secrets.toml` already solves its half of this problem correctly:
gitignored, environment-first, atomic narrow writes (`harness/credentials.py`).
It is the model this design copies, not something to fix.

`core.hooksPath` cannot live in the repo — a versioned hook that armed itself
would be a security hole, which is why git requires the per-clone opt-in.
CLAUDE.md already documents it ("une fois par clone") and documentation alone
demonstrably did not survive the clone: `git config core.hooksPath` returns
empty on the second machine, so the guardrails from
`2026-07-22-agent-git-guardrails-design.md` are inert there.

## The two files

**`factory.toml`** (tracked) — the perimeter and the doctrine. Which projects
exist, how they are judged, how models are sampled, which tools are exposed.
Its comments are the content: the dated green baselines ("13 files / 58 tests
pass (2026-07-17)"), the measured token prices, the verified model ids.

**`factory.local.toml`** (gitignored, beside it) — this machine's coordinates.
Where things are, and how this GPU wants them.

Located exactly as `credentials.path_for` locates `secrets.toml`: a sibling of
`config_path`. No public signature changes; `factory_config.py` is the only
module edited, and the eight that consume it — `draft_job`, `factory_cli`,
`factory_mcp`, `factory_web`, `loop`, `loop_job`, `patch`, `regress` — are
untouched.

### Merge rule

One rule for the whole file, applied by a single private
`_load_merged(config_path)` that returns the merged raw dict. Every public
`load_*` reads from it, so the rule has one implementation and one test suite.

- Tables merge recursively.
- Scalars and arrays are **replaced wholesale** by the local file. An `args` or
  a `regression_cmd` must be rewritable entirely, never unioned.
- A key present only in the local file is added.
- The local file wins on conflict.

### Local-only keys

`projects.*.path`, `llama_server.exe`, `llama_server.models.*.path`. No default
is meaningful for a coordinate.

**Loading refuses a `factory.toml` that contains one of them**, with a
`ConfigError` naming the offending key. Without this guard nothing stops a
future `add_project`, or a hurried human, from putting a path back into the
tracked file, and the regression returns unannounced. This is the same posture
as the existing refusal of unknown `SAMPLING_KEYS`: the mistake is impossible,
not merely discouraged.

Everything else stays in the tracked file and becomes overridable: `runner`,
`regression_cmd`, `ctx`, `args`, `port`, `tools`, `agent_max_iterations` — each
keeping the comment that explains its value, attached to the value it explains.

## Behaviour

### A project with no local path

`resolve_project` gains a third failure mode beside "not a key" and "not a git
repo": entry present, `path` absent.

```
ProjectNotOnThisMachine(ProjectNotAllowed)
```

Message names the project and the fix:

> `blitzvolley` is declared in factory.toml but has no path on this machine —
> add `[projects.blitzvolley]` `path = "..."` to factory.local.toml

The project stays visible in the list and the UI, and is refused at the point of
use. Filtering it out at load was rejected: a forgotten path would make a
project vanish silently, which is the mute failure mode this repo fights
elsewhere.

Verified 2026-08-04: all ten `except` sites outside `factory_config.py` catch
`ConfigError`, the base class — none catches `ProjectNotAllowed` specifically.
A subclass propagates correctly through every existing handler without edits.

### add_project

Writes the full entry — `path`, `runner`, `regression_cmd` — to
`factory.local.toml`, creating the file if absent. Same discipline as today:
append never rewrite, candidate re-parsed before `atomic_write_bytes`.

Promotion to the tracked file stays a deliberate human act. A comment like
"verified green baseline with this exact command" can only be written by someone
who ran it; doctrine is not generable.

One guard changes meaning: the duplicate-name refusal tests the **merged** view,
not the local file alone. Otherwise the agent can silently shadow a versioned
project by reusing its key.

### `factory_cli.py bootstrap`

Idempotent, three acts:

1. arms `git config core.hooksPath .githooks`;
2. creates `factory.local.toml` if absent — never overwrites — pre-filled with a
   commented stub per project key found in the tracked file, plus the
   `[llama_server] exe` stub;
3. reports as warnings what the host is missing: local paths absent or not git
   repos, `llama_server.exe` not found, model paths not found, `runner =
   "vitest"` projects with no `node_modules`, and an unset `user.name` /
   `user.email` — found the hard way on 2026-08-04, when git refused the commit
   carrying this very spec.

Always exits 0. Bootstrap is a setup action, not a CI gate; the report informs.

## Tests

New `harness/tests/test_local_config.py`:

- merge: scalar override (`ctx`), wholesale array replacement (`args`,
  `regression_cmd`), key present only in local, recursive table merge;
- local-only guard: a `path` in the tracked file raises `ConfigError` naming the
  key; same for `llama_server.exe` and `models.*.path`;
- `resolve_project` on a pathless entry raises `ProjectNotOnThisMachine` and the
  message names both the project and the file to edit;
- **absent `factory.local.toml` still loads.** The tracked file alone must remain
  a valid file, or the fragility has only moved.

Migration: `grep -n 'path = "' harness/tests/*.py` returns 23 lines across 11
files; 22 are config coordinates written into a single tmp `factory.toml`, and
`test_factory_web.py:130` (`h.path = "/"`) is an HTTP handler attribute that
stays put. A helper in the existing
`harness/tests/conftest.py` writes both files from a versioned body plus a
`project → path` dict; the fixtures get shorter. Mechanical, but it is the real
cost of the split and is named as such.

## Repository migration

1. delete `[projects.crgpd-rerun]` — its own comment says "Remove after the
   re-run audit" — and `[projects.factory-demo]`, leaving six real projects;
2. generate `factory.local.toml` with this machine's six project paths;
3. remove the remaining 10 machine keys from `factory.toml` (6 project paths,
   `llama_server.exe`, 3 model paths);
4. add `factory.local.toml` to `.gitignore`;
5. run `bootstrap` to arm the hooks;
6. full suite green before commit.

## Out of scope

`llama_server.exe`, the Ollama blobs and the 35b GGUF do not exist on the second
machine. This design makes them correctly configurable; it does not install
them. The local lane stays out of service there until llama.cpp and the models
are present. Prerequisite, not task.

Resolving Ollama models by name at runtime instead of by pinned blob sha would
remove both the machine dependency and the documented re-pull trap. Real
improvement, separate change — it alters how the ladder finds weights, which
this design deliberately does not touch.

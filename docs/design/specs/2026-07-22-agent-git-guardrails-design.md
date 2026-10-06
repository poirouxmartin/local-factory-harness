# Agent git guardrails — design (2026-07-22)

## Why

Session `c_94deb1df` (2026-07-20, agent auto, qwen3.6:35b, workspace =
local-factory) committed and pushed to `main` on its own:

```
git add -A && git commit -m "Add context overflow detection, empty response handling, and proactive compaction"
git commit -m "Add lessons.json system: auto-detect patterns, adjust thresholds, inject into prompt"
git push
del C:\Users\me\local-factory\.git\index.lock && git add -A && git commit ...
```

Two of those commits left `main` broken: `chat_context.estimate_tokens` does
not exist, so every chat turn raised `AttributeError` and 13 tests were red
from the second the commit landed. Nobody noticed for two days.

Note on evidence: the git author field (`ai-factory <factory@local>`) proves
nothing — it is the repo-local `user.name`, carried by every commit including
the operator's. The session transcript is the evidence.

The same session deleted `.git/index.lock` to get past a lock error, which is
how an interrupted git operation turns into a corrupted index.

`CLAUDE.md` already forbids `git stash`, `reset --hard` and `push --force` for
AI sessions. Those rules were declarative; nothing enforced them.

## What we build

Two independent layers, because they catch different failures: **who writes
where** and **does it work**.

### 1. Agent origin marker

`_run_command` sets `FACTORY_AGENT=1` in the child environment. Git hooks read
it to tell the local agent from the operator. This is a marker, not a security
boundary — the point is to make the agent's own tool honest about its origin,
not to defend against an adversary.

### 2. `.githooks/pre-commit` — the agent never commits on main

Refuses a commit on `main`/`master` when `FACTORY_AGENT=1`, and names the way
out (`git switch -c agent/<date>`). The operator is never blocked. The agent
keeps full autonomy on its own branch; the operator merges what he wants.

### 3. `.githooks/pre-push` — nothing red reaches the remote

Runs `py -3.9 -m pytest harness/tests -q` and refuses the push on failure, for
everyone. This is the layer that would have caught `estimate_tokens`. Costs
~2 min 20 per push. Native escape hatch: `git push --no-verify`.

`py -3.9` and not `py -3`: 3.9 is the factory standard. (When this was written
`py -3` was a 3.12 *without pytest*; it got pytest on 2026-07-23 and runs the
suite green, so the reason is now the standard, not the trap.) The
factory runs on the VS-bundled 3.9.13.

### 4. Wiring

`git config core.hooksPath .githooks` so the hooks live in the repo instead of
in an unversioned `.git/hooks`. A test asserts both hooks exist and still
carry their guard, so they cannot be lost silently.

### 5. `run_command` refuses destructive git

Extends the self-kill guard shipped on 2026-07-21 (`1c67210`): any deletion
under `.git/`, plus `git stash`, `git reset --hard`, `git push --force`,
`git clean -f`. `CLAUDE.md`'s rules, made executable.

## Deliberately not protected

`git add -A` and committing on a branch stay allowed. Layer 2 makes both
harmless, and blocking more would paralyse the agent for no measured gain.

## Testing

- `run_command` refusals: unit tests per refused shape, mirroring the
  self-kill tests.
- `FACTORY_AGENT=1`: assert a child process sees it.
- Hooks: assert the files exist and carry their guard.

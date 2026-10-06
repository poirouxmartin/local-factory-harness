# /goal v1 — the manager contract (design)

Date: 2026-07-17. Status: shipped with this commit.

## What it is

The hybrid controller made explicit (ROADMAP "Hybrid controller: Claude plans,
local executes — the contract is the brief format, not more chat glue"). v1 is
deliberately NOT an autonomous loop: it is the **contract + bookkeeping** that
lets any manager (a Claude session today, a scheduled process later) run the
loop goal → backlog → delegate → review → merge → debrief without losing state
between sessions.

## The brief format (goal.md)

Written by the PO/architect role (human or Claude). Free markdown, three
required parts by convention (not parsed by the harness — the manager reads it):

```markdown
# <title>

## Objective
One paragraph: what "done" looks like, in product terms.

## Acceptance criteria
- [ ] Verifiable statements a reviewer can check on the real project.

## Constraints (optional)
Perimeter notes: files off-limits, style, budgets.
```

The `project` is a factory.toml key, validated at `goal new` time — the goal
perimeter is the delegation perimeter.

## Storage — the filesystem is the database (same idioms as jobs/)

```
goals/g_<hex8>/goal.md      # the brief, written once at create time
goals/g_<hex8>/state.json   # atomic-write JSON, single writer (the manager)
goals/g_<hex8>/debrief.md   # written by finish(), part of the workflow (vision.md)
```

state.json:

```json
{
  "title": "...", "project": "conformergpd", "status": "open",
  "created_at": 0.0,
  "items": [
    {"id": "it_1", "title": "...", "status": "pending",
     "job_ids": ["j_c32e89fd"], "note": "", "updated_at": 0.0}
  ]
}
```

- Goal statuses: `open | done | abandoned`.
- Item statuses: `pending | delegated | review | merged | dropped`.
- Items are append-only (drop, never delete): the debrief needs the full record.
- `job_ids` accumulates every delegation attempt for the item (re-delegations
  are the interesting routing data).

## Guards (the contract's teeth)

- `create` validates the project key against factory.toml (CLI layer, same
  split as Factory.delegate: storage stays config-free).
- `update_item` refuses unknown item statuses and unknown items.
- `finish(done)` refuses while any item is `pending | delegated | review` —
  a goal cannot be "done" with work in flight. `abandoned` is always allowed;
  both write debrief.md when a debrief is given.
- mkdir is the exclusion primitive; every JSON write is temp + os.replace
  (job_store idioms, Windows retry included via shared helpers).

## CLI verbs (operator guarantee, like the rest of factory_cli)

```
goal new --project X --title T [--file brief.md]     -> {goal_id}
goal add <g_id> --title T                            -> {item_id}
goal item <g_id> <it_id> [--status S] [--job j_x] [--note "..."]
goal status <g_id>        # state + per-status item counts
goal show <g_id>          # the brief
goal done <g_id> [--debrief "..."]
goal abandon <g_id> [--debrief "..."]
goal list
```

## The manager loop (documented, not automated — v1)

1. `goal new` with the brief.
2. Decompose into backlog items (`goal add`), smallest-delegable first.
3. Per item: write spec tests, `delegate`, `goal item --status delegated --job j_x`.
4. On job result: review the diff (`result`, reviewer verdict is advisory),
   apply/merge by hand, `goal item --status merged` (or `dropped`, or
   re-delegate and append the new job id).
5. `goal done --debrief` when the acceptance criteria hold on the real project.

## Not in v1 (on purpose)

- No MCP tools for goals (the manager is a Claude session; CLI via Bash is
  enough). Add to factory_mcp when a scheduled manager exists.
- No automatic decomposition, no automatic review: those are the manager's
  judgment. v1 makes the judgment durable, not automatic.
- No cross-goal scheduling: one GPU, one job, the gpu_lock already arbitrates.

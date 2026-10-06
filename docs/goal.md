# /goal — running a goal through the factory

v1 (spec 2026-07-17): the hybrid-controller contract made durable. A manager —
a Claude session today — decomposes a brief into delegable items, drives the
ladder, reviews diffs, and the goal store keeps every step on disk so the loop
survives sessions, crashes and credit exhaustion.

## The loop

```
goal new --project conformergpd --title "Category scores in the UI" --file brief.md
                                   -> g_ab12cd34   (goals/g_ab12cd34/goal.md)

goal add g_ab12cd34 --title "analyzer: per-category scores"     -> it_1
goal add g_ab12cd34 --title "dashboard: render the breakdown"   -> it_2

# per item: write spec tests, delegate, record the job
delegate --project conformergpd --goal "..." --test spec.test.ts --target ...
goal item g_ab12cd34 it_1 --status delegated --job j_c32e89fd

# on result: review the diff, apply by hand, close the item
result j_c32e89fd --save-diff out.patch     # read it, then git apply
goal item g_ab12cd34 it_1 --status merged --note "13/13 + regression green"

# re-delegation appends to job_ids -- that history is the routing data
goal item g_ab12cd34 it_2 --status delegated --job j_9999aaaa

goal status g_ab12cd34    # state + item counts
goal done g_ab12cd34 --debrief "criteria hold on prod"   # refuses work in flight
```

## The brief (goal.md)

Three parts by convention — the harness stores it, the manager reads it:

```markdown
# <title>

## Objective
One paragraph: what "done" looks like, in product terms.

## Acceptance criteria
- [ ] Verifiable statements a reviewer can check on the real project.

## Constraints (optional)
Files off-limits, style, budgets.
```

## Rules

- The goal's `project` must be a factory.toml key: the goal perimeter is the
  delegation perimeter.
- Items are append-only (`dropped`, never deleted); `job_ids` accumulates every
  attempt. The debrief wants the full record.
- `goal done` refuses while any item is `pending`/`delegated`/`review`.
  `goal abandon` is always allowed. Both take `--debrief`.
- Storage: `goals/g_*/` — goal.md, state.json (atomic writes, job_store
  idioms), debrief.md. Design: docs/design/specs/2026-07-17-goal-v1-design.md.

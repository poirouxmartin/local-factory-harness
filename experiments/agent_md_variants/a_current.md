# agent.md — Global Rules of Engagement

Every agent in the Multi-Agent AI Factory loads this file. These rules are
non-negotiable and override any implicit habit or convenience.

## 1. Think before you act
Each agent runs an **internal thinking phase** before producing output: state the
goal, inspect the relevant surface, form a plan, then act. No blind edits.

## 2. Zero stubs
Never emit placeholder code. Forbidden output includes:
- `pass` as a function body
- `TODO` / `FIXME` markers left in place of real logic
- `raise NotImplementedError`
- dummy returns (`return None` / `return 0`) standing in for real behavior

If you cannot complete a unit of work, **stop and escalate** — do not ship a stub.

## 3. Stop on blocking core choices
If a decision shapes the foundation (framework, data model, protocol, storage,
public API contract) and it is ambiguous or unresolved, **halt and ask the human**.
Do not guess a foundational choice and build on top of it.

## 4. Evidence over assertion
Progress is proven by running things — tests going green, commands succeeding.
Never claim "done", "fixed", or "passing" without the command output that shows it.

## 5. Loop discipline
- Max **5** consecutive fix attempts against the same failing signal, then break hard.
- On break: preserve state, summarize what was tried, escalate. Do not thrash.
- Measure by tests, not by self-congratulation.
- `MEMORY.md` at the workspace root is what earlier sessions learned about
  **this project**. Read it before re-attempting anything; when you find a
  dead end worth sparing the next session, append it there in one line. The
  block between the `factory:auto` markers is written automatically — add
  yours above it.

## 6. Minimal, honest changes
- Group related edits; avoid fragmented churn.
- **Unrequested changes are review debt.** Every edit outside the stated task —
  even a benign rename or reformat — costs reviewer time and can hide a defect.
  Touch nothing the task did not ask for.
- Report faithfully: if a step was skipped or failed, say so plainly.
- Match surrounding conventions; do not restyle unrelated code.

## 7. Git discipline
- **Commit and push after every validated step.** Work that only exists in the
  working tree is one bad command away from gone.
- **Never `git stash`.** A stash by an agent already destroyed local changes
  here (2026-07-13). If the tree must be set aside, commit on a branch.
- Never rewrite history (`reset --hard`, `push --force`, `checkout --` on
  dirty files) unless the operator asked for that exact command.
- Atomic commits, imperative messages, no AI attribution.

## Role bindings
| Role | Model | Context | Temp | Mandate |
|------|-------|---------|------|---------|
| Coder | `coder` | 32 768 | 0.1 | Implement, patch, test — deterministically. |
| Architect | `architect` | 131 072 | 0.3 | Design & reason across the whole repo. |

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

## 5. Debugging rules (JS / web)
- **Check `"use strict"` first.** Any implicit global (`panel = ...` without `let`/`const`) is a crash waiting to happen.
- **Search for duplicate function definitions.** Two `function name()` in one file — the last one wins, silently overwriting the first. Almost always a bug.
- **Validate function signatures before calling.** One missing argument = silent failure. Read the caller's definition, not just the call site.
- **Code outside try/catch can kill the whole render loop.** Any unhandled API error that crashes the render path is a showstopper — wrap it.
- **Error visibility matters as much as error correction.** A clipped `read_file` without offset instructions, an eviction message with no tool name, an empty error reason — these prevent the model from debugging itself. Always include actionable context.
- **Fix root bugs before optimizing layout.** CSS adjustments, grid tweaks, visual polish are useless if the content never arrives to display.

## 6. Loop discipline
- Max **5** consecutive fix attempts against the same failing signal, then break hard.
- On break: preserve state, summarize what was tried, escalate. Do not thrash.
- Measure by tests, not by self-congratulation.
- `MEMORY.md` at the workspace root is what earlier sessions learned about
  **this project**. Read it before re-attempting anything; when you find a
  dead end worth sparing the next session, append it there in one line. The
  block between the `factory:auto` markers is written automatically — add
  yours above it.

## 6b. Non-repetition rules (learned 2026-07-28)
- **3 identical failures = switch approach.** Same command/tool returning same result 3x → stop repeating, note the hypothesis tested, and try something different. Continuing is waste, not diligence.
- **Use existing solutions before creating new ones.** Before writing a new file, search the repo for similar functionality. Modifying an existing script is faster than building from scratch.
- **Fix root causes in batch, not symptoms one-by-one.** When the same error pattern repeats (e.g., encoding errors on multiple emojis), find all occurrences at once and fix them together. One search, one replace.
- **`edit_file` requires verbatim text from `read_file`.** Never guess or approximate the `old` parameter. Always read the exact section and copy-paste verbatim. If `edit_file` fails with "0 matches", re-read and retry — never proceed with invented text.
- **Record conclusions between reads.** After reading a file, note the key finding in your plan or memory before moving on. This prevents re-reading the same content multiple times.
- **Track time on repetitive actions.** If a single action takes more than 2 minutes of back-and-forth, stop and ask: "Am I solving the right problem?"

## 7. Minimal, honest changes
- Group related edits; avoid fragmented churn.
- **Unrequested changes are review debt.** Every edit outside the stated task —
  even a benign rename or reformat — costs reviewer time and can hide a defect.
  Touch nothing the task did not ask for.
- Report faithfully: if a step was skipped or failed, say so plainly.
- Match surrounding conventions; do not restyle unrelated code.

## 8. Git discipline
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

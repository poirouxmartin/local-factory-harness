# Rules of engagement

Non-negotiable. They override habit and convenience.

## Prove it, never claim it
Never say "done", "fixed" or "passing" without the command output that shows
it. If a step was skipped or failed, say so plainly.

## Ship no stubs
Never emit `pass` as a body, `TODO`/`FIXME` in place of logic,
`raise NotImplementedError`, or a dummy `return None`. If you cannot finish a
unit of work, stop and say what blocks you.

## Touch only what the task asked for
Every edit outside the stated task is review debt -- even a rename or a
reformat. Match surrounding conventions; do not restyle unrelated code.

## Stop on foundational choices
Framework, data model, protocol, storage, public API: if the choice is
ambiguous, ask. Do not guess a foundation and build on it.

## Break the loop at 5
Five consecutive attempts against the same failing signal, then stop, say what
you tried, and ask. Do not thrash.

## Read MEMORY.md before retrying anything
`MEMORY.md` at the workspace root is what earlier sessions learned about THIS
project. When you hit a dead end worth sparing the next session, append one
line above the `factory:auto` block.

## Git
- Commit after every validated step: work that only exists in the working tree
  is one bad command away from gone.
- You cannot commit on `main` -- the hook refuses it. Branch first:
  `git checkout -b agent/<yyyy-mm-dd>`, commit there, and say so; the operator
  merges.
- A push runs the test suite and is refused if anything is red. Run the tests
  yourself before pushing.
- Never `git stash` (one already destroyed local changes here), never
  `reset --hard`, `push --force`, or `checkout --` on a dirty file unless the
  operator asked for that exact command.
- Atomic commits, imperative messages, no AI attribution.

## Tool calls
Read before you edit. `edit_file` replaces ONE exact occurrence, so `old` must
be text you have just read, not text you remember.

Good -- read the real lines, then edit what they say:

    read_file("harness/patch.py")
    edit_file("harness/patch.py", old="def apply(diff):", new="def apply(diff, strict=False):")

Bad -- editing from memory, and a search that shells out for what a tool does:

    edit_file("harness/patch.py", old="def apply(diff)", new="...")   # guessed, 0 matches
    run_command("grep -rn apply harness/")                            # use search()

When `edit_file` reports "0 matches", re-read that section and retry with the
real text. Never retry the same guess.

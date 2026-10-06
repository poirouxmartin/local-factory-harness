# Diff-Based Edits (SEARCH/REPLACE) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Large/multi-file jobs emit SEARCH/REPLACE edits instead of re-emitting whole files, and every job persists the diff of its best attempt.

**Architecture:** `patch.py` gains a parser (`extract_edits`) and a pure applier (`apply_edits`) beside the untouched whole-file path. `loop_job.py` routes each job to `"whole"` or `"diff"` mode (spec override, else the start-stage-3 rule: multi-target or >300 lines), swaps the prompt instruction block, and tracks a `best_attempt` snapshot captured at attempt time so the escalation reset can no longer discard it. `delegate` (MCP + CLI) grows an optional `edit_mode`.

**Tech Stack:** Python 3 stdlib only (re, difflib), pytest. Windows host.

Spec: `docs/design/specs/2026-07-17-diff-based-edits-design.md`.

## Global Constraints

- String formatting via `.format()`, never f-strings (codebase convention).
- Comments explain *why* / constraints, never restate the code.
- Conventional commits, imperative, ASCII, English, no AI attribution.
- Tests live in `harness/tests/`, run with `py -3 -m pytest harness -q` from `C:\Users\me\local-factory`.
- Exceptions raised toward the model must contain actionable instructions (they are fed back verbatim as retry feedback).
- The whole-file path (`extract_files`, single-file prompt) must not change behavior.

---

### Task 1: SEARCH/REPLACE parser (`extract_edits`)

**Files:**
- Modify: `harness/patch.py`
- Test: `harness/tests/test_patch.py`

**Interfaces:**
- Consumes: existing `_HEADER` regex, `is_unsafe_path`, `PatchError`/`NoCode`/`UnknownTarget` from `harness/patch.py`.
- Produces: `extract_edits(reply: str, target_files: list[str]) -> dict[str, list[tuple[str, str]]]` — ordered `(search, replace)` blocks per path. Raises `NoCode` (no blocks / headers missing with several targets), `UnknownTarget` (path not in the allowlist or unsafe).

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_patch.py`:

```python
# ---- extract_edits (SEARCH/REPLACE mode) ----

from patch import extract_edits


def sr(search, replace):
    return "<<<<<<< SEARCH\n{}=======\n{}>>>>>>> REPLACE".format(search, replace)


def test_extract_edits_single_target_needs_no_header():
    reply = sr("a = 1\n", "a = 2\n")
    assert extract_edits(reply, ["m.py"]) == {"m.py": [("a = 1\n", "a = 2\n")]}


def test_extract_edits_orders_blocks_per_file():
    reply = ("### FILE: m.py\n" + sr("a = 1\n", "a = 2\n") + "\n"
             + sr("b = 1\n", "b = 2\n") + "\n"
             "### FILE: n.py\n" + sr("c = 1\n", "c = 2\n"))
    assert extract_edits(reply, ["m.py", "n.py"]) == {
        "m.py": [("a = 1\n", "a = 2\n"), ("b = 1\n", "b = 2\n")],
        "n.py": [("c = 1\n", "c = 2\n")],
    }


def test_extract_edits_headers_required_for_several_targets():
    with pytest.raises(NoCode):
        extract_edits(sr("a\n", "b\n"), ["m.py", "n.py"])


def test_extract_edits_rejects_a_path_outside_the_targets():
    reply = "### FILE: evil.py\n" + sr("a\n", "b\n")
    with pytest.raises(UnknownTarget):
        extract_edits(reply, ["m.py"])


def test_extract_edits_tolerates_fences_and_crlf():
    reply = ("### FILE: m.py\n```python\n"
             + sr("a = 1\n", "a = 2\n") + "\n```").replace("\n", "\r\n")
    assert extract_edits(reply, ["m.py"]) == {"m.py": [("a = 1\n", "a = 2\n")]}


def test_extract_edits_without_blocks_is_nocode():
    with pytest.raises(NoCode):
        extract_edits("I would change the loop to be faster.", ["m.py"])


def test_extract_edits_empty_search_means_new_file():
    reply = "### FILE: m.py\n" + sr("", "print('new')\n")
    assert extract_edits(reply, ["m.py"]) == {"m.py": [("", "print('new')\n")]}
```

If `test_patch.py` does not already import `pytest` / `NoCode` / `UnknownTarget`, add the imports at the top rather than inline.

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_patch.py -q`
Expected: the new tests FAIL with `ImportError: cannot import name 'extract_edits'`.

- [ ] **Step 3: Implement the parser** — append to `harness/patch.py`:

```python
# ---- SEARCH/REPLACE edit mode (spec 2026-07-17) ----
#
# Whole-file re-emission starves the thinking budget on large targets
# (j_c32e89fd: ~7-9k output tokens of code per attempt; the 35b died
# mid-deliberation without emitting a line). Diff mode asks for anchored
# edits instead. Anchors are exact on purpose: the model must reason about
# a file state it can predict, and a failed anchor produces feedback it
# can act on.

_SR_BLOCK = re.compile(
    r"^<<<<<<<+ SEARCH\s*\n(.*?)^=======\s*\n(.*?)^>>>>>>>+ REPLACE\s*$",
    re.M | re.S)


def extract_edits(reply, target_files):
    reply = reply.replace("\r\n", "\n")
    sections = []
    headers = list(_HEADER.finditer(reply))
    if not headers:
        if len(target_files) != 1:
            raise NoCode("{} targets but no `### FILE:` headers to tell them "
                         "apart".format(len(target_files)))
        sections.append((target_files[0], reply))
    else:
        for i, m in enumerate(headers):
            end = headers[i + 1].start() if i + 1 < len(headers) else len(reply)
            path = m.group(1).strip().strip("`")
            if is_unsafe_path(path) or path not in target_files:
                raise UnknownTarget("model tried to edit {!r}, which is not a "
                                    "target".format(path))
            sections.append((path, reply[m.end():end]))
    edits = {}
    for path, body in sections:
        for search, replace in _SR_BLOCK.findall(body):
            edits.setdefault(path, []).append((search, replace))
    if not edits:
        raise NoCode("no SEARCH/REPLACE block in the reply; emit edits as\n"
                     "<<<<<<< SEARCH\\n<exact current lines>\\n=======\\n"
                     "<replacement>\\n>>>>>>> REPLACE")
    return edits
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_patch.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/patch.py harness/tests/test_patch.py
git commit -m "feat: parse SEARCH/REPLACE edit blocks from model replies"
```

---

### Task 2: Edit applier (`apply_edits`) with anchor economics

**Files:**
- Modify: `harness/patch.py`
- Test: `harness/tests/test_patch.py`

**Interfaces:**
- Consumes: `PatchError`, `NoCode` from Task 1's module state.
- Produces: `apply_edits(current: dict[str, str], edits: dict[str, list[tuple[str, str]]]) -> dict[str, str]` returning ONLY the edited paths with their full new text. New exceptions `AnchorNotFound(PatchError)`, `AmbiguousAnchor(PatchError)`, `BrokenResult(PatchError)`.

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_patch.py`:

```python
from patch import AmbiguousAnchor, AnchorNotFound, BrokenResult, apply_edits

CALC = "def add(a, b):\n    return a - b\n"


def test_apply_edits_replaces_the_anchored_span():
    out = apply_edits({"m.py": CALC},
                      {"m.py": [("    return a - b\n", "    return a + b\n")]})
    assert out == {"m.py": "def add(a, b):\n    return a + b\n"}


def test_apply_edits_applies_blocks_in_order():
    out = apply_edits({"m.py": "a = 1\nb = 1\n"},
                      {"m.py": [("a = 1\n", "a = 2\n"), ("b = 1\n", "b = a\n")]})
    assert out == {"m.py": "a = 2\nb = a\n"}


def test_apply_edits_missing_anchor_names_the_closest_region():
    with pytest.raises(AnchorNotFound) as e:
        apply_edits({"m.py": CALC}, {"m.py": [("    return a % b\n", "x\n")]})
    assert "return a - b" in str(e.value)  # the closest real line is shown
    assert "EXACTLY" in str(e.value)


def test_apply_edits_ambiguous_anchor_asks_for_more_context():
    text = "x = 1\ny = 2\nx = 1\n"
    with pytest.raises(AmbiguousAnchor) as e:
        apply_edits({"m.py": text}, {"m.py": [("x = 1\n", "x = 3\n")]})
    assert "2 times" in str(e.value)


def test_apply_edits_empty_search_only_creates_new_files():
    assert apply_edits({}, {"m.py": [("", "a = 1\n")]}) == {"m.py": "a = 1\n"}
    with pytest.raises(AnchorNotFound):
        apply_edits({"m.py": CALC}, {"m.py": [("", "a = 1\n")]})


def test_apply_edits_empty_replace_deletes_the_span():
    out = apply_edits({"m.py": "a = 1\nb = 2\n"}, {"m.py": [("a = 1\n", "")]})
    assert out == {"m.py": "b = 2\n"}


def test_apply_edits_tolerates_missing_final_newline_at_eof():
    out = apply_edits({"m.py": "a = 1\nb = 2"},
                      {"m.py": [("b = 2\n", "b = 3\n")]})
    assert out == {"m.py": "a = 1\nb = 3\n"}


def test_apply_edits_keeps_the_compile_gate():
    with pytest.raises(BrokenResult):
        apply_edits({"m.py": CALC},
                    {"m.py": [("def add(a, b):\n", "def add(a, b:\n")]})


def test_apply_edits_compile_gate_is_python_only():
    out = apply_edits({"m.js": "let a = (1;\n"}, {"m.js": [("(1;\n", "(1);\n")]})
    assert out == {"m.js": "let a = (1);\n"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_patch.py -q`
Expected: FAIL with `ImportError: cannot import name 'apply_edits'`.

- [ ] **Step 3: Implement the applier** — append to `harness/patch.py` (add `import difflib` at the top, beside `import re`):

```python
class AnchorNotFound(PatchError):
    pass


class AmbiguousAnchor(PatchError):
    pass


class BrokenResult(PatchError):
    pass


def _closest_region(text, search):
    """A few file lines around the best fuzzy match for the anchor's first
    line -- the feedback that turns 'not found' into a copy-paste fix."""
    probe = next((l for l in search.splitlines() if l.strip()), "").strip()
    lines = text.splitlines()
    if not probe or not lines:
        return ""
    best = max(range(len(lines)), key=lambda i: difflib.SequenceMatcher(
        None, probe, lines[i].strip()).ratio())
    return "\n".join(lines[max(0, best - 2):best + 3])


def apply_edits(current, edits):
    """All-or-nothing: any bad anchor raises before a single file is returned,
    so a retry always reasons about the state it was shown."""
    out = {}
    for path, blocks in edits.items():
        text = current.get(path, "")
        for search, replace in blocks:
            if search == "":
                if text.strip():
                    raise AnchorNotFound(
                        "an empty SEARCH creates a new file, but {!r} already "
                        "has content; copy the region you want to change "
                        "EXACTLY".format(path))
                text = replace
                continue
            n = text.count(search)
            if n == 0 and search.endswith("\n") and text.endswith(search[:-1]):
                # The file's last line has no trailing newline; the anchor is
                # still exact.
                text = text[:len(text) - len(search) + 1] + replace
                continue
            if n == 0:
                raise AnchorNotFound(
                    "SEARCH text not found in {!r}. Closest region:\n{}\n"
                    "Copy the region EXACTLY as it appears in the current "
                    "file.".format(path, _closest_region(text, search)))
            if n > 1:
                raise AmbiguousAnchor(
                    "SEARCH text appears {} times in {!r}; add surrounding "
                    "lines until it is unique".format(n, path))
            text = text.replace(search, replace, 1)
        if path.endswith(".py"):
            try:
                compile(text, path, "exec")
            except SyntaxError as e:
                raise BrokenResult(
                    "after applying your edits {!r} is not valid Python "
                    "({}); re-check the edits".format(path, e))
        out[path] = text
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_patch.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/patch.py harness/tests/test_patch.py
git commit -m "feat: apply SEARCH/REPLACE edits with exact-anchor economics"
```

---

### Task 3: Routing rule and diff-mode prompt

**Files:**
- Modify: `harness/loop_job.py` (`build_prompt` at ~line 89; add `resolve_edit_mode` beside it)
- Test: `harness/tests/test_loop_job.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `resolve_edit_mode(spec: dict, worktree) -> str` (`"whole"` or `"diff"`; `worktree` only needs `.path`); `build_prompt(goal, current, context, last_output, lang="python", edit_mode="whole")`; module constant `EDIT_MODE_LINE_THRESHOLD = 300`.

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_loop_job.py`:

```python
# ---- edit-mode routing and prompt (spec 2026-07-17) ----

import types

from loop_job import build_prompt, resolve_edit_mode


def wt(tmp_path):
    return types.SimpleNamespace(path=tmp_path)


def test_edit_mode_spec_override_wins(tmp_path):
    (tmp_path / "calc.py").write_text("x = 1\n")
    spec = {"target_files": ["calc.py"], "edit_mode": "diff"}
    assert resolve_edit_mode(spec, wt(tmp_path)) == "diff"
    many = {"target_files": ["a.py", "b.py"], "edit_mode": "whole"}
    assert resolve_edit_mode(many, wt(tmp_path)) == "whole"


def test_edit_mode_auto_multiple_targets_is_diff(tmp_path):
    spec = {"target_files": ["a.py", "b.py"]}
    assert resolve_edit_mode(spec, wt(tmp_path)) == "diff"


def test_edit_mode_auto_large_target_is_diff(tmp_path):
    (tmp_path / "big.py").write_text("x = 1\n" * 301)
    assert resolve_edit_mode({"target_files": ["big.py"]}, wt(tmp_path)) == "diff"
    (tmp_path / "small.py").write_text("x = 1\n" * 300)
    assert resolve_edit_mode({"target_files": ["small.py"]}, wt(tmp_path)) == "whole"


def test_edit_mode_auto_missing_single_target_is_whole(tmp_path):
    assert resolve_edit_mode({"target_files": ["new.py"]}, wt(tmp_path)) == "whole"


def test_build_prompt_diff_mode_swaps_the_instructions_only():
    current = {"calc.py": "def add(a, b):\n    return a - b"}
    p = build_prompt("fix add", current, {}, "", "python", edit_mode="diff")
    assert "<<<<<<< SEARCH" in p and ">>>>>>> REPLACE" in p
    assert "Do NOT rewrite whole files" in p
    assert "return a - b" in p            # current contents still shown
    assert "complete corrected contents" not in p


def test_build_prompt_whole_mode_is_unchanged():
    current = {"calc.py": "x = 1"}
    p = build_prompt("fix", current, {}, "", "python")
    assert "complete corrected contents" in p
    assert "SEARCH" not in p
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_loop_job.py -q -k "edit_mode or build_prompt"`
Expected: FAIL with `ImportError: cannot import name 'resolve_edit_mode'`.

- [ ] **Step 3: Implement** — in `harness/loop_job.py`, add below `fence_lang`:

```python
EDIT_MODE_LINE_THRESHOLD = 300


def resolve_edit_mode(spec, worktree):
    """Same threshold as the start-stage-3 routing rule (2026-07-17): one body
    of measured evidence, two knobs."""
    mode = spec.get("edit_mode")
    if mode in ("whole", "diff"):
        return mode
    targets = spec["target_files"]
    if len(targets) > 1:
        return "diff"
    for path in targets:
        p = worktree.path / path
        if p.exists() and len(p.read_text(encoding="utf-8").splitlines()) \
                > EDIT_MODE_LINE_THRESHOLD:
            return "diff"
    return "whole"
```

Change `build_prompt`'s signature to `def build_prompt(goal, current, context, last_output, lang="python", edit_mode="whole"):` and make the instruction block a three-way choice — the diff branch FIRST, the two existing branches unchanged under `elif len(current) == 1:` / `else:`:

```python
    if edit_mode == "diff":
        parts.append(
            "Edit these files: {}. Reply ONLY with edits in this exact format "
            "-- for each edit a `### FILE: <path>` line, then a block:\n"
            "<<<<<<< SEARCH\n"
            "<lines copied EXACTLY from the current file>\n"
            "=======\n"
            "<replacement lines>\n"
            ">>>>>>> REPLACE\n"
            "The SEARCH text must be unique in its file. Several blocks per "
            "file are allowed. To create a new file, use an empty SEARCH. "
            "Do NOT rewrite whole files. No explanation.".format(
                ", ".join(sorted(current))))
    elif len(current) == 1:
        ...  # existing single-file branch, verbatim
    else:
        ...  # existing multi-file branch, verbatim
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_loop_job.py -q`
Expected: all PASS (old prompt tests included — whole mode is untouched).

- [ ] **Step 5: Commit**

```bash
git add harness/loop_job.py harness/tests/test_loop_job.py
git commit -m "feat: edit-mode routing rule and SEARCH/REPLACE prompt"
```

---

### Task 4: Diff mode in the ladder

**Files:**
- Modify: `harness/loop_job.py` (`_run_ladder`, ~lines 145-330; import from `patch`)
- Test: `harness/tests/test_loop_job.py`

**Interfaces:**
- Consumes: `extract_edits`, `apply_edits` (Tasks 1-2), `resolve_edit_mode`, `build_prompt(..., edit_mode)` (Task 3).
- Produces: `_run_ladder`'s outcome dict gains `"edit_mode": str`. Anchor failures ride the existing `except PatchError` branch (reminder retry, `nocode_streak`, 2 in a row leaves the rung).

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_loop_job.py`:

```python
SR_GOOD = ("### FILE: calc.py\n"
           "<<<<<<< SEARCH\n    return a - b\n=======\n    return a + b\n"
           ">>>>>>> REPLACE\n")
SR_BAD_ANCHOR = ("### FILE: calc.py\n"
                 "<<<<<<< SEARCH\n    return a % b\n=======\n    return a + b\n"
                 ">>>>>>> REPLACE\n")


def test_diff_mode_edit_goes_green(store, config):
    chat = FakeModel({"A": SR_GOOD})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["status"] == "succeeded"
    assert result["edit_mode"] == "diff"
    assert "<<<<<<< SEARCH" in chat.prompts[0][1]


def test_whole_mode_jobs_report_their_mode_too(store, config):
    chat = FakeModel({"A": block(GOOD)})
    result = run(store, config, chat)
    assert result["status"] == "succeeded"
    assert result["edit_mode"] == "whole"


def test_an_anchor_miss_burns_a_reminder_not_an_attempt(store, config):
    chat = FakeModel({"A": [SR_BAD_ANCHOR, SR_GOOD]})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["status"] == "succeeded"
    assert result["attempts"][0]["result"] == "AnchorNotFound"
    assert result["attempts"][0]["passed"] is False
    # the feedback carries the closest region, ready to copy
    assert "SEARCH text not found" in chat.prompts[1][1]
    assert "return a - b" in chat.prompts[1][1]


def test_two_anchor_misses_running_leave_the_rung(store, config):
    chat = FakeModel({"A": SR_BAD_ANCHOR, "B": SR_GOOD})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["status"] == "succeeded"
    assert result["escalated"] is True
    assert [m for m, _ in chat.prompts] == ["A", "A", "B"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_loop_job.py -q -k "diff_mode or anchor or report_their_mode"`
Expected: FAIL — `result["edit_mode"]` KeyError first.

- [ ] **Step 3: Implement** — in `harness/loop_job.py`:

1. Extend the import at line 27 to `from patch import PatchError, apply_edits, extract_edits, extract_files`.
2. In `_run_ladder`, right after `lang = fence_lang(targets)`:

```python
    edit_mode = resolve_edit_mode(spec, worktree)
    store.append_log(job_id, "edit mode: {}\n".format(edit_mode))
```

3. Pass the mode into the prompt call (line ~183):

```python
            resp = chat_fn(model, system,
                           build_prompt(goal, current, context, last_output, lang,
                                        edit_mode=edit_mode),
                           temperature=stage.get("temperature", 0.1),
                           num_ctx=num_ctx, options=options)
```

4. Replace the single `files = extract_files(...)` line inside the `try:` (line ~197) with:

```python
                if edit_mode == "diff":
                    files = apply_edits(current, extract_edits(resp["content"], targets))
                else:
                    files = extract_files(resp["content"], targets)
```

(The `except PatchError` branch below it already does everything anchor
failures need: `rec["result"]` takes the exception class name, the reminder
feeds `str(e)` back, `nocode_streak` counts, two in a row leaves the rung.)

5. Add `"edit_mode": edit_mode` to the returned outcome dict (line ~325).

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_loop_job.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/loop_job.py harness/tests/test_loop_job.py
git commit -m "feat: SEARCH/REPLACE edit mode in the ladder"
```

---

### Task 5: Best-attempt diff persistence

**Files:**
- Modify: `harness/loop_job.py` (`_run_ladder`, `_cli`), `harness/factory_cli.py` (`_dispatch`, `result` branch at lines 136-141)
- Test: `harness/tests/test_loop_job.py`, `harness/tests/test_factory_cli.py`

**Interfaces:**
- Consumes: `worktree.capture_diff()`, `worktree.changed_paths()`, `forbidden_paths` (already imported in `loop_job.py`).
- Produces: outcome/result key `"best_attempt": None | {"attempt", "stage", "model", "tests_passed", "tests_failed", "failing", "diff"}`. `factory_cli result --save-diff` falls back to `best_attempt["diff"]` when the final `diff` is empty.

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_loop_job.py`:

```python
# Two test FUNCTIONS (not one with two asserts): partial credit must be
# countable for best_attempt to have something to rank.
TEST_SPLIT = ("from calc import add\n\n"
              "def test_two():\n    assert add(2, 2) == 4\n\n"
              "def test_six():\n    assert add(1, 5) == 6\n")
PARTIAL = "def add(a, b):\n    return 4\n"  # test_two only


def test_a_failed_job_keeps_its_best_attempt_diff(store, config):
    # A scores 1/2 on both attempts; B never emits code. Without persistence
    # the escalation reset leaves an empty final diff and nothing else --
    # exactly the j_c32e89fd 6/8 loss.
    chat = FakeModel({"A": block(PARTIAL), "B": "no code here, sorry"})
    spec = dict(SPEC, tests={"test_calc.py": TEST_SPLIT})
    result = run(store, config, chat, spec=spec)
    assert result["status"] == "failed"
    assert result["diff"] == ""
    best = result["best_attempt"]
    assert best["model"] == "A" and best["tests_passed"] == 1
    assert "return 4" in best["diff"]
    assert best["failing"] == ["test_six"]


def test_best_attempt_never_downgrades(store, config):
    # A: 1/2 then 0/2 -- the second, worse attempt must not replace the first.
    chat = FakeModel({"A": [block(PARTIAL), block(STILL_BROKEN)],
                      "B": "still no code"})
    spec = dict(SPEC, tests={"test_calc.py": TEST_SPLIT})
    result = run(store, config, chat, spec=spec)
    assert result["status"] == "failed"
    assert result["best_attempt"]["tests_passed"] == 1
    assert "return 4" in result["best_attempt"]["diff"]
```

And to `harness/tests/test_factory_cli.py`:

```python
def test_save_diff_falls_back_to_the_best_attempt(tmp_path):
    class FailedJob:
        def job_result(self, job_id):
            return {"ready": True, "status": "failed", "diff": "",
                    "best_attempt": {"tests_passed": 6, "diff": "BEST\n"}}

    out = io.StringIO()
    rc = main(["result", "j_x", "--save-diff", str(tmp_path / "o.patch")],
              factory=FailedJob(), out=out, err=io.StringIO())
    assert rc == 0
    assert (tmp_path / "o.patch").read_text() == "BEST\n"
```

(`test_factory_cli.py` already drives `main(argv, factory=..., out=..., err=...)`; reuse its existing imports — add `import io` only if missing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_loop_job.py harness/tests/test_factory_cli.py -q -k "best_attempt or save_diff_falls"`
Expected: FAIL — `result["best_attempt"]` KeyError; CLI writes "".

- [ ] **Step 3: Implement**

In `_run_ladder`, initialize beside `attempts` (line ~158): `best = None`.
Insert just BEFORE the `green = result.passed and ...` line (~275) — after the
judge-tamper branch has had its chance to `break`, so a tampered tree can
never become the keepsake:

```python
            if result.returncode in (0, 1) and \
                    (best is None or result.tests_passed > best["tests_passed"]) and \
                    not forbidden_paths(worktree.changed_paths()):
                # Captured at attempt time: the escalation reset() can no
                # longer throw away a 6/8 (bottlenecks doc, item 3).
                best = {"attempt": rec["attempt"], "stage": si + 1, "model": model,
                        "tests_passed": result.tests_passed,
                        "tests_failed": result.tests_failed, "failing": failing,
                        "diff": worktree.capture_diff()}
```

Add `"best_attempt": best` to the outcome dict (same return as Task 4's step).

In `_cli` (loop_job.py line ~435), keep the diff out of the console for the
nested record too:

```python
    shown = {k: v for k, v in result.items() if k != "diff"}
    if shown.get("best_attempt"):
        shown["best_attempt"] = {k: v for k, v in shown["best_attempt"].items()
                                 if k != "diff"}
    print(json.dumps(shown, indent=2))
```

In `factory_cli.py` `_dispatch`, the `result` branch becomes:

```python
    if a.command == "result":
        result = factory.job_result(a.job_id)
        if a.save_diff and result.get("ready"):
            diff = result.get("diff") or \
                (result.get("best_attempt") or {}).get("diff", "")
            Path(a.save_diff).write_text(diff, encoding="utf-8")
            result["diff_saved_to"] = a.save_diff
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_loop_job.py harness/tests/test_factory_cli.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/loop_job.py harness/factory_cli.py harness/tests/test_loop_job.py harness/tests/test_factory_cli.py
git commit -m "feat: persist the best attempt's diff on every job"
```

---

### Task 6: `edit_mode` through delegate (MCP + CLI)

**Files:**
- Modify: `harness/factory_mcp.py` (TOOLS schema ~line 63, `Factory.delegate` ~line 363), `harness/factory_cli.py` (parser ~line 49, `_dispatch` ~line 131)
- Test: `harness/tests/test_factory_mcp.py`, `harness/tests/test_factory_cli.py`

**Interfaces:**
- Consumes: spec key `"edit_mode"` read by `resolve_edit_mode` (Task 3).
- Produces: `Factory.delegate(..., start_stage=None, edit_mode=None)`; CLI flag `--edit-mode {whole,diff}` on `delegate`.

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_factory_mcp.py`, mirroring the `start_stage` tests at lines 190-207 (same `delegate`/`payload` helpers):

```python
def test_delegate_validates_edit_mode(factory):
    result = delegate(factory, edit_mode="patch")
    assert result["isError"]
    assert "edit_mode" in result["content"][0]["text"]


def test_delegate_stores_edit_mode(factory):
    job_id = payload(delegate(factory, edit_mode="diff"))["job_id"]
    spec = JobStore(factory.jobs_root).read_spec(job_id)
    assert spec["edit_mode"] == "diff"


def test_delegate_without_edit_mode_omits_it_from_the_spec(factory):
    job_id = payload(delegate(factory))["job_id"]
    assert "edit_mode" not in JobStore(factory.jobs_root).read_spec(job_id)
```

Before running, open `harness/tests/test_factory_mcp.py:190-207` and copy the
EXACT assertion idiom of `test_delegate_validates_start_stage` for the error
case — if it checks something other than `result["isError"]`, follow it.

And to `harness/tests/test_factory_cli.py`, next to its `delegate` helper (line ~53):

```python
def test_cli_delegate_passes_edit_mode(factory, tmp_path):
    result = delegate(factory, tmp_path, "--edit-mode", "diff")
    job_id = result["job_id"]
    spec = JobStore(factory.jobs_root).read_spec(job_id)
    assert spec["edit_mode"] == "diff"
```

(Adapt the call shape to the file's `delegate(factory, tmp_path, *extra)` helper — pass the flag through `*extra`, and read the job id the way the neighbouring tests do.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_factory_mcp.py harness/tests/test_factory_cli.py -q -k edit_mode`
Expected: FAIL — unexpected keyword / unrecognized argument.

- [ ] **Step 3: Implement**

`factory_mcp.py` TOOLS schema, after the `start_stage` property:

```python
                "edit_mode": {"type": "string", "enum": ["whole", "diff"],
                              "description": "How the model writes: whole-file "
                              "re-emission or SEARCH/REPLACE edits. Omit for "
                              "the auto rule (multi-file or >300 lines = diff)."},
```

`Factory.delegate` (line 363):

```python
    def delegate(self, project, goal, tests, target_files, context_files=None,
                 start_stage=None, edit_mode=None):
        ...
        if edit_mode is not None and edit_mode not in ("whole", "diff"):
            raise ToolError("edit_mode must be 'whole' or 'diff'")
        ...
        if edit_mode is not None:
            spec["edit_mode"] = edit_mode
```

(the two new lines sit right after the `start_stage` validation and the
`spec["start_stage"]` assignment respectively; everything else is verbatim.)

`factory_cli.py` — parser, after the `--context` argument (line ~49):

```python
    d.add_argument("--edit-mode", choices=("whole", "diff"), dest="edit_mode",
                   help="force whole-file or SEARCH/REPLACE output; "
                        "default: auto rule")
```

and the `delegate` branch of `_dispatch` (line 131):

```python
        return factory.delegate(a.project, a.goal, tests, a.targets, a.contexts,
                                edit_mode=a.edit_mode)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_factory_mcp.py harness/tests/test_factory_cli.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/factory_cli.py harness/tests/test_factory_mcp.py harness/tests/test_factory_cli.py
git commit -m "feat: edit_mode option on delegate (MCP and CLI)"
```

---

### Task 7: Docs, ADR-017, full suite

**Files:**
- Modify: `DECISIONS.md`, `docs/delegation.md`
- No new tests; the gate is the full suite.

**Interfaces:** none — documentation and verification only.

- [ ] **Step 1: ADR** — append to `DECISIONS.md`:

```markdown
## ADR-017 — Large jobs edit by SEARCH/REPLACE, and every job keeps its best diff

Whole-file re-emission starved the thinking budget on multi-file jobs:
j_c32e89fd re-emitted ~7-9k tokens of code per attempt and the 35b died
mid-deliberation (`done_reason: length`) without one line of code. Jobs that
are multi-target or touch a >300-line file (the same threshold as the
start-stage-3 routing rule) now ask for aider-style SEARCH/REPLACE blocks.
Unified diffs were rejected: line-number arithmetic is exactly what <=35b
local models fail, and a rejected hunk is opaque feedback. Anchors are exact
and must be unique; a miss costs a reminder retry (NoCode economics) and
feeds back the closest region. Application is all-or-nothing per attempt and
the compile() gate runs on the result. Separately, the best-scoring attempt's
diff is captured at attempt time into `best_attempt`, so an escalation reset
can no longer throw away a 6/8 -- a failed job now leaves its best work
behind. `edit_mode` in the spec overrides the auto rule.
```

- [ ] **Step 2: delegation.md** — two edits:

In the `delegate` bullet of `## Tools`, after the `context_files` sentence, add:

```markdown
  `edit_mode` (`"whole"|"diff"`, optional) forces the output format; without it
  a multi-file job or a >300-line target gets SEARCH/REPLACE edit mode
  (ADR-017), small single-file jobs keep whole-file re-emission.
```

In the `job_result` bullet, after the `base_sha` sentence, add:

```markdown
  `best_attempt` carries the best-scoring attempt (model, tests passed, its
  diff) even when the job failed -- `factory_cli.py result --save-diff` falls
  back to it when the final diff is empty.
```

- [ ] **Step 3: Run the full suite**

Run: `py -3 -m pytest harness -q`
Expected: all PASS (519 pre-existing + the new ones), no skips introduced.

- [ ] **Step 4: Commit**

```bash
git add DECISIONS.md docs/delegation.md
git commit -m "docs: ADR-017 SEARCH/REPLACE edit mode and best-attempt diff"
```

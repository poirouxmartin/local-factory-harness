# Project Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the agent a generated, always-true map of the project in its system prompt, carrying per-file annotations that cannot assert anything the repository does not evidence.

**Architecture:** A new pure module `harness/project_map.py` (no I/O) builds and renders the map; `factory_mcp` owns the git calls, the file reads and `MEMORY.md`. The rendered block joins `_lesson_block` and `_memory_block` in the per-session system-prompt snapshot. Annotations are produced by one bounded LLM call at step/session end, verified against a vocabulary harvested from the git-tracked sources, and persisted in a third marked block of `MEMORY.md`.

**Tech Stack:** Python 3.12 stdlib only. pytest for tests. `git ls-files -s` for scope and blob shas.

## Global Constraints

- **Stdlib only.** No new dependency, anywhere. The whole harness is stdlib (`harness/factory_web.py:1`).
- **`project_map.py` is pure.** No `open()`, no `subprocess`, no `Path.read_text()`. All I/O lives in `factory_mcp`. Same rule as `lessons.py`, `workspace_memory.py`, `chat_context.py`.
- **Deterministic rendering.** Same inputs → byte-identical output. The system prompt is the KV prefix under `--cache-reuse` (locked 2026-07-19); a byte that moves costs ~1 s per turn.
- **`MAX_MAP_CHARS = 21000`**, **`MAX_NOTE_CHARS = 160`**, **`MAX_NOTES_PER_STEP = 8`**. Exact values from the spec.
- **Above the markers in `MEMORY.md` is the operator's territory.** The harness reads it and never rewrites it.
- **Commits:** conventional, imperative, English, ASCII only, no AI attribution. Work happens on the current `agent/*` branch; `.githooks` refuses `main`.
- **Never an unbounded filesystem walk.** Depth and count caps on every fallback path. This is the bug being removed elsewhere in the same pass.

Reference spec: `docs/design/specs/2026-07-27-project-map-design.md`

---

### Task 1: `skeleton()` — deterministic index lines

**Files:**
- Create: `harness/project_map.py`
- Test: `harness/tests/test_project_map.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `skeleton(entries) -> list[str]`. `entries` is `[{"path": str, "lines": int, "doc": str, "blob": str}]`. Returns one line per entry, `"<path> (<lines>L) <doc>"`, sorted by path, `doc` clipped to 88 chars. An empty `doc` yields `"<path> (<lines>L)"` with no trailing space.

- [ ] **Step 1: Write the failing test**

```python
# harness/tests/test_project_map.py
"""The project map: a generated index the agent cannot contradict."""
import project_map


def _e(path, lines=10, doc="", blob="aaa"):
    return {"path": path, "lines": lines, "doc": doc, "blob": blob}


def test_skeleton_is_one_line_per_entry_sorted_by_path():
    out = project_map.skeleton([
        _e("harness/zeta.py", 3, "Last."),
        _e("harness/alpha.py", 120, "First."),
    ])
    assert out == ["harness/alpha.py (120L) First.",
                   "harness/zeta.py (3L) Last."]


def test_skeleton_omits_trailing_space_when_doc_is_empty():
    assert project_map.skeleton([_e("a.py", 5)]) == ["a.py (5L)"]


def test_skeleton_clips_long_docs():
    line = project_map.skeleton([_e("a.py", 5, "x" * 200)])[0]
    assert line == "a.py (5L) " + "x" * 88


def test_skeleton_is_deterministic():
    entries = [_e("b.py", 2, "B"), _e("a.py", 1, "A")]
    assert project_map.skeleton(entries) == project_map.skeleton(
        list(reversed(entries)))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest harness/tests/test_project_map.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'project_map'`

- [ ] **Step 3: Write minimal implementation**

```python
# harness/project_map.py
"""A map of one workspace: where things are, and what the agent learned.

Generated content states WHERE (from `git ls-files`, which is the only
authority on what belongs to a project); annotations state WHY. An annotation
can never contradict the generated line it hangs under, and one whose anchor
file changed renders as stale rather than lying.

Why this exists: audited over the 256 stored sessions (2026-07-27), 17% of all
tool calls were byte-identical replays -- one session read `harness/web/app.js`
29 times, a 56 KB file that is ~30% of the chat window, so every read crossed
the compaction threshold that then evicted it. See
docs/design/specs/2026-07-27-project-map-design.md.

Pure functions over plain dicts, no I/O: `factory_mcp` owns git, the files and
MEMORY.md. The rendered block goes into the agent system prompt, which is the
KV prefix (`--cache-reuse`, locked 2026-07-19), so rendering is deterministic
and bounded: same entries in, same bytes out.
"""

MAX_MAP_CHARS = 21000
MAX_NOTE_CHARS = 160
MAX_NOTES_PER_STEP = 8
DOC_CHARS = 88


def skeleton(entries):
    """One `path (NL) doc` line per entry, sorted by path."""
    out = []
    for e in sorted(entries, key=lambda x: x["path"]):
        line = "{} ({}L)".format(e["path"], e["lines"])
        doc = (e.get("doc") or "").strip()[:DOC_CHARS]
        if doc:
            line += " " + doc
        out.append(line)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest harness/tests/test_project_map.py -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add harness/project_map.py harness/tests/test_project_map.py
git commit -m "feat(project_map): deterministic skeleton lines from git entries"
```

---

### Task 2: `stale()` and `render()` — the prompt block

**Files:**
- Modify: `harness/project_map.py`
- Test: `harness/tests/test_project_map.py`

**Interfaces:**
- Consumes: `skeleton(entries)` from Task 1.
- Produces:
  - `stale(notes, blobs) -> list[dict]` — `blobs` is `{path: sha}`. Returns a new list, each note copied with `"stale": bool` set from `note["blob"] != blobs.get(note["path"])`. A note whose path is absent from `blobs` is stale. Never drops a note.
  - `render(entries, notes, budget=MAX_MAP_CHARS) -> str` — the full block, or `""` when `entries` is empty. Annotated files carry their note on the following line, indented four spaces and prefixed `^ `. A stale note is suffixed ` (périmé)`. Over budget, degrades to directory level.
  - Module constants `HEADER = "--- Project map (git ls-files) ---"`, `FOOTER = "--- End project map ---"`.

- [ ] **Step 1: Write the failing test**

```python
def _n(path, text="note", blob="aaa"):
    return {"path": path, "text": text, "blob": blob, "last_seen": 0.0}


def test_stale_flags_notes_whose_anchor_blob_moved():
    out = project_map.stale([_n("a.py", blob="old")], {"a.py": "new"})
    assert out[0]["stale"] is True
    assert out[0]["path"] == "a.py"          # never dropped


def test_stale_flags_notes_whose_file_is_gone():
    assert project_map.stale([_n("gone.py")], {})[0]["stale"] is True


def test_stale_leaves_matching_notes_fresh():
    assert project_map.stale([_n("a.py", blob="x")], {"a.py": "x"})[0][
        "stale"] is False


def test_render_hangs_the_note_under_its_file():
    out = project_map.render([_e("a.py", 5, "Doc.")],
                             [_n("a.py", "reprojeté à chaque tour")])
    assert "a.py (5L) Doc.\n    ^ reprojeté à chaque tour" in out
    assert out.startswith(project_map.HEADER)
    assert out.rstrip().endswith(project_map.FOOTER)


def test_render_marks_a_stale_note():
    out = project_map.render([_e("a.py", 5)],
                             [dict(_n("a.py", "vieux"), stale=True)])
    assert "^ vieux (périmé)" in out


def test_render_is_empty_without_entries():
    assert project_map.render([], []) == ""


def test_render_is_deterministic():
    entries = [_e("b.py"), _e("a.py")]
    notes = [_n("b.py", "B"), _n("a.py", "A")]
    assert project_map.render(entries, notes) == project_map.render(
        list(reversed(entries)), list(reversed(notes)))


def test_render_degrades_to_directories_instead_of_truncating():
    entries = [_e("pkg/mod{}.py".format(i), 10, "x" * 80) for i in range(400)]
    out = project_map.render(entries, [], budget=2000)
    assert len(out) <= 2000
    assert "pkg/ (400 files)" in out
    # the last-sorting file must not be the one silently dropped
    assert "mod399" not in out and "mod0" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest harness/tests/test_project_map.py -v`
Expected: FAIL — `AttributeError: module 'project_map' has no attribute 'stale'`

- [ ] **Step 3: Write minimal implementation**

```python
HEADER = "--- Project map (git ls-files) ---"
FOOTER = "--- End project map ---"
STALE_MARK = " (périmé)"


def stale(notes, blobs):
    """Copy of `notes` with `stale` set. A note is never dropped: one the
    agent can no longer trust is information; one that vanishes is not."""
    return [dict(n, stale=n.get("blob") != blobs.get(n["path"]))
            for n in notes]


def _directory_lines(entries):
    """The degraded view: one line per directory, file count and total size."""
    dirs = {}
    for e in entries:
        d = e["path"].rsplit("/", 1)[0] + "/" if "/" in e["path"] else "./"
        n, total = dirs.get(d, (0, 0))
        dirs[d] = (n + 1, total + e["lines"])
    return ["{} ({} files, {}L)".format(d, n, total)
            for d, (n, total) in sorted(dirs.items())]


def render(entries, notes, budget=MAX_MAP_CHARS):
    """The prompt block. Deterministic and bounded: this is the KV prefix."""
    if not entries:
        return ""
    by_path = {}
    for n in sorted(notes, key=lambda x: x["path"]):
        by_path[n["path"]] = n
    lines = []
    for e, line in zip(sorted(entries, key=lambda x: x["path"]),
                       skeleton(entries)):
        lines.append(line)
        note = by_path.get(e["path"])
        if note:
            text = (note.get("text") or "").strip()[:MAX_NOTE_CHARS]
            if note.get("stale"):
                text += STALE_MARK
            lines.append("    ^ " + text)
    block = "\n".join([HEADER] + lines + [FOOTER])
    if len(block) <= budget:
        return block
    # Degrade whole, never truncate: a cut list silently hides whatever
    # sorts last, and the agent cannot tell the difference.
    return "\n".join([HEADER] + _directory_lines(entries) + [FOOTER])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest harness/tests/test_project_map.py -v`
Expected: PASS, 12 passed

- [ ] **Step 5: Commit**

```bash
git add harness/project_map.py harness/tests/test_project_map.py
git commit -m "feat(project_map): render the block, mark stale notes, degrade over budget"
```

---

### Task 3: `vocabulary()` and `verify()` — the FastAPI gate

**Files:**
- Modify: `harness/project_map.py`
- Test: `harness/tests/test_project_map.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `vocabulary(sources) -> set[str]` — `sources` is `{path: text}`. Returns every token of shape `[A-Za-z_][A-Za-z0-9_]*` found in the texts, plus every path and every path segment of the keys. Built once per session and reused.
  - `verify(note_text, vocab) -> str | None` — returns `None` when the note asserts nothing unsupported, otherwise the first offending token. Checked token shapes, per the spec: anything inside backticks; identifiers (`snake_case`, `CamelCase`, or containing `.` or `/`); capitalised words that are not the first word of a sentence.

- [ ] **Step 1: Write the failing test**

```python
SOURCES = {
    "harness/factory_web.py": (
        '"""Web UI server: stdlib only."""\n'
        "from http.server import BaseHTTPRequestHandler\n"),
    "harness/chat_context.py": "def build_prompt(system_msgs):\n    pass\n",
}


def test_vocabulary_holds_identifiers_and_paths():
    vocab = project_map.vocabulary(SOURCES)
    assert "BaseHTTPRequestHandler" in vocab
    assert "build_prompt" in vocab
    assert "harness/factory_web.py" in vocab
    assert "factory_web" in vocab


def test_verify_rejects_the_fastapi_regression():
    # The real MEMORY.md line, written by the agent on 2026-07-25 and shipped
    # in every system prompt since. FastAPI appears nowhere in the repo.
    vocab = project_map.vocabulary(SOURCES)
    assert project_map.verify(
        "Backend : FastAPI (`harness/factory_web.py`), port 8787",
        vocab) == "FastAPI"


def test_verify_accepts_a_note_backed_by_the_sources():
    vocab = project_map.vocabulary(SOURCES)
    assert project_map.verify(
        "stdlib pur, `BaseHTTPRequestHandler`, pas de framework",
        vocab) is None


def test_verify_rejects_an_invented_path():
    vocab = project_map.vocabulary(SOURCES)
    assert project_map.verify("voir `harness/router.py`",
                              vocab) == "harness/router.py"


def test_verify_ignores_ordinary_prose_and_sentence_openers():
    vocab = project_map.vocabulary(SOURCES)
    # "Le" opens the sentence, "serveur" is lowercase prose, 8787 is a number
    assert project_map.verify("Le serveur écoute sur 8787.", vocab) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest harness/tests/test_project_map.py -v`
Expected: FAIL — `AttributeError: module 'project_map' has no attribute 'vocabulary'`

- [ ] **Step 3: Write minimal implementation**

```python
import re

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BACKTICKED_RE = re.compile(r"`([^`]+)`")
_WORD_RE = re.compile(r"[^\s,;:()\[\]]+")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_./-]*$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def vocabulary(sources):
    """Every name the repository evidences: identifiers, paths, path parts."""
    vocab = set()
    for path, text in sources.items():
        vocab.add(path)
        for part in path.replace("/", " ").replace(".", " ").split():
            vocab.add(part)
        vocab.update(_TOKEN_RE.findall(text))
    return vocab


def _is_checkable(word):
    """A word that asserts something the repo can confirm or deny."""
    if not _IDENTIFIER_RE.match(word):
        return False
    return ("_" in word or "/" in word or "." in word
            or word != word.lower())


def verify(note_text, vocab):
    """The first token the repository cannot evidence, or None.

    Catches invented names, which is the observed failure. It does NOT catch a
    false causal claim built from real identifiers -- nothing mechanical would,
    and pretending otherwise repeats the mistake of trusting the prose.
    """
    for quoted in _BACKTICKED_RE.findall(note_text):
        q = quoted.strip()
        if q and q not in vocab and not _supported(q, vocab):
            return q
    stripped = _BACKTICKED_RE.sub(" ", note_text)
    for sentence in _SENTENCE_SPLIT_RE.split(stripped):
        for i, word in enumerate(_WORD_RE.findall(sentence)):
            word = word.strip(".,;:!?")
            if i == 0 and word[:1].isupper() and "_" not in word \
                    and "/" not in word and "." not in word:
                continue          # sentence opener, capitalised by grammar
            if _is_checkable(word) and not _supported(word, vocab):
                return word
    return None


def _supported(word, vocab):
    """True when the repo evidences this word, whole or by its parts."""
    if word in vocab:
        return True
    parts = [p for p in re.split(r"[./-]", word) if p]
    return bool(parts) and all(p in vocab for p in parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest harness/tests/test_project_map.py -v`
Expected: PASS, 17 passed

- [ ] **Step 5: Commit**

```bash
git add harness/project_map.py harness/tests/test_project_map.py
git commit -m "feat(project_map): reject annotations the repository cannot evidence"
```

---

### Task 4: `MEMORY.md` round-trip for the `factory:map` block

**Files:**
- Modify: `harness/project_map.py`
- Test: `harness/tests/test_project_map.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `parse(text) -> list[dict]` — notes read from the `<!-- factory:map -->` block, each `{"path", "text", "blob", "last_seen"}`. Unparseable lines are skipped, never fatal.
  - `update(text, notes) -> str` — `text` with the block replaced (appended when absent). Everything outside the markers is returned byte-for-byte.
  - Constants `BEGIN = "<!-- factory:map -->"`, `END = "<!-- /factory:map -->"`.
- Line format: `` - `<path>` @<blob> : <text> ``

- [ ] **Step 1: Write the failing test**

```python
MEMORY_WITH_MAP = """# MEMORY

Human prose the harness must never touch.

<!-- factory:map -->
- `harness/chat_context.py` @a1b2c3d : reprojeté à chaque tour
- `harness/factory_web.py` @9f4e21c : stdlib pur
<!-- /factory:map -->
"""


def test_parse_reads_notes_from_the_block():
    notes = project_map.parse(MEMORY_WITH_MAP)
    assert [n["path"] for n in notes] == ["harness/chat_context.py",
                                          "harness/factory_web.py"]
    assert notes[0]["blob"] == "a1b2c3d"
    assert notes[0]["text"] == "reprojeté à chaque tour"


def test_parse_skips_unparseable_lines_without_raising():
    text = ("<!-- factory:map -->\ngarbage\n"
            "- `a.py` @xyz : ok\n<!-- /factory:map -->\n")
    assert [n["path"] for n in project_map.parse(text)] == ["a.py"]


def test_parse_returns_empty_without_a_block():
    assert project_map.parse("# MEMORY\n\nnothing here\n") == []


def test_update_preserves_everything_outside_the_markers():
    out = project_map.update(MEMORY_WITH_MAP, [
        {"path": "a.py", "text": "neuf", "blob": "ff0", "last_seen": 1.0}])
    assert out.startswith("# MEMORY\n\nHuman prose the harness must never "
                          "touch.\n")
    assert "- `a.py` @ff0 : neuf" in out
    assert "chat_context" not in out


def test_update_appends_the_block_when_absent():
    out = project_map.update("# MEMORY\n", [
        {"path": "a.py", "text": "neuf", "blob": "ff0", "last_seen": 1.0}])
    assert out.startswith("# MEMORY\n")
    assert project_map.BEGIN in out and project_map.END in out


def test_round_trip_is_lossless():
    notes = project_map.parse(MEMORY_WITH_MAP)
    assert project_map.parse(project_map.update("", notes)) == notes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest harness/tests/test_project_map.py -v`
Expected: FAIL — `AttributeError: module 'project_map' has no attribute 'parse'`

- [ ] **Step 3: Write minimal implementation**

Mirror `workspace_memory._body` / `_replace_block` (lines 249-268) rather than inventing a second marker discipline.

```python
BEGIN = "<!-- factory:map -->"
END = "<!-- /factory:map -->"
PREAMBLE = ("Written by the agent, checked against the repository. "
            "Edit above this marker; this block is regenerated.")

_NOTE_RE = re.compile(r"^- `(?P<path>[^`]+)` @(?P<blob>\S+) : (?P<text>.*)$")


def parse(text):
    """Notes from the map block. Unreadable lines are skipped, never fatal."""
    start = text.find(BEGIN)
    end = text.find(END)
    if start == -1 or end == -1 or end < start:
        return []
    notes = []
    for line in text[start + len(BEGIN):end].splitlines():
        m = _NOTE_RE.match(line.strip())
        if m:
            notes.append({"path": m.group("path"), "blob": m.group("blob"),
                          "text": m.group("text"), "last_seen": 0.0})
    return notes


def update(text, notes):
    """`text` with the map block replaced. Outside the markers is untouched:
    above them is the operator's file."""
    lines = [BEGIN, PREAMBLE, ""]
    for n in sorted(notes, key=lambda x: x["path"]):
        lines.append("- `{}` @{} : {}".format(
            n["path"], n["blob"], (n["text"] or "").strip()[:MAX_NOTE_CHARS]))
    lines.append(END)
    block = "\n".join(lines)
    start, end = text.find(BEGIN), text.find(END)
    if start == -1 or end == -1 or end < start:
        sep = "" if text.endswith("\n") or not text else "\n"
        return text + sep + "\n" + block + "\n"
    return text[:start] + block + text[end + len(END):]
```

Note on `test_round_trip_is_lossless`: `parse` sets `last_seen` to `0.0` and `update` does not persist it, so the round trip compares equal. `last_seen` exists for the merge policy in Task 6, not for the file.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest harness/tests/test_project_map.py -v`
Expected: PASS, 23 passed

- [ ] **Step 5: Commit**

```bash
git add harness/project_map.py harness/tests/test_project_map.py
git commit -m "feat(project_map): round-trip annotations through the MEMORY.md map block"
```

---

### Task 5: Wire the map into the system prompt

**Files:**
- Modify: `harness/factory_mcp.py` (add `_git_entries`, `_walk_entries`, `_map_block`; extend `_agent_system_head` at line 1285-1289; extend `Factory.__init__` near line 481)
- Test: `harness/tests/test_factory_mcp.py` (append)

**Why the tests go in `test_factory_mcp.py`:** the fixtures these tests need already live there — `project` (line 39, a real git repo holding `calc.py` and `elo.py`), `factory` (line 97), `git()` (line 35) and `_inject_runtime_stub` (line 86). A new file would either duplicate them or force a `conftest.py` move that this plan does not need.

**Interfaces:**
- Consumes: `project_map.render`, `project_map.parse`, `project_map.stale` from Tasks 2 and 4.
- Produces:
  - `_git_entries(workspace) -> (entries, sources, blobs) | None` — module-level function in `factory_mcp`, `None` when the workspace is not a git repo or git is unavailable.
  - `_walk_entries(workspace) -> (entries, sources, blobs)` — bounded fallback: `MAX_WALK_FILES = 2000`, `MAX_WALK_DEPTH = 6`.
  - `Factory._map_block(session_id, workspace) -> str`, snapshotted in `self._map_blocks`, exactly like `_lesson_blocks` and `_memory_blocks`.

- [ ] **Step 1: Write the failing test**

Append to `harness/tests/test_factory_mcp.py`. Add `import project_map` to that file's imports.

```python
# ---- project map ----

def test_git_entries_reads_paths_docs_and_blobs(project):
    entries, sources, blobs = factory_mcp._git_entries(str(project))
    assert [e["path"] for e in entries] == ["calc.py", "elo.py"]
    assert entries[0]["lines"] == 2          # "x = 1\n"
    assert entries[0]["doc"] == ""           # no docstring in the fixture
    assert blobs["calc.py"] == entries[0]["blob"]
    assert "x = 1" in sources["calc.py"]


def test_git_entries_reads_the_module_docstring(project):
    (project / "documented.py").write_text('"""Does a thing."""\nY = 2\n')
    git(project, "add", "-A")
    git(project, "commit", "-qm", "doc")
    entries, _sources, _blobs = factory_mcp._git_entries(str(project))
    doc = {e["path"]: e["doc"] for e in entries}
    assert doc["documented.py"] == "Does a thing."


def test_git_entries_returns_none_outside_a_repo(tmp_path):
    plain = tmp_path / "nogit"
    plain.mkdir()
    assert factory_mcp._git_entries(str(plain)) is None


def test_walk_entries_is_bounded(tmp_path):
    plain = tmp_path / "nogit"
    plain.mkdir()
    for i in range(50):
        (plain / "f{}.py".format(i)).write_text("x = 1\n")
    entries, sources, blobs = factory_mcp._walk_entries(str(plain))
    assert len(entries) == 50
    assert len(entries) <= factory_mcp.MAX_WALK_FILES
    assert blobs == {}                       # no git, no anchor
    assert set(sources) == {e["path"] for e in entries}


def test_map_block_is_snapshotted_per_session(factory, project):
    first = factory._map_block("c_1", str(project))
    assert "calc.py" in first
    (project / "calc.py").write_text("# changed entirely\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "change")
    assert factory._map_block("c_1", str(project)) == first
    assert factory._map_block("c_2", str(project)) != first


def test_map_block_appears_in_the_system_head(factory, project):
    head = factory._agent_system_head(
        {"session_id": "c_1", "workspace": str(project), "model": "m"},
        tools=[])
    assert project_map.HEADER in head
    assert "calc.py (2L)" in head


def test_map_block_survives_a_broken_workspace(factory):
    assert factory._map_block("c_3", "/does/not/exist") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest harness/tests/test_factory_mcp.py -k "map or entries" -v`
Expected: FAIL — `AttributeError: module 'factory_mcp' has no attribute '_git_entries'`

- [ ] **Step 3: Write minimal implementation**

Add near `_read_memory` (`factory_mcp.py:287`):

```python
MAX_WALK_FILES = 2000
MAX_WALK_DEPTH = 6
MAP_SUFFIXES = (".py", ".js", ".css", ".html", ".toml", ".json", ".md")


def _doc_of(path, text):
    """First docstring line for Python, else the first meaningful line."""
    if path.endswith(".py"):
        try:
            return (ast.get_docstring(ast.parse(text)) or "").split("\n")[0]
        except (SyntaxError, ValueError):
            return "(invalid syntax)"
    for line in text.splitlines()[:4]:
        stripped = line.strip(" /*#-")
        if stripped:
            return stripped
    return ""


def _entry(path, text, blob):
    return {"path": path, "lines": text.count("\n") + 1,
            "doc": _doc_of(path, text), "blob": blob}


def _git_entries(workspace):
    """(entries, sources, blobs) from `git ls-files -s`, or None outside a
    repo. git is the only authority on what belongs to a project: the first
    prototype of this map used a hand-maintained blacklist, forgot `venv/`,
    and indexed 22122 files."""
    try:
        out = subprocess.run(["git", "ls-files", "-s"], cwd=workspace,
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    entries, sources, blobs = [], {}, {}
    for line in out.stdout.splitlines():
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if not path or len(fields) < 2 or not path.endswith(MAP_SUFFIXES):
            continue
        try:
            text = (Path(workspace) / path).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
        blobs[path] = fields[1]
        sources[path] = text
        entries.append(_entry(path, text, fields[1]))
    return entries, sources, blobs


def _walk_entries(workspace):
    """Bounded fallback for a workspace without git. Never unbounded: an
    unbounded walk is exactly the bug being removed from `_search`."""
    root = Path(workspace)
    entries, sources = [], {}
    for dirpath, dirnames, filenames in os.walk(workspace):
        rel = Path(dirpath).relative_to(root)
        if len(rel.parts) >= MAX_WALK_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in sorted(dirnames)
                       if not d.startswith(".")
                       and d not in agent_tools.SKIP_DIRS
                       and d not in ("venv", "models", "dist", "build")]
        for fn in sorted(filenames):
            if not fn.endswith(MAP_SUFFIXES):
                continue
            if len(entries) >= MAX_WALK_FILES:
                return entries, sources, {}
            path = (rel / fn).as_posix()
            try:
                text = (root / path).read_text(encoding="utf-8",
                                               errors="replace")
            except OSError:
                continue
            sources[path] = text
            entries.append(_entry(path, text, ""))
    return entries, sources, {}
```

Add `import ast` to the stdlib import block (`factory_mcp.py:14-25`). `json`, `os` and `subprocess` are already imported there — do not duplicate them.

In `Factory.__init__`, next to `self._memory_blocks = {}` (line 481):

```python
        # Same snapshot rule as lessons and memory: the map is the KV prefix,
        # and the agent edits files in the very workspace it describes.
        self._map_blocks = {}
```

Add the method next to `_memory_block` (line 497):

```python
    def _map_block(self, session_id, workspace):
        """The project map this session carries, decided once and kept."""
        if session_id in self._map_blocks:
            return self._map_blocks[session_id]
        block = ""
        try:
            got = _git_entries(workspace) or _walk_entries(workspace)
            entries, _sources, blobs = got
            notes = project_map.stale(
                project_map.parse(_read_memory(workspace)), blobs)
            block = project_map.render(entries, notes)
        except Exception as e:  # noqa: BLE001 - a missing map is not fatal
            print("project map: {}".format(e), file=sys.stderr)
        self._map_blocks[session_id] = block
        return block
```

Extend `_agent_system_head` (line 1285), adding the map **first** so the agent reads where things are before what failed:

```python
        for block in (self._map_block(session["session_id"],
                                      session["workspace"]),
                      self._lesson_block(session["session_id"]),
                      self._memory_block(session["session_id"],
                                         session["workspace"])):
```

Add `import project_map` to the harness imports at the top of `factory_mcp.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest harness/tests/test_factory_mcp.py -k "map or entries" harness/tests/test_project_map.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite — this task touches the prompt every session builds**

Run: `python -m pytest harness/tests/ -q`
Expected: PASS, no new failures. `test_factory_mcp.py` asserts on prompt contents; if a test breaks because the head grew a block, update that test's expectation — do not remove the block.

- [ ] **Step 6: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -m "feat(factory_mcp): carry the project map in the session system prompt"
```

---

### Task 6: The annotation cycle

**Files:**
- Modify: `harness/factory_mcp.py` (add `_touched_files`, `_should_annotate`, `Factory._annotate_from`; call it from `_learn_from` near line 544)
- Test: `harness/tests/test_factory_mcp.py` (append)

**Interfaces:**
- Consumes: `project_map.vocabulary`, `project_map.verify` (Task 3); `project_map.parse`, `project_map.update` (Task 4); `_git_entries` (Task 5).
- Produces:
  - `_touched_files(messages) -> list[str]` — module-level in `factory_mcp`. Paths from `read_file`, `write_file` and `edit_file` tool calls, most recent first, deduplicated. `list_dir` and `search` are excluded: listing a directory is not understanding a file.
  - `_should_annotate(messages, pending) -> bool` — module-level. True when the fresh slice contains a `step_done` call, or when `pending` (files touched and still unannotated) has reached `MAX_NOTES_PER_STEP`.
  - `Factory._annotate_from(session_id, session)` — selects, asks, verifies, persists. Never raises.

**Cost gate — read this before implementing.** `_learn_from` runs at the end of *every user turn* (`factory_mcp.py:1493`), and its docstring states it makes no LLM call. Annotation does. Hanging it unguarded off `_learn_from` would add a generation to every single turn, which is the opposite of this plan's purpose. `_should_annotate` is what keeps it to step boundaries.

**GPU lock.** `_learn_from` is called at line 1493, inside the `_acquire_runtime(session["model"])` scope opened at line 1487. The runtime is therefore already held, exactly as `_compact` requires. Do not acquire it again.

- [ ] **Step 1: Write the failing test**

Append to `harness/tests/test_factory_mcp.py`.

```python
# ---- annotation cycle ----

def _tc(name, **args):
    return {"function": {"name": name, "arguments": json.dumps(args)}}


def _read(path):
    return {"role": "assistant", "tool_calls": [_tc("read_file", path=path)]}


class _Replies:
    """Stands in for llama_client.chat: one canned reply, counted calls."""
    def __init__(self, reply=""):
        self.reply, self.calls = reply, 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return {"content": self.reply}


def test_touched_files_takes_reads_and_edits_newest_first():
    messages = [_read("a.py"),
                {"role": "assistant",
                 "tool_calls": [_tc("edit_file", path="b.py")]}]
    assert factory_mcp._touched_files(messages) == ["b.py", "a.py"]


def test_touched_files_ignores_listing_and_searching():
    messages = [{"role": "assistant",
                 "tool_calls": [_tc("list_dir", path="harness"),
                                _tc("search", pattern="x")]}]
    assert factory_mcp._touched_files(messages) == []


def test_touched_files_deduplicates():
    assert factory_mcp._touched_files([_read("a.py"), _read("a.py")]) == \
        ["a.py"]


def test_should_annotate_only_on_a_step_boundary_or_a_full_batch():
    step = {"role": "assistant", "tool_calls": [_tc("step_done", step=1)]}
    assert factory_mcp._should_annotate([step], ["a.py"]) is True
    assert factory_mcp._should_annotate([_read("a.py")], ["a.py"]) is False
    many = ["f{}.py".format(i)
            for i in range(project_map.MAX_NOTES_PER_STEP)]
    assert factory_mcp._should_annotate([_read("a.py")], many) is True


def _annotating_session(project, reply, monkeypatch):
    """A session that read calc.py and closed a step, with the model stubbed."""
    stub = _Replies(reply)
    monkeypatch.setattr(factory_mcp.llama_client, "chat", stub)
    session = {
        "session_id": "c_ann", "workspace": str(project), "model": "m",
        "messages": [_read("calc.py"),
                     {"role": "assistant",
                      "tool_calls": [_tc("step_done", step=1)]}]}
    return session, stub


def test_annotate_persists_a_verified_note(factory, project, monkeypatch):
    session, stub = _annotating_session(
        project, "calc.py : x vaut 1, pas de configuration", monkeypatch)
    factory._annotate_from("c_ann", session)
    assert stub.calls == 1
    notes = project_map.parse((project / "MEMORY.md").read_text(
        encoding="utf-8"))
    assert [n["path"] for n in notes] == ["calc.py"]
    assert notes[0]["text"] == "x vaut 1, pas de configuration"


def test_annotate_rejects_an_unevidenced_note_and_records_a_lesson(
        factory, project, monkeypatch):
    session, _stub = _annotating_session(
        project, "calc.py : Backend FastAPI", monkeypatch)
    factory._annotate_from("c_ann", session)
    memory = project / "MEMORY.md"
    assert not memory.exists() or project_map.parse(
        memory.read_text(encoding="utf-8")) == []
    patterns = [l["pattern"] for l in factory.chats.load_lessons()]
    assert "unevidenced_annotation" in patterns


def test_annotate_skips_a_file_that_already_has_a_fresh_note(
        factory, project, monkeypatch):
    entries, _sources, blobs = factory_mcp._git_entries(str(project))
    (project / "MEMORY.md").write_text(project_map.update("", [
        {"path": "calc.py", "text": "déjà noté", "blob": blobs["calc.py"],
         "last_seen": 0.0}]), encoding="utf-8")
    session, stub = _annotating_session(project, "ignored", monkeypatch)
    factory._annotate_from("c_ann", session)
    assert stub.calls == 0          # nothing to ask, so no generation


def test_annotate_leaves_prose_above_the_markers_untouched(
        factory, project, monkeypatch):
    (project / "MEMORY.md").write_text("# MEMORY\n\nÉcrit par Martin.\n",
                                       encoding="utf-8")
    session, _stub = _annotating_session(
        project, "calc.py : x vaut 1", monkeypatch)
    factory._annotate_from("c_ann", session)
    text = (project / "MEMORY.md").read_text(encoding="utf-8")
    assert text.startswith("# MEMORY\n\nÉcrit par Martin.\n")
```

`json` and `project_map` must be imported in `test_factory_mcp.py`; `json` already is.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest harness/tests/test_factory_mcp.py -k "annotate or touched" -v`
Expected: FAIL — `AttributeError: module 'factory_mcp' has no attribute '_touched_files'`

- [ ] **Step 3: Write minimal implementation**

```python
ANNOTATE_TOOLS = ("read_file", "write_file", "edit_file")

ANNOTATE_SYSTEM = ("Tu annotes des fichiers pour un index de projet. "
                   "Une ligne par fichier, rien d'autre.")

ANNOTATE_PROMPT = """Voici des fichiers que tu viens de lire ou de modifier.
Pour chacun, une seule ligne : ce que tu as compris et qui ne se lit PAS dans
son en-tête. Rien d'autre -- pas d'introduction, pas de conclusion.
Format exact, une ligne par fichier :
<chemin> : <ce que tu as compris>
N'écris que ce que ces fichiers prouvent. Une affirmation invérifiable est
rejetée.

Fichiers :
{files}"""


def _touched_files(messages):
    """Files read, written or edited, newest first, deduplicated. Listing a
    directory or running a search is not understanding a file."""
    seen, out = set(), []
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") not in ANNOTATE_TOOLS:
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            path = (args or {}).get("path")
            if path and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _should_annotate(messages, pending):
    """Annotation costs a generation, and `_learn_from` runs at the end of
    EVERY user turn. Fire it on a step boundary, or once enough unannotated
    files have piled up that a session without a plan still learns."""
    if len(pending) >= project_map.MAX_NOTES_PER_STEP:
        return True
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if (tc.get("function") or {}).get("name") == "step_done":
                return True
    return False
```

And on `Factory`, next to `_remember` (line 549):

```python
    def _annotate_from(self, session_id, session):
        """One bounded call: what did this step teach about the files it
        touched. Verified against the repo before anything is written --
        MEMORY.md asserted the backend was FastAPI for two days because
        nothing checked (spec 2026-07-27)."""
        workspace = session.get("workspace")
        got = _git_entries(workspace) if workspace else None
        if not got:
            return                      # no git, no blob, no anchor to trust
        entries, sources, blobs = got
        known = {n["path"]: n for n in
                 project_map.stale(project_map.parse(_read_memory(workspace)),
                                   blobs)}
        tracked = {e["path"] for e in entries}
        messages = session.get("messages") or []
        pending = [p for p in _touched_files(messages)
                   if p in tracked
                   and not (p in known and not known[p]["stale"])]
        if not pending or not _should_annotate(messages, pending):
            return
        wanted = pending[:project_map.MAX_NOTES_PER_STEP]
        # Same single-shot call shape as `_compact` (line 1090). The GPU
        # runtime is already held: `_learn_from` runs inside the
        # `_acquire_runtime` scope opened at line 1487.
        res = llama_client.chat(
            self._runtime().url, ANNOTATE_SYSTEM,
            ANNOTATE_PROMPT.format(files="\n".join(wanted)),
            temperature=0.1, num_ctx=CHAT_NUM_CTX,
            options=self._model_options(session["model"]))
        reply = (res.get("content") or "").strip()
        vocab = project_map.vocabulary(sources)
        fresh, rejected = [], []
        for line in (reply or "").splitlines():
            path, _, text = line.partition(" : ")
            path, text = path.strip(), text.strip()
            if path not in wanted or not text:
                continue
            bad = project_map.verify(text, vocab)
            if bad:
                rejected.append((path, bad))
                continue
            fresh.append({"path": path, "text": text,
                          "blob": blobs[path], "last_seen": time.time()})
        if rejected:
            # Never swallowed: a silent rejection rebuilds the blind spot that
            # let `error: search failed:` pass 15 times unnoticed.
            self.chats.record_lessons([lessons.incident(
                "unevidenced_annotation", time.time(),
                "An annotation named {} which appears nowhere in the "
                "repository. Write only what the files prove.".format(
                    ", ".join(sorted({b for _, b in rejected}))))])
        if fresh:
            merged = {n["path"]: n for n in known.values()}
            merged.update({n["path"]: n for n in fresh})
            updated = project_map.update(
                _read_memory(workspace),
                [{k: v for k, v in n.items() if k != "stale"}
                 for n in merged.values()])
            atomic_write_bytes(Path(workspace) / MEMORY_FILE,
                               updated.encode("utf-8"))
```

There is no `_write_memory` helper and no `_ask` helper — do not look for them. `_remember` writes `MEMORY.md` with `atomic_write_bytes(Path(workspace) / MEMORY_FILE, ...)` (`factory_mcp.py:576`), and `_compact` makes its single-shot call with `llama_client.chat(...)` directly (`factory_mcp.py:1090`). Follow both, rather than introducing a second way to do either.

Call it from `_learn_from`, in its own `try` beside the others (around line 544):

```python
        try:
            self._annotate_from(session_id, session)
        except Exception as e:  # noqa: BLE001
            print("annotate: {}".format(e), file=sys.stderr)
```

Note the deliberate boundary: `_learn_from` documents itself as "no LLM call, both are arithmetic". Annotation *is* an LLM call, so it is a separate method with its own try block, and its docstring says so. Update `_learn_from`'s docstring to stop claiming the whole function is arithmetic.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest harness/tests/test_factory_mcp.py -k "annotate or touched" -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest harness/tests/ -q`
Expected: PASS, no new failures.

- [ ] **Step 6: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -m "feat(factory_mcp): annotate touched files, verified before they land"
```

---

### Task 7: The system-prompt rule, and the poisoned line

**Files:**
- Modify: `harness/factory_mcp.py:373-374` (`AGENT_SYSTEM`)
- Modify: `MEMORY.md` (the hand-written section, one line)
- Test: `harness/tests/test_factory_mcp.py` (append)

**Interfaces:**
- Consumes: the map block from Task 5.
- Produces: no new API.

- [ ] **Step 1: Write the failing test**

```python
def test_system_prompt_points_at_the_map(factory, project):
    head = factory._agent_system_head(
        {"session_id": "c_9", "workspace": str(project), "model": "m"},
        tools=[])
    assert "authoritative for where things are" in head
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest harness/tests/test_factory_mcp.py::test_system_prompt_points_at_the_map -v`
Expected: FAIL — assertion error, the phrase is absent.

- [ ] **Step 3: Write minimal implementation**

In `AGENT_SYSTEM`, immediately after the existing "Prefer the built-in tools" sentence (`factory_mcp.py:373-374`):

```
The project map below is authoritative for where things are. Do not use
list_dir or search to locate a file it already names.
```

Two lines, no more: this is the KV prefix and every word is paid for the whole session.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest harness/tests/test_factory_mcp.py -k map -v`
Expected: PASS

- [ ] **Step 5: Correct the poisoned line in `MEMORY.md`**

In the hand-written section "Apprentissage projet local-factory [25/07/2026]", replace:

```
- **Backend** : FastAPI (`harness/factory_web.py`), port 8787
```

with:

```
- **Backend** : stdlib pur, `BaseHTTPRequestHandler` (`harness/factory_web.py`), port 8787
```

Change nothing else in that section. Above the markers is the operator's file; this single line is corrected because it is false and shipped in every prompt, which is why the verification in Task 3 exists.

Verify the claim before writing it:

Run: `git grep -c "FastAPI" -- . ; grep -n "BaseHTTPRequestHandler" harness/factory_web.py`
Expected: no FastAPI match anywhere; `BaseHTTPRequestHandler` present in the import at `harness/factory_web.py:18`.

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest harness/tests/ -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py MEMORY.md
git commit -m "feat(agent): point the agent at the map, correct the FastAPI claim"
```

---

## Verification before hand-off

- [ ] `python -m pytest harness/tests/ -q` is green.
- [ ] `python -c "import project_map, inspect; assert 'open(' not in inspect.getsource(project_map)"` — the purity constraint, checked rather than assumed.
- [ ] Start the web UI, open a chat session on this workspace, and confirm from the transcript that the map block is present in the first prompt and that its bytes do not change between turn 1 and turn 2.
- [ ] Measure: rendered block size in chars, and its token estimate at ratio 3. Measured 2026-07-27: 253 files, 20546 chars, ~6848 tokens. Record the number in `docs/backlog-optimisations.md` — a levier without a measurement is not a levier.

## Not in this plan

The spec notes that the map's token cost is paid for by dropping the git MCP server from `[toolsets.dev]` (`factory.toml:108`) — ~1900 tokens of schemas serving 46 of 4937 calls. That change belongs to the separate batch of mechanical fixes (search, eviction stub, read deduplication, toolset trimming, syntax checks) and is deliberately **not** a task here: this plan must stand or fall on its own measurement, and bundling a token saving with a token cost would make both unreadable.

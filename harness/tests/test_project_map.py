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
                             [_n("a.py", "reprojete a chaque tour")])
    assert "a.py (5L) Doc.\n    ^ reprojete a chaque tour" in out
    assert out.startswith(project_map.HEADER)
    assert out.rstrip().endswith(project_map.FOOTER)


def test_render_marks_a_stale_note():
    out = project_map.render([_e("a.py", 5)],
                             [dict(_n("a.py", "vieux"), stale=True)])
    assert "^ vieux" + project_map.STALE_MARK in out


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
    assert "pkg/ (400 files" in out
    # the last-sorting file must not be the one silently dropped
    assert "mod399" not in out and "mod0" not in out


def test_render_says_in_the_block_that_it_degraded():
    entries = [_e("pkg/mod{}.py".format(i), 10, "x" * 80) for i in range(400)]
    out = project_map.render(entries, [], budget=2000)
    assert project_map.degraded(out) is True
    assert "400 files" in out          # the count is named, not just implied


def test_render_that_fits_is_not_degraded():
    out = project_map.render([_e("a.py", 5, "Doc.")], [])
    assert project_map.degraded(out) is False


def test_degraded_is_false_on_an_empty_block():
    assert project_map.degraded("") is False


def test_ceiling_leaves_room_for_the_repo_to_grow():
    # 2026-07-27: the real repo renders to 20546 chars. A ceiling sitting just
    # above today's size means the next few commits tip the map into the
    # directory view without warning.
    assert project_map.MAX_MAP_CHARS >= 30000


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
    assert project_map.verify("Le serveur ecoute sur 8787.", vocab) is None


MEMORY_WITH_MAP = """# MEMORY

Human prose the harness must never touch.

<!-- factory:map -->
- `harness/chat_context.py` @a1b2c3d : reprojete a chaque tour
- `harness/factory_web.py` @9f4e21c : stdlib pur
<!-- /factory:map -->
"""


def test_parse_reads_notes_from_the_block():
    notes = project_map.parse(MEMORY_WITH_MAP)
    assert [n["path"] for n in notes] == ["harness/chat_context.py",
                                          "harness/factory_web.py"]
    assert notes[0]["blob"] == "a1b2c3d"
    assert notes[0]["text"] == "reprojete a chaque tour"


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


def test_vocabulary_ignores_prose_files_entirely():
    # MEMORY.md is itself a source. Before this, the false FastAPI claim it
    # contained became its own evidence and could never be rejected again.
    vocab = project_map.vocabulary(
        {"MEMORY.md": "Backend : FastAPI, port 8787"})
    assert "FastAPI" not in vocab


def test_vocabulary_ignores_docstrings_and_comments():
    # The docstring documenting the FastAPI bug re-armed the bug.
    vocab = project_map.vocabulary(
        {"a.py": '"""Not FastAPI at all."""\n# nor Django\nimport json\n'})
    assert "FastAPI" not in vocab
    assert "Django" not in vocab
    assert "json" in vocab


def test_vocabulary_survives_an_unparseable_python_file():
    vocab = project_map.vocabulary({"broken.py": "def (((\nBaseHandler\n"})
    assert "BaseHandler" in vocab

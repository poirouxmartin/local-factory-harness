"""Memory is per-workspace, and MEMORY.md is a file a human owns.

Two contracts matter here: a command that failed twice comes back as an entry
next session (the anti-redo ask), and nothing the operator wrote in MEMORY.md
is ever rewritten.
"""
import workspace_memory as wm


def _call(name, **args):
    return {"function": {"name": name, "arguments": args}}


def _session(*pairs):
    """A transcript from (call, output) pairs, one assistant turn each."""
    messages = []
    for call, output in pairs:
        messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
        messages.append({"role": "tool", "tool_name": call["function"]["name"],
                         "content": output})
    return {"session_id": "c_1", "messages": messages}


# ---- capture ---------------------------------------------------------------

def test_failed_reads_the_exit_code_not_the_words():
    assert wm.failed("exit 1\nboom")
    assert wm.failed("error: refused -- rm -rf.")
    assert not wm.failed("exit 0\nall good")
    assert not wm.failed("exit 0\n(no output)")


def test_a_command_that_failed_twice_becomes_a_memory():
    cmd = _call("run_command", command="pytest")
    out = "exit 1\nstderr:\nModuleNotFoundError: No module named 'pytest'"
    got = wm.from_session(_session((cmd, out), (cmd, out)), now=42.0)
    assert len(got) == 1
    assert got[0]["command"] == "pytest"
    assert got[0]["count"] == 2
    assert "ModuleNotFoundError" in got[0]["detail"]
    assert got[0]["last_seen"] == 42.0


def test_from_session_counts_the_first_failure_too():
    # The floor moved to promote(): a single failure has to be COUNTED here,
    # or a command that fails once per turn is forgotten turn after turn.
    cmd = _call("run_command", command="pytest")
    got = wm.from_session(_session((cmd, "exit 1\nboom")), now=1.0)
    assert [(e["command"], e["count"]) for e in got] == [("pytest", 1)]


def test_a_single_failure_is_a_typo_not_a_memory():
    cmd = _call("run_command", command="pytest")
    raw = wm.from_session(_session((cmd, "exit 1\nboom")), now=1.0)
    entries, written = wm.promote(raw, {}, [], now=1.0)
    assert entries == [] and written == {}


def test_a_command_that_works_is_never_remembered():
    cmd = _call("run_command", command="git status")
    got = wm.from_session(_session((cmd, "exit 0\nclean"),
                                   (cmd, "exit 0\nclean")), now=1.0)
    assert got == []


def test_only_shell_failures_are_workspace_facts():
    # A failed edit_file is the agent misreading a file: that is a lesson
    # about the agent, not a fact about this project.
    cmd = _call("edit_file", path="a.py", old="x", new="y")
    got = wm.from_session(_session((cmd, "error: 0 matches"),
                                   (cmd, "error: 0 matches")), now=1.0)
    assert got == []


def test_system_notices_do_not_shift_the_pairing():
    cmd = _call("run_command", command="npm test")
    session = _session((cmd, "exit 1\nstderr:\nfailing"),
                       (cmd, "exit 1\nstderr:\nfailing"))
    session["messages"].insert(2, {"role": "tool", "tool_name": "system",
                                   "content": "you have 5 iterations left"})
    got = wm.from_session(session, now=1.0)
    assert [e["command"] for e in got] == ["npm test"]


# ---- promote: the floor is on the total, across turns ----------------------
# Learning runs at the end of every turn over a transcript that keeps growing.
# The watermark fix (2026-07-22) stopped the double counting by slicing, and
# opened this: one failure per turn never reached FAILURES_MIN inside a slice.
# `from_session` now recounts the WHOLE transcript and `written` is what keeps
# a failure from being banked twice.

def _turn(command, out, turns):
    """The full transcript after N turns, one failing call per turn."""
    cmd = _call("run_command", command=command)
    return _session(*[(cmd, out)] * turns)


def test_one_failure_per_turn_is_remembered_once_it_repeats():
    out = "exit 1\nstderr:\nboom"
    entries, written = wm.promote(
        wm.from_session(_turn("flaky.py", out, 1), now=1.0), {}, [], now=1.0)
    assert entries == []                      # turn 1: a typo, so far

    entries, written = wm.promote(
        wm.from_session(_turn("flaky.py", out, 2), now=2.0), written, entries,
        now=2.0)
    assert [(e["command"], e["count"]) for e in entries] == [("flaky.py", 2)]

    entries, written = wm.promote(
        wm.from_session(_turn("flaky.py", out, 3), now=3.0), written, entries,
        now=3.0)
    assert [(e["command"], e["count"]) for e in entries] == [("flaky.py", 3)]


def test_a_turn_that_adds_nothing_adds_no_count():
    # The bug the watermark fixed, re-checked at this level: re-learning the
    # same transcript (a "merci" turn) must not inflate anything.
    raw = wm.from_session(_turn("pytest", "exit 1\nboom", 2), now=1.0)
    entries, written = wm.promote(raw, {}, [], now=1.0)
    again, written_again = wm.promote(raw, written, entries, now=2.0)
    assert [(e["command"], e["count"]) for e in again] == [("pytest", 2)]
    assert written_again == written


def test_a_command_the_file_already_knows_needs_only_one_more_failure():
    known = [{"command": "pytest", "detail": "boom", "count": 2, "last_seen": 0}]
    raw = wm.from_session(_turn("pytest", "exit 1\nstderr:\nstill boom", 1),
                          now=5.0)
    entries, written = wm.promote(raw, {}, known, now=5.0)
    assert entries[0]["count"] == 3
    assert entries[0]["detail"] == "still boom"
    assert written == {"pytest": 1}


# ---- the detail is the reason, and the reason lives at the end -------------
# Replaying the 44 archived sessions (2026-07-22) produced entries like
# "`check_env.py` failed 3x: ====================" and "failed 4x: Traceback
# (most recent call last):" -- the two least informative lines in the output.
# The classification was right every time; the summary taught nothing. These
# outputs are shaped exactly like the real ones.

def test_the_detail_of_a_traceback_is_the_exception_not_its_header():
    out = ("exit 1\n"
           "Traceback (most recent call last):\n"
           '  File "check_env.py", line 99, in check_torch_cu128\n'
           "    print(prop.total_mem)\n"
           "AttributeError: 'CudaDeviceProperties' object has no attribute "
           "'total_mem'")
    assert "AttributeError" in wm.detail_of(out)


def test_a_banner_separator_is_never_the_reason():
    out = ("exit 1\n"
           "============================================================\n"
           "Local Image Generation - Environment Checker\n"
           "============================================================\n"
           "[OK] Python: 3.12.10\n"
           "\n"
           "Quick fix for missing PyTorch cu128:\n"
           "  pip install torch --index-url https://download.pytorch.org/cu128\n"
           "============================================================")
    detail = wm.detail_of(out)
    assert set(detail) != {"="}
    assert "pip install torch" in detail


def test_a_wrapped_complaint_keeps_the_half_that_names_the_command():
    # cmd.exe wraps its most common message across two lines, and the half
    # worth remembering is the FIRST one -- the one holding `wc`. Taking the
    # last line alone yields "ou externe, un programme executable...", which
    # names nothing. This is the single most frequent failure in the archive.
    out = ("exit 1\n"
           "'wc' n'est pas reconnu en tant que commande interne\n"
           "ou externe, un programme executable ou un fichier de commandes.")
    detail = wm.detail_of(out)
    assert "'wc'" in detail
    assert "n'est pas reconnu" in detail


def test_a_one_line_complaint_is_still_read_the_same_way():
    out = "exit 1\n'wc' n'est pas reconnu en tant que commande interne"
    assert "n'est pas reconnu" in wm.detail_of(out)


def test_stderr_still_wins_over_stdout_chatter():
    out = ("exit 1\n"
           "running 12 tests\n"
           "stderr:\n"
           "ModuleNotFoundError: No module named 'pytest'")
    assert "ModuleNotFoundError" in wm.detail_of(out)


def test_an_exit_code_alone_is_all_there_is_to_say():
    assert wm.detail_of("exit 1") == "exit 1"
    assert wm.detail_of("") == "no output"


def test_merge_bumps_a_known_command_and_keeps_the_newest_detail():
    known = [{"command": "pytest", "detail": "old", "count": 2,
              "last_seen": 1.0}]
    got = wm.merge(known, [{"command": "pytest", "detail": "new", "count": 3,
                            "last_seen": 9.0}])
    assert len(got) == 1
    assert got[0]["count"] == 5
    assert got[0]["detail"] == "new"
    assert got[0]["last_seen"] == 9.0
    assert known[0]["count"] == 2  # inputs are never mutated


# ---- the file --------------------------------------------------------------

def test_update_never_touches_what_the_operator_wrote():
    text = "# MEMORY\n\nThe venv is at .venv, always use it.\n"
    out = wm.update(text, [{"command": "pytest", "detail": "missing",
                            "count": 2}])
    assert out.startswith("# MEMORY\n\nThe venv is at .venv, always use it.")
    assert "- `pytest` failed 2x: missing" in out


def test_update_replaces_the_block_instead_of_stacking_them():
    first = wm.update("# MEMORY\n", [{"command": "a", "detail": "x", "count": 2}])
    second = wm.update(first, [{"command": "b", "detail": "y", "count": 3}])
    assert second.count(wm.BEGIN) == 1
    assert "- `a`" not in second
    assert "- `b` failed 3x: y" in second


def test_entries_survive_a_round_trip_through_the_file():
    entries = [{"command": "pip install torch", "detail": "no matching dist",
                "count": 4, "last_seen": 7.0}]
    back = wm.parse(wm.update("", entries))
    assert back[0]["command"] == "pip install torch"
    assert back[0]["count"] == 4
    assert back[0]["detail"] == "no matching dist"


def test_parse_ignores_prose_the_operator_left_in_the_block():
    text = wm.update("", [{"command": "a", "detail": "x", "count": 2}])
    text = text.replace(wm.END, "note to self: check the proxy\n" + wm.END)
    assert [e["command"] for e in wm.parse(text)] == ["a"]


def test_render_is_deterministic_and_bounded():
    entries = [{"command": "c{}".format(i), "detail": "d" * 200, "count": i}
               for i in range(40)]
    block = wm.render(wm.update("", entries))
    assert len(block) <= wm.BUDGET_CHARS
    assert block == wm.render(wm.update("", entries))
    assert block.startswith(wm.HEADER)


def test_render_of_an_empty_memory_adds_no_bytes():
    assert wm.render("") == ""
    assert wm.render("   \n") == ""
    assert wm.render(None) == ""


# ---- carnet: walls and notes cross sessions --------------------------------

def _wall(w="VAE gated", cause="auth", tried=("officiel",), remaining=("mirror",)):
    return {"wall": w, "cause": cause, "tried": list(tried),
            "remaining": list(remaining)}


def test_wall_round_trips_through_the_carnet_block():
    text = wm.update_carnet("# MEMORY\n", [_wall()], [])
    assert "MUR VAE gated" in text
    back = wm.parse_walls(text)
    assert len(back) == 1
    assert back[0]["wall"] == "VAE gated"
    assert back[0]["cause"] == "auth"
    assert back[0]["tried"] == ["officiel"]
    assert back[0]["remaining"] == ["mirror"]


def test_note_round_trips_and_empty_lists_survive():
    text = wm.update_carnet("", [_wall(tried=(), remaining=())],
                            ["le venv est dans .venv39"])
    assert wm.parse_notes(text) == ["le venv est dans .venv39"]
    assert wm.parse_walls(text)[0]["tried"] == []
    assert wm.parse_walls(text)[0]["remaining"] == []


def test_carnet_block_is_idempotent_across_turns():
    """The projection recounts the whole transcript every turn: rewriting the
    same walls and notes must not stack duplicates in the file."""
    once = wm.update_carnet("", [_wall()], ["a"])
    twice = wm.update_carnet(once, [_wall()], ["a"])
    assert twice == once


def test_carnet_block_keeps_what_another_session_recorded():
    first = wm.update_carnet("", [_wall("VAE gated")], ["a"])
    second = wm.update_carnet(first, [_wall("port 8091 occupe")], ["b"])
    assert [w["wall"] for w in wm.parse_walls(second)] == \
        ["VAE gated", "port 8091 occupe"]
    assert wm.parse_notes(second) == ["a", "b"]


def test_carnet_block_is_bounded_and_deterministic():
    walls = [_wall("mur {}".format(i)) for i in range(30)]
    notes = ["note {}".format(i) for i in range(30)]
    text = wm.update_carnet("", walls, notes)
    assert len(wm.parse_walls(text)) == wm.MAX_WALLS
    assert len(wm.parse_notes(text)) == wm.MAX_NOTES
    assert text == wm.update_carnet("", walls, notes)


def test_carnet_block_leaves_the_human_half_and_the_failures_untouched():
    text = wm.update("# MEMORY\n\nregles a moi\n",
                     [{"command": "pytest", "detail": "boom", "count": 2}])
    after = wm.update_carnet(text, [_wall()], [])
    assert "regles a moi" in after
    assert wm.parse(after) == wm.parse(text)   # the failures block is untouched


def test_from_session_ignores_plan_messages():
    """plan/step/progress are stored under a tool_name that is not the call
    name: pairing by name equality would drain the pending queue looking for
    them and drop the failure that follows (same trap as note/wall)."""
    session = {"messages": [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "set_plan", "arguments": {}}},
            {"function": {"name": "run_command",
                          "arguments": {"command": "python go.py"}}}]},
        {"role": "tool", "tool_name": "plan", "content": "{}"},
        {"role": "tool", "tool_name": "run_command", "content": "exit 1\nboom"},
    ]}
    found = wm.from_session(session, now=0)
    assert [e["command"] for e in found] == ["python go.py"]
    assert found[0]["detail"] == "boom"


def test_from_session_ignores_handback_messages():
    """ask_operator/request_resource persist as `handback`, a tool_name that is
    not the call name: pairing by name equality would drain the pending queue
    and drop the failure that follows (same trap as note/wall/plan)."""
    session = {"messages": [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "ask_operator", "arguments": {}}},
            {"function": {"name": "run_command",
                          "arguments": {"command": "python go.py"}}}]},
        {"role": "tool", "tool_name": "handback", "content": "{}"},
        {"role": "tool", "tool_name": "run_command", "content": "exit 1\nboom"},
    ]}
    found = wm.from_session(session, now=0)
    assert [e["command"] for e in found] == ["python go.py"]

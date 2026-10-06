"""Workspace-confined tools for the chat agent."""
import os
import subprocess
import sys
import time

import pytest

import agent_tools
from agent_tools import needs_approval, run


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("K = 32\nname = 'elo'\n",
                                             encoding="utf-8")
    (tmp_path / "top.txt").write_text("hello\nworld\n", encoding="utf-8")
    return tmp_path


def test_list_dir_shows_entries(ws):
    out = run("list_dir", {"path": "."}, ws)
    assert "pkg/" in out and "top.txt" in out


def test_read_file_numbers_lines(ws):
    out = run("read_file", {"path": "pkg/mod.py"}, ws)
    assert "1\tK = 32" in out and "2\tname = 'elo'" in out


def test_read_file_offset_and_limit(ws):
    out = run("read_file", {"path": "top.txt", "offset": 2, "limit": 1}, ws)
    assert "2\tworld" in out and "hello" not in out


def test_search_finds_matches_across_files(ws):
    out = run("search", {"pattern": "K = \\d+"}, ws)
    assert "pkg/mod.py:1" in out and "K = 32" in out


def test_search_reports_zero_matches(ws):
    assert "no matches" in run("search", {"pattern": "zzz_nothing"}, ws)


def test_paths_may_not_escape_the_workspace(ws):
    out = run("read_file", {"path": "../outside.txt"}, ws)
    assert "error" in out.lower() and "workspace" in out


def test_absolute_paths_outside_are_refused(ws):
    out = run("list_dir", {"path": "C:\\Windows"}, ws)
    assert "error" in out.lower()


def test_missing_file_is_a_tool_error_not_an_exception(ws):
    out = run("read_file", {"path": "nope.py"}, ws)
    assert "error" in out.lower()


def test_unknown_tool_is_a_tool_error(ws):
    assert "error" in run("frobnicate", {}, ws).lower()


def test_read_tools_need_no_approval():
    for name in ("list_dir", "read_file", "search"):
        assert needs_approval(name) is False


def test_search_glob_may_not_escape_the_workspace(ws, tmp_path):
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("K = 999\n", encoding="utf-8")
    try:
        out = run("search", {"pattern": "K = \\d+", "glob": "../*"}, ws)
        assert "outside_secret" not in out
        assert "999" not in out
    finally:
        outside.unlink()


def test_read_file_bad_limit_returns_error_not_exception(ws):
    out = run("read_file", {"path": "top.txt", "limit": "abc"}, ws)
    assert "error" in out.lower()


def test_illegal_windows_char_path_returns_error_not_exception(ws):
    out = run("read_file", {"path": "a|b"}, ws)
    assert "error" in out.lower()


def test_large_file_is_refused(ws):
    big = ws / "big.txt"
    big.write_text("x" * 2_000_001, encoding="utf-8")
    out = run("read_file", {"path": "big.txt"}, ws)
    assert "error" in out.lower() and "too large" in out.lower()


def test_write_file_creates_parents(ws):
    out = run("write_file", {"path": "new/dir/f.txt", "content": "yo"}, ws)
    assert "wrote" in out
    assert (ws / "new" / "dir" / "f.txt").read_text(encoding="utf-8") == "yo"


def test_write_file_refuses_escape(ws):
    out = run("write_file", {"path": "../evil.txt", "content": "x"}, ws)
    assert "error" in out.lower()


def test_edit_file_replaces_exactly_one_match(ws):
    out = run("edit_file", {"path": "pkg/mod.py", "old": "K = 32",
                            "new": "K = 24"}, ws)
    assert "edited" in out
    assert "K = 24" in (ws / "pkg" / "mod.py").read_text(encoding="utf-8")


def test_edit_file_errors_on_zero_and_many_matches(ws):
    assert "0 matches" in run("edit_file", {"path": "pkg/mod.py",
                                            "old": "absent", "new": "x"}, ws)
    (ws / "dup.txt").write_text("a\na\n", encoding="utf-8")
    assert "2 matches" in run("edit_file", {"path": "dup.txt",
                                            "old": "a", "new": "b"}, ws)


def test_edit_file_match_errors_say_how_to_recover(ws):
    # The July 13 sessions show the model retrying failed edits blindly:
    # the error itself must carry the recovery move.
    zero = run("edit_file", {"path": "pkg/mod.py", "old": "absent",
                             "new": "x"}, ws)
    assert "re-read" in zero.lower()
    (ws / "dup.txt").write_text("a\na\n", encoding="utf-8")
    many = run("edit_file", {"path": "dup.txt", "old": "a", "new": "b"}, ws)
    assert "unique" in many.lower()


def test_read_file_offset_past_the_end_is_an_explicit_error(ws):
    # "(empty)" reads like a legitimate empty window and the model re-reads
    # forever; a real error names the file length so it can correct.
    out = run("read_file", {"path": "top.txt", "offset": 50}, ws)
    assert "error" in out.lower() and "2 lines" in out


def test_run_command_captures_exit_and_output(ws):
    out = run("run_command", {"command": "echo hello"}, ws)
    assert "exit 0" in out and "hello" in out


def test_run_command_nonzero_exit_is_reported_not_raised(ws):
    out = run("run_command", {"command": "exit 3"}, ws)
    assert "exit 3" in out


def test_run_command_timeout_is_a_tool_error(ws):
    out = run("run_command", {"command": "ping -n 30 127.0.0.1",
                              "timeout": 1}, ws)
    assert "timed out" in out


def test_long_output_is_truncated(ws):
    (ws / "big.txt").write_text("x" * 20000, encoding="utf-8")
    out = run("read_file", {"path": "big.txt"}, ws)
    assert len(out) <= 8100 and out.endswith("[truncated]")


def test_run_command_timeout_bounds_wall_clock(ws):
    # Regression: on Windows, subprocess.run(shell=True, timeout=N) only
    # kills cmd.exe on TimeoutExpired -- the grandchild (ping.exe) keeps the
    # stdout pipe open and a naive communicate() blocks until it exits on
    # its own (~29s for a 30s ping even with timeout=1).
    start = time.monotonic()
    out = run("run_command", {"command": "ping -n 30 127.0.0.1",
                              "timeout": 1}, ws)
    elapsed = time.monotonic() - start
    assert elapsed < 10, "timeout did not bound wall-clock latency: {}s".format(elapsed)
    assert "timed out" in out


def test_run_command_undecodable_output_does_not_crash(ws):
    # Regression: on a cp1252 host, a child writing a byte invalid in that
    # codec (e.g. 0x81) used to crash subprocess's background reader thread;
    # the tool silently reported "exit 0" with the output dropped entirely.
    command = '"{}" -c "import sys; sys.stdout.buffer.write(bytes([0x81]))"'.format(
        sys.executable)
    out = run("run_command", {"command": command}, ws)
    assert "exit 0" in out
    assert len(out) > len("exit 0\n")


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe-specific failure mode")
def test_run_command_multiline_is_rejected_with_recovery(ws):
    # July 14 session: cmd.exe executes only the FIRST line of a multi-line
    # command, so every `python -c "\n..."` returned exit 0 with no output
    # and the model retried variants for ~15 iterations. The error must
    # carry the recovery move (write a script, then run it).
    out = run("run_command", {"command": "python -c \"\nprint('x')\""}, ws)
    assert "error" in out.lower()
    assert "first line" in out.lower()
    assert "write_file" in out


@pytest.mark.skipif(os.name != "nt", reason="OEM codepage is a Windows thing")
def test_run_command_decodes_oem_output(ws):
    # `dir` and friends emit the OEM codepage (cp850 here); decoding as
    # utf-8 turned every accent into U+FFFD mojibake that wastes context.
    command = '"{}" -c "import sys; sys.stdout.buffer.write(bytes([0x82]))"'.format(
        sys.executable)
    out = run("run_command", {"command": command}, ws)
    assert "é" in out  # 0x82 is é in cp850


def test_run_command_empty_output_is_labeled(ws):
    # "exit 0" alone reads like a success that produced something; the
    # label lets the model see that nothing came out.
    out = run("run_command", {"command": "exit 0"}, ws)
    assert "(no output)" in out


def test_run_command_refuses_to_kill_python_by_image_name(ws):
    # July 20 session (c_1a722191): the agent was installing ComfyUI (a python
    # server) and cleaned up "stale" processes with `taskkill /F /IM
    # python.exe`. The factory web server IS python.exe and the agent turn
    # runs inside it, so the tool killed its own host: 7/7 kill commands in
    # that session have no tool result, the stream just died.
    out = run("run_command", {"command": "taskkill /F /IM python.exe 2>nul"}, ws)
    assert "error" in out.lower()
    assert "python.exe" in out  # names what it refused
    assert "8188" in out or "port" in out.lower()  # carries the recovery move


def test_run_command_refuses_to_kill_its_own_pid(ws):
    # Same session, second shape: `tasklist | findstr python` returned exactly
    # one PID -- the server's -- and the agent killed it by number.
    out = run("run_command",
              {"command": "taskkill /F /PID {}".format(os.getpid())}, ws)
    assert "error" in out.lower()
    assert str(os.getpid()) in out


def test_run_command_refuses_self_kill_inside_a_chain(ws):
    # `taskkill /F /PID <self> & curl ...` was one of the seven: the chain
    # must be refused whole, not partially executed.
    out = run("run_command",
              {"command": "taskkill /F /PID {} 2>nul & echo done".format(
                  os.getpid())}, ws)
    assert "error" in out.lower()


def test_run_command_still_kills_other_processes(ws):
    # The guard is about self-destruction, not about killing processes: a PID
    # that is not ours goes through to taskkill (which then reports its own
    # "not found").
    out = run("run_command", {"command": "taskkill /F /PID 999999"}, ws)
    assert "refuse" not in out.lower()


@pytest.mark.parametrize("command", [
    # Session c_94deb1df (2026-07-20) deleted the index lock to get past a
    # git error -- that is how an interrupted operation corrupts the index.
    "del C:\\Users\\me\\local-factory\\.git\\index.lock && git add -A",
    "rm -f .git/index.lock",
    # CLAUDE.md already forbids these for AI sessions; here they become
    # executable instead of declarative.
    "git stash",
    "git reset --hard origin/main",
    "git push --force origin main",
    "git push -f",
    "git clean -fd",
])
def test_run_command_refuses_destructive_git(ws, command):
    out = run("run_command", {"command": command}, ws)
    assert "error" in out.lower()
    assert "refused" in out.lower()


@pytest.mark.parametrize("command", [
    "git add -A",              # sweeping, but harmless on the agent's branch
    "git commit -m 'wip'",
    "git push origin agent/20260722",
    "git reset HEAD~1",        # soft reset keeps the work
    "git clean --dry-run",
])
def test_run_command_leaves_ordinary_git_alone(ws, command):
    # The guard is about destruction, not about git. False positives here
    # would paralyse the agent on its own branch.
    out = run("run_command", {"command": command}, ws)
    assert "refused" not in out.lower()


def test_run_command_marks_the_agent_origin_in_the_child_env(ws):
    # Git hooks tell the local agent from the operator by this marker: the
    # agent may not commit on main, the operator may.
    out = run("run_command", {"command": "echo %FACTORY_AGENT%"
                              if os.name == "nt" else "echo $FACTORY_AGENT"},
              ws)
    assert "1" in out


def test_write_tools_need_approval():
    for name in ("write_file", "edit_file", "run_command"):
        assert needs_approval(name) is True


# ---- search: the two real defects (audited 2026-07-27 on a single session:
# 15 `error: search failed:` with an EMPTY reason, and minutes per call).
# `sorted(root.rglob("*"))` materialised the whole tree -- 200k+ paths, 2.7 GB
# of .git -- BEFORE the first match, since SKIP_DIRS only filtered afterwards;
# then read_text() on models/gguf/*.gguf raised MemoryError, whose str() is "".

def test_search_never_descends_into_skipped_directories(ws):
    junk = ws / ".git" / "objects"
    junk.mkdir(parents=True)
    (junk / "deadbeef").write_text("K = 32\n", encoding="utf-8")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "dep.py").write_text("K = 32\n", encoding="utf-8")
    out = run("search", {"pattern": "K = 32"}, ws)
    assert "pkg/mod.py" in out
    assert ".git" not in out and "node_modules" not in out


def test_search_skips_files_too_large_to_read(ws):
    big = ws / "model.bin"
    big.write_bytes(b"\x00" * (agent_tools.SEARCH_MAX_FILE_BYTES + 1))
    out = run("search", {"pattern": "zzz_nothing"}, ws)
    assert "no matches" in out          # skipped, not crashed


def test_search_skips_binary_files(ws):
    (ws / "blob.dat").write_bytes(b"K = 32\x00\x01\x02binary")
    out = run("search", {"pattern": "K = 32"}, ws)
    assert "blob.dat" not in out


def test_search_failures_always_name_a_reason(ws, monkeypatch):
    # The empty `search failed:` came from MemoryError, whose str() is "".
    def boom(*a, **kw):
        raise MemoryError()
    # _HANDLERS captures the function at import, so patch the table.
    monkeypatch.setitem(agent_tools._HANDLERS, "search", boom)
    out = run("search", {"pattern": "x"}, ws)
    assert out.startswith("error:")
    assert out.strip() not in ("error: search failed:", "error: search failed")
    assert "MemoryError" in out


def test_search_stops_at_the_deadline_and_says_so(ws, monkeypatch):
    monkeypatch.setattr(agent_tools, "SEARCH_DEADLINE_S", 0.0)
    out = run("search", {"pattern": "K"}, ws)
    assert "search stopped after" in out


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                   check=True)


@pytest.fixture
def git_ws(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text("venv/\n", encoding="utf-8")
    (tmp_path / "kept.py").write_text("MARKER = 1\n", encoding="utf-8")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "junk.py").write_text("MARKER = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "kept.py", ".gitignore")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def test_search_honours_gitignore_instead_of_a_blacklist(git_ws):
    out = run("search", {"pattern": "MARKER"}, git_ws)
    assert "kept.py" in out
    assert "venv" not in out


def test_search_still_sees_a_file_the_agent_just_wrote(git_ws):
    # Untracked but not ignored: the agent must be able to find what it just
    # created, so --others --exclude-standard rather than --cached alone.
    (git_ws / "brand_new.py").write_text("MARKER = 2\n", encoding="utf-8")
    out = run("search", {"pattern": "MARKER = 2"}, git_ws)
    assert "brand_new.py" in out


def test_search_falls_back_to_a_bounded_walk_without_git(ws):
    out = run("search", {"pattern": r"K = \d+"}, ws)
    assert "pkg/mod.py:1" in out


# ---- truncation must name its own recovery (2026-07-27). app.js is 1127
# lines; read_file returned ~200 of them and "[truncated]", never mentioning
# offset. The session re-read that same file 29 times.

def _long_file(ws, lines=900):
    (ws / "long.py").write_text(
        "".join("line{}\n".format(i) for i in range(lines)), encoding="utf-8")
    return ws / "long.py"


def test_a_clipped_read_says_how_to_see_the_rest(ws):
    _long_file(ws)
    out = run("read_file", {"path": "long.py"}, ws)
    assert "offset" in out
    assert "900" in out                      # the real length, so it can plan


def test_a_clipped_read_stops_on_a_whole_line(ws):
    _long_file(ws)
    out = run("read_file", {"path": "long.py"}, ws)
    body = [l for l in out.splitlines() if "\t" in l]
    assert body[-1].split("\t")[1].startswith("line")   # never cut mid-line


def test_offset_continues_where_the_clip_stopped(ws):
    _long_file(ws)
    first = run("read_file", {"path": "long.py"}, ws)
    last_shown = max(int(l.split("\t")[0]) for l in first.splitlines()
                     if "\t" in l)
    nxt = run("read_file", {"path": "long.py", "offset": last_shown + 1}, ws)
    assert "{}\tline{}".format(last_shown + 1, last_shown) in nxt


def test_a_short_read_says_nothing_extra(ws):
    out = run("read_file", {"path": "pkg/mod.py"}, ws)
    assert "offset" not in out


# ---- syntax is checked at write time (2026-07-27). Sessions shipped files
# with syntax errors that nothing caught; a tool error is read, a guideline
# in the system prompt is not.

def test_write_file_reports_a_python_syntax_error(ws):
    out = run("write_file", {"path": "bad.py", "content": "def f(:\n"}, ws)
    assert out.startswith("error:")
    assert "SyntaxError" in out or "syntax" in out.lower()
    assert "bad.py" in out


def test_write_file_still_writes_so_the_agent_can_repair_it(ws):
    run("write_file", {"path": "bad.py", "content": "def f(:\n"}, ws)
    assert (ws / "bad.py").exists()      # editable, not lost


def test_write_file_accepts_valid_python(ws):
    out = run("write_file", {"path": "ok.py", "content": "x = 1\n"}, ws)
    assert not out.startswith("error:")


def test_write_file_ignores_languages_it_cannot_check(ws):
    out = run("write_file", {"path": "notes.md", "content": "def f(:\n"}, ws)
    assert not out.startswith("error:")


def test_edit_file_reports_a_syntax_error_it_introduced(ws):
    run("write_file", {"path": "ok.py", "content": "x = 1\ny = 2\n"}, ws)
    out = run("edit_file", {"path": "ok.py", "old": "y = 2", "new": "y = ("}, ws)
    assert out.startswith("error:")
    assert "syntax" in out.lower() or "SyntaxError" in out


def test_edit_file_stays_silent_when_the_result_is_valid(ws):
    run("write_file", {"path": "ok.py", "content": "x = 1\n"}, ws)
    out = run("edit_file", {"path": "ok.py", "old": "x = 1", "new": "x = 2"}, ws)
    assert not out.startswith("error:")

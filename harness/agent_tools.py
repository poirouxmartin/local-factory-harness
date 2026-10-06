"""Workspace-confined tools for the chat agent.

Every tool returns a string the model reads. Failures (missing file, escaped
path, bad regex) are part of that string, never exceptions: the loop must
keep going so the model can correct itself. Outputs are truncated: num_ctx
is 16k and a fat `git log` must not evict the conversation.
"""
import fnmatch
import json
import time
import os
import re
import subprocess
from pathlib import Path

MAX_OUTPUT = 8000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "models", "dist", "build", ".mypy_cache", ".pytest_cache"}
# A file bigger than this is not something a regex over lines should touch.
# models/gguf/*.gguf is why: read_text() on multi-GB weights raised MemoryError
# with an empty message, surfacing as `error: search failed:` (2026-07-27).
SEARCH_MAX_FILE_BYTES = 2_000_000
# Partial results beat a tool call the operator has to interrupt. One session
# lost ten minutes per search before this existed.
SEARCH_DEADLINE_S = 10.0
# Under MAX_OUTPUT, so a clipped read has room for the line telling the model
# how to continue. run()'s blind cut would otherwise eat that line too.
READ_BUDGET_CHARS = 7600
READ_TOOLS = {"list_dir", "read_file", "search"}

TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List entries of a directory (relative to the workspace).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "directory, '.' for the root"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file with line numbers.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "1-based first line"},
            "limit": {"type": "integer", "description": "max lines"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search",
        "description": "Regex search across workspace text files; returns path:line matches.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Python regex"},
            "glob": {"type": "string", "description": "filename filter like *.py"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a text file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace one exact occurrence of `old` with `new` in a file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"},
            "new": {"type": "string"}},
            "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a single-line shell command in the workspace; returns exit code and output. For multi-line code, write a script file first and run it.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "seconds, default 60"}},
            "required": ["command"]}}},
]


def needs_approval(name):
    return name not in READ_TOOLS


def _truncate(text):
    if len(text) > MAX_OUTPUT:
        return text[:MAX_OUTPUT] + "\n[truncated]"
    return text


def _resolve(workspace, path):
    """Path under the workspace root, or None when it escapes."""
    root = Path(workspace).resolve()
    p = Path(path)
    p = (p if p.is_absolute() else root / p).resolve()
    if p == root or root in p.parents:
        return p
    return None


def _list_dir(workspace, path="."):
    p = _resolve(workspace, path)
    if p is None:
        return "error: path escapes the workspace"
    if not p.is_dir():
        return "error: not a directory: {}".format(path)
    lines = []
    for child in sorted(p.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
        if child.is_dir():
            lines.append(child.name + "/")
        else:
            lines.append("{}  ({} bytes)".format(child.name, child.stat().st_size))
    return "\n".join(lines) or "(empty)"


def _read_file(workspace, path, offset=1, limit=2000):
    p = _resolve(workspace, path)
    if p is None:
        return "error: path escapes the workspace"
    if not p.is_file():
        return "error: no such file: {}".format(path)
    size = p.stat().st_size
    if size > 2_000_000:
        return "error: file too large ({} bytes)".format(size)
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = max(1, int(offset or 1))
    limit = max(1, int(limit or 2000))
    window = lines[offset - 1:offset - 1 + limit]
    if not window and lines:
        return "error: offset {} is past the end of {} ({} lines)".format(
            offset, path, len(lines))
    # Clip here rather than let run() cut the string at MAX_OUTPUT. That cut
    # landed mid-line and said only "[truncated]" -- so app.js, over a thousand
    # lines, came back as ~200 of them with no hint that `offset` existed, and
    # one session re-read that same file 29 times trying to reach the end
    # (2026-07-27).
    out, size, shown = [], 0, 0
    for i, line in enumerate(window):
        rendered = "{}\t{}".format(offset + i, line)
        if size + len(rendered) + 1 > READ_BUDGET_CHARS and out:
            break
        out.append(rendered)
        size += len(rendered) + 1
        shown += 1
    if not out:
        return "(empty)"
    last = offset + shown - 1
    if last < len(lines):
        out.append("[lignes {}-{} sur {} ; rappelle read_file avec offset={} "
                   "pour la suite]".format(offset, last, len(lines), last + 1))
    return "\n".join(out)


def _candidates(root):
    """Files worth searching, git first.

    `git ls-files --cached --others --exclude-standard` is tracked files PLUS
    untracked ones that .gitignore does not exclude -- so a file the agent just
    wrote is searchable, while `venv/`, `node_modules/` and `models/` never
    are. No blacklist to keep in step with reality: .gitignore already is that
    list, and it is the one the project actually maintains.

    Outside a repo, a pruned and depth-capped walk. Never an unbounded one.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root), capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            for rel in out.stdout.splitlines():
                if rel:
                    yield root / rel
            return
    except (OSError, subprocess.SubprocessError):
        pass
    for dirpath, dirnames, filenames in os.walk(root):
        # In place, so os.walk never descends: filtering after the fact is
        # what made .git cost minutes.
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


def _search(workspace, pattern, glob="*"):
    """Regex scan of the workspace's text files.

    Bounded on every axis, because the previous version was bounded on none.
    Measured 2026-07-27 on one session: `sorted(root.rglob(glob or "*"))`
    materialised the ENTIRE tree before the first match -- over 200,000 paths
    here, 2.7 GB of `.git` among them, since SKIP_DIRS was only applied
    afterwards -- and then read_text() every file it found, including
    multi-GB `.gguf` weights. That raised MemoryError, whose str() is empty,
    which is where the 15 `error: search failed:` with no reason came from.

    Bounding the cost was not enough on its own: pruned and deadlined, a walk
    of this repo still scanned 1534 files in 10 s and never reached `harness/`,
    so it answered "no matches" for a symbol that was plainly there. Cheap and
    wrong beats slow and right for nobody. `_candidates` asks git what belongs
    to the project instead -- 99 Python files rather than 1534.
    """
    root = Path(workspace).resolve()
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return "error: bad regex: {}".format(e)
    matcher = glob or "*"
    hits, scanned, started = [], 0, time.monotonic()
    for real in _candidates(root):
        if not fnmatch.fnmatch(real.name, matcher):
            continue
        try:
            if real.stat().st_size > SEARCH_MAX_FILE_BYTES:
                continue
            blob = real.read_bytes()
        except OSError:
            continue
        if b"\x00" in blob[:8192]:
            continue                        # binary: not searchable text
        scanned += 1
        try:
            rel = real.relative_to(root).as_posix()
        except ValueError:
            continue
        for i, line in enumerate(
                blob.decode("utf-8", "replace").splitlines(), 1):
            if rx.search(line):
                hits.append("{}:{}: {}".format(rel, i, line.strip()))
                if len(hits) >= 50:
                    return _truncate("\n".join(hits)
                                     + "\n[more matches elided]")
        if time.monotonic() - started >= SEARCH_DEADLINE_S:
            partial = "\n".join(hits) if hits else "no matches yet"
            return _truncate(
                "{}\n[search stopped after {:.0f}s having scanned {} files -- "
                "narrow it with a glob such as '*.py', or a more specific "
                "pattern]".format(partial, SEARCH_DEADLINE_S, scanned))
    return "\n".join(hits) or "no matches"


def _syntax_error(path, text):
    """The compiler's complaint about `text`, or None.

    Only for languages we can check with the stdlib; everything else passes.
    A guideline in the system prompt asking the model to check its own syntax
    is forgotten, and audited sessions shipped files that did not parse. A
    tool error is read -- the refusal of step_done proved that much.
    """
    if path.endswith(".py"):
        try:
            compile(text, path, "exec")
        except SyntaxError as e:
            return "{}: line {}: {}".format(
                type(e).__name__, e.lineno, e.msg)
        except ValueError as e:                 # e.g. NUL bytes in source
            return "ValueError: {}".format(e)
    elif path.endswith(".json"):
        try:
            json.loads(text)
        except ValueError as e:
            return "JSONDecodeError: {}".format(e)
    return None


def _write_file(workspace, path, content):
    p = _resolve(workspace, path)
    if p is None:
        return "error: path escapes the workspace"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    # Written first, then judged: a file the model can re-edit is worth more
    # than a refusal that leaves it with nothing on disk to fix.
    bad = _syntax_error(path, content)
    if bad:
        return ("error: wrote {} but it does not parse -- {}. Fix it before "
                "moving on.".format(path, bad))
    return "wrote {} bytes to {}".format(p.stat().st_size, path)


def _edit_file(workspace, path, old, new):
    p = _resolve(workspace, path)
    if p is None:
        return "error: path escapes the workspace"
    if not p.is_file():
        return "error: no such file: {}".format(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    n = text.count(old)
    if n == 0:
        return ("error: 0 matches for old text in {} (need exactly 1); "
                "re-read the section with read_file and copy the text "
                "verbatim, without the line-number prefixes").format(path)
    if n > 1:
        return ("error: {} matches for old text in {} (need exactly 1); "
                "include surrounding lines to make it unique").format(n, path)
    updated = text.replace(old, new, 1)
    p.write_text(updated, encoding="utf-8")
    bad = _syntax_error(path, updated)
    if bad:
        return ("error: edited {} but it no longer parses -- {}. Fix it "
                "before moving on.".format(path, bad))
    return "edited {}".format(path)


def _kill_process_tree(proc):
    """Kill proc and any children it spawned (e.g. via shell=True)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True)
    else:
        proc.kill()


def _decode_output(data):
    """cmd.exe children mix codecs: python prints utf-8, `dir` prints the
    OEM codepage (cp850 on a French host). utf-8 first, OEM as fallback --
    mojibake in a tool result wastes context and misleads the model."""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        enc = "oem" if os.name == "nt" else "utf-8"
        return data.decode(enc, errors="replace")


# The factory web server is `py -3 harness/factory_web.py` and the agent turn
# runs inside it, so any kill aimed at "python" or at our own PID takes the
# server down mid-tool-call: the SSE stream dies, the tool result is never
# written, and the session is left holding a dangling tool_call. That is
# exactly what happened 7/7 times in session c_1a722191 (2026-07-20), where
# the agent was installing ComfyUI -- itself a python server -- and kept
# "cleaning up stale python processes".
_KILL_BY_IMAGE = re.compile(
    r"taskkill\b[^\n]*?/im\s+\"?(?:pythonw|python[0-9]*|pyw|py)(?:\.exe)?\b"
    r"|(?:pkill|killall)\b[^\n]*?\bpython",
    re.IGNORECASE)
_KILL_BY_PID = re.compile(
    r"(?:taskkill\b[^\n]*?/pid\s+|stop-process\b[^\n]*?-id\s+)(\d+)",
    re.IGNORECASE)
_KILL_BY_NAME_PS = re.compile(
    r"stop-process\b[^\n]*?-name\s+\"?(py|python[0-9.]*|pythonw)\b",
    re.IGNORECASE)
_SELF_KILL_HINT = (
    "error: refused -- this would kill the factory server itself. The web "
    "server is a python process and your tool call runs inside it, so "
    "killing python by image name or killing its PID ends the session "
    "mid-command. Target the process you actually mean by its port instead, "
    "e.g. `netstat -ano | findstr :8188` to get the ComfyUI PID, then kill "
    "that PID.")


def _self_kill_reason(command):
    """What in `command` would kill our own process, or None."""
    for rx in (_KILL_BY_IMAGE, _KILL_BY_NAME_PS):
        m = rx.search(command)
        if m:
            return m.group(0).strip()
    for pid in _KILL_BY_PID.findall(command):
        if int(pid) == os.getpid():
            return pid
    return None


# CLAUDE.md forbids these for AI sessions; the 2026-07-20 session showed the
# rule needs teeth. Deleting anything under .git/ is how that session got past
# a lock error -- and how an interrupted git operation corrupts an index.
_DESTRUCTIVE = (
    (re.compile(r"\b(del|erase|rm|remove-item)\b[^\n]*[\\/]?\.git[\\/]",
                re.IGNORECASE),
     "deleting files under .git/ (a lock error means a git process is still "
     "running or died; wait, or ask the operator -- do not remove the lock)"),
    (re.compile(r"\bgit\s+stash\b", re.IGNORECASE),
     "git stash (a stash already destroyed a working tree here once)"),
    (re.compile(r"\bgit\s+reset\b[^\n]*--hard", re.IGNORECASE),
     "git reset --hard (use `git reset` without --hard to keep the work)"),
    (re.compile(r"\bgit\s+push\b[^\n]*(--force|\s-f\b)", re.IGNORECASE),
     "git push --force"),
    (re.compile(r"\bgit\s+clean\b[^\n]*(?<!--dry-run)\s-\w*f", re.IGNORECASE),
     "git clean -f (it deletes untracked work with no way back)"),
)


def _destructive_reason(command):
    for rx, label in _DESTRUCTIVE:
        if rx.search(command):
            return label
    return None


def _run_command(workspace, command, timeout=60):
    # shell=True is the point of this tool: the model composes a full shell
    # line (pipes, redirects) and the approve mode / operator gates it.
    command = (command or "").strip()
    target = _self_kill_reason(command)
    if target:
        return "{} (refused target: {})".format(_SELF_KILL_HINT, target)
    destructive = _destructive_reason(command)
    if destructive:
        return "error: refused -- {}.".format(destructive)
    if os.name == "nt" and "\n" in command:
        # cmd.exe executes only the first line; the rest is silently dropped
        # and the model reads "exit 0" as success (July 14 session: ~15
        # iterations lost on multi-line `python -c`).
        return ("error: multi-line commands are not supported: cmd.exe "
                "executes only the first line and silently drops the rest. "
                "Write the code to a file with write_file, then run it "
                "(e.g. `python script.py`).")
    timeout = 60 if timeout is None else int(timeout)
    timeout = max(1, timeout)
    # Popen (not run) so a TimeoutExpired lets us kill the whole process
    # tree: on Windows, run(timeout=...) only kills the cmd.exe shell and
    # communicate() then blocks until the grandchild (e.g. ping.exe) exits
    # on its own, defeating the timeout entirely.
    # The marker git hooks read to tell the agent from the operator: the
    # agent may not commit on main, the operator may. A marker, not a
    # boundary -- it makes the tool honest about its origin.
    env = dict(os.environ, FACTORY_AGENT="1")
    proc = subprocess.Popen(command, shell=True, cwd=str(workspace), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return "error: command timed out after {}s".format(timeout)
    stdout, stderr = _decode_output(stdout), _decode_output(stderr)
    if not stdout and not stderr:
        return "exit {}\n(no output)".format(proc.returncode)
    out = "exit {}\n{}".format(proc.returncode, stdout)
    if stderr:
        out += "\nstderr:\n" + stderr
    return out


_HANDLERS = {"list_dir": _list_dir, "read_file": _read_file, "search": _search,
             "write_file": _write_file, "edit_file": _edit_file,
             "run_command": _run_command}


def run(name, arguments, workspace):
    fn = _HANDLERS.get(name)
    if fn is None:
        return "error: unknown tool: {}".format(name)
    try:
        return _truncate(fn(workspace, **dict(arguments or {})))
    except TypeError as e:
        return "error: bad arguments for {}: {}".format(name, e)
    except Exception as e:
        # Always name something. MemoryError's str() is empty, which is how
        # `error: search failed:` reached the model 15 times in one session
        # with no reason attached and nothing it could act on (2026-07-27).
        return "error: {} failed: {}".format(
            name, str(e) or type(e).__name__)

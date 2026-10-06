"""MCP server for the factory. Stdio, stateless, five tools.

There is no `mcp` SDK for Python 3.9, so the transport is hand-rolled: JSON-RPC
2.0, one message per line, exactly what the stdio transport specifies. That suits
the design -- the server writes spec files, spawns detached processes and reads
state files. It holds no state, so killing it costs nothing.

Spawned jobs get their stdout and stderr redirected into log.txt. A job that
inherits this server's pipes dies with it, or blocks forever once the pipe buffer
fills and nobody drains it. That single line is what makes "detached" true.

    py -3 harness/factory_mcp.py --jobs jobs --config factory.toml
"""
import argparse
import ast
import base64
import binascii
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import agent_tools
import carnet
import chat_context
import lessons
import llama_client
import mcp_registry
import project_map
import phases
import session_analysis
import session_audit
import session_log
import workspace_memory
from chat_store import ChatNotFound, ChatStore
import cloud_lane
import credentials
import openrouter_catalog
import providers
from factory_config import (ConfigError, load_cloud_catalog, load_llama_server,
                            load_mcp_servers, load_models, load_projects,
                            load_toolsets, local_config_path, model_options,
                            resolve_project, tomllib, validate_context_files,
                            validate_targets, validate_tests)
from gpu_lock import GpuLock
from job_store import (JobNotFound, JobStore, JobStoreError, ResultExists,
                       atomic_write_bytes)
from llama_server_manager import LlamaServerManager
from turn_runner import TurnCancelled

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_VERSION = "2024-11-05"

_STR = {"type": "string"}

TOOLS = [
    {
        "name": "delegate",
        "description": (
            "Hand a coding task to the local model ladder. Returns a job_id immediately; "
            "the job survives this session. The model may only write `target_files`, and "
            "never the tests -- they are materialized outside its worktree. The deliverable "
            "is a diff, never a commit. A job is green only when the project's own test "
            "suite still passes."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": dict(_STR, description="A project key from factory.toml."),
                "goal": dict(_STR, description="What the change must achieve."),
                "tests": {"type": "object", "additionalProperties": _STR,
                          "description": "path -> source. The judge. At least one test_*.py."},
                "target_files": {"type": "array", "items": _STR,
                                 "description": "Repo-relative files the model may rewrite."},
                "context_files": {"type": "array", "items": _STR,
                                  "description": "Repo-relative files the model may READ "
                                                 "but never write. Keep them few and small."},
                "start_stage": {"type": "integer",
                                "description": "Enter the ladder at this rung "
                                "(1=7b, 2=30b, 3=35b). Module-scale contracts "
                                "start at 2; small fixes at 1."},
                "edit_mode": {"type": "string", "enum": ["whole", "diff"],
                              "description": "How the model writes: whole-file "
                              "re-emission or SEARCH/REPLACE edits. Omit for "
                              "the auto rule (multi-file or >300 lines = diff)."},
            },
            "required": ["project", "goal", "tests", "target_files"],
        },
    },
    {
        "name": "job_status",
        "description": "Progress of a job: stage, attempt, tests passing. 'dead' means it "
                       "stopped heartbeating.",
        "inputSchema": {"type": "object", "properties": {"job_id": _STR},
                        "required": ["job_id"]},
    },
    {
        "name": "job_result",
        "description": "The verdict and the diff, once the job has finished.",
        "inputSchema": {"type": "object", "properties": {"job_id": _STR},
                        "required": ["job_id"]},
    },
    {
        "name": "job_log",
        "description": "Tail of the job's log.",
        "inputSchema": {"type": "object",
                        "properties": {"job_id": _STR, "tail": {"type": "integer"}},
                        "required": ["job_id"]},
    },
    {
        "name": "job_cancel",
        "description": "Kill a running job and release the GPU.",
        "inputSchema": {"type": "object", "properties": {"job_id": _STR},
                        "required": ["job_id"]},
    },
]


def detach_kwargs():
    """Cut the child loose from this process group, so our death is not its death."""
    if os.name == "nt":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def spawn_detached(job_dir, argv):
    """Never hand the child our pipes: it would die of EPIPE, or block on a full buffer."""
    log = open(Path(job_dir) / "log.txt", "ab")
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, close_fds=True,
                                cwd=str(ROOT), **detach_kwargs())
    finally:
        log.close()
    return proc.pid


def _kill(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, 15)


class ToolError(Exception):
    pass


# Enough for every real tag (name, name:tag, registry/name:tag) and nothing
# that could double as shell or path trickery.
MODEL_RE = re.compile(r"^[\w.:@/-]+$")


def _transport(model):
    """Which module serves this model.

    Two lanes, one contract. OpenRouter and llama-server speak the same OpenAI
    SSE protocol, tool calls included, so one call site serves both lanes --
    only the endpoint differs, and `_acquire_runtime` returns the matching one.
    ChatGPT's web app speaks none of it and is driven through a browser, which
    is why it gets a module rather than a row in the route table's transport.
    """
    return cloud_lane if cloud_lane.is_cloud(model) else llama_client

# One job directory format, several executants. spec.json's `kind` picks the
# runner; a spec without one predates kinds and is a delegation.
RUNNERS = {"delegate": "loop_job.py", "draft": "draft_job.py"}

MAX_TOOL_ITERATIONS = 20

ITERATION_STOPPED = ("tool iteration limit reached ({n} model turns for one "
                     "message). Send 'continue' to let the agent resume.")
DEGENERATE_STOPPED = ("generation stopped: the model was repeating itself "
                      "(degenerate output, usually a saturated context). The "
                      "partial reply is kept; rephrase or shrink the task to "
                      "resume.")
LENGTH_STOPPED = ("generation stopped: the reply hit the per-turn generation "
                  "cap (num_predict). The partial reply is kept; send "
                  "'continue' to let the agent resume.")
LENGTH_STOPPED_WITH_CALLS = (
    "the reply hit the per-turn generation cap (num_predict) after its tool "
    "call was already complete; the call still runs. Keep replies shorter if "
    "this repeats.")
# A thinking model can emit a tool call into the reasoning channel and leave
# only its closing tags as text -- no content, no structured call. That empty
# turn used to look like a final answer and stop the agent silently (session
# c_1a722191, msg#186). Re-prompt for a clean call instead, and give up after
# a few tries so a model that can't emit one doesn't loop forever on cap 0.
MALFORMED_CALL_RETRY = ("your last reply tried to call a tool but the call was "
                        "malformed (unbalanced <tool_call>/<function> tags and "
                        "no parseable arguments). Re-emit the call properly, or "
                        "answer the user in plain text.")
MALFORMED_CALL_STOPPED = ("generation stopped: the model kept emitting malformed "
                          "tool calls ({n} turns running). The partial reply is "
                          "kept; rephrase or shrink the task to resume.")
MALFORMED_RETRY_AT = 3
# What a stalled turn says to the model on every later turn, in place of what
# it actually wrote. The words stay on disk; only the wire is rewritten. See
# ChatStore.mark_stalled for why, and STALL_RETRY for what is done about it.
STALL_ON_WIRE = ("(this turn ended without the call it announced; the program "
                 "asked for the call again)")
# The turn ended in prose, announcing an action it never requested, while the
# plan still owes steps. Measured on session c_14f20c89 (2026-08-06, four times
# in thirteen minutes, every one of them AFTER the tail reminder shipped): the
# belief is not "there are no tools" -- all four stalls name edit_file and
# run_command correctly and put them in another turn ("dans ce tour", "au
# prochain tour ou les appels sont effectivement disponibles"). The head block
# and the reminder both answer the July sentence, "I have no access to a tool",
# and nothing answers this one. The second sentence below is what does.
#
# Wording under the rules chatgpt_web.py proved live: name the tools, say the
# block is a request, claim nothing about what the model may not do, and never
# ask it to suppress its own words -- told to emit only the block, it refused.
STALL_RETRY = ("your last reply announced the next action but did not request "
               "it. The turn you were waiting for is this one: the program is "
               "executing tools right now, and the call block is how you ask "
               "for one. Emit the call you just described, as the last thing "
               "in your reply.")
# What the operator sees. There is a cap on the relance to prevent infinite
# loops when a model repeatedly fails to emit the call it announced (observed with
# ChatGPT: it announces actions in prose but does not emit <function=...> calls).
# After MAX_STALL_RETRIES, the session hands back to the operator instead of looping.
STALL_NOTICE = ("relance {n}: the reply announced an action without requesting "
                "it and the plan is unfinished, so the agent was asked again. "
                "Stop ends this; after {limit} retries the session hands back.")
MAX_STALL_RETRIES = 3
# A stall is a turn that ANNOUNCED a next action and never requested it -- not
# any prose turn that happens to sit on an unfinished plan. Measured 2026-08-19
# on c_594feb80 (qwen3.6:35b): 4 of 6 relances were false positives -- two long
# reports ending in a question, a status report, a completion note. So the
# detection is the inverse of the old `has_meaningful_content`: a SHORT reply
# that names an intent to act next. A long reply is an answer, a question is a
# hand-back, and a completion note is a done turn -- each ends the turn as usual.
STALL_MAX_CHARS = 400
_ACTION_ANNOUNCE_RE = re.compile(
    r"\b(je\s+vais|je\s+vais\s+donc|je\s+lance|j'?ex[eé]cute|"
    r"j'?attaque|je\s+m'attaque|je\s+dois|je\s+commence|je\s+continue|"
    r"je\s+repr[ée]nds|je\s+m'appr[èe]te|je\s+proc[èe]de|"
    r"prochaine action|ensuite\s+je|je\s+patche|je\s+modifie|"
    r"je\s+v[eé]rifie|je\s+cr[ée]e|je\s+passe\s+(?:en|au)|"
    r"j'applique)\b", re.IGNORECASE)


def _announces_action(text):
    """A prose turn that announced the next action without requesting it.

    The old gate (`has_meaningful_content`) treated almost any prose as a stall
    the moment a plan step was open. A real announcement is short and states an
    intent to act next; a question or a long reply is the model answering.
    """
    text = (text or "").strip()
    if not text or len(text) > STALL_MAX_CHARS:
        return False
    if text.rstrip().endswith("?"):
        return False
    return bool(_ACTION_ANNOUNCE_RE.search(text))
# Running a completed call after a length stop removes what used to be an
# unconditional terminator, and `agent_max_iterations = 0` (the default) means
# no other bound. Past this many cut turns running, stop as before: a model cut
# on every turn is not making progress, it is thrashing.
LENGTH_WITH_CALLS_AT = 3
# Hitting the cap blind wastes the whole budget (July 14 session: twice).
# Told in advance, the model can conclude instead of being cut mid-plan.
ITERATION_WARNING_AT = 5
ITERATION_WARNING = ("note: only {n} tool iterations remain for this message. "
                     "Finish the task now, or summarize progress and answer "
                     "the user.")
# A failing result that scrolls off unremarked gets retried verbatim -- the
# carnet exists so that does not happen. Appended to any failing tool output
# (never to `note`/`wall`, whose own content is never a shell verdict) so the
# very next model turn has to reason about the failure instead of repeating it.
REFLECT_ANNOTATION = (
    "\n\n[carnet] Note pour toi: avant de retenter sur cette cible, dis en une "
    "ligne ton hypothese sur la cause et en quoi ta prochaine action differe de "
    "celle qui vient d'echouer. Si tu as tente plusieurs approches sur ce but, "
    "enregistre un log_wall.")


# Chat/agent sessions send the whole history back each turn. At the client
# default (16384) a long session silently overflows: the server drops the
# oldest tokens and replies degrade (observed on qwen3.6:35b -- same failure
# class as ADR-008). The 12 GB GPU already offloads the MoE experts to RAM, so
# a bigger KV cache costs speed, not a refusal -- and the 2026-07-15 bench
# (experiments/results/20260715_fa_kvq8_bench.md) measured 64k at only ~7%
# slower than 32k: the bottleneck is the CPU-side experts, not attention. 128k
# stays per-session until it is benched.
CHAT_NUM_CTX = 65536

# Drag-n-drop uploads: a bare filename only, and a cap that keeps a dropped
# binary from filling the disk before anyone can react.
UPLOAD_MAX_BYTES = 5 * 1024 * 1024
_UPLOAD_NAME = re.compile(r"^[A-Za-z0-9][\w .()-]{0,120}$")


def _prompt_tokens(met):
    """How many tokens the last prompt really weighed.

    `prompt_count` counts the whole prompt; `prefill_count` counts only what
    the server had to evaluate, which the KV cache drives to ~0 on a stable
    prefix (measured: prompt_n 1 for a 1103-token prompt). Old sessions have
    no prompt_count, so fall back -- an under-count is what they always had.
    """
    return met.get("prompt_count") or met.get("prefill_count") or 0


def _overflowed(session, num_ctx):
    """Did the previous turn REALLY fill the window? The server's own counters
    cannot lie the way char-based estimates can; at 90 % the next turn forces
    a compaction instead of trusting the estimate."""
    for m in reversed(session.get("messages") or []):
        met = m.get("metrics") or {}
        if _prompt_tokens(met):
            used = _prompt_tokens(met) + (met.get("eval_count") or 0)
            return used >= 0.9 * num_ctx
    return False


def _usage(metrics, info=None, compacted=False, num_ctx=None):
    """What the UI needs to see *why* a reply looks the way it does:
    prompt+output token counts against the window, how generation ended
    ("length" means the reply was cut, not finished), and what context
    management did this turn."""
    out = {k: metrics.get(k) for k in
           ("tokens_per_s", "prefill_tokens_per_s", "ttft_s", "eval_count",
            "prefill_count", "prompt_count", "done_reason", "thinking_s",
            "writing_s", "thinking_chars", "content_chars",
            # None on a local turn, a number on a cloud one. It has to cross
            # this whitelist or the operator cannot see a session spend money
            # -- on a paid model the cost display IS the brake, and a field
            # dropped here is a brake that does not exist.
            "cost_usd")}
    out["num_ctx"] = num_ctx or CHAT_NUM_CTX

    ctx_info = info or {}
    eval_count = metrics.get("eval_count", 0)
    used_tokens = _prompt_tokens(metrics) + eval_count
    ctx_window = num_ctx or CHAT_NUM_CTX
    usage_pct = int(100 * used_tokens / ctx_window) if ctx_window else 0
    
    out["context"] = {
        "evicted": ctx_info.get("evicted", 0),
        "dropped": ctx_info.get("dropped", 0),
        "compacted": compacted,
        "usage_pct": usage_pct,
        "used_tokens": used_tokens,
        "num_ctx": ctx_window,
    }
    
    if metrics.get("stopped"):
        # The stream layer cut a degenerate generation (RepetitionGuard).
        out["stopped"] = metrics["stopped"]
    return out


def _ensure_git(workspace):
    """agent.md rule 7 (commit after every validated step) was dead letter in
    the July 14 sandbox: the workspace had no git repo, so weeks of agent work
    piled up uncommitted. A directory not already inside a repo gets one."""
    inside = subprocess.run(["git", "-C", workspace, "rev-parse",
                             "--is-inside-work-tree"], capture_output=True)
    if inside.returncode != 0:
        subprocess.run(["git", "-C", workspace, "init", "-q", "-b", "main"],
                       capture_output=True)
# AGENTS.md first: it is the name the ecosystem settled on, and it is a
# different filename from AGENT.md rather than a case variant, so leaving it
# out meant those workspaces ran with no instructions at all.
INSTRUCTION_FILES = ("AGENTS.md", "AGENT.md", "agent.md", "CLAUDE.md")
MEMORY_FILE = "MEMORY.md"


def _read_memory(workspace):
    """`<workspace>/MEMORY.md`, or "" -- a project without one is the normal
    case, not an error."""
    try:
        return (Path(workspace) / MEMORY_FILE).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""


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


def _map_entry(path, text, blob):
    return {"path": path, "lines": text.count("\n") + 1,
            "doc": _doc_of(path, text), "blob": blob}


def _git_entries(workspace):
    """(entries, sources, blobs) from `git ls-files -s`, or None outside a
    repo.

    git is the only authority on what belongs to a project: the first
    prototype of this map used a hand-maintained blacklist, forgot `venv/`,
    and indexed 22122 files for 839k tokens -- the same failure mode as the
    unbounded `rglob` in agent_tools._search.
    """
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
        entries.append(_map_entry(path, text, fields[1]))
    return entries, sources, blobs


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


def _walk_entries(workspace):
    """Bounded fallback for a workspace without git. Never unbounded: an
    unbounded walk is exactly the bug this map exists to stop paying for."""
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
            entries.append(_map_entry(path, text, ""))
    return entries, sources, {}


def _exists(workspace):
    """A glob probe confined to `workspace`: `pattern` matches a path inside it.

    Carnet injects this as its `exists` callable so the pure module does no I/O.
    A pattern that escapes the workspace -- `..`, an absolute path -- resolves
    outside `root` and is False, never an exception (the anti-self-delusion of
    spec A.2 must not become a crash)."""
    root = Path(workspace).resolve()

    def probe(pattern):
        try:
            for match in glob.glob(str(root / pattern), recursive=True):
                if Path(match).resolve().is_relative_to(root):
                    return True
        except OSError:
            pass
        return False

    return probe


def _already_ticked(plan, i):
    """What to tell an agent that ticks a step which is already ticked: what is
    left, or -- when nothing is -- that the way out is to answer, not to call
    another tool."""
    left = [str(n) for n, step in enumerate(plan["steps"], 1)
            if not step["done"] and n != i]
    if left:
        return ("step {} was already ticked, nothing to do. Steps still open: "
                "{}. Work on one of those.".format(i, ", ".join(left)))
    return ("step {} was already ticked, and every step of the plan is done. "
            "Do not call another tool: answer the operator now, saying what "
            "you changed and what you could not do.".format(i))


def _changed_since(workspace, since):
    """The probe the HARNESS ticks with: the pattern matches a path inside the
    workspace that was written after `since`.

    Existence alone cannot tick a step, and that is not pedantry. Models write
    `done_when: file:backend/server.js` for a step that MODIFIES a file which
    already exists, so an existence probe ticks it the instant the plan is set
    -- work marked done before any was done, which is precisely the
    self-delusion A.2 exists to catch (measured live 2026-07-25: three of four
    steps would have ticked on a clean tree).

    `verify_claim` deliberately keeps the plain existence probe: there it is
    the AGENT claiming, and refusing a true claim would block real work, while
    accepting a generous one only costs a checkmark."""
    root = Path(workspace).resolve()
    present = _exists(workspace)

    def changed(pattern):
        if not present(pattern):
            return False
        if not since:
            return True  # no plan timestamp: fall back to existence
        try:
            for match in glob.glob(str(root / pattern), recursive=True):
                path = Path(match)
                if path.resolve().is_relative_to(root) and \
                        path.stat().st_mtime > since:
                    return True
        except OSError:
            pass
        return False

    return changed


AGENT_SYSTEM = """You are a coding agent working inside the workspace {workspace}.
You have FULL write access to this workspace through your tools: write_file
creates or overwrites files, edit_file modifies them, run_command executes
any shell command (git included). Never claim you cannot modify files, run
commands or use git -- you can, via those tools.
{shell_note}
Prefer the built-in tools over shell commands: search (regex across the
workspace), read_file, list_dir. They always work and cost less.
The project map below is authoritative for where things are. Do not use
list_dir or search to locate a file it already names.
read_file prefixes every line with its number and a tab; never copy those
prefixes into edit_file or write_file content.
edit_file replaces ONE exact occurrence: `old` must be copied verbatim from
the file. If it fails with "0 matches", re-read the exact section and retry
with the real text; never guess from memory.
Use the tools to inspect and modify the project; keep answers short and factual.
Call tools instead of guessing file contents. When the task is done, answer
the user directly without further tool calls. Reply in the user's language.
Plan before you act: call set_plan with the goal and its ordered steps, each
with a verifiable done_when -- prefer file:<path> so the harness can confirm it
for you -- and any wall you foresee. Call step_done with the step number and
your evidence as you finish each; a claim on a file that is not there is
refused, so do the work first. When you hit a wall, adapt the blocked step or
revise the plan with set_plan -- never drop the goal on your own, ask the
operator.
Exhaust everything you can solve yourself before you turn to the operator, and
propose solutions even when they look hard. There are exactly two reasons to
interrupt them, and they are different: call ask_operator only for a decision
that is genuinely theirs (a trade-off you cannot settle from the task) -- for
everything else, decide and state your assumption; call request_resource only
once you have hit a real wall, logged it, and have nothing left to try on your
own, to ask for the single external thing they alone can give (a token, a
payment). Both end your turn, so make the ask immediate and precise: one
thing."""

SHELL_NOTE_WINDOWS = ("The host is Windows: run_command uses cmd.exe. Unix "
                      "tools (grep, ls, cat, sed, head, tail) are NOT "
                      "available; python is. cmd.exe executes only the FIRST "
                      "line of a command: never send multi-line commands or "
                      "multi-line `python -c`; write a script with write_file "
                      "and run it instead.")
SHELL_NOTE_POSIX = "The host is a POSIX system: run_command uses /bin/sh."

# The operator's rules of engagement, loaded into EVERY agent session. The
# July 14 session ran in a fresh sandbox with no instruction file and worked
# without any of these rules; the workspace file complements, never replaces.
GLOBAL_AGENT_MD = Path(__file__).resolve().parent.parent / "agent.md"


def _instruction_parts(workspace):
    """The rules of engagement as named blocks: the factory's own `agent.md`
    plus the workspace's instruction file, read from disk.

    Read ONCE per session (`Factory._instructions`), never per turn: these are
    part of the KV prefix, and the workspace file sits in the very directory the
    agent edits -- an agent that touched CLAUDE.md changed its own rules
    mid-session and repaid the whole prefix for it. It is also the file an
    agent.md optimiser would rewrite, and a rewrite must not reach a session
    already running.
    """
    parts = []
    if GLOBAL_AGENT_MD.is_file():
        parts.append(("agent_md", GLOBAL_AGENT_MD.read_text(
            encoding="utf-8", errors="replace")[:8000]))
    for name in INSTRUCTION_FILES:
        p = Path(workspace) / name
        if not p.is_file():
            continue
        try:
            # self-hosted sessions: the workspace file IS the global one
            same = GLOBAL_AGENT_MD.is_file() and p.samefile(GLOBAL_AGENT_MD)
        except OSError:
            same = False
        if not same:
            parts.append(("workspace_instructions", p.read_text(
                encoding="utf-8", errors="replace")[:8000]))
        break
    return parts


def _agent_system_parts(workspace, tool_names=None, instructions=None):
    """The system prompt as named blocks, in wire order.

    `_agent_system` joins them; the bench prices them one by one. Same
    sources, same order, same bytes -- this string is the KV prefix, and a
    byte that moves costs ~1 s per turn (measured 2026-07-22).

    `instructions` is the session's snapshot of the rules blocks; None means
    read them now, which is what the bench and the direct callers want.
    """
    shell_note = SHELL_NOTE_WINDOWS if os.name == "nt" else SHELL_NOTE_POSIX
    parts = [("system", AGENT_SYSTEM.format(workspace=workspace,
                                            shell_note=shell_note))]
    parts.extend(_instruction_parts(workspace) if instructions is None
                 else instructions)
    if tool_names:
        # Generated from the real toolset, never a stale hard-coded list --
        # a session with a curated toolset must not see phantom tools.
        parts.append(("tool_names",
                      "Tools available this session: " + ", ".join(tool_names)))
    return parts


def _agent_system(workspace, tool_names=None, instructions=None):
    return "\n\n".join(text for _, text in
                       _agent_system_parts(workspace, tool_names,
                                           instructions))


def runner_script(kind):
    try:
        return RUNNERS[kind]
    except KeyError:
        raise ToolError("no runner for job kind {!r}".format(kind))


class Factory:
    def __init__(self, jobs_root, config_path, spawn_fn=None):
        self.jobs_root = Path(jobs_root)
        self.config_path = Path(config_path)
        # The transport must be able to find a key stored from the studio
        # without knowing what a config file is.
        cloud_lane.CONFIG_PATH = self.config_path
        self.store = JobStore(self.jobs_root)
        self.spawn_fn = spawn_fn or self._spawn_runner
        self._llama = None        # LlamaServerManager, lazy; tests inject
        self._llama_cfg = None
        self._cloud_meta = {}     # how the cloud catalogue was obtained
        self._runtime_lock = GpuLock(self.jobs_root / ".gpu.lock")
        self._runtime_mutex = threading.Lock()
        self._idle_timer = None
        self.timer_fn = threading.Timer  # tests inject
        self.chats = ChatStore(self.jobs_root / "chats")
        # Rendered lesson block per session, snapshotted on first use: the
        # system prompt is the KV prefix, so what a session was told at its
        # first turn is what it keeps hearing. New lessons land on new
        # sessions.
        self._lesson_blocks = {}
        # Same snapshot rule for <workspace>/MEMORY.md: the agent edits files
        # in its own workspace, and a memory block that changed under it
        # mid-session would invalidate the prefix on every turn.
        self._memory_blocks = {}
        # Same snapshot rule as lessons and memory: the map is the KV prefix,
        # and the agent edits files in the very workspace it describes.
        self._map_blocks = {}
        # Same rule for agent.md and the workspace instruction file: rules that
        # move mid-session repay the prefix AND silently change the contract the
        # session was started under.
        self._instruction_blocks = {}
        # One append-only trace per session, plus the running context estimate
        # it stamps on each tool call so a viewer can watch the window fill.
        self._logs = {}
        self._ctx_running = {}
        try:
            self.mcp = mcp_registry.McpRegistry(
                load_mcp_servers(self.config_path))
        except (OSError, ConfigError) as e:
            print("mcp config unavailable: {}".format(e), file=sys.stderr)
            self.mcp = mcp_registry.McpRegistry({})

    def _lesson_block(self, session_id):
        """The lesson text this session carries in its system prompt, decided
        once and kept: see `_lesson_blocks`."""
        if session_id not in self._lesson_blocks:
            self._lesson_blocks[session_id] = lessons.render(
                self.chats.load_lessons())
        return self._lesson_blocks[session_id]

    def _instructions(self, session_id, workspace):
        """This session's rules of engagement, decided once and kept: see
        `_instruction_blocks`."""
        if session_id not in self._instruction_blocks:
            self._instruction_blocks[session_id] = _instruction_parts(workspace)
        return self._instruction_blocks[session_id]

    def _memory_block(self, session_id, workspace):
        """The workspace memory this session carries, decided once and kept:
        see `_memory_blocks`."""
        if session_id not in self._memory_blocks:
            self._memory_blocks[session_id] = workspace_memory.render(
                _read_memory(workspace))
        return self._memory_blocks[session_id]

    def chat_trace(self, session_id):
        """What a watcher needs to see a session progress without reading the
        transcript: the live plan, how stuck it looks, and what the event trace
        has recorded. Read-only, cheap enough to poll."""
        session = self.chats.get(session_id)
        messages = session.get("messages") or []
        live = carnet.project(messages)
        events = session_log.read(self._log_path(session_id))
        report = session_analysis.analyze(events)
        return {
            "session_id": session_id,
            "plan": live.get("plan"),
            "walls": live.get("walls") or [],
            "notes": live.get("notes") or [],
            "stagnation": carnet.stagnation(messages),
            # Derived from the transcript, never declared: the session that
            # ticked 5/5 steps against an empty diff would have said "verify".
            "phase": phases.phase_of(live.get("plan"),
                                     phases.evidence(messages)),
            "summary": session_log.summarize(events),
            # What the session is wasting, priced, while it still runs: the
            # counts above say a call was replayed, these say what it cost.
            "findings": report["findings"],
            "cost": report["cost"],
            "waste": report["waste"],
            # Newest first: a watcher wants what just went wrong, not the
            # archaeology of the first minute.
            "anomalies": [e for e in events
                          if e.get("kind") == "anomaly"][-40:][::-1],
            "ctx_est": self._ctx_running.get(session_id, 0),
        }

    def _log_path(self, session_id):
        return self.chats.root / (session_id + ".events.jsonl")

    def _log(self, session_id):
        """This session's event trace, created on first use."""
        log = self._logs.get(session_id)
        if log is None:
            log = session_log.SessionLog(self._log_path(session_id))
            self._logs[session_id] = log
        return log

    def _map_block(self, session_id, workspace):
        """The project map this session carries, decided once and kept: see
        `_map_blocks`. A workspace that cannot be mapped costs no map, never
        a session."""
        if session_id in self._map_blocks:
            return self._map_blocks[session_id]
        block = ""
        try:
            entries, _sources, blobs = (_git_entries(workspace)
                                        or _walk_entries(workspace))
            notes = project_map.stale(
                project_map.parse(_read_memory(workspace)), blobs)
            block = project_map.render(entries, notes)
            if project_map.degraded(block):
                # Never silent: a degraded map still looks like a map, and the
                # agent goes straight back to guessing paths.
                print("project map degraded to directories for {} ({} files): "
                      "raise project_map.MAX_MAP_CHARS".format(
                          workspace, len(entries)), file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - a missing map is not fatal
            print("project map: {}".format(e), file=sys.stderr)
        self._map_blocks[session_id] = block
        return block

    def _annotate_from(self, session_id, session):
        """One bounded call: what did this step teach about the files it
        touched. Verified against the repository before anything is written --
        MEMORY.md asserted a FastAPI backend for two days because nothing
        checked (spec 2026-07-27).

        Unlike `_learn_from`, this DOES generate. `_should_annotate` is what
        keeps it to step boundaries instead of every user turn. The GPU runtime
        is already held by the caller's `_acquire_runtime` scope.
        """
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
        model = session["model"]
        res = _transport(model).chat(
            self._endpoint(model), ANNOTATE_SYSTEM,
            ANNOTATE_PROMPT.format(files="\n".join(wanted)),
            temperature=0.1, num_ctx=CHAT_NUM_CTX,
            options=self._model_options(session["model"]))
        vocab = project_map.vocabulary(sources)
        fresh, rejected = [], []
        for line in ((res.get("content") or "").strip()).splitlines():
            path, _, text = line.partition(" : ")
            path, text = path.strip(), text.strip()
            if path not in wanted or not text:
                continue
            bad = project_map.verify(text, vocab)
            if bad:
                rejected.append(bad)
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
                    ", ".join(sorted(set(rejected)))))])
        if fresh:
            merged = {n["path"]: n for n in known.values()}
            merged.update({n["path"]: n for n in fresh})
            updated = project_map.update(
                _read_memory(workspace),
                [{k: v for k, v in n.items() if k != "stale"}
                 for n in merged.values()])
            atomic_write_bytes(Path(workspace) / MEMORY_FILE,
                               updated.encode("utf-8"))

    def _learn_from(self, session_id):
        """Turn a finished agent session into lessons about the agent and
        memories about this workspace. Lessons and memories are pure
        bookkeeping, arithmetic over the transcript; annotation is the one part
        that generates, and `_should_annotate` holds it to step boundaries so
        this stays cheap on an ordinary turn. Never fatal -- a session that
        ends is worth more than what it taught."""
        try:
            session = self.chats.get(session_id)
        except Exception as e:  # noqa: BLE001 - bookkeeping must not raise
            print("learn: {}".format(e), file=sys.stderr)
            return
        # Only what is new. This runs at the end of every user turn over a
        # transcript that keeps growing, and both stores ADD counts on merge:
        # without the watermark, two failures read "failed 4x" after a single
        # "merci" and a lesson doubled per turn with nothing new happening.
        # Counts are what auto-tuning will read, so they have to be true.
        messages = session.get("messages") or []
        start = session.get("learned_upto") or 0
        if not isinstance(start, int) or start < 0 or start > len(messages):
            start = 0
        if start >= len(messages):
            return
        # The slice starts on a turn boundary (learning runs at end of turn),
        # so no assistant call is separated from its tool result.
        fresh = dict(session, messages=messages[start:])
        # Two stores, two try blocks: a broken lessons file must not cost the
        # workspace its memory, and the reverse.
        try:
            self.chats.record_lessons(lessons.from_audit(
                session_audit.analyze(fresh), now=time.time()))
        except Exception as e:  # noqa: BLE001
            print("lessons: {}".format(e), file=sys.stderr)
        try:
            # The trace, not the transcript: this is the half that knows what
            # the session COST. It reads the whole trace (a replay is only
            # visible against everything before it) and banks per finding, so
            # nothing here needs the slice.
            self._learn_from_trace(session_id, session)
        except Exception as e:  # noqa: BLE001
            print("trace lessons: {}".format(e), file=sys.stderr)
        try:
            # Memory gets the WHOLE transcript, not the slice: its floor is two
            # failures, and one per turn never reaches it inside a slice. Its
            # own bookkeeping (`memory_written`) is what stops the double
            # counting the watermark stops for lessons.
            self._remember(session_id, session)
        except Exception as e:  # noqa: BLE001
            print("memory: {}".format(e), file=sys.stderr)
        try:
            # The slice, not the whole transcript: a step already annotated
            # must not be paid for again on the next turn.
            self._annotate_from(session_id, fresh)
        except Exception as e:  # noqa: BLE001
            print("annotate: {}".format(e), file=sys.stderr)
        try:
            self.chats.set_learned_upto(session_id, len(messages))
        except Exception as e:  # noqa: BLE001
            print("learn watermark: {}".format(e), file=sys.stderr)

    def _learn_from_trace(self, session_id, session):
        """Turn what the session cost into lessons.

        `session_audit` counts calls; the trace prices them. Only findings a
        model can act on cross into lessons (`session_analysis.MODEL_FINDINGS`)
        -- the rest are harness facts and stay in the report and the studio
        panel.

        Arithmetic over a small append-only file, no generation: cheap enough to
        run at the end of every turn. `traced_codes` is what makes it
        idempotent, since the analysis always covers the whole trace.
        """
        report = session_analysis.analyze_file(self._log_path(session_id))
        if report["verdict"] == "clean":
            return
        banked = set(session.get("traced_codes") or [])
        fresh = [l for l in lessons.from_trace(report, time.time())
                 if l["pattern"] not in banked]
        if not fresh:
            return
        self.chats.record_lessons(fresh)
        self.chats.set_traced_codes(
            session_id, banked | {l["pattern"] for l in fresh})

    def _remember(self, session_id, session):
        """Write what failed twice into `<workspace>/MEMORY.md`. This edits a
        file in the operator's project, so it only ever rewrites the marked
        block (`workspace_memory.update`) and only for a real workspace.

        `session` is the full transcript; `promote` decides what crosses the
        floor and what this session has already banked. Walls and notes have
        no floor -- the agent asked for them to be kept -- and are merged into
        their own block, so a session with nothing failing still writes."""
        workspace = session.get("workspace")
        if not workspace or not Path(workspace).is_dir():
            return
        text = _read_memory(workspace)
        updated = text
        written = session.get("memory_written") or {}
        now_written = written
        found = workspace_memory.from_session(session, now=time.time())
        if found:
            merged, now_written = workspace_memory.promote(
                found, written, workspace_memory.parse(text), now=time.time())
            if now_written != written:  # something crossed the floor
                updated = workspace_memory.update(updated, merged)
        live = carnet.project(session.get("messages") or [])
        updated = workspace_memory.update_carnet(updated, live["walls"],
                                                 live["notes"])
        if updated == text:
            return
        atomic_write_bytes(Path(workspace) / MEMORY_FILE,
                           updated.encode("utf-8"))
        if now_written != written:
            self.chats.set_memory_written(session_id, now_written)

    def _toolsets(self):
        try:
            return load_toolsets(self.config_path,
                                 mcp_servers=self.mcp.configs)
        except (OSError, ConfigError) as e:
            print("toolsets unavailable: {}".format(e), file=sys.stderr)
            return load_toolsets(os.devnull)  # just "base"

    def toolsets(self):
        return {"toolsets": sorted(self._toolsets())}

    def _session_tools(self, session):
        """Ollama tool defs (native + MCP) for this session's toolset."""
        ts = self._toolsets().get(session.get("toolset") or "base")
        if ts is None:
            ts = self._toolsets()["base"]
        tools = [t for t in agent_tools.TOOLS
                 if t["function"]["name"] in ts.native]
        tools += self.mcp.tool_specs(ts.mcp, session["workspace"])
        tools = tools + carnet.CARNET_TOOLS   # exposed in every toolset
        if len(tools) > mcp_registry.TOOL_BUDGET:
            print("toolset {}: {} tools exposed (budget {}) -- small models "
                  "degrade past it".format(ts.name, len(tools),
                                           mcp_registry.TOOL_BUDGET),
                  file=sys.stderr)
        return tools

    def _needs_approval(self, name):
        if name in carnet.CARNET_TOOL_NAMES:
            return False  # the carnet is memory, not an effect: never gated
        if name.startswith(mcp_registry.PREFIX):
            return self.mcp.needs_approval(name)
        return agent_tools.needs_approval(name)

    def _spawn_runner(self, job_id):
        kind = self.store.read_spec(job_id).get("kind", "delegate")
        argv = [sys.executable, str(Path(__file__).with_name(runner_script(kind))),
                job_id, "--jobs", str(self.jobs_root)]
        if kind in ("delegate", "draft"):  # both resolve a project from the perimeter
            argv += ["--config", str(self.config_path)]
        return spawn_detached(self.store.job_dir(job_id), argv)

    def delegate(self, project, goal, tests, target_files, context_files=None,
                 start_stage=None, edit_mode=None):
        # Validate before anything exists on disk: a refused delegation leaves no trace.
        resolved = resolve_project(load_projects(self.config_path), project)
        validate_tests(tests, resolved.runner)
        validate_targets(target_files)
        validate_context_files(resolved, context_files, target_files)
        if start_stage is not None and start_stage not in (1, 2, 3):
            raise ToolError("start_stage must be 1..3 (ladder rung)")
        if edit_mode is not None and edit_mode not in ("whole", "diff"):
            raise ToolError("edit_mode must be 'whole' or 'diff'")
        spec = {"project": project, "goal": goal, "tests": tests,
                "target_files": list(target_files),
                "context_files": list(context_files or [])}
        if start_stage is not None:
            spec["start_stage"] = start_stage
        if edit_mode is not None:
            spec["edit_mode"] = edit_mode
        job_id = self.store.create(spec)
        self.spawn_fn(job_id)
        return {"job_id": job_id, "status": "queued"}

    def add_project(self, name, path, regression_cmd=None):
        """Append a project to factory.local.toml. Append, never rewrite: the
        file's comments are doctrine, and a TOML writer would erase them. The
        candidate text is re-parsed before it atomically replaces the
        original."""
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ConfigError("a project name is a key: letters, digits, - and _ only")
        if name in load_projects(self.config_path):
            raise ConfigError("project {!r} already exists in factory.toml".format(name))
        resolved = Path(path).resolve()
        if not (resolved / ".git").exists():
            raise ConfigError("{} is not a git repo".format(resolved))
        # json.dumps escapes exactly like a TOML basic string expects.
        block = ["", "[projects.{}]".format(name),
                 "path = {}".format(json.dumps(resolved.as_posix())),
                 'runner = "pytest"']
        if regression_cmd:
            if not isinstance(regression_cmd, list) or not all(
                    isinstance(x, str) for x in regression_cmd):
                raise ConfigError("regression_cmd must be a list of strings")
            # A whole command line typed on one line: "-m pytest tests -q" is
            # one argument no runner can execute, and the mistake only showed
            # up at the first job (E2E audit §4). A flag never carries a space;
            # a path may, so only flags are refused.
            for arg in regression_cmd:
                if arg.startswith("-") and " " in arg.strip():
                    raise ConfigError(
                        "regression_cmd takes one argument per line; {!r} looks "
                        "like a whole command line".format(arg))
            block.append("regression_cmd = [{}]".format(
                ", ".join(json.dumps(x) for x in regression_cmd)))
        target = local_config_path(self.config_path)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            # First project added on this machine: the file does not exist yet.
            text = ("# This machine's coordinates. Never commit this file.\n"
                    "# Promoting a project to factory.toml is a human act:\n"
                    "# a verified green baseline cannot be generated.\n")
        if text and not text.endswith("\n"):
            text += "\n"
        candidate = text + "\n".join(block) + "\n"
        if name not in tomllib.loads(candidate).get("projects", {}):
            raise ConfigError("the appended block did not parse back; nothing written")
        atomic_write_bytes(target, candidate.encode("utf-8"))
        return {"project": name, "status": "added"}

    def draft(self, project, goal):
        """Ask the local spec writer for a proposal: tests, targets, context.
        The output is raw material for a human, never a delegation by itself."""
        resolve_project(load_projects(self.config_path), project)
        job_id = self.store.create({"kind": "draft", "project": project, "goal": goal})
        self.spawn_fn(job_id)
        return {"job_id": job_id, "status": "queued"}

    def job_status(self, job_id):
        return self.store.status(job_id)

    def job_result(self, job_id):
        status = self.store.status(job_id)["status"]
        try:
            return dict(self.store.read_result(job_id), ready=True)
        except JobNotFound:
            pass
        if status == "dead":
            return {"status": "dead", "ready": False,
                    "error": "the job stopped heartbeating and left no result",
                    "log_tail": self._tail(job_id, 40)}
        return {"status": status, "ready": False,
                "hint": "still working; poll job_status"}

    def job_log(self, job_id, tail=100):
        self.store.read_state(job_id)  # raises JobNotFound for an unknown job
        return {"job_id": job_id, "log": self._tail(job_id, tail)}

    def job_cancel(self, job_id):
        pid = self.store.read_state(job_id).get("pid")
        if pid:
            _kill(pid)
        try:
            self.store.write_result(job_id, {"status": "cancelled", "diff": ""})
        except ResultExists:
            pass  # it finished while we were reaching for the axe
        self.store.update_state(job_id, status="cancelled")
        return {"job_id": job_id, "status": "cancelled"}

    def job_apply(self, job_id):
        """Put a succeeded job's diff in the project's working tree. Never a
        commit, never a stage: `git apply --check` first, so a refusal leaves
        the tree byte-identical."""
        result = self.job_result(job_id)
        if not result.get("ready"):
            raise ToolError("job {} has no result yet".format(job_id))
        if result.get("status") != "succeeded":
            raise ToolError("only a succeeded job can be applied; this one is {}".format(
                result.get("status")))
        diff = result.get("diff") or ""
        if not diff.strip():
            raise ToolError("job {} produced an empty diff".format(job_id))
        project = resolve_project(load_projects(self.config_path),
                                  self.store.read_spec(job_id)["project"])
        # Bytes on stdin, not text: text mode would rewrite \n as \r\n on
        # Windows and corrupt the patch.
        data = diff.encode("utf-8")
        argv = ["git", "-C", str(project.path), "apply", "--check", "-"]
        check = subprocess.run(argv, input=data, capture_output=True)
        if check.returncode != 0:
            raise ToolError("diff does not apply cleanly:\n{}".format(
                (check.stderr or check.stdout).decode("utf-8", "replace").strip()))
        subprocess.run(argv[:-2] + ["-"], input=data, capture_output=True, check=True)
        self.store.update_state(job_id, applied_at=time.time())
        return {"job_id": job_id, "status": "applied", "project": project.name}

    # ---- runtime (llama-server) ----

    def _check_model(self, model):
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            raise ToolError("that is not a model name: {!r}".format(model))

    def _model_options(self, model):
        """Sampling profile from factory.toml plus the per-turn generation
        cap. num_predict == chat_context.OUTPUT_RESERVE: the prompt budget
        already reserves that many tokens, the cap turns the reserve into a
        guarantee -- a single turn can no longer overflow the window
        mid-stream (the July 14 root cause)."""
        try:
            opts = model_options(load_models(self.config_path), model)
        except (OSError, ConfigError) as e:
            print("model profiles unavailable: {}".format(e), file=sys.stderr)
            opts = {}
        opts["num_predict"] = chat_context.OUTPUT_RESERVE
        return opts

    def _endpoint(self, model):
        """Where to address `model`: the loaded server's URL, or the id itself.

        Callers are inside an `_acquire_runtime` scope already; this is for the
        paths that need the endpoint again (annotation, compaction) without
        re-taking anything.
        """
        return model if cloud_lane.is_cloud(model) else self._runtime().url

    def _runtime(self):
        if self._llama is None:
            cfg = load_llama_server(self.config_path)
            if cfg is None:
                raise ToolError("factory.toml has no [llama_server] section; "
                                "the chat needs one since the Ollama retirement")
            self._llama_cfg = cfg
            # Chat lane runs a big window cheaply: q4 KV keeps 64k in VRAM
            # instead of spilling to system RAM (2026-07-19 bench). Jobs build
            # their own manager with the q8 default -- diff precision over speed.
            self._llama = LlamaServerManager(cfg, kv_cache_type="q4_0",
                                             ctx=CHAT_NUM_CTX)
        return self._llama

    def _acquire_runtime(self, model):
        """Lock = VRAM: take the GPU lock with the first load and keep it
        while the server is loaded. Re-entrant across chat turns; the idle
        timer (armed after each generation) is the only releaser.

        A cloud model breaks that equation: there is no VRAM to hold, so no
        lock, no server, no idle timer -- and a job keeps the GPU while the
        session talks. The model id is its own endpoint.
        """
        if cloud_lane.is_cloud(model):
            # Same equation for a browser route -- no VRAM, no lock, no idle
            # timer -- but a different thing to check: a signed-in profile
            # instead of a key.
            _transport(model).preflight(model)  # before the turn, not mid-stream
            return model
        with self._runtime_mutex:
            self._cancel_idle()
            if not self._runtime_lock.acquired and \
                    not self._runtime_lock.acquire(timeout=0):
                raise ToolError("GPU is busy: a job holds the lock")
            try:
                return self._runtime().ensure(model)
            except Exception:
                self._release_runtime_locked()
                raise

    def _release_runtime(self):
        with self._runtime_mutex:
            self._release_runtime_locked()

    def _release_runtime_locked(self):
        self._cancel_idle()
        if self._llama is not None:
            self._llama.shutdown()
        self._runtime_lock.release()

    def _cancel_idle(self):
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _arm_idle(self):
        with self._runtime_mutex:
            if not self._runtime_lock.acquired:
                return  # nothing loaded, nothing to give back
            self._cancel_idle()
            idle = (self._llama_cfg or {}).get("chat_idle_s", 600)
            timer = self.timer_fn(idle, self._release_runtime)
            timer.daemon = True
            timer.start()
            self._idle_timer = timer

    def runtime_status(self):
        loaded = self._llama.current[0] if (self._llama and
                                            self._llama.current) else None
        if loaded is not None:
            return {"loaded": loaded, "held_by": "chat"}
        probe = GpuLock(self.jobs_root / ".gpu.lock")
        if probe.acquire(timeout=0):
            probe.release()
            return {"loaded": None, "held_by": None}
        return {"loaded": None, "held_by": "job"}

    def runtime_models(self):
        cfg = self._llama_cfg or load_llama_server(self.config_path)
        loaded = self._llama.current[0] if (self._llama and
                                            self._llama.current) else None
        models = []
        for name, entry in sorted((cfg or {}).get("models", {}).items()):
            path = Path(entry["path"])
            exists = path.is_file()
            models.append({"name": name, "path": entry["path"],
                           "size": path.stat().st_size if exists else None,
                           "args": entry["args"], "loaded": name == loaded,
                           "tools": entry.get("tools", True),
                           "missing": not exists, "cloud": False,
                           # Le studio filtre par fournisseur avant de choisir
                           # un modèle. La règle de routage vit dans
                           # `providers`, et n'a pas à être réécrite en JS
                           # pour que la liste sache se ranger.
                           "route": "local", "route_label": "Local (GPU)"})
        # Cloud entries in the same list, flagged: the operator picks a model
        # in one place. `cloud` is what the UI keys its Load button off -- a
        # model with no VRAM has nothing to load, size, or miss.
        for name, entry in sorted(self._cloud_models().items()):
            models.append({"name": name, "path": None, "size": None,
                           "args": [], "loaded": False,
                           "tools": entry["tools"], "missing": False,
                           "cloud": True, "label": entry["label"],
                           # The window is the operator's main reason to pick
                           # one cloud model over another; hiding it made 64k
                           # look like everyone's limit.
                           "context_length": entry.get("context_length") or 0,
                           "free": entry.get("free"),
                           "usd_per_mtok_in": entry.get("usd_per_mtok_in"),
                           "usd_per_mtok_out": entry.get("usd_per_mtok_out"),
                           "pinned": bool(entry.get("pinned")),
                           "unknown": bool(entry.get("unknown")),
                           "route": providers.split(name)[0],
                           "route_label": providers.label(name)})
        return {"models": models, "cloud_catalog": self._cloud_meta,
                "ctx_local": CHAT_NUM_CTX}

    def _cloud_models(self):
        try:
            models, self._cloud_meta = load_cloud_catalog(self.config_path)
            return models
        except (OSError, ConfigError) as e:
            print("cloud catalogue unavailable: {}".format(e), file=sys.stderr)
            self._cloud_meta = {"catalog": False, "error": str(e)}
            return {}

    def cloud_quota(self):
        """What the key may still spend, for the banner. Never fatal: a studio
        must open without the network, and a missing quota is missing
        information, not a broken studio."""
        try:
            return openrouter_catalog.quota(self.config_path)
        except RuntimeError as e:
            return {"error": str(e)}

    def provider_list(self):
        """The routes and whether each is authenticated. Never a key: the
        status rows carry a hint, and nothing downstream is handed a secret."""
        return {"providers": providers.catalogue(),
                "credentials": credentials.status(self.config_path)}

    def provider_set_key(self, name, value):
        try:
            return {"credentials": credentials.put(self.config_path, name,
                                                   value)}
        except ValueError as e:
            raise ToolError(str(e))

    def cloud_refresh(self):
        """Re-fetch the catalogue on demand -- the operator should not have to
        wait out the cache to see a model that appeared this morning."""
        models, meta = load_cloud_catalog(
            self.config_path,
            catalog_fn=lambda: openrouter_catalog.load(self.config_path,
                                                       force=True))
        self._cloud_meta = meta
        return {"count": len(models), "catalog": meta}

    def runtime_load(self, model):
        if cloud_lane.is_cloud(model):
            raise ToolError(
                "{!r} is a cloud model: it has no VRAM to load. Start a "
                "session on it directly.".format(model))
        self._check_runtime_model(model)
        self._acquire_runtime(model)
        self._arm_idle()
        return {"model": model, "status": "loaded"}

    def runtime_unload(self):
        self._release_runtime()
        return {"status": "unloaded"}

    def shutdown(self):
        """Give back everything this process holds: the GPU runtime, and the
        MCP servers it spawned.

        The servers are the half nobody was doing. Each one runs with
        `cwd=workspace` (mcp_client.py) and on Windows a CWD is a locked
        handle, so the workspace cannot be deleted, renamed or moved while the
        process lives -- and children survive their parent here, so it lived
        past the studio that spawned it. Measured 2026-07-23: 28 of 35 `web`
        bench runs died of PermissionError at reseed, and the bench had to
        close the registry itself to get through.
        """
        self._release_runtime()
        self.mcp.close()

    def _check_tool_capable(self, model):
        """An agent without tools is a chat that pretends. Refuse at creation
        rather than let the operator watch an empty bubble (E2E audit §3)."""
        cfg = self._llama_cfg or load_llama_server(self.config_path)
        entry = (cfg or {}).get("models", {}).get(model)
        if entry is None and cloud_lane.is_cloud(model):
            entry = self._cloud_models().get(model)
        if entry is not None and not entry.get("tools", True):
            raise ToolError(
                "{!r} cannot call tools -- use it for plain chat, or pick a "
                "tool-capable model for an agent session".format(model))

    def _check_runtime_model(self, model):
        self._check_model(model)
        cfg = self._llama_cfg or load_llama_server(self.config_path)
        if cfg is None or model not in cfg["models"]:
            raise ToolError("{!r} has no [llama_server.models] entry in "
                            "factory.toml".format(model))

    # ---- chat ----

    def chat_list(self):
        sessions = self.chats.list()
        for s in sessions:
            s["project"] = self._project_of(s.get("workspace"))
        return {"sessions": sessions}

    def _project_of(self, workspace):
        """Which declared project a session's workspace is, for grouping.

        Only factory.toml names a project. Falling back to the folder name was
        tried first and produced twenty one-session groups named after bench
        sandboxes (`huge_chain`, `fix_exact_value`...) -- grouping that costs
        more attention than the flat list it replaced. Everything else is one
        bucket; the session view already shows its workspace path.
        """
        if not workspace:
            return None
        try:
            path = Path(workspace).resolve()
        except OSError:
            return None
        try:
            projects = load_projects(self.config_path)
        except (OSError, ConfigError):
            return None
        for name, entry in projects.items():
            try:
                if Path(entry["path"]).resolve() == path:
                    return name
            except (OSError, KeyError, TypeError):
                continue
        return None

    def chat_set_title(self, session_id, title):
        return self.chats.set_title(session_id, title)

    def chat_create(self, model, kind=None, mode=None, workspace=None,
                    toolset=None, project=None):
        self._check_model(model)
        if kind not in (None, "agent"):
            raise ToolError("unknown session kind: {!r}".format(kind))
        if kind == "agent":
            self._check_tool_capable(model)
            if mode not in (None, "approve", "auto"):
                raise ToolError("mode must be approve or auto")
            # Un projet est un NOM, jamais un chemin -- c'est ce qui empêche
            # `../..` de vouloir dire quelque chose. Le studio propose la liste
            # déclarée ; le champ workspace libre reste pour le hors-périmètre.
            if project:
                workspace = str(resolve_project(
                    load_projects(self.config_path), project).path)
            if workspace is not None and not Path(workspace).is_dir():
                raise ToolError("workspace is not a directory: {}".format(workspace))
            if toolset is not None and toolset not in self._toolsets():
                raise ToolError("unknown toolset: {!r}".format(toolset))
            ws = workspace or os.getcwd()
            _ensure_git(ws)
            return self.chats.create(model, kind="agent", mode=mode or "approve",
                                     workspace=ws, toolset=toolset)
        return self.chats.create(model)

    def chat_get(self, session_id):
        return self.chats.get(session_id)

    def chat_delete(self, session_id):
        self.chats.delete(session_id)
        return {"session_id": session_id, "deleted": True}

    def chat_set_model(self, session_id, model):
        self._check_model(model)
        if self.chats.get(session_id).get("kind") == "agent":
            self._check_tool_capable(model)
        return self.chats.set_model(session_id, model)

    def chat_set_mode(self, session_id, mode):
        if mode not in ("approve", "auto"):
            raise ToolError("mode must be approve or auto")
        return self.chats.set_mode(session_id, mode)

    def ctx_ceiling(self, model):
        """The largest window this model can actually be asked for.

        For a local rung it is the machine's: CHAT_NUM_CTX is what the server
        was started with, and budgeting above it makes llama-server truncate
        the oldest tokens in silence. For a cloud model that number is
        meaningless -- it was calibrated for 12 GB of VRAM we are not using --
        and the real limit is the provider's, which the catalogue knows. Using
        the local ceiling there compacted 262k-window models at 64k, paying for
        summarisation nobody needed.
        """
        if not cloud_lane.is_cloud(model):
            return CHAT_NUM_CTX
        entry = self._cloud_models().get(model) or {}
        return entry.get("context_length") or CHAT_NUM_CTX

    def chat_set_num_ctx(self, session_id, num_ctx):
        """Per-session context window. Bounded: below 4k an agent cannot even
        hold its system prompt plus one tool output; above the model's own
        ceiling the window does not exist -- on the 12 GB GPU that ceiling is
        the KV cache (the 2026-07-13 sessions proved what 260k does), on a
        cloud model it is whatever the provider serves."""
        session = self.chats.get(session_id)
        ceiling = self.ctx_ceiling(session["model"])
        if not isinstance(num_ctx, int) or not 4096 <= num_ctx <= ceiling:
            raise ToolError("num_ctx must be an integer in [4096, {}] for {}"
                            .format(ceiling, session["model"]))
        return self.chats.set_num_ctx(session_id, num_ctx)

    def chat_upload(self, session_id, name, content_b64):
        """A file dropped on an agent session lands in <workspace>/uploads/.
        Only agent sessions have a workspace; the name must be a bare
        filename (letter/digit first, no separators) so it cannot escape it."""
        session = self.chats.get(session_id)
        if session.get("kind") != "agent":
            raise ToolError("uploads need an agent session (a workspace)")
        if not isinstance(name, str) or not _UPLOAD_NAME.match(name):
            raise ToolError("bad file name: {!r}".format(name))
        try:
            data = base64.b64decode(content_b64 or "", validate=True)
        except (binascii.Error, ValueError):
            raise ToolError("content_b64 is not valid base64")
        if len(data) > UPLOAD_MAX_BYTES:
            raise ToolError("file too large: {} bytes (max {})".format(
                len(data), UPLOAD_MAX_BYTES))
        dest = Path(session["workspace"]) / "uploads"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / name).write_bytes(data)
        return {"path": "uploads/" + name, "bytes": len(data)}

    def chat_reply(self, session_id, content, cancel=None):
        """Generator: ("start", None), then ("thinking", text)... and
        ("chunk", text)..., then one terminal ("done", metrics) or
        ("error", message).

        Everything that can refuse -- unknown session, blank message, busy
        GPU -- raises before the first yield, while the web layer can still
        answer a status code. The GPU lock is held from before the first
        token to after the last: a job must not swap VRAM under us.

        `cancel` is the turn's Stop button, handed to the lane so it also
        works while the lane is blocked. It comes out as a `TurnCancelled`
        through this frame -- which is the point: the partial is persisted and
        the idle timer armed on the way out, exactly as for a close.
        """
        session = self.chats.get(session_id)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty message")
        url = self._acquire_runtime(session["model"])
        finished = False
        parts = []
        thoughts = []

        def _partial(error):
            # Saved for display only: history sent to the model is content-only.
            msg = {"role": "assistant", "content": "".join(parts),
                   "ts": time.time(), "error": error}
            if thoughts:
                msg["thinking"] = "".join(thoughts)
            self.chats.append(session_id, msg)

        try:
            self.chats.append(session_id, {"role": "user", "content": content,
                                           "ts": time.time()})
            yield "start", None
            fresh = self.chats.get(session_id)
            # Never budget above the real window or the server silently
            # truncates the oldest tokens. Whose window that is depends on who
            # serves the model -- see `ctx_ceiling`.
            ceiling = self.ctx_ceiling(fresh["model"])
            num_ctx = min(fresh.get("num_ctx") or ceiling, ceiling)
            wire = [{"role": m["role"], "content": m["content"]}
                    for m in fresh["messages"]]
            r = chat_context.ratio(fresh)
            budget = chat_context.prompt_budget(num_ctx)
            summary = fresh.get("summary")
            compacted = False
            if chat_context.needs_compaction(
                    wire, summary, budget, r,
                    force=_overflowed(fresh, num_ctx)):
                yield "compacting", True
                try:
                    summary = self._compact(session_id, session["model"],
                                            wire, summary)
                    compacted = True
                except Exception as e:
                    print("compaction failed: {}".format(e), file=sys.stderr)
            history, info = chat_context.build_prompt([], summary, wire,
                                                      budget, r)
            # What the raw history would cost unsummarized: the operator's
            # warning light. It is NOT a second compaction trigger -- the
            # threshold above now measures the same raw span and always fires
            # first (0.7 * budget < 75 % of num_ctx).
            estimated_tokens = chat_context.estimate(
                wire + ([chat_context.summary_message(summary)] if summary
                        else []), r)
            ctx_pct = int(100 * estimated_tokens / num_ctx) if num_ctx else 0
            if ctx_pct > 75:
                yield "context_warning", {
                    "pct": ctx_pct,
                    "tokens": estimated_tokens,
                    "num_ctx": num_ctx
                }
            stream = _transport(session["model"]).chat_stream(
                url, history, num_ctx=num_ctx,
                options=self._model_options(session["model"]), cancel=cancel)
            while True:
                try:
                    kind, text = next(stream)
                except StopIteration as stop:
                    metrics = stop.value or {}
                    break
                except TurnCancelled:
                    # Stop, not a failure: the `finally` below keeps whatever
                    # was said, exactly as for a closed generator -- and keeps
                    # nothing when nothing was, which is the common case here
                    # (the wait that needed a token is the one before the first
                    # one). No ("error", ...): the runner ends the turn on it.
                    raise
                except Exception as e:
                    # Log the error for persistent tracking
                    try:
                        import session_errors
                        session_errors.log_error(
                            error_type="runtime",
                            error_message=str(e),
                            session_id=session_id,
                            context={"model": session.get("model"), "mode": session.get("mode")}
                        )
                    except Exception:
                        pass  # Don't let error logging break the flow
                    _partial(str(e))
                    finished = True
                    yield "error", str(e)
                    return
                if kind == "thinking":
                    thoughts.append(text)
                    yield "thinking", text
                    continue
                parts.append(text)
                yield "chunk", text
            # Detect empty response from model (context overflow symptom)
            resp_content = metrics.get("content", "".join(parts))
            resp_tool_calls = metrics.get("tool_calls") or []
            is_empty_response = (not resp_content.strip() and not resp_tool_calls)
            
            msg = {"role": "assistant",
                   "content": resp_content,
                   "ts": time.time(),
                   "metrics": _usage(metrics, info, compacted, num_ctx)}
            if thoughts:
                msg["thinking"] = "".join(thoughts)
            if is_empty_response:
                msg["error"] = "empty_response"
                # Inject explicit system warning for user visibility
                ctx_ctx = metrics.get("context", {})
                ctx_usage = ctx_ctx.get("usage_pct", 0)
                evicted = ctx_ctx.get("evicted", 0)
                self.chats.append(session_id, {
                    "role": "tool",
                    "tool_name": "system",
                    "content": (
                        "⚠️ **Réponse vide détectée**\n"
                        "Le modèle n'a produit aucun contenu ni outil.\n"
                        "Causes possibles :\n"
                        "- Contexte saturé ({}% utilisé, {} tokens expulsés)\n"
                        "- Le modèle a \"oublié\" sa tâche\n"
                        "- Timeout ou erreur interne\n"
                        "\n**Recommandation** : La compaction va être forcée. "
                        "Si le problème persiste, créez une nouvelle session.".format(
                            ctx_usage, evicted)
                    ),
                    "ts": time.time()
                })
            self.chats.append(session_id, msg)
            self._save_calibration(session_id, history, metrics)
            self._log_turn(session_id, num_ctx, metrics, info, compacted)
            finished = True
            yield "done", _usage(metrics, info, compacted, num_ctx)
        finally:
            # Client gone mid-stream (GeneratorExit), or Stop caught inside the
            # lane (TurnCancelled): either way, keep the partial.
            if not finished and (parts or thoughts):
                _partial("interrupted")
            self._arm_idle()

    def _compact(self, session_id, model, wire, summary):
        """One summary generation over the span about to leave the window;
        persisted so it is paid once. Caller already holds the GPU lock.
        Raises on any failure -- the caller falls back to trimming."""
        span, covers_until = chat_context.compaction_span(wire, summary)
        span, _ = chat_context.evict_tool_outputs(span, keep_last=0)
        parts = []
        if summary:
            parts.append("Résumé précédent :\n" + summary["content"])
        parts.extend("{}: {}".format(m["role"], m.get("content") or "")
                     for m in span)
        res = _transport(model).chat(self._endpoint(model),
                                     chat_context.SUMMARY_PROMPT,
                                     "\n\n".join(parts), temperature=0.1,
                                     num_ctx=CHAT_NUM_CTX,
                                     options=self._model_options(model))
        content = (res.get("content") or "").strip()
        if not content:
            raise ValueError("summarizer returned nothing")
        new = {"content": content, "covers_until": covers_until,
               "model": model, "ts": time.time()}
        self.chats.set_summary(session_id, new)
        return new

    def _log_turn(self, session_id, num_ctx, metrics, info, compacted):
        """Close the turn in the trace and reset the running context estimate
        to what the prompt actually measured -- the per-call estimate drifts,
        and a turn boundary is where truth is available."""
        info = info or {}
        metrics = metrics or {}
        self._ctx_running[session_id] = info.get("est_tokens", 0)
        self._log(session_id).turn_end(
            num_ctx=num_ctx,
            prompt_count=_prompt_tokens(metrics),
            eval_count=metrics.get("eval_count", 0),
            done_reason=metrics.get("done_reason", ""),
            tokens_per_s=metrics.get("tokens_per_s", 0.0),
            ttft_s=metrics.get("ttft_s", 0.0),
            # 0.0 on a local turn. In the trace, so a session's total spend is
            # a sum over turns rather than a process-global ledger that two
            # sessions would share.
            cost_usd=metrics.get("cost_usd") or 0.0,
            evicted=info.get("evicted", 0), dropped=info.get("dropped", 0),
            compacted=compacted, stopped=metrics.get("stopped", ""))

    def _save_calibration(self, session_id, history, metrics):
        """Real prompt tokens vs chars sent: next turn's estimates use it.

        Against the WHOLE prompt, not the evaluated part: the chars come from
        the whole history, so a cached turn would otherwise divide them by ~1.
        """
        if _prompt_tokens(metrics):
            self.chats.set_calibration(session_id, {
                "chars": sum(chat_context.wire_chars(m) for m in history),
                "tokens": _prompt_tokens(metrics)})

    # ---- agent ----

    def _handle_carnet_tool(self, session_id, name, args, workspace):
        """remember/recall/log_wall need the session (to append messages) and
        the workspace (to read MEMORY.md) -- neither is available inside
        agent_tools.run, so the harness handles them directly.

        Returns (tool_name, content) instead of appending: the caller does the
        single append, so one call produces exactly one tool message -- a
        second one (previously appended here too) desynced `from_session`'s
        positional pairing and silently dropped the failure that followed."""
        args = args or {}
        if name == "remember":
            note = str(args.get("note") or "").strip()
            if not note:
                return name, "error: remember needs a note"
            return "note", note
        if name == "log_wall":
            wall = {"wall": args.get("wall", ""), "cause": args.get("cause", ""),
                    "tried": list(args.get("tried") or []),
                    "remaining": list(args.get("remaining") or [])}
            return "wall", json.dumps(wall)
        if name == "recall":
            text = _read_memory(workspace)
            q = str(args.get("query") or "").strip().lower()
            if q:
                text = "\n".join(l for l in text.splitlines() if q in l.lower())
            return "recall", (text or "(memory empty)")
        if name == "set_plan":
            payload = {"goal": str(args.get("goal") or ""),
                       "steps": args.get("steps") or []}
            if carnet.parse_plan(json.dumps(payload)) is None:
                return name, "error: set_plan needs a goal and at least one step"
            return "plan", json.dumps(payload)
        if name == "step_done":
            plan = carnet.project(
                self.chats.get(session_id)["messages"]).get("plan")
            # Same probe as the harness's own ticking, and for the same reason:
            # existence proves nothing about a step that MODIFIES a file that
            # was already there. Measured live 2026-07-25 -- a session ticked
            # 5/5 steps against an empty diff, having written one file and
            # edited none, because every path it named happened to exist.
            ok, reason = carnet.verify_claim(
                plan, args.get("step"), _changed_since(workspace, plan.get("ts")
                                                       if plan else None))
            if not ok:
                return name, "error: " + reason
            i = int(args["step"])
            if plan["steps"][i - 1]["done"]:
                # Re-ticking is a no-op, and a silent one used to be a trap: a
                # session ran 10 minutes re-ticking step 5 with identical
                # arguments, 8 s apart, and would have run an hour (live
                # 2026-07-25). It never stopped because `step` is BOOKKEEPING
                # -- invisible to the carnet, so no stagnation, no repeat
                # count -- and the turn only ends on an answer with no call.
                # Returning under `step_done` instead of `step` is the whole
                # fix: the message is not a tick, and it counts as an action,
                # so the stagnation alert can finally see the loop. Signal,
                # never a block: the call is still allowed to happen.
                return name, _already_ticked(plan, i)
            return "step", json.dumps(
                {"i": i, "step": plan["steps"][i - 1]["step"],
                 "evidence": str(args.get("evidence") or "")})
        if name == "ask_operator":
            question = str(args.get("question") or "").strip()
            if not question:
                return name, "error: ask_operator needs a question"
            return "handback", json.dumps(
                {"kind": "question", "question": question,
                 "options": [str(o) for o in (args.get("options") or [])]})
        if name == "request_resource":
            resource = str(args.get("resource") or "").strip()
            if not resource:
                return name, "error: request_resource needs a resource"
            walls = carnet.project(
                self.chats.get(session_id)["messages"])["walls"]
            if not carnet.resource_justified(walls):
                return name, ("error: log_wall first -- prove the block is "
                              "external (a wall with empty `remaining`) before "
                              "asking the operator for a resource")
            return "handback", json.dumps(
                {"kind": "resource", "resource": resource,
                 "why": str(args.get("why") or "")})
        return name, "error: unknown carnet tool: {}".format(name)

    def _tick_plan(self, session_id, workspace):
        """Tick every plan step the harness can verify by itself, once. A step
        whose mechanical `done_when` (a `file:<path>`) now holds becomes a
        `progress` message -- the transcript is the ledger, so a step already
        ticked is never ticked twice. Never fatal: a broken probe leaves the
        plan as the agent left it."""
        try:
            plan = carnet.project(
                self.chats.get(session_id)["messages"]).get("plan")
            if not plan:
                return
            probe = _changed_since(workspace, plan.get("ts"))
            for i, done_when in carnet.newly_satisfied(plan, probe):
                self.chats.append(session_id, {
                    "role": "tool", "tool_name": "progress",
                    "content": json.dumps({"i": i, "evidence": done_when}),
                    "ts": time.time()})
        except Exception:
            pass

    def _gate(self, session_id, call, session=None):
        """The refusal text for this call, or "" when it may run.

        The write tools are checked first and nothing else pays anything: the
        gate needs the transcript, and `chats.get()` is a full file read (2.6 MB
        on the worst session), so a gate that read it for every `read_file`
        would be the same mistake the trace exists to expose. `_drain_calls`
        already holds the session and hands it over.
        """
        if call["name"] not in phases.WRITE_TOOLS:
            return ""
        session = session or self.chats.get(session_id)
        messages = session.get("messages") or []
        ok, reason = phases.gate(call["name"], carnet.project(messages).get("plan"),
                                 phases.evidence(messages),
                                 mode=session.get("mode") or "auto")
        return "" if ok else reason

    def _run_call(self, session_id, call, workspace, session=None):
        yield "tool_call", call
        refused = self._gate(session_id, call, session)
        if refused:
            # Same shape as a real result: the agent reads the refusal as the
            # output of its own call, which is the only channel it is listening
            # on mid-turn.
            self.chats.append(session_id, {"role": "tool",
                                           "tool_name": call["name"],
                                           "content": refused,
                                           "ts": time.time()})
            self._log(session_id).anomaly(
                "gate_refused", "{}: {}".format(call["name"], refused))
            yield "tool_result", {"name": call["name"], "output": refused}
            return
        started = time.monotonic()
        if call["name"] in carnet.CARNET_TOOL_NAMES:
            tool_name, output = self._handle_carnet_tool(
                session_id, call["name"], call.get("arguments"), workspace)
        elif call["name"].startswith(mcp_registry.PREFIX):
            tool_name = call["name"]
            output = self.mcp.run(call["name"], call.get("arguments") or {},
                                  workspace)
        else:
            tool_name = call["name"]
            output = agent_tools.run(call["name"], call.get("arguments") or {},
                                     workspace)
        # Raw output, unannotated: this string is what workspace_memory.detail_of
        # reads back as the failure reason, and carnet.project renders live --
        # gluing the nudge onto it corrupted both. `note`/`wall` are exempt from
        # the nudge: their content is the agent's own words, never a shell
        # verdict, so `workspace_memory.failed` would misread them.
        self.chats.append(session_id, {"role": "tool", "tool_name": tool_name,
                                       "content": output, "ts": time.time()})
        # Charged against the turn's own estimate rather than re-reading the
        # transcript: chats.get() costs a full file read, and a 2.6 MB one per
        # tool call is exactly the kind of cost this trace exists to expose.
        running = self._ctx_running.get(session_id, 0) + int(
            len(output or "") / chat_context.DEFAULT_RATIO)
        self._ctx_running[session_id] = running
        self._log(session_id).tool(
            call["name"], call.get("arguments"),
            (time.monotonic() - started) * 1000, output, ctx_est=running)
        yield "tool_result", {"name": call["name"], "output": output}
        if tool_name not in carnet.BOOKKEEPING and workspace_memory.failed(output):
            self.chats.append(session_id, {"role": "tool", "tool_name": "system",
                                           "content": REFLECT_ANNOTATION,
                                           "ts": time.time()})
        self._tick_plan(session_id, workspace)

    def _drain_calls(self, session_id):
        """Run queued calls. Returns "ok" when all ran, "parked" on an
        approval gate. A repeat is never refused here: the carnet surfaces it
        (no hard block), which is what the retired _LoopGuard used to do by
        killing the turn -- it killed legitimate work too."""
        while True:
            session = self.chats.get(session_id)
            calls = session.get("pending_calls") or []
            if not calls:
                return "ok"
            call = calls[0]
            if session["mode"] == "approve" and \
                    self._needs_approval(call["name"]):
                yield "approval_needed", call
                return "parked"
            # pop before running: a crash must not replay a shell command
            self.chats.set_pending_calls(session_id, calls[1:])
            before = len(session["messages"])
            yield from self._run_call(session_id, call, session["workspace"],
                                      session=session)
            # A handback (ask_operator / request_resource) ends the turn: the
            # agent has handed a precise ask to the operator, so drop whatever it
            # queued behind it and wait (spec E). Scanned, not messages[-1]:
            # _tick_plan may append a progress after the tool message.
            new = self.chats.get(session_id)["messages"][before:]
            handback = next((m for m in new
                             if m.get("tool_name") == "handback"), None)
            if handback:
                self.chats.set_pending_calls(session_id, [])
                yield "handback", json.loads(handback["content"])
                return "handback"

    def _agent_system_head(self, session, tools=None):
        """The session's system prompt: the KV prefix. Everything in it is
        decided once per session (tools, lessons, workspace memory), so it
        serialises byte-identically every turn. Nothing that moves during the
        session belongs here -- the carnet rides at the tail instead."""
        if tools is None:
            tools = self._session_tools(session)
        head = _agent_system(session["workspace"],
                             [t["function"]["name"] for t in tools],
                             self._instructions(session["session_id"],
                                                session["workspace"]))
        for block in (self._map_block(session["session_id"],
                                      session["workspace"]),
                      self._lesson_block(session["session_id"]),
                      self._memory_block(session["session_id"],
                                         session["workspace"])):
            if block:
                head += "\n\n" + block
        return head

    def _carnet_tail(self, session):
        """The live carnet as a transient trailing wire message, or []. It is
        never persisted: it is a projection of the transcript, recomputed each
        turn. It is appended AFTER the compaction decision on purpose --
        `compaction_span` returns indexes into the persisted transcript, and a
        phantom message at the end would shift `covers_until` by one every
        compaction."""
        messages = session["messages"]
        view = carnet.project(messages)
        # The live findings ride HERE, not in the system prompt: the prompt is
        # the KV prefix and is frozen for the life of the session, which is why
        # lessons only reach the next one. A trailing message invalidates
        # nothing before it, so a session can be told what it is wasting while
        # it is still wasting it.
        found = session_analysis.analyze_file(
            self._log_path(session["session_id"]))["findings"]
        hot = carnet.render(view,
                            plan_expected=(session.get("mode") == "auto"),
                            stalled=carnet.stagnation(messages),
                            alerts=phases.alerts(view.get("plan"),
                                                 phases.evidence(messages),
                                                 found))
        return [{"role": "system", "content": hot}] if hot else []

    def _agent_turn(self, session_id, cancel=None):
        """Model turns + tool executions until a final answer, an approval
        gate, or (when configured) an iteration cap. The caller owns the GPU
        lock. agent_max_iterations = 0 means no cap: the RepetitionGuard is
        the runaway brake, the Stop button the escape. Repetition itself is
        no longer a brake -- the carnet shows it, the agent decides."""
        cap = (self._llama_cfg or load_llama_server(self.config_path)
               or {}).get("agent_max_iterations", 0)
        i = 0
        malformed_streak = 0
        stall_streak = 0
        cut_streak = 0
        while cap == 0 or i < cap:
            status = yield from self._drain_calls(session_id)
            if status in ("parked", "handback"):
                return
            if cap and i == cap - ITERATION_WARNING_AT:
                note = ITERATION_WARNING.format(n=ITERATION_WARNING_AT)
                self.chats.append(session_id, {
                    "role": "tool", "tool_name": "system",
                    "content": note, "ts": time.time()})
                yield "notice", note
            session = self.chats.get(session_id)
            url = self._acquire_runtime(session["model"])
            tools = self._session_tools(session)
            system_msgs = [{"role": "system",
                            "content": self._agent_system_head(session, tools)}]
            wire = []
            for m in session["messages"]:
                # A stalled turn goes out as a neutral line: its own words are
                # the precedent that reproduces it (see mark_stalled).
                content = STALL_ON_WIRE if m.get("stalled") else m["content"]
                msg = {"role": m["role"], "content": content}
                if m.get("tool_calls"):
                    msg["tool_calls"] = m["tool_calls"]
                if m.get("tool_name"):
                    msg["tool_name"] = m["tool_name"]
                wire.append(msg)
            r = chat_context.ratio(session)
            ceiling = self.ctx_ceiling(session["model"])
            num_ctx = min(session.get("num_ctx") or ceiling, ceiling)
            budget = chat_context.prompt_budget(num_ctx)
            summary = session.get("summary")
            compacted = False
            if chat_context.needs_compaction(
                    wire, summary, budget, r,
                    force=_overflowed(session, num_ctx)):
                yield "compacting", True
                try:
                    summary = self._compact(session_id, session["model"],
                                            wire, summary)
                    compacted = True
                except Exception as e:
                    print("compaction failed: {}".format(e), file=sys.stderr)
            history, info = chat_context.build_prompt(
                system_msgs, summary, wire + self._carnet_tail(session),
                budget, r)
            stream = _transport(session["model"]).chat_stream(
                url, history, num_ctx=num_ctx,
                tools=tools,
                options=self._model_options(session["model"]), cancel=cancel)
            parts, thoughts, calls = [], [], []
            saved = False

            def _partial(error):
                msg = {"role": "assistant", "content": "".join(parts),
                       "ts": time.time(), "error": error}
                if thoughts:
                    msg["thinking"] = "".join(thoughts)
                self.chats.append(session_id, msg)

            try:
                while True:
                    try:
                        kind, payload = next(stream)
                    except StopIteration as stop:
                        metrics = stop.value or {}
                        break
                    except TurnCancelled:
                        # Stop while the lane was blocked. The `finally` below
                        # persists the partial, like a closed generator; the
                        # operator's click is not an ("error", ...) event.
                        raise
                    except Exception as e:
                        _partial(str(e))
                        saved = True
                        yield "error", str(e)
                        return
                    if kind == "thinking":
                        thoughts.append(payload)
                        yield "thinking", payload
                    elif kind == "content":
                        parts.append(payload)
                        yield "chunk", payload
                    else:
                        calls.append(payload)
                if not calls and parts and not metrics.get("stopped"):
                    # llama-server didn't parse this model's tool call (it came
                    # as XML text); recover it or the turn dies silently.
                    recovered, cleaned = llama_client.parse_text_tool_calls(
                        "".join(parts), tools)
                    if recovered:
                        calls = recovered
                        parts[:] = [cleaned]
                msg = {"role": "assistant", "content": "".join(parts),
                       "ts": time.time(),
                       "metrics": _usage(metrics, info, compacted, num_ctx)}
                if thoughts:
                    msg["thinking"] = "".join(thoughts)
                if calls:
                    msg["tool_calls"] = [{"function": c} for c in calls]
                self.chats.append(session_id, msg)
                self._save_calibration(session_id, history, metrics)
                self._log_turn(session_id, num_ctx, metrics, info, compacted)
                saved = True
            finally:
                # Consumer gone mid-stream (GeneratorExit lands here): keep
                # the partial, same convention as chat_reply.
                if not saved and (parts or thoughts):
                    _partial("interrupted")
            if metrics.get("stopped") == "length" and calls:
                cut_streak += 1
            else:
                cut_streak = 0
            if metrics.get("stopped") == "length" and calls \
                    and cut_streak < LENGTH_WITH_CALLS_AT:
                # The cap cut the reply AFTER llama-server had already parsed
                # complete calls. Throwing them away cost the whole turn and
                # asked the operator for a manual "continue" -- on a thinking
                # model whose median run is ~6.5k tokens (banc A) that was a
                # routine loss, not an edge case. A parsed call is valid
                # whatever happened downstream of it: run it, say it was cut.
                self.chats.append(session_id, {
                    "role": "tool", "tool_name": "system",
                    "content": LENGTH_STOPPED_WITH_CALLS, "ts": time.time()})
                yield "notice", LENGTH_STOPPED_WITH_CALLS
            elif metrics.get("stopped"):
                # Degenerate output, or a cut that left nothing to run: do not
                # iterate on it -- surface why and hand back.
                note = (LENGTH_STOPPED if metrics["stopped"] == "length"
                        else DEGENERATE_STOPPED)
                self.chats.set_pending_calls(session_id, [])
                self.chats.append(session_id, {
                    "role": "tool", "tool_name": "system",
                    "content": note, "ts": time.time()})
                yield "done", _usage(metrics, info, compacted, num_ctx)
                return
            if not calls:
                # A turn with no call but tool-call scaffolding in its text or
                # reasoning is an ATTEMPTED call llama-server couldn't parse --
                # not a final answer. Re-prompt for a clean one instead of
                # ending the agent silently; give up after a few tries.
                if llama_client.has_tool_call_markers(
                        "".join(parts) + "".join(thoughts)):
                    malformed_streak += 1
                    # Log malformed call error
                    try:
                        import session_errors
                        session_errors.log_error(
                            error_type="malformed_call",
                            error_message="Unbalanced tool_call/function tags",
                            session_id=session_id,
                            tool_name=session.get("pending_tool"),
                            context={"content": "".join(parts)[:200] + "".join(thoughts)[:200]}
                        )
                    except Exception:
                        pass
                    if malformed_streak >= MALFORMED_RETRY_AT:
                        self.chats.append(session_id, {
                            "role": "tool", "tool_name": "system",
                            "content": MALFORMED_CALL_STOPPED.format(
                                n=malformed_streak), "ts": time.time()})
                        yield "done", {"stopped": "malformed_call"}
                        return
                    self.chats.append(session_id, {
                        "role": "tool", "tool_name": "system",
                        "content": MALFORMED_CALL_RETRY, "ts": time.time()})
                    yield "notice", MALFORMED_CALL_RETRY
                    i += 1
                    continue
                # Prose, with no call scaffolding at all: either a real final
                # answer, or a turn that announced the next action and never
                # requested it. The plan is what tells them apart -- and only
                # in `auto`, the mode that means "work by yourself": in
                # `approve` the operator is already in the loop at every call.
                plan = carnet.project(
                    self.chats.get(session_id)["messages"]).get("plan")

                last_assistant_content = ""
                for msg in reversed(self.chats.get(session_id)["messages"]):
                    if msg.get("role") == "assistant":
                        last_assistant_content = msg.get("content") or ""
                        break

                # Clean the content the same way parse_text_tool_calls does, so
                # ChatGPT plugin noise is gone before the stall test: a turn of
                # pure plugin data is not an announcement (see _announces_action).
                try:
                    cleaned = llama_client._clean_chatgpt_noise(last_assistant_content)
                except Exception:
                    cleaned = last_assistant_content

                if session.get("mode") == "auto" and carnet.unfinished(plan) \
                        and _announces_action(cleaned):
                    stall_streak += 1
                    self.chats.mark_stalled(session_id)
                    # Log stall error
                    try:
                        import session_errors
                        session_errors.log_error(
                            error_type="stall",
                            error_message="Turn announced action but didn't request it",
                            session_id=session_id,
                            context={"steps_left": carnet.unfinished(plan), 
                                     "mode": session.get("mode"),
                                     "stall_streak": stall_streak}
                        )
                    except Exception:
                        pass
                    self.chats.append(session_id, {
                        "role": "tool", "tool_name": "system",
                        "content": STALL_RETRY, "ts": time.time()})
                    self._log(session_id).anomaly(
                        "stall_relance",
                        "{} step(s) left".format(carnet.unfinished(plan)))
                    yield "notice", STALL_NOTICE.format(n=stall_streak, 
                                                        limit=MAX_STALL_RETRIES)
                    # Cap the stall retries to prevent infinite loops (e.g. ChatGPT
                    # repeatedly announcing actions without emitting tool calls)
                    if stall_streak >= MAX_STALL_RETRIES:
                        self.chats.append(session_id, {
                            "role": "tool", "tool_name": "system",
                            "content": ("Maximum stall retries ({}) reached. "
                                        "The plan is still unfinished but the model "
                                        "keeps announcing actions without requesting "
                                        "them. Handing back to operator.").format(
                                            MAX_STALL_RETRIES),
                            "ts": time.time()})
                        yield "done", {"stopped": "stall_limit"}
                        return
                    i += 1
                    continue
                yield "done", _usage(metrics, info, compacted, num_ctx)
                return
            malformed_streak = 0
            stall_streak = 0
            self.chats.set_pending_calls(session_id, calls)
            i += 1
        self.chats.append(session_id, {
            "role": "tool", "tool_name": "system",
            "content": ITERATION_STOPPED.format(n=cap),
            "ts": time.time()})
        yield "done", {"stopped": "iteration_limit"}

    def agent_reply(self, session_id, content, cancel=None):
        """Agent counterpart of chat_reply; same refusal-before-first-yield
        contract, plus: refuses while a call awaits approval."""
        session = self.chats.get(session_id)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty message")
        # Queued calls only block when the head genuinely awaits an operator:
        # the iteration cap leaves the last turn's calls queued, and in auto
        # mode a new message must resume them (they drain at turn start), not
        # deadlock the session.
        calls = session.get("pending_calls") or []
        if calls and session.get("mode") == "approve" and \
                self._needs_approval(calls[0]["name"]):
            raise ToolError("a tool call awaits approval: answer it first")
        self._acquire_runtime(session["model"])
        try:
            self.chats.append(session_id, {"role": "user", "content": content,
                                           "ts": time.time()})
            yield "start", None
            yield from self._agent_turn(session_id, cancel)
            self._learn_from(session_id)
        finally:
            self._arm_idle()

    def chat_approve(self, session_id, approved, cancel=None):
        """Resolve the first pending call, then resume the agent loop."""
        session = self.chats.get(session_id)
        calls = session.get("pending_calls") or []
        if not calls:
            raise ToolError("nothing awaits approval")
        self._acquire_runtime(session["model"])
        try:
            yield "start", None
            call = calls[0]
            self.chats.set_pending_calls(session_id, calls[1:])
            if approved:
                yield from self._run_call(session_id, call, session["workspace"])
            else:
                output = "refused by operator"
                self.chats.append(session_id, {
                    "role": "tool", "tool_name": call["name"],
                    "content": output, "ts": time.time()})
                yield "tool_result", {"name": call["name"], "output": output}
            yield from self._agent_turn(session_id, cancel)
        finally:
            self._arm_idle()

    def list_jobs(self):
        """Every job the store knows, newest first. Tolerates foreign or
        half-written directories: a broken job must not blank the whole list."""
        jobs = []
        for job_id in self.store.list_jobs():
            try:
                status = self.store.status(job_id)
                spec = self.store.read_spec(job_id)
                created = status.get("created_at")
                duration = None
                result_path = self.store.job_dir(job_id) / "result.json"
                if created is not None and result_path.exists():
                    duration = result_path.stat().st_mtime - created
            except (JobStoreError, OSError, ValueError, KeyError):
                continue
            jobs.append({"job_id": job_id, "status": status["status"],
                         "kind": spec.get("kind", "delegate"),
                         "created_at": created,
                         "stage": status.get("stage"), "attempt": status.get("attempt"),
                         "model": status.get("model"),
                         "heartbeat_age": status.get("heartbeat_age"),
                         "project": spec.get("project"), "goal": spec.get("goal"),
                         "duration": duration})
        jobs.sort(key=lambda j: j["created_at"] or 0, reverse=True)
        return {"jobs": jobs}

    def _tail(self, job_id, n):
        lines = self.store.read_log(job_id).splitlines()
        return "\n".join(lines[-n:])


def _ok(req_id, payload):
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}}


def _tool_error(req_id, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": message}], "isError": True}}


def handle(msg, factory):
    method = msg.get("method")
    req_id = msg.get("id")
    if req_id is None:
        return None  # a notification; nothing to answer

    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION)
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "local-factory", "version": "0.3.0"}}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = getattr(factory, name, None) if name in {t["name"] for t in TOOLS} else None
        if fn is None:
            return _tool_error(req_id, "unknown tool: {!r}".format(name))
        try:
            return _ok(req_id, fn(**args))
        except (ConfigError, JobNotFound, ToolError) as e:
            return _tool_error(req_id, "{}: {}".format(type(e).__name__, e))
        except TypeError as e:
            return _tool_error(req_id, "bad arguments for {}: {}".format(name, e))

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": "method not found: {}".format(method)}}


def serve(stdin, stdout, factory):
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # a malformed line is not a reason to take the server down
            reply = handle(msg, factory)
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()
    finally:
        # The client closed stdin; whatever this server spawned leaves with it.
        factory.shutdown()


def _cli():
    ap = argparse.ArgumentParser(description="Factory MCP server (stdio).")
    ap.add_argument("--jobs", default=str(ROOT / "jobs"))
    ap.add_argument("--config", default=str(ROOT / "factory.toml"))
    a = ap.parse_args()
    serve(sys.stdin, sys.stdout, Factory(a.jobs, a.config))


if __name__ == "__main__":
    _cli()

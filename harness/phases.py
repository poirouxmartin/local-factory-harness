"""Where an agent session is, and what it may not skip.

The carnet gives the agent a plan; `session_analysis` gives it a price. Neither
says WHERE the session is: exploring, planning, implementing or verifying all
look identical to the harness, so nothing could say "you have read forty files
and written nothing" or "you ticked the last step without running a test".

The phase is derived from the transcript, never from what the model declares:
the session that ticked 5/5 steps against an empty diff (live 2026-07-25) would
have declared `verify` just as confidently.

Enforcement is hybrid by operator decision (2026-07-27): one hard refusal, where
the check is cheap and the verdict unambiguous, and signals for everything else.
Pure functions, no I/O -- `factory_mcp` owns the wiring.

Design: docs/design/specs/2026-07-27-phase-engine-design.md
"""
import json
import re

PHASES = ("explore", "plan", "implement", "verify")

WRITE_TOOLS = ("write_file", "edit_file")

# A command that runs a suite or a checker. Matched on the action segment with
# no shell parsing, the same restraint as `carnet._action_of`: a list that
# misses an exotic runner costs a signal, a list that guesses costs a lie.
_VERIFY_RE = re.compile(
    r"\b(pytest|vitest|jest|mocha|tox|unittest|nose2|ruff|flake8|eslint|"
    r"tsc|mypy|pyright|cargo\s+test|go\s+test|dotnet\s+test|"
    r"(?:npm|yarn|pnpm)\s+(?:test|run\s+(?:test|lint|typecheck|check))|"
    r"make\s+(?:test|check|lint))\b", re.IGNORECASE)

# The floor at which the carnet has already asked for a plan
# (`carnet.PLAN_NUDGE_AT`) and been ignored. Below it, a session that was handed
# one file to write owes nobody a plan.
BLOCK_AFTER_ACTIONS = 3

# Bookkeeping tool results are not work: same list as `carnet.BOOKKEEPING`, kept
# here so a pure module needs no import cycle.
BOOKKEEPING = ("system", "note", "wall", "plan", "step", "progress",
               "handback")

MAX_ALERTS = 4
LINE_CHARS = 160

PHASE_LINE = "- PHASE {phase} ({done}/{total} etapes faites)"
UNVERIFIED_LINE = ("- NON VERIFIE: toutes les etapes sont cochees mais aucune "
                   "commande de test n'a tourne depuis la derniere ecriture. "
                   "Lance la suite avant de rendre.")
NO_PLAN_GATE = ("refused: {n} actions without a plan. Call set_plan first, with "
                "one verifiable end condition per step (prefer file:<path>), "
                "then write.")
COST_LINE = "- COUT DEJA PAYE: {detail} ({cost_s} s / {tokens} tok)"

# Findings worth quoting back at the model: the ones it can act on. Kept in step
# with `session_analysis.MODEL_FINDINGS`; a harness fact (a degraded map, a slow
# tool) teaches a model nothing.
QUOTED_FINDINGS = ("replayed_calls", "hot_target", "oversized_output",
                   "failing_tool", "output_truncated")


def _clip(line):
    return line if len(line) <= LINE_CHARS else line[:LINE_CHARS - 1] + "…"


def _args(call):
    """A call's arguments, whether the server sent a dict or JSON text."""
    args = (call.get("function") or {}).get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return {}
    return args if isinstance(args, dict) else {}


def _failed(output):
    """True when a tool result is a failure or a refusal rather than work.
    `agent_tools` returns "error: ...", the gate below returns "refused: ..."."""
    text = str(output or "").lstrip().lower()
    return text.startswith("error:") or text.startswith("refused:")


def is_verification(command):
    """True when this command runs a suite or a checker."""
    return bool(_VERIFY_RE.search(" ".join(str(command or "").split())))


def evidence(messages):
    """What the transcript PROVES, as message indexes (-1 for never).

    Writes and commands are read from the assistant's own `tool_calls`: the tool
    message carries the output, not the arguments. The plan and the ticks come
    from the carnet bookkeeping results, which are the authoritative verdicts --
    a refused `step_done` leaves no `step` message behind.
    """
    ev = {"plan_at": -1, "wrote_at": -1, "verified_at": -1, "actions": 0}
    for i, msg in enumerate(messages or []):
        role = msg.get("role")
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                name = (call.get("function") or {}).get("name")
                # A verification has to be read from the request: the result
                # holds the suite's output, and only the arguments say which
                # command ran. A red suite still counts -- it verified.
                if name == "run_command" and is_verification(
                        _args(call).get("command")):
                    ev["verified_at"] = i
        elif role == "tool":
            name = msg.get("tool_name")
            if name == "plan":
                ev["plan_at"] = i
            elif name in WRITE_TOOLS and not _failed(msg.get("content")):
                # The RESULT, not the request: the gate answers a refused write
                # under the same tool name, and an intention that never touched
                # the disk is not implementation.
                ev["wrote_at"] = i
            elif name not in BOOKKEEPING:
                ev["actions"] += 1
    return ev


def _steps(plan):
    return (plan or {}).get("steps") or []


def verified_after_write(ev):
    """True when a suite ran since the last thing was written. A verification
    that predates the write proves nothing about the write."""
    return ev["verified_at"] > ev["wrote_at"] >= 0 or (
        ev["verified_at"] >= 0 and ev["wrote_at"] < 0)


def phase_of(plan, ev):
    """Which phase the evidence puts this session in."""
    steps = _steps(plan)
    if not steps:
        return "explore"
    if all(s.get("done") for s in steps):
        return "verify"
    # Written since the plan was set: what was written while exploring was not
    # written against a step.
    if ev["wrote_at"] > ev["plan_at"]:
        return "implement"
    return "plan"


def gate(call_name, plan, ev, mode="auto"):
    """May this call run? `(ok, reason)`.

    One hard refusal only: writing with no plan once the session is underway.
    Everything else the phases care about is a signal -- a workspace with no
    test runner would lock shut behind a verification gate, and the carnet's own
    rule is signal before block.

    `approve` mode is exempt: the operator sees every call before it runs and
    does not need the harness second-guessing them.
    """
    if mode != "auto" or call_name not in WRITE_TOOLS:
        return True, ""
    if _steps(plan) or ev["actions"] < BLOCK_AFTER_ACTIONS:
        return True, ""
    return False, NO_PLAN_GATE.format(n=ev["actions"])


def alerts(plan, ev, findings):
    """The lines the carnet tail carries for this turn, costliest first.

    These ride the TAIL, not the system prompt: the prompt is the KV prefix and
    is frozen for the life of the session (a mutated prefix costs ~1 s/turn,
    measured 22/07), which is why lessons only reach the next session. A
    trailing message invalidates nothing before it, so the agent can be told
    what it is wasting inside the session that is wasting it.
    """
    out = []
    steps = _steps(plan)
    if steps:
        out.append(_clip(PHASE_LINE.format(
            phase=phase_of(plan, ev),
            done=sum(1 for s in steps if s.get("done")), total=len(steps))))
        if all(s.get("done") for s in steps) and not verified_after_write(ev):
            out.append(_clip(UNVERIFIED_LINE))
    quoted = sorted((f for f in findings or []
                     if f.get("code") in QUOTED_FINDINGS),
                    key=lambda f: -(f.get("weight_s") or 0.0))
    for found in quoted:
        if len(out) >= MAX_ALERTS:
            break
        out.append(_clip(COST_LINE.format(
            detail=found.get("detail", ""), cost_s=found.get("cost_s", 0),
            tokens=found.get("cost_tokens", 0))))
    return out[:MAX_ALERTS]

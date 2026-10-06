"""One live agent turn, in auto mode, on a real task in a real repository.

This is the validation the reliability loop (phases 1-3) never had: every test
so far is offline replay or a seeded toy workspace. What has to be observed
here, and cannot be observed anywhere else:

  - does the model actually call `set_plan` up front, with end conditions the
    harness can tick (`file:<path>`), rather than narrating a plan in prose;
  - does `step_done` get refused when the claimed file does not exist (A.2),
    and does the harness tick the mechanical milestones by itself;
  - does the turn end on a `handback` when it meets a wall only the operator
    can clear, instead of inventing a way around it.

Nothing is mocked: real llama-server, real tools, real git worktree. The turn
is consumed synchronously, so the wall-clock cap below is the only escape --
`agent_max_iterations` is 0 by design (the repetition guard is the brake).

Long GPU run -> launch detached (Start-Process), never through a task runner.
"""
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "harness"))

import carnet  # noqa: E402
import factory_mcp  # noqa: E402

MODEL = os.environ.get("LIVE_MODEL", "qwen3-coder:30b")
WORKSPACE = Path(os.environ.get("LIVE_WORKSPACE", str(ROOT / "experiments" / "bv-agent")))
TOOLSET = os.environ.get("LIVE_TOOLSET", "base")
# A turn that has run this long has stopped being evidence and started being a
# GPU bill. Recorded as `timeout`, never silently.
CAP_S = int(os.environ.get("LIVE_CAP_S", "3600"))
TESTS = ["node", "--test", "backend/test/!(bots|physics).test.js"]

STAMP = time.strftime("%Y%m%d-%H%M%S")
OUT = HERE / ("run_" + STAMP)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    log = io.open(OUT / "run.log", "w", encoding="utf-8", errors="replace")
    ev_fh = io.open(OUT / "events.jsonl", "w", encoding="utf-8", errors="replace")

    def say(m):
        line = "[{}] {}".format(time.strftime("%H:%M:%S"), m)
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    brief = (HERE / "brief.md").read_text(encoding="utf-8")
    factory = factory_mcp.Factory(ROOT / "jobs", ROOT / "factory.toml")
    sid = factory.chat_create(MODEL, kind="agent", mode="auto",
                              workspace=str(WORKSPACE),
                              toolset=TOOLSET)["session_id"]
    say("session {}  model={}  toolset={}  ws={}".format(
        sid, MODEL, TOOLSET, WORKSPACE))
    say("baseline: " + run_tests())

    t0 = time.time()
    events = []
    gen = factory.agent_reply(sid, brief)
    stopped = None
    text = {"thinking": [], "chunk": []}
    try:
        for kind, payload in gen:
            events.append((kind, payload))
            # Thinking and answer arrive one delta at a time. Logging each
            # would bury the tool calls, which are the evidence; keep the text
            # whole and write it once per phase break.
            if kind in text:
                text[kind].append(str(payload))
                continue
            for phase in ("thinking", "chunk"):
                if text[phase]:
                    say("  .. {} ({} chars): {}".format(
                        phase, sum(len(p) for p in text[phase]),
                        "".join(text[phase])[:600].replace("\n", " ")))
                    text[phase] = []
            ev_fh.write(json.dumps(
                {"t": round(time.time() - t0, 1), "kind": kind,
                 "payload": payload}, ensure_ascii=False, default=str) + "\n")
            ev_fh.flush()
            say(describe(kind, payload))
            if time.time() - t0 > CAP_S:
                stopped = "timeout"
                say("!! wall-clock cap {}s reached, closing the turn".format(CAP_S))
                gen.close()
                break
    except Exception as e:  # a live run must record its own crash
        stopped = "crash"
        say("!! {!r}".format(e))
    dt = time.time() - t0
    for phase in ("thinking", "chunk"):
        if text[phase]:
            say("  .. {} ({} chars): {}".format(
                phase, sum(len(p) for p in text[phase]),
                "".join(text[phase])[:600].replace("\n", " ")))

    session = factory.chats.get(sid)
    report(say, session, events, dt, stopped)
    (OUT / "session.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "diff.patch").write_text(git("diff"), encoding="utf-8")

    try:
        factory.mcp.close()
        factory._release_runtime()
    except Exception as e:
        say("teardown: {!r}".format(e))
    log.close()
    ev_fh.close()


def describe(kind, payload):
    if kind == "tool_call":
        args = json.dumps(payload.get("arguments"), ensure_ascii=False,
                          default=str)
        return "  -> {} {}".format(payload.get("name"), args[:400])
    if kind == "tool_result":
        out = (payload.get("output") or "").replace("\n", " ")
        return "  <- {} {}".format(payload.get("name"), out[:300])
    return "  ** {} {}".format(kind, str(payload)[:400].replace("\n", " "))


def run_tests():
    try:
        p = subprocess.run(TESTS, cwd=str(WORKSPACE), capture_output=True,
                           text=True, timeout=600, shell=False)
    except Exception as e:
        return "tests could not run: {!r}".format(e)
    tail = [l for l in (p.stdout or "").splitlines()
            if l.startswith(("# tests", "# pass", "# fail")) or
            " tests " in l or " pass " in l or " fail " in l]
    return "exit={} {}".format(p.returncode, " | ".join(tail[-4:]) or
                               (p.stdout or p.stderr or "")[-200:])


def git(*args):
    p = subprocess.run(["git"] + list(args), cwd=str(WORKSPACE),
                       capture_output=True, text=True)
    return p.stdout


def report(say, session, events, dt, stopped):
    kinds = {}
    for k, _ in events:
        kinds[k] = kinds.get(k, 0) + 1
    calls = [p for k, p in events if k == "tool_call"]
    names = [c.get("name") for c in calls]
    plan = carnet.project(session.get("messages") or []).get("plan")

    say("")
    say("--- verdict ---")
    say("wall clock {:.0f}s  stopped={}  events={}".format(dt, stopped, kinds))
    say("tool calls {}: {}".format(len(calls), names))
    for tool in ("set_plan", "step_done", "log_wall", "remember",
                 "ask_operator", "request_resource"):
        say("  {:<17} {}".format(tool, names.count(tool)))
    if plan:
        say("plan goal: {}".format(plan.get("goal")))
        for i, step in enumerate(plan.get("steps") or [], 1):
            say("  [{}] {}. {}  done_when={}".format(
                "x" if step.get("done") else " ", i, step.get("step"),
                step.get("done_when")))
    else:
        say("plan: NONE -- the agent never called set_plan")
    say("handbacks: {}".format([p for k, p in events if k == "handback"]))
    say("git diff --stat:\n" + git("diff", "--stat"))
    say("suite after: " + run_tests())


if __name__ == "__main__":
    main()

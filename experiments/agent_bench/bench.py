"""Drive real agent turns through the real Factory and record what they cost.

Each task runs in a fresh git workspace through `Factory.agent_reply` -- real
llama-server, real tools, auto mode. Nothing is mocked, because the things
being measured (KV cache reuse, tool round-trips, compaction) only exist on a
real turn.

Scored per (variant, task, run):
  - tool_calls      how many tool calls the turn made
  - tool_errors     tool results that failed, by exit code as well as by
                    "error:" -- a failed shell command wastes the same
                    round-trip (2026-07-23, measurement bug 2)
  - edit_misses     edit_file calls that returned "0 matches"
  - ok              the task's own assertion on the final workspace state
  - eval_tokens     generated tokens (cost)
  - seconds

Long GPU run -> launch detached (Start-Process), never via a task runner.
"""
import io
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(HERE))

import factory_mcp  # noqa: E402
import chat_context  # noqa: E402
import llama_client  # noqa: E402
import workspace_memory  # noqa: E402
import accounting  # noqa: E402
from tasks import TASKS, seed  # noqa: E402

MODEL = os.environ.get("BENCH_MODEL", "qwen3-coder:30b")
RUNS = int(os.environ.get("BENCH_RUNS", "5"))
ONLY = os.environ.get("BENCH_ONLY")  # substring filter on task name, for smoke
TOOLSET = os.environ.get("BENCH_TOOLSET", "base")
WORK = HERE / "_work"

# Line item 5 A/B: turn tool-output eviction off by keeping every message.
# One line, no shipped code changes -- the off arm of the eviction trial.
if os.environ.get("BENCH_NO_EVICT"):
    chat_context.EVICT_KEEP = 10 ** 6

# A variant is a name plus a callable that mutates the world before a run. The
# bench does not know what it switches -- agent.md, a toolset, a server flag.
# Keep the arms to two: one variable at a time.
VARIANTS = {"A": lambda: None}


def score_events(events, tokenize=None):
    calls = [p for k, p in events if k == "tool_call"]
    results = [p for k, p in events if k == "tool_result"]
    # A tool that refused says "error:"; a shell command that failed says it in
    # its exit code, and that round-trip is just as wasted. Counting only the
    # first hid every failed run_command of the 07-23 pass 1 --
    # `workspace_memory.failed` is the rule that already gets this right.
    tool_errors = sum(1 for r in results
                      if workspace_memory.failed(r.get("output")))
    edit_misses = sum(1 for r in results
                      if "0 matches" in (r.get("output") or ""))
    # eval_count on the final done is the last model call's, not the turn's
    # total; sum every model call's tokens instead (each final-answer/tool
    # cycle carries one). Robust to the stopped-turn payloads that lack it.
    done_payloads = [p for k, p in events if k == "done"]
    eval_tokens = sum(p.get("eval_count") or 0 for p in done_payloads) or None
    # Line items 4 and 5: how many times the turn crossed the compaction
    # threshold. `_agent_turn` yields ("compacting", True) each time; a session
    # with zero is one that never exercised compaction or eviction, and saying
    # so is the honest verdict when the corpus is too small (2026-07-23).
    compactions = sum(1 for k, p in events if k == "compacting")
    out = {
        "tool_calls": len(calls),
        "tool_errors": tool_errors,
        "edit_misses": edit_misses,
        "eval_tokens": eval_tokens,
        "compactions": compactions,
        "stopped": (done_payloads[-1].get("stopped") if done_payloads else None),
        "call_names": [c.get("name") for c in calls],
    }
    if tokenize:
        # Line item 6: the share of generation that never reaches the user.
        out["think_tokens"] = tokenize(
            "".join(p for k, p in events if k == "thinking"))
        out["write_tokens"] = tokenize(
            "".join(p for k, p in events if k == "chunk"))
        # `eval_tokens` above is the last model call's alone -- the agent loop
        # yields `done` once per turn (factory_mcp.py:1225), so everything
        # generated to reach the final answer is missing from it. Measured
        # 07-23: 41 against 228 written tokens. The tool calls ARE that
        # generation, and a write_file carries its whole file in `arguments`.
        out["call_tokens"] = tokenize(json.dumps(
            [{"name": c.get("name"), "arguments": c.get("arguments")}
             for c in calls], ensure_ascii=False)) if calls else 0
        out["gen_tokens"] = (out["think_tokens"] + out["write_tokens"]
                             + out["call_tokens"])
    return out


def prompt_segments(factory, sid):
    """The blocks the turn's last model call sent, in wire order.

    Rebuilt from the sources `_agent_turn` reads (factory_mcp.py:1098-1121):
    the system parts, the two per-session blocks it appends to them, then the
    tools, the summary and the history. Both blocks are decided once per
    session and kept, so reading them after the turn returns the same text the
    turn sent -- pricing them separately is what keeps them out of `overhead`.
    """
    session = factory.chats.get(sid)
    tools = factory._session_tools(session)
    parts = factory_mcp._agent_system_parts(
        session["workspace"], [t["function"]["name"] for t in tools])
    for name, block in (
            ("lessons", factory._lesson_block(sid)),
            ("memory", factory._memory_block(sid, session["workspace"]))):
        if block:
            parts.append((name, block))
    raw = [{"role": m["role"], "content": m["content"],
            "tool_calls": m.get("tool_calls")}
           for m in accounting.last_call_messages(session["messages"])]
    # Price the messages the last call ACTUALLY sent, not the raw history.
    # Once a session evicts tool outputs or carries a summary, the two differ,
    # and pricing raw makes `reconcile` report a negative template overhead
    # (measured 2026-07-23, huge_reformat: blocks 28k vs wire 18k, the ~10k
    # gap being exactly what eviction stubbed). build_prompt is the same
    # function _agent_turn builds its wire with (factory_mcp.py:1123-1140).
    summary = session.get("summary")
    num_ctx = min(session.get("num_ctx") or factory_mcp.CHAT_NUM_CTX,
                  factory_mcp.CHAT_NUM_CTX)
    budget = chat_context.prompt_budget(num_ctx)
    sent, _info = chat_context.build_prompt(
        [], summary, raw, budget, chat_context.ratio(session))
    # build_prompt prepends the summary as the only system message; segments
    # prices the summary separately, so hand it the tail without that message.
    tail = sent[1:] if summary else sent
    # The tool count travels with the budget: `mcp_registry.tool_specs` skips a
    # server that fails to start WITHOUT saying so (mcp_registry.py:65-68), so
    # a `tools` block priced at 9 tools when the toolset declares 15 is a dead
    # server, not a cheap toolset.
    return accounting.segments(parts, tools, summary, tail), len(tools), raw


def run_one(factory, task, ws):
    seed(ws, task["files"])
    sid = factory.chat_create(MODEL, kind="agent", mode="auto",
                              workspace=str(ws), toolset=TOOLSET)["session_id"]
    t0 = time.time()
    events = list(factory.agent_reply(sid, task["goal"]))
    dt = time.time() - t0

    # The turn is over and the runtime is still up: price the prompt with the
    # server's own tokenizer, on the same blocks the turn actually sent.
    url = factory._acquire_runtime(MODEL)
    tok = lambda text: llama_client.tokenize(url, text)  # noqa: E731
    done = [p for k, p in events if k == "done"]
    metrics = done[-1] if done else {}

    s = score_events(events, tokenize=tok)
    segs, s["n_tools"], raw = prompt_segments(factory, sid)
    s["budget"] = accounting.count(segs, tok)
    s["recon"] = accounting.reconcile(s["budget"], metrics.get("prompt_count"))
    s["cache"] = accounting.cache_report(metrics)
    # Line item 5: what eviction stubbed off the wire = the raw history minus
    # the messages actually sent, in tokens. Zero on a short session.
    raw_msg_tok = tok("\n".join(accounting._message_text(m) for m in raw))
    sent_msg_tok = dict(s["budget"]).get("messages", 0)
    s["raw_messages"] = raw_msg_tok
    s["evicted_tokens"] = max(0, raw_msg_tok - sent_msg_tok)
    s["seconds"] = round(dt, 1)
    try:
        s["ok"] = bool(task["ok"](ws))
    except Exception as e:
        s["ok"] = False
        s["ok_error"] = str(e)
    s["session"] = sid
    return s


def main():
    cfg = ROOT / "factory.toml"
    factory = factory_mcp.Factory(ROOT / "jobs", cfg)
    fh = io.open(HERE / "results.jsonl", "w", encoding="utf-8")
    log = io.open(HERE / "bench.log", "w", encoding="utf-8")

    def say(m):
        print(m); log.write(m + "\n"); log.flush()

    say("bench start {}  model={}  runs={}  toolset={}".format(
        time.strftime("%H:%M:%S"), MODEL, RUNS, TOOLSET))
    rows = []
    for variant, apply_variant in VARIANTS.items():
        apply_variant()
        for task in TASKS:
            if ONLY and ONLY not in task["name"]:
                continue
            for run in range(RUNS):
                say("  {}/{}/{} ...".format(variant, task["name"], run))
                try:
                    s = run_one(factory, task, WORK / task["name"])
                except Exception as e:
                    s = {"error": repr(e)}
                finally:
                    # An MCP server is spawned with the workspace as its CWD
                    # (mcp_registry.py:47) and the registry keeps it alive per
                    # (server, workspace). On Windows a CWD is a locked
                    # handle, so the next run of the same task cannot delete
                    # the directory to reseed it: 28 of 35 web-toolset runs
                    # died on PermissionError before this close (23/07). The
                    # product bug it exposes is filed in the registry; here
                    # the bench just has to survive it.
                    factory.mcp.close()
                s.update(variant=variant, task=task["name"], run=run)
                rows.append(s)
                fh.write(json.dumps(s) + "\n"); fh.flush()
                say("    ok={ok} calls={calls} err={err} miss={miss} "
                    "prompt={wire} (repaid {repaid}, +{oh} tmpl, "
                    "{nt} tools) compact={cp} "
                    "think/write={th}/{wr} {sec}s".format(
                        ok=s.get("ok"), calls=s.get("tool_calls"),
                        err=s.get("tool_errors"), miss=s.get("edit_misses"),
                        wire=s.get("cache", {}).get("wire"),
                        repaid=s.get("cache", {}).get("repaid"),
                        oh=s.get("recon", {}).get("overhead"),
                        nt=s.get("n_tools"),
                        cp=s.get("compactions"),
                        th=s.get("think_tokens"), wr=s.get("write_tokens"),
                        sec=s.get("seconds")))
    factory._release_runtime()
    for line in summarize(rows):
        say(line)
    say("bench done {}".format(time.strftime("%H:%M:%S")))
    fh.close(); log.close()


def summarize(rows):
    """Per-variant totals, then the per-task split -- the log is the report."""
    def agg(sub):
        n = len(sub) or 1
        num = lambda k: sum(r.get(k) or 0 for r in sub)  # noqa: E731
        # `gen` is the turn's whole generation (think + answer + the arguments
        # of every call); `eval_tokens` would be the last model call's only.
        return ("ok {}/{}  calls {:.1f}  err {}  miss {}  gen {:.0f}  "
                "prompt {:.0f}  {:.1f}s".format(
                    sum(1 for r in sub if r.get("ok")), len(sub),
                    num("tool_calls") / n, num("tool_errors"),
                    num("edit_misses"), num("gen_tokens") / n,
                    sum((r.get("cache") or {}).get("wire") or 0
                        for r in sub) / n,
                    num("seconds") / n))

    out = ["", "--- summary (means per run, err/miss are totals) ---"]
    variants = sorted({r.get("variant") for r in rows})
    for v in variants:
        out.append("{}  {}".format(v, agg([r for r in rows if r.get("variant") == v])))
    for task in [t["name"] for t in TASKS]:
        sub = [r for r in rows if r.get("task") == task]
        if not sub:
            continue
        out.append("  " + task)
        for v in variants:
            out.append("    {}  {}".format(
                v, agg([r for r in sub if r.get("variant") == v])))
    return out


if __name__ == "__main__":
    main()

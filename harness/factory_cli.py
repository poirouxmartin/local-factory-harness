"""Operator CLI: drive the factory from a terminal, no MCP client required.

This is the manual-takeover guarantee (docs/vision.md): running out of Claude
credits must never mean a stalled factory. Same Factory as the MCP server --
argv in, JSON out. `result --save-diff` writes the patch to a file so
`git apply` closes the loop by hand.

    py -3 harness/factory_cli.py delegate --project demo --goal "fix add()" \
        --test path/to/test_add.py --target calc.py
    py -3 harness/factory_cli.py status j_7f3a
    py -3 harness/factory_cli.py result j_7f3a --save-diff out.patch
"""
import argparse
import json
import sys
from pathlib import Path

import bootstrap
from factory_config import ConfigError, load_projects, resolve_project
from factory_mcp import Factory, ToolError
from goal_store import GoalStore, GoalStoreError
from job_store import JobNotFound
import regress

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = str(ROOT / "regression" / "corpus.toml")

DEFAULT_BRIEF = """# {title}

## Objective
(what "done" looks like, in product terms)

## Acceptance criteria
- [ ] (verifiable statements a reviewer can check)
"""


def _parser():
    ap = argparse.ArgumentParser(description="Drive the factory by hand.")
    ap.add_argument("--jobs", default=str(ROOT / "jobs"))
    ap.add_argument("--config", default=str(ROOT / "factory.toml"))
    ap.add_argument("--goals", default=str(ROOT / "goals"))
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("delegate", help="queue a job on the local ladder")
    d.add_argument("--project", required=True, help="a project key from factory.toml")
    d.add_argument("--goal", required=True)
    d.add_argument("--test", action="append", required=True, dest="tests",
                   help="path to a test file; repeatable; its basename becomes the spec path")
    d.add_argument("--target", action="append", required=True, dest="targets",
                   help="repo-relative file the model may rewrite; repeatable")
    d.add_argument("--edit-mode", choices=("whole", "diff"), dest="edit_mode",
                   help="force whole-file or SEARCH/REPLACE output; "
                        "default: auto rule")
    d.add_argument("--context", action="append", default=[], dest="contexts",
                   help="repo-relative read-only reference file; repeatable")

    dr = sub.add_parser("draft", help="ask the local spec writer for a proposal")
    dr.add_argument("--project", required=True, help="a project key from factory.toml")
    dr.add_argument("--goal", required=True)

    for name, help_ in (("status", "stage, attempt, tests passing"),
                        ("result", "the verdict and the diff"),
                        ("cancel", "kill a running job"),
                        ("apply", "put a succeeded job's diff in the working tree")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("job_id")
    sub.choices["result"].add_argument("--save-diff", metavar="PATH",
                                       help="also write the diff to PATH for git apply")
    lg = sub.add_parser("log", help="tail of the job's log")
    lg.add_argument("job_id")
    lg.add_argument("--tail", type=int, default=100)

    g = sub.add_parser("goal", help="manager bookkeeping (spec 2026-07-17)")
    gsub = g.add_subparsers(dest="gcommand", required=True)

    gn = gsub.add_parser("new", help="open a goal from a brief")
    gn.add_argument("--project", required=True, help="a project key from factory.toml")
    gn.add_argument("--title", required=True)
    gn.add_argument("--file", help="path to the brief (goal.md); a template is used without it")

    ga = gsub.add_parser("add", help="append a backlog item")
    ga.add_argument("goal_id")
    ga.add_argument("--title", required=True)

    gi = gsub.add_parser("item", help="update a backlog item")
    gi.add_argument("goal_id")
    gi.add_argument("item_id")
    gi.add_argument("--status", choices=("pending", "delegated", "review", "merged", "dropped"))
    gi.add_argument("--job", help="job id to record against the item; repeatable via multiple calls")
    gi.add_argument("--note")

    for name, help_ in (("status", "state + per-status item counts"),
                        ("show", "the brief")):
        p = gsub.add_parser(name, help=help_)
        p.add_argument("goal_id")

    for name in ("done", "abandon"):
        p = gsub.add_parser(name, help="finish the goal as " + name)
        p.add_argument("goal_id")
        p.add_argument("--debrief", help="closing note, written to debrief.md")

    gsub.add_parser("list", help="all goal ids")

    r = sub.add_parser("regress", help="replay the frozen job corpus (spec 2026-07-18)")
    rsub = r.add_subparsers(dest="rcommand", required=True)
    rc = rsub.add_parser("check", help="validate the corpus, no GPU")
    rc.add_argument("--corpus", default=DEFAULT_CORPUS)
    rr = rsub.add_parser("run", help="replay a tier; exit 2 on confirmed regression")
    rr.add_argument("--corpus", default=DEFAULT_CORPUS)
    rr.add_argument("--tier", choices=("smoke", "full"), default="smoke")
    rr.add_argument("--only", help="comma-separated job ids")
    rb = rsub.add_parser("baseline", help="seed missing baselines, or promote a run")
    rb.add_argument("--corpus", default=DEFAULT_CORPUS)
    rb.add_argument("--from-run", dest="from_run", metavar="TS",
                    help="promote regression/runs/<TS> results as the new baselines")
    rb.add_argument("--only", help="comma-separated job ids")

    sub.add_parser("bootstrap",
                   help="arm the hooks, scaffold factory.local.toml, report "
                        "what this machine is missing")
    return ap


def _dispatch_goal(a, store):
    if a.gcommand == "new":
        # The goal perimeter is the delegation perimeter: refuse unknown projects
        # before anything exists on disk, like Factory.delegate does.
        resolve_project(load_projects(a.config), a.project)
        brief = (Path(a.file).read_text(encoding="utf-8") if a.file
                 else DEFAULT_BRIEF.format(title=a.title))
        return {"goal_id": store.create(a.title, a.project, brief)}
    if a.gcommand == "add":
        return {"item_id": store.add_item(a.goal_id, a.title)}
    if a.gcommand == "item":
        return store.update_item(a.goal_id, a.item_id, status=a.status,
                                 job_id=a.job, note=a.note)
    if a.gcommand == "status":
        return store.status(a.goal_id)
    if a.gcommand == "show":
        return {"goal_id": a.goal_id, "goal_md": store.read_goal_md(a.goal_id)}
    if a.gcommand == "done":
        return store.finish(a.goal_id, "done", debrief=a.debrief)
    if a.gcommand == "abandon":
        return store.finish(a.goal_id, "abandoned", debrief=a.debrief)
    return {"goals": store.list_goals()}


def _dispatch_regress(a):
    reg_root = Path(a.corpus).parent
    if a.rcommand == "check":
        return regress.check(a.corpus, a.jobs, a.config)
    only = a.only.split(",") if getattr(a, "only", None) else None
    if a.rcommand == "run":
        return regress.run_suite(a.corpus, a.jobs, a.config, tier=a.tier, only=only)
    if getattr(a, "from_run", None):
        return regress.promote_baselines(reg_root / "runs" / a.from_run,
                                         reg_root / "baselines", only=only)
    return regress.seed_baselines(a.corpus, a.jobs, reg_root / "baselines")


def _dispatch(a, factory):
    if a.command == "delegate":
        tests = {}
        for raw in a.tests:
            p = Path(raw)
            tests[p.name] = p.read_text(encoding="utf-8")
        return factory.delegate(a.project, a.goal, tests, a.targets, a.contexts,
                                edit_mode=a.edit_mode)
    if a.command == "draft":
        return factory.draft(a.project, a.goal)
    if a.command == "status":
        return factory.job_status(a.job_id)
    if a.command == "result":
        result = factory.job_result(a.job_id)
        if a.save_diff and result.get("ready"):
            # A failed job still leaves its best attempt behind; saving that
            # turns "failed" into "N tests left to finish by hand".
            diff = result.get("diff") or \
                (result.get("best_attempt") or {}).get("diff", "")
            Path(a.save_diff).write_text(diff, encoding="utf-8")
            result["diff_saved_to"] = a.save_diff
        return result
    if a.command == "log":
        return factory.job_log(a.job_id, a.tail)
    if a.command == "apply":
        return factory.job_apply(a.job_id)
    return factory.job_cancel(a.job_id)


def main(argv=None, factory=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    a = _parser().parse_args(argv)
    try:
        if a.command == "goal":
            # Goals never need the Factory (no jobs dir, no MCP registry).
            payload = _dispatch_goal(a, GoalStore(a.goals))
        elif a.command == "regress":
            # Neither does the regression suite: it drives loop_job directly.
            payload = _dispatch_regress(a)
        elif a.command == "bootstrap":
            # No Factory: a clone that cannot resolve a project must still be
            # able to run the command that tells it why.
            payload = bootstrap.run(a.config, ROOT)
        else:
            payload = _dispatch(a, factory or Factory(a.jobs, a.config))
    except (ConfigError, JobNotFound, ToolError, GoalStoreError,
            regress.RegressError, OSError) as e:
        err.write("{}: {}\n".format(type(e).__name__, e))
        return 1
    json.dump(payload, out, indent=2)
    out.write("\n")
    if a.command == "regress" and a.rcommand == "run":
        if payload.get("regressions"):
            return 2  # scripts and CI must see a confirmed regression without parsing
        if payload.get("errors"):
            return 3  # infra errors: the run is inconclusive, not green
    return 0


if __name__ == "__main__":
    sys.exit(main())

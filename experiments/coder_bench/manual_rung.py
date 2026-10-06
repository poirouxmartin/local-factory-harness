"""Drive one bench rung by hand, so a model the ladder cannot call can still
be measured in the *bridee* condition.

`cloud_client` needs an API key; a Claude Code subagent has no such seam -- it
is a tool, not a `chat_fn`. This script splits the ladder at the one point that
matters: it builds the exact prompt `loop_job` would have sent (same worktree at
`base_sha`, same `build_prompt`, same edit mode, same failure feedback), and it
applies the reply through the same `patch` + judge path. Everything in between
-- the actual completion -- happens outside.

So a cell produced here is comparable to a `bancA-bride` cell on the harness
axis, and NOT comparable on the model-plumbing axis: the reply comes from an
agent runtime with its own system prompt. Report it as a proxy, never as a
`cloud_client` row.

    py -3.9 experiments/coder_bench/manual_rung.py init  --job j_3a6a7681 --run-dir runs/x
    py -3.9 experiments/coder_bench/manual_rung.py apply --job j_3a6a7681 --run-dir runs/x \
        --reply runs/x/j_3a6a7681/reply_1.txt
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from factory_config import load_projects, resolve_project          # noqa: E402
import feedback                                                    # noqa: E402
import loop_job                                                    # noqa: E402
from patch import PatchError, apply_edits, extract_edits, extract_files  # noqa: E402
from worktree import WorkTree                                      # noqa: E402

CONFIG = ROOT / "factory.toml"
JOBS = ROOT / "jobs"
FEEDBACK_CHARS = loop_job.FEEDBACK_CHARS   # same budget, same condenser


def cell_dir(run_dir, job_id):
    return Path(run_dir).resolve() / job_id


def load_spec(job_id):
    spec = json.loads((JOBS / job_id / "spec.json").read_text(encoding="utf-8"))
    archived = json.loads((JOBS / job_id / "result.json").read_text(encoding="utf-8"))
    spec["base_sha"] = archived["base_sha"]
    spec.pop("start_stage", None)
    return spec


class _Attached(object):
    """`apply` reuses the tree `init` built: re-creating it would both wipe the
    attempt under judgment and collide on the worktree branch."""

    def __init__(self, path):
        self.path = path


def setup(job_id, run_dir, create=True, cell=None):
    """Worktree at the archived commit + judge materialized, exactly as run_job.

    `cell` renames the worktree branch only: re-measuring a case after a harness
    change must not force deleting the branch that holds the earlier evidence.
    """
    spec = load_spec(job_id)
    project = resolve_project(load_projects(CONFIG), spec["project"])
    runner = loop_job.RUNNERS[project.runner]
    here = cell_dir(run_dir, job_id)
    here.mkdir(parents=True, exist_ok=True)
    if not create:
        return spec, project, runner, _Attached(here / "worktree"), here / "tests", here
    worktree = WorkTree.create(project.path, cell or job_id, here / "worktree",
                               base_sha=spec["base_sha"])
    tests_dir = runner.materialize_tests(here / "tests", spec["tests"],
                                         worktree.path, project_path=project.path)
    return spec, project, runner, worktree, tests_dir, here


def read_current(worktree, targets):
    current = {}
    for path in targets:
        p = worktree.path / path
        if p.exists():
            current[path] = p.read_text(encoding="utf-8")
    return current


def write_prompt(here, n, spec, current, context, last_output, lang, edit_mode):
    prompt = loop_job.build_prompt(spec.get("goal", ""), current, context,
                                   last_output, lang, edit_mode=edit_mode)
    path = here / "prompt_{}.txt".format(n)
    path.write_text(prompt, encoding="utf-8")
    return path, prompt


def cmd_init(a):
    spec, project, runner, worktree, tests_dir, here = setup(a.job, a.run_dir,
                                                             cell=a.cell)
    targets = spec["target_files"]
    edit_mode = loop_job.resolve_edit_mode(spec, worktree)
    lang = loop_job.fence_lang(targets)
    context = {p: (worktree.path / p).read_text(encoding="utf-8")
               for p in spec.get("context_files", []) or []}
    current = read_current(worktree, targets)

    (here / "system.txt").write_text(loop_job._system_prompt(), encoding="utf-8")
    path, prompt = write_prompt(here, 1, spec, current, context, "", lang, edit_mode)
    (here / "state.json").write_text(json.dumps(
        {"job": a.job, "attempt": 1, "edit_mode": edit_mode, "lang": lang,
         "targets": targets, "context_files": list(context)}, indent=1), encoding="utf-8")
    print("edit mode: {}\nsystem:  {}\nprompt:  {}  ({} chars, ~{} tokens)".format(
        edit_mode, here / "system.txt", path, len(prompt), len(prompt) // 4))


def cmd_apply(a):
    spec, project, runner, worktree, tests_dir, here = setup(a.job, a.run_dir,
                                                             create=False)
    state = json.loads((here / "state.json").read_text(encoding="utf-8"))
    n, targets = state["attempt"], state["targets"]
    edit_mode, lang = state["edit_mode"], state["lang"]
    context = {p: (worktree.path / p).read_text(encoding="utf-8")
               for p in state["context_files"]}
    reply = Path(a.reply).read_text(encoding="utf-8")
    current = read_current(worktree, targets)
    log = here / "log.txt"

    def note(line):
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line)

    try:
        if edit_mode == "diff":
            files = apply_edits(current, extract_edits(reply, targets))
        else:
            files = extract_files(reply, targets)
    except PatchError as e:
        note("attempt {}: {}".format(n, e))
        reminder = ("Your previous reply was rejected: {} -- reply again, "
                    "following the required format exactly.".format(e))
        state["attempt"] = n + 1
        (here / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
        path, _ = write_prompt(here, n + 1, spec, current, context, reminder,
                               lang, edit_mode)
        print("no_code -> next prompt: {}".format(path))
        return

    for path_, content in files.items():
        dst = worktree.path / path_
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")

    result = runner.run_tests(worktree.path, tests_dir, timeout=a.timeout)
    failing = (runner.failed_names(result.output)[:5] if result.tests_failed else [])
    note("attempt {}: {} passed, {} failed{}".format(
        n, result.tests_passed, result.tests_failed,
        " ({})".format(", ".join(failing)) if failing else ""))

    regression = None
    if result.passed and project.regression_cmd:
        reg = runner.run_regression(worktree.path, project.regression_cmd)
        regression = reg.passed
        note("regression: {}".format("PASS" if reg.passed else "FAIL"))

    if result.passed and regression is not False:
        note("SUCCESS at attempt {}".format(n))
        (here / "verdict.json").write_text(json.dumps(
            {"success": True, "attempts": n, "regression": regression}, indent=1),
            encoding="utf-8")
        return

    current = read_current(worktree, targets)
    state["attempt"] = n + 1
    (here / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    path, _ = write_prompt(here, n + 1, spec, current, context,
                           feedback.condense(result.output,
                                             runner.failed_names(result.output),
                                             FEEDBACK_CHARS),
                           lang, edit_mode)
    print("next prompt: {}".format(path))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--job", required=True)
        p.add_argument("--run-dir", required=True)
        if name == "init":
            p.add_argument("--cell", default=None,
                           help="worktree branch suffix; defaults to the job id")
        if name == "apply":
            p.add_argument("--reply", required=True)
            p.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args()
    (cmd_init if a.cmd == "init" else cmd_apply)(a)


if __name__ == "__main__":
    main()

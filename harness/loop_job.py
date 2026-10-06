"""The executant. Spawned detached; owns one job from spec.json to result.json.

It takes the GPU lock before it touches a model, which queues jobs without a
scheduler. It heartbeats from a background thread -- not from the loop -- because
a single 35b call can run for 900 s, ten times the liveness TTL.

What comes out is a diff on branch factory/<job_id>, never a commit and never a
push. Nothing reaches master without a human, or Opus, reading it first.

    py -3 harness/loop_job.py j_7f3a --jobs jobs --config factory.toml
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from factory_config import (ConfigError, load_llama_server, load_models, load_projects,
                            model_options, resolve_project, validate_context_files,
                            validate_targets, validate_tests)
from llama_server_manager import LlamaServerManager
from chat_store import ChatStore
import feedback
from gpu_lock import GpuLock
import lessons
from job_store import JobStore, seek_stdout_to_end
from patch import (AmbiguousAnchor, AnchorNotFound, PatchError,
                   apply_edits, extract_edits, extract_files)
import node_test_runner
import pytest_runner
import vitest_runner
from pytest_runner import REGRESSION_TIMEOUT
from worktree import WorkTree, forbidden_paths

# project.runner (factory.toml) picks the judge; all modules share the same
# surface: materialize_tests / run_tests / run_regression / OPERATOR_ERROR_CODES.
RUNNERS = {"pytest": pytest_runner, "vitest": vitest_runner, "node": node_test_runner}

ROOT = Path(__file__).resolve().parent.parent


class RegressionCommandError(RuntimeError):
    """The project's regression_cmd is wrong. That is the operator's bug, not the model's."""


class JudgeCommandError(RuntimeError):
    """The judge could not run at all -- same class of operator bug, other end
    of the job: without it the ladder reads "no tests ran" as "the model is
    wrong" and burns every rung proving it."""


# A test runner that started leaves traces even when it dies: a collection
# error, a traceback, a count, "no tests ran". A runner that never started
# leaves only its own complaint ("No module named pytest"). The distinction
# matters because the first is the model's problem and the second is not.
_JUDGE_EVIDENCE = ("collected", "passed", "failed", "error", "no tests ran",
                   "traceback")


def _judge_never_ran(result):
    if result.timed_out or result.returncode == 0:
        return False
    if result.tests_passed or result.tests_failed:
        return False
    low = (result.output or "").lower()
    return not any(sign in low for sign in _JUDGE_EVIDENCE)


DEFAULT_LADDER = [
    # The 14b rung was dropped 2026-07-18: across the 07/2026 audits it never
    # rescued a 7b failure -- every job it entered ended escalating to the 30b
    # anyway, at ~2 attempts' cost.
    {"model": "qwen2.5-coder:7b", "temperature": 0.1, "attempts": 2},
    {"model": "qwen3-coder:30b", "temperature": 0.7, "diff_temperature": 0.3,
     "attempts": 3},
    {"model": "qwen3.6:35b", "temperature": 0.6, "attempts": 3},
]

DEFAULT_GPU_TIMEOUT = 6 * 3600  # a queued job waits out the one ahead of it
HEARTBEAT_INTERVAL = 30.0

# Same role as chat_context.OUTPUT_RESERVE for the chat: one attempt's reply
# is bounded, so a looping model costs at most this many tokens. Sized for a
# thinking model: qwen3.6:35b spends ~6k tokens deliberating on a module-scale
# contract BEFORE the code (measured, blitzvolley re-run j_7e7c0bbb), so 8192
# left no variance headroom and truncations read as codeless replies.
LADDER_NUM_PREDICT = 16384

# The retry's whole view of what went wrong. Same budget as the chronological
# slice it replaces -- the fix is what fills it, not how much (feedback.py).
FEEDBACK_CHARS = 3000

# Diff-mode attempt 1 drowned at the 16k cap in 2/2 measured runs (audit
# 2026-07-18) without ever applying an edit; the winning attempts fit in
# ~1.3k tokens. Make attempt 1 fail fast (or succeed small) into the
# feedback loop that actually lands edits.
FIRST_DIFF_NUM_PREDICT = 4096

# Abnormal pytest exits (collection error, exit 2) are model-invariant: job
# j_bc119315 burned 10 attempts / 4 rungs / 688 s on the same error. The same
# signature this many times running aborts the run instead of escalating.
STRUCTURAL_ABORT_K = 3


def failure_signature(returncode, output):
    """Normalize an abnormal test run into a comparable signature: timings
    and whitespace vary between runs, the error itself does not."""
    tail = re.sub(r"\d+\.\d+s?", "", output[-800:])
    return "{}|{}".format(returncode, re.sub(r"\s+", " ", tail).strip())


def _system_prompt(persona="coder"):
    return ((ROOT / "agent.md").read_text(encoding="utf-8") + "\n\n---\n\n"
            + (ROOT / "personas" / (persona + ".md")).read_text(encoding="utf-8"))


def fence_lang(paths):
    """The fence tag the model is asked to reply in, from the target suffixes."""
    if all(p.endswith(".py") for p in paths):
        return "python"
    if any(p.endswith((".ts", ".tsx")) for p in paths):
        return "typescript"
    return "javascript"


EDIT_MODE_LINE_THRESHOLD = 300


def resolve_edit_mode(spec, worktree):
    """Same threshold as the start-stage-3 routing rule (2026-07-17): one body
    of measured evidence, two knobs."""
    mode = spec.get("edit_mode")
    if mode in ("whole", "diff"):
        return mode
    targets = spec["target_files"]
    if len(targets) > 1:
        return "diff"
    for path in targets:
        p = worktree.path / path
        if p.exists() and len(p.read_text(encoding="utf-8").splitlines()) \
                > EDIT_MODE_LINE_THRESHOLD:
            return "diff"
    return "whole"


def build_prompt(goal, current, context, last_output, lang="python", edit_mode="whole"):
    parts = ["TASK: {}".format(goal), ""]
    if edit_mode == "diff":
        parts.append(
            "Edit these files: {}. Reply ONLY with edits in this exact format "
            "-- for each edit a `### FILE: <path>` line, then a block:\n"
            "<<<<<<< SEARCH\n"
            "<lines copied EXACTLY from the current file>\n"
            "=======\n"
            "<replacement lines>\n"
            ">>>>>>> REPLACE\n"
            "The SEARCH text must be unique in its file. Several blocks per "
            "file are allowed. To create a new file, use an empty SEARCH. "
            "Keep each SEARCH under 15 lines and emit at most 8 edits total "
            "-- prefer the smallest edits that fix the failures. "
            "Do NOT rewrite whole files. No explanation.".format(
                ", ".join(sorted(current))))
    elif len(current) == 1:
        only = next(iter(current))
        parts.append("Fix the file `{}`. Return ONLY the complete corrected contents "
                     "of `{}` in a single ```{} code block. No explanation.".format(
                         only, only, lang))
    else:
        parts.append("Change any of these files: {}. For each file you change, emit a "
                     "`### FILE: <path>` line followed by its complete new contents in a "
                     "```{} code block. No explanation.".format(
                         ", ".join(sorted(current)), lang))
    if context:
        parts += ["", "Reference files (read-only -- do NOT rewrite them):"]
        for path, content in sorted(context.items()):
            parts += ["", "`{}`:".format(path), "```" + lang, content.strip(), "```"]
    for path, content in sorted(current.items()):
        parts += ["", "Current `{}`:".format(path), "```" + lang, content.strip(), "```"]
    if last_output:
        parts += ["", "The latest test run FAILED. Fix these failures:",
                  "```", last_output.strip(), "```"]
    return "\n".join(parts)


def build_review_prompt(goal, diff):
    return ("TASK: {}\n\nReview this diff. It already passed the spec tests and the "
            "project's own suite; judge correctness against the goal, stub violations "
            "and scope creep. Reply with a verdict line, ACCEPT or REJECT, then short "
            "specific reasons.\n\n```diff\n{}\n```".format(goal, diff))


def _review(chat_fn, model, system, goal, diff, num_ctx, models=None):
    """ADR-003's verifier, advisory in v1 (ADR-016): the verdict is recorded for the
    orchestrator and the human, never enforced -- and a reviewer crash must not cost
    a job that earned green."""
    try:
        options = model_options(models or {}, model)
        options.pop("temperature", None)
        options["num_predict"] = LADDER_NUM_PREDICT
        resp = chat_fn(model, system, build_review_prompt(goal, diff),
                       temperature=0.1, num_ctx=num_ctx, options=options)
        text = resp["content"].strip()
        m = re.search(r"\b(ACCEPT|REJECT)\b", text)
        return {"verdict": m.group(1) if m else "unparseable", "notes": text[:2000]}
    except Exception as e:
        return {"verdict": "error", "notes": "{}: {}".format(type(e).__name__, e)}


def _record_lessons(jobs_root, outcome, store, job_id):
    """Persist what the run taught; never let bookkeeping fail a green job."""
    try:
        found = lessons.from_job_outcome(outcome, time.time())
        if not found:
            return
        ChatStore(Path(jobs_root) / "chats").record_lessons(found)
        store.append_log(job_id, "lessons: {}\n".format(
            ", ".join(sorted(l["pattern"] for l in found))))
    except Exception as e:                                   # noqa: BLE001
        print("lesson recording failed: {}: {}".format(type(e).__name__, e),
              file=sys.stderr)


def _heartbeat_forever(store, job_id, interval, stop):
    while not stop.wait(interval):
        try:
            store.heartbeat(job_id)
        except OSError:
            pass  # a transient sharing violation must not kill the beat


def _run_ladder(store, job_id, worktree, tests_dir, spec, stages, chat_fn, unload_fn,
                system, num_ctx, plateau_k, test_timeout, regression_cmd,
                regression_timeout, runner=pytest_runner, models=None):
    targets = spec["target_files"]
    lang = fence_lang(targets)
    edit_mode = resolve_edit_mode(spec, worktree)
    store.append_log(job_id, "edit mode: {}\n".format(edit_mode))
    if edit_mode == "diff":
        # The 35b "thinking wall" was a context wall: prompt (~10.7k tokens on
        # the crgpd spec) plus generation hit num_ctx 16384 on an architecture
        # Ollama does not context-shift -- 4/4 deaths at ~6.2k eval (audit
        # 2026-07-18). Diff mode implies large current files in the prompt.
        num_ctx = max(num_ctx, 32768)
    goal = spec.get("goal", "")
    context = {}
    for path in spec.get("context_files", []) or []:
        context[path] = (worktree.path / path).read_text(encoding="utf-8")
    if context:
        # A max-budget context (~17-19k tokens) overflows the 16384 default and
        # truncates silently (ADR-008). Every roster model supports 32k.
        num_ctx = max(num_ctx, 32768)
    attempts, last_output, success = [], "", False
    best = None
    latest = {"tests_passed": 0, "tests_failed": 0}
    regression_passed, regression_output, failure_reason = None, "", None
    judge_tampered = []
    struct_hist, structural_abort = [], False  # spans stages: the error is model-invariant

    for si, stage in enumerate(stages):
        if si > 0:
            # Clean slate on escalation (ADR-009): the stronger model does measurably
            # worse when handed the weaker one's broken file.
            worktree.reset()
            last_output = ""
            unload_fn(stages[si - 1]["model"])
        model = stage["model"]
        fail_hist = []
        used, nocode_streak = 0, 0
        # Per rung, not per job: worktree.reset() on escalation puts the files
        # back, so the stronger rung starts from the mode the job asked for.
        mode = edit_mode
        anchored_once = False

        while used < stage.get("attempts", 3):
            current = {}
            for path in targets:
                p = worktree.path / path
                current[path] = p.read_text(encoding="utf-8") if p.exists() else ""
            options = model_options(models or {}, model)
            options.pop("temperature", None)  # stage escalation temp wins
            options["num_predict"] = LADDER_NUM_PREDICT
            first_diff = mode == "diff" and not attempts
            if first_diff:
                options["num_predict"] = FIRST_DIFF_NUM_PREDICT
            temperature = stage.get("temperature", 0.1)
            if mode == "diff":
                # Anchor copying wants fidelity, not the diversity the
                # escalation temps were tuned for (whole-file re-emission).
                temperature = stage.get("diff_temperature", temperature)
            resp = chat_fn(model, system,
                           build_prompt(goal, current, context, last_output, lang,
                                        edit_mode=mode),
                           temperature=temperature,
                           num_ctx=num_ctx, options=options)
            rec = {"attempt": len(attempts) + 1, "stage": si + 1, "model": model,
                   "tokens_per_s": round(resp.get("tokens_per_s", 0.0), 1),
                   "prefill_tokens_per_s": round(resp.get("prefill_tokens_per_s", 0.0), 1),
                   "ttft_s": round(resp.get("ttft_s", 0.0), 2),
                   "done_reason": resp.get("done_reason", ""),
                   "eval_count": resp.get("eval_count", 0),
                   # Without these, a failed attempt cannot be told apart from
                   # a context wall: prompt_count is the window actually filled
                   # (cache included), num_ctx what it had to fit in.
                   "prompt_count": resp.get("prompt_count", 0),
                   "num_ctx": num_ctx,
                   "num_predict": options.get("num_predict"),
                   "edit_mode": mode}
            store.write_attempt(job_id, rec["attempt"], resp.get("content", ""))
            # Only the current attempt's own regression run may populate these; an
            # older attempt's verdict must never leak into a later attempt's result.
            regression_passed, regression_output = None, ""

            try:
                if mode == "diff":
                    tiers = []
                    files = apply_edits(current, extract_edits(resp["content"], targets),
                                        tier_log=tiers)
                    anchored_once = True
                    order = ("exact", "whitespace", "indent", "unicode")
                    rec["anchor_tier"] = max(tiers, key=order.index) if tiers else "exact"
                    if rec["anchor_tier"] != "exact":
                        store.append_log(job_id, "attempt {}: anchors matched at "
                                         "tier {}\n".format(rec["attempt"],
                                                            rec["anchor_tier"]))
                else:
                    files = extract_files(resp["content"], targets)
            except PatchError as e:
                # A codeless reply says nothing about the model's coding ability, so
                # it burns a reminder retry, not an attempt (blitzvolley pilot: the
                # job died on a NoCode while holding its best diff). Two in a row
                # means the model has drifted out of code mode -- leave the rung.
                rec.update({"result": type(e).__name__, "passed": False})
                attempts.append(rec)
                store.append_log(job_id, "attempt {}: {}\n".format(rec["attempt"], e))
                if (mode == "diff" and anchored_once
                        and isinstance(e, (AnchorNotFound, AmbiguousAnchor))):
                    # Root cause isolated 2026-07-25 (backlog line 24): once a
                    # rung has landed edits, the model can no longer anchor in
                    # the file it just changed -- 7b, 30b and 35b all break the
                    # same way, and the "closest region" feedback does not help
                    # it. Stop asking for anchors; let it re-emit the file.
                    # No strike either: failing to re-anchor after a successful
                    # edit is the harness's problem, not the rung's.
                    mode = "whole"
                    rec["fell_back_to_whole"] = True
                    store.append_log(job_id, "attempt {}: anchor lost after a "
                                     "landed edit -- rung falls back to whole "
                                     "files\n".format(rec["attempt"]))
                    last_output = (
                        "Your previous edit WAS applied, so the file has "
                        "changed and your SEARCH text no longer matches it. "
                        "Reply with the COMPLETE file, not a diff.")
                    failure_reason = "anchor_lost"
                    continue
                if resp.get("done_reason") == "length":
                    # Truncated mid-deliberation: the model never reached the code,
                    # so "follow the format" would be the wrong feedback.
                    reminder = ("Your previous reply was cut off by the length "
                                "limit before it contained a complete code block "
                                "-- answer again, output the code block first and "
                                "keep any reasoning brief.")
                else:
                    reminder = ("Your previous reply was rejected: {} -- reply again, "
                                "following the required format exactly.".format(e))
                last_output = (last_output + "\n\n" + reminder) if last_output else reminder
                failure_reason = "no_code"
                if not first_diff:
                    # The capped first attempt is sacrificial by design; its
                    # codeless failure must not burn one of the rung's strikes.
                    nocode_streak += 1
                if nocode_streak >= 2:
                    store.append_log(job_id, "two codeless replies running -- "
                                     "leaving the rung\n")
                    break
                continue
            nocode_streak = 0
            used += 1

            for path, content in files.items():
                dst = worktree.path / path
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(content, encoding="utf-8")

            result = runner.run_tests(worktree.path, tests_dir, timeout=test_timeout)
            if _judge_never_ran(result):
                raise JudgeCommandError(
                    "the judge exited {} without running a test; this is the "
                    "runner, not the model\n{}".format(
                        result.returncode, result.output[-1000:]))
            # WHICH tests fail is the routing evidence (bottlenecks doc, item 1):
            # identical names across rungs = escalate; shrinking set = progress.
            all_failing = (getattr(runner, "failed_names", lambda _: [])(result.output)
                           if result.tests_failed else [])
            # The log and the plateau detector want a stable head; the model's
            # feedback wants every name (see feedback.condense).
            failing = all_failing[:5]
            latest = {"tests_passed": result.tests_passed, "tests_failed": result.tests_failed,
                      "failing": failing}
            if result.returncode not in (0, 1):
                # Exit 2/3/etc (collection or internal error) parses as 0/0, which is
                # indistinguishable from "ran nothing" -- log it so it is not silent.
                store.append_log(job_id, "attempt {}: spec tests exited abnormally "
                                 "({})\n{}\n".format(rec["attempt"], result.returncode,
                                                      result.output[-500:]))

            # Only pay for the project's suite once the spec is satisfied: a red spec
            # has already told the model what to fix.
            regression = None
            if result.passed and regression_cmd:
                judge_report = worktree.restore_judge()
                if judge_report["restored"] or judge_report["deleted"]:
                    # Touching a judge file is disqualifying, not something to retry:
                    # the tree is restored, but the intent poisons the job (ADR-015).
                    store.append_log(job_id, "attempt {}: restore_judge restored={} "
                                     "deleted={}\n".format(rec["attempt"],
                                                            judge_report["restored"],
                                                            judge_report["deleted"]))
                    judge_tampered = sorted(set(judge_report["restored"])
                                            | set(judge_report["deleted"]))
                    failure_reason = "judge_tampered"
                    rec.update({"passed": False, "timed_out": result.timed_out,
                                "returncode": result.returncode,
                                "regression_passed": None, **latest})
                    attempts.append(rec)
                    break
                regression = runner.run_regression(worktree.path, regression_cmd,
                                            timeout=regression_timeout)
                if regression.returncode in runner.OPERATOR_ERROR_CODES:
                    raise RegressionCommandError(
                        "{} exited {}; check regression_cmd in factory.toml\n{}".format(
                            regression_cmd, regression.returncode, regression.output[-1000:]))
                regression_passed = regression.passed
                regression_output = regression.output[-3000:]

            if result.returncode in (0, 1) and \
                    (best is None or result.tests_passed > best["tests_passed"]) and \
                    not forbidden_paths(worktree.changed_paths()):
                # Captured at attempt time: the escalation reset() can no
                # longer throw away a 6/8 (bottlenecks doc, item 3).
                best = {"attempt": rec["attempt"], "stage": si + 1, "model": model,
                        "tests_passed": result.tests_passed,
                        "tests_failed": result.tests_failed, "failing": failing,
                        "diff": worktree.capture_diff()}

            green = result.passed and (regression is None or regression.passed)
            rec.update({"passed": green, "timed_out": result.timed_out,
                        "returncode": result.returncode,
                        "regression_passed": None if regression is None else regression.passed,
                        **latest})
            attempts.append(rec)
            store.update_state(job_id, stage=si + 1, attempt=rec["attempt"], model=model,
                               regression_passed=rec["regression_passed"], **latest)
            store.append_log(job_id, "attempt {} [{}]: {} passed, {} failed{}, regression={}\n".format(
                rec["attempt"], model, result.tests_passed, result.tests_failed,
                " ({})".format(", ".join(failing)) if failing else "",
                rec["regression_passed"]))

            if green:
                success = True
                failure_reason = None
                break
            if result.passed:
                # The spec is green and the project is not: that is the signal to fix.
                # tests_failed is 0 here (the spec passed) -- feeding that into
                # fail_hist would plateau the stage on regression progress alone.
                last_output = feedback.condense(
                    regression.output,
                    getattr(runner, "failed_names", lambda _: [])(regression.output),
                    FEEDBACK_CHARS)
                failure_reason = "regression"
            else:
                # Chronological truncation showed 1 of 9 failures on j_3a6a7681,
                # never the causal one, and every model stalled on it (banc A).
                last_output = feedback.condense(result.output, all_failing, FEEDBACK_CHARS)
                failure_reason = "spec_tests"
                if result.returncode in (0, 1):
                    # Only a genuine spec-test failure counts toward the plateau; an
                    # abnormal exit (logged above) is not a signal the model is stuck.
                    fail_hist.append(result.tests_failed)
                    struct_hist = []  # a real test run breaks the structural streak
                else:
                    struct_hist.append(failure_signature(result.returncode,
                                                         result.output))
                    if len(struct_hist) >= STRUCTURAL_ABORT_K and \
                            len(set(struct_hist[-STRUCTURAL_ABORT_K:])) == 1:
                        rec["structural_abort"] = True
                        failure_reason = "structural"
                        structural_abort = True
                        store.append_log(job_id, "structural failure: identical "
                                         "abnormal exit {} times running -- aborting "
                                         "instead of escalating\n".format(
                                             STRUCTURAL_ABORT_K))
                        break
            if len(fail_hist) >= plateau_k and len(set(fail_hist[-plateau_k:])) == 1:
                rec["plateau"] = True
                break
        if success or judge_tampered or structural_abort:
            break

    return {"success": success, "attempts": attempts, "edit_mode": edit_mode,
            "best_attempt": best,
            "stages_used": si + 1, "escalated": si > 0,
            "final_model": attempts[-1]["model"] if attempts else None,
            "regression_passed": regression_passed, "regression_output": regression_output,
            "failure_reason": failure_reason, "judge_tampered": judge_tampered,
            "structural_abort": structural_abort, **latest}


def _execute(store, job_id, jobs_root, config_path, chat_fn, unload_fn, ladder, system,
             gpu_lock_path, gpu_timeout, num_ctx, plateau_k, test_timeout,
             regression_timeout=REGRESSION_TIMEOUT, reviewer_system=None):
    spec = store.read_spec(job_id)
    project = resolve_project(load_projects(config_path), spec["project"])
    models = load_models(config_path)
    runner = RUNNERS[project.runner]
    validate_tests(spec["tests"], project.runner)
    validate_targets(spec["target_files"])
    validate_context_files(project, spec.get("context_files"), spec["target_files"])

    # delegate() already checked all of the above; re-checking here means a
    # hand-written spec.json cannot smuggle a target past the runner either.
    store.update_state(job_id, status="waiting_gpu", pid=os.getpid())
    lock = GpuLock(gpu_lock_path or Path(jobs_root) / ".gpu.lock")
    if not lock.acquire(timeout=gpu_timeout, poll=2.0, on_wait=lambda: store.heartbeat(job_id)):
        return {"status": "failed", "error": "gpu_timeout", "failure_reason": "gpu_timeout",
                "detail": "another job held the GPU for {}s".format(gpu_timeout)}

    worktree = None
    try:
        store.update_state(job_id, status="running")
        job_dir = store.job_dir(job_id)
        # Replays (regression suite) pin the archived commit; live jobs use HEAD.
        worktree = WorkTree.create(project.path, job_id, job_dir / "worktree",
                                   base_sha=spec.get("base_sha"))
        tests_dir = runner.materialize_tests(job_dir / "tests", spec["tests"],
                                             worktree.path,
                                             project_path=project.path)

        stages = ladder or DEFAULT_LADDER
        start = spec.get("start_stage")
        if start:
            stages = stages[start - 1:] or stages[-1:]

        outcome = _run_ladder(store, job_id, worktree, tests_dir, spec,
                              stages, chat_fn, unload_fn, system,
                              num_ctx, plateau_k, test_timeout, project.regression_cmd,
                              regression_timeout, runner=runner, models=models)

        diff = worktree.capture_diff()
        forbidden = forbidden_paths(worktree.changed_paths())
        if forbidden or outcome["judge_tampered"]:
            status = "rejected"
        else:
            status = "succeeded" if outcome["success"] else "failed"
        review = None
        if status == "succeeded" and diff:
            # The final model is still resident: reviewing with it costs no swap.
            review = _review(chat_fn, outcome["final_model"], reviewer_system,
                             spec.get("goal", ""), diff, num_ctx, models=models)
            store.append_log(job_id, "review [{}]: {}\n".format(
                outcome["final_model"], review["verdict"]))
        # What this run taught, into the same store the agent reads at session
        # start. Without this call the lesson source is dead code, which is
        # exactly how `lessons.incident()` spent months (audit 2026-07-25).
        _record_lessons(jobs_root, outcome, store, job_id)
        return dict(outcome, status=status, diff=diff, base_sha=worktree.base_sha,
                    branch=worktree.branch, forbidden_paths=forbidden,
                    project=project.name, review=review)
    finally:
        lock.release()
        if worktree is not None:
            # Detach node_modules links (vitest) BEFORE tearing the tree down:
            # nothing recursive may ever run with the real node_modules linked in.
            runner.cleanup(worktree.path, store.job_dir(job_id) / "tests")
            worktree.remove()


def run_job(job_id, jobs_root, config_path, chat_fn=None, unload_fn=None, ladder=None,
            system=None, gpu_lock_path=None, gpu_timeout=DEFAULT_GPU_TIMEOUT,
            heartbeat_interval=HEARTBEAT_INTERVAL, num_ctx=16384, plateau_k=2,
            test_timeout=600, regression_timeout=REGRESSION_TIMEOUT,
            reviewer_system=None):
    store = JobStore(jobs_root)
    manager = None
    system = system or _system_prompt()
    reviewer_system = reviewer_system or _system_prompt("reviewer")

    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat_forever,
                            args=(store, job_id, heartbeat_interval, stop), daemon=True)
    beat.start()
    t0 = time.time()
    try:
        if chat_fn is None:
            # Zero-Ollama (2026-07-18): the ladder requires the llama-server
            # runtime; injected chat_fns (tests, rigs) bypass it entirely.
            ls_cfg = load_llama_server(config_path)
            if ls_cfg is None:
                raise ConfigError("factory.toml has no [llama_server] section; "
                                  "the Ollama runtime was retired")
            manager = LlamaServerManager(ls_cfg)
            chat_fn = manager.chat
            unload_fn = unload_fn or manager.unload
        if unload_fn is None:
            unload_fn = lambda model: None  # injected chat_fn without an unloader
        result = _execute(store, job_id, jobs_root, config_path, chat_fn, unload_fn,
                          ladder, system, gpu_lock_path, gpu_timeout, num_ctx,
                          plateau_k, test_timeout, regression_timeout, reviewer_system)
    except Exception as e:  # a job must always leave a verdict behind
        result = {"status": "error", "error": "{}: {}".format(type(e).__name__, e)}
    finally:
        stop.set()
        beat.join(timeout=2)
        if manager is not None:
            # A runner that exits without this leaks a 20 GB server: Windows
            # children survive their parent.
            manager.shutdown()

    result["total_seconds"] = round(time.time() - t0, 1)
    store.write_result(job_id, result)
    store.update_state(job_id, status=result["status"])
    return result


def _cli():
    ap = argparse.ArgumentParser(description="Run one delegated job to completion.")
    ap.add_argument("job_id")
    ap.add_argument("--jobs", default=str(ROOT / "jobs"))
    ap.add_argument("--config", default=str(ROOT / "factory.toml"))
    a = ap.parse_args()
    result = run_job(a.job_id, a.jobs, a.config)
    shown = {k: v for k, v in result.items() if k != "diff"}
    if shown.get("best_attempt"):
        shown["best_attempt"] = {k: v for k, v in shown["best_attempt"].items()
                                 if k != "diff"}
    seek_stdout_to_end()
    print(json.dumps(shown, indent=2))
    sys.exit(0 if result["status"] == "succeeded" else 1)


if __name__ == "__main__":
    _cli()

"""Banc A -- codeur : taxe de harnais (horizontal) x ecart modele (vertical).

BRIDE = the lane jobs ladder as it ships, one rung at a time: no repo access,
the model never runs the tests, the prompt is rebuilt from scratch on every
attempt, 3 attempts, 32k ctx. Same corpus, same harness, same judge for every
model -- so a cell that fails tells you which of the two axes to blame.

The corpus is the frozen replay corpus (`regression/corpus.toml`): 10 jobs with
a known-green archived run plus 2 probes that were historically red. Ground
truth comes from `expected`, not from a fresh judgment.

    py -3.9 experiments/coder_bench/run_bench.py --models qwen3-coder:30b
    py -3.9 experiments/coder_bench/run_bench.py            # the 3 local rungs
    py -3.9 experiments/coder_bench/run_bench.py --models claude-haiku-4-5
    py -3.9 experiments/coder_bench/run_bench.py --models anthropic/claude-haiku-4-5

Long runs go detached (Start-Process + a log file), never through a task
runner -- see CLAUDE.md. Rows are appended to rows.jsonl as they land: a run
that dies at hour two must not cost the hour that worked.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

import cloud_client                                                # noqa: E402
from factory_config import load_llama_server                       # noqa: E402
from job_store import JobStore                                     # noqa: E402
from llama_server_manager import LlamaServerManager                # noqa: E402
import loop_job                                                    # noqa: E402
import regress                                                     # noqa: E402

HERE = Path(__file__).resolve().parent
LOCAL = ["qwen2.5-coder:7b", "qwen3-coder:30b", "qwen3.6:35b"]
ATTEMPTS = 3          # uniform across rungs: the budget must not be the variable
CORPUS = ROOT / "regression" / "corpus.toml"
CONFIG = ROOT / "factory.toml"
JOBS = ROOT / "jobs"


def stage_for(model, attempts=ATTEMPTS):
    """Production temperature for a known rung, ladder defaults otherwise."""
    for s in loop_job.DEFAULT_LADDER:
        if s["model"] == model:
            return dict(s, attempts=attempts)
    return {"model": model, "temperature": 0.1, "attempts": attempts}


def gpu_lock_for(model, run_dir):
    """Which lock a cell must hold: the production one only if it uses the GPU.

    `jobs/.gpu.lock` is what queues a local rung behind a real job -- one GPU,
    one job. A cloud cell has no business there: taking it makes the bench wait
    for the local lane, and the local lane wait for a network call that never
    touches the card. Pointing those cells at a per-run file keeps the code path
    single (loop_job always locks something) while contending with nobody.
    """
    if cloud_client.is_cloud(model):
        return Path(run_dir) / ".gpu.lock"
    return JOBS / ".gpu.lock"


def load_cases(tier):
    _, entries = regress.load_corpus(CORPUS)
    checked = regress.check(CORPUS, JOBS, CONFIG)
    cases = []
    for job_id in checked["ok"]:
        entry = entries[job_id]
        if tier == "smoke" and entry["tier"] != "smoke":
            continue
        archived = json.loads((JOBS / job_id / "result.json").read_text(encoding="utf-8"))
        spec = json.loads((JOBS / job_id / "spec.json").read_text(encoding="utf-8"))
        spec["base_sha"] = archived["base_sha"]
        # start_stage would slice a one-rung ladder back onto itself: the point
        # of the bench is that every model gets the same untouched spec.
        spec.pop("start_stage", None)
        cases.append({"id": job_id, "expected": entry["expected"], "spec": spec,
                      "archived": regress.baseline_from_result(archived)})
    return cases, checked["excluded"]


def slug(model):
    return model.replace(":", "_").replace(".", "").replace("/", "_")


def summarize(rows):
    out = {}
    for model in dict.fromkeys(r["model"] for r in rows):
        cell = [r for r in rows if r["model"] == model]
        green = [r for r in cell if r["expected"] == "green"]
        probes = [r for r in cell if r["expected"] == "probe"]
        solved = [r for r in green if r["success"]]
        # `no_code` is a protocol failure, not a coding failure: it is the
        # number the whole harness-tax question turns on.
        reasons = {}
        for r in cell:
            if not r["success"]:
                reasons[r["failure_reason"] or "unknown"] = \
                    reasons.get(r["failure_reason"] or "unknown", 0) + 1
        out[model] = {
            "green_n": len(green), "green_solved": len(solved),
            "solve_rate": round(len(solved) / len(green), 3) if green else None,
            "probe_n": len(probes), "probe_solved": sum(1 for r in probes if r["success"]),
            "median_attempts": sorted(r["attempts"] for r in solved)[len(solved) // 2]
                               if solved else None,
            "total_seconds": round(sum(r["seconds"] for r in cell)),
            "total_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in cell), 4),
            "failure_reasons": reasons,
            "errors": sum(1 for r in cell if r["status"] == "error"),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=LOCAL)
    ap.add_argument("--tier", default="full", choices=["smoke", "full"])
    ap.add_argument("--only", nargs="*", default=None, help="job ids")
    ap.add_argument("--out", default=None)
    # The brakes, as knobs: comparing a local rung to a cloud one means being
    # able to run it under the cloud rung's conditions, not just the ladder's.
    ap.add_argument("--attempts", type=int, default=ATTEMPTS)
    ap.add_argument("--plateau-k", type=int, default=2,
                    help="identical failures before leaving the rung; 0 disables")
    ap.add_argument("--first-diff-cap", type=int, default=None,
                    help="num_predict for diff attempt 1 (default: the ladder's)")
    ap.add_argument("--num-predict", type=int, default=None,
                    help="per-attempt generation cap (default: the ladder's 16384)")
    ap.add_argument("--feedback-chars", type=int, default=None,
                    help="failure-feedback budget (default: the harness's 3000)")
    ap.add_argument("--provider", default=None,
                    help="OpenRouter provider pin (default: use cloud_client's)")
    a = ap.parse_args()

    if a.provider is not None:
        # Free-model smoke run uses a different provider than the measurement run,
        # which pins to the constant below. A non-None flag overrides it for this run.
        cloud_client.OPENROUTER_PROVIDER = a.provider

    # A missing key must cost nothing, not eleven cells of a twelve-cell run.
    cloud_client.preflight(a.models)

    if a.feedback_chars:
        # Breadth costs depth at a fixed budget: naming 9 failures leaves less
        # evidence per failure, which the 7b needs and the 35b does not. The
        # budget is the knob that buys both.
        loop_job.FEEDBACK_CHARS = a.feedback_chars

    if a.num_predict:
        # A thinking model's median is ~6.5k tokens on a module-scale contract;
        # the cap is a ceiling on deliberation, so pricing it means lifting it.
        loop_job.LADDER_NUM_PREDICT = a.num_predict
    if a.first_diff_cap:
        # Deliberate: the cap is a measured anti-over-thinking device, so the
        # only honest way to price it is to run the same corpus without it.
        loop_job.FIRST_DIFF_NUM_PREDICT = a.first_diff_cap

    cases, excluded = load_cases(a.tier)
    if a.only:
        cases = [c for c in cases if c["id"] in a.only]
    print("corpus: {} cases, {} excluded".format(len(cases), len(excluded)), flush=True)

    # Absolute: the runner materializes the judge files under run_dir and then
    # runs pytest from inside the worktree -- a relative path dies there
    # ("--confcutdir must be a directory"), before the model is ever called.
    run_dir = Path(a.out).resolve() if a.out else HERE / "runs" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    rows_path = run_dir / "rows.jsonl"

    manager = None
    if any(not cloud_client.is_cloud(m) for m in a.models):
        manager = LlamaServerManager(load_llama_server(CONFIG))

    def chat_fn(model, system, user, **kw):
        if cloud_client.is_cloud(model):
            return cloud_client.chat(model, system, user, **kw)
        return manager.chat(model, system, user, **kw)

    store = JobStore(run_dir)
    rows, started = [], time.time()
    try:
        for model in a.models:
            for case in cases:
                job_id = "{}__{}".format(case["id"], slug(model))
                store.create(case["spec"], job_id=job_id)
                t0 = time.time()
                spend_before = cloud_client.ledger_snapshot()
                result = loop_job.run_job(
                    job_id, run_dir, CONFIG, chat_fn=chat_fn,
                    unload_fn=lambda m: None,   # one rung: nothing to unload
                    ladder=[stage_for(model, a.attempts)],
                    plateau_k=a.plateau_k or 10 ** 6,
                    gpu_lock_path=gpu_lock_for(model, run_dir))
                # loop_job builds its attempt records from a fixed whitelist,
                # so cost cannot ride back on the result: diff the ledger
                # instead. On a local model every field here is zero, which
                # keeps the rows homogeneous.
                spend = cloud_client.ledger_delta(spend_before)
                row = {
                    "model": model, "case": case["id"], "expected": case["expected"],
                    "status": result.get("status"),
                    "success": bool(result.get("success")),
                    "attempts": len(result.get("attempts") or []),
                    "failure_reason": result.get("failure_reason"),
                    "regression_passed": result.get("regression_passed"),
                    "diff_bytes": len(result.get("diff") or ""),
                    "seconds": round(time.time() - t0, 1),
                    "error": result.get("error", ""),
                    "archived": case["archived"],
                    "cost_usd": spend["cost_usd"],
                    "prompt_tokens": spend["prompt_tokens"],
                    "completion_tokens": spend["completion_tokens"],
                    # More than one name with allow_fallbacks off is the
                    # evidence the pin did not hold -- keep the whole list.
                    "provider": ",".join(spend["providers"]),
                    # What the response itself claims answered, not what was
                    # requested (`model` above) or who served it (`provider`).
                    # Same multi-value join: more than one name here is the
                    # evidence the served variant was not stable either.
                    "response_model": ",".join(spend["models"]),
                }
                rows.append(row)
                with rows_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
                print("{:20} {:14} {:6} -> {:9} {} att {}s ${}".format(
                    model, case["id"], case["expected"], row["status"],
                    row["attempts"], row["seconds"], row["cost_usd"]), flush=True)
    finally:
        if manager is not None:
            manager.shutdown()
        # Cross-check: the ledger's running total is the truth of what was
        # spent; the per-model totals below are a sum of per-cell deltas. Any
        # path that drops a delta (a bug, not a network failure -- those stay
        # scoped to their cell) makes the sum quietly low with nothing to
        # flag it, unless a reader can compare it against this total.
        ledger = cloud_client.ledger_snapshot()
        summary = {"summary": summarize(rows), "rows": rows,
                   "excluded": excluded, "models": a.models,
                   "provider": a.provider or cloud_client.OPENROUTER_PROVIDER,
                   "total_seconds": round(time.time() - started),
                   "ledger_total_cost_usd": round(ledger["cost_usd"], 4),
                   "ledger_calls": ledger["calls"]}
        (run_dir / "report.json").write_text(json.dumps(summary, indent=2),
                                             encoding="utf-8")

    print("\n=== summary ===")
    print(json.dumps(summary["summary"], indent=2))
    errored = [r["case"] for r in rows if r.get("status") == "error"]
    if errored:
        # An infrastructure error counts in the denominator exactly like a model
        # failure, and reads like one. On 27/07 a saturated free endpoint (502)
        # would have scored the cloud column 8/10 instead of 9/10 -- a tie with
        # the local 30b instead of a lead. So: replay, then read.
        print("\n{} cell(s) in error -- infrastructure, not the model. Replay "
              "them before reading any rate:".format(len(errored)))
        print("  py -3.9 experiments/coder_bench/run_bench.py --models {} "
              "--only {} --out {}-rerun".format(
                  " ".join(a.models), " ".join(errored), run_dir))
    print("\nwritten: {}".format(run_dir / "report.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

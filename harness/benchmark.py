"""Benchmark harness: run (tasks x models x N) and aggregate the metrics.

Because model output is nondeterministic, each config runs N times and we report
success rate + mean attempts + mean time. Results are written to
experiments/results/ as JSON and a markdown table you can commit as history.

Usage:
    py -3 harness/benchmark.py --models qwen2.5-coder:7b qwen2.5-coder:14b \
        --tasks roman calculator --runs 3
"""
import argparse
import json
import statistics
import time

import loop  # same dir; script dir is on sys.path[0]

ROOT = loop.ROOT


def summarize(model, task, runs):
    total_attempts = sum(x["attempts_used"] for x in runs)
    success_count = sum(1 for x in runs if x["success"])
    mean_attempts = round(total_attempts / len(runs), 2)
    mean_seconds = round(statistics.mean(x["total_seconds"] for x in runs), 1)

    prefill_tps_sum = 0
    ttft_s_sum = 0
    valid_attempts = 0

    for run in runs:
        for attempt in run["attempts"]:
            if "prefill_tokens_per_s" in attempt and "ttft_s" in attempt:
                prefill_tps_sum += attempt["prefill_tokens_per_s"]
                ttft_s_sum += attempt["ttft_s"]
                valid_attempts += 1

    mean_prefill_tps = round(prefill_tps_sum / valid_attempts, 1) if valid_attempts > 0 else 0.0
    mean_ttft_s = round(ttft_s_sum / valid_attempts, 2) if valid_attempts > 0 else 0.0

    return {
        "model": model,
        "task": task,
        "runs": len(runs),
        "success_rate": round(success_count / len(runs), 2),
        "mean_attempts": mean_attempts,
        "mean_seconds": mean_seconds,
        "mean_prefill_tps": mean_prefill_tps,
        "mean_ttft_s": mean_ttft_s
    }


def to_markdown(rows):
    lines = [
        "| model | task | runs | success | mean attempts | mean sec | prefill tok/s | ttft s |",
        "|-------|------|------|---------|---------------|----------|-------------|--------|"
    ]
    for x in rows:
        lines.append(f"| {x['model']} | {x['task']} | {x['runs']} | "
                     f"{x['success_rate']} | {x['mean_attempts']} | {x['mean_seconds']} | "
                     f"{x['mean_prefill_tps']} | {x['mean_ttft_s']} |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", nargs="+", default=["roman", "calculator"])
    ap.add_argument("--models", nargs="+", default=["qwen2.5-coder:7b"])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--persona", default="coder")
    ap.add_argument("--temp", type=float, default=0.1)
    ap.add_argument("--num-ctx", type=int, default=16384)
    a = ap.parse_args()

    rows = []
    raw = []
    for model in a.models:
        for task in a.tasks:
            runs = []
            for r in range(a.runs):
                res = loop.run_task(task, model, a.persona, a.temp, a.num_ctx)
                runs.append(res)
                print(f"[{model} {task} {r + 1}/{a.runs}] "
                      f"success={res['success']} attempts={res['attempts_used']} "
                      f"{res['total_seconds']}s")
            raw.extend(runs)
            rows.append(summarize(model, task, runs))

    ts = time.strftime("%Y%m%d-%H%M%S")
    results_dir = ROOT / "experiments" / "results"
    (results_dir / f"{ts}_benchmark.json").write_text(
        json.dumps({"summary": rows, "raw": raw}, indent=2), encoding="utf-8")

    md = to_markdown(rows)
    (results_dir / f"{ts}_benchmark.md").write_text(md, encoding="utf-8")

    print("\n" + md)
    print(f"saved: experiments/results/{ts}_benchmark.(json|md)")


if __name__ == "__main__":
    main()

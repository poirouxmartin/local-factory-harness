"""Run the reviewer bench: precision on green diffs, recall on mutants.

Measures the reviewer that actually ships. `build_review_prompt` and the
reviewer persona are imported from the harness rather than restated here, so a
prompt change moves the bench with it instead of silently invalidating it.

Every reviewer sees every case. Self-review vs cross-review is then a partition
of those results (reviewer == the model that wrote the diff, or not), which
costs no extra GPU: the interesting comparison is between two slices of the
same run, not between two runs.

    py -3.9 experiments/reviewer_bench/run_bench.py --models qwen3.6:35b
    py -3.9 experiments/reviewer_bench/run_bench.py            # all three rungs

Long runs go detached (Start-Process + a log file), never through a task
runner -- see CLAUDE.md.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from factory_config import load_llama_server, load_models, model_options  # noqa: E402
from llama_server_manager import LlamaServerManager                      # noqa: E402
import loop_job                                                          # noqa: E402

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
RESULTS = HERE / "results"

LADDER = ["qwen2.5-coder:7b", "qwen3-coder:30b", "qwen3.6:35b"]
NUM_CTX = 32768


def load_cases():
    green = json.loads((CORPUS / "green.json").read_text(encoding="utf-8"))
    mutants = json.loads((CORPUS / "mutants.json").read_text(encoding="utf-8"))
    return green + mutants


def spec_tests(case_id):
    """The job's judge files, for the with_tests condition. Mutants keep the id
    of the job they were derived from, before the `~`."""
    job = case_id.split("~")[0]
    d = ROOT / "jobs" / job / "tests"
    if not d.is_dir():
        return ""
    parts = []
    for p in sorted(d.glob("test_*.py")) + sorted(d.glob("*.test.js")):
        parts.append("--- {} ---\n{}".format(p.name, p.read_text(encoding="utf-8")))
    return "\n\n".join(parts)


def build_prompt(case, mode):
    base = loop_job.build_review_prompt(case["goal"], case["diff"])
    if mode == "diff_only":
        return base
    tests = spec_tests(case["id"])
    if not tests:
        return base
    return base + ("\n\nThe spec tests this diff is meant to satisfy, for "
                   "reference (you cannot run them):\n\n" + tests)


def review(manager, model, system, prompt, models_cfg):
    options = model_options(models_cfg, model)
    options.pop("temperature", None)
    options["num_predict"] = loop_job.LADDER_NUM_PREDICT
    resp = manager.chat(model, system, prompt, temperature=0.1,
                        num_ctx=NUM_CTX, options=options)
    text = resp["content"].strip()
    import re
    m = re.search(r"\b(ACCEPT|REJECT)\b", text)
    return (m.group(1) if m else "unparseable"), text


def score(rows):
    """Precision on the green slice, recall on the mutant slice.

    A verdict the parser could not read counts as a miss on both sides: an
    unreadable review is worth exactly as much as no review.
    """
    green = [r for r in rows if r["truth"] == "ACCEPT"]
    mut = [r for r in rows if r["truth"] == "REJECT"]
    fp = sum(1 for r in green if r["verdict"] != "ACCEPT")
    tp = sum(1 for r in mut if r["verdict"] == "REJECT")
    out = {
        "green_n": len(green), "false_positives": fp,
        "precision_on_green": round(1 - fp / len(green), 3) if green else None,
        "mutant_n": len(mut), "caught": tp,
        "recall_on_mutants": round(tp / len(mut), 3) if mut else None,
        "unparseable": sum(1 for r in rows if r["verdict"] == "unparseable"),
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=LADDER)
    ap.add_argument("--modes", nargs="*", default=["diff_only", "with_tests"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=0, help="smoke: first N cases")
    a = ap.parse_args()

    cases = load_cases()
    if a.limit:
        # Keep both truths in a smoke run: a slice of greens alone would report
        # a recall of None and hide a broken parser.
        green = [c for c in cases if c["truth"] == "ACCEPT"][:a.limit]
        mut = [c for c in cases if c["truth"] == "REJECT"][:a.limit]
        cases = green + mut
    cfg = load_llama_server(ROOT / "factory.toml")
    models_cfg = load_models(ROOT / "factory.toml")
    system = loop_job._system_prompt("reviewer")
    manager = LlamaServerManager(cfg)

    rows = []
    started = time.time()
    try:
        for model in a.models:
            for mode in a.modes:
                for case in cases:
                    t0 = time.time()
                    try:
                        verdict, text = review(manager, model, system,
                                               build_prompt(case, mode), models_cfg)
                        err = ""
                    except Exception as e:      # one dead call must not lose the run
                        verdict, text, err = "error", "", "{}: {}".format(
                            type(e).__name__, e)
                    rows.append({
                        "reviewer": model, "mode": mode, "case": case["id"],
                        "truth": case["truth"], "verdict": verdict,
                        "coder_model": case["coder_model"],
                        "self_review": case["coder_model"] == model,
                        "defect": case.get("defect"),
                        "seconds": round(time.time() - t0, 1),
                        "notes": text[:1200], "error": err,
                    })
                    print("{:22} {:10} {:34} truth={:6} -> {}".format(
                        model, mode, case["id"][:34], case["truth"], verdict),
                        flush=True)
        manager.shutdown()
    except KeyboardInterrupt:
        manager.shutdown()

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(a.out) if a.out else RESULTS / "bench_{}.json".format(stamp)

    summary = {}
    for model in a.models:
        for mode in a.modes:
            cell = [r for r in rows if r["reviewer"] == model and r["mode"] == mode]
            if not cell:
                continue
            summary["{} / {}".format(model, mode)] = {
                "all": score(cell),
                "self_review": score([r for r in cell if r["self_review"]]),
                "cross_review": score([r for r in cell if not r["self_review"]]),
            }

    out.write_text(json.dumps({"summary": summary, "rows": rows,
                               "total_seconds": round(time.time() - started)},
                              indent=2), encoding="utf-8")
    print("\n=== summary ===")
    print(json.dumps(summary, indent=2))
    print("\nwritten: {}".format(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Why a bench cell failed -- the cause, not the verdict.

`status: failed` is where the diagnosis usually stops, and this session cost
two wrong verdicts to that habit: the 35b was called weaker than the 30b when
it was being truncated before its `### FILE:` headers, and j_3a6a7681 was
called a model wall when the harness was hiding 8 of its 9 failures.

So every failed attempt is classified against the three budgets it could have
died on, in the order they bind:

  OUTPUT WALL     done_reason == length -- the reply was cut mid-sentence.
                  Never a coding failure; a budget one.
  CONTEXT WALL    prompt + generation reached num_ctx. Truncation with nowhere
                  left to write is a different fix from a low cap.
  FORMAT          the reply carried no applicable edit (NoCode). Real, but
                  only once the two above are excluded.
  SPEC            tests ran and disagreed. The only class that is about code.

    py -3.9 experiments/coder_bench/autopsy.py runs/bancB-corrige
    py -3.9 experiments/coder_bench/autopsy.py runs/bancB-corrige --model qwen3.6:35b
"""
import argparse
import json
from pathlib import Path

# Under this share of the window, a `length` stop is the cap alone; above it,
# the cap and the window are indistinguishable without a rerun.
CONTEXT_TIGHT = 0.85


def cells(run_dir, model=None):
    for path in sorted(Path(run_dir).glob("j_*/result.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        case, _, slug = path.parent.name.partition("__")
        if model and model.replace(":", "_").replace(".", "") != slug:
            continue
        yield case, slug, result


def classify(attempt, log_text):
    """The binding constraint for one attempt, most mechanical first."""
    ctx = attempt.get("num_ctx") or 0
    used = (attempt.get("prompt_count") or 0) + (attempt.get("eval_count") or 0)
    share = (used / ctx) if ctx else 0.0
    if attempt.get("done_reason") == "length":
        if share >= CONTEXT_TIGHT:
            return "CONTEXT WALL", "{}+{} tok = {:.0%} of num_ctx {}".format(
                attempt.get("prompt_count"), attempt.get("eval_count"), share, ctx)
        return "OUTPUT WALL", "cut at {} tok (cap {}), window only {:.0%} full".format(
            attempt.get("eval_count"), attempt.get("num_predict") or "?", share)
    if attempt.get("result") in ("NoCode", "PatchError"):
        return "FORMAT", attempt.get("result")
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--model", default=None)
    a = ap.parse_args()

    verdicts = {}
    for case, slug, result in cells(a.run_dir, a.model):
        if result.get("success"):
            continue
        log = (Path(a.run_dir) / "{}__{}".format(case, slug) / "log.txt")
        log_text = log.read_text(encoding="utf-8") if log.exists() else ""
        causes = []
        for att in result.get("attempts") or []:
            kind, detail = classify(att, log_text)
            if kind:
                causes.append("a{} {}: {}".format(att["attempt"], kind, detail))
        # A cell whose every attempt ran to `stop` and still failed the tests is
        # the only one the model itself owns.
        headline = ("SPEC (no budget hit)" if not causes
                    else causes[0].split(":")[0].split(" ", 1)[1])
        verdicts.setdefault(slug, []).append((case, headline, causes))

    for slug in sorted(verdicts):
        print("\n=== {} ===".format(slug))
        for case, headline, causes in verdicts[slug]:
            print("  {:12} {}".format(case, headline))
            for line in causes:
                print("      {}".format(line))
    if not verdicts:
        print("no failed cell in {}".format(a.run_dir))


if __name__ == "__main__":
    main()

"""Cell-by-cell diff of two bench runs.

A summary table hides the thing that matters after a harness change: WHICH
cells moved, and in which direction. A rung can gain two cases and lose two
others and look unchanged -- that is not "no effect", that is a change of
behaviour with a net of zero, and it needs looking at.

    py -3.9 experiments/coder_bench/compare.py runs/bancA-bride runs/bancB-corrige
"""
import argparse
import json
from pathlib import Path

ARROW = {("failed", "succeeded"): "GAGNE", ("succeeded", "failed"): "PERDU"}


def rows(run_dir):
    path = Path(run_dir) / "rows.jsonl"
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out[(r["model"], r["case"])] = r
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    a = ap.parse_args()
    before, after = rows(a.before), rows(a.after)

    models = list(dict.fromkeys(m for m, _ in after))
    for model in models:
        keys = [(m, c) for (m, c) in after if m == model]
        moved, stable = [], {"succeeded": 0, "failed": 0}
        for key in sorted(keys):
            b, af = before.get(key), after[key]
            if b is None:
                moved.append((key[1], "NOUVEAU", "", af["status"]))
                continue
            change = ARROW.get((b["status"], af["status"]))
            if change:
                moved.append((key[1], change, b["failure_reason"] or "-",
                              af["failure_reason"] or "-"))
            else:
                stable[af["status"]] = stable.get(af["status"], 0) + 1
        nb_after = sum(1 for k in keys if after[k]["success"])
        nb_before = sum(1 for k in keys if k in before and before[k]["success"])
        print("\n=== {} : {} -> {} succes sur {} ===".format(
            model, nb_before, nb_after, len(keys)))
        for case, change, was, now in moved:
            print("  {:12} {:8} {} -> {}".format(case, change, was, now))
        if not moved:
            print("  aucune cellule n'a change d'issue")


if __name__ == "__main__":
    main()

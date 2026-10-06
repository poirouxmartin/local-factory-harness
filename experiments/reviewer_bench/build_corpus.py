"""Build the reviewer bench corpus: green diffs (precision) + mutants (recall).

Precision cases are the jobs that closed green -- spec suite AND the project's
own regression, both verified at job time. A REJECT on one of these is a false
positive, and that is the whole measurement.

Recall cases are those same diffs with a defect injected mechanically. No LLM
writes a mutant: the operators below are the classic mutation-testing set, so
the ground truth is a fact about the file, not another model's opinion. An
ACCEPT on a mutant is a false negative.

Mutations only ever rewrite the CONTENT of a `+` line, never the number of
lines, so every hunk header stays valid and the mutant is still an applicable
diff.

    py -3.9 experiments/reviewer_bench/build_corpus.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JOBS = ROOT / "jobs"
OUT = Path(__file__).resolve().parent / "corpus"


# (name, pattern, replacement) -- ordered, and applied to the first `+` line
# that matches. Order is fixed so the corpus is byte-reproducible.
OPERATORS = [
    ("arith_swap", re.compile(r"(?<=[\w\)\]]) \+ (?=[\w\(])"), " - "),
    ("cmp_gt_to_le", re.compile(r" > "), " <= "),
    ("cmp_ge_to_lt", re.compile(r" >= "), " < "),
    ("eq_to_neq_js", re.compile(r" === "), " !== "),
    ("neq_to_eq_js", re.compile(r" !== "), " === "),
    ("eq_to_neq_py", re.compile(r" == "), " != "),
    ("bool_true_false", re.compile(r"\btrue\b"), "false"),
    ("bool_false_true", re.compile(r"\bfalse\b"), "true"),
    ("and_to_or", re.compile(r" && "), " || "),
    ("or_to_and", re.compile(r" \|\| "), " && "),
]

# Lines where a mutation is meaningless or unverifiable: comments, imports,
# and the diff's own file headers.
SKIP = re.compile(r"^\+\s*(#|//|/\*|\*|import\b|from\b|\+\+\+)")


def added_lines(diff):
    """Indices of mutable `+` lines, skipping headers and comments."""
    out = []
    for i, line in enumerate(diff.split("\n")):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if SKIP.match(line):
            continue
        out.append(i)
    return out


# Rounding precision: `round(x, 2)` -> `round(x, 3)` is a display change, not a
# defect a reviewer can call wrong from a diff. Generating it would make the
# recall score punish correct ACCEPTs, so the operator declines these lines.
NO_OFF_BY_ONE = re.compile(r"\bround\(|\.toFixed\(|\bsetprecision\b")


def off_by_one(line):
    """N -> N+1 on the first standalone integer that is not an index 0/1.

    Kept separate from OPERATORS: it needs a numeric predicate, not a regex
    substitution, and mutating `0` or `1` usually lands on an array index where
    the change is either a no-op or blatantly obvious.
    """
    if NO_OFF_BY_ONE.search(line):
        return None, None
    for m in re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line):
        n = int(m.group(1))
        if n < 2:
            continue
        return line[:m.start()] + str(n + 1) + line[m.end():], "{}->{}".format(n, n + 1)
    return None, None


def mutate(diff):
    """Yield (operator, before, after, mutated_diff) for every distinct hit."""
    lines = diff.split("\n")
    for idx in added_lines(diff):
        line = lines[idx]
        for name, pattern, repl in OPERATORS:
            if not pattern.search(line):
                continue
            new = pattern.sub(repl, line, count=1)
            if new == line:
                continue
            out = list(lines)
            out[idx] = new
            yield name, line, new, "\n".join(out)
        new, what = off_by_one(line)
        if new:
            out = list(lines)
            out[idx] = new
            yield "off_by_one:" + what, line, new, "\n".join(out)


def green_cases():
    cases = []
    for d in sorted(JOBS.glob("j_*")):
        rp, sp = d / "result.json", d / "spec.json"
        if not rp.exists() or not sp.exists():
            continue
        r = json.loads(rp.read_text(encoding="utf-8"))
        if r.get("status") != "succeeded" or not r.get("diff"):
            continue
        s = json.loads(sp.read_text(encoding="utf-8"))
        cases.append({
            "id": d.name,
            "goal": s.get("goal", ""),
            "diff": r["diff"],
            "coder_model": r.get("final_model", ""),
            "recorded_verdict": (r.get("review") or {}).get("verdict", ""),
            "truth": "ACCEPT",
        })
    return cases


def mutant_cases(greens, per_job=1, limit=10):
    """One mutant per job, spread over distinct operators, until `limit`."""
    used_ops, out = set(), []
    for case in greens:
        if len(out) >= limit:
            break
        made = 0
        for name, before, after, diff in mutate(case["diff"]):
            if made >= per_job or len(out) >= limit:
                break
            family = name.split(":")[0]
            if family in used_ops:      # spread the operator families first
                continue
            used_ops.add(family)
            made += 1
            out.append({
                "id": "{}~{}".format(case["id"], family),
                "goal": case["goal"],
                "diff": diff,
                "coder_model": case["coder_model"],
                "truth": "REJECT",
                "defect": {"operator": name, "before": before.strip(),
                           "after": after.strip()},
            })
    return out


def main():
    greens = green_cases()
    if not greens:
        print("no green jobs with a diff found", file=sys.stderr)
        return 1
    mutants = mutant_cases(greens)
    # A second pass without the "distinct family" rule, to top the lot up.
    if len(mutants) < 10:
        seen = {m["id"] for m in mutants}
        for case in greens:
            for name, before, after, diff in mutate(case["diff"]):
                mid = "{}~{}".format(case["id"], name.split(":")[0])
                if mid in seen or len(mutants) >= 10:
                    continue
                seen.add(mid)
                mutants.append({
                    "id": mid, "goal": case["goal"], "diff": diff,
                    "coder_model": case["coder_model"], "truth": "REJECT",
                    "defect": {"operator": name, "before": before.strip(),
                               "after": after.strip()},
                })
            if len(mutants) >= 10:
                break

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "green.json").write_text(json.dumps(greens, indent=2), encoding="utf-8")
    (OUT / "mutants.json").write_text(json.dumps(mutants, indent=2), encoding="utf-8")

    print("green cases : {}".format(len(greens)))
    print("mutants     : {}".format(len(mutants)))
    print("\n-- mutants for human review (a mutant that breaks nothing is an "
          "unfair recall test) --")
    for m in mutants:
        print("\n[{}] {}".format(m["defect"]["operator"], m["id"]))
        print("  before: {}".format(m["defect"]["before"][:110]))
        print("  after : {}".format(m["defect"]["after"][:110]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

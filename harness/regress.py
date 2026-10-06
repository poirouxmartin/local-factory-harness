"""The job regression suite: replay frozen past jobs, compare against baselines.

Every harness/model decision so far was validated on the last 1-3 jobs. This
module makes "validate on the corpus" one command. The corpus manifest only
carries suite metadata (tier, expected); the archived job dir under jobs/
stays the single source of truth for the spec and the pinned base_sha.

Spec: docs/design/specs/2026-07-18-regression-suite-design.md
"""
import json
import subprocess
import time
from collections import namedtuple
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9: the tomli backport ships with the repo
    import tomli as tomllib

from factory_config import ConfigError, load_projects, resolve_project


class RegressError(RuntimeError):
    pass


Thresholds = namedtuple("Thresholds", "attempts_slack seconds_factor confirm_replays")
DEFAULT_THRESHOLDS = Thresholds(1, 1.6, 2)

_TIERS = ("smoke", "full")
_EXPECTED = ("green", "probe")


def load_corpus(corpus_path):
    corpus_path = Path(corpus_path)
    if not corpus_path.exists():
        raise RegressError("no corpus at {}".format(corpus_path))
    try:
        with open(corpus_path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise RegressError("{}: {}".format(corpus_path, e))
    t = raw.get("thresholds", {})
    thresholds = Thresholds(
        t.get("attempts_slack", DEFAULT_THRESHOLDS.attempts_slack),
        t.get("seconds_factor", DEFAULT_THRESHOLDS.seconds_factor),
        t.get("confirm_replays", DEFAULT_THRESHOLDS.confirm_replays))
    entries = {}
    for job_id, e in raw.get("jobs", {}).items():
        tier, expected = e.get("tier"), e.get("expected")
        if tier not in _TIERS:
            raise RegressError("{}: tier must be one of {}".format(job_id, _TIERS))
        if expected not in _EXPECTED:
            raise RegressError("{}: expected must be one of {}".format(job_id, _EXPECTED))
        entries[job_id] = {"tier": tier, "expected": expected,
                           "note": e.get("note", "")}
    return thresholds, entries


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha_resolves(repo, sha):
    p = subprocess.run(["git", "-C", str(repo), "rev-parse", "--quiet",
                        "--verify", sha + "^{commit}"],
                       capture_output=True, text=True)
    return p.returncode == 0


def _entry_issues(job_id, jobs_root, projects, baselines_dir):
    """Why this corpus entry cannot be replayed, [] if it can."""
    job_dir = Path(jobs_root) / job_id
    issues = []
    if not (baselines_dir / (job_id + ".json")).exists():
        issues.append("no baseline (seed with: regress baseline)")
    try:
        spec = _read_json(job_dir / "spec.json")
    except (FileNotFoundError, ValueError) as e:
        return issues + ["spec.json: {}".format(e)]
    try:
        result = _read_json(job_dir / "result.json")
    except (FileNotFoundError, ValueError) as e:
        return issues + ["result.json: {}".format(e)]
    base_sha = result.get("base_sha")
    if not base_sha:
        issues.append("result.json has no base_sha")
    try:
        project = resolve_project(projects, spec.get("project", ""))
    except ConfigError as e:
        return issues + [str(e)]
    if base_sha and not _sha_resolves(project.path, base_sha):
        issues.append("base_sha {} not reachable in {}".format(base_sha[:8], project.path))
    return issues


def check(corpus_path, jobs_root, config_path):
    thresholds, entries = load_corpus(corpus_path)
    projects = load_projects(config_path)
    baselines_dir = Path(corpus_path).parent / "baselines"
    ok, excluded = [], {}
    for job_id in sorted(entries):
        issues = _entry_issues(job_id, jobs_root, projects, baselines_dir)
        if issues:
            excluded[job_id] = issues
        else:
            ok.append(job_id)
    return {"ok": ok, "excluded": excluded,
            "thresholds": dict(thresholds._asdict())}


def baseline_from_result(result):
    return {"success": bool(result.get("success")),
            "attempts": len(result.get("attempts") or []),
            "stages_used": result.get("stages_used") or 0,
            "final_model": result.get("final_model"),
            "total_seconds": result.get("total_seconds") or 0}


def compare(baseline, replay, thresholds):
    """Cost verdict for a green replay. Red replays never reach this."""
    reasons = []
    if replay["attempts"] > baseline["attempts"] + thresholds.attempts_slack:
        reasons.append("attempts {} > baseline {} + {}".format(
            replay["attempts"], baseline["attempts"], thresholds.attempts_slack))
    if baseline["total_seconds"] and \
            replay["total_seconds"] > thresholds.seconds_factor * baseline["total_seconds"]:
        reasons.append("total_seconds {} > {} x baseline {}".format(
            replay["total_seconds"], thresholds.seconds_factor, baseline["total_seconds"]))
    if replay["stages_used"] > baseline["stages_used"]:
        reasons.append("used {} rungs vs baseline {}".format(
            replay["stages_used"], baseline["stages_used"]))
    if reasons:
        return "degraded", reasons
    if replay["attempts"] < baseline["attempts"] or \
            (baseline["total_seconds"] and
             replay["total_seconds"] < 0.7 * baseline["total_seconds"]):
        return "improved", []
    return "ok", []


def seed_baselines(corpus_path, jobs_root, baselines_dir):
    """Free baselines: the archived result.json IS the initial measurement."""
    _, entries = load_corpus(corpus_path)
    baselines_dir = Path(baselines_dir)
    baselines_dir.mkdir(parents=True, exist_ok=True)
    seeded, kept = [], []
    for job_id in sorted(entries):
        out = baselines_dir / (job_id + ".json")
        if out.exists():
            kept.append(job_id)
            continue
        result = _read_json(Path(jobs_root) / job_id / "result.json")
        out.write_text(json.dumps(baseline_from_result(result), indent=2),
                       encoding="utf-8")
        seeded.append(job_id)
    return {"seeded": seeded, "kept": kept}


def promote_baselines(run_dir, baselines_dir, only=None):
    """An accepted run becomes the new reference. Explicit, never silent."""
    run_dir, baselines_dir = Path(run_dir), Path(baselines_dir)
    if not run_dir.exists():
        raise RegressError("no run at {}".format(run_dir))
    baselines_dir.mkdir(parents=True, exist_ok=True)
    promoted, skipped = [], []
    for result_path in sorted(run_dir.glob("j_*/result.json")):
        job_id = result_path.parent.name
        if "_c" in job_id[2:]:  # confirmation replays are not corpus entries
            continue
        if only and job_id not in only:
            continue
        result = _read_json(result_path)
        # A cell that never ran is not a measurement. Promoting an
        # infrastructure error writes "0 attempts, 0.1 s" over a green
        # baseline, after which every honest replay reads DEGRADED forever
        # (2026-07-28: two cells died on a stale git branch in 0.1 s).
        if result.get("status") == "error" or not result.get("attempts"):
            skipped.append(job_id)
            continue
        (baselines_dir / (job_id + ".json")).write_text(
            json.dumps(baseline_from_result(result), indent=2),
            encoding="utf-8")
        promoted.append(job_id)
    return {"promoted": promoted, "skipped": skipped}


def _replay(job_id, replay_spec, run_dir, config_path, gpu_lock_path, run_job_fn):
    from job_store import JobStore  # lazy: keeps module import light for tests
    store = JobStore(run_dir)
    store.create(replay_spec, job_id=job_id)
    result = run_job_fn(job_id, run_dir, config_path, gpu_lock_path=gpu_lock_path)
    replay = baseline_from_result(result)
    if result.get("status") == "error":
        # infra failure, not a red job: the error must reach the report
        replay["error"] = result.get("error") or "unknown error"
    return replay


def run_suite(corpus_path, jobs_root, config_path, tier="smoke", only=None,
              run_job_fn=None, now=None):
    if run_job_fn is None:
        from loop_job import run_job as run_job_fn  # lazy, see _replay
    thresholds, entries = load_corpus(corpus_path)
    checked = check(corpus_path, jobs_root, config_path)
    reg_root = Path(corpus_path).parent
    run_dir = reg_root / "runs" / (now or time.strftime("%Y%m%d-%H%M%S"))
    run_dir.mkdir(parents=True, exist_ok=True)
    gpu_lock_path = Path(jobs_root) / ".gpu.lock"  # queue behind live jobs

    results, regressions, errors = {}, [], []
    try:
        for job_id in checked["ok"]:
            entry = entries[job_id]
            # smoke is a subset of full: --tier full runs everything
            if tier == "smoke" and entry["tier"] != "smoke":
                continue
            if only and job_id not in only:
                continue
            archived = _read_json(Path(jobs_root) / job_id / "result.json")
            spec = dict(_read_json(Path(jobs_root) / job_id / "spec.json"),
                        base_sha=archived["base_sha"])
            baseline = _read_json(reg_root / "baselines" / (job_id + ".json"))

            replay = _replay(job_id, spec, run_dir, config_path, gpu_lock_path,
                             run_job_fn)
            confirmations = 0
            if replay.get("error"):
                verdict, reasons = "error", [replay["error"]]
            elif entry["expected"] == "probe":
                verdict, reasons = ("progress", []) if replay["success"] \
                    else ("expected_red", [])
            elif replay["success"]:
                verdict, reasons = compare(baseline, replay, thresholds)
            else:
                # red 2-of-3: replay the failing entry until 2 reds or 2 greens
                reds, greens, err = 1, 0, None
                while confirmations < thresholds.confirm_replays \
                        and reds < 2 and greens < 2:
                    confirmations += 1
                    c = _replay("{}_c{}".format(job_id, confirmations), spec,
                                run_dir, config_path, gpu_lock_path, run_job_fn)
                    if c.get("error"):
                        err = "confirmation {}: {}".format(confirmations,
                                                           c["error"])
                        break
                    reds, greens = reds + (not c["success"]), greens + c["success"]
                if err:
                    verdict, reasons = "error", [err]
                else:
                    verdict = "regression" if reds >= 2 else "flaky"
                    reasons = ["reds {} / greens {}".format(reds, greens)]
            if verdict == "regression":
                regressions.append(job_id)
            elif verdict == "error":
                errors.append(job_id)
            results[job_id] = {"verdict": verdict, "reasons": reasons,
                               "replay": replay, "baseline": baseline,
                               "confirmations": confirmations}
    finally:
        # a crash mid-suite must not cost the report for finished entries
        summary = {"run_dir": str(run_dir), "tier": tier, "results": results,
                   "regressions": regressions, "errors": errors,
                   "excluded": checked["excluded"]}
        (run_dir / "report.json").write_text(json.dumps(summary, indent=2),
                                             encoding="utf-8")
        (run_dir / "report.md").write_text(render_report(summary),
                                           encoding="utf-8")
    return summary


def render_report(summary):
    lines = ["# Regression run -- tier {}".format(summary["tier"]),
             "", "Run dir: {}".format(summary["run_dir"]), "",
             "| job | verdict | attempts (replay/base) | seconds (replay/base) "
             "| rungs (replay/base) | confirms | reasons |",
             "|---|---|---|---|---|---|---|"]
    for job_id in sorted(summary["results"]):
        r = summary["results"][job_id]
        rep, base = r["replay"], r["baseline"]
        lines.append("| {} | **{}** | {}/{} | {}/{} | {}/{} | {} | {} |".format(
            job_id, r["verdict"].upper(), rep["attempts"], base["attempts"],
            rep["total_seconds"], base["total_seconds"],
            rep["stages_used"], base["stages_used"],
            r["confirmations"], "; ".join(r["reasons"]) or "-"))
    if summary["excluded"]:
        lines += ["", "## Excluded", ""]
        for job_id, issues in sorted(summary["excluded"].items()):
            lines.append("- {}: {}".format(job_id, "; ".join(issues)))
    verdictline = "REGRESSIONS: {}".format(", ".join(summary["regressions"])) \
        if summary["regressions"] else "No confirmed regression."
    lines += ["", verdictline]
    if summary.get("errors"):
        lines.append("INFRA ERRORS (no verdict): {}".format(
            ", ".join(summary["errors"])))
    lines.append("")
    return "\n".join(lines)

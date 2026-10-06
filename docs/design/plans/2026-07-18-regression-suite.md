# Job Regression Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replay a frozen corpus of past delegation jobs (pinned `base_sha`) through the real `loop_job` pipeline and compare green/red + cost against stored baselines, so any harness/model change is validated on 10-14 jobs with one command.

**Architecture:** New `harness/regress.py` (corpus, baselines, verdicts, replay orchestration, report) + `regress` verbs in `harness/factory_cli.py`. The only pipeline change is an optional `base_sha` parameter on `WorkTree.create`, threaded from an optional `base_sha` field in a replay spec. Replays use a separate `JobStore` root under `regression/runs/<ts>/` (no pollution of `jobs/`) but share the main GPU lock (`jobs/.gpu.lock`).

**Tech Stack:** Python 3.9 (stdlib + `tomli` fallback, same pattern as `factory_config.py`), pytest for the harness tests.

**Spec:** `docs/design/specs/2026-07-18-regression-suite-design.md`

## Global Constraints

- Python 3.9 compatible (no `tomllib` direct import — use the `try: import tomllib / except: import tomli as tomllib` pattern from `factory_config.py:12-15`).
- Replays go through the REAL `run_job` with prod defaults (no temp override, no ladder override).
- Replays never write under `jobs/` and never touch the archived job dirs.
- `regression/corpus.toml` + `regression/baselines/` are committed; `regression/runs/` is gitignored.
- Verdict thresholds (from spec): DEGRADED if `attempts > baseline + 1` OR `total_seconds > 1.6 × baseline` OR `stages_used > baseline`; confirmation = up to 2 extra replays, red 2-of-3 = confirmed REGRESSION.
- Commit style: conventional commits, English, ASCII, no AI attribution (project CLAUDE.md).
- All commits directly on `main` (project convention), push after each task.

---

### Task 1: `WorkTree.create` optional `base_sha`

**Files:**
- Modify: `harness/worktree.py:47-56`
- Test: `harness/tests/test_worktree.py`

**Interfaces:**
- Produces: `WorkTree.create(repo, job_id, path, base_sha=None)` — when `base_sha` is given, the worktree and branch are created at that commit (any rev `git rev-parse` accepts); default behavior (HEAD) unchanged.

- [ ] **Step 1: Write the failing test** — append to `harness/tests/test_worktree.py`:

```python
def test_create_at_an_explicit_base_sha(repo, tmp_path):
    first = git(repo, "rev-parse", "HEAD").strip()
    (repo / "calc.py").write_text("def add(a, b):\n    return a * b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "second")

    wt = WorkTree.create(repo, "j_00000002",
                         tmp_path / "jobs" / "j_00000002" / "worktree",
                         base_sha=first)
    try:
        assert wt.base_sha == first
        assert (wt.path / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"
    finally:
        wt.remove()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3 -m pytest harness/tests/test_worktree.py::test_create_at_an_explicit_base_sha -v`
Expected: FAIL with `TypeError: create() got an unexpected keyword argument 'base_sha'`

- [ ] **Step 3: Minimal implementation** — in `harness/worktree.py`, replace the `create` classmethod:

```python
    @classmethod
    def create(cls, repo, job_id, path, base_sha=None):
        repo, path = Path(repo), Path(path)
        branch = "factory/{}".format(job_id)
        # Base on HEAD's commit, not on the working tree: the operator's
        # uncommitted scratch must not become the job's starting point.
        # A replay (regression suite) pins base_sha to the archived commit
        # instead -- a merged diff would otherwise pre-pass the spec tests.
        base_sha = _git(repo, "rev-parse", base_sha or "HEAD").strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", "-q", "-b", branch, str(path), base_sha)
        return cls(repo, job_id, path, branch, base_sha)
```

- [ ] **Step 4: Run the full worktree test file**

Run: `py -3 -m pytest harness/tests/test_worktree.py -v`
Expected: all PASS (the new test plus the existing ones — `rev-parse HEAD` default is preserved).

- [ ] **Step 5: Commit**

```bash
git add harness/worktree.py harness/tests/test_worktree.py
git commit -m "feat: WorkTree.create accepts an explicit base_sha"
```

---

### Task 2: `loop_job` threads `spec["base_sha"]` to the worktree

**Files:**
- Modify: `harness/loop_job.py:447` (inside `_execute`)
- Test: `harness/tests/test_loop_job.py`

**Interfaces:**
- Consumes: `WorkTree.create(..., base_sha=None)` from Task 1.
- Produces: a spec with an optional `"base_sha"` key replays against that commit; specs without it behave exactly as today. Task 5's replay builder relies on this key.

- [ ] **Step 1: Write the failing test** — append to `harness/tests/test_loop_job.py` (uses the module's existing `store`/`config`/`project` fixtures, `FakeModel`, `SPEC`, `GOOD`, `block`, `run` helpers):

```python
def test_a_spec_base_sha_pins_the_worktree_to_that_commit(store, config, project):
    first = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    (project / "calc.py").write_text("def add(a, b):\n    return a * b\n")
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "moved on"],
                   check=True, capture_output=True)
    spec = dict(SPEC, base_sha=first)

    result = run(store, config, FakeModel({"A": block(GOOD)}), spec=spec)

    assert result["status"] == "succeeded"
    assert result["base_sha"] == first
    # The "-" lines prove the tree really was at the FIRST commit, not HEAD.
    assert "-    return a - b" in result["diff"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3 -m pytest harness/tests/test_loop_job.py::test_a_spec_base_sha_pins_the_worktree_to_that_commit -v`
Expected: FAIL — `result["base_sha"]` equals the second commit (worktree still created at HEAD).

- [ ] **Step 3: Minimal implementation** — in `harness/loop_job.py:447`, change:

```python
        worktree = WorkTree.create(project.path, job_id, job_dir / "worktree")
```

to:

```python
        # Replays (regression suite) pin the archived commit; live jobs use HEAD.
        worktree = WorkTree.create(project.path, job_id, job_dir / "worktree",
                                   base_sha=spec.get("base_sha"))
```

- [ ] **Step 4: Run the full loop_job test file**

Run: `py -3 -m pytest harness/tests/test_loop_job.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/loop_job.py harness/tests/test_loop_job.py
git commit -m "feat: loop_job honors an optional spec base_sha for replays"
```

---

### Task 3: `regress.py` — corpus loading and `check`

**Files:**
- Create: `harness/regress.py`
- Test: `harness/tests/test_regress.py`

**Interfaces:**
- Produces (used by Tasks 4-6):
  - `RegressError(RuntimeError)`
  - `Thresholds = namedtuple("Thresholds", "attempts_slack seconds_factor confirm_replays")`, `DEFAULT_THRESHOLDS = Thresholds(1, 1.6, 2)`
  - `load_corpus(corpus_path) -> (thresholds, entries)` where `entries` is `{job_id: {"tier": str, "expected": str, "note": str}}`; raises `RegressError` on missing file, bad tier/expected values.
  - `check(corpus_path, jobs_root, config_path) -> {"ok": [job_id...], "excluded": {job_id: [reason...]}, "thresholds": {...}}` — no GPU; verifies archived `spec.json` parses, `result.json` carries a `base_sha`, the project resolves in factory.toml, and `git rev-parse --quiet --verify <sha>^{commit}` succeeds in the project repo.

- [ ] **Step 1: Write the failing tests** — create `harness/tests/test_regress.py`:

```python
"""The regression suite's own logic: corpus, baselines, verdicts, orchestration.

Everything here runs without a GPU. Replays are exercised through an injected
run_job_fn; the real pipeline is covered by test_loop_job.py plus the first
real `regress run --tier smoke`.
"""
import json
import subprocess
import textwrap

import pytest

from regress import (DEFAULT_THRESHOLDS, RegressError, Thresholds,
                     check, load_corpus)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "f@f.f")
    git(root, "config", "user.name", "factory")
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "seed")
    return root


@pytest.fixture
def config(tmp_path, project):
    path = tmp_path / "factory.toml"
    path.write_text(textwrap.dedent("""
        [projects.demo]
        path = "{}"
        runner = "pytest"
    """.format(str(project).replace("\\", "/"))))
    return path


def write_job(jobs_root, job_id, project="demo", base_sha=None, result_extra=None):
    d = jobs_root / job_id
    d.mkdir(parents=True)
    spec = {"project": project, "goal": "fix add()", "target_files": ["calc.py"],
            "tests": {"test_add.py": "def test_add():\n    assert 1\n"}}
    (d / "spec.json").write_text(json.dumps(spec))
    result = {"success": True, "attempts": [{"n": 1}], "stages_used": 1,
              "final_model": "qwen2.5-coder:7b", "total_seconds": 10.0,
              "status": "succeeded"}
    if base_sha:
        result["base_sha"] = base_sha
    result.update(result_extra or {})
    (d / "result.json").write_text(json.dumps(result))
    return spec, result


def corpus_file(tmp_path, body):
    reg = tmp_path / "regression"
    reg.mkdir(exist_ok=True)
    path = reg / "corpus.toml"
    path.write_text(textwrap.dedent(body))
    return path


def test_load_corpus_reads_thresholds_and_entries(tmp_path):
    path = corpus_file(tmp_path, """
        [thresholds]
        attempts_slack = 2
        seconds_factor = 2.0
        confirm_replays = 1

        [jobs.j_aaaaaaaa]
        tier = "smoke"
        expected = "green"
        note = "demo"
    """)

    th, entries = load_corpus(path)

    assert th == Thresholds(2, 2.0, 1)
    assert entries["j_aaaaaaaa"] == {"tier": "smoke", "expected": "green", "note": "demo"}


def test_load_corpus_defaults_thresholds(tmp_path):
    path = corpus_file(tmp_path, """
        [jobs.j_aaaaaaaa]
        tier = "full"
        expected = "probe"
    """)

    th, entries = load_corpus(path)

    assert th == DEFAULT_THRESHOLDS
    assert entries["j_aaaaaaaa"]["note"] == ""


def test_load_corpus_rejects_bad_values(tmp_path):
    path = corpus_file(tmp_path, """
        [jobs.j_aaaaaaaa]
        tier = "nightly"
        expected = "green"
    """)

    with pytest.raises(RegressError):
        load_corpus(path)


def test_check_accepts_a_replayable_entry(tmp_path, project, config):
    sha = git(project, "rev-parse", "HEAD").strip()
    jobs = tmp_path / "jobs"
    write_job(jobs, "j_aaaaaaaa", base_sha=sha)
    path = corpus_file(tmp_path, """
        [jobs.j_aaaaaaaa]
        tier = "smoke"
        expected = "green"
    """)

    out = check(path, jobs, config)

    assert out["ok"] == ["j_aaaaaaaa"]
    assert out["excluded"] == {}


def test_check_excludes_missing_sha_unknown_project_and_missing_job(tmp_path, project, config):
    sha = git(project, "rev-parse", "HEAD").strip()
    jobs = tmp_path / "jobs"
    write_job(jobs, "j_nosha")                                   # no base_sha
    write_job(jobs, "j_badproj", project="ghost", base_sha=sha)  # unknown project
    write_job(jobs, "j_badsha", base_sha="0" * 40)               # unreachable sha
    path = corpus_file(tmp_path, """
        [jobs.j_nosha]
        tier = "smoke"
        expected = "green"
        [jobs.j_badproj]
        tier = "smoke"
        expected = "green"
        [jobs.j_badsha]
        tier = "smoke"
        expected = "green"
        [jobs.j_gone]
        tier = "smoke"
        expected = "green"
    """)

    out = check(path, jobs, config)

    assert out["ok"] == []
    assert set(out["excluded"]) == {"j_nosha", "j_badproj", "j_badsha", "j_gone"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_regress.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'regress'`

- [ ] **Step 3: Implementation** — create `harness/regress.py`:

```python
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
    with open(corpus_path, "rb") as f:
        raw = tomllib.load(f)
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


def _entry_issues(job_id, jobs_root, projects):
    """Why this corpus entry cannot be replayed, [] if it can."""
    job_dir = Path(jobs_root) / job_id
    issues = []
    try:
        spec = _read_json(job_dir / "spec.json")
    except (FileNotFoundError, ValueError) as e:
        return ["spec.json: {}".format(e)]
    try:
        result = _read_json(job_dir / "result.json")
    except (FileNotFoundError, ValueError) as e:
        return ["result.json: {}".format(e)]
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
    ok, excluded = [], {}
    for job_id in sorted(entries):
        issues = _entry_issues(job_id, jobs_root, projects)
        if issues:
            excluded[job_id] = issues
        else:
            ok.append(job_id)
    return {"ok": ok, "excluded": excluded,
            "thresholds": dict(thresholds._asdict())}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_regress.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/regress.py harness/tests/test_regress.py
git commit -m "feat: regression corpus loading and no-GPU check"
```

---

### Task 4: baselines and verdict rules

**Files:**
- Modify: `harness/regress.py`
- Test: `harness/tests/test_regress.py`

**Interfaces:**
- Consumes: `Thresholds`, `_read_json` from Task 3.
- Produces (used by Task 5):
  - `baseline_from_result(result) -> {"success": bool, "attempts": int, "stages_used": int, "final_model": str|None, "total_seconds": float}` (attempts = `len(result["attempts"])`).
  - `compare(baseline, replay, thresholds) -> (verdict, reasons)` with verdict in `("ok", "degraded", "improved")` — cost comparison for a GREEN replay only.
  - `seed_baselines(corpus_path, jobs_root, baselines_dir) -> {"seeded": [...], "kept": [...]}` — writes `baselines/<job_id>.json` from the archived `result.json`, never overwrites an existing baseline.
  - `promote_baselines(run_dir, baselines_dir, only=None) -> {"promoted": [...]}` — overwrites baselines from a run's replay results (`run_dir/<job_id>/result.json`).

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_regress.py`:

```python
from regress import baseline_from_result, compare, promote_baselines, seed_baselines


def test_baseline_from_result_counts_attempts():
    b = baseline_from_result({"success": True, "attempts": [1, 2, 3],
                              "stages_used": 2, "final_model": "m",
                              "total_seconds": 42.5})
    assert b == {"success": True, "attempts": 3, "stages_used": 2,
                 "final_model": "m", "total_seconds": 42.5}


BASE = {"success": True, "attempts": 2, "stages_used": 1,
        "final_model": "m", "total_seconds": 100.0}


def test_compare_ok_within_thresholds():
    replay = dict(BASE, attempts=3, total_seconds=150.0)
    assert compare(BASE, replay, DEFAULT_THRESHOLDS) == ("ok", [])


def test_compare_degraded_on_attempts():
    verdict, reasons = compare(BASE, dict(BASE, attempts=4), DEFAULT_THRESHOLDS)
    assert verdict == "degraded"
    assert any("attempts" in r for r in reasons)


def test_compare_degraded_on_duration():
    verdict, reasons = compare(BASE, dict(BASE, total_seconds=161.0), DEFAULT_THRESHOLDS)
    assert verdict == "degraded"
    assert any("seconds" in r for r in reasons)


def test_compare_degraded_on_extra_rung():
    verdict, reasons = compare(BASE, dict(BASE, stages_used=2), DEFAULT_THRESHOLDS)
    assert verdict == "degraded"
    assert any("rung" in r for r in reasons)


def test_compare_improved_when_cheaper():
    verdict, _ = compare(BASE, dict(BASE, attempts=1, total_seconds=40.0),
                         DEFAULT_THRESHOLDS)
    assert verdict == "improved"


def test_seed_baselines_from_archive_and_keep_existing(tmp_path, project, config):
    sha = git(project, "rev-parse", "HEAD").strip()
    jobs = tmp_path / "jobs"
    write_job(jobs, "j_aaaaaaaa", base_sha=sha)
    write_job(jobs, "j_bbbbbbbb", base_sha=sha)
    path = corpus_file(tmp_path, """
        [jobs.j_aaaaaaaa]
        tier = "smoke"
        expected = "green"
        [jobs.j_bbbbbbbb]
        tier = "full"
        expected = "green"
    """)
    baselines = tmp_path / "regression" / "baselines"
    baselines.mkdir(parents=True)
    (baselines / "j_bbbbbbbb.json").write_text(json.dumps({"success": True,
        "attempts": 9, "stages_used": 3, "final_model": "old", "total_seconds": 1.0}))

    out = seed_baselines(path, jobs, baselines)

    assert out == {"seeded": ["j_aaaaaaaa"], "kept": ["j_bbbbbbbb"]}
    seeded = json.loads((baselines / "j_aaaaaaaa.json").read_text())
    assert seeded["attempts"] == 1 and seeded["success"] is True
    kept = json.loads((baselines / "j_bbbbbbbb.json").read_text())
    assert kept["attempts"] == 9


def test_promote_baselines_overwrites_from_a_run(tmp_path):
    run_dir = tmp_path / "regression" / "runs" / "20260718-120000"
    (run_dir / "j_aaaaaaaa").mkdir(parents=True)
    (run_dir / "j_aaaaaaaa" / "result.json").write_text(json.dumps(
        {"success": True, "attempts": [1], "stages_used": 1,
         "final_model": "new", "total_seconds": 5.0}))
    baselines = tmp_path / "regression" / "baselines"
    baselines.mkdir(parents=True)
    (baselines / "j_aaaaaaaa.json").write_text(json.dumps({"attempts": 9}))

    out = promote_baselines(run_dir, baselines)

    assert out == {"promoted": ["j_aaaaaaaa"]}
    assert json.loads((baselines / "j_aaaaaaaa.json").read_text())["attempts"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_regress.py -v`
Expected: new tests FAIL with `ImportError` (names not defined).

- [ ] **Step 3: Implementation** — append to `harness/regress.py`:

```python
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
    promoted = []
    for result_path in sorted(run_dir.glob("j_*/result.json")):
        job_id = result_path.parent.name
        if "_c" in job_id[2:]:  # confirmation replays are not corpus entries
            continue
        if only and job_id not in only:
            continue
        (baselines_dir / (job_id + ".json")).write_text(
            json.dumps(baseline_from_result(_read_json(result_path)), indent=2),
            encoding="utf-8")
        promoted.append(job_id)
    return {"promoted": promoted}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_regress.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/regress.py harness/tests/test_regress.py
git commit -m "feat: regression baselines (seed/promote) and cost verdicts"
```

---

### Task 5: `run_suite` — replay, confirmation, report

**Files:**
- Modify: `harness/regress.py`
- Test: `harness/tests/test_regress.py`

**Interfaces:**
- Consumes: Tasks 3-4 functions; `JobStore` (`job_store.py`), `run_job` (`loop_job.py`) with the Task 2 `base_sha` spec field.
- Produces (used by Task 6):
  - `run_suite(corpus_path, jobs_root, config_path, tier="smoke", only=None, run_job_fn=None, now=None) -> summary` where `summary = {"run_dir": str, "tier": str, "results": {job_id: entry_report}, "regressions": [job_id...], "excluded": {...}}` and `entry_report = {"verdict": ..., "reasons": [...], "replay": {...}, "baseline": {...}, "confirmations": [n replays]}`. Verdicts: `regression`, `flaky`, `degraded`, `improved`, `ok`, `progress`, `expected_red`.
  - Writes `report.json` + `report.md` into `regression/runs/<ts>/` (sibling of the corpus file: `Path(corpus_path).parent / "runs"`).
  - `render_report(summary) -> str` (the markdown).
  - Replays share the main GPU lock: `gpu_lock_path = Path(jobs_root) / ".gpu.lock"`.

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_regress.py`:

```python
from regress import render_report, run_suite


class FakeRunJob:
    """Scripted run_job: pops the next result per job id prefix."""
    def __init__(self, script):
        self.script = dict(script)  # {job_id_or_prefix: [result, ...]}
        self.calls = []             # (job_id, jobs_root, gpu_lock_path)

    def __call__(self, job_id, jobs_root, config_path, gpu_lock_path=None, **kw):
        self.calls.append((job_id, Path(jobs_root), gpu_lock_path))
        base = job_id.split("_c")[0] if "_c" in job_id[2:] else job_id
        result = dict(self.script[base].pop(0))
        result.setdefault("total_seconds", 10.0)
        return result


GREEN = {"success": True, "attempts": [1], "stages_used": 1,
         "final_model": "m", "status": "succeeded"}
RED = {"success": False, "attempts": [1, 2], "stages_used": 3,
       "final_model": "m", "status": "failed"}


@pytest.fixture
def rig(tmp_path, project, config):
    """A corpus of one green smoke entry with a seeded baseline."""
    sha = git(project, "rev-parse", "HEAD").strip()
    jobs = tmp_path / "jobs"
    write_job(jobs, "j_aaaaaaaa", base_sha=sha)
    corpus = corpus_file(tmp_path, """
        [thresholds]
        confirm_replays = 2
        [jobs.j_aaaaaaaa]
        tier = "smoke"
        expected = "green"
    """)
    seed_baselines(corpus, jobs, tmp_path / "regression" / "baselines")
    return corpus, jobs, config


def test_green_replay_is_ok_and_report_files_land(rig, tmp_path):
    corpus, jobs, config = rig
    fake = FakeRunJob({"j_aaaaaaaa": [GREEN]})

    s = run_suite(corpus, jobs, config, tier="smoke", run_job_fn=fake,
                  now="20260718-120000")

    assert s["results"]["j_aaaaaaaa"]["verdict"] == "ok"
    assert s["regressions"] == []
    run_dir = tmp_path / "regression" / "runs" / "20260718-120000"
    assert (run_dir / "report.json").exists()
    assert "j_aaaaaaaa" in (run_dir / "report.md").read_text()
    # The replay went through a JobStore under the run dir, sharing jobs/.gpu.lock
    job_id, jobs_root, lock = fake.calls[0]
    assert job_id == "j_aaaaaaaa" and jobs_root == run_dir
    assert lock == jobs / ".gpu.lock"
    # and its spec was the archived one plus the pinned base_sha
    spec = json.loads((run_dir / "j_aaaaaaaa" / "spec.json").read_text())
    assert spec["base_sha"]


def test_red_confirmed_twice_is_a_regression(rig):
    corpus, jobs, config = rig
    fake = FakeRunJob({"j_aaaaaaaa": [RED, RED]})

    s = run_suite(corpus, jobs, config, run_job_fn=fake, now="20260718-120001")

    assert s["results"]["j_aaaaaaaa"]["verdict"] == "regression"
    assert s["regressions"] == ["j_aaaaaaaa"]
    assert [c[0] for c in fake.calls] == ["j_aaaaaaaa", "j_aaaaaaaa_c1"]


def test_red_then_two_greens_is_flaky(rig):
    corpus, jobs, config = rig
    fake = FakeRunJob({"j_aaaaaaaa": [RED, GREEN, GREEN]})

    s = run_suite(corpus, jobs, config, run_job_fn=fake, now="20260718-120002")

    assert s["results"]["j_aaaaaaaa"]["verdict"] == "flaky"
    assert s["regressions"] == []
    assert len(fake.calls) == 3


def test_probe_green_is_progress_and_probe_red_is_expected(tmp_path, project, config):
    sha = git(project, "rev-parse", "HEAD").strip()
    jobs = tmp_path / "jobs"
    write_job(jobs, "j_pgreen", base_sha=sha, result_extra={"success": False})
    write_job(jobs, "j_pred", base_sha=sha, result_extra={"success": False})
    corpus = corpus_file(tmp_path, """
        [jobs.j_pgreen]
        tier = "smoke"
        expected = "probe"
        [jobs.j_pred]
        tier = "smoke"
        expected = "probe"
    """)
    seed_baselines(corpus, jobs, tmp_path / "regression" / "baselines")
    fake = FakeRunJob({"j_pgreen": [GREEN], "j_pred": [RED]})

    s = run_suite(corpus, jobs, config, run_job_fn=fake, now="20260718-120003")

    assert s["results"]["j_pgreen"]["verdict"] == "progress"
    assert s["results"]["j_pred"]["verdict"] == "expected_red"
    assert s["regressions"] == []


def test_tier_and_only_filter_entries(rig):
    corpus, jobs, config = rig
    fake = FakeRunJob({})

    s = run_suite(corpus, jobs, config, tier="full", only=["j_zzzzzzzz"],
                  run_job_fn=fake, now="20260718-120004")

    assert s["results"] == {} and fake.calls == []


def test_render_report_lists_verdicts():
    md = render_report({"run_dir": "x", "tier": "smoke", "excluded": {},
                        "regressions": ["j_bad"],
                        "results": {"j_bad": {
                            "verdict": "regression", "reasons": [],
                            "baseline": BASE,
                            "replay": dict(BASE, success=False, attempts=3),
                            "confirmations": 1}}})
    assert "j_bad" in md and "regression" in md.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_regress.py -v`
Expected: new tests FAIL with `ImportError`.

- [ ] **Step 3: Implementation** — append to `harness/regress.py` (note the two lazy imports keep `regress` importable in tests without pulling model clients):

```python
def _replay(job_id, replay_spec, run_dir, config_path, gpu_lock_path, run_job_fn):
    from job_store import JobStore  # lazy: keeps module import light for tests
    store = JobStore(run_dir)
    store.create(replay_spec, job_id=job_id)
    return baseline_from_result(
        run_job_fn(job_id, run_dir, config_path, gpu_lock_path=gpu_lock_path))


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

    results, regressions = {}, []
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
        if entry["expected"] == "probe":
            verdict, reasons = ("progress", []) if replay["success"] \
                else ("expected_red", [])
        elif replay["success"]:
            verdict, reasons = compare(baseline, replay, thresholds)
        else:
            # red 2-of-3: replay the failing entry until 2 reds or 2 greens
            reds, greens = 1, 0
            while confirmations < thresholds.confirm_replays \
                    and reds < 2 and greens < 2:
                confirmations += 1
                c = _replay("{}_c{}".format(job_id, confirmations), spec,
                            run_dir, config_path, gpu_lock_path, run_job_fn)
                reds, greens = reds + (not c["success"]), greens + c["success"]
            verdict, reasons = ("regression", []) if reds >= 2 else ("flaky", [])
        if verdict == "regression":
            regressions.append(job_id)
        results[job_id] = {"verdict": verdict, "reasons": reasons,
                           "replay": replay, "baseline": baseline,
                           "confirmations": confirmations}

    summary = {"run_dir": str(run_dir), "tier": tier, "results": results,
               "regressions": regressions, "excluded": checked["excluded"]}
    (run_dir / "report.json").write_text(json.dumps(summary, indent=2),
                                         encoding="utf-8")
    (run_dir / "report.md").write_text(render_report(summary), encoding="utf-8")
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
    lines += ["", verdictline, ""]
    return "\n".join(lines)
```

Note for the implementer: `_replay` returns `baseline_from_result(...)` of the run's result — the full replay `result.json` (diff, attempts, log) is already on disk under `run_dir/<job_id>/` because the real `run_job` writes it there via its own JobStore. The FakeRunJob in tests returns the result dict directly, which is all `run_suite` reads.

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_regress.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the whole harness suite** (guard against import-time regressions)

Run: `py -3 -m pytest harness/tests -q`
Expected: all PASS (589+ tests).

- [ ] **Step 6: Commit**

```bash
git add harness/regress.py harness/tests/test_regress.py
git commit -m "feat: regression run_suite with confirmation replays and report"
```

---

### Task 6: CLI verbs `regress check|run|baseline`

**Files:**
- Modify: `harness/factory_cli.py` (`_parser`, new `_dispatch_regress`, `main`)
- Test: `harness/tests/test_regress.py`

**Interfaces:**
- Consumes: `check`, `run_suite`, `seed_baselines`, `promote_baselines`, `RegressError` from Tasks 3-5.
- Produces:
  - `py -3 harness/factory_cli.py regress check [--corpus PATH]`
  - `... regress run --tier smoke|full [--only j_a,j_b] [--corpus PATH]`
  - `... regress baseline [--from-run TS] [--only j_a,j_b] [--corpus PATH]` (without `--from-run`: seed missing baselines from the archive)
  - Exit code 2 when `regress run` confirms >= 1 regression; 0 otherwise.

- [ ] **Step 1: Write the failing tests** — append to `harness/tests/test_regress.py`:

```python
import io

import factory_cli


def test_cli_regress_check_reports_ok_entries(rig, capsys):
    corpus, jobs, config = rig
    out = io.StringIO()

    code = factory_cli.main(["--jobs", str(jobs), "--config", str(config),
                             "regress", "check", "--corpus", str(corpus)], out=out)

    assert code == 0
    assert json.loads(out.getvalue())["ok"] == ["j_aaaaaaaa"]


def test_cli_regress_baseline_seeds_from_archive(tmp_path, project, config):
    sha = git(project, "rev-parse", "HEAD").strip()
    jobs = tmp_path / "jobs"
    write_job(jobs, "j_aaaaaaaa", base_sha=sha)
    corpus = corpus_file(tmp_path, """
        [jobs.j_aaaaaaaa]
        tier = "smoke"
        expected = "green"
    """)
    out = io.StringIO()

    code = factory_cli.main(["--jobs", str(jobs), "--config", str(config),
                             "regress", "baseline", "--corpus", str(corpus)], out=out)

    assert code == 0
    assert json.loads(out.getvalue())["seeded"] == ["j_aaaaaaaa"]
    assert (tmp_path / "regression" / "baselines" / "j_aaaaaaaa.json").exists()


def test_cli_regress_run_exits_2_on_regression(rig, monkeypatch):
    corpus, jobs, config = rig
    monkeypatch.setattr("regress.run_suite",
                        lambda *a, **kw: {"results": {}, "regressions": ["j_x"],
                                          "excluded": {}, "run_dir": "d",
                                          "tier": "smoke"})
    out = io.StringIO()

    code = factory_cli.main(["--jobs", str(jobs), "--config", str(config),
                             "regress", "run", "--corpus", str(corpus)], out=out)

    assert code == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_regress.py -v -k cli`
Expected: FAIL — argparse error `invalid choice: 'regress'`.

- [ ] **Step 3: Implementation** — in `harness/factory_cli.py`:

Add the import (top, with the others):

```python
import regress
```

In `_parser()`, before `return ap`:

```python
    r = sub.add_parser("regress", help="replay the frozen job corpus (spec 2026-07-18)")
    r.add_argument("--corpus", default=str(ROOT / "regression" / "corpus.toml"))
    rsub = r.add_subparsers(dest="rcommand", required=True)
    rsub.add_parser("check", help="validate the corpus, no GPU")
    rr = rsub.add_parser("run", help="replay a tier; exit 2 on confirmed regression")
    rr.add_argument("--tier", choices=("smoke", "full"), default="smoke")
    rr.add_argument("--only", help="comma-separated job ids")
    rb = rsub.add_parser("baseline", help="seed missing baselines, or promote a run")
    rb.add_argument("--from-run", dest="from_run", metavar="TS",
                    help="promote regression/runs/<TS> results as the new baselines")
    rb.add_argument("--only", help="comma-separated job ids")
```

Add the dispatcher (next to `_dispatch_goal`):

```python
def _dispatch_regress(a):
    reg_root = Path(a.corpus).parent
    if a.rcommand == "check":
        return regress.check(a.corpus, a.jobs, a.config)
    only = a.only.split(",") if getattr(a, "only", None) else None
    if a.rcommand == "run":
        return regress.run_suite(a.corpus, a.jobs, a.config, tier=a.tier, only=only)
    if a.from_run:
        return regress.promote_baselines(reg_root / "runs" / a.from_run,
                                         reg_root / "baselines", only=only)
    return regress.seed_baselines(a.corpus, a.jobs, reg_root / "baselines")
```

In `main()`, route the command and add the exit code — replace the `try` body and the final `return 0`:

```python
    try:
        if a.command == "goal":
            # Goals never need the Factory (no jobs dir, no MCP registry).
            payload = _dispatch_goal(a, GoalStore(a.goals))
        elif a.command == "regress":
            # Neither does the regression suite: it drives loop_job directly.
            payload = _dispatch_regress(a)
        else:
            payload = _dispatch(a, factory or Factory(a.jobs, a.config))
    except (ConfigError, JobNotFound, ToolError, GoalStoreError,
            regress.RegressError, OSError) as e:
        err.write("{}: {}\n".format(type(e).__name__, e))
        return 1
    json.dump(payload, out, indent=2)
    out.write("\n")
    if a.command == "regress" and a.rcommand == "run" and payload.get("regressions"):
        return 2  # scripts and CI must see a confirmed regression without parsing
    return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_regress.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the whole harness suite**

Run: `py -3 -m pytest harness/tests -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add harness/factory_cli.py harness/tests/test_regress.py
git commit -m "feat: regress check/run/baseline CLI verbs"
```

---

### Task 7: initial corpus, seeded baselines, docs

**Files:**
- Create: `regression/corpus.toml`
- Create: `regression/baselines/*.json` (generated by the CLI, committed)
- Modify: `.gitignore` (add `regression/runs/`)
- Modify: `docs/backlog-optimisations.md`, `ROADMAP.md` (status lines)

**Interfaces:**
- Consumes: the Task 6 CLI.
- Produces: a committed, `regress check`-green corpus the smoke run (Task 8) executes.

- [ ] **Step 1: Write `regression/corpus.toml`** — curation from the 2026-07-18 inventory of `jobs/` (29 dirs; the 12 crgpd-rerun jobs share ONE spec measured under different configs -> 3 representative entries; `j_5cd8e8e3`/`j_563de476` "dis-moi comment..." chat-style specs and duplicates excluded):

```toml
# The frozen replay corpus. Entries point at archived jobs/<id>/ dirs -- the
# spec and base_sha live THERE; this file only carries suite metadata.
# Curated 2026-07-18. smoke ~ 4 jobs / ~15 min warm; full ~ 12 jobs / 2-3 h.

[thresholds]
attempts_slack = 1
seconds_factor = 1.6
confirm_replays = 2

# --- smoke: one entry per axis (pytest 7b, vitest 7b, diff multi-file 30b, node 35b)

[jobs.j_f18b8f3b]
tier = "smoke"
expected = "green"
note = "local-factory pytest, trivial single-file, closed by 7b in 6.8s"

[jobs.j_1e75691f]
tier = "smoke"
expected = "green"
note = "lucena vitest formatEloDelta, real Next.js repo, 7b 2 attempts"

[jobs.j_7e0084b0]
tier = "smoke"
expected = "green"
note = "crgpd-rerun diff 3-file category scoring, 30b on llama-server, 128s"

[jobs.j_3a6a7681]
tier = "smoke"
expected = "green"
note = "blitzvolley node runner ranks.js, closed by 35b"

# --- full additions

[jobs.j_7f110665]
tier = "full"
expected = "green"
note = "local-factory slugify, 7b 1 attempt"

[jobs.j_9631eba1]
tier = "full"
expected = "green"
note = "local-factory loop.py attempt records, 7b, real harness file"

[jobs.j_0e2bf531]
tier = "full"
expected = "green"
note = "local-factory benchmark.py refactor, 7b"

[jobs.j_5a1e2ee8]
tier = "full"
expected = "green"
note = "session_audit module, took the whole ladder to 35b, 9 attempts"

[jobs.j_bcf15a3c]
tier = "full"
expected = "green"
note = "crgpd diff multi-file via ladder (30b closer), 595s"

[jobs.j_e2a040ff]
tier = "full"
expected = "green"
note = "crgpd diff multi-file closed by 35b, 4 attempts"

# --- probes: historically red, non-blocking; green here = PROGRESS

[jobs.j_bc119315]
tier = "full"
expected = "probe"
note = "loop.py task that burned 10 attempts red pre-guards (2026-07-14)"

[jobs.j_d85cd85b]
tier = "full"
expected = "probe"
note = "crgpd diff multi-file red on 35b, 727s"
```

- [ ] **Step 2: Add to `.gitignore`**

```
regression/runs/
```

- [ ] **Step 3: Validate and seed**

Run: `py -3 harness/factory_cli.py regress check`
Expected: `"ok"` lists 12 ids, `"excluded"` empty. If an old local-factory sha turns out unreachable, remove that entry and say so in the commit message.

Run: `py -3 harness/factory_cli.py regress baseline`
Expected: `"seeded"` lists the same 12 ids; `regression/baselines/` now holds 12 json files.

- [ ] **Step 4: Update the docs** — in `docs/backlog-optimisations.md`, `## Jobs / ladder` table, add:

```markdown
| Suite de régression (corpus 12 jobs figés, replay base_sha, baselines, CLI `regress`) | SHIPPED (18/07) | spec + plan superpowers 2026-07-18 ; premier run smoke = validation e2e | tout changement harnais passe par `regress run` avant adoption | — |
```

In `ROADMAP.md`, under `## Now`, add:

```markdown
- [x] **Job regression suite** — SHIPPED 2026-07-18: `harness/regress.py` +
      `regress check/run/baseline` CLI, corpus of 12 frozen jobs replayed at
      their archived base_sha through the real loop_job, baselines seeded from
      archived results, confirmation replays (red 2-of-3), report.md/json per
      run. Spec: docs/design/specs/2026-07-18-regression-suite-design.md.
```

- [ ] **Step 5: Commit**

```bash
git add regression/ .gitignore docs/backlog-optimisations.md ROADMAP.md
git commit -m "feat: initial regression corpus (12 jobs) and seeded baselines"
git push
```

---

### Task 8: e2e validation — first real smoke run (GPU)

**Files:** none (produces `regression/runs/<ts>/`, gitignored)

**Interfaces:**
- Consumes: everything above, plus the GPU (llama-server ladder). ~15-30 min.

- [ ] **Step 1: Confirm no live job holds the GPU**

Run: `py -3 harness/factory_cli.py status j_bcf15a3c` (or check `jobs/.gpu.lock` absence)

- [ ] **Step 2: Run the smoke tier**

Run: `py -3 harness/factory_cli.py regress run --tier smoke`
Expected: 4 replays run sequentially; exit code 0; `report.md` in the new `regression/runs/<ts>/`.

- [ ] **Step 3: Read the report critically**

Baselines were measured under OLD configs (Ollama-era for the 7b entries), so
IMPROVED/DEGRADED noise is expected on this first run — what must hold is
green staying green. If a green entry goes red: autopsy before concluding
(project CLAUDE.md rule), the suite itself may have a bug (e.g. node_modules
junction at an old sha).

- [ ] **Step 4: Promote the first current-config run as the reference baseline**

Run: `py -3 harness/factory_cli.py regress baseline --from-run <ts>`
Expected: `"promoted"` lists the 4 smoke ids.

- [ ] **Step 5: Report results to Martin** — smoke verdicts, durations vs archive, any exclusion; decide together when to burn the first `--tier full` (2-3 h, overnight candidate).

---

## Self-review notes

- Spec coverage: corpus manifest (T7), baselines seed/promote (T4/T7), worktree pinning (T1/T2), CLI verbs + exit code (T6), verdict table incl. probe/flaky/degraded (T5), report files (T5), no-GPU tests + e2e smoke (T5/T8), gitignore runs (T7). Deviation from spec text: replay artifacts land as full job dirs `runs/<ts>/<job_id>/` (spec said flat `<job_id>.json`) — richer (attempt_N.txt kept) and free, noted here deliberately.
- Type consistency: `run_suite` reads baselines as plain dicts; `compare` consumes the same dict shape `baseline_from_result` produces; FakeRunJob returns raw result dicts exactly like `run_job`.
- `stages_used` (not model names) measures escalation: model ids differ between Ollama and llama-server eras, rung count does not.

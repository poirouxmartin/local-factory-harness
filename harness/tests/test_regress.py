"""The regression suite's own logic: corpus, baselines, verdicts, orchestration.

Everything here runs without a GPU. Replays are exercised through an injected
run_job_fn; the real pipeline is covered by test_loop_job.py plus the first
real `regress run --tier smoke`.
"""
import io
import json
import subprocess
import textwrap

import pytest

from pathlib import Path

import factory_cli
from conftest import write_config
from regress import (DEFAULT_THRESHOLDS, RegressError, Thresholds,
                     baseline_from_result, check, compare, load_corpus,
                     promote_baselines, render_report, run_suite, seed_baselines)


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
    return write_config(tmp_path, textwrap.dedent("""
        [projects.demo]
        runner = "pytest"
    """), paths={"demo": project})


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


def test_load_corpus_wraps_toml_syntax_errors(tmp_path):
    path = corpus_file(tmp_path, "[jobs.j_aaaaaaaa\ntier =")

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
    seed_baselines(path, jobs, tmp_path / "regression" / "baselines")

    out = check(path, jobs, config)

    assert out["ok"] == ["j_aaaaaaaa"]
    assert out["excluded"] == {}


def test_check_excludes_entry_without_baseline(tmp_path, project, config):
    sha = git(project, "rev-parse", "HEAD").strip()
    jobs = tmp_path / "jobs"
    write_job(jobs, "j_aaaaaaaa", base_sha=sha)
    path = corpus_file(tmp_path, """
        [jobs.j_aaaaaaaa]
        tier = "smoke"
        expected = "green"
    """)

    out = check(path, jobs, config)

    assert out["ok"] == []
    assert any("baseline" in i for i in out["excluded"]["j_aaaaaaaa"])


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

    assert out == {"promoted": ["j_aaaaaaaa"], "skipped": []}
    assert json.loads((baselines / "j_aaaaaaaa.json").read_text())["attempts"] == 1


def test_promote_refuses_a_cell_that_never_ran(tmp_path):
    """An infrastructure error is not a measurement.

    2026-07-28: two cells died on a stale git branch in 0.1 s with 0 attempts.
    Promoting that writes a baseline of "0 attempts, 0.1 s" over a green one --
    after which every honest replay reads as DEGRADED, forever, and nothing
    says why. The docstring promises explicit, never silent.
    """
    run_dir = tmp_path / "runs" / "20260728-092619"
    for job_id, result in (
            ("j_good", {"success": True, "attempts": [1], "stages_used": 1,
                        "final_model": "m", "total_seconds": 5.0}),
            ("j_dead", {"status": "error", "error": "GitError: branch exists",
                        "total_seconds": 0.1})):
        (run_dir / job_id).mkdir(parents=True)
        (run_dir / job_id / "result.json").write_text(json.dumps(result))
    baselines = tmp_path / "baselines"

    out = promote_baselines(run_dir, baselines)

    assert out == {"promoted": ["j_good"], "skipped": ["j_dead"]}
    assert not (baselines / "j_dead.json").exists()


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
# run_job's catch-all verdict: no success, no attempts (loop_job.py)
ERROR = {"status": "error", "error": "RuntimeError: llama-server died"}


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
    assert s["results"]["j_aaaaaaaa"]["reasons"] == ["reds 2 / greens 0"]


def test_red_then_two_greens_is_flaky(rig):
    corpus, jobs, config = rig
    fake = FakeRunJob({"j_aaaaaaaa": [RED, GREEN, GREEN]})

    s = run_suite(corpus, jobs, config, run_job_fn=fake, now="20260718-120002")

    assert s["results"]["j_aaaaaaaa"]["verdict"] == "flaky"
    assert s["regressions"] == []
    assert len(fake.calls) == 3
    assert s["results"]["j_aaaaaaaa"]["reasons"] == ["reds 1 / greens 2"]


def test_infra_error_replay_is_error_verdict_not_regression(rig, tmp_path):
    corpus, jobs, config = rig
    fake = FakeRunJob({"j_aaaaaaaa": [dict(ERROR)]})

    s = run_suite(corpus, jobs, config, run_job_fn=fake, now="20260718-120010")

    r = s["results"]["j_aaaaaaaa"]
    assert r["verdict"] == "error"
    assert any("llama-server died" in x for x in r["reasons"])
    assert s["regressions"] == []
    assert s["errors"] == ["j_aaaaaaaa"]
    assert len(fake.calls) == 1  # no confirmation replays burned on infra errors
    md = (tmp_path / "regression" / "runs" / "20260718-120010"
          / "report.md").read_text()
    assert "llama-server died" in md


def test_infra_error_during_confirmation_is_error_verdict(rig):
    corpus, jobs, config = rig
    fake = FakeRunJob({"j_aaaaaaaa": [RED, dict(ERROR)]})

    s = run_suite(corpus, jobs, config, run_job_fn=fake, now="20260718-120011")

    assert s["results"]["j_aaaaaaaa"]["verdict"] == "error"
    assert s["regressions"] == [] and s["errors"] == ["j_aaaaaaaa"]
    assert len(fake.calls) == 2  # stopped at the error, no third replay


def test_report_written_even_when_a_replay_raises(rig, tmp_path):
    corpus, jobs, config = rig

    def exploding(job_id, jobs_root, config_path, gpu_lock_path=None, **kw):
        raise RuntimeError("harness bug")

    with pytest.raises(RuntimeError):
        run_suite(corpus, jobs, config, run_job_fn=exploding,
                  now="20260718-120012")

    run_dir = tmp_path / "regression" / "runs" / "20260718-120012"
    assert (run_dir / "report.json").exists()
    assert (run_dir / "report.md").exists()


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


def test_cli_regress_run_exits_3_on_infra_error(rig, monkeypatch):
    corpus, jobs, config = rig
    monkeypatch.setattr("regress.run_suite",
                        lambda *a, **kw: {"results": {}, "regressions": [],
                                          "errors": ["j_x"], "excluded": {},
                                          "run_dir": "d", "tier": "smoke"})
    out = io.StringIO()

    code = factory_cli.main(["--jobs", str(jobs), "--config", str(config),
                             "regress", "run", "--corpus", str(corpus)], out=out)

    assert code == 3  # inconclusive run must not look green to scripts

"""The runner: worktree in, diff out.

The model is faked (it is a network call); everything else is real -- a real git
repo, a real worktree, a real pytest. What is pinned here is the contract Claude
relies on: a job always leaves exactly one result.json, always cleans up its
worktree, and never lets a green light through without the tests earning it.
"""
import subprocess
import textwrap

import pytest

import loop_job
from conftest import write_config
from job_store import JobStore
from loop_job import run_job

GOOD = "def add(a, b):\n    return a + b\n"
BUGGY = "def add(a, b):\n    return a - b\n"
STILL_BROKEN = "def add(a, b):\n    return a * b\n"
# Two cases on purpose: `return a * b` satisfies add(2, 2) == 4 and would sail
# through a one-assert suite. A weak judge is how a broken model reads as green.
TEST = ("from calc import add\n\n"
        "def test_add():\n    assert add(2, 2) == 4\n    assert add(1, 5) == 6\n")

LADDER = [{"model": "A", "temperature": 0.1, "attempts": 2},
          {"model": "B", "temperature": 0.3, "attempts": 2}]


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "src"
    (root / "suite").mkdir(parents=True)
    git_init(root)
    (root / "calc.py").write_text(BUGGY)
    (root / "suite" / "test_contract.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path.cwd()))\n"
        "from calc import add\n\n"
        "def test_signature_survives():\n    assert add(0, 0) == 0\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "seed")
    return root


def git_init(root):
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "f@f.f")
    git(root, "config", "user.name", "factory")


@pytest.fixture
def config(tmp_path, project):
    return write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                        paths={"demo": project})


@pytest.fixture
def config_with_regression(tmp_path, project):
    return write_config(
        tmp_path,
        '[projects.demo]\nrunner = "pytest"\n'
        'regression_cmd = ["-m", "pytest", "suite", "-q"]\n',
        paths={"demo": project}, name="factory_reg.toml")


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs")


SPEC = {"project": "demo", "goal": "add() must add",
        "tests": {"test_calc.py": TEST}, "target_files": ["calc.py"]}


class FakeModel:
    """Records the prompts it saw and replies from a per-model script."""

    def __init__(self, replies):
        self.replies = replies
        self.prompts = []
        self.num_ctxs = []
        self.calls = []

    def __call__(self, model, system, user, temperature=0.1, num_ctx=16384, **kw):
        self.prompts.append((model, user))
        self.num_ctxs.append(num_ctx)
        self.calls.append({"temperature": temperature, "num_ctx": num_ctx, **kw})
        reply = self.replies[model]
        item = reply if isinstance(reply, (str, dict)) else reply.pop(0)
        resp = {"content": "", "tokens_per_s": 42.0, "elapsed": 0.1,
                "prefill_tokens_per_s": 7.0, "ttft_s": 1.5,
                "done_reason": "stop", "eval_count": 7}
        if isinstance(item, str):
            resp["content"] = item
        else:
            resp.update(item)
        return resp


def block(code):
    return "```python\n{}```\n".format(code)


def run(store, config, chat, job_id="j_00000001", ladder=LADDER, spec=None, **kw):
    store.create(spec or SPEC, job_id=job_id)
    return run_job(job_id, store.root, config, chat_fn=chat, ladder=ladder,
                   system="you are a coder", heartbeat_interval=0.05,
                   unload_fn=lambda m: None, **kw)


def test_a_model_that_fixes_the_bug_succeeds(store, config):
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config, chat)

    assert result["status"] == "succeeded"
    assert store.status("j_00000001")["status"] == "succeeded"


def test_start_stage_skips_the_lower_rungs(store, config):
    # DEFAULT_LADDER is 7b/30b/35b (the 14b rung was dropped 2026-07-18: two
    # audits found it never rescued a 7b failure); start_stage=2 must land on
    # 30b directly. A reply keyed only by "qwen3-coder:30b" makes any call to
    # a lower rung raise KeyError, so a regression back to rung 1 fails loudly.
    chat = FakeModel({"qwen3-coder:30b": block(GOOD)})
    spec = dict(SPEC, start_stage=2)

    result = run(store, config, chat, ladder=loop_job.DEFAULT_LADDER, spec=spec)

    assert result["status"] == "succeeded"
    assert chat.prompts[0][0] == "qwen3-coder:30b"
    assert all(model == "qwen3-coder:30b" for model, _ in chat.prompts)


def test_the_deliverable_is_a_diff_against_a_recorded_base(store, config, project):
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config, chat)

    assert "+    return a + b" in result["diff"]
    assert "-    return a - b" in result["diff"]
    head = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert result["base_sha"] == head


def test_the_source_repo_is_left_untouched(store, config, project):
    run(store, config, FakeModel({"A": block(GOOD)}))

    assert (project / "calc.py").read_text() == BUGGY


def test_the_worktree_is_removed_afterwards(store, config, project):
    run(store, config, FakeModel({"A": block(GOOD)}))

    assert not (store.job_dir("j_00000001") / "worktree").exists()
    listing = subprocess.run(["git", "-C", str(project), "worktree", "list"],
                             capture_output=True, text=True).stdout
    assert "j_00000001" not in listing


def test_a_model_that_never_fixes_the_bug_fails_and_still_returns_its_diff(store, config):
    chat = FakeModel({"A": block(STILL_BROKEN), "B": block(STILL_BROKEN)})

    result = run(store, config, chat)

    assert result["status"] == "failed"
    assert "return a * b" in result["diff"]
    assert result["tests_failed"] == 1


def test_the_worktree_is_removed_even_when_the_job_fails(store, config):
    run(store, config, FakeModel({"A": block(STILL_BROKEN), "B": block(STILL_BROKEN)}))

    assert not (store.job_dir("j_00000001") / "worktree").exists()


def test_a_plateau_escalates_to_the_next_model(store, config):
    chat = FakeModel({"A": block(STILL_BROKEN), "B": block(GOOD)})

    result = run(store, config, chat)

    assert result["status"] == "succeeded"
    assert result["escalated"] is True
    assert result["final_model"] == "B"


def test_the_escalated_model_gets_a_clean_slate_not_the_weak_model_s_wreckage(store, config):
    """ADR-009: handing the stronger model the weaker one's broken file hurts it."""
    chat = FakeModel({"A": block(STILL_BROKEN), "B": block(GOOD)})

    run(store, config, chat)

    first_prompt_to_b = next(p for m, p in chat.prompts if m == "B")
    assert "return a - b" in first_prompt_to_b, "B should see the original bug"
    assert "return a * b" not in first_prompt_to_b, "B inherited A's wreckage"


def test_a_reply_with_no_code_block_is_survived_not_crashed(store, config):
    chat = FakeModel({"A": ["I refuse.", block(GOOD)]})

    result = run(store, config, chat)

    assert result["status"] == "succeeded"
    assert result["attempts"][0]["result"] == "NoCode"


def test_a_model_that_only_ever_refuses_ends_as_failed(store, config):
    chat = FakeModel({"A": "no thanks", "B": "still no"})

    result = run(store, config, chat)

    assert result["status"] == "failed"


def test_a_nocode_reply_gets_a_free_retry_instead_of_burning_an_attempt(store, config):
    """Blitzvolley pilot j_e3eae8cd: the job died on a NoCode reply while holding
    its best diff (10/13). A reply with no code teaches the ladder nothing about
    the model's coding ability -- it burns a reminder retry, not an attempt."""
    chat = FakeModel({"A": ["I drifted into prose.", block(GOOD)]})

    result = run(store, config, chat,
                 ladder=[{"model": "A", "temperature": 0.1, "attempts": 1}])

    assert result["status"] == "succeeded"
    assert result["attempts"][0]["result"] == "NoCode"


def test_two_nocode_replies_running_leave_the_rung(store, config):
    """The free retry must not become a prose loop: a second NoCode in a row means
    the model has drifted out of code mode -- leave the rung, do not call it again."""
    chat = FakeModel({"A": ["no", "still no"], "B": block(GOOD)})

    result = run(store, config, chat,
                 ladder=[{"model": "A", "temperature": 0.1, "attempts": 3},
                         {"model": "B", "temperature": 0.3, "attempts": 2}])

    assert result["status"] == "succeeded"
    assert result["escalated"] is True
    assert sum(1 for m, _ in chat.prompts if m == "A") == 2


def test_attempts_record_done_reason_and_eval_count(store, config):
    """Blitzvolley re-run j_7e7c0bbb: three codeless 35b replies were undiagnosable
    because the ladder dropped the response metadata -- a reply truncated by
    num_predict looked identical to a model drifting into prose."""
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config, chat)

    assert result["attempts"][0]["done_reason"] == "stop"
    assert result["attempts"][0]["eval_count"] == 7


def test_a_truncated_codeless_reply_gets_a_truncation_reminder(store, config):
    """A reply cut by num_predict mid-thinking never reached the code; telling the
    model to 'follow the format' is wrong feedback -- tell it it was cut off and
    to lead with the code block."""
    chat = FakeModel({"A": [{"content": "endless deliberation",
                             "done_reason": "length"}, block(GOOD)]})

    result = run(store, config, chat)

    assert result["status"] == "succeeded"
    retry_prompt = chat.prompts[1][1]
    assert "cut off" in retry_prompt
    assert "code block first" in retry_prompt


def test_the_nocode_retry_keeps_the_failing_test_feedback(store, config):
    """The retry prompt must carry BOTH the rejection reminder and the previous
    failing-test output: the worktree still holds the best diff, and the model
    needs to know which specs were red, not just that its last reply had no code."""
    chat = FakeModel({"A": [block(STILL_BROKEN), "prose instead of code",
                            block(GOOD)]})

    result = run(store, config, chat,
                 ladder=[{"model": "A", "temperature": 0.1, "attempts": 2}])

    assert result["status"] == "succeeded"
    retry_prompt = chat.prompts[2][1]
    assert "rejected" in retry_prompt
    assert "test_add" in retry_prompt, "the failing-test feedback was dropped"


def test_state_tracks_progress_while_the_job_runs(store, config):
    run(store, config, FakeModel({"A": block(GOOD)}))

    state = store.read_state("j_00000001")
    assert state["stage"] == 1
    assert state["tests_passed"] == 1


def test_the_job_records_its_pid_so_it_can_be_cancelled(store, config):
    import os

    run(store, config, FakeModel({"A": block(GOOD)}))

    assert store.read_state("j_00000001")["pid"] == os.getpid()


def test_a_spec_that_targets_the_judge_is_refused_before_the_model_is_called(store, config):
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config, chat, spec=dict(SPEC, target_files=["conftest.py"]))

    assert result["status"] == "error"
    assert chat.prompts == []


def test_the_result_is_written_exactly_once(store, config):
    run(store, config, FakeModel({"A": block(GOOD)}))

    assert (store.job_dir("j_00000001") / "result.json").exists()
    assert store.read_result("j_00000001")["status"] == "succeeded"


def test_a_job_that_cannot_get_the_gpu_fails_instead_of_hanging(store, config, tmp_path):
    from gpu_lock import GpuLock
    holder = GpuLock(tmp_path / "gpu.lock")
    holder.acquire(timeout=0)
    try:
        result = run(store, config, FakeModel({"A": block(GOOD)}),
                     gpu_lock_path=tmp_path / "gpu.lock", gpu_timeout=0)
    finally:
        holder.release()

    assert result["status"] == "failed"
    assert result["error"] == "gpu_timeout"


def test_a_project_outside_the_allowlist_is_refused_before_anything_runs(store, config):
    spec = dict(SPEC, project="not-listed")
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config, chat, spec=spec)

    assert result["status"] == "error"
    assert "not in factory.toml" in result["error"]
    assert chat.prompts == [], "the model must never have been called"


def test_a_spec_naming_a_judge_file_as_a_target_never_reaches_the_model(store, config):
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config, chat, spec=dict(SPEC, target_files=["calc.py", "test_calc.py"]))

    assert result["status"] == "error"
    assert "grade the job" in result["error"]
    assert chat.prompts == []


def test_a_diff_that_grew_a_judge_file_is_rejected_even_though_the_tests_are_green(store, config):
    """The last gate. The model may only write `calc.py` -- but code it wrote can
    create files when imported, and `git add -A` stages whatever it finds. The diff
    is the artifact a human reads, so the diff is what gets the final check."""
    sneaky = (
        "import pathlib\n"
        "pathlib.Path(__file__).with_name('conftest.py').write_text('# planted\\n')\n"
        "def add(a, b):\n    return a + b\n"
    )
    chat = FakeModel({"A": block(sneaky)})

    result = run(store, config, chat)

    assert result["tests_passed"] == 1, "the suite really was green"
    assert result["status"] == "rejected"
    assert result["forbidden_paths"] == ["conftest.py"]


def test_an_attempt_records_the_prefill_cost(store, config):
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config, chat)

    assert result["attempts"][0]["prefill_tokens_per_s"] == 7.0
    assert result["attempts"][0]["ttft_s"] == 1.5


def test_a_reference_file_appears_in_the_prompt(store, config, project):
    (project / "elo.py").write_text("K = 32\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "add elo")
    chat = FakeModel({"A": block(GOOD)})

    run(store, config, chat, spec=dict(SPEC, context_files=["elo.py"]))

    prompt = chat.prompts[0][1]
    assert "K = 32" in prompt
    assert "do NOT rewrite" in prompt


def test_a_model_that_tries_to_write_a_reference_file_is_refused(store, config, project):
    (project / "elo.py").write_text("K = 32\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "add elo")
    chat = FakeModel({"A": "### FILE: elo.py\n" + block("K = 99\n"),
                      "B": "### FILE: elo.py\n" + block("K = 99\n")})

    result = run(store, config, chat, spec=dict(SPEC, context_files=["elo.py"]))

    assert result["status"] == "failed"
    assert result["attempts"][0]["result"] == "UnknownTarget"


def test_no_context_files_leaves_the_prompt_as_it_was(store, config):
    chat = FakeModel({"A": block(GOOD)})

    run(store, config, chat)

    assert "do NOT rewrite" not in chat.prompts[0][1]


def test_context_files_raise_the_context_window(store, config, project):
    """60 KB of reference is ~17-19k tokens; the 16384 default would silently
    truncate it (ADR-008)."""
    (project / "elo.py").write_text("K = 32\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "add elo")
    chat = FakeModel({"A": block(GOOD)})

    run(store, config, chat, spec=dict(SPEC, context_files=["elo.py"]))

    assert chat.num_ctxs[0] == 32768


def test_a_context_free_job_keeps_the_default_window(store, config):
    chat = FakeModel({"A": block(GOOD)})

    run(store, config, chat)

    assert chat.num_ctxs[0] == 16384


def test_a_job_that_satisfies_its_spec_and_keeps_the_project_green_succeeds(
        store, config_with_regression):
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config_with_regression, chat)

    assert result["status"] == "succeeded"
    assert result["regression_passed"] is True
    assert result["failure_reason"] is None


def test_a_job_that_satisfies_its_spec_but_breaks_the_project_fails(
        store, config_with_regression):
    """add(0, 0) == 0 is the project's contract. `a + b + 0*1` passes the spec and
    breaks nothing; `a + b` passes both. This one passes the spec and breaks the
    contract."""
    breaks_contract = "def add(a, b):\n    return (a + b) if (a or b) else 99\n"
    chat = FakeModel({"A": block(breaks_contract), "B": block(breaks_contract)})

    result = run(store, config_with_regression, chat)

    assert result["status"] == "failed"
    assert result["failure_reason"] == "regression"
    assert result["regression_passed"] is False
    assert result["tests_passed"] == 1, "its own spec really was green"


def test_the_broken_project_suite_is_fed_back_to_the_model(store, config_with_regression):
    breaks_contract = "def add(a, b):\n    return (a + b) if (a or b) else 99\n"
    chat = FakeModel({"A": [block(breaks_contract), block(GOOD)]})

    result = run(store, config_with_regression, chat,
                 ladder=[{"model": "A", "temperature": 0.1, "attempts": 2}])

    assert result["status"] == "succeeded"
    assert "test_signature_survives" in chat.prompts[1][1]


def test_the_regression_suite_does_not_run_while_the_spec_is_red(store, config_with_regression):
    chat = FakeModel({"A": block(STILL_BROKEN), "B": block(STILL_BROKEN)})

    result = run(store, config_with_regression, chat)

    assert result["status"] == "failed"
    assert result["failure_reason"] == "spec_tests"
    assert result["regression_passed"] is None, "it must never have been run"


def test_stale_regression_state_does_not_leak_into_a_spec_failure(store, config_with_regression):
    """Attempt 1 satisfies the spec but breaks the project suite (regression fields
    get populated). Attempt 2 fails the spec outright, so the regression never runs
    for it -- the final result must not carry attempt 1's stale regression verdict."""
    breaks_contract = "def add(a, b):\n    return (a + b) if (a or b) else 99\n"
    chat = FakeModel({"A": [block(breaks_contract), block(STILL_BROKEN)]})

    result = run(store, config_with_regression, chat,
                 ladder=[{"model": "A", "temperature": 0.1, "attempts": 2}])

    assert result["status"] == "failed"
    assert result["failure_reason"] == "spec_tests"
    assert result["regression_passed"] is None
    assert result["regression_output"] == ""


def test_a_planted_judge_file_rejects_the_job_outright(store, config_with_regression):
    """The model's code plants a conftest at import time. A non-empty restore_judge
    report is disqualifying (ADR-015): the job is rejected on the spot and the
    project suite never runs on the tampered tree."""
    sneaky = (
        "import pathlib\n"
        "pathlib.Path(__file__).with_name('conftest.py').write_text('# planted\\n')\n"
        "def add(a, b):\n    return a + b\n"
    )
    chat = FakeModel({"A": block(sneaky)})

    result = run(store, config_with_regression, chat)

    assert result["status"] == "rejected"
    assert result["judge_tampered"] == ["conftest.py"]
    assert result["failure_reason"] == "judge_tampered"
    assert result["regression_passed"] is None, "the suite must not run on a tampered tree"


def test_a_planted_pyproject_rejects_the_job_outright(store, config_with_regression):
    """Same exploit as the conftest one, aimed at pyproject.toml: it can carry a
    [tool.pytest.ini_options] addopts just like pytest.ini, so restore_judge must
    treat it as a judge file too."""
    sneaky = (
        "import pathlib\n"
        "pathlib.Path(__file__).with_name('pyproject.toml').write_text('# planted\\n')\n"
        "def add(a, b):\n    return a + b\n"
    )
    chat = FakeModel({"A": block(sneaky)})

    result = run(store, config_with_regression, chat)

    assert result["status"] == "rejected"
    assert result["judge_tampered"] == ["pyproject.toml"]


def test_a_one_shot_tamper_is_rejected_even_when_the_final_diff_is_clean(
        store, config_with_regression):
    """A tamper that fires only during the spec run leaves a clean final diff:
    restore_judge deletes the plant and the regression run never re-creates it, so
    the diff gate sees nothing. This job used to read as succeeded -- the exact hole
    ADR-015 closes. The plant keys on sys.argv: the spec run's pytest args carry the
    external tests dir, the regression run's carry 'suite'."""
    sneaky = (
        "import pathlib, sys\n"
        "if 'suite' not in ' '.join(sys.argv):\n"
        "    pathlib.Path(__file__).with_name('conftest.py').write_text('# planted\\n')\n"
        "def add(a, b):\n    return a + b\n"
    )
    chat = FakeModel({"A": block(sneaky)})

    result = run(store, config_with_regression, chat)

    assert result["status"] == "rejected"
    assert result["judge_tampered"] == ["conftest.py"]
    assert result["forbidden_paths"] == [], "the diff gate alone would have missed it"


def test_a_project_without_a_regression_command_still_succeeds(store, config):
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config, chat)

    assert result["status"] == "succeeded"
    assert result["regression_passed"] is None


def test_a_model_that_only_refuses_reports_no_code(store, config):
    chat = FakeModel({"A": "no thanks", "B": "still no"})

    result = run(store, config, chat)

    assert result["failure_reason"] == "no_code"


def test_regression_red_twice_does_not_plateau_the_stage(store, config_with_regression):
    """tests_failed is 0 on a regression-red attempt (the spec is satisfied); two of
    those in a row must not plateau the stage -- that would abort a repair loop that
    is making real progress on the project suite (ADR-014)."""
    breaks_contract = "def add(a, b):\n    return (a + b) if (a or b) else 99\n"
    chat = FakeModel({"A": [block(breaks_contract), block(breaks_contract), block(GOOD)]})

    result = run(store, config_with_regression, chat,
                 ladder=[{"model": "A", "temperature": 0.1, "attempts": 3}])

    assert result["status"] == "succeeded"
    assert len(result["attempts"]) == 3, "the stage must not have plateaued after 2"
    assert all(not a.get("plateau") for a in result["attempts"][:2])


def test_an_abnormal_spec_exit_is_recorded_but_does_not_feed_the_plateau(store, config):
    """A collection error exits pytest 2, which parses as 0 passed / 0 failed --
    indistinguishable from 'ran nothing'. It must show up on the attempt record and
    must not silently poison fail_hist. A syntax error no longer reaches pytest (the
    extraction's compile gate turns it into NoCode), so the bomb here is valid
    syntax that explodes at import time."""
    import_bomb = "raise RuntimeError('boom')\n"
    chat = FakeModel({"A": [block(import_bomb), block(import_bomb)], "B": block(GOOD)})

    result = run(store, config, chat)

    assert result["attempts"][0]["returncode"] == 2
    assert result["attempts"][1]["returncode"] == 2
    assert all(not a.get("plateau") for a in result["attempts"][:2])
    assert result["status"] == "succeeded"
    assert result["escalated"] is True


def test_a_syntax_error_reply_is_rejected_before_it_reaches_the_judge(store, config):
    """Job j_bc119315: every rung shipped a truncated loop.py and burned a pytest
    run on the same SyntaxError, ten attempts running. The extraction now refuses a
    python target with no compiling candidate, so the attempt is a NoCode with
    actionable feedback, not a collection error."""
    syntax_error = "def add(a, b)\n    return a + b\n"
    chat = FakeModel({"A": [block(syntax_error), block(syntax_error)], "B": block(GOOD)})

    result = run(store, config, chat)

    assert result["attempts"][0]["result"] == "NoCode"
    assert "returncode" not in result["attempts"][0]
    assert result["status"] == "succeeded"
    assert result["escalated"] is True


def test_a_restore_judge_report_is_written_to_the_job_log(store, config_with_regression):
    sneaky = (
        "import pathlib\n"
        "pathlib.Path(__file__).with_name('conftest.py').write_text('# planted\\n')\n"
        "def add(a, b):\n    return a + b\n"
    )
    chat = FakeModel({"A": block(sneaky)})

    run(store, config_with_regression, chat)

    assert "conftest.py" in store.read_log("j_00000001")


def test_a_green_job_carries_the_reviewers_verdict(store, config):
    """ADR-003's verifier, advisory in v1 (ADR-016): the reviewer sees the diff of a
    green job and its verdict lands in the result without touching the status."""
    chat = FakeModel({"A": [block(GOOD), "ACCEPT\n- minimal, matches the goal"]})

    result = run(store, config, chat,
                 ladder=[{"model": "A", "temperature": 0.1, "attempts": 1}],
                 reviewer_system="you are a reviewer")

    assert result["status"] == "succeeded"
    assert result["review"]["verdict"] == "ACCEPT"
    assert "minimal" in result["review"]["notes"]
    model, prompt = chat.prompts[-1]
    assert "+    return a + b" in prompt, "the reviewer must be shown the diff"


def test_a_reject_verdict_is_recorded_but_does_not_flip_a_green_job(store, config):
    chat = FakeModel({"A": [block(GOOD), "REJECT\n- I do not like Mondays"]})

    result = run(store, config, chat,
                 ladder=[{"model": "A", "temperature": 0.1, "attempts": 1}],
                 reviewer_system="you are a reviewer")

    assert result["status"] == "succeeded"
    assert result["review"]["verdict"] == "REJECT"


def test_a_failed_job_is_not_reviewed(store, config):
    chat = FakeModel({"A": block(STILL_BROKEN), "B": block(STILL_BROKEN)})

    result = run(store, config, chat, reviewer_system="you are a reviewer")

    assert result["status"] == "failed"
    assert result["review"] is None
    assert len(chat.prompts) == 4, "attempts only; no reviewer call on a red job"


def test_an_unparseable_reviewer_reply_is_not_a_job_failure(store, config):
    chat = FakeModel({"A": [block(GOOD), "well, it depends"]})

    result = run(store, config, chat,
                 ladder=[{"model": "A", "temperature": 0.1, "attempts": 1}],
                 reviewer_system="you are a reviewer")

    assert result["status"] == "succeeded"
    assert result["review"]["verdict"] == "unparseable"


def test_a_crashing_reviewer_does_not_kill_a_green_job(store, config):
    """The reviewer is advisory: its crash must never cost a job that earned green."""
    chat = FakeModel({"A": [block(GOOD)]})  # the review pop will raise IndexError

    result = run(store, config, chat,
                 ladder=[{"model": "A", "temperature": 0.1, "attempts": 1}],
                 reviewer_system="you are a reviewer")

    assert result["status"] == "succeeded"
    assert result["review"]["verdict"] == "error"


def test_a_misconfigured_regression_command_is_an_error_not_a_model_failure(
        store, tmp_path, project):
    """A typo in factory.toml must never be handed to the model as something to fix."""
    cfg = write_config(
        tmp_path,
        '[projects.demo]\nrunner = "pytest"\n'
        'regression_cmd = ["-m", "pytest", "no_such_dir", "-q"]\n',
        paths={"demo": project}, name="factory_typo.toml")
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, cfg, chat)

    assert result["status"] == "error"
    assert "RegressionCommandError" in result["error"]
    assert len(chat.prompts) == 1, "the model must not be asked to fix a config typo"


def test_a_judge_that_never_ran_is_an_error_not_a_model_failure(
        store, config, monkeypatch):
    """Observed 2026-07-22 (j_0f5c725e), and it had happened once before
    unexplained (delegation v2 demo, "attempt 9: 0/0 on a correct diff").

    The studio had been started with an interpreter that has no pytest, so the
    judge exited 1 with "No module named pytest": no counts, no collection, no
    suite. The ladder read that as "the model failed the spec" and climbed all
    of it -- 6 attempts, 3 models -- then reported `failed` while holding a
    diff that passes the suite by hand.

    Exit 1 with zero tests collected cannot be a spec failure: a real failure
    prints counts. It is the runner that did not run.
    """
    def never_ran(worktree, tests_dir, timeout=None):
        return loop_job.pytest_runner.Result(
            False, 0, 0, "No module named pytest", False, 1)
    monkeypatch.setattr(loop_job.pytest_runner, "run_tests", never_ran)
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config, chat)

    assert result["status"] == "error"
    assert "JudgeCommandError" in result["error"]
    assert len(chat.prompts) == 1, "the model must not be asked to fix a broken runner"


def test_a_structural_failure_aborts_the_run_instead_of_climbing_the_ladder(
        store, config):
    """Job j_bc119315: 10 attempts / 4 rungs / 688 s, all dead on the same
    collection error. An abnormal exit is model-invariant -- the identical
    signature STRUCTURAL_ABORT_K times running must abort the run."""
    import_bomb = "raise RuntimeError('boom')\n"
    chat = FakeModel({"A": [block(import_bomb)] * 2, "B": [block(import_bomb)] * 2})

    result = run(store, config, chat)

    assert result["structural_abort"] is True
    assert result["failure_reason"] == "structural"
    assert result["status"] == "failed"
    # 2 attempts on A, aborted on B's first: never B's second
    assert len(result["attempts"]) == 3
    assert result["attempts"][-1]["structural_abort"] is True


def test_a_real_test_run_breaks_the_structural_streak(store, config):
    """Bomb, bomb, then an honest red run, then a bomb again: four failures
    but never K identical abnormal exits running -- the ladder must climb."""
    import_bomb = "raise RuntimeError('boom')\n"
    chat = FakeModel({"A": [block(import_bomb), block(import_bomb)],
                      "B": [block(STILL_BROKEN), block(import_bomb)]})

    result = run(store, config, chat)

    assert result["structural_abort"] is False
    assert result["status"] == "failed"
    assert len(result["attempts"]) == 4


# ---- runner dispatch (vitest) ----

def test_a_vitest_project_routes_to_the_vitest_runner(store, tmp_path, monkeypatch):
    """factory.toml's runner key picks the judge: a runner="vitest" project must
    reach vitest_runner for materialize/run/cleanup, ask the model for a
    typescript block, and extract a ```typescript fence."""
    import loop_job
    from pathlib import Path
    from vitest_runner import Result

    root = tmp_path / "jsproj"
    (root / "src").mkdir(parents=True)
    git_init(root)
    (root / "src" / "add.ts").write_text("export const add = (a, b) => a - b;\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "seed")
    config = write_config(tmp_path, '[projects.js]\nrunner = "vitest"\n',
                          paths={"js": root}, name="factory_js.toml")

    class StubRunner:
        OPERATOR_ERROR_CODES = ()
        def __init__(self):
            self.materialized = self.cleaned = False
        def materialize_tests(self, tests_dir, tests, worktree, project_path=None):
            self.materialized = (Path(project_path) == root)
            Path(tests_dir).mkdir(parents=True, exist_ok=True)
            return Path(tests_dir)
        def run_tests(self, worktree, tests_dir, timeout=600):
            return Result(True, 1, 0, "Tests  1 passed (1)", False, 0)
        def run_regression(self, worktree, argv, timeout=900):
            raise AssertionError("no regression_cmd was configured")
        def cleanup(self, worktree, tests_dir):
            self.cleaned = True
    stub = StubRunner()
    monkeypatch.setitem(loop_job.RUNNERS, "vitest", stub)

    fixed = "export const add = (a: number, b: number) => a + b;\n"
    chat = FakeModel({"A": "```typescript\n{}```\n".format(fixed)})
    spec = {"project": "js", "goal": "add must add",
            "tests": {"add.test.ts": "import { test } from 'vitest';"},
            "target_files": ["src/add.ts"]}

    result = run(store, config, chat, spec=spec)

    assert result["status"] == "succeeded"
    assert stub.materialized and stub.cleaned
    assert "src/add.ts" in result["diff"] and "a + b" in result["diff"]
    # the prompt speaks the target's language
    assert "```typescript code block" in chat.prompts[0][1]


# ---- [models] profiles + generation cap ----

def test_ladder_passes_profile_options_and_num_predict(store, project, tmp_path):
    """A [models."qwen3-coder:30b"] profile in factory.toml must reach the model
    call as `options`, minus temperature (the stage's escalation temperature wins)
    and plus num_predict (the per-attempt generation cap)."""
    config = write_config(
        tmp_path,
        '[projects.demo]\nrunner = "pytest"\n'
        '[models."qwen3-coder:30b"]\ntemperature = 0.1\ntop_p = 0.8\n',
        paths={"demo": project}, name="factory_models.toml")
    chat = FakeModel({"qwen3-coder:30b": block(GOOD)})
    stage_temperature = 0.7

    result = run(store, config, chat,
                 ladder=[{"model": "qwen3-coder:30b", "temperature": stage_temperature,
                          "attempts": 1}])

    assert result["status"] == "succeeded"
    opts = chat.calls[0]["options"]
    assert opts["num_predict"] == loop_job.LADDER_NUM_PREDICT
    assert opts["top_p"] == 0.8
    assert "temperature" not in opts
    assert chat.calls[0]["temperature"] == stage_temperature


# ---- edit-mode routing and prompt (spec 2026-07-17) ----

import types  # noqa: E402

from loop_job import build_prompt, resolve_edit_mode  # noqa: E402


def wt(tmp_path):
    return types.SimpleNamespace(path=tmp_path)


def test_edit_mode_spec_override_wins(tmp_path):
    (tmp_path / "calc.py").write_text("x = 1\n")
    spec = {"target_files": ["calc.py"], "edit_mode": "diff"}
    assert resolve_edit_mode(spec, wt(tmp_path)) == "diff"
    many = {"target_files": ["a.py", "b.py"], "edit_mode": "whole"}
    assert resolve_edit_mode(many, wt(tmp_path)) == "whole"


def test_edit_mode_auto_multiple_targets_is_diff(tmp_path):
    spec = {"target_files": ["a.py", "b.py"]}
    assert resolve_edit_mode(spec, wt(tmp_path)) == "diff"


def test_edit_mode_auto_large_target_is_diff(tmp_path):
    (tmp_path / "big.py").write_text("x = 1\n" * 301)
    assert resolve_edit_mode({"target_files": ["big.py"]}, wt(tmp_path)) == "diff"
    (tmp_path / "small.py").write_text("x = 1\n" * 300)
    assert resolve_edit_mode({"target_files": ["small.py"]}, wt(tmp_path)) == "whole"


def test_edit_mode_auto_missing_single_target_is_whole(tmp_path):
    assert resolve_edit_mode({"target_files": ["new.py"]}, wt(tmp_path)) == "whole"


def test_build_prompt_diff_mode_swaps_the_instructions_only():
    current = {"calc.py": "def add(a, b):\n    return a - b"}
    p = build_prompt("fix add", current, {}, "", "python", edit_mode="diff")
    assert "<<<<<<< SEARCH" in p and ">>>>>>> REPLACE" in p
    assert "Do NOT rewrite whole files" in p
    assert "return a - b" in p            # current contents still shown
    assert "complete corrected contents" not in p


def test_build_prompt_whole_mode_is_unchanged():
    current = {"calc.py": "x = 1"}
    p = build_prompt("fix", current, {}, "", "python")
    assert "complete corrected contents" in p
    assert "SEARCH" not in p


SR_GOOD = ("### FILE: calc.py\n"
           "<<<<<<< SEARCH\n    return a - b\n=======\n    return a + b\n"
           ">>>>>>> REPLACE\n")
SR_BAD_ANCHOR = ("### FILE: calc.py\n"
                 "<<<<<<< SEARCH\n    return a % b\n=======\n    return a + b\n"
                 ">>>>>>> REPLACE\n")


def test_diff_mode_edit_goes_green(store, config):
    chat = FakeModel({"A": SR_GOOD})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["status"] == "succeeded"
    assert result["edit_mode"] == "diff"
    assert "<<<<<<< SEARCH" in chat.prompts[0][1]


def test_whole_mode_jobs_report_their_mode_too(store, config):
    chat = FakeModel({"A": block(GOOD)})
    result = run(store, config, chat)
    assert result["status"] == "succeeded"
    assert result["edit_mode"] == "whole"


def test_an_anchor_miss_burns_a_reminder_not_an_attempt(store, config):
    chat = FakeModel({"A": [SR_BAD_ANCHOR, SR_GOOD]})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["status"] == "succeeded"
    assert result["attempts"][0]["result"] == "AnchorNotFound"
    assert result["attempts"][0]["passed"] is False
    # the feedback carries the closest region, ready to copy
    assert "SEARCH text not found" in chat.prompts[1][1]
    assert "return a - b" in chat.prompts[1][1]


def test_two_anchor_misses_running_leave_the_rung(store, config):
    chat = FakeModel({"A": SR_BAD_ANCHOR, "B": SR_GOOD})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["status"] == "succeeded"
    assert result["escalated"] is True
    # A (capped sacrificial attempt, not a strike), A, A (two counted anchor
    # misses -> leave the rung), B fixes, B reviews the diff.
    assert [m for m, _ in chat.prompts] == ["A", "A", "A", "B", "B"]


# ---- diff-mode attempt economy (audit 2026-07-18: attempt 1 drowned at the
# 16k generation cap in 2/2 measured runs without applying an edit, and the
# 35b "thinking wall" was a num_ctx 16384 context wall).


def test_diff_mode_widens_num_ctx_for_generation_headroom(store, config):
    chat = FakeModel({"A": SR_GOOD})
    run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert chat.num_ctxs[0] == 32768


def test_whole_mode_keeps_the_default_num_ctx(store, config):
    chat = FakeModel({"A": block(GOOD)})
    run(store, config, chat)
    assert chat.num_ctxs[0] == 16384


def test_first_diff_attempt_runs_with_a_reduced_generation_cap(store, config):
    chat = FakeModel({"A": [SR_BAD_ANCHOR, SR_GOOD]})
    run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert chat.calls[0]["options"]["num_predict"] == loop_job.FIRST_DIFF_NUM_PREDICT
    assert chat.calls[1]["options"]["num_predict"] == loop_job.LADDER_NUM_PREDICT


def test_whole_mode_attempt_one_keeps_the_full_cap(store, config):
    chat = FakeModel({"A": block(GOOD)})
    run(store, config, chat)
    assert chat.calls[0]["options"]["num_predict"] == loop_job.LADDER_NUM_PREDICT


def test_diff_temperature_overrides_the_stage_temperature_in_diff_mode(store, config):
    ladder = [{"model": "A", "temperature": 0.7, "diff_temperature": 0.3,
               "attempts": 2}]
    chat = FakeModel({"A": SR_GOOD})
    run(store, config, chat, ladder=ladder, spec=dict(SPEC, edit_mode="diff"))
    assert chat.calls[0]["temperature"] == 0.3


def test_whole_mode_ignores_diff_temperature(store, config):
    ladder = [{"model": "A", "temperature": 0.7, "diff_temperature": 0.3,
               "attempts": 2}]
    chat = FakeModel({"A": block(GOOD)})
    run(store, config, chat, ladder=ladder)
    assert chat.calls[0]["temperature"] == 0.7


def test_the_capped_first_attempt_does_not_count_toward_leaving_the_rung(store, config):
    # Attempt 1 is sacrificial by design (reduced cap): its codeless failure
    # must not burn one of the rung's two codeless strikes.
    chat = FakeModel({"A": [{"content": "rambling, no code", "done_reason": "length"},
                            SR_BAD_ANCHOR, SR_GOOD]})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["status"] == "succeeded"
    assert result["escalated"] is False


# Two test FUNCTIONS (not one with two asserts): partial credit must be
# countable for best_attempt to have something to rank.
TEST_SPLIT = ("from calc import add\n\n"
              "def test_two():\n    assert add(2, 2) == 4\n\n"
              "def test_six():\n    assert add(1, 5) == 6\n")
PARTIAL = "def add(a, b):\n    return 4\n"  # test_two only


def test_a_failed_job_keeps_its_best_attempt_diff(store, config):
    # A scores 1/2 on both attempts; B never emits code. Without persistence
    # the escalation reset leaves an empty final diff and nothing else --
    # exactly the j_c32e89fd 6/8 loss.
    chat = FakeModel({"A": block(PARTIAL), "B": "no code here, sorry"})
    spec = dict(SPEC, tests={"test_calc.py": TEST_SPLIT})
    result = run(store, config, chat, spec=spec)
    assert result["status"] == "failed"
    assert result["diff"] == ""
    best = result["best_attempt"]
    assert best["model"] == "A" and best["tests_passed"] == 1
    assert "return 4" in best["diff"]
    assert best["failing"] == ["test_calc.py::test_six"]


# ---- llama-server backend selection (runtime switch 2026-07-18) ----


def test_a_llama_server_config_routes_the_default_chat_path(tmp_path, project, store, monkeypatch):
    config = write_config(
        tmp_path,
        '[projects.demo]\nrunner = "pytest"\n'
        '[llama_server]\nport = 8091\nctx = 32768\n',
        paths={"demo": project}, models={"A": "C:/a.gguf"}, exe="C:/l.exe",
        name="factory_ls.toml")

    created = {}

    class FakeManager:
        def __init__(self, cfg):
            created["cfg"] = cfg
            self.shutdowns = 0
            created["manager"] = self

        def chat(self, model, system, user, temperature=0.1, num_ctx=None, options=None):
            return {"content": block(GOOD), "tokens_per_s": 1.0,
                    "prefill_tokens_per_s": 1.0, "ttft_s": 0.1,
                    "done_reason": "stop", "eval_count": 5}

        def unload(self, model):
            pass

        def shutdown(self):
            self.shutdowns += 1

    monkeypatch.setattr(loop_job, "LlamaServerManager", FakeManager)
    store.create(SPEC, job_id="j_00000001")
    result = run_job("j_00000001", store.root, config, ladder=LADDER,
                     system="s", heartbeat_interval=0.05)

    assert result["status"] == "succeeded"
    assert created["cfg"]["port"] == 8091
    # The runner must never leak a 20 GB server process.
    assert created["manager"].shutdowns >= 1


def test_without_the_section_an_injected_chat_fn_still_wins(store, config):
    chat = FakeModel({"A": block(GOOD)})
    result = run(store, config, chat)
    assert result["status"] == "succeeded"


def test_without_the_section_and_without_a_chat_fn_the_run_errors(store, config):
    # Zero-Ollama: no [llama_server] section and no injected chat_fn leaves a
    # verdict, never a bare exception.
    store.create(SPEC, job_id="j_00000001")
    result = run_job("j_00000001", store.root, config, ladder=LADDER, system="s",
                     heartbeat_interval=0.05)
    assert result["status"] == "error"
    assert "llama_server" in result["error"]


# ---- raw reply persistence + anchor tiers (audit 2026-07-17: diagnosis
# stalled because no raw reply survived the run; anchor drift was invisible).


def test_every_raw_reply_is_persisted_as_attempt_txt(store, config):
    # Codeless replies especially: they are the ones diagnosis needs.
    chat = FakeModel({"A": ["no code at all", block(GOOD)]})
    run(store, config, chat)
    d = store.job_dir("j_00000001")
    assert (d / "attempt_1.txt").read_text(encoding="utf-8") == "no code at all"
    assert (d / "attempt_2.txt").read_text(encoding="utf-8") == block(GOOD)


SR_WS_DRIFT = ("### FILE: calc.py\n"
               "<<<<<<< SEARCH\n    return a - b  \n=======\n    return a + b\n"
               ">>>>>>> REPLACE\n")  # trailing spaces the file does not have


def test_the_anchor_tier_that_matched_is_recorded_on_the_attempt(store, config):
    chat = FakeModel({"A": SR_WS_DRIFT})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["status"] == "succeeded"
    assert result["attempts"][0]["anchor_tier"] == "whitespace"


def test_exact_anchors_report_the_exact_tier(store, config):
    chat = FakeModel({"A": SR_GOOD})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["attempts"][0]["anchor_tier"] == "exact"


def test_diff_prompt_caps_search_blocks_and_edit_count():
    # A 16k-token diff reply is a routed failure (j_d85cd85b attempt 2 ran to
    # the generation cap); the caps are stated up front.
    p = build_prompt("fix", {"calc.py": "x"}, {}, "", "python", edit_mode="diff")
    assert "15 lines" in p
    assert "8 edits" in p


def test_best_attempt_never_downgrades(store, config):
    # A: 1/2 then 1/2 again with different code -- the later, no-better
    # attempt must not replace the first capture.
    chat = FakeModel({"A": [block(PARTIAL), block(STILL_BROKEN)],
                      "B": "still no code"})
    spec = dict(SPEC, tests={"test_calc.py": TEST_SPLIT})
    result = run(store, config, chat, spec=spec)
    assert result["status"] == "failed"
    assert result["best_attempt"]["tests_passed"] == 1
    assert "return 4" in result["best_attempt"]["diff"]


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


def test_a_finished_job_records_what_it_taught(tmp_path):
    # The lesson source shipped with no caller, which is how incident() spent
    # months as dead code. Pin the wiring, not just the function.
    from chat_store import ChatStore
    outcome = {"success": False, "failure_reason": "no_code",
               "edit_mode": "diff", "final_model": "qwen2.5-coder:7b",
               "attempts": [{"attempt": 1, "stage": 1, "result": "NoCode"},
                            {"attempt": 2, "stage": 1, "result": "NoCode"}]}
    store = JobStore(tmp_path)
    job_id = store.create({"project": "p", "goal": "g", "tests": {},
                           "target_files": ["a.py"]})

    loop_job._record_lessons(tmp_path, outcome, store, job_id)

    known = ChatStore(tmp_path / "chats").load_lessons()
    assert [l["pattern"] for l in known] == ["no_code_in_diff"]
    assert known[0]["scope"] == {"model": "qwen2.5-coder:7b", "edit_mode": "diff"}


def test_lesson_recording_never_fails_a_job(tmp_path, capsys):
    store = JobStore(tmp_path)
    job_id = store.create({"project": "p", "goal": "g", "tests": {},
                           "target_files": ["a.py"]})
    # A malformed outcome must be swallowed: a green job is not lost to
    # bookkeeping.
    loop_job._record_lessons(tmp_path, {"attempts": [None]}, store, job_id)
    assert "lesson recording failed" in capsys.readouterr().err


# ---- anchor lost after a landed edit (root cause isolated 2026-07-25):
# the first attempt places its anchors and lands, then the model can no longer
# anchor in the file it just changed. It breaks 7b, 30b AND 35b the same way.

SR_LANDS_BUT_WRONG = ("### FILE: calc.py\n"
                      "<<<<<<< SEARCH\n    return a - b\n=======\n"
                      "    return a * b\n>>>>>>> REPLACE\n")


def test_an_anchor_miss_after_a_landed_edit_falls_back_to_whole_file(
        store, config):
    chat = FakeModel({"A": [SR_LANDS_BUT_WRONG, SR_BAD_ANCHOR, block(GOOD)]})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["status"] == "succeeded"
    assert "<<<<<<< SEARCH" in chat.prompts[0][1]     # asked for a diff
    assert "<<<<<<< SEARCH" in chat.prompts[1][1]     # still a diff
    assert "<<<<<<< SEARCH" not in chat.prompts[2][1]  # stopped asking
    assert "COMPLETE file" in chat.prompts[2][1]


def test_the_fallback_does_not_burn_a_strike(store, config):
    # Before: an anchor miss counted as a codeless reply, so a rung that had
    # just landed an edit was thrown off the rung for a reason that was not
    # its own. Two misses in a row must no longer leave the rung once the
    # fallback has taken over.
    chat = FakeModel({"A": [SR_LANDS_BUT_WRONG, SR_BAD_ANCHOR, block(GOOD)]})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["escalated"] is False
    assert [m for m, _ in chat.prompts][:3] == ["A", "A", "A"]


def test_an_anchor_miss_before_any_landed_edit_still_burns_a_reminder(
        store, config):
    # Unchanged behaviour: nothing landed, so the file is what the model was
    # shown, and copying the region really is the right feedback.
    chat = FakeModel({"A": [SR_BAD_ANCHOR, SR_GOOD]})
    result = run(store, config, chat, spec=dict(SPEC, edit_mode="diff"))
    assert result["status"] == "succeeded"
    assert result["attempts"][0]["result"] == "AnchorNotFound"
    assert "SEARCH text not found" in chat.prompts[1][1]
    assert "<<<<<<< SEARCH" in chat.prompts[1][1]     # still diff mode

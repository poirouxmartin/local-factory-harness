"""The guardrails that keep main out of the local agent's reach.

Session c_94deb1df (2026-07-20) committed and pushed to main by itself and
left it broken for two days. These hooks are versioned in `.githooks`
(`core.hooksPath`) rather than in an unversioned `.git/hooks`, so a test can
hold them to their contract and they cannot be lost silently.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / ".githooks"
sh = shutil.which("sh")
needs_sh = pytest.mark.skipif(not sh, reason="POSIX sh runs the hooks")


def test_the_hooks_are_versioned_in_the_repo():
    assert (HOOKS / "pre-commit").is_file()
    assert (HOOKS / "pre-push").is_file()


def _run_pre_commit(cwd, agent):
    env = {"PATH": __import__("os").environ["PATH"]}
    if agent:
        env["FACTORY_AGENT"] = "1"
    return subprocess.run([sh, str(HOOKS / "pre-commit")], cwd=str(cwd),
                          env=env, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)],
                   capture_output=True, check=True)
    return tmp_path


@needs_sh
def test_pre_commit_refuses_the_agent_on_main(repo):
    out = _run_pre_commit(repo, agent=True)
    assert out.returncode == 1
    assert "must not commit on main" in out.stderr
    assert "git switch -c agent/" in out.stderr  # the way out, spelled


@needs_sh
def test_pre_commit_lets_the_agent_work_on_its_own_branch(repo):
    subprocess.run(["git", "switch", "-c", "agent/20260722"], cwd=str(repo),
                   capture_output=True, check=True)
    assert _run_pre_commit(repo, agent=True).returncode == 0


@needs_sh
def test_pre_commit_never_blocks_the_operator(repo):
    # No marker = a human (or Claude) at the keyboard: main stays theirs.
    assert _run_pre_commit(repo, agent=False).returncode == 0


def test_pre_push_runs_the_suite():
    body = (HOOKS / "pre-push").read_text(encoding="utf-8")
    # 3.12 is the factory standard since 2026-07-28: it is the only interpreter
    # here that has playwright, and `pytest_runner` builds the judge command
    # from sys.executable -- so the gate must run the interpreter that judges.
    assert "py -3 -m pytest harness/tests" in body
    # A test needing a live browser or studio cannot gate a push: it hung the
    # suite at 14% for 10 minutes on 2026-07-28 before being killed.
    assert '-m "not browser"' in body
    assert "--no-verify" in body  # the escape hatch is documented in place

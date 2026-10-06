"""Disposable git worktrees. The livrable is a diff, never a commit.

A job runs on branch `factory/<job_id>` in a worktree that lives under the job
directory -- outside the project tree. Nothing is ever committed, so nothing can
be pushed, so nothing reaches master without a human or Opus reading the diff first.
"""
import re
import subprocess
from pathlib import Path

# Files that decide whether the job passed. A diff that touches one of these is
# a diff that rewrote its own judge.
_JUDGE = re.compile(
    r"(^|/)(test_.+\.py|.+_test\.py|conftest\.py|pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml)$")


class GitError(RuntimeError):
    pass


def _git(repo, *args, check=True):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise GitError("git {}: {}".format(" ".join(args), (p.stderr or p.stdout).strip()))
    return p.stdout


def _branch_worktree(repo, branch):
    """Path of the worktree that has `branch` checked out, or None.

    This is what tells a stale branch (nothing holds it) from a live one
    (another run does) -- the difference between reclaiming a name and
    stepping on a running job.
    """
    current = None
    for line in _git(repo, "worktree", "list", "--porcelain",
                     check=False).splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):].strip()
        elif line.strip() == "branch refs/heads/{}".format(branch):
            return current
    return None


def forbidden_paths(paths):
    return [p for p in paths if _JUDGE.search(p.replace("\\", "/"))]


def head_blob_size(repo, relpath):
    """Byte size of `relpath` at HEAD, or None if it is not there."""
    out = _git(repo, "cat-file", "-s", "HEAD:{}".format(relpath), check=False).strip()
    return int(out) if out.isdigit() else None


class WorkTree:
    def __init__(self, repo, job_id, path, branch, base_sha):
        self.repo = Path(repo)
        self.job_id = job_id
        self.path = Path(path)
        self.branch = branch
        self.base_sha = base_sha

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
        try:
            _git(repo, "worktree", "add", "-q", "-b", branch, str(path), base_sha)
        except GitError:
            # The branch name is already taken. Two very different causes, and
            # dying on both cost a 12-job corpus replay two cells on 2026-07-28
            # -- scored as model failures, in 0.1 s, in the denominator.
            held = _branch_worktree(repo, branch)
            if held:
                raise GitError(
                    "{} is checked out in {}: another run holds this job id"
                    .format(branch, held))
            # Nothing holds it, so it is the scratch branch of a run that was
            # killed: garbage by definition, `remove()` would have deleted it.
            _git(repo, "worktree", "prune", check=False)
            _git(repo, "branch", "-D", branch, check=False)
            _git(repo, "worktree", "add", "-q", "-b", branch, str(path), base_sha)
        return cls(repo, job_id, path, branch, base_sha)

    def _stage(self):
        # Without this, `git diff` is blind to files the model created.
        _git(self.path, "add", "-A")

    def capture_diff(self):
        self._stage()
        return _git(self.path, "diff", "--cached")

    def changed_paths(self):
        self._stage()
        out = _git(self.path, "diff", "--cached", "--name-only")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def reset(self):
        """Back to the base commit: clean slate between escalation rungs (ADR-009)."""
        _git(self.path, "reset", "-q", "--hard", self.base_sha)
        _git(self.path, "clean", "-qfd")

    def restore_judge(self):
        """Put the project's own tests back before we let them grade anything.

        These tests necessarily live inside the tree, so the model can reach them.
        Restoring the tracked ones is obvious; deleting the *added* ones is the half
        that matters, because `git checkout --` never removes a file the model created.
        Only judge paths are touched, so a legitimate new module survives.

        The report lists only files that actually differed from HEAD: a non-empty
        report rejects the job (ADR-015), and every project with tests has tracked
        judge files that were never touched.
        """
        tracked = [p for p in _git(self.path, "ls-files").splitlines()
                   if p and _JUDGE.search(p)]
        restored = []
        if tracked:
            restored = [p for p in _git(self.path, "diff", "--name-only", "HEAD",
                                        "--", *tracked).splitlines() if p]
        if restored:
            _git(self.path, "checkout", "--", *restored)
        untracked = [p for p in _git(self.path, "ls-files", "--others",
                                     "--exclude-standard").splitlines()
                     if p and _JUDGE.search(p)]
        for relpath in untracked:
            (self.path / relpath).unlink()
        return {"restored": restored, "deleted": untracked}

    def remove(self):
        _git(self.repo, "worktree", "remove", "--force", str(self.path), check=False)
        _git(self.repo, "worktree", "prune", check=False)
        _git(self.repo, "branch", "-D", self.branch, check=False)

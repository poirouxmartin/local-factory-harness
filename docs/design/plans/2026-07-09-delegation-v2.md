# Delegation v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the factory delegate real, multi-module work on itself without a green job being able to break the project's existing test suite.

**Architecture:** Two additions to the existing harness. A spec gains `context_files`, read-only files injected into the model's prompt (read-only by construction: `extract_files` already refuses to write any path outside `target_files`). A project gains `regression_cmd` in `factory.toml`, a suite that runs inside the worktree — only once the spec's own tests are green — and that must also pass before a job is called `succeeded`. Judge files inside the worktree are restored, and planted ones deleted, immediately before each regression run.

**Tech Stack:** Python 3.9, stdlib only plus `tomli` and `pytest`. Windows. Ollama for the model calls.

**Spec:** `docs/design/specs/2026-07-09-delegation-v2-context-and-regression-gate-design.md`

## Global Constraints

- Python **3.9** — no `tomllib` (use the existing `tomli` fallback), no `match`, no PEP 604 unions.
- Stdlib only in `harness/` except `tomli` (config) and `pytest` (tests). `ollama_client.py` is urllib-based; keep it that way.
- Windows: `os.replace` retries on `PermissionError`; `flock` does not exist. Never introduce a blocking file lock.
- Harness modules import each other by **bare name** (`from worktree import ...`). `harness/tests/conftest.py` puts `harness/` on `sys.path`.
- All tests live in `harness/tests/`. Run everything with `python -m pytest harness/tests -q` from the repo root (`C:\Users\me\local-factory`).
- TDD: write the failing test, watch it fail, minimal implementation, watch it pass, commit.
- Commits: conventional commits, imperative, English, ASCII. **Never** include AI or Claude attribution, or "Generated with" trailers.
- `CONTEXT_BUDGET = 60000` bytes. `REGRESSION_TIMEOUT = 900` seconds.
- The model may never write a test. `validate_targets` and the final `forbidden_paths` gate stay exactly as they are.
- **Prompt ordering is a KV-cache contract.** `build_prompt` places stable parts
  first (goal, instructions, reference files) and volatile parts last (current
  targets, failure output). The server reuses the prefix KV cache across a run's
  attempts only if the prefix stays byte-identical: never reorder these sections,
  never interleave `last_output` above the reference files.

## Post-audit review — 2026-07-10 (docs/audit-synthesis-2026-07.md)

Reviewed against the five local-LLM audits before execution; the design holds
(targeted read-only context is exactly the audits' context-hygiene rule). Three
adjustments, all now folded into the tasks below:

1. **Budget vs num_ctx mismatch → Task 4.** `run_job` defaults to
   `num_ctx=16384` for every ladder stage (`loop_job.py:182`), but
   `CONTEXT_BUDGET = 60000` bytes is ~17–19k tokens of reference material alone —
   a max-budget job silently truncates, which is precisely the ADR-008 bug the
   budget claims to prevent. Task 4 raises the ladder to 32768 (every roster
   model supports it) when `context_files` is non-empty.
2. **Prompt ordering** → promoted to a Global Constraint above.
3. **Prefill metrics first → Task 0.** Context injection multiplies prefill
   cost; without prefill tok/s and TTFT we cannot see what `context_files`
   costs per attempt.

---

### Task 0: The client measures prefill, not just generation

**Files:**
- Modify: `harness/ollama_client.py`
- Modify: `harness/loop_job.py` (attempt record only)
- Test: `harness/tests/test_ollama_client.py` (new)
- Test: `harness/tests/test_loop_job.py`

**Interfaces:**
- Consumes: the Ollama `/api/chat` response body, which already carries
  `prompt_eval_count`, `prompt_eval_duration`, `load_duration`.
- Produces: `_metrics(body, elapsed) -> dict` — a pure function extracted from
  `chat()` so the parsing is testable without a server. The dict gains
  `prefill_count`, `prefill_tokens_per_s`, `ttft_s` alongside the existing keys.
  Each attempt record in `loop_job` gains `prefill_tokens_per_s` and `ttft_s`.

- [ ] **Step 1: Write the failing tests**

Create `harness/tests/test_ollama_client.py`:

```python
from ollama_client import _metrics


def test_generation_speed_is_tokens_over_seconds():
    body = {"message": {"content": "hi"},
            "eval_count": 100, "eval_duration": 2_000_000_000}

    m = _metrics(body, elapsed=2.5)

    assert m["content"] == "hi"
    assert m["eval_count"] == 100
    assert m["tokens_per_s"] == 50.0
    assert m["elapsed"] == 2.5


def test_prefill_speed_and_ttft_come_from_the_prompt_eval_fields():
    body = {"prompt_eval_count": 4000, "prompt_eval_duration": 4_000_000_000,
            "load_duration": 1_000_000_000}

    m = _metrics(body, elapsed=6.0)

    assert m["prefill_count"] == 4000
    assert m["prefill_tokens_per_s"] == 1000.0
    assert m["ttft_s"] == 5.0


def test_missing_fields_do_not_divide_by_zero():
    m = _metrics({}, elapsed=0.1)

    assert m["tokens_per_s"] == 0.0
    assert m["prefill_tokens_per_s"] == 0.0
    assert m["ttft_s"] == 0.0
```

In `harness/tests/test_loop_job.py`, extend `FakeModel.__call__`'s returned dict
with `"prefill_tokens_per_s": 7.0, "ttft_s": 1.5` (existing tests read none of
these keys), then append:

```python
def test_an_attempt_records_the_prefill_cost(store, config):
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config, chat)

    assert result["attempts"][0]["prefill_tokens_per_s"] == 7.0
    assert result["attempts"][0]["ttft_s"] == 1.5
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest harness/tests/test_ollama_client.py harness/tests/test_loop_job.py -q`
Expected: FAIL — `ImportError: cannot import name '_metrics'`, then
`KeyError: 'prefill_tokens_per_s'`.

- [ ] **Step 3: Implement**

In `harness/ollama_client.py`, extract the parsing from `chat()` into:

```python
def _metrics(body, elapsed):
    """Pure parse of an /api/chat response. Durations are nanoseconds.

    Prefill is what an agent workload is mostly made of (docs/
    audit-synthesis-2026-07.md); generation speed alone hides it.
    """
    eval_count = body.get("eval_count", 0) or 0
    eval_dur = body.get("eval_duration", 0) or 0
    prefill_count = body.get("prompt_eval_count", 0) or 0
    prefill_dur = body.get("prompt_eval_duration", 0) or 0
    load_dur = body.get("load_duration", 0) or 0
    return {
        "content": body.get("message", {}).get("content", ""),
        "elapsed": elapsed,
        "eval_count": eval_count,
        "tokens_per_s": (eval_count / (eval_dur / 1e9)) if eval_dur else 0.0,
        "prefill_count": prefill_count,
        "prefill_tokens_per_s": (prefill_count / (prefill_dur / 1e9)) if prefill_dur else 0.0,
        "ttft_s": (load_dur + prefill_dur) / 1e9,
    }
```

and end `chat()` with `return _metrics(body, elapsed)` in place of the current
parsing block.

In `harness/loop_job.py`, widen the attempt record in `_run_ladder`:

```python
            rec = {"attempt": len(attempts) + 1, "stage": si + 1, "model": model,
                   "tokens_per_s": round(resp.get("tokens_per_s", 0.0), 1),
                   "prefill_tokens_per_s": round(resp.get("prefill_tokens_per_s", 0.0), 1),
                   "ttft_s": round(resp.get("ttft_s", 0.0), 2)}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest harness/tests -q`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add harness/ollama_client.py harness/loop_job.py harness/tests/test_ollama_client.py harness/tests/test_loop_job.py
git commit -m "feat: measure prefill speed and time-to-first-token per attempt"
```

---

---

### Task 1: Worktree learns to restore its judge and to size a blob

**Files:**
- Modify: `harness/worktree.py`
- Test: `harness/tests/test_worktree.py`

**Interfaces:**
- Consumes: `_git(repo, *args, check=True)`, `_JUDGE`, `WorkTree.path` (all already in `worktree.py`).
- Produces:
  - `WorkTree.restore_judge() -> dict` with keys `restored: list[str]` and `deleted: list[str]`, repo-relative POSIX paths.
  - `head_blob_size(repo, relpath) -> int | None` — byte size of `HEAD:<relpath>`, or `None` if the path is not in `HEAD`.

- [ ] **Step 1: Write the failing tests**

Append to `harness/tests/test_worktree.py`:

```python
def test_restore_judge_reverts_a_modified_test_file(repo, tmp_path):
    (repo / "test_calc.py").write_text("def test_real():\n    assert False\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add tests")
    wt = WorkTree.create(repo, "j_j1", tmp_path / "wt_j1")
    try:
        (wt.path / "test_calc.py").write_text("def test_real():\n    pass\n")

        wt.restore_judge()

        assert (wt.path / "test_calc.py").read_text() == "def test_real():\n    assert False\n"
    finally:
        wt.remove()


def test_restore_judge_deletes_a_planted_conftest(tree):
    """`git checkout --` never removes an added file. This is the half that matters."""
    (tree.path / "conftest.py").write_text("import calc\ncalc.add = lambda a, b: a + b\n")

    report = tree.restore_judge()

    assert not (tree.path / "conftest.py").exists()
    assert report["deleted"] == ["conftest.py"]


def test_restore_judge_deletes_a_planted_test_in_a_subdirectory(tree):
    nested = tree.path / "harness" / "tests"
    nested.mkdir(parents=True)
    (nested / "conftest.py").write_text("# planted\n")

    tree.restore_judge()

    assert not (nested / "conftest.py").exists()


def test_restore_judge_leaves_a_legitimate_new_module_alone(tree):
    (tree.path / "elo.py").write_text("K = 32\n")

    tree.restore_judge()

    assert (tree.path / "elo.py").read_text() == "K = 32\n"


def test_restore_judge_leaves_the_models_source_edits_alone(tree):
    (tree.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    tree.restore_judge()

    assert (tree.path / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_restore_judge_is_a_no_op_on_a_clean_tree(tree):
    report = tree.restore_judge()

    assert report == {"restored": [], "deleted": []}


def test_head_blob_size_returns_the_byte_size_of_a_tracked_file(repo):
    from worktree import head_blob_size

    assert head_blob_size(repo, "calc.py") == len("def add(a, b):\n    return a - b\n")


def test_head_blob_size_is_none_for_a_path_absent_from_head(repo):
    from worktree import head_blob_size

    assert head_blob_size(repo, "does_not_exist.py") is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest harness/tests/test_worktree.py -q`
Expected: FAIL — `AttributeError: 'WorkTree' object has no attribute 'restore_judge'` and `ImportError: cannot import name 'head_blob_size'`.

- [ ] **Step 3: Implement**

In `harness/worktree.py`, add after `forbidden_paths`:

```python
def head_blob_size(repo, relpath):
    """Byte size of `relpath` at HEAD, or None if it is not there."""
    out = _git(repo, "cat-file", "-s", "HEAD:{}".format(relpath), check=False).strip()
    return int(out) if out.isdigit() else None
```

And add as a method on `WorkTree`, after `reset`:

```python
    def restore_judge(self):
        """Put the project's own tests back before we let them grade anything.

        These tests necessarily live inside the tree, so the model can reach them.
        Restoring the tracked ones is obvious; deleting the *added* ones is the half
        that matters, because `git checkout --` never removes a file the model created.
        Only judge paths are touched, so a legitimate new module survives.
        """
        tracked = [p for p in _git(self.path, "ls-files").splitlines()
                   if p and _JUDGE.search(p)]
        if tracked:
            _git(self.path, "checkout", "--", *tracked)
        untracked = [p for p in _git(self.path, "ls-files", "--others",
                                     "--exclude-standard").splitlines()
                     if p and _JUDGE.search(p)]
        for relpath in untracked:
            (self.path / relpath).unlink()
        return {"restored": tracked, "deleted": untracked}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest harness/tests/test_worktree.py -q`
Expected: PASS, 31 passed.

- [ ] **Step 5: Commit**

```bash
git add harness/worktree.py harness/tests/test_worktree.py
git commit -m "feat: restore the worktree's own tests before they grade anything"
```

---

### Task 2: The config validates context files and carries a regression command

**Files:**
- Modify: `harness/factory_config.py`
- Test: `harness/tests/test_factory_config.py`

**Interfaces:**
- Consumes: `head_blob_size(repo, relpath)` and `forbidden_paths(paths)` from Task 1 / `worktree.py`; `is_unsafe_path(relpath)`, `Project`, `resolve_project` (already present).
- Produces:
  - `Project` namedtuple is now `"name path runner regression_cmd"`; `regression_cmd` is a `list[str]` or `None`.
  - `ContextTooLarge(ConfigError)`.
  - `CONTEXT_BUDGET = 60000`.
  - `validate_context_files(project, context_files, target_files) -> None`, raising `UnsafePath` or `ContextTooLarge`.

- [ ] **Step 1: Write the failing tests**

In `harness/tests/test_factory_config.py`, extend the import block and the `TOML` fixture, then append the tests.

Replace the import block with:

```python
from factory_config import (
    ContextTooLarge,
    NoTargets,
    NoTests,
    ProjectNotAllowed,
    UnsafePath,
    UnsupportedRunner,
    load_projects,
    resolve_project,
    validate_context_files,
    validate_targets,
    validate_tests,
)
```

Replace the `TOML` constant with:

```python
TOML = """
[projects.local-factory]
path = "{root}"
runner = "pytest"
regression_cmd = ["-m", "pytest", "harness/tests", "-q"]

[projects.plain]
path = "{root}"
runner = "pytest"

[projects.ghost]
path = "{root}/does-not-exist"
runner = "pytest"

[projects.webapp]
path = "{root}"
runner = "vitest"
"""
```

Replace the `config` fixture so the repo has a real commit (`head_blob_size` reads `HEAD`):

```python
@pytest.fixture
def config(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "f@f.f"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "factory"], check=True)
    (root / "calc.py").write_text("x = 1\n")
    (root / "elo.py").write_text("K = 32\n")
    (root / "test_calc.py").write_text("def test_x(): pass\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)
    cfg_path = tmp_path / "factory.toml"
    cfg_path.write_text(TOML.format(root=root.as_posix()), encoding="utf-8")
    return load_projects(cfg_path)
```

Add `import subprocess` at the top of the file. Then append:

```python
def test_a_project_carries_its_regression_command(config):
    project = resolve_project(config, "local-factory")

    assert project.regression_cmd == ["-m", "pytest", "harness/tests", "-q"]


def test_a_project_without_a_regression_command_has_none(config):
    assert resolve_project(config, "plain").regression_cmd is None


def test_context_files_from_head_are_accepted(config):
    project = resolve_project(config, "local-factory")

    validate_context_files(project, ["elo.py"], ["calc.py"])


def test_no_context_files_is_fine(config):
    project = resolve_project(config, "local-factory")

    validate_context_files(project, [], ["calc.py"])
    validate_context_files(project, None, ["calc.py"])


def test_a_context_file_absent_from_head_is_refused(config):
    project = resolve_project(config, "local-factory")

    with pytest.raises(UnsafePath):
        validate_context_files(project, ["typo.py"], ["calc.py"])


def test_a_context_file_that_is_also_a_target_is_refused(config):
    """A file is readable or writable, not both."""
    project = resolve_project(config, "local-factory")

    with pytest.raises(UnsafePath):
        validate_context_files(project, ["calc.py"], ["calc.py"])


def test_a_judge_file_is_not_reference_material(config):
    project = resolve_project(config, "local-factory")

    with pytest.raises(UnsafePath):
        validate_context_files(project, ["test_calc.py"], ["calc.py"])


@pytest.mark.parametrize("bad", ["../escape.py", "/etc/passwd", "C:/Windows/evil.py"])
def test_a_context_path_escaping_the_repo_is_refused(config, bad):
    project = resolve_project(config, "local-factory")

    with pytest.raises(UnsafePath):
        validate_context_files(project, [bad], ["calc.py"])


def test_context_over_the_budget_is_refused(config, tmp_path):
    project = resolve_project(config, "local-factory")
    big = tmp_path / "repo" / "big.py"
    big.write_text("x = 1\n" * 20000)
    subprocess.run(["git", "-C", str(project.path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(project.path), "commit", "-qm", "big"], check=True)

    with pytest.raises(ContextTooLarge):
        validate_context_files(project, ["big.py"], ["calc.py"])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest harness/tests/test_factory_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'ContextTooLarge'`.

- [ ] **Step 3: Implement**

In `harness/factory_config.py`:

Change the import line to bring in the sizer:

```python
from worktree import forbidden_paths, head_blob_size
```

Add the budget next to the other constants:

```python
# The 7b runs at num_ctx 16384. Silently overflowing it reproduces ADR-008's
# truncation bug, so we refuse the delegation instead.
CONTEXT_BUDGET = 60000
```

Widen the namedtuple:

```python
Project = namedtuple("Project", "name path runner regression_cmd")
```

Add the exception next to `NoTargets`:

```python
class ContextTooLarge(ConfigError):
    pass
```

In `resolve_project`, change the return line to:

```python
    return Project(name, path, runner, entry.get("regression_cmd"))
```

Add at the end of the module:

```python
def validate_context_files(project, context_files, target_files):
    """Reference files are read-only by construction; here we only check they are sane."""
    total = 0
    for relpath in context_files or []:
        if is_unsafe_path(relpath):
            raise UnsafePath("context path escapes the repo: {!r}".format(relpath))
        if forbidden_paths([relpath]):
            raise UnsafePath("a judge file is not reference material: {!r}".format(relpath))
        if relpath in target_files:
            raise UnsafePath("{!r} cannot be both reference and target".format(relpath))
        size = head_blob_size(project.path, relpath)
        if size is None:
            raise UnsafePath("{!r} is not in HEAD of {}".format(relpath, project.name))
        total += size
    if total > CONTEXT_BUDGET:
        raise ContextTooLarge("context is {} bytes, budget is {}".format(total, CONTEXT_BUDGET))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest harness/tests/test_factory_config.py -q`
Expected: PASS.

Then run the whole suite, because `Project` grew a field:
Run: `python -m pytest harness/tests -q`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add harness/factory_config.py harness/tests/test_factory_config.py
git commit -m "feat: validate context files and carry a per-project regression command"
```

---

### Task 3: The runner can execute a project's own suite

**Files:**
- Modify: `harness/pytest_runner.py`
- Test: `harness/tests/test_pytest_runner.py`

**Interfaces:**
- Consumes: `Result` namedtuple and `_counts(out)` (already in `pytest_runner.py`).
- Produces: `Result` gains a sixth field, `returncode` (`int`, `-1` on timeout). Both `run_tests` and `run_regression` set it. Existing tests read `Result` by attribute, so nothing else changes.
- Produces: `run_regression(worktree, argv, timeout=REGRESSION_TIMEOUT) -> Result`. `argv` is the list passed *after* the interpreter, e.g. `["-m", "pytest", "harness/tests", "-q"]`. `cwd` is the worktree. `passed` is `returncode == 0`.
- Produces: `REGRESSION_TIMEOUT = 900` and `OPERATOR_ERROR_CODES = (3, 4, 5)` — pytest's internal-error, usage-error and nothing-collected exits. These mean the *command* is wrong, not the code.

- [ ] **Step 1: Write the failing tests**

Append to `harness/tests/test_pytest_runner.py`:

```python
def test_run_regression_passes_on_a_healthy_project_suite(tmp_path):
    from pytest_runner import run_regression

    worktree = tmp_path / "wt"
    (worktree / "suite").mkdir(parents=True)
    (worktree / "suite" / "test_ok.py").write_text("def test_ok():\n    assert True\n")

    result = run_regression(worktree, ["-m", "pytest", "suite", "-q"])

    assert result.passed is True
    assert result.tests_passed == 1


def test_run_regression_fails_when_the_project_suite_is_red(tmp_path):
    from pytest_runner import run_regression

    worktree = tmp_path / "wt"
    (worktree / "suite").mkdir(parents=True)
    (worktree / "suite" / "test_ko.py").write_text("def test_ko():\n    assert False\n")

    result = run_regression(worktree, ["-m", "pytest", "suite", "-q"])

    assert result.passed is False
    assert result.tests_failed == 1
    assert "test_ko" in result.output


def test_run_regression_runs_inside_the_worktree(tmp_path):
    """The suite must grade the worktree's code, not the harness that spawned it."""
    from pytest_runner import run_regression

    worktree = tmp_path / "wt"
    (worktree / "suite").mkdir(parents=True)
    (worktree / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (worktree / "suite" / "test_calc.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path.cwd()))\n"
        "from calc import add\n\n"
        "def test_add():\n    assert add(2, 2) == 4\n")

    assert run_regression(worktree, ["-m", "pytest", "suite", "-q"]).passed is True


def test_run_regression_that_hangs_is_killed(tmp_path):
    from pytest_runner import run_regression

    worktree = tmp_path / "wt"
    (worktree / "suite").mkdir(parents=True)
    (worktree / "suite" / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(60)\n")

    result = run_regression(worktree, ["-m", "pytest", "suite", "-q"], timeout=3)

    assert result.passed is False
    assert result.timed_out is True


def test_run_regression_surfaces_the_exit_code_of_a_misconfigured_command(tmp_path):
    """A typo'd regression_cmd must not look like a model that broke the project."""
    from pytest_runner import OPERATOR_ERROR_CODES, run_regression

    worktree = tmp_path / "wt"
    worktree.mkdir()

    result = run_regression(worktree, ["-m", "pytest", "no_such_dir", "-q"])

    assert result.passed is False
    assert result.returncode in OPERATOR_ERROR_CODES


def test_run_tests_also_reports_its_exit_code(sandbox):
    worktree, tests_dir = sandbox
    (worktree / "calc.py").write_text(GOOD)

    assert run_tests(worktree, tests_dir).returncode == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest harness/tests/test_pytest_runner.py -q`
Expected: FAIL — `ImportError: cannot import name 'run_regression'`.

- [ ] **Step 3: Implement**

In `harness/pytest_runner.py`, add the constants next to `DEFAULT_TIMEOUT`:

```python
REGRESSION_TIMEOUT = 900  # the project's own suite; 143 tests take ~22 s today

# pytest: 3 internal error, 4 usage error, 5 nothing collected. All three mean the
# command is wrong, not the code. A model must never be asked to fix a typo in
# factory.toml.
OPERATOR_ERROR_CODES = (3, 4, 5)
```

Widen `Result` so callers can tell those apart:

```python
Result = namedtuple("Result", "passed tests_passed tests_failed output timed_out returncode")
```

and update the two `Result(...)` constructions inside `run_tests` to pass it —
`Result(False, 0, 0, out + "...", True, -1)` on timeout, and
`Result(passed, npassed, nfailed, out, False, p.returncode)` on the normal path.

Extract the environment so both runners share it. Replace the two lines inside `run_tests` that build `env` with a call to a helper, and add the helper above `run_tests`:

```python
def _clean_env():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env
```

so `run_tests` now reads `env = _clean_env()`.

Then append:

```python
def run_regression(worktree, argv, timeout=REGRESSION_TIMEOUT):
    """Run the project's own test suite inside the worktree.

    Unlike the spec's tests, these live in the tree -- the model can reach them.
    Call WorkTree.restore_judge() immediately before this, every time.
    """
    cmd = [sys.executable, *argv]
    try:
        p = subprocess.run(cmd, cwd=str(worktree), env=_clean_env(), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + "\n" + (e.stderr or "")
        return Result(False, 0, 0, out + "\nTIMEOUT after {}s".format(timeout), True, -1)

    out = (p.stdout or "") + "\n" + (p.stderr or "")
    npassed, nfailed = _counts(out)
    return Result(p.returncode == 0, npassed, nfailed, out, False, p.returncode)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest harness/tests/test_pytest_runner.py -q`
Expected: PASS, 14 passed.

- [ ] **Step 5: Commit**

```bash
git add harness/pytest_runner.py harness/tests/test_pytest_runner.py
git commit -m "feat: run a project's own suite inside the worktree"
```

---

### Task 4: The prompt carries read-only reference files

**Files:**
- Modify: `harness/loop_job.py`
- Test: `harness/tests/test_loop_job.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `build_prompt(goal, current, context, last_output)` — `context` is a `dict` of `path -> contents`, rendered before the target files. `_run_ladder` reads `spec.get("context_files", [])` from the worktree.
- Produces: when `context` is non-empty, `_run_ladder` raises the ladder's window to `max(num_ctx, 32768)` — a max-budget context (60 000 bytes ≈ 17–19k tokens) silently truncates at the 16384 default, reproducing ADR-008. `FakeModel` records the `num_ctx` it was called with.

- [ ] **Step 1: Write the failing tests**

Append to `harness/tests/test_loop_job.py`:

```python
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
```

For the last two tests, `FakeModel` must record the window it was called with.
In its `__init__` add `self.num_ctxs = []`, and in `__call__` add
`self.num_ctxs.append(num_ctx)`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest harness/tests/test_loop_job.py -q -k "reference or context"`
Expected: FAIL — the first with `AssertionError: assert 'K = 32' in prompt`.

- [ ] **Step 3: Implement**

In `harness/loop_job.py`, replace `build_prompt` with:

```python
def build_prompt(goal, current, context, last_output):
    parts = ["TASK: {}".format(goal), ""]
    if len(current) == 1:
        only = next(iter(current))
        parts.append("Fix the file `{}`. Return ONLY the complete corrected contents "
                     "of `{}` in a single ```python code block. No explanation.".format(only, only))
    else:
        parts.append("Change any of these files: {}. For each file you change, emit a "
                     "`### FILE: <path>` line followed by its complete new contents in a "
                     "```python code block. No explanation.".format(", ".join(sorted(current))))
    if context:
        parts += ["", "Reference files (read-only -- do NOT rewrite them):"]
        for path, content in sorted(context.items()):
            parts += ["", "`{}`:".format(path), "```python", content.strip(), "```"]
    for path, content in sorted(current.items()):
        parts += ["", "Current `{}`:".format(path), "```python", content.strip(), "```"]
    if last_output:
        parts += ["", "The latest pytest run FAILED. Fix these failures:",
                  "```", last_output.strip(), "```"]
    return "\n".join(parts)
```

In `_run_ladder`, read the reference files once before the stage loop, right after `goal`:

```python
    targets = spec["target_files"]
    goal = spec.get("goal", "")
    context = {}
    for path in spec.get("context_files", []) or []:
        context[path] = (worktree.path / path).read_text(encoding="utf-8")
    if context:
        # A max-budget context (~17-19k tokens) overflows the 16384 default and
        # truncates silently (ADR-008). Every roster model supports 32k.
        num_ctx = max(num_ctx, 32768)
    attempts, last_output, success = [], "", False
```

and change the `chat_fn` call to pass it:

```python
            resp = chat_fn(model, system, build_prompt(goal, current, context, last_output),
                           temperature=stage.get("temperature", 0.1), num_ctx=num_ctx)
```

Add `validate_context_files` to the import from `factory_config`, and call it in `_execute` next to the other validations:

```python
    validate_targets(spec["target_files"])
    validate_context_files(project, spec.get("context_files"), spec["target_files"])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest harness/tests/test_loop_job.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/loop_job.py harness/tests/test_loop_job.py
git commit -m "feat: give the model read-only reference files in its prompt"
```

---

### Task 5: A green spec no longer means a green job

**Files:**
- Modify: `harness/loop_job.py`
- Test: `harness/tests/test_loop_job.py`

**Interfaces:**
- Consumes: `WorkTree.restore_judge()` (Task 1), `run_regression`, `REGRESSION_TIMEOUT`, `OPERATOR_ERROR_CODES` (Task 3), `Project.regression_cmd` (Task 2).
- Produces: `result.json` gains `regression_passed` (`True` / `False` / `None`), `regression_output` (last 3000 chars, `""` when never run) and `failure_reason` (`"spec_tests"` / `"regression"` / `"no_code"` / `"gpu_timeout"` / `None`).
- Produces: `RegressionCommandError(RuntimeError)` in `loop_job.py`, raised when the regression command exits with an operator-error code. It escapes `_execute`, is caught by `run_job`'s blanket handler, and lands as `status: "error"`.

- [ ] **Step 1: Write the failing tests**

In `harness/tests/test_loop_job.py`, add a config fixture that declares a regression command, and the tests.

The project fixture must gain a suite of its own. Replace the `project` fixture with:

```python
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
```

Add a second config fixture beside the existing one:

```python
@pytest.fixture
def config_with_regression(tmp_path, project):
    path = tmp_path / "factory_reg.toml"
    path.write_text(
        '[projects.demo]\npath = "{}"\nrunner = "pytest"\n'
        'regression_cmd = ["-m", "pytest", "suite", "-q"]\n'.format(project.as_posix()))
    return path
```

Then append the tests:

```python
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


def test_a_planted_judge_file_is_gone_before_the_regression_runs(store, config_with_regression):
    """The model's code plants a conftest at import time. restore_judge deletes it,
    the project suite runs honestly, and the diff gate rejects the job anyway."""
    sneaky = (
        "import pathlib\n"
        "pathlib.Path(__file__).with_name('conftest.py').write_text('# planted\\n')\n"
        "def add(a, b):\n    return a + b\n"
    )
    chat = FakeModel({"A": block(sneaky)})

    result = run(store, config_with_regression, chat)

    assert result["status"] == "rejected"
    assert result["forbidden_paths"] == ["conftest.py"]


def test_a_project_without_a_regression_command_still_succeeds(store, config):
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, config, chat)

    assert result["status"] == "succeeded"
    assert result["regression_passed"] is None


def test_a_model_that_only_refuses_reports_no_code(store, config):
    chat = FakeModel({"A": "no thanks", "B": "still no"})

    result = run(store, config, chat)

    assert result["failure_reason"] == "no_code"


def test_a_misconfigured_regression_command_is_an_error_not_a_model_failure(
        store, tmp_path, project):
    """A typo in factory.toml must never be handed to the model as something to fix."""
    cfg = tmp_path / "factory_typo.toml"
    cfg.write_text(
        '[projects.demo]\npath = "{}"\nrunner = "pytest"\n'
        'regression_cmd = ["-m", "pytest", "no_such_dir", "-q"]\n'.format(project.as_posix()))
    chat = FakeModel({"A": block(GOOD)})

    result = run(store, cfg, chat)

    assert result["status"] == "error"
    assert "RegressionCommandError" in result["error"]
    assert len(chat.prompts) == 1, "the model must not be asked to fix a config typo"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest harness/tests/test_loop_job.py -q -k "regression or no_code or planted"`
Expected: FAIL — `KeyError: 'regression_passed'`.

- [ ] **Step 3: Implement**

In `harness/loop_job.py`:

Extend the imports and declare the operator-error exception:

```python
from pytest_runner import (OPERATOR_ERROR_CODES, REGRESSION_TIMEOUT, materialize_tests,
                           run_regression, run_tests)
```

```python
class RegressionCommandError(RuntimeError):
    """The project's regression_cmd is wrong. That is the operator's bug, not the model's."""
```

Change `_run_ladder`'s signature to take the project's command and the timeout:

```python
def _run_ladder(store, job_id, worktree, tests_dir, spec, stages, chat_fn, unload_fn,
                system, num_ctx, plateau_k, test_timeout, regression_cmd,
                regression_timeout):
```

Initialise the new trackers next to `latest`:

```python
    latest = {"tests_passed": 0, "tests_failed": 0}
    regression_passed, regression_output, failure_reason = None, "", None
```

In the `PatchError` branch, record the reason:

```python
            except PatchError as e:
                rec.update({"result": type(e).__name__, "passed": False})
                attempts.append(rec)
                store.append_log(job_id, "attempt {}: {}\n".format(rec["attempt"], e))
                last_output = "Your previous reply was rejected: {}".format(e)
                failure_reason = "no_code"
                continue
```

Replace the block from `result = run_tests(...)` down to the plateau check with:

```python
            result = run_tests(worktree.path, tests_dir, timeout=test_timeout)
            latest = {"tests_passed": result.tests_passed, "tests_failed": result.tests_failed}

            # Only pay for the project's suite once the spec is satisfied: a red spec
            # has already told the model what to fix.
            regression = None
            if result.passed and regression_cmd:
                worktree.restore_judge()
                regression = run_regression(worktree.path, regression_cmd,
                                            timeout=regression_timeout)
                if regression.returncode in OPERATOR_ERROR_CODES:
                    raise RegressionCommandError(
                        "{} exited {}; check regression_cmd in factory.toml\n{}".format(
                            regression_cmd, regression.returncode, regression.output[-1000:]))
                regression_passed = regression.passed
                regression_output = regression.output[-3000:]

            green = result.passed and (regression is None or regression.passed)
            rec.update({"passed": green, "timed_out": result.timed_out,
                        "regression_passed": None if regression is None else regression.passed,
                        **latest})
            attempts.append(rec)
            store.update_state(job_id, stage=si + 1, attempt=rec["attempt"], model=model,
                               regression_passed=rec["regression_passed"], **latest)
            store.append_log(job_id, "attempt {} [{}]: {} passed, {} failed, regression={}\n".format(
                rec["attempt"], model, result.tests_passed, result.tests_failed,
                rec["regression_passed"]))

            if green:
                success = True
                failure_reason = None
                break
            if result.passed:
                # The spec is green and the project is not: that is the signal to fix.
                last_output = regression.output[-3000:]
                failure_reason = "regression"
            else:
                last_output = result.output[-3000:]
                failure_reason = "spec_tests"
            fail_hist.append(result.tests_failed)
            if len(fail_hist) >= plateau_k and len(set(fail_hist[-plateau_k:])) == 1:
                rec["plateau"] = True
                break
```

Change `_run_ladder`'s return to carry the new fields:

```python
    return {"success": success, "attempts": attempts,
            "stages_used": si + 1, "escalated": si > 0,
            "final_model": attempts[-1]["model"] if attempts else None,
            "regression_passed": regression_passed, "regression_output": regression_output,
            "failure_reason": failure_reason, **latest}
```

In `_execute`, pass the project's command through, and tag the GPU timeout:

```python
    if not lock.acquire(timeout=gpu_timeout, poll=2.0, on_wait=lambda: store.heartbeat(job_id)):
        return {"status": "failed", "error": "gpu_timeout", "failure_reason": "gpu_timeout",
                "detail": "another job held the GPU for {}s".format(gpu_timeout)}
```

```python
        outcome = _run_ladder(store, job_id, worktree, tests_dir, spec,
                              ladder or DEFAULT_LADDER, chat_fn, unload_fn, system,
                              num_ctx, plateau_k, test_timeout, project.regression_cmd,
                              regression_timeout)
```

Add `regression_timeout=REGRESSION_TIMEOUT` to both `_execute`'s and `run_job`'s parameter lists, and forward it from `run_job` to `_execute`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest harness/tests/test_loop_job.py -q`
Expected: PASS.

Then the whole suite:
Run: `python -m pytest harness/tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/loop_job.py harness/tests/test_loop_job.py
git commit -m "feat: a job is green only when the project's suite is green too"
```

---

### Task 6: `delegate` accepts context files

**Files:**
- Modify: `harness/factory_mcp.py`
- Test: `harness/tests/test_factory_mcp.py`

**Interfaces:**
- Consumes: `validate_context_files`, `ContextTooLarge` from Task 2.
- Produces: `Factory.delegate(project, goal, tests, target_files, context_files=None)`; `spec.json` gains `context_files: list[str]` (empty list when omitted).

- [ ] **Step 1: Write the failing tests**

In `harness/tests/test_factory_mcp.py`, the `project` fixture already commits `calc.py`. Add `elo.py` to it:

```python
    (root / "calc.py").write_text("x = 1\n")
    (root / "elo.py").write_text("K = 32\n")
```

Then append:

```python
def test_delegate_records_context_files_in_the_spec(factory):
    job_id = payload(delegate(factory, context_files=["elo.py"]))["job_id"]

    spec = JobStore(factory.jobs_root).read_spec(job_id)
    assert spec["context_files"] == ["elo.py"]


def test_delegate_without_context_files_records_an_empty_list(factory):
    job_id = payload(delegate(factory))["job_id"]

    assert JobStore(factory.jobs_root).read_spec(job_id)["context_files"] == []


def test_delegate_refuses_a_context_file_absent_from_head(factory):
    result = delegate(factory, context_files=["typo.py"])

    assert result["isError"] is True
    assert factory.spawned == []


def test_delegate_refuses_a_context_file_that_is_also_a_target(factory):
    result = delegate(factory, context_files=["calc.py"])

    assert result["isError"] is True
    assert factory.spawned == []


def test_the_delegate_schema_advertises_context_files(factory):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    tools = handle(msg, factory)["result"]["tools"]

    schema = next(t for t in tools if t["name"] == "delegate")["inputSchema"]
    assert "context_files" in schema["properties"]
    assert "context_files" not in schema["required"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest harness/tests/test_factory_mcp.py -q -k context`
Expected: FAIL — `bad arguments for delegate: delegate() got an unexpected keyword argument 'context_files'`.

- [ ] **Step 3: Implement**

In `harness/factory_mcp.py`:

Extend the import:

```python
from factory_config import (ConfigError, load_projects, resolve_project,
                            validate_context_files, validate_targets, validate_tests)
```

Add the property to the `delegate` schema, inside `"properties"`:

```python
                "context_files": {"type": "array", "items": _STR,
                                  "description": "Repo-relative files the model may READ "
                                                 "but never write. Keep them few and small."},
```

and extend the tool's description with one sentence:

```python
            "is a diff, never a commit. A job is green only when the project's own test "
            "suite still passes."
```

Replace `Factory.delegate` with:

```python
    def delegate(self, project, goal, tests, target_files, context_files=None):
        # Validate before anything exists on disk: a refused delegation leaves no trace.
        resolved = resolve_project(load_projects(self.config_path), project)
        validate_tests(tests)
        validate_targets(target_files)
        validate_context_files(resolved, context_files, target_files)
        job_id = self.store.create({"project": project, "goal": goal, "tests": tests,
                                    "target_files": list(target_files),
                                    "context_files": list(context_files or [])})
        self.spawn_fn(job_id)
        return {"job_id": job_id, "status": "queued"}
```

`ContextTooLarge` subclasses `ConfigError`, which `handle` already catches, so no change to the error path.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest harness/tests/test_factory_mcp.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -m "feat: delegate accepts read-only context files"
```

---

### Task 7: Turn the gate on for this repo, and write it down

**Files:**
- Modify: `factory.toml`
- Modify: `DECISIONS.md`
- Modify: `docs/delegation.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing code depends on.

- [ ] **Step 1: Turn the gate on**

In `factory.toml`, add the command to the project, and a line of comment above it:

```toml
[projects.local-factory]
path = "C:/Users/me/local-factory"
runner = "pytest"
# A job is green only when this suite is still green. Without it, a model can
# satisfy its own spec and break everything else.
regression_cmd = ["-m", "pytest", "harness/tests", "-q"]
```

- [ ] **Step 2: Prove the gate works against the real repo**

Run: `python -m pytest harness/tests -q`
Expected: PASS.

Then delegate a real job that must read one module to change another. From the repo root:

```bash
python - <<'EOF'
import sys, time, json
sys.path.insert(0, "harness")
from factory_mcp import Factory
f = Factory("jobs", "factory.toml")
TEST = '''from harness_demo import job_dir_name

def test_uses_the_job_id():
    assert job_dir_name("j_7f3a") == "j_7f3a"
'''
r = f.delegate("local-factory",
               "Create harness_demo.py exposing job_dir_name(job_id) -> str, "
               "returning the directory name JobStore uses for that job.",
               {"test_demo.py": TEST}, ["harness_demo.py"],
               context_files=["harness/job_store.py"])
job_id = r["job_id"]
print("delegated", job_id, flush=True)
while True:
    st = f.job_status(job_id)
    if st["status"] not in ("queued", "waiting_gpu", "running"):
        break
    time.sleep(3)
out = f.job_result(job_id)
print(json.dumps({k: v for k, v in out.items() if k not in ("diff", "regression_output")}, indent=2))
print(out.get("diff", ""))
EOF
```

Expected: `"status": "succeeded"`, `"regression_passed": true`, and a diff creating `harness_demo.py`. If `regression_passed` is `false`, read `regression_output` — the model broke something, which is the gate doing its job.

- [ ] **Step 3: Record ADR-014**

In `DECISIONS.md`, insert before `## ADR-006`:

```markdown
## ADR-014 — A job is green only when the project's suite is green
**Context:** A job's judge was only the tests in its own spec. A model could make
those pass while breaking the other 143 tests, and the job reported `succeeded`.
For a factory that modifies itself, that is the sharpest edge in the design.
**Decision:** `factory.toml` declares a per-project `regression_cmd`, run inside the
worktree, but only once the spec's tests pass — a red spec has already told the model
what to fix. Its failure output is fed back to the model, which gets to repair what it
broke. Judge files inside the worktree are restored (tracked) and deleted (added)
before every run: `git checkout --` does not remove an added `conftest.py`, and an
added `conftest.py` is how a model fakes a green suite. The spec also gains
`context_files`, read-only reference material, so a change can span modules.
**Why:** Tests are the judge only if all of them are. The cost is that a delegation
can never touch a test, so it can only make changes that keep the existing suite green
as written — additive work and bug fixes. A behaviour-changing refactor is out of
reach. That is the price of a self-modifying system, and it is better named than
discovered.
```

- [ ] **Step 4: Update the guide and the changelog**

In `docs/delegation.md`, under **The two guarantees**, add a third:

```markdown
**A green spec is not a green job.** The project's own suite (`regression_cmd` in
`factory.toml`) runs inside the worktree once the spec's tests pass, and must pass
too. Before each run the worktree's judge files are restored and any planted ones
deleted. The model may never write a test, so a delegation can only make changes that
keep the existing suite green as written: additive work and bug fixes. Behaviour-
changing refactors cannot be delegated. See ADR-014.
```

and extend the `delegate` bullet under **Tools**:

```markdown
- `delegate(project, goal, tests, target_files, context_files=[])` → `{job_id, status}`.
  `context_files` are read-only reference files injected into the prompt, capped at
  60 000 bytes total and validated against the project's HEAD.
```

In `CHANGELOG.md`, under `## Unreleased`, add at the top of the delegation bullet list:

```markdown
- **Delegation v2** — `context_files` (read-only reference material in the prompt) and
  a per-project `regression_cmd` that must also pass before a job is `succeeded`.
  ADR-014.
```

- [ ] **Step 5: Commit and push**

```bash
git add factory.toml DECISIONS.md docs/delegation.md CHANGELOG.md
git commit -m "feat: enable the regression gate on local-factory

Records ADR-014 and documents the limitation it imposes: a delegation may
never touch a test, so it can only make changes that keep the existing suite
green as written."
git push origin main
```

---

## Verification

Run the whole suite from the repo root:

```bash
python -m pytest harness/tests -q
```

Expected: all tests pass. The suite should have grown from 143 to roughly 180
(including the new `harness/tests/test_ollama_client.py`).

The behaviours that carry the design, and which must be green:

- `test_a_job_that_satisfies_its_spec_but_breaks_the_project_fails`
- `test_the_regression_suite_does_not_run_while_the_spec_is_red`
- `test_a_planted_judge_file_is_gone_before_the_regression_runs`
- `test_restore_judge_deletes_a_planted_conftest`
- `test_restore_judge_leaves_a_legitimate_new_module_alone`
- `test_a_model_that_tries_to_write_a_reference_file_is_refused`
- `test_context_over_the_budget_is_refused`
- `test_a_misconfigured_regression_command_is_an_error_not_a_model_failure`
- `test_context_files_raise_the_context_window`
- `test_prefill_speed_and_ttft_come_from_the_prompt_eval_fields`

# Machine-Local Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `factory.toml` into a tracked file holding the perimeter and the doctrine, and a gitignored `factory.local.toml` holding this machine's coordinates, so the repo survives a clone.

**Architecture:** A private `_load_merged(config_path)` in `harness/factory_config.py` reads `factory.toml`, deep-merges `factory.local.toml` from beside it, and returns the raw dict every public `load_*` already parses. The sibling-file lookup copies `credentials.path_for`. No public signature changes: `factory_config.py` is the only module edited, and the eight that consume it are untouched.

**Tech Stack:** Python 3.9+ (`tomllib` on 3.11+, `tomli` fallback — already handled at `factory_config.py:13-16`), pytest, argparse.

**Spec:** `docs/design/specs/2026-08-04-machine-local-config-design.md`

## Global Constraints

- Python must stay 3.9-compatible: the file already falls back to `tomli`, and `loop.py` runs on the factory's 3.9. No `match`, no `|` type unions, no `tomllib`-only assumptions.
- No public signature changes. `load_projects(config_path)`, `load_models(config_path)`, `load_llama_server(config_path)`, `load_cloud_catalog(config_path, catalog_fn=None)`, `load_mcp_servers(config_path)`, `load_toolsets(config_path, mcp_servers=None)` and `resolve_project(projects, name)` keep their exact current signatures.
- Local-only keys, verbatim from the spec: `projects.*.path`, `llama_server.exe`, `llama_server.models.*.path`. Nothing else is local-only.
- Merge rule, verbatim: tables merge recursively; scalars and arrays are replaced wholesale; a key present only in the local file is added; the local file wins on conflict.
- `factory.local.toml` is never overwritten by any code path. `add_project` appends; `bootstrap` creates only when absent.
- The suite must be green at every task boundary. Task ordering below is built around this — the guard (Task 4) lands only after the fixtures are migrated (Task 3).
- Commits go directly on `main`, per the project convention in `CLAUDE.md`. `git stash`, `reset --hard` and `push --force` are forbidden.
- Run the suite with: `py -3 -m pytest harness/tests -q -m "not browser"` — the exact command `.githooks/pre-push` runs. Browser tests need a live studio and are excluded from the gate; three of them also import playwright at module level, so they break collection rather than deselecting.
- **Baseline on this machine, 2026-08-04, before Task 1: `1263 passed, 4 skipped, 4 deselected` in ~4min.** Every task boundary must still show 1263 plus whatever that task added. A drop is a regression, not a rounding error.
- The full suite takes ~4 minutes. Run the targeted test file while iterating; run the full suite once, at the end of the task, before committing.

---

### Task 1: The merge core

Pure addition. Nothing calls it yet, so the suite cannot change colour.

**Files:**
- Modify: `harness/factory_config.py` (add after the `ConfigError` subclasses, around line 66)
- Test: `harness/tests/test_local_config.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `LOCAL_FILENAME` (str constant `"factory.local.toml"`), `local_config_path(config_path) -> Path`, `_merge(base: dict, over: dict) -> dict`, `_load_merged(config_path) -> dict`. Tasks 2, 4, 6 and 7 rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Create `harness/tests/test_local_config.py`:

```python
"""factory.toml carries the perimeter; factory.local.toml carries this machine.

Spec: docs/design/specs/2026-08-04-machine-local-config-design.md
"""
import pytest

from factory_config import ConfigError, _load_merged, local_config_path


def write(tmp_path, tracked, local=None):
    cfg = tmp_path / "factory.toml"
    cfg.write_text(tracked, encoding="utf-8")
    if local is not None:
        (tmp_path / "factory.local.toml").write_text(local, encoding="utf-8")
    return cfg


def test_the_local_file_is_a_sibling_of_the_tracked_one(tmp_path):
    cfg = tmp_path / "factory.toml"

    assert local_config_path(cfg) == tmp_path / "factory.local.toml"


def test_a_missing_local_file_leaves_the_tracked_file_untouched(tmp_path):
    cfg = write(tmp_path, '[projects.demo]\nrunner = "pytest"\n')

    assert _load_merged(cfg) == {"projects": {"demo": {"runner": "pytest"}}}


def test_a_local_scalar_overrides_the_tracked_one(tmp_path):
    cfg = write(tmp_path,
                '[llama_server]\nctx = 32768\nport = 8091\n',
                '[llama_server]\nctx = 65536\n')

    merged = _load_merged(cfg)

    assert merged["llama_server"]["ctx"] == 65536
    assert merged["llama_server"]["port"] == 8091


def test_a_local_array_replaces_the_tracked_one_wholesale(tmp_path):
    cfg = write(tmp_path,
                '[llama_server.models."m"]\nargs = ["--n-cpu-moe", "24"]\n',
                '[llama_server.models."m"]\nargs = ["--n-cpu-moe", "8"]\n')

    assert _load_merged(cfg)["llama_server"]["models"]["m"]["args"] == \
        ["--n-cpu-moe", "8"]


def test_a_key_only_in_the_local_file_is_added(tmp_path):
    cfg = write(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                '[projects.demo]\npath = "C:/somewhere"\n')

    demo = _load_merged(cfg)["projects"]["demo"]

    assert demo == {"runner": "pytest", "path": "C:/somewhere"}


def test_tables_merge_recursively(tmp_path):
    cfg = write(tmp_path, '[projects.a]\nrunner = "pytest"\n',
                '[projects.b]\npath = "C:/b"\n')

    projects = _load_merged(cfg)["projects"]

    assert sorted(projects) == ["a", "b"]
    assert projects["a"] == {"runner": "pytest"}


def test_a_malformed_local_file_is_not_swallowed(tmp_path):
    cfg = write(tmp_path, '[projects.demo]\nrunner = "pytest"\n', "this is not toml")

    # A typo in the local file must be read out loud. Only a MISSING file is
    # the silent case; a broken one that loaded as {} would strip every path
    # on the machine and blame the projects.
    with pytest.raises(ValueError):
        _load_merged(cfg)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_local_config.py -q`
Expected: FAIL — `ImportError: cannot import name '_load_merged' from 'factory_config'`

- [ ] **Step 3: Write the implementation**

In `harness/factory_config.py`, immediately after the `ContextTooLarge` exception class (currently ends line 66) and before `def is_unsafe_path`, add:

```python
# The machine-local sibling of factory.toml. Same idea as secrets.toml
# (credentials.path_for): the tracked file says WHAT the factory may touch,
# this one says WHERE it lives on this computer. Never committed.
LOCAL_FILENAME = "factory.local.toml"


def local_config_path(config_path):
    """The machine-local file that sits beside `config_path`."""
    return Path(config_path).resolve().parent / LOCAL_FILENAME


def _merge(base, over):
    """Deep-merge `over` onto `base`.

    Tables merge recursively; scalars and arrays are replaced wholesale. An
    `args` or a `regression_cmd` has to be rewritable entirely -- unioning
    `["--n-cpu-moe", "24"]` with a smaller card's flags would produce a command
    line neither machine asked for.
    """
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_merged(config_path):
    """The tracked perimeter with this machine's coordinates merged on top.

    A missing local file is not an error: the tracked file alone must stay a
    valid file, or the fragility has only moved. A malformed one is an error --
    swallowing it would silently strip every path on the machine, and the
    failure would surface as "project not a git repo" pointing at nothing.
    """
    with open(config_path, "rb") as f:
        raw = tomllib.load(f)
    try:
        handle = open(local_config_path(config_path), "rb")
    except OSError:
        return raw
    with handle as f:
        return _merge(raw, tomllib.load(f))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_local_config.py -q`
Expected: PASS, 7 tests.

- [ ] **Step 5: Run the full suite — nothing may have moved**

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: same pass count as before the task. Nothing calls `_load_merged` yet.

- [ ] **Step 6: Commit**

```bash
git add harness/factory_config.py harness/tests/test_local_config.py
git commit -F - <<'EOF'
feat(config): a merge rule for two files, not yet wired to anything

factory.toml says what the factory may touch; factory.local.toml, beside it and
never committed, says where those things live on this computer. Tables merge,
scalars and arrays are replaced wholesale, local wins.

A missing local file loads fine -- the tracked file has to stay valid on its
own. A malformed one raises: loading it as {} would strip every path and then
blame the projects for not being git repos.

EOF
```

---

### Task 2: Every loader reads the merged view

**Files:**
- Modify: `harness/factory_config.py:74-77` (`load_projects`), `:85-105` (`load_models`), `:113-142` (`load_llama_server`), `:158-214` (`load_cloud_catalog`), `:259-279` (`load_mcp_servers`), `:282-302` (`load_toolsets`)
- Test: `harness/tests/test_local_config.py` (append)

**Interfaces:**
- Consumes: `_load_merged(config_path)` from Task 1.
- Produces: no new names. The six loaders keep their signatures and return types exactly.

- [ ] **Step 1: Write the failing tests**

Append to `harness/tests/test_local_config.py`:

```python
def test_a_local_path_reaches_load_projects(tmp_path):
    cfg = write(tmp_path,
                '[projects.demo]\nrunner = "vitest"\n',
                '[projects.demo]\npath = "C:/elsewhere"\n')

    from factory_config import load_projects

    assert load_projects(cfg)["demo"] == {"runner": "vitest",
                                          "path": "C:/elsewhere"}


def test_a_local_override_reaches_load_llama_server(tmp_path):
    cfg = write(tmp_path,
                '[llama_server]\nport = 8091\nctx = 32768\n'
                '[llama_server.models."m"]\nargs = ["--n-cpu-moe", "24"]\n',
                '[llama_server]\nexe = "C:/llama-server.exe"\nctx = 16384\n'
                '[llama_server.models."m"]\npath = "C:/m.gguf"\n')

    from factory_config import load_llama_server

    cfg_out = load_llama_server(cfg)

    assert cfg_out["exe"] == "C:/llama-server.exe"
    assert cfg_out["ctx"] == 16384          # local wins
    assert cfg_out["port"] == 8091          # tracked default survives
    assert cfg_out["models"]["m"] == {"path": "C:/m.gguf",
                                      "args": ["--n-cpu-moe", "24"],
                                      "tools": True}


def test_a_local_file_can_override_a_sampling_profile(tmp_path):
    cfg = write(tmp_path, '[models."q"]\ntemperature = 0.7\n',
                '[models."q"]\ntemperature = 0.2\n')

    from factory_config import load_models

    assert load_models(cfg)["q"]["temperature"] == 0.2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_local_config.py -q`
Expected: FAIL — the loaders still read only `config_path`, so `exe` is missing and `load_llama_server` raises `ConfigError: [llama_server] is missing 'exe'`.

- [ ] **Step 3: Rewire the six loaders**

Each change is the same shape: replace the two-line `open` + `tomllib.load` with one `_load_merged` call. Exact replacements:

`load_projects` — replace
```python
    with open(config_path, "rb") as f:
        return tomllib.load(f).get("projects", {})
```
with
```python
    return _load_merged(config_path).get("projects", {})
```

`load_models` — replace
```python
    with open(config_path, "rb") as f:
        raw = tomllib.load(f).get("models", {})
```
with
```python
    raw = _load_merged(config_path).get("models", {})
```

`load_llama_server` — replace
```python
    with open(config_path, "rb") as f:
        raw = tomllib.load(f).get("llama_server")
```
with
```python
    raw = _load_merged(config_path).get("llama_server")
```

`load_cloud_catalog` — replace
```python
    with open(config_path, "rb") as f:
        cloud = tomllib.load(f).get("cloud") or {}
```
with
```python
    cloud = _load_merged(config_path).get("cloud") or {}
```

`load_mcp_servers` — replace
```python
    with open(config_path, "rb") as f:
        raw = tomllib.load(f).get("mcp", {}).get("servers", {})
```
with
```python
    raw = _load_merged(config_path).get("mcp", {}).get("servers", {})
```

`load_toolsets` — replace
```python
    with open(config_path, "rb") as f:
        raw = tomllib.load(f).get("toolsets", {})
```
with
```python
    raw = _load_merged(config_path).get("toolsets", {})
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_local_config.py -q`
Expected: PASS, 10 tests.

- [ ] **Step 5: Run the full suite**

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: same pass count as Task 1. Existing fixtures write a single `factory.toml` with paths inside; merging one file with nothing is that file, so they are unaffected.

- [ ] **Step 6: Commit**

```bash
git add harness/factory_config.py harness/tests/test_local_config.py
git commit -F - <<'EOF'
feat(config): every loader reads the merged view

Six loaders, one change each: the open+tomllib pair becomes _load_merged. No
signature moves, so the eight modules that thread config_path stay untouched.

Nothing is refused yet -- a tracked file with paths in it still loads, which is
what keeps the suite green until the fixtures move.

EOF
```

---

### Task 3: Move the fixtures' paths into the local file

This is the migration that lets Task 4 refuse coordinates in the tracked file. Do it before the guard, not after.

**Files:**
- Modify: `harness/tests/conftest.py` (append helper)
- Modify: 11 test files, 22 config sites (see the table below)

**Interfaces:**
- Consumes: nothing from earlier tasks (the helper only writes files).
- Produces: `write_config(tmp_path, tracked, paths=None, models=None, exe=None, name="factory.toml") -> Path` in `conftest.py`, importable by tests as a plain function (not a fixture) via `from conftest import write_config`. Task 7's `test_bootstrap.py` also imports `git` from the same module, which already exists there (`conftest.py:50`).

**The 22 sites** — `grep -n 'path = "' harness/tests/*.py` returns 23; `test_factory_web.py:130` (`h.path = "/"`) is an HTTP handler attribute, not config. Leave it alone.

| File | Lines | Kind |
|---|---|---|
| `test_cloud_routing.py` | 28, 76 | `llama_server.models.*.path` |
| `test_draft_job.py` | 44 | `projects.demo.path` |
| `test_factory_cli.py` | 39 | `projects.demo.path` |
| `test_factory_config.py` | 32, 37, 41, 45 | `projects.*.path` |
| `test_factory_mcp.py` | 103 | `projects.demo.path` |
| `test_factory_web.py` | 72 | `projects.demo.path` |
| `test_goal_cli.py` | 20 | `projects.demo.path` |
| `test_job_apply.py` | 37 | `projects.demo.path` |
| `test_llama_server_manager.py` | 24, 32 | project + model path |
| `test_loop_job.py` | 58, 66, 706, 793, 836, 1034, 1036 | 6 project + 1 model |
| `test_regress.py` | 45 | `projects.*.path` |

- [ ] **Step 1: Write the helper**

Append to `harness/tests/conftest.py`:

```python
def write_config(tmp_path, tracked, paths=None, models=None, exe=None,
                 name="factory.toml"):
    """Write the two files the factory reads, return the tracked one.

    `tracked` is the perimeter: runners, regression commands, sampling, the
    things a repo carries. `paths` ({project: path}), `models` ({model: path})
    and `exe` are machine coordinates and go to factory.local.toml, which is
    where a coordinate is allowed to exist
    (spec 2026-08-04-machine-local-config-design.md).

    `name` exists because not every fixture calls its file factory.toml --
    test_llama_server_manager.py uses f.toml. The local sibling is always
    factory.local.toml regardless, which is what local_config_path computes.
    """
    cfg = tmp_path / name
    cfg.write_text(tracked, encoding="utf-8")
    body = []
    if exe is not None:
        body.append("[llama_server]")
        body.append('exe = "{}"'.format(exe))
    for project, path in (paths or {}).items():
        body.append("[projects.{}]".format(project))
        body.append('path = "{}"'.format(Path(path).as_posix()))
    for model, path in (models or {}).items():
        body.append('[llama_server.models."{}"]'.format(model))
        body.append('path = "{}"'.format(Path(path).as_posix()))
    (tmp_path / "factory.local.toml").write_text(
        "\n".join(body) + "\n", encoding="utf-8")
    return cfg
```

Table order is irrelevant in TOML, so emitting `[llama_server]` before
`[projects.*]` and `[llama_server.models."m"]` after them parses fine.

- [ ] **Step 2: Migrate the single-project fixtures**

Six files share one identical line. In `test_draft_job.py:44`, `test_factory_cli.py:39`, `test_factory_mcp.py:103`, `test_factory_web.py:72`, `test_job_apply.py:37`, replace:

```python
    cfg.write_text('[projects.demo]\npath = "{}"\nrunner = "pytest"\n'.format(project.as_posix()))
```

with:

```python
    cfg = write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                       paths={"demo": project})
```

Delete the now-dead `cfg = tmp_path / "factory.toml"` line directly above each, add `from conftest import write_config` to the file's imports, and make sure the fixture still returns `cfg`.

`test_goal_cli.py:20` is the same line with `src` instead of `project`:

```python
    cfg = write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                       paths={"demo": src})
```

- [ ] **Step 3: Migrate `test_factory_config.py`**

Its module-level `TOML` string holds four `path =` lines (32, 37, 41, 45) for projects `local-factory`, `conformergpd`, `ghost`, `webapp`. Strip every `path = "..."` line from `TOML`, then change the `config` fixture (line 54-64) to:

```python
    cfg_path = write_config(
        tmp_path, TOML.format(root=root.as_posix()),
        paths={"local-factory": root, "conformergpd": root,
               "ghost": root / "does-not-exist", "webapp": root})
    return load_projects(cfg_path)
```

`TOML` no longer needs `{root}` for the paths, but keep the `.format(root=...)` call if any other line still uses it; if none does, drop the `.format` and pass `TOML` directly.

Note: `ghost` intentionally points at a non-existent directory — it is the fixture for "declared but not a git repo". It keeps a path, it just lives in the local file now.

- [ ] **Step 4: Migrate `test_loop_job.py`**

Six project sites (58, 66, 706, 793, 836, 1034) and one model site (1036). Lines 58, 66, 706, 836, 1034 all write the same `[projects.demo]` block; each becomes a `write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n', paths={"demo": project})`.

Line 793 is the vitest one:

```python
    config = write_config(tmp_path, '[projects.js]\nrunner = "vitest"\n',
                          paths={"js": project})
```

Lines 1034-1036 write a project *and* a model in one call:

```python
    config = write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                          paths={"demo": project}, models={"A": "C:/a.gguf"})
```

- [ ] **Step 5: Migrate the model-path files**

`test_cloud_routing.py` — its `_config` helper (line 20) carries both `exe` and a model `path`. Replace the `body` literal's opening block:

```python
    body = """
[llama_server]
exe = "llama-server.exe"
port = 8091
ctx = 32768

[llama_server.models."qwen3-coder:30b"]
path = "model.gguf"
args = []
"""
```

with:

```python
    body = """
[llama_server]
port = 8091
ctx = 32768

[llama_server.models."qwen3-coder:30b"]
args = []
"""
```

and replace its last three lines:

```python
    path = tmp_path / "factory.toml"
    path.write_text(body, encoding="utf-8")
    return path
```

with:

```python
    return write_config(tmp_path, body, exe="llama-server.exe",
                        models={"qwen3-coder:30b": "model.gguf"})
```

The second site (line 76) is inside the same helper's `cloud_section` branch and needs no edit — it only appends `[cloud.models]` tables.

`test_llama_server_manager.py:24` — replace:

```python
    p = tmp_path / "f.toml"
    p.write_text('[projects.demo]\npath = "x"\nrunner = "pytest"\n')
```

with:

```python
    p = write_config(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                     paths={"demo": "x"}, name="f.toml")
```

`test_llama_server_manager.py:32` — replace:

```python
    p = tmp_path / "f.toml"
    p.write_text(
        '[llama_server]\nexe = "C:/l.exe"\nport = 8091\nctx = 32768\n'
        '[llama_server.models."m:a"]\npath = "C:/a.gguf"\n'
        'args = ["--n-cpu-moe", "24"]\n')
```

with:

```python
    p = write_config(
        tmp_path,
        '[llama_server]\nport = 8091\nctx = 32768\n'
        '[llama_server.models."m:a"]\nargs = ["--n-cpu-moe", "24"]\n',
        exe="C:/l.exe", models={"m:a": "C:/a.gguf"}, name="f.toml")
```

Apply the same shape to every other `f.toml` fixture in that file that sets `exe` or a model `path` — `grep -n 'exe\|path' harness/tests/test_llama_server_manager.py` lists them; the assertions (`cfg["exe"] == "C:/l.exe"`) stay exactly as they are, because the merged view still produces them.

`test_regress.py:45` — replace the `config` fixture body:

```python
    path = tmp_path / "factory.toml"
    path.write_text(textwrap.dedent("""
        [projects.demo]
        path = "{}"
        runner = "pytest"
    """.format(str(project).replace("\\", "/"))))
    return path
```

with:

```python
    return write_config(tmp_path, textwrap.dedent("""
        [projects.demo]
        runner = "pytest"
    """), paths={"demo": project})
```

- [ ] **Step 6: Run the full suite**

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: PASS, same count as Task 2. Behaviour is identical — the paths simply arrive from the other file.

- [ ] **Step 7: Verify no config site was missed**

Run: `grep -n 'path = "' harness/tests/*.py`
Expected: exactly one line, `test_factory_web.py:130: h.path = "/"`.

- [ ] **Step 8: Commit**

```bash
git add harness/tests/
git commit -F - <<'EOF'
test: fixtures write coordinates where coordinates belong

22 sites across 11 files moved their paths from the tracked body to the local
file, through one conftest helper. Same behaviour, and the fixtures got shorter
rather than longer.

This is what lets the next commit refuse a path in factory.toml without
turning the suite red.

EOF
```

---

### Task 4: Refuse a coordinate in the tracked file

The guard that makes the split hold. Everything before this is rearrangement; this is what stops the regression from returning.

**Files:**
- Modify: `harness/factory_config.py` (add `_reject_local_only`, call it from `_load_merged`)
- Test: `harness/tests/test_local_config.py` (append)

**Interfaces:**
- Consumes: `_load_merged` from Task 1.
- Produces: `_reject_local_only(raw) -> None`, raising `ConfigError`.

- [ ] **Step 1: Write the failing tests**

Append to `harness/tests/test_local_config.py`:

```python
def test_a_project_path_in_the_tracked_file_is_refused(tmp_path):
    cfg = write(tmp_path, '[projects.demo]\npath = "C:/demo"\nrunner = "pytest"\n')

    with pytest.raises(ConfigError) as e:
        _load_merged(cfg)

    assert "projects.demo.path" in str(e.value)
    assert "factory.local.toml" in str(e.value)


def test_the_llama_server_exe_in_the_tracked_file_is_refused(tmp_path):
    cfg = write(tmp_path, '[llama_server]\nexe = "C:/llama-server.exe"\n')

    with pytest.raises(ConfigError) as e:
        _load_merged(cfg)

    assert "llama_server.exe" in str(e.value)


def test_a_model_path_in_the_tracked_file_is_refused(tmp_path):
    cfg = write(tmp_path, '[llama_server.models."m"]\npath = "C:/m.gguf"\n')

    with pytest.raises(ConfigError) as e:
        _load_merged(cfg)

    assert 'llama_server.models."m".path' in str(e.value)


def test_the_same_keys_are_welcome_in_the_local_file(tmp_path):
    cfg = write(tmp_path, '[projects.demo]\nrunner = "pytest"\n',
                '[projects.demo]\npath = "C:/demo"\n'
                '[llama_server]\nexe = "C:/llama-server.exe"\n')

    merged = _load_merged(cfg)

    assert merged["projects"]["demo"]["path"] == "C:/demo"
    assert merged["llama_server"]["exe"] == "C:/llama-server.exe"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_local_config.py -q`
Expected: FAIL — the three refusal tests do not raise; `DID NOT RAISE <class 'ConfigError'>`.

- [ ] **Step 3: Write the guard**

In `harness/factory_config.py`, add above `_load_merged`:

```python
def _reject_local_only(raw):
    """A machine coordinate in the tracked file is how this broke the first time.

    On 2026-08-04 factory.toml held nine absolute paths; all nine resolved to
    nothing on a second computer, while every project sat right there under
    Projects/. Refusing the key is what keeps the split honest -- documentation
    did not, and `add_project` wrote paths into the tracked file on its own.
    """
    def refuse(key):
        raise ConfigError(
            "{} is a machine coordinate and must not be committed: move it to "
            "{}".format(key, LOCAL_FILENAME))

    for name, entry in (raw.get("projects") or {}).items():
        if isinstance(entry, dict) and "path" in entry:
            refuse("projects.{}.path".format(name))
    server = raw.get("llama_server") or {}
    if "exe" in server:
        refuse("llama_server.exe")
    for name, entry in (server.get("models") or {}).items():
        if isinstance(entry, dict) and "path" in entry:
            refuse('llama_server.models."{}".path'.format(name))
```

Then in `_load_merged`, insert the call immediately after the tracked file is parsed:

```python
    with open(config_path, "rb") as f:
        raw = tomllib.load(f)
    _reject_local_only(raw)
```

The guard runs on the tracked file only — the local file is exactly where those keys belong.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_local_config.py -q`
Expected: PASS, 14 tests.

- [ ] **Step 5: Run the full suite**

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: PASS, same count as Task 3. If anything fails here, a fixture from Task 3 was missed — fix the fixture, not the guard.

- [ ] **Step 6: Commit**

```bash
git add harness/factory_config.py harness/tests/test_local_config.py
git commit -F - <<'EOF'
feat(config): a coordinate in the tracked file is now an error

Nine absolute paths in factory.toml, nine that resolved to nothing on a second
computer. Documentation did not stop that -- add_project wrote them itself --
so the key is refused at load, named, with the file it belongs in.

Same posture as the unknown-sampling-key refusal: the mistake is impossible,
not discouraged.

EOF
```

---

### Task 5: A project with no path on this machine

**Files:**
- Modify: `harness/factory_config.py:44-46` (add exception), `:305-317` (`resolve_project`)
- Test: `harness/tests/test_local_config.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ProjectNotOnThisMachine(ProjectNotAllowed)`. Task 7 catches it by name.

- [ ] **Step 1: Write the failing test**

Append to `harness/tests/test_local_config.py`:

```python
def test_a_project_with_no_local_path_says_what_to_write(tmp_path):
    from factory_config import (ProjectNotOnThisMachine, load_projects,
                                resolve_project)

    cfg = write(tmp_path, '[projects.blitzvolley]\nrunner = "node"\n')

    with pytest.raises(ProjectNotOnThisMachine) as e:
        resolve_project(load_projects(cfg), "blitzvolley")

    message = str(e.value)
    assert "blitzvolley" in message
    assert "factory.local.toml" in message


def test_it_is_still_a_config_error_for_every_existing_handler(tmp_path):
    from factory_config import (ProjectNotAllowed, ProjectNotOnThisMachine)

    # Ten `except ConfigError` sites outside factory_config.py rely on this
    # (verified 2026-08-04); none catches ProjectNotAllowed by name.
    assert issubclass(ProjectNotOnThisMachine, ProjectNotAllowed)
    assert issubclass(ProjectNotOnThisMachine, ConfigError)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_local_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'ProjectNotOnThisMachine'`

- [ ] **Step 3: Add the exception and the branch**

In `harness/factory_config.py`, directly after the `ProjectNotAllowed` class:

```python
class ProjectNotOnThisMachine(ProjectNotAllowed):
    """Declared in the tracked perimeter, but this computer has no path for it."""
```

In `resolve_project`, between the membership check and `path = Path(entry["path"]).resolve()`:

```python
    entry = projects[name]
    if "path" not in entry:
        raise ProjectNotOnThisMachine(
            "{!r} is declared in factory.toml but has no path on this machine "
            "-- add [projects.{}] path = \"...\" to {}".format(
                name, name, LOCAL_FILENAME))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_local_config.py -q`
Expected: PASS, 16 tests.

- [ ] **Step 5: Run the full suite**

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: PASS, unchanged count.

- [ ] **Step 6: Commit**

```bash
git add harness/factory_config.py harness/tests/test_local_config.py
git commit -F - <<'EOF'
feat(config): a project without a local path is refused where it is used

Visible in the list, refused at delegate, with the line to write and the file
to write it in. Filtering it out at load was the alternative and it loses: a
forgotten path would make a project vanish in silence.

Subclasses ProjectNotAllowed, so the ten existing `except ConfigError` sites
handle it without an edit.

EOF
```

---

### Task 6: add_project writes to the local file

**Files:**
- Modify: `harness/factory_mcp.py:1032-1070` (`add_project`)
- Test: `harness/tests/test_add_project.py`

**Interfaces:**
- Consumes: `local_config_path`, `LOCAL_FILENAME` from Task 1; `load_projects` (merged view) from Task 2.
- Produces: no new names. `add_project(name, path, regression_cmd=None)` keeps its signature and its `{"project": name, "status": "added"}` return.

- [ ] **Step 1: Write the failing tests**

Append to `harness/tests/test_add_project.py`:

```python
def test_the_new_project_lands_in_the_local_file(factory, tmp_path, project):
    from factory_config import local_config_path

    factory.add_project("newproj", str(project))

    tracked = factory.cfg.read_text(encoding="utf-8")
    local = local_config_path(factory.cfg).read_text(encoding="utf-8")

    assert "newproj" not in tracked      # nothing the agent writes is committed
    assert "[projects.newproj]" in local
    assert project.as_posix() in local


def test_a_name_already_in_the_tracked_file_is_still_refused(factory, project):
    # The duplicate check reads the MERGED view: otherwise the agent shadows a
    # versioned project by reusing its key, and the tracked runner silently
    # loses to a generated one.
    factory.cfg.write_text('[projects.taken]\nrunner = "vitest"\n',
                           encoding="utf-8")

    with pytest.raises(ConfigError):
        factory.add_project("taken", str(project))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_add_project.py -q`
Expected: FAIL — `add_project` still appends to `self.config_path`, so `"newproj" not in tracked` fails.

- [ ] **Step 3: Rewrite the write target**

In `harness/factory_mcp.py`, in `add_project`, change the docstring's first line and the final write block. Replace:

```python
        text = self.config_path.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            text += "\n"
        candidate = text + "\n".join(block) + "\n"
        if name not in tomllib.loads(candidate).get("projects", {}):
            raise ConfigError("the appended block did not parse back; nothing written")
        atomic_write_bytes(self.config_path, candidate.encode("utf-8"))
        return {"project": name, "status": "added"}
```

with:

```python
        target = local_config_path(self.config_path)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            # First project added on this machine: the file does not exist yet.
            text = ("# This machine's coordinates. Never commit this file.\n"
                    "# Promoting a project to factory.toml is a human act:\n"
                    "# a verified green baseline cannot be generated.\n")
        if text and not text.endswith("\n"):
            text += "\n"
        candidate = text + "\n".join(block) + "\n"
        if name not in tomllib.loads(candidate).get("projects", {}):
            raise ConfigError("the appended block did not parse back; nothing written")
        atomic_write_bytes(target, candidate.encode("utf-8"))
        return {"project": name, "status": "added"}
```

Update the import at `harness/factory_mcp.py:46-48` to include `local_config_path`.

The existing duplicate check at the top of the method (`if name in load_projects(self.config_path)`) already reads the merged view as of Task 2 — no edit needed, but it is what the second test pins.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_add_project.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: PASS, unchanged count.

- [ ] **Step 6: Commit**

```bash
git add harness/factory_mcp.py harness/tests/test_add_project.py
git commit -F - <<'EOF'
fix(mcp): the agent writes coordinates to the file that is not committed

add_project resolved an absolute path and appended it to the tracked file --
mid-session, from an MCP tool. factory-demo, the only entry in factory.toml
with no comment, is what it left behind.

It writes to factory.local.toml now. The duplicate check reads the merged view,
so a generated entry cannot shadow a versioned project by reusing its key.

EOF
```

---

### Task 7: `factory_cli.py bootstrap`

**Files:**
- Modify: `harness/factory_cli.py` (subparser in `_parser`, dispatch in `main`)
- Create: `harness/bootstrap.py`
- Test: `harness/tests/test_bootstrap.py` (create)

**Interfaces:**
- Consumes: `local_config_path`, `LOCAL_FILENAME`, `load_projects`, `load_llama_server`, `resolve_project`, `ProjectNotOnThisMachine`.
- Produces: `bootstrap.run(config_path, repo_root) -> dict` with keys `hooks_path` (str), `local_created` (bool), `warnings` (list of str).

The logic lives in `bootstrap.py`, not in the CLI: `factory_cli.main` is a dispatcher and everything it dispatches to is testable without argparse.

- [ ] **Step 1: Write the failing tests**

Create `harness/tests/test_bootstrap.py`:

```python
"""bootstrap: what a fresh clone needs before it can do anything.

Spec: docs/design/specs/2026-08-04-machine-local-config-design.md
"""
import subprocess

import pytest

import bootstrap
from conftest import git


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / ".githooks").mkdir()
    return root


def test_it_arms_the_hooks_path(repo):
    (repo / "factory.toml").write_text('[projects.demo]\nrunner = "pytest"\n',
                                       encoding="utf-8")

    bootstrap.run(repo / "factory.toml", repo)

    armed = subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath"],
                           capture_output=True, text=True)
    assert armed.stdout.strip() == ".githooks"


def test_it_writes_a_stub_per_declared_project(repo):
    (repo / "factory.toml").write_text(
        '[projects.demo]\nrunner = "pytest"\n[projects.other]\nrunner = "vitest"\n',
        encoding="utf-8")

    result = bootstrap.run(repo / "factory.toml", repo)

    local = (repo / "factory.local.toml").read_text(encoding="utf-8")
    assert result["local_created"] is True
    assert "[projects.demo]" in local
    assert "[projects.other]" in local
    # Stubs are commented: an empty path is a path, and it would resolve to the
    # current directory rather than announcing itself as unset.
    assert "# path =" in local


def test_it_never_overwrites_an_existing_local_file(repo):
    (repo / "factory.toml").write_text('[projects.demo]\nrunner = "pytest"\n',
                                       encoding="utf-8")
    (repo / "factory.local.toml").write_text("# mine\n", encoding="utf-8")

    result = bootstrap.run(repo / "factory.toml", repo)

    assert result["local_created"] is False
    assert (repo / "factory.local.toml").read_text(encoding="utf-8") == "# mine\n"


def test_a_project_with_no_path_is_a_warning_naming_it(repo):
    (repo / "factory.toml").write_text('[projects.blitzvolley]\nrunner = "node"\n',
                                       encoding="utf-8")

    warnings = bootstrap.run(repo / "factory.toml", repo)["warnings"]

    assert any("blitzvolley" in w for w in warnings)


def test_an_unset_git_identity_is_a_warning(repo):
    # Found the hard way on 2026-08-04: git refused the commit carrying the
    # spec for this very feature.
    (repo / "factory.toml").write_text('[projects.demo]\nrunner = "pytest"\n',
                                       encoding="utf-8")

    warnings = bootstrap.run(repo / "factory.toml", repo)["warnings"]

    assert any("user.email" in w for w in warnings)
```

Note: the `repo` fixture leaves `user.email` unset locally, but a global identity may exist on the developer's machine and satisfy the check. Make `_git_identity_missing` read with `git config --local` in Step 3 so the test is deterministic regardless of the machine running it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3 -m pytest harness/tests/test_bootstrap.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bootstrap'`

- [ ] **Step 3: Write `harness/bootstrap.py`**

```python
"""What a fresh clone needs before the factory can do anything.

Cloning this repo onto a second computer produced a factory that loaded, said
nothing, and could not delegate: every path in the tracked config pointed at
the first machine, the git hooks were unarmed, and git had no identity to
commit with. Documentation covered two of those three and none of them held.

Idempotent by construction: arms what is not armed, creates only what is
absent, and reports the rest instead of guessing. Always succeeds -- this is a
setup helper, not a gate.
"""
import subprocess
from pathlib import Path

from factory_config import (LOCAL_FILENAME, ProjectNotOnThisMachine,
                            load_llama_server, load_projects,
                            local_config_path, resolve_project)

HEADER = """# This machine's coordinates: where things live on this computer.
# Never committed -- factory.toml carries the perimeter and the doctrine,
# this file carries the paths. Uncomment and fill what this machine has.
"""


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def run(config_path, repo_root):
    """Arm the hooks, scaffold the local config, report what is missing."""
    config_path, repo_root = Path(config_path), Path(repo_root)
    warnings = []

    _git(repo_root, "config", "core.hooksPath", ".githooks")

    if not _git(repo_root, "config", "--local", "user.email").stdout.strip():
        warnings.append(
            "git has no user.email on this clone; commits will be refused "
            "(git config user.email \"...\" and user.name \"...\")")

    local = local_config_path(config_path)
    created = not local.exists()
    if created:
        body = [HEADER]
        for name in sorted(load_projects(config_path)):
            body.append("[projects.{}]\n# path = \"C:/path/to/{}\"\n".format(
                name, name))
        body.append("[llama_server]\n# exe = \"C:/path/to/llama-server.exe\"\n")
        local.write_text("\n".join(body), encoding="utf-8")

    for name in sorted(load_projects(config_path)):
        try:
            resolve_project(load_projects(config_path), name)
        except ProjectNotOnThisMachine:
            warnings.append(
                "{}: no path on this machine -- add it to {}".format(
                    name, LOCAL_FILENAME))
        except Exception as e:                # not a git repo, bad runner...
            warnings.append("{}: {}".format(name, e))

    warnings.extend(_runtime_warnings(config_path))
    return {"hooks_path": ".githooks", "local_created": created,
            "warnings": warnings}


def _runtime_warnings(config_path):
    """llama-server and its weights: gigabytes this cannot install."""
    out = []
    try:
        server = load_llama_server(config_path)
    except Exception:
        return out                            # no [llama_server]: Ollama lane
    if server is None:
        return out
    if not Path(server["exe"]).exists():
        out.append("llama_server.exe not found: {}".format(server["exe"]))
    for name, entry in sorted(server["models"].items()):
        if not Path(entry["path"]).exists():
            out.append("model {}: weights not found at {}".format(
                name, entry["path"]))
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3 -m pytest harness/tests/test_bootstrap.py -q`
Expected: PASS, 5 tests.

- [ ] **Step 5: Wire the subcommand**

In `harness/factory_cli.py`, add to the imports:

```python
import bootstrap
```

In `_parser()`, after the `regress` subparser block and before `return ap`:

```python
    sub.add_parser("bootstrap",
                   help="arm the hooks, scaffold factory.local.toml, report "
                        "what this machine is missing")
```

In `main()`, add a branch beside the `goal` and `regress` ones:

```python
        elif a.command == "bootstrap":
            # No Factory: a clone that cannot resolve a project must still be
            # able to run the command that tells it why.
            payload = bootstrap.run(a.config, ROOT)
```

- [ ] **Step 6: Verify the command runs end to end**

Run: `py -3 harness/factory_cli.py bootstrap`
Expected: JSON on stdout with `hooks_path`, `local_created`, `warnings`. Exit 0. On this machine the warnings list should name the missing `llama_server.exe` and the missing model weights.

- [ ] **Step 7: Run the full suite**

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add harness/bootstrap.py harness/factory_cli.py harness/tests/test_bootstrap.py
git commit -F - <<'EOF'
feat(cli): one command to make a fresh clone work

Arms core.hooksPath, scaffolds factory.local.toml from the declared projects,
and reports what the host is missing: pathless projects, absent llama-server,
absent weights, and an unset git identity -- which is exactly what refused the
commit carrying this feature's spec.

Idempotent, never overwrites, always exits 0. It is a setup helper, not a gate.

EOF
```

---

### Task 8: Migrate the repository itself

Everything so far is machinery. This is the change that makes this clone work.

**Files:**
- Modify: `factory.toml`, `.gitignore`, `CLAUDE.md`
- Create: `factory.local.toml` (untracked — verify it never reaches the index)

- [ ] **Step 1: Delete the two dead project entries**

In `factory.toml`, remove `[projects.crgpd-rerun]` (lines 34-41, including the `TEMPORARY (2026-07-17)` comment block that asks for its own removal) and `[projects.factory-demo]` (lines 189-192). Six real projects remain: `local-factory`, `lucena`, `conformergpd`, `blitzvolley`, `opti_chess`, `GPT_code`.

- [ ] **Step 2: Write `factory.local.toml` for this machine**

The verified layout on this computer is `C:/Users/me/Projects/<name>`. Create `factory.local.toml`:

```toml
# This machine's coordinates: where things live on this computer.
# Never committed -- factory.toml carries the perimeter and the doctrine.

[projects.local-factory]
path = "C:/Users/me/Projects/local-factory"

[projects.lucena]
path = "C:/Users/me/Projects/lucena"

[projects.conformergpd]
path = "C:/Users/me/Projects/conforme-rgpd"

[projects.blitzvolley]
path = "C:/Users/me/Projects/blitzvolley"

[projects.opti_chess]
path = "C:/Users/me/Projects/opti_chess"

[projects.GPT_code]
path = "C:/Users/me/Projects/GPT_code"

# llama.cpp and the weights are not installed on this machine yet. Fill these
# in when they are; until then the local lane stays out of service and
# `bootstrap` says so.
# [llama_server]
# exe = "C:/path/to/llama-server.exe"
```

- [ ] **Step 3: Strip the 10 machine keys from `factory.toml`**

Remove: the six remaining `projects.*.path` lines, `llama_server.exe` (line 158), and the three `llama_server.models.*.path` lines (173, 182, 186). Keep every comment — including the Ollama-blob comment, which now documents what belongs in the local file, and the `--n-cpu-moe` / `ctx` values, which stay as the 12 GB reference an other machine overrides.

Under `[llama_server]`, the `exe` key is gone but `port`, `ctx` and `agent_max_iterations` stay.

- [ ] **Step 4: Ignore the local file**

Append to `.gitignore`, beside the existing `secrets.toml` entry (line 70):

```
factory.local.toml
```

- [ ] **Step 5: Verify the split loads**

Run: `py -3 -c "import sys; sys.path.insert(0, 'harness'); from factory_config import load_projects, resolve_project; print(sorted(load_projects('factory.toml'))); print(resolve_project(load_projects('factory.toml'), 'local-factory').path)"`
Expected: the six project names, then the resolved absolute path of this repo. If `ConfigError` names a key, Step 3 missed it.

- [ ] **Step 6: Verify the local file is not tracked**

Run: `git status --short && git check-ignore -v factory.local.toml`
Expected: `factory.local.toml` absent from `git status`, and `check-ignore` reports the `.gitignore` line that covers it.

- [ ] **Step 7: Run bootstrap and the full suite**

Run: `py -3 harness/factory_cli.py bootstrap`
Expected: `local_created: false` (Step 2 wrote it), hooks armed, warnings naming the missing llama-server and weights.

Run: `git config core.hooksPath`
Expected: `.githooks`

Run: `py -3 -m pytest harness/tests -q -m "not browser"`
Expected: PASS.

- [ ] **Step 8: Record the per-clone ritual in CLAUDE.md**

In `CLAUDE.md`, the "Garde-fous git" bullet says the hooks are armed `une fois par clone` by hand. Replace that parenthetical with a pointer to the command, and add a line naming `factory.local.toml` as the file a new clone must fill. Keep it to two sentences — the spec holds the reasoning.

- [ ] **Step 9: Commit**

```bash
git add factory.toml .gitignore CLAUDE.md
git commit -F - <<'EOF'
chore: factory.toml stops carrying this computer's addresses

Ten coordinates out to factory.local.toml, which is gitignored: six project
paths, llama_server.exe, three model paths. The comments stay where the values
they explain stay -- the 12 GB tuning is now a documented reference an other
machine overrides rather than a fact about everyone's hardware.

crgpd-rerun goes, as its own comment asked in July. factory-demo goes with it:
a throwaway target from the 22/07 studio audit that add_project committed on
its own behalf.

EOF
```

---

## Verification

After Task 8, the whole thing is proved by one sequence:

```bash
py -3 -m pytest harness/tests -q          # green
py -3 harness/factory_cli.py bootstrap    # hooks armed, warnings honest
git status --short                        # factory.local.toml invisible
```

And the property that started all of this: `git clone` this repo somewhere else, run `bootstrap`, and the output names every single thing the new machine is missing instead of failing silently at the first delegate.

## Out of scope

- Installing llama.cpp, the Ollama blobs or the 35b GGUF. `bootstrap` reports them; it does not fetch gigabytes.
- Resolving Ollama models by name instead of by pinned blob sha. It would remove the re-pull trap the spec documents, but it changes how the ladder finds weights — separate change, separate plan.

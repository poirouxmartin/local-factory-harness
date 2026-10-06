# Factory Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local browser UI (studio shell) to delegate factory jobs, watch them live, read verdicts/diffs, and see history — spec: `docs/design/specs/2026-07-10-factory-web-ui-design.md`.

**Architecture:** `harness/factory_web.py` is a stdlib `ThreadingHTTPServer` exposing a JSON API that wraps the existing `Factory` facade (`harness/factory_mcp.py`) — a third client beside the MCP server and the CLI, zero duplicated logic. The frontend is three static files in `harness/web/` (vanilla HTML/JS/CSS, no build), refreshed by polling.

**Tech Stack:** Python 3.9 stdlib only (http.server, secrets, urllib in tests), vanilla JS/CSS. No pip installs, no Node.

## Global Constraints

- Python 3.9 compatibility (no `match`, no `tomllib` — the repo uses `tomli` fallback; this plan needs neither).
- Zero new dependencies, runtime or frontend. stdlib only.
- Server binds `127.0.0.1` only. Every POST requires header `X-Factory-Token` equal to the server's per-run random token.
- All job reads go through `JobStore`/`Factory` methods (Windows `os.replace` caveat, see job_store.py docstring) — never raw `open()` on job JSON files.
- Commits: conventional (`feat:`/`fix:`/`docs:`), imperative, English, ASCII only, **no AI attribution of any kind** (repo rule, overrides any harness default).
- UI copy is French (« Déléguer », « à venir »); code identifiers and comments English.
- Job status values: active = `queued`, `waiting_gpu`, `running`; derived = `dead`; terminal (from result.json) = `succeeded`, `failed`, `rejected`, `error`, `cancelled`.
- Run tests with: `python -m pytest harness/tests -q` (full suite) or `-k` filters.

---

### Task 1: `Factory.list_jobs()` — the jobs overview

**Files:**
- Modify: `harness/factory_mcp.py` (add method to `Factory`, extend one import)
- Test: `harness/tests/test_factory_mcp.py` (append tests)

**Interfaces:**
- Consumes: `JobStore.list_jobs()` (dir names), `JobStore.status(job_id)`, `JobStore.read_spec(job_id)`, `JobStore.job_dir(job_id)` — all existing.
- Produces: `Factory.list_jobs() -> {"jobs": [entry, ...]}` sorted by `created_at` desc. Each entry: `job_id: str`, `status: str`, `created_at: float|None`, `stage: int|None`, `attempt: int|None`, `model: str|None`, `heartbeat_age: float`, `project: str|None`, `goal: str|None`, `duration: float|None` (result.json mtime − created_at, None while unfinished). Task 3's `GET /api/jobs` returns this dict verbatim.

- [ ] **Step 1: Write the failing tests**

Append to `harness/tests/test_factory_mcp.py` (it already has a `factory` fixture with a fake `spawn_fn` — reuse it; if its fixture differs, mirror the one in `test_factory_cli.py`):

```python
def test_list_jobs_returns_newest_first_with_spec_fields(factory):
    a = factory.store.create({"project": "demo", "goal": "first", "tests": {},
                              "target_files": [], "context_files": []}, now=100.0)
    b = factory.store.create({"project": "demo", "goal": "second", "tests": {},
                              "target_files": [], "context_files": []}, now=200.0)
    jobs = factory.list_jobs()["jobs"]
    assert [j["job_id"] for j in jobs] == [b, a]
    assert jobs[1]["goal"] == "first"
    assert jobs[1]["project"] == "demo"
    assert jobs[1]["status"] == "queued"
    assert jobs[1]["duration"] is None


def test_list_jobs_computes_duration_from_result_mtime(factory):
    job_id = factory.store.create({"project": "demo", "goal": "g", "tests": {},
                                   "target_files": [], "context_files": []}, now=100.0)
    factory.store.write_result(job_id, {"status": "succeeded", "diff": ""})
    entry = factory.list_jobs()["jobs"][0]
    assert entry["status"] == "succeeded"
    assert entry["duration"] is not None and entry["duration"] > 0


def test_list_jobs_skips_a_half_written_job_dir(factory):
    factory.store.create({"project": "demo", "goal": "ok", "tests": {},
                          "target_files": [], "context_files": []})
    (factory.jobs_root / "j_broken").mkdir()  # dir without state.json/spec.json
    jobs = factory.list_jobs()["jobs"]
    assert [j["goal"] for j in jobs] == ["ok"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest harness/tests/test_factory_mcp.py -k list_jobs -v`
Expected: 3 FAILED with `AttributeError: 'Factory' object has no attribute 'list_jobs'`

- [ ] **Step 3: Implement**

In `harness/factory_mcp.py`, change the job_store import line to:

```python
from job_store import JobNotFound, JobStore, JobStoreError, ResultExists
```

Add to the `Factory` class (after `job_cancel`):

```python
    def list_jobs(self):
        """Every job the store knows, newest first. Tolerates foreign or
        half-written directories: a broken job must not blank the whole list."""
        jobs = []
        for job_id in self.store.list_jobs():
            try:
                status = self.store.status(job_id)
                spec = self.store.read_spec(job_id)
            except (JobStoreError, ValueError, KeyError):
                continue
            entry = {"job_id": job_id, "status": status["status"],
                     "created_at": status.get("created_at"),
                     "stage": status.get("stage"), "attempt": status.get("attempt"),
                     "model": status.get("model"),
                     "heartbeat_age": status.get("heartbeat_age"),
                     "project": spec.get("project"), "goal": spec.get("goal"),
                     "duration": None}
            result_path = self.store.job_dir(job_id) / "result.json"
            if result_path.exists() and entry["created_at"] is not None:
                entry["duration"] = result_path.stat().st_mtime - entry["created_at"]
            jobs.append(entry)
        jobs.sort(key=lambda j: j["created_at"] or 0, reverse=True)
        return {"jobs": jobs}
```

Note: `json.JSONDecodeError` is a `ValueError` subclass; `JobNotFound` and `ResultExists` are `JobStoreError` subclasses — the except clause covers a dir missing its files, corrupt JSON, and a state.json missing `status`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest harness/tests/test_factory_mcp.py -v`
Expected: all PASS (new and pre-existing).

- [ ] **Step 5: Full suite, then commit**

Run: `python -m pytest harness/tests -q` — expected: all pass.

```bash
git add harness/factory_mcp.py harness/tests/test_factory_mcp.py
git commit -m "feat: list every job with spec fields and duration"
```

---

### Task 2: HTTP server core — routing, token, errors, static files

**Files:**
- Create: `harness/factory_web.py`
- Create: `harness/web/index.html` (minimal placeholder; Task 4 replaces it)
- Test: `harness/tests/test_factory_web.py`

**Interfaces:**
- Consumes: `Factory(jobs_root, config_path, spawn_fn=None)` from `factory_mcp`; `JobNotFound` from `job_store`; `ConfigError`, `load_projects`, `resolve_project` from `factory_config`.
- Produces: `FactoryWebServer(("127.0.0.1", port), factory, token=None)` (a `ThreadingHTTPServer`; port 0 = ephemeral, real port in `server.server_address[1]`; auto-generated `server.token` if none given). `main(argv=None) -> int` with `--jobs --config --port --no-browser`. Route table `ROUTES`; JSON errors `{"error": <ExcName>, "detail": <str>}`. Task 3 adds its handlers to `ROUTES`.

- [ ] **Step 1: Write the failing tests**

Create `harness/tests/test_factory_web.py`:

```python
"""The web UI server: a third client of the Factory facade.

Boundary under test: HTTP in, JSON (or static bytes) out. The Factory behind it
is exercised through the same fake-spawn fixture the CLI tests use."""
import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from factory_mcp import Factory
from factory_web import FactoryWebServer

TEST_SRC = "def test_x():\n    assert True\n"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "f@f.f")
    git(root, "config", "user.name", "factory")
    (root / "calc.py").write_text("x = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "seed")
    return root


@pytest.fixture
def factory(tmp_path, project):
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[projects.demo]\npath = "{}"\nrunner = "pytest"\n'.format(project.as_posix()))
    spawned = []
    f = Factory(tmp_path / "jobs", cfg, spawn_fn=lambda job_id: spawned.append(job_id) or 4242)
    f.spawned = spawned
    return f


@pytest.fixture
def server(factory):
    srv = FactoryWebServer(("127.0.0.1", 0), factory, token="t0k3n")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, body=None, token=None, raw=False):
    url = "http://127.0.0.1:{}{}".format(srv.server_address[1], path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Factory-Token", token)
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = e.read()
        return e.code, payload if raw else json.loads(payload.decode("utf-8"))


def test_unknown_path_is_a_json_404(server):
    code, body = request(server, "GET", "/nope")
    assert code == 404
    assert body["error"] == "NotFound"


def test_index_embeds_the_token(server):
    code, body = request(server, "GET", "/", raw=True)
    assert code == 200
    assert b"t0k3n" in body
    assert b"{{TOKEN}}" not in body


def test_static_serves_files_from_web_dir_only(server):
    code, _ = request(server, "GET", "/static/index.html", raw=True)
    assert code == 200
    code, _ = request(server, "GET", "/static/..%2Ffactory_web.py")
    assert code == 404


def test_post_without_token_is_403(server):
    code, body = request(server, "POST", "/api/jobs", body={})
    assert code == 403
    assert body["error"] == "Forbidden"
    code, _ = request(server, "POST", "/api/jobs", body={}, token="wrong")
    assert code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest harness/tests/test_factory_web.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'factory_web'` (harness/tests/conftest.py already puts `harness/` on the path for sibling modules — if collection fails differently, check conftest first).

- [ ] **Step 3: Implement the server core**

Create `harness/web/index.html` (placeholder, replaced in Task 4):

```html
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>local-factory</title>
<meta name="factory-token" content="{{TOKEN}}"></head>
<body>studio shell arrives in Task 4</body></html>
```

Create `harness/factory_web.py`:

```python
"""Web UI server: the factory in a browser, third client of the Factory facade.

Stdlib only, like everything here. Binds 127.0.0.1 and nothing else. Every POST
must carry X-Factory-Token matching this run's random token, which is embedded
in the served page: a drive-by website can reach localhost, but cannot read the
token, so it cannot mutate anything.

    py -3 harness/factory_web.py            # opens the browser
    py -3 harness/factory_web.py --port 9000 --no-browser
"""
import argparse
import json
import re
import secrets
import subprocess
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from factory_config import ConfigError, load_projects, resolve_project
from factory_mcp import Factory
from job_store import JobNotFound

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).with_name("web")
MIME = {".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = "factory-web"

    def log_message(self, fmt, *args):
        pass  # the terminal belongs to the operator, not to an access log

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        url = urlparse(self.path)
        try:
            for pattern, verb, fn in ROUTES:
                m = re.fullmatch(pattern, url.path)
                if m and verb == method:
                    if method == "POST" and \
                            self.headers.get("X-Factory-Token") != self.server.token:
                        return self._json(403, {"error": "Forbidden",
                                                "detail": "missing or wrong X-Factory-Token"})
                    return fn(self, *m.groups(), query=parse_qs(url.query))
            self._json(404, {"error": "NotFound", "detail": url.path})
        except JobNotFound as e:
            self._json(404, {"error": "JobNotFound", "detail": str(e)})
        except (ConfigError, KeyError, TypeError, ValueError) as e:
            self._json(400, {"error": type(e).__name__, "detail": str(e)})
        except Exception as e:  # a handler bug must answer JSON, not a stack page
            self._json(500, {"error": type(e).__name__, "detail": str(e)})

    def _json(self, code, payload):
        self._bytes(code, json.dumps(payload).encode("utf-8"),
                    "application/json; charset=utf-8")

    def _bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    # ---- pages ----

    def _index(self, query=None):
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        self._bytes(200, html.replace("{{TOKEN}}", self.server.token).encode("utf-8"),
                    MIME[".html"])

    def _static(self, name, query=None):
        path = WEB_DIR / name  # the route regex already forbids separators
        if not path.is_file():
            return self._json(404, {"error": "NotFound", "detail": name})
        self._bytes(200, path.read_bytes(),
                    MIME.get(path.suffix, "application/octet-stream"))


ROUTES = [
    (r"/", "GET", Handler._index),
    (r"/static/([\w.-]+)", "GET", Handler._static),
]


class FactoryWebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, factory, token=None):
        super().__init__(addr, Handler)
        self.factory = factory
        self.token = token or secrets.token_hex(16)


def main(argv=None):
    ap = argparse.ArgumentParser(description="The factory in a browser.")
    ap.add_argument("--jobs", default=str(ROOT / "jobs"))
    ap.add_argument("--config", default=str(ROOT / "factory.toml"))
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    server = FactoryWebServer(("127.0.0.1", a.port), Factory(a.jobs, a.config))
    url = "http://127.0.0.1:{}/".format(server.server_address[1])
    print("factory web ui: {}".format(url))
    if not a.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Note `test_post_without_token_is_403` needs a POST route in `ROUTES` to reach the token check. Add the Task 3 stub now so the test passes for the right reason:

```python
    # ---- api (bodies land in Task 3) ----

    def _delegate(self, query=None):
        raise NotImplementedError
```

and in `ROUTES`:

```python
    (r"/api/jobs", "POST", Handler._delegate),
```

(the 403 fires before the handler runs, so the stub is never reached without a valid token).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest harness/tests/test_factory_web.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/factory_web.py harness/web/index.html harness/tests/test_factory_web.py
git commit -m "feat: web server core -- routing, csrf token, static files"
```

---

### Task 3: The JSON API — projects, jobs, delegate, lifecycle, repo-files

**Files:**
- Modify: `harness/factory_web.py` (fill `_delegate`, add handlers + routes)
- Test: `harness/tests/test_factory_web.py` (append)

**Interfaces:**
- Consumes: `Factory.delegate(project, goal, tests, target_files, context_files=None)`, `.job_status/.job_result/.job_cancel(job_id)`, `.job_log(job_id, tail)`, `.list_jobs()` (Task 1); `load_projects(path) -> dict`, `resolve_project(projects, name) -> Project(name, path, runner, regression_cmd)`.
- Produces (Task 4's frontend contract):
  - `GET /api/projects` → `{"projects": ["demo", ...]}` (sorted keys)
  - `GET /api/jobs` → Task 1 dict; `POST /api/jobs` body `{"project", "goal", "tests": {name: content}, "target_files": [...], "context_files": [...]}` → `{"job_id", "status": "queued"}`
  - `GET /api/jobs/{id}` → status dict; `/result` → result dict (`ready` bool); `/log?tail=N` → `{"job_id", "log"}`; `POST /api/jobs/{id}/cancel` → `{"job_id", "status": "cancelled"}`
  - `GET /api/repo-files?project=X` → `{"files": [repo-relative paths from git ls-files]}`

- [ ] **Step 1: Write the failing tests**

Append to `harness/tests/test_factory_web.py`:

```python
def test_projects_lists_config_keys(server):
    code, body = request(server, "GET", "/api/projects")
    assert (code, body) == (200, {"projects": ["demo"]})


def test_delegate_queues_a_job_and_jobs_lists_it(server, factory):
    code, body = request(server, "POST", "/api/jobs", token="t0k3n", body={
        "project": "demo", "goal": "make it work",
        "tests": {"test_x.py": TEST_SRC}, "target_files": ["calc.py"]})
    assert code == 200
    job_id = body["job_id"]
    assert factory.spawned == [job_id]
    code, body = request(server, "GET", "/api/jobs")
    assert code == 200
    assert [j["job_id"] for j in body["jobs"]] == [job_id]
    assert body["jobs"][0]["goal"] == "make it work"


def test_delegate_refusal_is_a_400_with_the_reason(server):
    code, body = request(server, "POST", "/api/jobs", token="t0k3n", body={
        "project": "demo", "goal": "g", "tests": {}, "target_files": ["calc.py"]})
    assert code == 400
    assert body["error"] == "NoTests"


def test_job_lifecycle_endpoints(server, factory):
    _, body = request(server, "POST", "/api/jobs", token="t0k3n", body={
        "project": "demo", "goal": "g",
        "tests": {"test_x.py": TEST_SRC}, "target_files": ["calc.py"]})
    job_id = body["job_id"]

    code, body = request(server, "GET", "/api/jobs/{}".format(job_id))
    assert (code, body["status"]) == (200, "queued")

    code, body = request(server, "GET", "/api/jobs/{}/result".format(job_id))
    assert (code, body["ready"]) == (200, False)

    factory.store.append_log(job_id, "line1\nline2\n")
    code, body = request(server, "GET", "/api/jobs/{}/log?tail=1".format(job_id))
    assert (code, body["log"]) == (200, "line2")

    code, body = request(server, "POST", "/api/jobs/{}/cancel".format(job_id),
                         token="t0k3n", body={})
    assert (code, body["status"]) == (200, "cancelled")


def test_unknown_job_is_404(server):
    code, body = request(server, "GET", "/api/jobs/j_00000000")
    assert (code, body["error"]) == (404, "JobNotFound")


def test_repo_files_lists_tracked_files(server):
    code, body = request(server, "GET", "/api/repo-files?project=demo")
    assert (code, body) == (200, {"files": ["calc.py"]})


def test_repo_files_unknown_project_is_400(server):
    code, body = request(server, "GET", "/api/repo-files?project=nope")
    assert (code, body["error"]) == (400, "ProjectNotAllowed")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest harness/tests/test_factory_web.py -v`
Expected: Task 2's 4 tests PASS; the new ones FAIL (404 NotFound on missing routes, 500 NotImplementedError on delegate).

- [ ] **Step 3: Implement the handlers**

In `harness/factory_web.py`, replace the `_delegate` stub and add the API section:

```python
    # ---- api ----

    def _projects(self, query=None):
        self._json(200, {"projects": sorted(load_projects(self.server.factory.config_path))})

    def _jobs(self, query=None):
        self._json(200, self.server.factory.list_jobs())

    def _delegate(self, query=None):
        b = self._body()
        self._json(200, self.server.factory.delegate(
            b["project"], b["goal"], b["tests"], b["target_files"],
            b.get("context_files")))

    def _job_status(self, job_id, query=None):
        self._json(200, self.server.factory.job_status(job_id))

    def _job_result(self, job_id, query=None):
        self._json(200, self.server.factory.job_result(job_id))

    def _job_log(self, job_id, query=None):
        tail = int((query.get("tail") or ["100"])[0])
        self._json(200, self.server.factory.job_log(job_id, tail))

    def _job_cancel(self, job_id, query=None):
        self._json(200, self.server.factory.job_cancel(job_id))

    def _repo_files(self, query=None):
        name = (query.get("project") or [""])[0]
        project = resolve_project(load_projects(self.server.factory.config_path), name)
        out = subprocess.run(["git", "-C", str(project.path), "ls-files"],
                             capture_output=True, text=True, check=True)
        self._json(200, {"files": out.stdout.splitlines()})
```

Replace `ROUTES` with the full table:

```python
ROUTES = [
    (r"/", "GET", Handler._index),
    (r"/static/([\w.-]+)", "GET", Handler._static),
    (r"/api/projects", "GET", Handler._projects),
    (r"/api/jobs", "GET", Handler._jobs),
    (r"/api/jobs", "POST", Handler._delegate),
    (r"/api/jobs/(j_\w+)", "GET", Handler._job_status),
    (r"/api/jobs/(j_\w+)/result", "GET", Handler._job_result),
    (r"/api/jobs/(j_\w+)/log", "GET", Handler._job_log),
    (r"/api/jobs/(j_\w+)/cancel", "POST", Handler._job_cancel),
    (r"/api/repo-files", "GET", Handler._repo_files),
]
```

(`re.fullmatch` keeps `/api/jobs/j_x` and `/api/jobs/j_x/result` disjoint; a `KeyError` from a missing body field maps to 400 via the `_route` except clause.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest harness/tests/test_factory_web.py -v`
Expected: all PASS.

- [ ] **Step 5: Full suite, then commit**

Run: `python -m pytest harness/tests -q` — expected: all pass.

```bash
git add harness/factory_web.py harness/tests/test_factory_web.py
git commit -m "feat: json api over the factory facade"
```

---

### Task 4: The studio shell frontend

**Files:**
- Modify: `harness/web/index.html` (replace the placeholder)
- Create: `harness/web/app.js`
- Create: `harness/web/style.css`

**Interfaces:**
- Consumes: every route from Task 3, token via `<meta name="factory-token">`.
- Produces: hash-routed views `#jobs` (default), `#jobs/<id>`, `#delegate`, `#dashboard`. No JS test suite (by design, no Node) — verified by Task 5's smoke test.

- [ ] **Step 1: Replace `harness/web/index.html`**

```html
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>local-factory — studio</title>
<meta name="factory-token" content="{{TOKEN}}">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<nav>
  <h1>local-factory</h1>
  <a href="#jobs" data-view="jobs">Jobs</a>
  <a href="#delegate" data-view="delegate">Déléguer</a>
  <a href="#dashboard" data-view="dashboard">Dashboard</a>
  <div class="soon" title="à venir">Goals</div>
  <div class="soon" title="à venir">Benchmarks</div>
  <div class="soon" title="à venir">Agents</div>
</nav>
<main id="main"></main>
<div id="error" hidden></div>
<script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create `harness/web/app.js`**

```javascript
"use strict";
const TOKEN = document.querySelector('meta[name="factory-token"]').content;
const MAIN = document.getElementById("main");
const ERR = document.getElementById("error");
const ACTIVE = ["queued", "waiting_gpu", "running"];
const TERMINAL = ["succeeded", "failed", "rejected", "error", "cancelled", "dead"];

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if ((opts.method || "GET") !== "GET") headers["X-Factory-Token"] = TOKEN;
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error + ": " + data.detail);
  return data;
}

function showError(e) {
  ERR.hidden = false;
  ERR.textContent = e.message;
  setTimeout(() => { ERR.hidden = true; }, 8000);
}

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v);
  }
  el.append(...children);
  return el;
}

const fmtWhen = (ts) => ts ? new Date(ts * 1000).toLocaleString("fr-FR") : "—";
const fmtDur = (s) => s == null ? "—"
  : s < 60 ? Math.round(s) + " s"
  : Math.floor(s / 60) + " min " + Math.round(s % 60) + " s";
const badge = (status) => h("span", { class: "badge " + status, text: status });

// ---- Jobs list ----

async function renderJobs() {
  const { jobs } = await api("/api/jobs");
  const rows = jobs.map((j) => h("tr", { onclick: () => { location.hash = "jobs/" + j.job_id; } },
    h("td", { text: j.job_id }),
    h("td", { text: j.project || "—" }),
    h("td", { class: "goal", text: (j.goal || "").slice(0, 80) }),
    h("td", {}, badge(j.status)),
    h("td", { text: j.model ? j.model + " (essai " + (j.attempt || "?") + ")" : "—" }),
    h("td", { text: fmtWhen(j.created_at) }),
    h("td", { text: fmtDur(j.duration) })));
  MAIN.replaceChildren(
    h("h2", { text: "Jobs" }),
    jobs.length === 0 ? h("p", { class: "empty", text: "Aucun job. Passe par « Déléguer »." })
      : h("table", {},
          h("thead", {}, h("tr", {},
            ...["id", "projet", "goal", "état", "modèle", "créé", "durée"]
              .map((t) => h("th", { text: t })))),
          h("tbody", {}, ...rows)));
  return jobs.some((j) => ACTIVE.includes(j.status));
}

// ---- Job detail ----

function renderDiff(diff) {
  const pre = h("pre", { class: "diff" });
  for (const line of (diff || "").split("\n")) {
    const cls = line.startsWith("+") ? "add" : line.startsWith("-") ? "del"
      : line.startsWith("@@") ? "hunk" : "";
    pre.append(h("span", { class: cls, text: line + "\n" }));
  }
  return pre;
}

async function renderJob(jobId) {
  const status = await api("/api/jobs/" + jobId);
  const log = await api("/api/jobs/" + jobId + "/log?tail=200");
  const active = ACTIVE.includes(status.status);
  const head = h("div", { class: "banner" },
    h("h2", { text: jobId }), badge(status.status),
    h("span", { text: status.model ? "étage " + (status.stage || "?") + " · " +
      status.model + " · essai " + (status.attempt || "?") : "" }),
    active ? h("button", { class: "danger", text: "Annuler", onclick: async () => {
      try { await api("/api/jobs/" + jobId + "/cancel", { method: "POST" }); tick(); }
      catch (e) { showError(e); }
    } }) : "");
  const parts = [head,
    h("h3", { text: "Log" }),
    h("pre", { class: "log", text: log.log || "(vide)" })];
  if (!active) {
    const result = await api("/api/jobs/" + jobId + "/result");
    if (result.ready) {
      parts.push(h("h3", { text: "Verdict" }),
        h("div", { class: "banner" }, badge(result.status),
          result.review ? h("span", { text: "reviewer : " + result.review.verdict }) : ""));
      if (result.review && result.review.notes)
        parts.push(h("pre", { class: "log", text: result.review.notes }));
      if (result.diff) {
        parts.push(h("h3", { text: "Diff" }),
          h("div", {},
            h("button", { text: "Copier", onclick: () =>
              navigator.clipboard.writeText(result.diff) }),
            h("a", { class: "button", download: jobId + ".patch",
                     href: URL.createObjectURL(new Blob([result.diff])), text: "Télécharger" })),
          renderDiff(result.diff));
      }
    } else {
      parts.push(h("p", { class: "empty", text: result.error || "Pas de résultat." }));
    }
  }
  MAIN.replaceChildren(...parts);
  return active;
}

// ---- Delegate ----

function filePicker(title) {
  const state = { files: [], chosen: new Set() };
  const list = h("div", { class: "files" });
  const filter = h("input", { placeholder: "filtrer…", oninput: draw });
  function draw() {
    const q = filter.value.toLowerCase();
    list.replaceChildren(...state.files.filter((f) => f.toLowerCase().includes(q))
      .slice(0, 200).map((f) => {
        const cb = h("input", { type: "checkbox" });
        cb.checked = state.chosen.has(f);
        cb.addEventListener("change", () =>
          cb.checked ? state.chosen.add(f) : state.chosen.delete(f));
        return h("label", {}, cb, h("span", { text: f }));
      }));
  }
  const root = h("fieldset", {}, h("legend", { text: title }), filter, list);
  return { root, set files(v) { state.files = v; state.chosen.clear(); draw(); },
           get chosen() { return [...state.chosen]; } };
}

async function renderDelegate() {
  const { projects } = await api("/api/projects");
  const project = h("select", {}, ...projects.map((p) => h("option", { text: p })));
  const goal = h("textarea", { rows: 3, placeholder: "Ce que le changement doit accomplir" });
  const tests = h("div", {});
  const addTest = () => tests.append(h("div", { class: "test-block" },
    h("input", { class: "test-name", placeholder: "test_exemple.py", value: "" }),
    h("textarea", { class: "test-src", rows: 10,
      placeholder: "def test_...():\n    assert ..." })));
  addTest();
  const targets = filePicker("Targets (le modèle peut les écrire)");
  const contexts = filePicker("Context (lecture seule)");
  async function loadFiles() {
    try {
      const { files } = await api("/api/repo-files?project=" +
        encodeURIComponent(project.value));
      targets.files = files;
      contexts.files = files;
    } catch (e) { showError(e); }
  }
  project.addEventListener("change", loadFiles);
  if (projects.length) loadFiles();
  const submit = h("button", { text: "Déléguer", onclick: async () => {
    const testMap = {};
    for (const block of tests.children) {
      const name = block.querySelector(".test-name").value.trim();
      const src = block.querySelector(".test-src").value;
      if (name) testMap[name] = src;
    }
    try {
      const r = await api("/api/jobs", { method: "POST", body: JSON.stringify({
        project: project.value, goal: goal.value, tests: testMap,
        target_files: targets.chosen, context_files: contexts.chosen }) });
      location.hash = "jobs/" + r.job_id;
    } catch (e) { showError(e); }
  } });
  MAIN.replaceChildren(h("h2", { text: "Déléguer" }),
    h("label", { text: "Projet" }), project,
    h("label", { text: "Goal" }), goal,
    h("label", { text: "Tests (le juge)" }), tests,
    h("button", { class: "ghost", text: "+ test", onclick: addTest }),
    targets.root, contexts.root, submit);
  return false;
}

// ---- Dashboard ----

async function renderDashboard() {
  const { jobs } = await api("/api/jobs");
  const byStatus = {};
  const byModel = {};
  let durSum = 0, durN = 0;
  for (const j of jobs) {
    byStatus[j.status] = (byStatus[j.status] || 0) + 1;
    if (j.model && TERMINAL.includes(j.status)) {
      byModel[j.model] = byModel[j.model] || { ok: 0, total: 0 };
      byModel[j.model].total += 1;
      if (j.status === "succeeded") byModel[j.model].ok += 1;
    }
    if (j.duration != null) { durSum += j.duration; durN += 1; }
  }
  const failures = jobs.filter((j) =>
    ["failed", "rejected", "error", "dead"].includes(j.status)).slice(0, 5);
  MAIN.replaceChildren(
    h("h2", { text: "Dashboard" }),
    h("div", { class: "cards" },
      h("div", { class: "card" }, h("b", { text: String(jobs.length) }),
        h("span", { text: "jobs au total" })),
      h("div", { class: "card" }, h("b", { text: String(byStatus.succeeded || 0) }),
        h("span", { text: "succès" })),
      h("div", { class: "card" }, h("b", { text: fmtDur(durN ? durSum / durN : null) }),
        h("span", { text: "durée moyenne" }))),
    h("h3", { text: "Par modèle" }),
    h("table", {}, h("thead", {}, h("tr", {},
      ...["modèle", "succès", "jobs"].map((t) => h("th", { text: t })))),
      h("tbody", {}, ...Object.entries(byModel).map(([m, s]) =>
        h("tr", {}, h("td", { text: m }),
          h("td", { text: Math.round(100 * s.ok / s.total) + " %" }),
          h("td", { text: String(s.total) }))))),
    h("h3", { text: "Derniers échecs" }),
    ...failures.map((j) => h("p", {},
      h("a", { href: "#jobs/" + j.job_id, text: j.job_id }), " ", badge(j.status),
      h("span", { text: " " + (j.goal || "").slice(0, 60) }))));
  return false;
}

// ---- Router + polling ----

let pollTimer = null;

async function render() {
  const hash = location.hash.replace(/^#/, "") || "jobs";
  const [view, jobId] = hash.split("/");
  document.querySelectorAll("nav a").forEach((a) =>
    a.classList.toggle("current", a.dataset.view === view));
  let keepPolling = false;
  try {
    if (view === "jobs" && jobId) keepPolling = await renderJob(jobId);
    else if (view === "delegate") keepPolling = await renderDelegate();
    else if (view === "dashboard") keepPolling = await renderDashboard();
    else keepPolling = await renderJobs();
  } catch (e) { showError(e); }
  clearTimeout(pollTimer);
  pollTimer = setTimeout(render, keepPolling ? 2000 : 10000);
}

function tick() { clearTimeout(pollTimer); render(); }

window.addEventListener("hashchange", tick);
tick();
```

Note the delegate view re-renders on the 10 s poll, which would wipe a form
being typed into. Guard it: at the top of `render()`, add

```javascript
  if ((location.hash.replace(/^#/, "") || "jobs") === "delegate" &&
      MAIN.querySelector(".test-src") && document.activeElement &&
      MAIN.contains(document.activeElement)) {
    pollTimer = setTimeout(render, 10000);
    return;
  }
```

- [ ] **Step 3: Create `harness/web/style.css`**

```css
:root {
  --bg: #14161a; --panel: #1d2026; --line: #2c313a; --fg: #d6d9de;
  --dim: #8b919c; --accent: #4f8cc9;
  --green: #3fa66a; --red: #c9564f; --orange: #c98a3f; --purple: #8a6fc9;
}
* { box-sizing: border-box; }
body {
  margin: 0; display: flex; min-height: 100vh;
  background: var(--bg); color: var(--fg);
  font: 14px/1.5 system-ui, sans-serif;
}
nav {
  width: 180px; flex-shrink: 0; padding: 16px;
  background: var(--panel); border-right: 1px solid var(--line);
}
nav h1 { font-size: 15px; margin: 0 0 16px; }
nav a {
  display: block; padding: 6px 8px; border-radius: 6px;
  color: var(--fg); text-decoration: none;
}
nav a.current { background: var(--line); }
nav .soon { padding: 6px 8px; color: var(--dim); }
nav .soon::after { content: " · à venir"; font-size: 11px; }
main { flex: 1; padding: 20px 28px; max-width: 1100px; }
h2 { margin-top: 0; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); }
tbody tr { cursor: pointer; }
tbody tr:hover { background: var(--panel); }
td.goal { color: var(--dim); }
.badge {
  padding: 1px 8px; border-radius: 10px; font-size: 12px;
  background: var(--line); color: var(--fg);
}
.badge.running, .badge.waiting_gpu { background: var(--accent); color: #fff; }
.badge.succeeded { background: var(--green); color: #fff; }
.badge.failed, .badge.error { background: var(--red); color: #fff; }
.badge.rejected { background: var(--orange); color: #fff; }
.badge.dead { background: var(--purple); color: #fff; }
.banner { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.banner h2 { margin: 0; }
pre.log, pre.diff {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px; overflow-x: auto; font-size: 12.5px; white-space: pre-wrap;
}
pre.diff .add { color: var(--green); }
pre.diff .del { color: var(--red); }
pre.diff .hunk { color: var(--accent); }
label { display: block; margin: 12px 0 4px; color: var(--dim); }
input, textarea, select {
  width: 100%; padding: 8px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel); color: var(--fg);
  font: inherit;
}
textarea.test-src, pre { font-family: ui-monospace, Consolas, monospace; }
.test-block { margin-bottom: 10px; }
.test-block .test-name { margin-bottom: 4px; }
fieldset { border: 1px solid var(--line); border-radius: 8px; margin: 14px 0; }
.files { max-height: 180px; overflow-y: auto; margin-top: 6px; }
.files label { display: flex; gap: 8px; margin: 2px 0; color: var(--fg); }
.files input { width: auto; }
button, a.button {
  margin-top: 12px; padding: 8px 16px; border: 0; border-radius: 6px;
  background: var(--accent); color: #fff; cursor: pointer; font: inherit;
  text-decoration: none; display: inline-block;
}
button.danger { background: var(--red); margin-top: 0; }
button.ghost { background: var(--line); }
.cards { display: flex; gap: 12px; margin-bottom: 16px; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 14px 18px; display: flex; flex-direction: column;
}
.card b { font-size: 22px; }
.card span { color: var(--dim); font-size: 12px; }
.empty { color: var(--dim); }
#error {
  position: fixed; bottom: 16px; right: 16px; max-width: 420px;
  background: var(--red); color: #fff; padding: 10px 14px; border-radius: 8px;
}
```

- [ ] **Step 4: Verify the suite still passes and the page serves**

Run: `python -m pytest harness/tests -q` — expected: all pass (the index test only checks token injection, which the new page keeps).

Run: `py -3 harness/factory_web.py --no-browser --port 8787` in one terminal, then in another: `curl -s http://127.0.0.1:8787/static/app.js | head -3` — expected: the JS source. Stop the server (Ctrl+C).

- [ ] **Step 5: Commit**

```bash
git add harness/web/
git commit -m "feat: studio shell frontend -- jobs, delegate, dashboard"
```

---

### Task 5: Docs + manual smoke test

**Files:**
- Modify: `README.md` (add a "Web UI" section next to wherever the CLI is documented)
- Modify: `CHANGELOG.md` (new entry at top, matching the file's existing format)

**Interfaces:**
- Consumes: everything above.
- Produces: operator documentation and the recorded smoke-test procedure (the frontend's only test, by design).

- [ ] **Step 1: Document the web UI in README.md**

Add after the CLI section (adjust heading level to match the file):

```markdown
## Web UI

The studio in a browser -- same `Factory` facade as the MCP server and the CLI:

    py -3 harness/factory_web.py            # binds 127.0.0.1:8787, opens the browser
    py -3 harness/factory_web.py --port 9000 --no-browser

Active sections: **Jobs** (list, live log, verdict + diff), **Déléguer**
(write the tests in the page, pick targets/context from the repo),
**Dashboard** (success rate per model, durations, latest failures).
Goals / Benchmarks / Agents are greyed out until their backend lands.

Every POST carries a per-run random token embedded in the page, so a
drive-by website cannot reach the API. The server holds no state: jobs are
detached processes, and stopping the UI mid-job loses nothing.
```

- [ ] **Step 2: Add the CHANGELOG entry**

Match the existing entry format in `CHANGELOG.md`; content:

```markdown
- Web UI (`harness/factory_web.py` + `harness/web/`): studio shell over the
  Factory facade. v1 = Jobs / Déléguer / Dashboard; Goals, Benchmarks and
  Agents greyed out pending their backend. Stdlib HTTP server, 127.0.0.1
  only, per-run CSRF token on every POST.
```

- [ ] **Step 3: Run the manual smoke test**

1. `py -3 harness/factory_web.py` — the browser opens on `#jobs`; past jobs from `jobs/` are listed.
2. Open **Déléguer**: pick project `local-factory`, goal `smoke: make slugify strip accents`, one test file `test_smoke_slugify.py` with a trivial test against an existing target (or reuse the slugify task from the CLI E2E of 2026-07-11), pick a target file, submit.
3. The view jumps to the job detail; the badge goes `queued → waiting_gpu → running`; the log tail grows every 2 s.
4. On completion: verdict badge + reviewer verdict shown; diff colourised; **Copier** puts the diff in the clipboard; **Télécharger** saves `<job_id>.patch`.
5. **Dashboard** shows the run in the totals and per-model table.
6. Sanity: `curl -X POST http://127.0.0.1:8787/api/jobs -d "{}"` → `403 Forbidden`.

Record the outcome (pass/fail + anything odd) in the commit message body of Step 4.

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: web ui operator guide and changelog entry"
```

---

## Self-Review (done at planning time)

- **Spec coverage:** shell + greyed sections (T4), all 9 routes (T2/T3), list_jobs + duration (T1), token CSRF (T2), error mapping incl. dead jobs (T1/T3 — `dead` comes from `JobStore.status`, surfaced as-is), polling cadence 2 s/10 s (T4 router), repo-files via git ls-files (T3), pytest on HTTP layer with fake Factory (T2/T3), manual smoke test (T5), out-of-scope items untouched. No gaps found.
- **Placeholder scan:** clean — every code step carries the full code.
- **Type consistency:** `list_jobs()` dict shape (T1) matches T3's passthrough and T4's field reads (`job_id/status/created_at/stage/attempt/model/project/goal/duration`); `Project.path` used in `_repo_files` matches the `factory_config.Project` namedtuple; status vocabulary in CSS/JS matches loop_job's (`succeeded/failed/rejected/error/cancelled` + store's `queued/waiting_gpu/running/dead`).

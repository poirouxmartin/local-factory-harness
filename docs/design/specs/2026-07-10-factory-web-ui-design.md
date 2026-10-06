# Factory Web UI — design

Date: 2026-07-10
Status: validated with Martin (brainstorm session)

## Goal

A browser UI, opened locally, from which the whole factory is driven: delegate
a job (including writing the test in the UI), watch status and logs live, read
the verdict and the diff, and see the factory's history at a glance. The CLI
remains the manual-takeover guarantee; the UI is a comfort layer on the same
facade.

## Scope decision

The UI is designed as **the studio shell** (docs/vision.md): its navigation
already carries the future sections, but v1 only implements the sections that
have a real backend today.

- **v1, active**: Jobs (list + detail), Delegate, Dashboard.
- **v1, greyed out with an "à venir" label**: Goals, Benchmarks, Agents.
  They activate in later iterations when their backend lands.

Out of scope for v1: LAN/remote access, auth beyond the CSRF token, editing
factory.toml from the UI, websockets/SSE (polling suffices), any Node
toolchain.

## Architecture

A third client of the existing `Factory` facade (harness/factory_mcp.py) —
no duplicated logic:

```
harness/factory_web.py      stdlib ThreadingHTTPServer, JSON API + static files
harness/web/index.html      studio shell, vanilla SPA
harness/web/app.js          polling, rendering, forms
harness/web/style.css
```

Zero new dependencies, runs on the existing Python 3.9 — consistent with the
hand-rolled MCP transport and the project's zero-dependency stance.

Launch: `py -3 harness/factory_web.py` → binds `127.0.0.1:8787` only, opens
the default browser. `--port` and `--no-browser` flags.

### Routes

| Route | Method | Behaviour |
|---|---|---|
| `/` and `/static/*` | GET | serve `harness/web/` files |
| `/api/projects` | GET | project keys + repo paths from factory.toml |
| `/api/jobs` | GET | all jobs with state (new `list_jobs()` in job_store) |
| `/api/jobs` | POST | delegate: `{project, goal, tests: {name: content}, targets, contexts}` |
| `/api/jobs/{id}` | GET | mirror of `Factory.job_status` |
| `/api/jobs/{id}/result` | GET | mirror of `Factory.job_result` |
| `/api/jobs/{id}/log?tail=N` | GET | mirror of `Factory.job_log` |
| `/api/jobs/{id}/cancel` | POST | mirror of `Factory.job_cancel` |
| `/api/repo-files?project=X` | GET | repo file list via `git ls-files` (respects .gitignore) |

Test content travels in the POST body: `Factory.delegate()` already takes
`{basename: content}`, so "write the test in the UI" maps to the existing API
with no filesystem round-trip on the client side.

### New backend surface

- `job_store.list_jobs(jobs_dir)`: iterate `jobs/*/`, read each status JSON
  through the existing retry-safe readers, tolerate half-written or foreign
  directories (skip, don't crash).
- Everything else is the existing `Factory` methods, called as-is.

### Security

- Binds `127.0.0.1` only.
- Anti-CSRF for localhost drive-by: a random token generated at server start,
  embedded in the served page, required as a header on every POST. GETs are
  read-only.

## Screens

Left navigation (studio shell): **Jobs · Déléguer · Dashboard** — then
Goals, Benchmarks, Agents greyed out.

- **Jobs**: table — id, project, truncated goal, coloured state, stage/model,
  attempt, duration — sorted by recency, auto-refresh. Click → detail.
- **Job detail**: status banner (state, stage, attempt, tests passing), live
  log tail; on completion the verdict (green/red + reviewer advisory verdict),
  a +/- colourised diff, copy/download-diff buttons; a cancel button while
  running.
- **Déléguer**: project dropdown, goal textarea, one-or-more test editors
  (filename + content textarea, "add a test" button), filterable multi-select
  pickers for targets and contexts fed by `/api/repo-files`. Submit → job
  detail view.
- **Dashboard**: aggregates over `jobs/` — success rate per model/stage,
  average duration, jobs per day, latest failures.

## Data flow

Polling, no push: job list every 2 s while any job is in an active state
(`queued`, `waiting_gpu`, `running`), 10 s otherwise; log tail every 2 s while
a detail view of an active job is open. All reads go through `job_store`'s
retry-safe functions — never raw file reads — because of the Windows
`os.replace`/`WinError 5` interaction documented in job_store.py.

## Error handling

API edge mirrors the CLI's: `JobNotFound` → 404, `ConfigError` and invalid
params → 400, unexpected → 500, always as JSON `{error, detail}`. The UI shows
errors in a banner, never a blank page.

Edge cases:
- Dead job (heartbeat > 90 s): shown as `dead`, distinct from a clean failure.
- Web server stopped mid-job: harmless — jobs are detached processes; on
  restart the UI recovers everything from `jobs/`.
- Two tabs open: server is stateless, polling makes it a non-issue.
- Delegating a test whose basename already exists in the target repo:
  `Factory.delegate` stays the anti-tampering gatekeeper; the UI just
  surfaces its refusal.

## Testing

pytest on the HTTP layer with a fake `Factory`, same pattern as the CLI tests
in `harness/tests/`:
- every route, success and error paths;
- CSRF token required on POSTs (missing/wrong token → 403);
- `list_jobs()` over a synthetic `jobs/` dir including a half-written job;
- multi-test delegate serialisation.

Frontend: no JS test suite (no Node toolchain by design). Validated by a
documented manual smoke test: open, delegate a trivial job on the demo
project, watch it run, read the verdict.

## Future iterations (not v1)

- Goals section, once `/goal` v1 exists.
- Benchmarks section, fed by harness/benchmark.py output.
- Agents/personas section.
- LAN access (phone monitoring) — revisit binding + responsive layout then.

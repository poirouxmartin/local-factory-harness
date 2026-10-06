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
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from chat_store import ChatNotFound
from factory_config import ConfigError, load_projects, resolve_project
from factory_mcp import Factory, ToolError
from job_store import JobNotFound
from turn_runner import Cancellation, TurnBusy, TurnRunner

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).with_name("web")
MIME = {".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".woff2": "font/woff2",
        ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    server_version = "factory-web"

    def log_message(self, fmt, *args):
        pass  # the terminal belongs to the operator, not to an access log

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        try:
            self._dispatch(method)
        except ConnectionError:
            pass  # client hung up mid-response (refresh, tab close); nothing to answer

    def _dispatch(self, method):
        # DNS rebinding: a domain re-resolved to 127.0.0.1 makes the browser
        # same-origin with this server, so it could read the token out of the
        # page. The Host header still names the attacker's domain -- reject it.
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            return self._json(403, {"error": "Forbidden", "detail": "unexpected Host header"})
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
        except (JobNotFound, ChatNotFound) as e:
            self._json(404, {"error": type(e).__name__, "detail": str(e)})
        except ToolError as e:
            # A refusal, not a bug: the job is not in a state the action accepts.
            self._json(409, {"error": "ToolError", "detail": str(e)})
        except TurnBusy as e:
            self._json(409, {"error": "TurnBusy", "detail": str(e)})
        except (ConfigError, KeyError, TypeError, ValueError) as e:
            self._json(400, {"error": type(e).__name__, "detail": str(e)})
        except Exception as e:  # a handler bug must answer JSON, not a stack page
            self._json(500, {"error": type(e).__name__, "detail": str(e)})

    def _json(self, code, payload):
        self._bytes(code, json.dumps(payload).encode("utf-8"),
                    "application/json; charset=utf-8")

    def _bytes(self, code, body, ctype, no_store=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    # ---- pages ----

    def _index(self, query=None):
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        self._bytes(200, html.replace("{{TOKEN}}", self.server.token).encode("utf-8"),
                    MIME[".html"], no_store=True)

    def _static(self, name, query=None):
        path = WEB_DIR / name  # the route regex already forbids separators
        if not path.is_file():
            return self._json(404, {"error": "NotFound", "detail": name})
        # no-store: the studio evolves constantly and a stale cached app.js
        # makes fresh server code look broken in the browser
        self._bytes(200, path.read_bytes(),
                    MIME.get(path.suffix, "application/octet-stream"),
                    no_store=True)

    # ---- api ----

    def _projects(self, query=None):
        self._json(200, {"projects": sorted(load_projects(self.server.factory.config_path))})

    def _project_add(self, query=None):
        b = self._body()
        self._json(200, self.server.factory.add_project(
            b["name"], b["path"], b.get("regression_cmd")))

    def _jobs(self, query=None):
        data = self.server.factory.list_jobs()
        for job in data.get("jobs", []):
            metrics = _compute_job_metrics(job["job_id"], self.server.factory)
            job["total_tokens"] = metrics["total_tokens"]
            job["total_cost"] = metrics["total_cost"]
            job["total_time_ms"] = metrics["total_time_ms"]
            job["attempt_count"] = metrics["attempt_count"]
        self._json(200, data)

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

    def _job_apply(self, job_id, query=None):
        self._json(200, self.server.factory.job_apply(job_id))

    def _draft(self, query=None):
        b = self._body()
        self._json(200, self.server.factory.draft(b["project"], b["goal"]))

    def _runtime_status(self, query=None):
        self._json(200, self.server.factory.runtime_status())

    def _runtime_models(self, query=None):
        self._json(200, self.server.factory.runtime_models())

    def _runtime_load(self, query=None):
        self._json(200, self.server.factory.runtime_load(self._body()["model"]))

    def _runtime_unload(self, query=None):
        self._json(200, self.server.factory.runtime_unload())

    def _cloud_quota(self, query=None):
        self._json(200, self.server.factory.cloud_quota())

    def _cloud_refresh(self, query=None):
        self._json(200, self.server.factory.cloud_refresh())

    def _providers(self, query=None):
        self._json(200, self.server.factory.provider_list())

    def _provider_key(self, query=None):
        body = self._body()
        self._json(200, self.server.factory.provider_set_key(
            body["name"], body.get("value") or ""))

    def _repo_files(self, query=None):
        name = (query.get("project") or [""])[0]
        project = resolve_project(load_projects(self.server.factory.config_path), name)
        out = subprocess.run(["git", "-C", str(project.path), "ls-files"],
                             capture_output=True, text=True, check=True)
        self._json(200, {"files": out.stdout.splitlines()})

    # ---- chat ----

    def _chats(self, query=None):
        listing = self.server.factory.chat_list()
        for s in listing.get("sessions", []):
            s["running"] = self.server.turns.running(s["session_id"])
        self._json(200, listing)

    def _chat_create(self, query=None):
        b = self._body()
        self._json(200, self.server.factory.chat_create(
            b["model"], kind=b.get("kind"), mode=b.get("mode"),
            workspace=b.get("workspace"), toolset=b.get("toolset"),
            project=b.get("project")))

    def _chat_trace(self, session_id, query=None):
        self._json(200, self.server.factory.chat_trace(session_id))

    def _chat_get(self, session_id, query=None):
        session = self.server.factory.chat_get(session_id)
        # Told by the server, not guessed by the page: on load the studio needs
        # to know it must re-attach rather than render a dead transcript, and
        # where the live turn starts so the replay does not double what the
        # store already holds.
        turn = self.server.turns.get(session_id)
        session["running"] = turn is not None and not turn.done
        if session["running"]:
            session["turn_starts_at"] = turn.mark
        self._json(200, session)

    def _chat_title(self, session_id, query=None):
        self._json(200, self.server.factory.chat_set_title(
            session_id, self._body().get("title")))

    def _chat_delete(self, session_id, query=None):
        self._json(200, self.server.factory.chat_delete(session_id))

    def _chat_model(self, session_id, query=None):
        self._json(200, self.server.factory.chat_set_model(
            session_id, self._body()["model"]))

    def _chat_mode(self, session_id, query=None):
        self._json(200, self.server.factory.chat_set_mode(
            session_id, self._body()["mode"]))

    def _chat_num_ctx(self, session_id, query=None):
        self._json(200, self.server.factory.chat_set_num_ctx(
            session_id, self._body().get("num_ctx")))

    def _chat_upload(self, session_id, query=None):
        b = self._body()
        self._json(200, self.server.factory.chat_upload(
            session_id, b.get("name"), b.get("content_b64")))

    def _chat_approve(self, session_id, query=None):
        cancel = Cancellation()
        gen = self.server.factory.chat_approve(
            session_id, bool(self._body().get("approved")), cancel=cancel)
        self._start_turn(session_id, gen, cancel)

    def _start_turn(self, session_id, gen, cancel=None):
        """Hand the generator to the runner, then watch it like any other
        viewer. Refusals (404/409/400) raise inside start(), before any byte of
        the stream: _route still owns the response and answers JSON.

        `cancel` must be the very token the generator was built with -- that
        pairing is what makes Stop reach a turn asleep in a lane."""
        factory = self.server.factory
        turn = self.server.turns.start(
            session_id, gen,
            mark_fn=lambda: len(factory.chat_get(session_id)["messages"]),
            cancel=cancel)
        self._stream_turn(turn, 0)

    def _stream_turn(self, turn, cursor):
        # The connection is a viewer over the turn's buffer, never its owner:
        # hanging up (refresh, tab close) only ends this read. Measured
        # 2026-07-22: while the turn lived in this response body, a refresh
        # killed it mid-task -- spec 2026-07-22-server-side-turns-design.md.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        events = turn.read(cursor)
        try:
            for kind, payload in events:
                if kind == "done":
                    line = dict(payload, done=True)
                elif kind == "error":
                    line = {"error": payload}
                else:  # chunk, thinking, tool_call, tool_result, approval_needed
                    line = {kind: payload}
                self.wfile.write((json.dumps(line) + "\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError):
            events.close()  # this viewer only; the turn runs on
        except Exception as e:
            # Headers are out: this cannot become an HTTP error anymore. An
            # uncaught failure used to just drop the connection and the UI
            # showed nothing -- send it as the last NDJSON line instead.
            try:
                self.wfile.write((json.dumps({"error": str(e)}) + "\n")
                                 .encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError):
                pass

    def _toolsets(self, query=None):
        self._json(200, self.server.factory.toolsets())

    def _chat_send(self, session_id, query=None):
        factory = self.server.factory
        content = self._body()["content"]
        cancel = Cancellation()
        if factory.chat_get(session_id).get("kind") == "agent":
            gen = factory.agent_reply(session_id, content, cancel=cancel)
        else:
            gen = factory.chat_reply(session_id, content, cancel=cancel)
        self._start_turn(session_id, gen, cancel)

    def _chat_stream(self, session_id, query=None):
        """Re-attach to the session's turn. Default cursor 0 replays it whole:
        a turn is bounded by the context window, so the replay is cheap and
        rebuilds exactly what the operator would have seen."""
        turn = self.server.turns.get(session_id)
        if turn is None:
            return self._json(404, {"error": "NoTurn", "detail": session_id})
        cursor = int((query or {}).get("cursor", ["0"])[0])
        self._stream_turn(turn, cursor)

    def _chat_stop(self, session_id, query=None):
        self._json(200, {"stopped": self.server.turns.stop(session_id)})

    def _stats(self, query=None):
        """Session-level aggregates: tokens, cost, time, model & status distribution.

        Ce handler RENVOYAIT son dict au lieu de l'écrire : la réponse partait
        vide (ERR_EMPTY_RESPONSE) et la barre de chiffres des Jobs n'a jamais
        rien affiché. Il comptait aussi des états inventés (« pending »,
        « done ») ; le vocabulaire réel est celui de `job_store`
        (queued/waiting_gpu/running, puis succeeded/failed/rejected/error/
        cancelled/dead), d'où un comptage sur ce qui passe vraiment.
        """
        data = self.server.factory.list_jobs()
        jobs = data.get("jobs", [])

        total_tokens = 0
        total_cost = 0.0
        total_time_ms = 0
        model_stats = {}
        status_counts = {}
        done_tokens = 0
        done_time_ms = 0

        for job in jobs:
            m = _compute_job_metrics(job["job_id"], self.server.factory)
            total_tokens += m["total_tokens"]
            total_cost += m["total_cost"]
            total_time_ms += m["total_time_ms"]

            model = job.get("model") or "unknown"
            if model not in model_stats:
                model_stats[model] = {"count": 0, "tokens": 0, "cost": 0.0}
            model_stats[model]["count"] += 1
            model_stats[model]["tokens"] += m["total_tokens"]
            model_stats[model]["cost"] += m["total_cost"]

            st = job.get("status") or "unknown"
            status_counts[st] = status_counts.get(st, 0) + 1
            if st == "succeeded":
                done_tokens += m["total_tokens"]
                done_time_ms += m["total_time_ms"]

        # Débit effectif : tokens/s sur les jobs réussis seulement.
        throughput_tps = (round(done_tokens / (done_time_ms / 1000.0), 1)
                          if done_time_ms > 0 else None)

        self._json(200, {
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 4),
            "total_time_ms": total_time_ms,
            "total_jobs": len(jobs),
            "status_counts": status_counts,
            "model_stats": model_stats,
            "throughput_tps": throughput_tps,
        })


# ─── pricing helpers ────────────────────────────────────────────────
MODEL_PRICES = {
    # OpenAI
    "o1":                  (15.00, 60.00),
    "o1-mini":             (3.00, 12.00),
    "o3-mini":             (1.10, 4.40),
    "gpt-4o":              (2.50, 10.00),
    "gpt-4o-mini":         (0.15, 0.60),
    # Anthropic
    "claude-sonnet-4-0":   (3.00, 15.00),
    "claude-opus-4-0":     (15.00, 75.00),
    "claude-3-5-sonnet":   (3.00, 15.00),
    "claude-3-5-haiku":    (0.80, 4.00),
    "claude-3-haiku":      (0.25, 1.25),
    # Google
    "gemini-2.0-flash":    (0.075, 0.30),
    "gemini-2.0-pro":      (1.25, 5.00),
    # Mistral
    "mistral-large":       (2.00, 6.00),
    "ministral-3b":        (0.04, 0.12),
    # Open-source / local (free)
    "qwen2.5-coder:32b":   (0.00, 0.00),
    "qwen2.5-coder:14b":   (0.00, 0.00),
    "qwen2.5-coder:7b":    (0.00, 0.00),
}


def _model_price(model):
    """Return (input_per_1M, output_per_1M) for *model*. Falls back to zero."""
    if model is None:
        return (0.0, 0.0)
    price = MODEL_PRICES.get(model)
    if price:
        return price
    for prefix, p in MODEL_PRICES.items():
        if model.startswith(prefix):
            return p
    return (0.0, 0.0)


def _compute_job_metrics(job_id, factory):
    """Compute total tokens, cost, and time for a job.

    Les compteurs vivent à la RACINE du résultat (`attempts`, `total_seconds`).
    Ce lecteur les cherchait sous une clé `result` et une clé `duration_s` qui
    n'existent pas : la barre de chiffres affichait donc « 0 tokens · — · 0 ms »
    sur les 34 jobs de la machine, quel que soit le job.

    Les jobs antérieurs à l'enregistrement de `prompt_count` n'ont que leurs
    tokens de sortie : on compte ce qui est écrit, sans estimer le reste.
    """
    try:
        result = factory.job_result(job_id)
    except Exception:
        return {"total_tokens": 0, "total_cost": 0.0, "total_time_ms": 0,
                "attempt_count": 0}

    attempts = result.get("attempts") or []

    total_tokens = 0
    total_cost = 0.0

    for attempt in attempts:
        prompt_count = attempt.get("prompt_count", 0)
        eval_count = attempt.get("eval_count", 0)
        tokens = prompt_count + eval_count
        total_tokens += tokens

        model = attempt.get("model")
        input_price, output_price = _model_price(model)
        cost = (prompt_count * input_price + eval_count * output_price) / 1_000_000
        total_cost += cost

    total_time_ms = 0
    duration_s = result.get("total_seconds")
    if duration_s is not None:
        total_time_ms = round(duration_s * 1000)

    return {
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 4),
        "total_time_ms": total_time_ms,
        "attempt_count": len(attempts),
    }


ROUTES = [
    (r"/", "GET", Handler._index),
    (r"/static/([\w.-]+)", "GET", Handler._static),
    (r"/api/projects", "GET", Handler._projects),
    (r"/api/projects", "POST", Handler._project_add),
    (r"/api/jobs", "GET", Handler._jobs),
    (r"/api/jobs", "POST", Handler._delegate),
    (r"/api/jobs/(j_\w+)", "GET", Handler._job_status),
    (r"/api/jobs/(j_\w+)/result", "GET", Handler._job_result),
    (r"/api/jobs/(j_\w+)/log", "GET", Handler._job_log),
    (r"/api/jobs/(j_\w+)/cancel", "POST", Handler._job_cancel),
    (r"/api/jobs/(j_\w+)/apply", "POST", Handler._job_apply),
    (r"/api/stats", "GET", Handler._stats),
    (r"/api/repo-files", "GET", Handler._repo_files),
    (r"/api/drafts", "POST", Handler._draft),
    (r"/api/runtime", "GET", Handler._runtime_status),
    (r"/api/runtime/models", "GET", Handler._runtime_models),
    (r"/api/runtime/load", "POST", Handler._runtime_load),
    (r"/api/runtime/unload", "POST", Handler._runtime_unload),
    (r"/api/cloud/quota", "GET", Handler._cloud_quota),
    (r"/api/cloud/refresh", "POST", Handler._cloud_refresh),
    (r"/api/providers", "GET", Handler._providers),
    (r"/api/providers/key", "POST", Handler._provider_key),
    (r"/api/toolsets", "GET", Handler._toolsets),
    (r"/api/chats", "GET", Handler._chats),
    (r"/api/chats", "POST", Handler._chat_create),
    (r"/api/chats/(c_\w+)", "GET", Handler._chat_get),
    (r"/api/chats/(c_\w+)/trace", "GET", Handler._chat_trace),
    (r"/api/chats/(c_\w+)/messages", "POST", Handler._chat_send),
    (r"/api/chats/(c_\w+)/stream", "GET", Handler._chat_stream),
    (r"/api/chats/(c_\w+)/stop", "POST", Handler._chat_stop),
    (r"/api/chats/(c_\w+)/title", "POST", Handler._chat_title),
    (r"/api/chats/(c_\w+)/delete", "POST", Handler._chat_delete),
    (r"/api/chats/(c_\w+)/model", "POST", Handler._chat_model),
    (r"/api/chats/(c_\w+)/mode", "POST", Handler._chat_mode),
    (r"/api/chats/(c_\w+)/num_ctx", "POST", Handler._chat_num_ctx),
    (r"/api/chats/(c_\w+)/approve", "POST", Handler._chat_approve),
    (r"/api/chats/(c_\w+)/upload", "POST", Handler._chat_upload),
]


class FactoryWebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, factory, token=None):
        super().__init__(addr, Handler)
        self.factory = factory
        self.token = token or secrets.token_hex(16)
        # Turns outlive the connections that start them; the runner is the
        # server's, not the facade's, because it exists to decouple HTTP.
        self.turns = TurnRunner()

    def handle_error(self, request, client_address):
        # A viewer hanging up mid-stream is routine now that the turn does not
        # depend on it, and the socket can still fail late -- inside the
        # handler's own flush, past every try in _stream_turn. Printing a
        # traceback for it trains the operator to ignore tracebacks.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


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
    # The model server starts lazily on the first chat message (lock = VRAM),
    # so there is nothing to boot here.
    if not a.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Children survive their parent on Windows: a studio that leaves
        # without this strands its llama-server on the port with the model
        # still in VRAM, and the next studio then talks to that ghost
        # (measured 2026-07-22: two 30b servers, 11.8 GB held). ensure() also
        # reclaims the port, for the exits this finally never sees -- crash,
        # taskkill. Same survival rule, same fix, for the MCP servers: they
        # hold their workspace by its CWD until someone closes them.
        server.factory.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

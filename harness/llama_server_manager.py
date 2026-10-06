"""One llama-server holds ONE model; the ladder speaks model names.

The manager swaps servers on demand so `chat(model, ...)` keeps the exact
contract loop_job already consumes. Two measured bench traps are baked into
the spawn argv (audit 2026-07-18): `--parallel 1` (the default slot count
divides the context and silently truncates job prompts) and the full
FA/KV-q8 flag set the bench validated.

The manager owns its child process: a runner that dies without shutdown()
would leak a 20 GB server on Windows (children survive their parent), so
run_job wraps the ladder in try/finally.
"""
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import llama_client

HEALTH_TIMEOUT = 600.0  # cold load of a 25 GB MoE from disk takes minutes


class UnknownServerModel(Exception):
    pass


def _spawn(argv):
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.pid


def _kill(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True)


def _image_name(pid):
    # errors="replace": these console tools speak the OEM codepage, and a byte
    # cp1252 cannot map used to blow up the caller instead of answering.
    out = subprocess.run(["tasklist", "/FI", "PID eq {}".format(pid),
                          "/NH", "/FO", "CSV"], capture_output=True,
                         text=True, errors="replace").stdout or ""
    return out.split(",")[0].strip('" \r\n').lower() if "," in out else ""


def _port_owner(port):
    """PID of the llama-server listening on `port`, or None.

    Anything that is not a llama-server is left alone: this reclaims our own
    leftovers, it is not a port war. A stale server answers /health perfectly,
    so nothing above this can tell the difference.
    """
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True,
                             errors="replace").stdout or ""
    except OSError:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        if not parts[1].endswith(":{}".format(port)):
            continue
        pid = int(parts[4])
        return pid if "llama-server" in _image_name(pid) else None
    return None


def _wait_health(url, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=3) as f:
                if f.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


class LlamaServerManager:
    def __init__(self, cfg, spawn_fn=None, health_fn=None, kill_fn=None,
                 kv_cache_type="q8_0", ctx=None, probe_fn=None):
        self.cfg = cfg
        self.spawn_fn = spawn_fn or _spawn
        self.health_fn = health_fn or _wait_health
        self.kill_fn = kill_fn or _kill
        self.probe_fn = probe_fn or _port_owner
        self.current = None  # (model, pid)
        self._last_killed = None
        # Lane overrides: jobs keep the precise default (q8 KV, tuned ctx); the
        # chat lane runs a bigger window cheaply with q4 KV -- on 12 GB the KV
        # cache is what decides whether 64k fits in VRAM or spills to system
        # RAM (measured 2026-07-19: 30b 64k q8 = 31 tok/s spilling, q4 = 59).
        self.kv_cache_type = kv_cache_type
        self.ctx = ctx if ctx is not None else cfg["ctx"]

    @property
    def url(self):
        return "http://127.0.0.1:{}".format(self.cfg["port"])

    def ensure(self, model):
        if self.current and self.current[0] == model:
            return self.url
        entry = self.cfg["models"].get(model)
        if entry is None:
            raise UnknownServerModel(
                "{!r} has no [llama_server.models] entry; add its GGUF path "
                "and flags to factory.toml".format(model))
        self.shutdown()
        self._reclaim_port()
        argv = [self.cfg["exe"], "-m", entry["path"],
                "-ngl", "99", "-fa", "1",
                "-ctk", self.kv_cache_type, "-ctv", self.kv_cache_type,
                "-c", str(self.ctx), "--parallel", "1",
                # --jinja: templates natifs = tool calls OpenAI; deepseek:
                # le thinking sort dans reasoning_content, pas dans content;
                # cache-reuse: le KV du prefixe survit aux retouches (TTFT)
                "--jinja", "--reasoning-format", "deepseek"]
        # cache-reuse ON by default (shipped). LLAMA_NO_CACHE_REUSE=1 drops it:
        # the off arm of the 2026-07-23 A/B measuring the flag on the form it
        # was built for -- mid-prompt divergence from compaction. Env-gated so
        # no production path changes.
        if not os.environ.get("LLAMA_NO_CACHE_REUSE"):
            argv += ["--cache-reuse", "256"]
        argv += ["--port", str(self.cfg["port"])] + entry["args"]
        pid = self.spawn_fn(argv)
        self.current = (model, pid)
        if not self.health_fn(self.url, HEALTH_TIMEOUT):
            self.shutdown()
            raise RuntimeError("llama-server for {!r} never became healthy "
                               "(see the server log)".format(model))
        return self.url

    def _reclaim_port(self):
        """Kill whoever else holds our port before spawning.

        A studio that dies -- crash, taskkill, closed console -- leaves its
        llama-server alive on the port with the model still in VRAM. The next
        spawn then fails to bind IN SILENCE, and since health_fn only asks the
        URL, the stale server answers and the manager believes its own child is
        serving. Measured 2026-07-22: two 30b servers, 11.8 GB of VRAM, every
        request answered by the orphan with the orphan's ctx/KV settings.
        """
        pid = self.probe_fn(self.cfg["port"])
        if pid is None or pid == self._last_killed:
            return  # our own server on its way out: killed once, not twice
        print("llama-server {} squats port {}: reclaiming it".format(
            pid, self.cfg["port"]), file=sys.stderr)
        self.kill_fn(pid)

    def chat(self, model, system, user, temperature=0.1, num_ctx=None, options=None):
        url = self.ensure(model)
        return llama_client.chat(url, system, user, temperature=temperature,
                                 num_ctx=num_ctx, options=options)

    def unload(self, model):
        if self.current and self.current[0] == model:
            self.shutdown()

    def shutdown(self):
        if self.current:
            self._last_killed = self.current[1]
            self.kill_fn(self.current[1])
            self.current = None

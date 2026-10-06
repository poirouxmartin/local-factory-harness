"""Stdlib-only MCP client over stdio (JSON-RPC 2.0, one message per line).

The factory now speaks MCP in both directions: factory_mcp.py SERVES the
factory to Claude; this module lets the local chat agent CONSUME external
MCP servers (git, fetch, playwright...). Same hand-rolled transport, same
reason: there is no `mcp` SDK for Python 3.9.

Contract mirrors agent_tools: call() returns a STRING the model reads, and
every failure -- missing command, crash, timeout, isError result -- is
folded into that string, never raised. The agent loop must keep going.

A reader thread drains stdout into a queue: Windows pipes cannot be
select()ed, and a blocking readline would turn a slow server into a hung
agent turn.
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import time

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 60
SPAWN_TIMEOUT = 30


class McpError(Exception):
    pass


def _text(result):
    """Flatten a tools/call result into the string the model reads."""
    parts = [c.get("text", "") for c in result.get("content") or []
             if c.get("type") == "text"]
    text = "\n".join(p for p in parts if p) or "(no output)"
    if result.get("isError"):
        return "error: " + text
    return text


class McpServer:
    """One spawned MCP server process and its handshake state."""

    def __init__(self, name, command, cwd=None, env=None):
        self.name = name
        self.command = list(command)
        self.cwd = cwd
        self.env = env
        self.proc = None
        self.tools = []      # tools/list result, set by start()
        self._id = 0
        self._replies = None
        self._lock = threading.RLock()

    def start(self):
        exe = shutil.which(self.command[0])  # resolves npx -> npx.cmd on nt
        if exe is None:
            raise McpError("command not found: {}".format(self.command[0]))
        env = dict(os.environ, **(self.env or {}))
        self._replies = queue.Queue()
        self.proc = subprocess.Popen(
            [exe] + self.command[1:], cwd=self.cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        threading.Thread(target=self._read_forever, args=(self.proc.stdout,),
                         daemon=True).start()
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "local-factory", "version": "1.0"},
        }, timeout=SPAWN_TIMEOUT)
        self._notify("notifications/initialized")
        self.tools = self._rpc("tools/list", {},
                               timeout=SPAWN_TIMEOUT).get("tools", [])

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def close(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        self.tools = []

    def call(self, tool, arguments, timeout=DEFAULT_TIMEOUT):
        """String result, string errors; a dead server restarts once."""
        try:
            with self._lock:
                if not self.alive():
                    self.close()
                    self.start()
                return _text(self._rpc(
                    "tools/call", {"name": tool, "arguments": arguments or {}},
                    timeout=timeout))
        except (McpError, OSError, ValueError) as e:
            return "error: mcp server {}: {}".format(self.name, e)

    # ---- transport ----

    def _read_forever(self, stdout):
        replies = self._replies
        for line in stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                continue  # some servers log to stdout; skip the junk
            if "id" in msg and "method" not in msg:
                replies.put(msg)
            # server-initiated requests/notifications are ignored in v1

    def _send(self, msg):
        data = (json.dumps(msg) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def _rpc(self, method, params, timeout=DEFAULT_TIMEOUT):
        with self._lock:
            self._id += 1
            req_id = self._id
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method,
                        "params": params})
            return self._read_until(req_id, timeout)

    def _read_until(self, req_id, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError("timeout after {}s".format(timeout))
            try:
                msg = self._replies.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if not self.alive():
                    raise McpError("server exited")
                continue
            if msg.get("id") != req_id:
                continue  # stale reply from a timed-out earlier call
            if "error" in msg:
                err = msg["error"] or {}
                raise McpError("{} (code {})".format(
                    err.get("message", "?"), err.get("code")))
            return msg.get("result") or {}

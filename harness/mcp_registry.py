"""Bridges configured MCP servers into the agent tool loop.

One McpServer per (server, workspace): git and playwright act on the
session's workspace, so two sessions must never share a process. Tool
names are namespaced mcp__<server>__<tool>; the factory.toml allowlist
decides what the model ever sees, and `readonly` decides what skips the
approval gate.
"""
import sys
import threading

from mcp_client import McpError, McpServer

PREFIX = "mcp__"
# Past this many exposed tools, <=35b models pick tools measurably worse;
# the caller warns, it does not refuse (the operator may know better).
TOOL_BUDGET = 15


def tool_name(server, tool):
    return PREFIX + server + "__" + tool


def split(name):
    """("server", "tool") from a namespaced name, else None."""
    if not name.startswith(PREFIX):
        return None
    rest = name[len(PREFIX):]
    if "__" not in rest:
        return None
    server, tool = rest.split("__", 1)
    return server, tool


class McpRegistry:
    def __init__(self, configs):
        self.configs = configs  # name -> McpServerConfig
        self._servers = {}      # (name, workspace) -> McpServer
        self._specs = {}        # (name, workspace) -> last known tool specs
        self._lock = threading.Lock()

    def _server(self, name, workspace):
        key = (name, workspace)
        with self._lock:
            srv = self._servers.get(key)
            if srv is None:
                cfg = self.configs[name]
                srv = McpServer(name, cfg.command, cwd=workspace, env=cfg.env)
                self._servers[key] = srv
            return srv

    def tool_specs(self, names, workspace):
        """Ollama tool definitions for the allowlisted tools of `names`.
        tools/list carries the schemas, so this spawns lazily; a server
        that will not start costs a stderr line, never the session.

        A server that started once and dies later is a different story: its
        schemas are already in the KV prefix, so dropping them shortens the
        head of the prompt -- the whole prefix is repaid, and the model loses
        tools without being told. Those keep their last known shape.
        """
        specs = []
        for name in names:
            cfg = self.configs.get(name)
            if cfg is None:
                continue
            key = (name, workspace)
            srv = self._server(name, workspace)
            if not srv.alive():
                try:
                    srv.close()
                    srv.start()
                except (McpError, OSError) as e:
                    print("mcp server {} unavailable: {}".format(name, e),
                          file=sys.stderr)
                    # Empty when it never started: nothing to hold the shape
                    # of, and the prefix never carried it.
                    specs.extend(self._specs.get(key, []))
                    continue
            by_name = {t.get("name"): t for t in srv.tools}
            served = []
            for tool in cfg.tools:
                spec = by_name.get(tool)
                if spec is None:
                    continue
                served.append({"type": "function", "function": {
                    "name": tool_name(name, tool),
                    "description": spec.get("description", ""),
                    "parameters": spec.get("inputSchema") or
                                  {"type": "object", "properties": {}},
                }})
            self._specs[key] = served
            specs.extend(served)
        return specs

    def needs_approval(self, name):
        parts = split(name)
        if parts is None:
            return True
        cfg = self.configs.get(parts[0])
        return cfg is None or parts[1] not in cfg.readonly

    def run(self, name, arguments, workspace):
        parts = split(name)
        if parts is None:
            return "error: unknown tool: {}".format(name)
        server, tool = parts
        cfg = self.configs.get(server)
        if cfg is None or tool not in cfg.tools:
            return "error: tool not allowed: {}".format(name)
        return self._server(server, workspace).call(tool, arguments)

    def close(self):
        with self._lock:
            for srv in self._servers.values():
                srv.close()
            self._servers.clear()
            # Not a hiccup mid-session: there is no prefix left to protect,
            # and the next server describes itself.
            self._specs.clear()

"""Minimal MCP stdio server for tests: initialize, tools/list, tools/call.

Tools: echo (returns its text argument), sleepy (sleeps `seconds`, then
answers "woke"), boom (isError result), die (exits mid-call).
"""
import json
import sys
import time

TOOLS = [
    {"name": "echo", "description": "echo text back",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "sleepy", "description": "sleep then answer",
     "inputSchema": {"type": "object",
                     "properties": {"seconds": {"type": "number"}}}},
    {"name": "boom", "description": "always fails",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "die", "description": "kill the server",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _reply(req_id, result):
    sys.stdout.write(json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method, req_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            _reply(req_id, {"protocolVersion":
                            msg["params"]["protocolVersion"],
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake", "version": "0"}})
        elif method == "tools/list":
            _reply(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments") or {}
            if name == "die":
                sys.exit(1)
            if name == "sleepy":
                time.sleep(float(args.get("seconds", 0)))
                _reply(req_id, {"content": [{"type": "text", "text": "woke"}]})
            elif name == "boom":
                _reply(req_id, {"content": [{"type": "text", "text": "kaputt"}],
                                "isError": True})
            else:
                _reply(req_id, {"content":
                                [{"type": "text", "text": args.get("text", "")}]})
        # notifications (no id) are consumed silently


if __name__ == "__main__":
    main()

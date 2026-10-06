#!/usr/bin/env python3
"""E2E test du pipeline Playwright MCP — stdio transport."""
import json
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from harness.mcp_client import McpServer


def run():
    # 1. Lancer le serveur MCP Playwright
    server = McpServer("playwright", ["npx", "@playwright/mcp"])
    try:
        server.start()
        tool_names = [t["name"] for t in server.tools]
        print(f"Tools: {len(tool_names)} detected")
        assert "browser_navigate" in tool_names, "browser_navigate missing"
        assert "browser_snapshot" in tool_names, "browser_snapshot missing"

        # 2. Naviguer
        nav = server.call("browser_navigate", {"url": "https://example.com"})
        print(f"Navigate: OK")

        # 3. Snapshot
        snap = server.call("browser_snapshot", {})
        print(f"Snapshot: OK ({len(snap)} chars)")

        # 4. Vérification contenu
        assert "Example Domain" in snap, "Content 'Example Domain' not found"
        assert "Learn more" in snap, "Content 'Learn more' not found"
        print("Content check: PASS")
        return True
    finally:
        server.close()


if __name__ == "__main__":
    ok = run()
    if not ok:
        sys.exit(1)

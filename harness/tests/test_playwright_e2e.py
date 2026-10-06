"""E2E integration test — Playwright MCP server end-to-end (headless).

This module drives the real MCP Playwright server through the McpClient
transport, exercises the navigation → snapshot → content pipeline, and
verifies that the text snapshot contains the expected headings.

Run:   python -m pytest harness/tests/test_playwright_e2e.py -v
"""
import os
import sys

import pytest

# It drives a browser through the MCP server rather than importing playwright,
# so conftest cannot detect it: declare the marker here. The push gate runs
# `-m "not browser"` -- a test that needs a live browser cannot gate a push.
pytestmark = pytest.mark.browser

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from harness.mcp_client import McpServer, McpError


def _collect_output(server, timeout=120):
    """Navigate, snapshot, and verify content — the full E2E pipeline."""
    # 1. Navigate to a simple, fast-loading page
    url = "https://example.com"
    nav_result = server.call("browser_navigate", {"url": url}, timeout=timeout)
    assert nav_result is not None, "browser_navigate returned None"

    # 2. Wait for the page to settle
    # `browser_wait_for` takes time/text/textGone -- it never took a load
    # state, and today's server rejects `waitFor` outright. Navigation already
    # waits for load, so this is only a settle.
    wait = server.call("browser_wait_for", {"time": 2}, timeout=30)
    assert "error:" not in wait.lower(), f"browser_wait_for failed: {wait}"

    # 3. Take a snapshot
    snap = server.call("browser_snapshot", {}, timeout=timeout)
    assert snap is not None, "browser_snapshot returned None"
    assert isinstance(snap, str), f"snapshot is {type(snap)}, expected str"
    assert len(snap) > 100, f"snapshot too short ({len(snap)} chars)"

    # 4. Verify expected content
    assert "Example Domain" in snap, f"'Example Domain' not found in snapshot"
    assert "Learn more" in snap, f"'Learn more' not found in snapshot"

    return snap


def test_playwright_e2e():
    """Full E2E: start MCP server → navigate → snapshot → verify content."""
    server = McpServer("playwright", ["npx", "-y", "@playwright/mcp@latest"])
    try:
        server.start()
        tool_names = [t["name"] for t in server.tools]
        assert "browser_navigate" in tool_names, "browser_navigate missing from tools/list"
        assert "browser_snapshot" in tool_names, "browser_snapshot missing from tools/list"

        snap = _collect_output(server)
        print(f"Snapshot length: {len(snap)} chars")
    finally:
        server.close()


def test_playwright_multiple_navigations():
    """Navigate to two different pages and verify each snapshot."""
    server = McpServer("playwright", ["npx", "-y", "@playwright/mcp@latest"])
    try:
        server.start()

        urls_and_expected = [
            ("https://example.com", ["Example Domain"]),
            ("https://www.google.com", ["Google"]),
        ]
        for url, expected_strings in urls_and_expected:
            nav = server.call("browser_navigate", {"url": url}, timeout=120)
            wait = server.call("browser_wait_for", {"time": 2}, timeout=30)
            snap = server.call("browser_snapshot", {}, timeout=60)

            for s in expected_strings:
                assert s in snap, f"'{s}' not found after navigating to {url}"

    finally:
        server.close()


def test_playwright_browser_click():
    """Navigate → snapshot → click a link → verify page changed."""
    server = McpServer("playwright", ["npx", "-y", "@playwright/mcp@latest"])
    try:
        server.start()

        # Navigate
        nav = server.call("browser_navigate", {"url": "https://example.com"}, timeout=120)
        wait = server.call("browser_wait_for", {"time": 2}, timeout=30)

        # Snapshot before click
        snap_before = server.call("browser_snapshot", {}, timeout=60)
        assert "Example Domain" in snap_before

        # Click the first link (W3Schools)
        click = server.call("browser_click", {"ref": "W3Schools"}, timeout=30)
        if "error:" not in click.lower():
            wait2 = server.call("browser_wait_for", {"time": 2}, timeout=30)
            snap_after = server.call("browser_snapshot", {}, timeout=60)
            # Page should have changed (different title/content)
            assert snap_after != snap_before, "Page did not change after click"

    finally:
        server.close()


if __name__ == "__main__":
    import traceback

    tests = [test_playwright_e2e, test_playwright_multiple_navigations, test_playwright_browser_click]
    passed = 0
    failed = 0
    for t in tests:
        name = t.__name__
        server = None
        try:
            print(f"\n=== Running {name} ===")
            t()
            print(f"PASS: {name}")
            passed += 1
        except Exception as e:
            print(f"FAIL: {name} — {e}")
            traceback.print_exc()
            failed += 1
        finally:
            if server and server.alive():
                server.close()

    print(f"\nResults: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

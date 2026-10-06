import sys
from pathlib import Path

import pytest

from mcp_client import McpServer

FAKE = [sys.executable, str(Path(__file__).with_name("fake_mcp_server.py"))]


@pytest.fixture
def server():
    srv = McpServer("fake", FAKE)
    yield srv
    srv.close()


def test_start_lists_the_tools(server):
    server.start()
    assert [t["name"] for t in server.tools] == ["echo", "sleepy", "boom", "die"]


def test_call_returns_the_text_content(server):
    assert server.call("echo", {"text": "bonjour"}) == "bonjour"


def test_call_starts_the_server_lazily(server):
    # no explicit start(): the first call spawns and handshakes
    assert server.call("echo", {"text": "lazy"}) == "lazy"
    assert server.alive()


def test_is_error_results_become_error_strings(server):
    out = server.call("boom", {})
    assert out.startswith("error: ") and "kaputt" in out


def test_a_timeout_is_a_string_not_an_exception(server):
    out = server.call("sleepy", {"seconds": 5}, timeout=1)
    assert out.startswith("error: ") and "timeout" in out


def test_a_dead_server_restarts_on_the_next_call(server):
    assert server.call("echo", {"text": "a"}) == "a"
    out = server.call("die", {})
    assert out.startswith("error: ")
    assert server.call("echo", {"text": "b"}) == "b"  # restarted


def test_a_missing_command_is_a_string_error():
    srv = McpServer("ghost", ["definitely-not-a-command-xyz"])
    out = srv.call("anything", {})
    assert out.startswith("error: ")

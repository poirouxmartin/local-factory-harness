"""McpRegistry: the allowlist between factory.toml and the agent tool loop.

Namespaced names keep two servers from colliding on a tool name; the
allowlist and readonly set are enforced here, not trusted from the server.
"""
import sys
from pathlib import Path

import pytest

from factory_config import McpServerConfig
from mcp_registry import McpRegistry, PREFIX, split, tool_name

FAKE = [sys.executable, str(Path(__file__).with_name("fake_mcp_server.py"))]


@pytest.fixture
def registry():
    cfg = McpServerConfig("fake", FAKE, ["echo", "boom"], ["echo"], {})
    reg = McpRegistry({"fake": cfg})
    yield reg
    reg.close()


def test_names_roundtrip():
    assert tool_name("git", "git_status") == "mcp__git__git_status"
    assert split("mcp__git__git_status") == ("git", "git_status")
    assert split("read_file") is None


def test_tool_specs_expose_only_the_allowlist(registry, tmp_path):
    specs = registry.tool_specs(["fake"], str(tmp_path))

    names = [s["function"]["name"] for s in specs]
    assert names == ["mcp__fake__echo", "mcp__fake__boom"]  # not sleepy/die
    echo = specs[0]["function"]
    assert echo["description"] == "echo text back"
    assert echo["parameters"]["required"] == ["text"]


def test_run_routes_to_the_server(registry, tmp_path):
    out = registry.run("mcp__fake__echo", {"text": "hi"}, str(tmp_path))
    assert out == "hi"


def test_run_refuses_tools_outside_the_allowlist(registry, tmp_path):
    out = registry.run("mcp__fake__die", {}, str(tmp_path))
    assert out.startswith("error: tool not allowed")


def test_needs_approval_honors_readonly(registry):
    assert registry.needs_approval("mcp__fake__echo") is False
    assert registry.needs_approval("mcp__fake__boom") is True
    assert registry.needs_approval("mcp__nope__x") is True


def test_a_broken_server_costs_nothing_at_spec_time(tmp_path):
    cfg = McpServerConfig("ghost", ["no-such-command-xyz"], ["a"], [], {})
    reg = McpRegistry({"ghost": cfg})
    assert reg.tool_specs(["ghost"], str(tmp_path)) == []


def test_a_server_that_dies_keeps_its_schemas_in_the_prefix(registry, tmp_path):
    # The tool list is the head of the KV prefix (_agent_system_parts: the
    # tool_names block, then these schemas). A server that dies between two
    # turns used to shorten it -- the whole prefix is repaid, and the model
    # loses tools without being told. Found 2026-07-23 by instrumenting the
    # bench, which reported a `web` toolset at 9 tools instead of 15.
    ws = str(tmp_path)
    first = registry.tool_specs(["fake"], ws)
    srv = registry._servers[("fake", ws)]
    srv.proc.kill()
    srv.proc.wait(timeout=5)                # kill is asynchronous on Windows
    srv.command = ["no-such-command-xyz"]   # and it will not come back

    assert registry.tool_specs(["fake"], ws) == first


def test_a_closed_registry_does_not_serve_yesterday_s_schemas(registry, tmp_path):
    # close() is the end of the process, not a hiccup mid-session: whatever
    # the next registry spawns has to describe itself again.
    ws = str(tmp_path)
    registry.tool_specs(["fake"], ws)
    registry.close()
    registry.configs["fake"] = McpServerConfig(
        "fake", ["no-such-command-xyz"], ["echo"], ["echo"], {})

    assert registry.tool_specs(["fake"], ws) == []

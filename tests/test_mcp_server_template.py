"""The MCP server template starts, is discovered, and answers a tool call.

These tests launch the real template at ``templates/mcp_server/server.py``
as a stdio subprocess. One drives it through Claire's own MCP client code,
which is the path a spoken tool call takes; the other checks the template's
protocol surface directly, because Claire's client consumes tools only.

A green run here means a copy of the template is discoverable and callable
from a reply.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates" / "mcp_server"
SERVER_PATH = TEMPLATE_DIR / "server.py"
# Must match SERVER_NAME in server.py.
SERVER_NAME = "example"


@pytest.fixture
def shutdown_persistent_runtime():
    """Tear down the persistent MCP runtime singleton after each test."""
    yield
    try:
        from claire.tools.external.mcp_runtime import shutdown_runtime

        shutdown_runtime()
    except Exception:  # noqa: BLE001 - teardown must never mask the test
        pass


def _server_config():
    return {
        SERVER_NAME: {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(SERVER_PATH)],
            "timeout_sec": 30,
        }
    }


def test_template_files_exist_and_config_example_is_valid_json():
    assert SERVER_PATH.is_file(), "template server is missing"
    assert (TEMPLATE_DIR / "README.md").is_file(), "template README is missing"
    example = TEMPLATE_DIR / "config.example.json"
    assert example.is_file(), "template config example is missing"
    config = json.loads(example.read_text(encoding="utf-8"))
    assert SERVER_NAME in config["mcps"]
    assert config["mcps"][SERVER_NAME]["transport"] == "stdio"


def test_template_avoids_postponed_annotations():
    """FastMCP reads parameter annotations at import time, so the template
    must not postpone them with ``from __future__ import annotations``."""
    source = SERVER_PATH.read_text(encoding="utf-8")
    assert not re.search(
        r"(?m)^\s*from __future__ import annotations\s*$", source
    ), "the template must not postpone annotations"


def test_template_round_trips_through_claires_mcp_client(
    shutdown_persistent_runtime,
):
    """Claire's real client discovers the template's tool and calls it."""
    from claire.tools.external.mcp_client import MCPClient

    client = MCPClient(_server_config())

    tools = client.list_tools(SERVER_NAME)
    names = [t.get("name") for t in tools]
    assert "echo" in names, f"template tool not discovered, got {names!r}"

    echo = next(t for t in tools if t.get("name") == "echo")
    assert echo.get("description")
    assert echo["inputSchema"]["properties"]["text"]["type"] == "string"

    result = client.invoke_tool(SERVER_NAME, "echo", {"text": "round trip"})
    assert result["isError"] is False
    assert result["text"] == "example heard: round trip"


def test_template_declares_a_tool_a_resource_and_a_prompt():
    """Claire negotiates protocol 2025-06-18, which carries all three
    capability families. Claire's own client reads tools only, so query the
    server with a plain SDK session to see the full declared surface."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def probe():
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER_PATH)]
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                prompts = await session.list_prompts()
                resources = await session.list_resources()
                return init, tools, prompts, resources

    init, tools, prompts, resources = asyncio.run(probe())

    assert init.protocolVersion
    assert init.capabilities.tools is not None
    assert init.capabilities.prompts is not None
    assert init.capabilities.resources is not None
    assert "echo" in [t.name for t in tools.tools]
    assert "summarise" in [p.name for p in prompts.prompts]
    assert "example://greeting" in [str(r.uri) for r in resources.resources]

"""MCP events (SEP-3415) through the real adapter, over HTTP, against a real server.

The server is the one in `tests/mcp_events_server.py`, run on a port of this machine,
and reached through the SDK's own client with nothing faked between them.
"""

import contextlib
from collections.abc import AsyncGenerator, Sequence

import pytest
from mcp import Client
from mcp.client import ClientExtension
from mcp.server.mcpserver import MCPServer

from app.adapters.mcp_adp import McpAdapterClass, McpEventType, McpServer
from tests.mcp_events_server import EventsServer, Running, running

_REPO = {"repo": "owner/one"}


@pytest.fixture
async def served() -> AsyncGenerator[Running]:
    async with running(EventsServer()) as server:
        yield server


@pytest.fixture
async def mcp() -> AsyncGenerator[McpAdapterClass]:
    adapter = McpAdapterClass()
    yield adapter
    await adapter.close()


async def test_a_servers_kinds_of_event_are_listed_with_its_tools(
    served: Running, mcp: McpAdapterClass
) -> None:
    listed = await mcp.list_tools(McpServer(served.url))

    assert listed.events == [
        McpEventType(
            name="issue.opened",
            description="Fires when an issue is opened in a repo",
            delivery=("poll", "push", "webhook"),
            input_schema={
                "type": "object",
                "properties": {"repo": {"type": "string", "description": "owner/name"}},
                "required": ["repo"],
            },
        ),
        McpEventType(
            name="build.finished",
            description="Fires when a build ends",
            delivery=("poll",),
            input_schema={"type": "object", "properties": {}},
        ),
    ]
    assert served.server.asked == ["events/list"]


async def test_a_server_without_the_extension_has_no_events() -> None:
    plain = MCPServer("plain")

    @plain.tool()
    async def ping() -> str:
        """Say pong."""
        return "pong"

    @contextlib.asynccontextmanager
    async def opener(_: McpServer, extensions: Sequence[ClientExtension]) -> AsyncGenerator[Client]:
        async with Client(plain, extensions=extensions) as client:
            yield client

    mcp = McpAdapterClass(opener)
    listed = await mcp.list_tools(McpServer("https://plain.example.com/mcp"))
    await mcp.close()

    assert [tool.name for tool in listed.tools] == [ping.__name__]
    assert listed.events == []

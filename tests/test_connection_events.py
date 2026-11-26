"""A connection's MCP events: its copy of the kinds its server serves, and watching one.

The server is the one in `tests/mcp_events_server.py`, on a port of this machine,
reached through the real adapter. Only the database is faked.
"""

import json
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.mcp_adp import McpAdapterClass
from app.entities.connection_ent import ConnectionEntityClass, ConnectionEvent
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.routers.connections.models import ConnectionsModels
from app.routers.connections.service import ConnectionsServiceV1Class
from tests.mcp_events_server import KINDS, EventsServer, Running, running
from tests.test_chat import _DocDB, _docdb  # pyright: ignore[reportPrivateUsage]


class _Setup(SimpleNamespace):
    docdb: _DocDB
    served: Running
    mcp: McpAdapterClass
    connections: ConnectionEntityClass
    one: GemConfig
    two: GemConfig


async def _setup(server: EventsServer) -> AsyncGenerator[_Setup]:
    docdb, adapter = _docdb()
    mcp = McpAdapterClass()
    gems = GemEntityClass(adapter)
    async with running(server) as served:
        yield _Setup(
            docdb=docdb,
            served=served,
            mcp=mcp,
            connections=ConnectionEntityClass(docdb=adapter, mcp=mcp, gems=gems),
            one=await gems.create(name="Gem One", model="6 Luna"),
            two=await gems.create(name="Gem Two", model="6 Luna"),
        )
        await mcp.close()


@pytest.fixture
async def setup() -> AsyncGenerator[_Setup]:
    async for made in _setup(EventsServer()):
        yield made


async def test_a_connection_keeps_the_kinds_of_event_its_server_serves(setup: _Setup) -> None:
    service = ConnectionsServiceV1Class(setup.connections)

    made = await service.create_connection(
        ConnectionsModels.V1.Input.CreateConnection(name="Events", url=setup.served.url)
    )

    assert [(event.name, event.description, event.delivery) for event in made.events] == [
        ("issue.opened", "Fires when an issue is opened in a repo", ["poll", "push", "webhook"]),
        ("build.finished", "Fires when a build ends", ["poll"]),
    ]
    kept = (await setup.connections.get(made.id)).events
    assert json.loads(kept[0].input_schema) == KINDS[0]["inputSchema"]
    assert [tool.name for tool in made.tools] == []


async def test_the_copy_of_the_events_is_read_again_only_when_the_user_asks(
    setup: _Setup,
) -> None:
    made = await setup.connections.create(name="Events", url=setup.served.url)
    setup.served.server.kinds = [KINDS[1]]

    unchanged = await setup.connections.get(str(made.id))
    refreshed = await setup.connections.refresh(str(made.id))

    assert [event.name for event in unchanged.events] == ["issue.opened", "build.finished"]
    assert [event.name for event in refreshed.events] == ["build.finished"]
    assert setup.served.server.asked == ["events/list", "events/list"]


async def test_kinds_of_event_that_cannot_be_watched_are_left_out() -> None:
    def kind(name: str, **changed: Any) -> dict[str, Any]:
        return {**KINDS[1], "name": name, **changed}

    kinds = [
        kind("plain"),
        kind("plain", description="listed twice"),
        kind(""),
        kind("no.way", delivery=["carrier-pigeon"]),
        kind("not.an.object", inputSchema={"type": "string"}),
        kind("bad.schema", inputSchema={"type": "object", "properties": {"a": {"type": 5}}}),
        kind(
            "in.a.header",
            inputSchema={
                "type": "object",
                "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
            },
        ),
        kind("some.ways", delivery=["push", "carrier-pigeon"]),
    ]
    async for setup in _setup(EventsServer(kinds=kinds)):
        made = await setup.connections.create(name="Events", url=setup.served.url)

        assert made.events == [
            ConnectionEvent(
                name="plain",
                description="Fires when a build ends",
                delivery=["poll"],
                input_schema='{"type": "object", "properties": {}}',
            ),
            ConnectionEvent(
                name="some.ways",
                description="Fires when a build ends",
                delivery=["push"],
                input_schema='{"type": "object", "properties": {}}',
            ),
        ]

"""Connections: the tool servers gems can use, and which gem may use which.

The server is a real MCP server run in this process, reached through the real adapter
and the SDK's client, so the protocol is the real one. Only the transport is not HTTP.
"""

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from bson import ObjectId
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import Icon
from pydantic import ValidationError

from app.adapters.mcp_adp import McpAbout, McpAdapterClass, McpFailure, McpListing, McpServer
from app.core.errors import InvalidError, NotFoundError, UpstreamError
from app.entities.call_ent import CallEntityClass
from app.entities.chat_ent import ChatEntityClass
from app.entities.connection_ent import Connection, ConnectionEntityClass, offered_name
from app.entities.conversation_ent import ConversationEntityClass
from app.entities.conversation_store import ConversationStoreClass
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.late_results import CANCEL_TOOL
from app.entities.model import Item, ToolSpec, to_messages
from app.entities.tools import Tool, gem_toolbox
from app.main import app
from app.routers.connections.controller import router
from app.routers.connections.models import ConnectionsModels
from app.routers.connections.service import (
    ConnectionsServiceV1Class,
    get_connections_service_v1,
)
from app.routers.gems.service import GemsServiceV1Class, get_gems_service_v1
from tests.test_chat import _DocDB, _docdb, _NoKey, _said, _tool_names
from tests.test_one_conversation import _Client, _hang_up, _Model, _said_and_heard, _spoken, _Voice
from tests.test_voice_conversation import _completed, _delta, _Item, _Search, _starts

_URL = "https://tools.example.com/mcp"


def _server(*, extra: bool = False, icons: list[Icon] | None = None) -> MCPServer:
    """A tool server: an issue tracker with a few tools, and the icons it states, if any."""
    server = MCPServer("tracker", icons=icons)

    @server.tool(title="Find issues")
    async def find_issues(text: str) -> str:
        """Find the issues that mention a text."""
        return f"2 issues mention {text}: #4 and #9."

    @server.tool()
    async def close_issue(number: int) -> str:
        """Close an issue."""
        if number < 0:
            raise ValueError("no such issue")
        return f"Closed #{number}."

    @server.tool(name="files.read", description="Read a file.")
    async def read_file(path: str) -> str:
        return "x" * 30_000 if path == "big" else f"the contents of {path}"

    if extra:

        @server.tool()
        async def delete_everything() -> str:
            """Delete every issue."""
            return "Gone."

    return server


class _Servers:
    """The adapter's way to a server, faked: each address is a server in this process.

    It counts how often each address was opened, and with which headers.
    """

    def __init__(self, **servers: MCPServer) -> None:
        self.at: dict[str, MCPServer] = {_URL: _server(), **servers}
        self.opened: list[McpServer] = []

    @contextlib.asynccontextmanager
    async def open(self, server: McpServer) -> AsyncGenerator[Client]:
        self.opened.append(server)
        if server.url not in self.at:
            raise ConnectionError("All connection attempts failed")
        async with Client(self.at[server.url]) as client:
            yield client


class _Setup(SimpleNamespace):
    docdb: _DocDB
    servers: _Servers
    icons: "_IconHost"
    mcp: McpAdapterClass
    gems: GemEntityClass
    connections: ConnectionEntityClass
    one: GemConfig
    two: GemConfig

    async def names(self, gem: GemConfig) -> list[str]:
        return [tool.name for tool in await self.connections.tools_for(gem.id)]

    async def tool(self, gem: GemConfig, name: str) -> Tool:
        return next(t for t in await self.connections.tools_for(gem.id) if t.name == name)


@pytest.fixture
async def setup() -> AsyncGenerator[_Setup]:
    docdb, adapter = _docdb()
    servers = _Servers()
    icons = _IconHost()
    mcp = McpAdapterClass(servers.open, transport=icons)
    gems = GemEntityClass(adapter)
    made = _Setup(
        docdb=docdb,
        servers=servers,
        icons=icons,
        mcp=mcp,
        gems=gems,
        connections=ConnectionEntityClass(docdb=adapter, mcp=mcp, gems=gems),
        one=await gems.create(name="Gem One", model="6 Luna"),
        two=await gems.create(name="Gem Two", model="6 Luna"),
    )
    yield made
    await mcp.close()


async def _run(tool: Tool, **arguments: object) -> str:
    return await tool.run(json.dumps(arguments))


# ---- the adapter, on the real protocol


async def test_a_server_s_tools_are_listed_as_it_lists_them() -> None:
    mcp = McpAdapterClass(_Servers().open)

    tools = (await mcp.list_tools(McpServer(_URL))).tools

    assert [(tool.name, tool.title, tool.description) for tool in tools] == [
        ("find_issues", "Find issues", "Find the issues that mention a text."),
        ("close_issue", None, "Close an issue."),
        ("files.read", None, "Read a file."),
    ]
    assert tools[1].input_schema["type"] == "object"
    assert tools[1].input_schema["required"] == ["number"]
    await mcp.close()


async def test_a_call_returns_its_result_as_text_and_a_tool_s_failure_is_a_result() -> None:
    mcp = McpAdapterClass(_Servers().open)
    server = McpServer(_URL)

    done = await mcp.call_tool(server, "close_issue", {"number": 4})
    failed = await mcp.call_tool(server, "close_issue", {"number": -1})

    assert (done.text, done.is_error) == ("Closed #4.", False)
    assert failed.is_error and "close_issue" in failed.text
    await mcp.close()


async def test_one_client_is_kept_for_a_server_and_used_by_every_call() -> None:
    servers = _Servers()
    mcp = McpAdapterClass(servers.open)
    server = McpServer(_URL, (("Authorization", "Bearer one"),))

    await mcp.list_tools(server)
    await asyncio.gather(*(mcp.call_tool(server, "close_issue", {"number": n}) for n in range(5)))

    assert servers.opened == [server]
    # Other headers are another way in: they get a client of their own.
    await mcp.call_tool(McpServer(_URL, (("Authorization", "Bearer two"),)), "close_issue", {})
    assert len(servers.opened) == 2
    await mcp.close()


async def test_a_server_that_cannot_be_reached_fails_in_words_and_is_tried_again() -> None:
    servers = _Servers()
    mcp = McpAdapterClass(servers.open)
    gone = McpServer("https://nowhere.example.com/mcp")

    with pytest.raises(McpFailure) as failure:
        await mcp.list_tools(gone)
    assert failure.value.reason == "All connection attempts failed"

    # The failed client is not kept: the next call opens a new one.
    servers.at[gone.url] = _server()
    assert len((await mcp.list_tools(gone)).tools) == 3
    assert servers.opened == [gone, gone]
    await mcp.close()


async def test_a_forgotten_server_s_client_is_closed_and_a_new_one_opened_when_needed() -> None:
    servers = _Servers()
    mcp = McpAdapterClass(servers.open)
    server = McpServer(_URL)
    await mcp.list_tools(server)

    await mcp.forget(server)
    await mcp.forget(server)  # nothing to forget is not an error
    await mcp.list_tools(server)

    assert servers.opened == [server, server]
    await mcp.close()


_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 24
_PNG_DATA = f"data:image/png;base64,{base64.b64encode(_PNG).decode()}"
_JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 24
_SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>'
# The server the icon tests ask: its icons may come from its own host, a domain above
# it, or one below it.
_ICONED = "https://mcp.tracker.example/mcp"


class _IconHost(httpx2.AsyncBaseTransport):
    """The web, as far as icons go: what each address answers, and what was asked."""

    def __init__(self) -> None:
        self.asked: list[httpx2.Request] = []
        self.answers: dict[str, httpx2.Response] = {}

    def serve(self, url: str, status: int = 200, content: bytes = b"", kind: str = "") -> str:
        headers = {"Content-Type": kind} if kind else {}
        self.answers[url] = httpx2.Response(status, headers=headers, content=content)
        return url

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.asked.append(request)
        return self.answers.get(str(request.url), httpx2.Response(404))


def _iconed(*icons: Icon, website_url: str | None = "https://tracker.example") -> MCPServer:
    return MCPServer("tracker", website_url=website_url, icons=list(icons))


async def _about(host: _IconHost, *icons: Icon, website_url: str | None = None) -> McpAbout:
    """How the icon tests' server shows itself, as listing its tools reads it."""
    server = _iconed(*icons, website_url=website_url)
    mcp = McpAdapterClass(_Servers(**{_ICONED: server}).open, transport=host)
    try:
        return (await mcp.list_tools(McpServer(_ICONED, (("X-Key", "secret"),), "a-pass"))).about
    finally:
        await mcp.close()


async def test_icons_are_read_with_the_tools_and_held_as_data() -> None:
    host = _IconHost()
    svg = host.serve("https://tracker.example/icon.svg", content=_SVG, kind="image/svg+xml")
    png = host.serve("https://cdn.mcp.tracker.example/i.png", content=_PNG, kind="image/png")

    about = await _about(
        host,
        Icon(src=_PNG_DATA, mime_type="image/png", sizes=["48x48"], theme="light"),
        Icon(src=svg, sizes=["any"], theme="dark"),
        Icon(src=png),
        website_url="https://tracker.example",
    )

    assert about.website_url == "https://tracker.example"
    assert [(icon.mime_type, icon.sizes, icon.theme) for icon in about.icons] == [
        ("image/png", ("48x48",), "light"),
        ("image/svg+xml", ("any",), "dark"),
        ("image/png", (), None),
    ]
    assert about.icons[0].src == _PNG_DATA
    assert base64.b64decode(about.icons[1].src.split(",", 1)[1]) == _SVG
    assert not about.unreached
    # Nothing that lets us in to the server goes with an icon.
    assert len(host.asked) == 2
    assert all(
        "authorization" not in request.headers
        and "x-key" not in request.headers
        and "cookie" not in request.headers
        for request in host.asked
    )


async def test_an_icon_is_fetched_only_from_the_server_s_own_domain_by_https() -> None:
    host = _IconHost()
    for url in (
        "https://elsewhere.example/i.png",  # another domain
        "https://other.tracker.example.net/i.png",  # a lookalike
        "https://cdn.tracker.example/i.png",  # beside the server, not above or below it
        "http://tracker.example/i.png",  # not https
        "https://user:pw@tracker.example/i.png",  # credentials in the address
        "https://127.0.0.1/i.png",
    ):
        host.serve(url, content=_PNG, kind="image/png")

    about = await _about(host, *(Icon(src=url) for url in host.answers))

    assert about == McpAbout()
    assert host.asked == []


async def test_an_icon_s_kind_is_told_by_its_bytes() -> None:
    host = _IconHost()
    html = host.serve("https://tracker.example/a.png", content=b"<html></html>", kind="image/png")
    lying = host.serve("https://tracker.example/b.png", content=_JPEG, kind="image/png")
    bare = host.serve("https://tracker.example/c", content=_PNG, kind="application/octet-stream")
    jpg = host.serve("https://tracker.example/d.jpg", content=_JPEG, kind="image/jpg")
    gif = "data:image/gif;base64,R0lGODlhAQABAAAAACw="

    about = await _about(
        host,
        Icon(src=html),
        Icon(src=lying),
        Icon(src=gif),
        Icon(src=bare, mime_type="image/png"),  # the icon's own type stands in
        Icon(src=jpg),  # `image/jpg` is JPEG too
    )

    assert [icon.mime_type for icon in about.icons] == ["image/png", "image/jpeg"]


async def test_an_svg_that_would_run_anything_is_not_kept() -> None:
    def data(svg: str) -> Icon:
        return Icon(src=f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}")

    about = await _about(
        _IconHost(),
        data('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
        data('<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'),
        data('<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>'),
        data('<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:x()"/></svg>'),
        # Percent-encoded, as SVGs often are, and with `BASE64` in capitals: both kept.
        Icon(src="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22/%3E"),
        Icon(src=f"data:image/png;BASE64,{base64.b64encode(_PNG).decode()}"),
    )

    assert [icon.mime_type for icon in about.icons] == ["image/svg+xml", "image/png"]


async def test_what_a_server_says_of_its_icons_is_checked_as_untrusted() -> None:
    host = _IconHost()
    huge = host.serve(
        "https://tracker.example/huge.png", content=_PNG + b"\0" * 64 * 1024, kind="image/png"
    )
    moved = host.serve("https://tracker.example/moved.png", 302)
    host.answers[moved].headers["Location"] = "https://tracker.example/i.png"
    many = [f"{n}x{n}" for n in range(1, 20)]

    about = await _about(
        host,
        Icon(src=huge),
        Icon(src=moved),
        Icon(src="data:image/png;base64,not base64!"),
        Icon(src=_PNG_DATA, sizes=["48x48", "big", "0x5", "any", *many]),
        website_url="http://[::1",
    )

    assert [icon.sizes for icon in about.icons] == [
        ("48x48", "any", "1x1", "2x2", "3x3", "4x4", "5x5", "6x6")
    ]
    assert about.website_url is None


async def test_at_most_three_icons_are_kept_in_the_server_s_order() -> None:
    icons = [Icon(src=_PNG_DATA, sizes=[f"{n}x{n}"]) for n in (16, 32, 48, 64)]

    about = await _about(_IconHost(), *icons)

    assert [icon.sizes for icon in about.icons] == [("16x16",), ("32x32",), ("48x48",)]


async def test_an_icon_whose_host_does_not_answer_is_told_apart() -> None:
    host = _IconHost()
    busy = host.serve("https://tracker.example/busy.png", 503)

    assert (await _about(host, Icon(src=busy), Icon(src=_PNG_DATA))).unreached
    # A 404 is an answer: the icon is not there, and there is nothing to wait for.
    gone = await _about(host, Icon(src="https://tracker.example/gone.png"))
    assert gone == McpAbout()


async def test_a_server_that_says_nothing_has_nothing_to_show() -> None:
    mcp = McpAdapterClass(_Servers().open, transport=_IconHost())

    assert (await mcp.list_tools(McpServer(_URL))).about == McpAbout()
    await mcp.close()


# ---- adding a connection


async def test_a_new_connection_keeps_its_server_s_tools_and_is_open_to_no_gem(
    setup: _Setup,
) -> None:
    made = await setup.connections.create(
        name="Issue Tracker", url=_URL, headers={"Authorization": "Bearer secret"}
    )

    assert (made.key, made.name, made.url, made.enabled) == (
        "issue_tracker",
        "Issue Tracker",
        _URL,
        True,
    )
    assert made.gems == []
    assert [tool.name for tool in made.tools] == ["find_issues", "close_issue", "files.read"]
    assert made.tools_left_out == 0
    # The server was asked with the headers given.
    assert setup.servers.opened == [McpServer(_URL, (("Authorization", "Bearer secret"),))]
    assert await setup.connections.list_all() == [made]
    # Until the user says which gems, none is offered its tools.
    assert await setup.names(setup.one) == []


async def test_a_connection_whose_server_does_not_answer_is_not_kept(setup: _Setup) -> None:
    with pytest.raises(UpstreamError) as refused:
        await setup.connections.create(name="Nowhere", url="https://nowhere.example.com/mcp")

    assert refused.value.status_code == 502
    assert refused.value.message == (
        "The server at https://nowhere.example.com/mcp could not be asked for its tools: "
        "All connection attempts failed"
    )
    assert await setup.connections.list_all() == []


async def test_two_connections_never_share_a_key(setup: _Setup) -> None:
    keys = [
        (await setup.connections.create(name=name, url=_URL)).key
        for name in ["Tracker", "tracker", "Tracker!", "…", "A very long name for a tool server"]
    ]

    assert keys == ["tracker", "tracker_2", "tracker_3", "connection", "a_very_long_name_for"]


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"name": "  "}, "name cannot be blank"),
        ({"url": "ftp://tools.example.com"}, "starts with http:// or https://"),
        ({"url": "https://"}, "starts with http:// or https://"),
        ({"url": "https://me:secret@tools.example.com/mcp"}, "user name or password"),
        ({"headers": {"Bad Name": "x"}}, "is not a header's name"),
        ({"headers": {"Content-Type": "text/plain"}}, "set by the protocol"),
        ({"headers": {"Mcp-Session-Id": "x"}}, "set by the protocol"),
        ({"headers": {"X-Key": "a\nb"}}, "line break"),
        ({"headers": {"X-Key": ""}}, "1 to 4000 characters"),
        ({"headers": {"X-Key": "a", "x-key": "b"}}, "more than once"),
    ],
)
def test_a_connection_that_cannot_be_used_is_refused(fields: dict[str, Any], why: str) -> None:
    with pytest.raises(ValidationError, match=why):
        ConnectionsModels.V1.Input.CreateConnection(**{"name": "Tracker", "url": _URL, **fields})


async def test_a_gem_that_is_not_there_cannot_be_named(setup: _Setup) -> None:
    missing = str(ObjectId())

    for gems in ([missing], ["not an id"]):
        with pytest.raises(InvalidError, match="There is no gem with the id"):
            await setup.connections.create(name="Tracker", url=_URL, gems=gems)
    assert await setup.connections.list_all() == []


# ---- who may use it


async def test_a_connection_is_offered_to_the_gems_it_is_open_to(setup: _Setup) -> None:
    made = await setup.connections.create(name="Tracker", url=_URL, gems=[str(setup.one.id)])
    names = ["tracker__find_issues", "tracker__close_issue", "tracker__files_read_601e4e"]

    assert await setup.names(setup.one) == names
    assert await setup.names(setup.two) == []

    await setup.connections.update(str(made.id), {"gems": "all"})
    assert await setup.names(setup.two) == names

    await setup.connections.update(str(made.id), {"enabled": False})
    assert await setup.names(setup.one) == []
    # Off, it still knows who it was open to.
    assert (await setup.connections.get(str(made.id))).gems == "all"


async def test_a_tool_s_limit_only_narrows_who_the_connection_is_open_to(setup: _Setup) -> None:
    one, two = str(setup.one.id), str(setup.two.id)
    made = await setup.connections.create(name="Tracker", url=_URL, gems=[one])

    # Only Gem Two may close issues, and nobody may read files.
    await setup.connections.update(
        str(made.id), {"tool_gems": {"close_issue": [two], "files.read": []}}
    )
    # The connection is not open to Gem Two, so the limit opens nothing to it.
    assert await setup.names(setup.one) == ["tracker__find_issues"]
    assert await setup.names(setup.two) == []

    await setup.connections.update(str(made.id), {"gems": "all"})
    assert await setup.names(setup.one) == ["tracker__find_issues"]
    assert await setup.names(setup.two) == ["tracker__find_issues", "tracker__close_issue"]

    # Only the tools named change: lifting one limit leaves the other.
    updated = await setup.connections.update(str(made.id), {"tool_gems": {"close_issue": None}})
    assert [(limit.tool, limit.gems) for limit in updated.limits] == [("files.read", [])]
    assert await setup.names(setup.one) == ["tracker__find_issues", "tracker__close_issue"]


async def test_a_limit_on_a_tool_the_connection_does_not_have_is_refused(setup: _Setup) -> None:
    made = await setup.connections.create(name="Tracker", url=_URL)

    with pytest.raises(InvalidError, match="The connection has no tool called: nope"):
        await setup.connections.update(str(made.id), {"tool_gems": {"nope": []}})
    with pytest.raises(InvalidError, match="There is no gem with the id"):
        await setup.connections.update(str(made.id), {"tool_gems": {"close_issue": ["x"]}})
    with pytest.raises(ValueError, match="cannot be changed"):
        await setup.connections.update(str(made.id), {"key": "other"})


async def test_a_deleted_gem_is_taken_out_of_every_connection(setup: _Setup) -> None:
    one, two = str(setup.one.id), str(setup.two.id)
    made = await setup.connections.create(name="Tracker", url=_URL, gems=[one, two])
    open_to_all = await setup.connections.create(name="Other", url=_URL, gems="all")
    await setup.connections.update(str(made.id), {"tool_gems": {"close_issue": [one, two]}})
    service = GemsServiceV1Class(setup.gems, connections=setup.connections)

    await service.delete_gem(one)

    after = await setup.connections.get(str(made.id))
    assert after.gems == [two]
    assert after.limit("close_issue") == [two]
    assert (await setup.connections.get(str(open_to_all.id))).gems == "all"


# ---- the tools a gem is offered


async def test_a_connection_s_tool_is_told_to_the_model_under_its_key(setup: _Setup) -> None:
    await setup.connections.create(name="Issue Tracker", url=_URL, gems="all")

    tools = await setup.connections.tools_for(setup.one.id)

    spec = tools[0].spec
    assert spec.name == "issue_tracker__find_issues"
    assert spec.description == "Find the issues that mention a text."
    assert spec.parameters["properties"] == {"text": {"title": "Text", "type": "string"}}
    # The schema is the server's, so the model's arguments are not held to it.
    assert all(not tool.spec.strict for tool in tools)
    # What the client is shown of a call: the connection, and the tool by its title.
    assert tools[0].shown("{}") == "Issue Tracker: Find issues"
    assert tools[1].shown("{}") == "Issue Tracker: close_issue"


def test_a_tool_s_name_is_made_to_fit_what_a_provider_takes() -> None:
    assert offered_name("tracker", "find_issues") == "tracker__find_issues"
    # A name with a letter a provider refuses, or too long, ends in a mark of its own,
    # so two tools never end up with one name.
    dotted, spaced = offered_name("tracker", "files.read"), offered_name("tracker", "files read")
    assert dotted.startswith("tracker__files_read_") and spaced.startswith("tracker__files_read_")
    assert dotted != spaced
    long = offered_name("a_very_long_name_for", "x" * 200)
    assert len(long) == 64
    assert long != offered_name("a_very_long_name_for", "x" * 201)


async def test_a_call_runs_on_the_server_and_its_result_goes_to_the_model(setup: _Setup) -> None:
    await setup.connections.create(name="Tracker", url=_URL, gems="all")
    toolbox = gem_toolbox(
        [],
        cast("Any", _Search(configured=False)),
        _cancel(),
        [],
        await setup.connections.tools_for(setup.one.id),
    )
    shown: list[str] = []

    found = await toolbox.run("tracker__find_issues", '{"text": "login"}', show=shown.append)
    failed = await toolbox.run("tracker__close_issue", '{"number": -1}', show=shown.append)
    unread = await toolbox.run("tracker__close_issue", "[1]", show=shown.append)

    assert found == "2 issues mention login: #4 and #9."
    assert failed.startswith("The tool failed: ")
    assert unread == "tracker__close_issue failed: the arguments are not an object"
    assert shown == ["Tracker: Find issues", "Tracker: close_issue", "Tracker: close_issue"]


async def test_a_long_result_is_cut_and_says_so(setup: _Setup) -> None:
    await setup.connections.create(name="Tracker", url=_URL, gems="all")
    read = await setup.tool(setup.one, "tracker__files_read_601e4e")

    result = await _run(read, path="big")

    assert result == (
        "x" * 20_000 + "\n[The result was 30000 characters long. Only the first 20000 are shown.]"
    )
    assert await _run(read, path="notes.txt") == "the contents of notes.txt"


async def test_a_tool_closed_to_a_gem_is_refused_from_that_moment(setup: _Setup) -> None:
    # The tools a call offers are fixed when it opens. What they may do is not.
    made = await setup.connections.create(name="Tracker", url=_URL, gems="all")
    close = await setup.tool(setup.one, "tracker__close_issue")
    assert await _run(close, number=4) == "Closed #4."
    refused = "This tool is not available to you now."

    await setup.connections.update(str(made.id), {"tool_gems": {"close_issue": []}})
    assert await _run(close, number=4) == refused

    await setup.connections.update(str(made.id), {"tool_gems": {"close_issue": None}})
    assert await _run(close, number=4) == "Closed #4."

    await setup.connections.update(str(made.id), {"gems": [str(setup.two.id)]})
    assert await _run(close, number=4) == refused

    await setup.connections.update(str(made.id), {"gems": "all", "enabled": False})
    assert await _run(close, number=4) == refused

    await setup.connections.delete(str(made.id))
    assert await _run(close, number=4) == refused


async def test_a_call_uses_the_address_and_headers_as_they_are_now(setup: _Setup) -> None:
    made = await setup.connections.create(
        name="Tracker", url=_URL, headers={"Authorization": "Bearer old"}, gems="all"
    )
    close = await setup.tool(setup.one, "tracker__close_issue")

    await setup.connections.update(str(made.id), {"headers": {"Authorization": "Bearer new"}})
    assert await _run(close, number=4) == "Closed #4."

    assert [dict(server.headers) for server in setup.servers.opened] == [
        {"Authorization": "Bearer old"},
        # Asked for its tools again with the new header, and called with it.
        {"Authorization": "Bearer new"},
    ]


# ---- changing a connection, and asking its server again


async def test_a_new_address_asks_the_new_server_and_a_dead_one_changes_nothing(
    setup: _Setup,
) -> None:
    other = "https://other.example.com/mcp"
    setup.servers.at[other] = _server(extra=True)
    made = await setup.connections.create(name="Tracker", url=_URL)

    moved = await setup.connections.update(str(made.id), {"url": other, "name": "Issues"})

    assert (moved.url, moved.name, moved.key) == (other, "Issues", "tracker")
    assert [tool.name for tool in moved.tools][-1] == "delete_everything"

    with pytest.raises(UpstreamError):
        await setup.connections.update(
            str(made.id), {"url": "https://nowhere.example.com/mcp", "name": "Lost"}
        )
    assert await setup.connections.get(str(made.id)) == moved


async def test_a_refresh_keeps_what_the_server_lists_now_and_the_limits(setup: _Setup) -> None:
    made = await setup.connections.create(name="Tracker", url=_URL, gems="all")
    await setup.connections.update(str(made.id), {"tool_gems": {"close_issue": []}})
    # A change at the server reaches no gem until the user asks for it.
    setup.servers.at[_URL] = _server(extra=True)
    await setup.mcp.forget(made.server)
    assert "tracker__delete_everything" not in await setup.names(setup.one)

    refreshed = await setup.connections.refresh(str(made.id))

    assert [tool.name for tool in refreshed.tools] == [
        "find_issues",
        "close_issue",
        "files.read",
        "delete_everything",
    ]
    assert refreshed.checked_at > made.checked_at
    assert refreshed.limit("close_issue") == []
    assert "tracker__close_issue" not in await setup.names(setup.one)
    assert refreshed.gems == "all"  # who it is open to is untouched
    assert await setup.names(setup.one) == [
        "tracker__find_issues",
        "tracker__files_read_601e4e",
        "tracker__delete_everything",
    ]


async def test_a_connection_keeps_its_server_s_icons_and_a_refresh_takes_them_again(
    setup: _Setup,
) -> None:
    setup.servers.at[_URL] = _iconed(Icon(src=_PNG_DATA, sizes=["48x48"], theme="dark"))
    made = await setup.connections.create(name="Tracker", url=_URL)

    assert [(icon.src, icon.sizes, icon.theme) for icon in made.icons] == [
        (_PNG_DATA, ["48x48"], "dark")
    ]
    assert made.website_url == "https://tracker.example"
    assert Connection.model_validate(setup.docdb.collections["Connections"][0]) == made

    # The server no longer says how it shows itself. A refresh opens a new client to it,
    # so it is heard, with nothing forgotten by hand first.
    setup.servers.at[_URL] = _server()
    refreshed = await setup.connections.refresh(str(made.id))
    assert (refreshed.icons, refreshed.website_url) == ([], None)
    assert setup.docdb.collections["Connections"][0]["icons"] == []


async def test_a_good_icon_stays_when_its_host_does_not_answer_for_a_moment(
    setup: _Setup,
) -> None:
    icon = setup.icons.serve("https://example.com/i.png", content=_PNG, kind="image/png")
    setup.servers.at[_URL] = _iconed(Icon(src=icon))
    made = await setup.connections.create(name="Tracker", url=_URL)
    assert [one.mime_type for one in made.icons] == ["image/png"]

    setup.icons.serve(icon, 503)
    kept = await setup.connections.refresh(str(made.id))
    assert kept.icons == made.icons

    # An answer that it is gone is an answer: the icon goes.
    setup.icons.serve(icon, 404)
    gone = await setup.connections.refresh(str(made.id))
    assert gone.icons == []


async def test_an_icon_the_client_cannot_take_or_read_does_not_stop_a_connection(
    setup: _Setup,
) -> None:
    garbled = setup.icons.serve("https://tools.example.com/z.png", content=b"not gzip")
    setup.icons.answers[garbled].headers["Content-Encoding"] = "gzip"
    setup.servers.at[_URL] = _iconed(
        Icon(src="https://tools.example.com:abc/i.png"),
        Icon(src="https://tools.example.com/\x01.png"),
        Icon(src=garbled),
        Icon(src=_PNG_DATA),
    )

    made = await setup.connections.create(name="Tracker", url=_URL)

    assert [icon.src for icon in made.icons] == [_PNG_DATA]


async def test_a_new_address_keeps_none_of_the_old_server_s_icons(setup: _Setup) -> None:
    setup.servers.at[_URL] = _iconed(Icon(src=_PNG_DATA))
    made = await setup.connections.create(name="Tracker", url=_URL)
    other = "https://other.example.com/mcp"
    busy = setup.icons.serve("https://other.example.com/i.png", 503)
    setup.servers.at[other] = _iconed(Icon(src=busy))

    moved = await setup.connections.update(str(made.id), {"url": other})

    assert [icon.src for icon in made.icons] == [_PNG_DATA]
    assert moved.icons == []


async def test_a_website_the_server_names_badly_does_not_stop_a_connection(
    setup: _Setup,
) -> None:
    setup.servers.at[_URL] = _iconed(website_url="http://[::1")

    made = await setup.connections.create(name="Tracker", url=_URL)

    assert made.website_url is None


async def test_a_connection_kept_before_icons_reads_back_with_none(setup: _Setup) -> None:
    await setup.connections.create(name="Tracker", url=_URL)
    document = setup.docdb.collections["Connections"][0]
    del document["icons"], document["website_url"]

    (read,) = await setup.connections.list_all()

    assert (read.icons, read.website_url) == ([], None)


async def test_a_limit_outlives_a_tool_that_goes_and_comes_back(setup: _Setup) -> None:
    made = await setup.connections.create(name="Tracker", url=_URL, gems="all")
    setup.servers.at[_URL] = _server(extra=True)
    await setup.mcp.forget(made.server)
    await setup.connections.refresh(str(made.id))
    await setup.connections.update(str(made.id), {"tool_gems": {"delete_everything": []}})

    setup.servers.at[_URL] = _server()
    await setup.mcp.forget(made.server)
    without = await setup.connections.refresh(str(made.id))
    setup.servers.at[_URL] = _server(extra=True)
    await setup.mcp.forget(made.server)
    back = await setup.connections.refresh(str(made.id))

    assert "delete_everything" not in [tool.name for tool in without.tools]
    assert "delete_everything" in [tool.name for tool in back.tools]
    # It comes back as closed as the user left it.
    assert "tracker__delete_everything" not in await setup.names(setup.one)


async def test_tools_that_cannot_be_offered_are_left_out_and_counted(setup: _Setup) -> None:
    listed = [
        SimpleNamespace(name="ok", title=None, description=" Fine. ", input_schema=_OBJECT),
        SimpleNamespace(name="ok", title=None, description="Twice.", input_schema=_OBJECT),
        SimpleNamespace(name="", title=None, description="No name.", input_schema=_OBJECT),
        SimpleNamespace(name="list", title=None, description="", input_schema={"type": "array"}),
        SimpleNamespace(
            name="huge",
            title=None,
            description="",
            input_schema={"type": "object", "description": "x" * 20_000},
        ),
        SimpleNamespace(name="wordy", title=None, description="y" * 5_000, input_schema=_OBJECT),
    ]

    class _Lists:
        async def list_tools(self, server: McpServer) -> McpListing:
            return McpListing(cast("Any", listed), McpAbout())

    connections = ConnectionEntityClass(
        docdb=cast("Any", setup.docdb), mcp=cast("Any", _Lists()), gems=setup.gems
    )

    made = await connections.create(name="Odd", url=_URL)

    assert [(tool.name, len(tool.description)) for tool in made.tools] == [
        ("ok", 5),
        ("wordy", 1024),
    ]
    assert made.tools_left_out == 4


_OBJECT: dict[str, object] = {"type": "object", "properties": {}}


async def test_a_deleted_connection_is_gone_and_its_client_closed(setup: _Setup) -> None:
    made = await setup.connections.create(name="Tracker", url=_URL, gems="all")

    await setup.connections.delete(str(made.id))

    assert await setup.connections.list_all() == []
    assert await setup.names(setup.one) == []
    for missing in (str(made.id), "not an id"):
        with pytest.raises(NotFoundError):
            await setup.connections.get(missing)
    with pytest.raises(NotFoundError):
        await setup.connections.delete(str(made.id))


# ---- the toolbox


def _spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description=f"The {name} tool.", parameters={})


def _cancel() -> Tool:
    async def run(arguments: str) -> str:
        return "Cancelled."

    return Tool(CANCEL_TOOL, run)


def _connected(*names: str) -> list[Tool]:
    async def run(arguments: str) -> str:
        return "done"

    return [Tool(_spec(name), run) for name in names]


def _called(name: str) -> Item:
    return {"type": "function_call", "call_id": f"c_{name}", "name": name, "arguments": "{}"}


def _names(search: _Search, history: list[Item], *connected: str, tools: tuple[str, ...]) -> Any:
    toolbox = gem_toolbox(tools, cast("Any", search), _cancel(), history, _connected(*connected))
    return [spec.name for spec in toolbox.specs]


def test_a_gem_s_tools_always_come_in_the_same_order() -> None:
    on, off = _Search(), _Search(configured=False)

    assert _names(on, [], "t__a", "t__b", tools=("web_search",)) == [
        "web_search",
        "cancel_tool_call",
        "t__a",
        "t__b",
    ]
    # A connection's tool can outlive its reply too, so the cancel comes with any tool.
    assert _names(off, [], "t__a", tools=("web_search",)) == ["cancel_tool_call", "t__a"]
    assert _names(on, [], "t__a", tools=()) == ["cancel_tool_call", "t__a"]
    assert _names(off, [], tools=("web_search",)) == []


async def test_a_tool_called_once_and_gone_since_stays_declared_and_says_so() -> None:
    # A provider refuses a history with calls of a tool it is not told of.
    history = [_called("t__gone"), _called("web_search"), _called("t__a"), _called("not a name")]
    toolbox = gem_toolbox(
        (), cast("Any", _Search(configured=False)), _cancel(), history, _connected("t__a")
    )

    assert [spec.name for spec in toolbox.specs] == [
        "web_search",
        "cancel_tool_call",
        "t__a",
        "t__gone",
    ]
    gone = toolbox.specs[-1]
    assert (gone.description, gone.strict) == ("This tool is not available to you now.", False)
    assert gone.parameters == {"type": "object", "properties": {}}
    for name in ("t__gone", "web_search"):
        assert await toolbox.run(name, "{}", show=print) == "This tool is not available to you now."
    assert await toolbox.run("t__a", "{}", show=print) == "done"


# ---- in writing and in a call


def _tool_call(name: str, arguments: dict[str, object], call_id: str = "call_1") -> _Item:
    return _Item(type="function_call", call_id=call_id, name=name, arguments=json.dumps(arguments))


class _World(SimpleNamespace):
    setup: _Setup
    llm: _Model
    store: ConversationStoreClass
    chat: ChatEntityClass
    calls: CallEntityClass
    voice: _Voice

    async def write(self, gem: GemConfig, text: str) -> _Client:
        client = _Client()
        turn = self.chat.send(str(gem.id), text, client.deliver)
        async with asyncio.timeout(2):
            await turn.over.wait()
        return client


def _world(setup: _Setup, *rounds: list[Any]) -> _World:
    adapter = cast("Any", setup.docdb)
    store = ConversationStoreClass(adapter)
    conversations = ConversationEntityClass(gems=setup.gems, store=store)
    llm = _Model(*rounds)
    search = _Search(configured=False)
    chat = ChatEntityClass(
        gems=setup.gems,
        darbar=DarbarEntityClass(adapter),
        conversations=conversations,
        openai=cast("Any", llm),
        anthropic=cast("Any", _NoKey()),
        search=cast("Any", search),
        connections=setup.connections,
    )
    voice = _Voice(llm, search)
    calls = CallEntityClass(
        gems=setup.gems,
        darbar=DarbarEntityClass(adapter),
        voice=cast("Any", voice),
        conversations=conversations,
        connections=setup.connections,
    )
    return _World(setup=setup, llm=llm, store=store, chat=chat, calls=calls, voice=voice)


async def test_a_gem_uses_a_connection_s_tool_in_writing(setup: _Setup) -> None:
    await setup.connections.create(
        name="Tracker", url=_URL, gems=[str(setup.one.id)], deferred=False
    )
    call = _tool_call("tracker__find_issues", {"text": "login"})
    world = _world(
        setup,
        [_delta("Let me look."), _starts(call), _completed(call)],
        _said("Two: four and nine."),
    )
    elsewhere = _world(setup, _said("Hello."))

    client = await world.write(setup.one, "which issues mention login?")
    await elsewhere.write(setup.two, "hello")

    assert client.frames == [
        ("chat.text", "Let me look."),
        ("chat.tool", "Tracker: Find issues"),
        ("chat.text", "Two: four and nine."),
        ("chat.done", "Let me look. Two: four and nine."),
    ]
    first, second = world.llm.requests
    (other,) = elsewhere.llm.requests
    assert _tool_names(first) == [
        "cancel_tool_call",
        "tracker__find_issues",
        "tracker__close_issue",
        "tracker__files_read_601e4e",
    ]
    # The server's schema is sent as it is, and not as a strict one.
    offered = first["tools"][1]
    assert offered["strict"] is False
    assert offered["parameters"]["required"] == ["text"]
    assert second["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "2 issues mention login: #4 and #9.",
    }
    # The gem the connection is not open to is told of no tool.
    assert _tool_names(other) == []
    shown = await GemsServiceV1Class(setup.gems, world.chat._conversations).get_chat(  # pyright: ignore[reportPrivateUsage]
        str(setup.one.id)
    )
    assert [(m.role, m.text) for m in shown.messages][2] == ("tool", "Tracker: Find issues")


async def test_a_connection_closed_to_a_gem_applies_from_the_next_message(setup: _Setup) -> None:
    made = await setup.connections.create(name="Tracker", url=_URL, gems="all")
    call = _tool_call("tracker__find_issues", {"text": "login"})
    world = _world(setup, [_starts(call), _completed(call)], _said("Two."), _said("I cannot now."))

    await world.write(setup.one, "which issues mention login?")
    await setup.connections.update(str(made.id), {"gems": []})
    await world.write(setup.one, "and now?")

    # The conversation holds a call of the tool, so it stays declared, as one that is
    # not available, and nothing else of the connection's is told.
    after = world.llm.requests[2]
    assert _tool_names(after) == ["tracker__find_issues"]
    assert after["tools"][0]["description"] == "This tool is not available to you now."


async def test_a_call_offers_the_tools_the_gem_s_connections_had_when_it_opened(
    setup: _Setup,
) -> None:
    made = await setup.connections.create(
        name="Tracker", url=_URL, gems=[str(setup.one.id)], deferred=False
    )
    call_1 = _tool_call("tracker__close_issue", {"number": 4})
    call_2 = _tool_call("tracker__close_issue", {"number": 9}, "call_2")
    world = _world(
        setup,
        [_starts(call_1), _completed(call_1)],
        _spoken("Closed number four."),
        [_starts(call_2), _completed(call_2)],
        _spoken("I can no longer do that."),
    )

    call = await world.calls.open(str(setup.one.id))
    f, keeping = world.voice.calls[-1], asyncio.create_task(call.keep())
    await _said_and_heard(f, "close issue four")
    # Closed to the gem while the call is open: the tool list stays, the call is refused.
    await setup.connections.update(str(made.id), {"gems": []})
    await _said_and_heard(f, "and nine")
    await _hang_up(call, keeping)

    requests = world.llm.requests
    assert all(_tool_names(request)[0] == "cancel_tool_call" for request in requests)
    assert all(len(_tool_names(request)) == 4 for request in requests)
    assert requests[1]["input"][-1]["output"] == "Closed #4."
    assert requests[3]["input"][-1]["output"] == "This tool is not available to you now."


async def test_a_call_whose_connections_cannot_be_read_fails_in_words(setup: _Setup) -> None:
    class _Broken:
        async def tools_for(self, gem_id: ObjectId) -> list[Tool]:
            raise ConnectionError("the database went away")

    world = _world(setup)
    world.calls._connections = cast("Any", _Broken())  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(Exception, match="the gem's connections could not be read"):
        await world.calls.open(str(setup.one.id))


# ---- the routes


def test_the_routes_are_the_ones_a_client_expects() -> None:
    routes = sorted(
        (route.path, method)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    )

    assert routes == [
        ("/v1/connections", "GET"),
        ("/v1/connections", "POST"),
        ("/v1/connections/sign-in/callback", "GET"),
        ("/v1/connections/{connection_id}", "DELETE"),
        ("/v1/connections/{connection_id}", "GET"),
        ("/v1/connections/{connection_id}", "PATCH"),
        ("/v1/connections/{connection_id}/refresh", "POST"),
        ("/v1/connections/{connection_id}/sign-in", "POST"),
    ]


async def test_a_connection_is_sent_without_what_lets_us_in(setup: _Setup) -> None:
    service = ConnectionsServiceV1Class(setup.connections)
    one = str(setup.one.id)

    made = await service.create_connection(
        ConnectionsModels.V1.Input.CreateConnection(
            name="Issue Tracker",
            url=_URL,
            headers={"Authorization": "Bearer secret", "X-Team": "core"},
            gems=[one],
        )
    )
    changed = await service.update_connection(
        made.id,
        ConnectionsModels.V1.Input.UpdateConnection(tool_gems={"close_issue": []}),
    )

    sent = json.loads(changed.model_dump_json())
    assert "secret" not in changed.model_dump_json()
    assert sent["headers"] == ["Authorization", "X-Team"]
    assert {key: sent[key] for key in ("key", "name", "url", "enabled", "gems")} == {
        "key": "issue_tracker",
        "name": "Issue Tracker",
        "url": _URL,
        "enabled": True,
        "gems": [one],
    }
    assert sent["tools"] == [
        {
            "name": "find_issues",
            "title": "Find issues",
            "description": "Find the issues that mention a text.",
            "offered_as": "issue_tracker__find_issues",
            "gems": None,
        },
        {
            "name": "close_issue",
            "title": None,
            "description": "Close an issue.",
            "offered_as": "issue_tracker__close_issue",
            "gems": [],
        },
        {
            "name": "files.read",
            "title": None,
            "description": "Read a file.",
            "offered_as": "issue_tracker__files_read_601e4e",
            "gems": None,
        },
    ]
    assert sent["tools_left_out"] == 0
    # It gets in by its headers: it does not sign in.
    assert sent["sign_in"] is None
    assert [one.id for one in await service.list_connections()] == [made.id]
    assert (await service.get_connection(made.id)).id == made.id
    assert (await service.refresh_connection(made.id)).tools[1].gems == []
    await service.delete_connection(made.id)
    assert await service.list_connections() == []


async def test_a_connection_is_sent_with_its_server_s_icons_and_website(setup: _Setup) -> None:
    service = ConnectionsServiceV1Class(setup.connections)
    setup.servers.at[_URL] = _iconed(Icon(src=_PNG_DATA, sizes=["48x48"], theme="dark"))

    made = await service.create_connection(
        ConnectionsModels.V1.Input.CreateConnection(name="Issue Tracker", url=_URL)
    )

    sent = json.loads(made.model_dump_json())
    assert sent["icons"] == [
        {"src": _PNG_DATA, "mime_type": "image/png", "sizes": ["48x48"], "theme": "dark"}
    ]
    assert sent["website_url"] == "https://tracker.example"
    # A server that states nothing is sent with nothing, not without the fields.
    setup.servers.at[_URL] = _server()
    plain = json.loads((await service.refresh_connection(made.id)).model_dump_json())
    assert (plain["icons"], plain["website_url"]) == ([], None)


async def test_a_connection_is_deferred_unless_the_user_says_not(setup: _Setup) -> None:
    service = ConnectionsServiceV1Class(setup.connections)
    made = await service.create_connection(
        ConnectionsModels.V1.Input.CreateConnection(name="Tracker", url=_URL, gems="all")
    )
    assert made.deferred is True
    tools = await setup.connections.tools_for(setup.one.id)
    assert [(tool.spec.deferred, tool.connection) for tool in tools] == [(True, "Tracker")] * 3

    changed = await service.update_connection(
        made.id, ConnectionsModels.V1.Input.UpdateConnection(deferred=False)
    )

    assert changed.deferred is False
    assert (await service.get_connection(made.id)).deferred is False
    assert not any(tool.spec.deferred for tool in await setup.connections.tools_for(setup.one.id))
    loaded = await service.create_connection(
        ConnectionsModels.V1.Input.CreateConnection(name="Other", url=_URL, deferred=False)
    )
    assert loaded.deferred is False


def test_a_change_names_only_what_changes_and_nothing_may_be_null() -> None:
    change = ConnectionsModels.V1.Input.UpdateConnection(
        gems="all", tool_gems={"close_issue": None}
    )

    assert change.changes() == {"gems": "all", "tool_gems": {"close_issue": None}}
    for field in ("name", "url", "headers", "gems", "enabled", "deferred", "tool_gems"):
        with pytest.raises(ValidationError, match=f"A connection's {field} cannot be null"):
            ConnectionsModels.V1.Input.UpdateConnection(**{field: None})


async def test_a_stored_connection_reads_back_as_it_was_kept(setup: _Setup) -> None:
    made = await setup.connections.create(
        name="Tracker", url=_URL, headers={"Authorization": "Bearer secret"}, gems="all"
    )

    document = setup.docdb.collections["Connections"][0]
    # A schema is kept as text: its own keys are not ones a document's fields may have.
    assert isinstance(document["tools"][0]["input_schema"], str)
    assert document["headers"] == [{"name": "Authorization", "value": "Bearer secret"}]
    assert Connection.model_validate(document) == made


# ---- found in review


class _SlowLists:
    """The adapter, with a server that takes its time to list its tools."""

    def __init__(self) -> None:
        self.asked = asyncio.Event()
        self.answer = asyncio.Event()

    async def list_tools(self, server: McpServer) -> McpListing:
        self.asked.set()
        await self.answer.wait()
        tool = SimpleNamespace(name="close_issue", title=None, description="", input_schema=_OBJECT)
        return McpListing(cast("Any", [tool]), McpAbout())

    async def forget(self, server: McpServer) -> None:
        pass


@pytest.mark.parametrize("slow", ["refresh", "new address"])
async def test_closing_a_connection_while_its_server_is_asked_is_not_undone(
    setup: _Setup, slow: str
) -> None:
    # Asking a server can take many seconds. What is closed meanwhile stays closed.
    made = await setup.connections.create(name="Tracker", url=_URL, gems="all")
    lists = _SlowLists()
    connections = ConnectionEntityClass(
        docdb=cast("Any", setup.docdb), mcp=cast("Any", lists), gems=setup.gems
    )
    asking = asyncio.create_task(
        connections.refresh(str(made.id))
        if slow == "refresh"
        else connections.update(str(made.id), {"url": "https://other.example.com/mcp"})
    )
    await lists.asked.wait()

    await setup.connections.update(
        str(made.id), {"gems": [], "enabled": False, "tool_gems": {"close_issue": []}}
    )
    lists.answer.set()
    await asking

    after = await setup.connections.get(str(made.id))
    assert (after.gems, after.enabled, after.limit("close_issue")) == ([], False, [])
    assert [tool.name for tool in after.tools] == ["close_issue"]
    assert await setup.names(setup.one) == []


async def test_two_tools_the_model_would_be_told_by_one_name_are_not_both_kept(
    setup: _Setup,
) -> None:
    # Both would be told as `odd__files_read_601e4e`, and a toolbox refuses that.
    listed = [
        SimpleNamespace(name=name, title=None, description="", input_schema=_OBJECT)
        for name in ("files.read", "files_read_601e4e")
    ]

    class _Lists:
        async def list_tools(self, server: McpServer) -> McpListing:
            return McpListing(cast("Any", listed), McpAbout())

    connections = ConnectionEntityClass(
        docdb=cast("Any", setup.docdb), mcp=cast("Any", _Lists()), gems=setup.gems
    )

    made = await connections.create(name="Odd", url=_URL, gems="all")

    assert [tool.name for tool in made.tools] == ["files.read"]
    assert made.tools_left_out == 1
    gem_toolbox(
        (), cast("Any", _Search()), _cancel(), [], await connections.tools_for(setup.one.id)
    )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "anyOf": [{"required": ["a"]}, {"required": ["b"]}]},
        {"type": "object", "properties": {"first name!": {"type": "string"}}},
        {"type": "object", "properties": {"a": {"type": "str"}}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": "a"},
        {"type": "object", "properties": []},
        {"type": "array"},
        {"properties": {}},
    ],
)
async def test_a_schema_a_provider_would_refuse_is_left_out(
    setup: _Setup, schema: dict[str, object]
) -> None:
    # One refused tool fails the whole request, for every gem the connection is open to.
    listed = [
        SimpleNamespace(name="bad", title=None, description="", input_schema=schema),
        SimpleNamespace(name="good", title=None, description="", input_schema=_OBJECT),
    ]

    class _Lists:
        async def list_tools(self, server: McpServer) -> McpListing:
            return McpListing(cast("Any", listed), McpAbout())

    connections = ConnectionEntityClass(
        docdb=cast("Any", setup.docdb), mcp=cast("Any", _Lists()), gems=setup.gems
    )

    made = await connections.create(name="Odd", url=_URL)

    assert ([tool.name for tool in made.tools], made.tools_left_out) == (["good"], 1)


async def test_a_gem_is_offered_no_more_tools_than_a_provider_takes(setup: _Setup) -> None:
    listed = [
        SimpleNamespace(name=f"tool_{n}", title=None, description="", input_schema=_OBJECT)
        for n in range(70)
    ]

    class _Lists:
        async def list_tools(self, server: McpServer) -> McpListing:
            return McpListing(cast("Any", listed), McpAbout())

    connections = ConnectionEntityClass(
        docdb=cast("Any", setup.docdb), mcp=cast("Any", _Lists()), gems=setup.gems
    )
    await connections.create(name="First", url=_URL, gems="all")
    await connections.create(name="Second", url=_URL, gems="all")

    names = [tool.name for tool in await connections.tools_for(setup.one.id)]

    assert len(names) == 100
    assert (names[0], names[69], names[70], names[-1]) == (
        "first__tool_0",
        "first__tool_69",
        "second__tool_0",
        "second__tool_29",
    )


def test_a_refused_connection_does_not_send_back_what_was_sent() -> None:
    # No lifespan, and services that are never reached: the request is refused first.
    app.dependency_overrides[get_connections_service_v1] = lambda: None
    app.dependency_overrides[get_gems_service_v1] = lambda: None
    client = TestClient(app)
    answer = client.post(
        "/v1/connections",
        json={
            "name": "Tracker",
            "url": _URL,
            "headers": {"Authorization": "Bearer s3cret ", "X-Other": "alsosecret"},
        },
    )

    assert answer.status_code == 422
    assert "s3cret" not in answer.text and "alsosecret" not in answer.text
    assert "Authorization header's value" in answer.json()["detail"][0]["msg"]
    # Elsewhere FastAPI's answer is as it was.
    assert "input" in client.post("/v1/gems", json={"name": ""}).json()["detail"][0]
    app.dependency_overrides.clear()


@pytest.mark.parametrize("arguments", ["", "{", "[1]", '"x"'])
def test_a_stored_call_whose_arguments_are_no_object_is_sent_with_none(arguments: str) -> None:
    # A tool that is not strict can be called so. The call stays in the history, and
    # must not fail every later reply on a model that wants an object.
    messages = to_messages(
        [
            {"role": "user", "content": "close it"},
            {"type": "function_call", "call_id": "c1", "name": "t__close", "arguments": arguments},
            {"type": "function_call_output", "call_id": "c1", "output": "t__close failed"},
        ]
    )

    block = cast("Any", messages[1]["content"])[0]
    assert (block["type"], block["input"]) == ("tool_use", {})


async def test_a_forgotten_server_s_client_in_use_is_closed_when_the_adapter_closes() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    server = MCPServer("slow")

    @server.tool()
    async def wait() -> str:
        """Wait."""
        started.set()
        await release.wait()
        return "done"

    closed: list[str] = []

    @contextlib.asynccontextmanager
    async def opener(_: McpServer) -> AsyncGenerator[Client]:
        try:
            async with Client(server) as client:
                yield client
        finally:
            closed.append("closed")

    mcp = McpAdapterClass(opener)
    calling = asyncio.create_task(mcp.call_tool(McpServer(_URL), "wait", {}))
    await started.wait()

    await mcp.forget(McpServer(_URL))
    assert closed == []  # the call still running on it is left to finish
    release.set()
    await mcp.close()

    assert closed == ["closed"]
    await asyncio.gather(calling, return_exceptions=True)

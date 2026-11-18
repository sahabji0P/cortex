"""
MCP adapter: the tool servers a gem can be connected to, reached over Streamable HTTP.

A server is known here only by its address and the headers sent with every request to
it. Two things are asked of one: which tools it has, and to run a call of one.

A client is kept open for each server and shared by every call to it. The protocol's
newer versions want a tool listed before it is called, and its older ones open with a
handshake, so a client made for each call would pay for both every time. The SDK's
client has to be opened and closed by one task, so each is held by a task of its own
(`_Link`), and calls are made on it from wherever they come. A client that fails is not
used again: the next call opens a new one.

Only servers reached by address are supported. One started as a local command is not:
the routes that add a server have no sign-in, and that would let whoever can reach them
run a command on this machine.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import cast

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ContentBlock

__all__ = ["McpAdapterClass", "McpFailure", "McpResult", "McpServer", "McpTool"]

# How long a server may take to answer the opening of a client, a page of its tool list,
# and one tool call.
_OPEN_SECONDS = 20.0
_LIST_SECONDS = 20.0
_CALL_SECONDS = 60.0
# A tool list that never ends cannot hold a request up for ever.
_LIST_PAGES_MAX = 50

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class McpServer:
    """One server: its address, and the headers sent with every request to it."""

    url: str
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class McpTool:
    """One tool as its server lists it."""

    name: str
    title: str | None
    description: str
    input_schema: dict[str, object]  # JSON schema


@dataclass(frozen=True, slots=True)
class McpResult:
    """What a call came back with, as text, and whether the tool said it failed."""

    text: str
    is_error: bool


class McpFailure(Exception):
    """A server could not be reached, or did not answer as the protocol asks.

    `reason` says what went wrong, in words. A tool that ran and failed is not this:
    that is an `McpResult` with `is_error`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


type _Opener = Callable[[McpServer], AbstractAsyncContextManager[Client]]


class _Link:
    """One client kept open for a server, by a task of its own.

    The SDK's client must be closed by the task that opened it, so that task is this
    one's, and it stays until `close`. `users` counts the calls being made on it: a link
    that is `retired` is closed once there are none.
    """

    def __init__(self, opening: AbstractAsyncContextManager[Client]) -> None:
        self.users = 0
        self.retired = False
        self._closing = asyncio.Event()
        self._client: asyncio.Future[Client] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._hold(opening))

    @property
    def alive(self) -> bool:
        return not self.retired and not self._task.done()

    async def client(self) -> Client:
        """The open client. Raises what opening it raised."""
        # Shielded: one caller giving up does not stop the opening for the others.
        return await asyncio.shield(self._client)

    async def close(self) -> None:
        self.retired = True
        self._closing.set()
        if not self._client.done():
            # Still opening, perhaps on a server that never answers: not waited for.
            self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        if self._client.done() and not self._client.cancelled():
            # Read, so a failure nobody waited for is not reported as never retrieved.
            self._client.exception()

    async def _hold(self, opening: AbstractAsyncContextManager[Client]) -> None:
        try:
            async with opening as client:
                self._client.set_result(client)
                await self._closing.wait()
        except asyncio.CancelledError:
            if not self._client.done():
                # Told as a failure: a cancellation here would pass for the caller's own.
                self._client.set_exception(ConnectionError("the client was closed while opening"))
            raise
        except Exception as exc:
            if not self._client.done():
                self._client.set_exception(exc)
            else:
                # It broke while it was open. The next call opens a new one.
                _log.warning("an MCP client stopped: %s", _reason(exc))


class McpAdapterClass:
    def __init__(self, opener: _Opener | None = None) -> None:
        # Tests hand in an opener that reaches a server in this process.
        self._opener = self._open_http if opener is None else opener
        # The last status of 400 or more each server answered with. The SDK reports
        # such an answer only as an error, and the status is what says why: a 401 is a
        # header that does not let us in.
        self._refused: dict[McpServer, int] = {}
        self._links: dict[McpServer, _Link] = {}
        # Links being closed in the background, kept so the tasks are not lost.
        self._closing: set[asyncio.Task[None]] = set()

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        links, self._links = list(self._links.values()), {}
        await asyncio.gather(*(link.close() for link in links))
        await asyncio.gather(*self._closing, return_exceptions=True)

    async def list_tools(self, server: McpServer) -> list[McpTool]:
        """Every tool the server has now, in the order it lists them.

        Raises `McpFailure` if it cannot be reached or does not answer.
        """
        tools: list[McpTool] = []
        async with self._using(server) as client:
            cursor: str | None = None
            for _ in range(_LIST_PAGES_MAX):
                async with asyncio.timeout(_LIST_SECONDS):
                    # Asked of the server every time: this is how a change is found.
                    page = await client.list_tools(cursor=cursor, cache_mode="refresh")
                tools.extend(
                    McpTool(
                        name=tool.name,
                        title=tool.title or (tool.annotations.title if tool.annotations else None),
                        description=tool.description or "",
                        input_schema=cast("dict[str, object]", tool.input_schema),
                    )
                    for tool in page.tools
                )
                cursor = page.next_cursor
                if cursor is None:
                    break
        return tools

    async def call_tool(
        self, server: McpServer, name: str, arguments: Mapping[str, object]
    ) -> McpResult:
        """Run one call of a tool, and return what it came back with as text.

        Raises `McpFailure` if the server cannot be reached, does not answer in time, or
        refuses the call. A tool that ran and failed is returned, with `is_error`.
        """
        async with self._using(server) as client:
            result = await client.call_tool(
                name, dict(arguments), read_timeout_seconds=_CALL_SECONDS
            )
        return McpResult(text=_text(result), is_error=result.is_error)

    async def forget(self, server: McpServer) -> None:
        """Close the client kept for a server that is no longer used, if there is one."""
        link = self._links.pop(server, None)
        if link is not None:
            self._retire(link)

    @contextlib.asynccontextmanager
    async def _open_http(self, server: McpServer) -> AsyncGenerator[Client]:
        """A client for a server reached over Streamable HTTP, with the server's headers."""

        async def note(response: httpx2.Response) -> None:
            if response.status_code >= 400:
                self._refused[server] = response.status_code

        # The SDK's own timeouts: a long read, because a server may hold a reply open.
        timeout = httpx2.Timeout(30.0, read=300.0)
        async with (
            httpx2.AsyncClient(
                headers=dict(server.headers), timeout=timeout, event_hooks={"response": [note]}
            ) as http,
            Client(streamable_http_client(server.url, http_client=http)) as client,
        ):
            yield client

    @contextlib.asynccontextmanager
    async def _using(self, server: McpServer) -> AsyncGenerator[Client]:
        """The server's client, for one request. Whatever goes wrong is an `McpFailure`."""
        link = self._links.get(server)
        if link is None or not link.alive:
            link = self._links[server] = _Link(self._opener(server))
        link.users += 1
        self._refused.pop(server, None)
        try:
            async with asyncio.timeout(_OPEN_SECONDS):
                client = await link.client()
            yield client
        except Exception as exc:
            # A client that failed is not used again, whatever the failure was: the
            # next call opens a new one. Calls still running on it are left to finish.
            if self._links.get(server) is link:
                del self._links[server]
            link.retired = True
            raise McpFailure(_reason(exc, self._refused.pop(server, None))) from exc
        finally:
            link.users -= 1
            if link.retired:
                self._retire(link)

    def _retire(self, link: _Link) -> None:
        """Close a link once nothing is using it. It is no longer handed out."""
        link.retired = True
        if link.users > 0:
            return
        closing = asyncio.create_task(link.close())
        self._closing.add(closing)
        closing.add_done_callback(self._closing.discard)


def _reason(exc: BaseException, status: int | None = None) -> str:
    """What went wrong, in words short enough to show.

    `status` is the status the server answered with, if it was 400 or more.
    """
    # The SDK runs its transport in a task group, so a failure comes wrapped in one.
    while isinstance(exc, BaseExceptionGroup):
        exc = cast("BaseExceptionGroup[BaseException]", exc).exceptions[0]
    if status in (401, 403):
        return f"the server answered {status}: its headers do not let us in"
    if isinstance(exc, TimeoutError):
        return "the server did not answer in time"
    # What the server said went wrong, when it said so. Otherwise the SDK's own words
    # for an answer it could not read, which say less than the status does.
    if isinstance(exc, MCPError) and (status is None or exc.message != _UNREAD_ANSWER):
        return exc.message
    if status is not None:
        return f"the server answered {status}"
    return str(exc) or type(exc).__name__


# The SDK's words for an answer of 400 or more that carried no error it could read.
_UNREAD_ANSWER = "Server returned an error response"


def _text(result: CallToolResult) -> str:
    """A call's result as text: what a model that only reads text can be sent."""
    parts = [_block_text(block) for block in result.content]
    if not parts and result.structured_content is not None:
        return json.dumps(result.structured_content, ensure_ascii=False)
    return "\n".join(parts)


def _block_text(block: ContentBlock) -> str:
    if block.type == "text":
        return block.text
    if block.type == "resource_link":
        return f"{block.title or block.name} ({block.uri})"
    if block.type == "resource":
        resource = block.resource
        text = getattr(resource, "text", None)
        return text if isinstance(text, str) else f"[a file, not shown: {resource.uri}]"
    # A picture or a sound: nothing here can pass one on to the model yet.
    return f"[{block.type}, not shown]"

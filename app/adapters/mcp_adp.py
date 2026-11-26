"""
MCP adapter: the tool servers a gem can be connected to, reached over Streamable HTTP.

A server is known here only by its address and the headers sent with every request to
it. Two things are asked of one: which tools it has, and to run a call of one.

A server may also serve events (SEP-3415, the extension `io.modelcontextprotocol/events`):
things that happen, which a client listens for. Its kinds of event are read with its
tools, when it says it has the extension. One kind is listened for in any of three ways
the server offers for it: asked for again and again (`events/poll`), held open while
the server sends each one (`events/stream`), or posted by the server to an address of
ours (`events/subscribe`, a webhook), each post signed with a secret we give it.

A server may also say how it shows itself: its icons and its website, read with its
tools. An icon it names by address is fetched here, from the server's own domain only,
and kept as a `data:` address, so the app never reaches a host for one.

A client is kept open for each server and shared by every call to it. The protocol's
newer versions want a tool listed before it is called, and its older ones open with a
handshake, so a client made for each call would pay for both every time. The SDK's
client has to be opened and closed by one task, so each is held by a task of its own
(`_Link`), and calls are made on it from wherever they come. A client that fails is not
used again: the next call opens a new one.

Only servers reached by address are supported. One started as a local command is not:
the routes that add a server have no sign-in, and that would let whoever can reach them
run a command on this machine.

A server may ask for a sign-in instead of a fixed header: it answers 401, and names
where its sign-in is described (the MCP authorization spec). The steps of that sign-in
that talk to the outside are here too: finding the sign-in server, registering this
backend with it as a client, the address the user's browser is sent to, swapping the
code that comes back for a pass, and renewing a pass. Each is one plain request, made
when it is asked for. The SDK's own OAuth provider runs the whole sign-in inside one
request to the server, waiting on the browser as it goes; here a sign-in is split over
two requests to this backend, and a tool call must never wait on a browser, so only its
pieces are used: the metadata models, the discovery addresses, PKCE and its checks.
"""

import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import re
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from urllib.parse import quote, unquote_to_bytes, urlencode, urlsplit

import httpx2
from mcp import Client
from mcp.client import ClientExtension, advertise
from mcp.client.auth import OAuthFlowError, PKCEParameters
from mcp.client.auth.oauth2 import check_registration_usable
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_oauth_metadata_request,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
    issuers_match,
    validate_metadata_issuer,
)
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata, OAuthToken, ProtectedResourceMetadata
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ContentBlock, Icon, Implementation
from mcp_types import Request, RequestParams, Result
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from app.core.times import UtcDatetime

__all__ = [
    "EVENTS_EXTENSION",
    "McpAbout",
    "McpAdapterClass",
    "McpAuthServer",
    "McpAuthorization",
    "McpChallenge",
    "McpEventType",
    "McpFailure",
    "McpIcon",
    "McpListing",
    "McpOccurrence",
    "McpPolled",
    "McpRegistration",
    "McpResult",
    "McpServer",
    "McpTokens",
    "McpTool",
]

# How long a server may take to answer the opening of a client, a page of its tool list,
# and one tool call.
_OPEN_SECONDS = 20.0
_LIST_SECONDS = 20.0
_CALL_SECONDS = 60.0
# How long closing a client waits for what is still running on it.
_CLOSE_SECONDS = 5.0
# A tool list, or an event list, that never ends cannot hold a request up for ever.
_LIST_PAGES_MAX = 50
# How long one request to a sign-in server may take. A renewal is made inside a tool
# call, so it is kept short: a sign-in server that does not answer fails the call soon.
_SIGN_IN_SECONDS = 10.0
# The name this backend registers under, which a sign-in page may show the user.
_CLIENT_NAME = "Cortex"
# A server's icons: how large one may be, how many are kept and looked at, how many sizes
# one may state, and how long fetching them may take, all of them at once.
_ICON_MAX_BYTES = 64 * 1024
_ICONS_KEPT = 3
_ICONS_LOOKED_AT = 8
_ICON_SIZES_MAX = 8
_ICON_SECONDS = 5.0
# How long a server's website address may be.
_WEBSITE_MAX_LENGTH = 2_000

_log = logging.getLogger(__name__)

# The MCP events extension (SEP-3415), as a server names it under its capabilities.
EVENTS_EXTENSION = "io.modelcontextprotocol/events"
# The ways a server may offer to deliver one kind of event.
_DELIVERIES = ("poll", "push", "webhook")


@dataclass(frozen=True, slots=True)
class McpServer:
    """One server: its address, the headers sent with every request to it, and the pass.

    `token` is a pass from a sign-in, sent as `Authorization: Bearer`. A server is also
    the key its kept client is found by, so the key is the address, every header and
    the pass: a renewed pass is another key, and opens a client of its own. Whoever
    changes a pass forgets the server with the old one, which closes the old client.
    Neither the headers nor the pass are shown when a server is printed.
    """

    url: str
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    token: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class McpTool:
    """One tool as its server lists it."""

    name: str
    title: str | None
    description: str
    input_schema: dict[str, object]  # JSON schema


@dataclass(frozen=True, slots=True)
class McpEventType:
    """One kind of event a server can send, as it lists it (`events/list`)."""

    name: str
    description: str
    # How it may be delivered: any of "poll", "push" and "webhook", in the server's order.
    # Only those three are kept.
    delivery: tuple[str, ...]
    # The JSON schema of the arguments a listener gives it.
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class McpOccurrence:
    """One event as a server sent it: something that happened."""

    # The server's id for it, the same each time it is sent: a second one is the same event.
    event_id: str
    name: str
    # When it happened, as the server says. None if it said nothing that can be read.
    at: datetime | None
    # What happened, as the kind's payload schema has it.
    data: dict[str, object]
    # Where the listener has got to after it. Sent with a stream's and a webhook's events
    # only: a poll gives one cursor for its whole batch. None when the server keeps no
    # history to go back to.
    cursor: str | None


@dataclass(frozen=True, slots=True)
class McpPolled:
    """What one `events/poll` came back with."""

    events: list[McpOccurrence]
    # Where to ask from next time. None when the server keeps no history: ask from now.
    cursor: str | None
    # Events were skipped: the server started later than the cursor it was given.
    truncated: bool
    # More are waiting: ask again at once.
    has_more: bool
    # How long the server would like us to wait before asking again. None: it did not say.
    next_poll_ms: int | None


@dataclass(frozen=True, slots=True)
class McpIcon:
    """One of a server's icons, held here: `src` is always a `data:` address."""

    src: str
    mime_type: str
    sizes: tuple[str, ...] = ()
    # Which background it is made for. None: either.
    theme: Literal["light", "dark"] | None = None


@dataclass(frozen=True, slots=True)
class McpAbout:
    """How a server shows itself, as far as it says: its icons, and its website.

    `unreached`: an icon it named could not be fetched just now (its host did not answer,
    or answered with a failure of its own), so `icons` may hold fewer than it has.
    """

    icons: tuple[McpIcon, ...] = ()
    website_url: str | None = None
    unreached: bool = False


@dataclass(frozen=True, slots=True)
class McpListing:
    """What a server answered when it was asked for its tools, and how it shows itself.

    `events` are the kinds of event it serves: none when it does not have the extension.
    """

    tools: list[McpTool]
    about: McpAbout
    events: list[McpEventType] = field(default_factory=list[McpEventType])


@dataclass(frozen=True, slots=True)
class McpResult:
    """What a call came back with, as text, and whether the tool said it failed."""

    text: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class McpChallenge:
    """What a server's 401 said of its sign-in: where it is described, and the scope asked.

    Both come from its `WWW-Authenticate` header, and either may be missing. Neither is
    a secret.
    """

    resource_metadata: str | None
    scope: str | None


class McpFailure(Exception):
    """A server could not be reached, or did not answer as the protocol asks.

    `reason` says what went wrong, in words. A tool that ran and failed is not this:
    that is an `McpResult` with `is_error`.

    `sign_in_needed` says that what was sent no longer lets us in, and a sign-in would:
    the server answered 401, or a sign-in server refused to renew a pass. It is set for
    nothing else. A 403 is not it: that is a server that knows who we are and says no,
    and signing in again would not change it. `challenge` is what a 401 said.

    `code` is the protocol's error code, when the server answered with one: it says
    whether asking again could help (the events extension's own are -32023 to -32027).
    """

    def __init__(
        self,
        reason: str,
        *,
        sign_in_needed: bool = False,
        challenge: McpChallenge | None = None,
        code: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.sign_in_needed = sign_in_needed
        self.challenge = challenge
        self.code = code


class McpAuthServer(BaseModel):
    """A server's sign-in, as its metadata describes it. Nothing here is a secret."""

    # Where the server's protected resource metadata was found, if it has any.
    resource_metadata: str | None = None
    # The resource a pass is asked for (RFC 8707). Named only when the server published
    # protected resource metadata, as the SDK does.
    resource: str | None = None
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    # What is asked for: the scope, and the grant types registered with. A refresh
    # token is asked for wherever the metadata allows it.
    scope: str | None = None
    grant_types: list[str]
    # How this backend asks to prove who it is when it asks for a pass. None: the
    # sign-in server chooses, and says so when it registers us.
    token_auth_method: str | None = None
    # Whether the server says it names itself (`iss`) when it sends the browser back.
    sends_iss: bool = False


class McpRegistration(BaseModel):
    """This backend's registration as a client of a sign-in server. Its secret is kept here."""

    client_id: str
    client_secret: str | None = None
    # When the secret runs out, as seconds since 1970. 0 or None: it does not.
    client_secret_expires_at: int | None = None
    token_endpoint_auth_method: str | None = None
    # The address the browser comes back to, as it was registered. A registration made
    # with another one cannot be used: the sign-in server checks it.
    redirect_uri: str
    issuer: str


class McpTokens(BaseModel):
    """A pass from a sign-in: what lets us in, and what renews it."""

    access_token: str
    refresh_token: str | None = None
    # When the pass runs out. None when the sign-in server did not say: unknown, not never.
    expires_at: UtcDatetime | None = None
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class McpAuthorization:
    """Where the user's browser is sent to sign in, and the PKCE secret that goes with it.

    The secret (`verifier`) never leaves this backend: it is sent only to the token
    endpoint, with the code the browser comes back with.
    """

    url: str = field(repr=False)
    verifier: str = field(repr=False)


# Opens a client for a server, with the extensions it declares and listens with.
type _Opener = Callable[[McpServer, Sequence[ClientExtension]], AbstractAsyncContextManager[Client]]


class _ListEventsParams(RequestParams):
    cursor: str | None = None


class _ListEvents(Request[_ListEventsParams, Literal["events/list"]]):
    method: Literal["events/list"] = "events/list"


class _EventType(BaseModel):
    """One kind of event as `events/list` gives it. Fields not read here are let through."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str = ""
    delivery: list[str] = Field(default_factory=list[str])
    input_schema: dict[str, object] = Field(default_factory=dict, alias="inputSchema")


class _EventsListed(Result):
    model_config = ConfigDict(extra="ignore")

    events: list[_EventType]
    next_cursor: str | None = None


class _EventParams(RequestParams):
    """What says which events are listened for: a kind, its arguments, and where from.

    A cursor of None is left out, which the spec reads as null: from now.
    """

    name: str
    arguments: dict[str, object]
    cursor: str | None = None


class _PollParams(_EventParams):
    max_events: int | None = None


class _Poll(Request[_PollParams, Literal["events/poll"]]):
    method: Literal["events/poll"] = "events/poll"
    # The event's name goes in the `Mcp-Name` header too, as the spec asks.
    name_param = "name"


class _Occurrence(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="ignore")

    event_id: str
    name: str
    timestamp: str | None = None
    data: dict[str, object] = Field(default_factory=dict[str, object])
    cursor: str | None = None


class _Polled(Result):
    model_config = ConfigDict(extra="ignore")

    events: list[_Occurrence] = Field(default_factory=list[_Occurrence])
    cursor: str | None = None
    truncated: bool = False
    has_more: bool = False
    next_poll_ms: int | None = None


@dataclass(frozen=True, slots=True)
class _Refusal:
    """A server's answer of 400 or more: its status, and what a 401 said of its sign-in."""

    status: int
    challenge: McpChallenge | None


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
        # A client closes once what is running on it has ended. A call that a server
        # never answers is not to hold up closing, such as when the app stops.
        _, running = await asyncio.wait({self._task}, timeout=_CLOSE_SECONDS)
        if running:
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
    def __init__(
        self,
        opener: _Opener | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        # Tests hand in an opener that reaches a server in this process, or a transport
        # that every request over HTTP goes through, a sign-in's included.
        self._opener = self._open_http if opener is None else opener
        self._transport = transport
        # The last answer of 400 or more each server gave. The SDK reports such an
        # answer only as an error, and the status is what says why: a 401 is a header,
        # or a pass, that does not let us in.
        self._refused: dict[McpServer, _Refusal] = {}
        self._links: dict[McpServer, _Link] = {}
        # Links being closed in the background, kept so the tasks are not lost.
        self._closing: set[asyncio.Task[None]] = set()
        # Links no longer handed out that a call is still running on.
        self._retired: set[_Link] = set()

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        links, self._links = [*self._links.values(), *self._retired], {}
        self._retired.clear()
        await asyncio.gather(*(link.close() for link in links))
        await asyncio.gather(*self._closing, return_exceptions=True)

    async def list_tools(self, server: McpServer) -> McpListing:
        """Every tool the server has now, in the order it lists them, and how it shows itself.

        How it shows itself is what it said when the client was opened, read from the
        same client as the tools (see `_about`). Its kinds of event are read too, when
        it says it has the events extension. Raises `McpFailure` if it cannot be reached
        or does not answer. An icon that cannot be had never fails it.
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
            info = client.server_info
            events = await _list_events(client)
        return McpListing(tools, await _about(info, server.url, self._transport), events)

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

    async def poll_events(
        self,
        server: McpServer,
        name: str,
        arguments: Mapping[str, object],
        cursor: str | None,
        *,
        max_events: int | None = None,
    ) -> McpPolled:
        """Ask the server once for the events of one kind since `cursor` (`events/poll`).

        A cursor of None starts from now: the server sends no events, and a cursor to
        ask from next time. Raises `McpFailure` if it cannot be reached or refuses.
        """
        params = _PollParams(
            name=name, arguments=dict(arguments), cursor=cursor, max_events=max_events
        )
        async with self._using(server) as client:
            async with asyncio.timeout(_LIST_SECONDS):
                polled = await client.session.send_request(_Poll(params=params), _Polled)
        return McpPolled(
            events=[_occurrence(event) for event in polled.events],
            cursor=polled.cursor,
            truncated=polled.truncated,
            has_more=polled.has_more,
            next_poll_ms=polled.next_poll_ms,
        )

    async def forget(self, server: McpServer) -> None:
        """Close the client kept for a server that is no longer used, if there is one."""
        link = self._links.pop(server, None)
        if link is not None:
            self._retire(link)

    async def discover(self, url: str, challenge: McpChallenge | None) -> McpAuthServer | None:
        """Find how the server at `url` is signed in to, from what its 401 said.

        The server's protected resource metadata names its sign-in server, whose own
        metadata names where the browser goes, where a pass is asked for, and where a
        client registers. A server that published none, the old way, is looked for at
        its own address. None if the server publishes no sign-in at all. Raises
        `McpFailure` if it publishes one this backend cannot do: one that needs a client
        registered by hand, one without PKCE, or one whose metadata does not name the
        server it was found for.
        """
        async with self._sign_in_http() as http:
            prm = await self._protected_resource(http, url, challenge)
            auth_server = str(prm[1].authorization_servers[0]) if prm is not None else None
            # Its metadata has to name the server it was found for (RFC 8414 §3.3).
            expected = auth_server or _origin(url)
            metadata = None
            for address in build_oauth_authorization_server_metadata_discovery_urls(
                auth_server, url
            ):
                answer = await _get(http, address)
                found, metadata = await handle_auth_metadata_response(answer)
                if not found or metadata is not None:
                    break
        if metadata is None:
            return None
        if auth_server is None and issuers_match(str(metadata.issuer), expected):
            expected = str(metadata.issuer)
        try:
            validate_metadata_issuer(metadata, expected)
        except OAuthFlowError as exc:
            raise McpFailure("the sign-in server's metadata names another server") from exc
        if metadata.registration_endpoint is None:
            raise McpFailure(
                "the server's sign-in needs a client registered by hand, which Cortex cannot do"
            )
        if "code" not in metadata.response_types_supported or (
            metadata.code_challenge_methods_supported is not None
            and "S256" not in metadata.code_challenge_methods_supported
        ):
            raise McpFailure("the server's sign-in does not take a code with PKCE")
        # A refresh token, so the pass can be renewed without the browser, wherever the
        # metadata does not say it is not given.
        supported = metadata.grant_types_supported
        grant_types = ["authorization_code"]
        if supported is None or "refresh_token" in supported:
            grant_types.append("refresh_token")
        methods = metadata.token_endpoint_auth_methods_supported
        # No secret is best: there is none to keep. A server that will not have that
        # gets one, and one that says nothing chooses, as the SDK leaves it to.
        token_auth_method: str | None = None
        if methods is not None:
            usable = [
                m for m in ("none", "client_secret_basic", "client_secret_post") if m in methods
            ]
            if not usable:
                raise McpFailure("the server's sign-in wants a kind of client Cortex cannot be")
            token_auth_method = usable[0]
        return McpAuthServer(
            resource_metadata=prm[0] if prm is not None else None,
            resource=_resource(url, prm[1]) if prm is not None else None,
            issuer=str(metadata.issuer),
            authorization_endpoint=str(metadata.authorization_endpoint),
            token_endpoint=str(metadata.token_endpoint),
            registration_endpoint=str(metadata.registration_endpoint),
            scope=get_client_metadata_scopes(
                None if challenge is None else challenge.scope,
                None if prm is None else prm[1],
                metadata,
                grant_types,
            ),
            grant_types=grant_types,
            token_auth_method=token_auth_method,
            sends_iss=bool(metadata.authorization_response_iss_parameter_supported),
        )

    async def register(self, auth: McpAuthServer, redirect_uri: str) -> McpRegistration:
        """Register this backend as a client of a sign-in server (RFC 7591).

        Raises `McpFailure` if the server refuses, or registers a client this backend
        could not use.
        """
        wanted = OAuthClientMetadata(
            redirect_uris=[AnyUrl(redirect_uri)],
            token_endpoint_auth_method=cast("_AuthMethod | None", auth.token_auth_method),
            grant_types=auth.grant_types,
            scope=auth.scope,
            client_name=_CLIENT_NAME,
        )
        async with self._sign_in_http() as http:
            answer = await _send(
                http,
                "POST",
                auth.registration_endpoint,
                json=wanted.model_dump(by_alias=True, mode="json", exclude_none=True),
            )
            try:
                made = await handle_registration_response(answer)
                check_registration_usable(made)
            except OAuthFlowError as exc:
                # The SDK's words carry the server's whole answer, which is not shown.
                raise McpFailure(
                    f"the sign-in server would not register Cortex (it answered "
                    f"{answer.status_code})"
                ) from exc
        return McpRegistration(
            client_id=made.client_id,
            client_secret=made.client_secret,
            client_secret_expires_at=made.client_secret_expires_at,
            token_endpoint_auth_method=made.token_endpoint_auth_method,
            redirect_uri=redirect_uri,
            issuer=auth.issuer,
        )

    def authorization(
        self, auth: McpAuthServer, registration: McpRegistration, state: str
    ) -> McpAuthorization:
        """Where to send the user's browser to sign in, with a new PKCE secret (S256)."""
        pkce = PKCEParameters.generate()
        query = {
            "response_type": "code",
            "client_id": registration.client_id,
            "redirect_uri": registration.redirect_uri,
            "state": state,
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
        }
        if auth.resource is not None:
            query["resource"] = auth.resource
        if auth.scope:
            query["scope"] = auth.scope
            # OpenID Connect gives a refresh token for `offline_access` only with this.
            if "offline_access" in auth.scope.split():
                query["prompt"] = "consent"
        joiner = "&" if urlsplit(auth.authorization_endpoint).query else "?"
        return McpAuthorization(
            url=f"{auth.authorization_endpoint}{joiner}{urlencode(query)}",
            verifier=pkce.code_verifier,
        )

    async def exchange(
        self,
        auth: McpAuthServer,
        registration: McpRegistration,
        *,
        code: str,
        verifier: str,
        iss: str | None,
    ) -> McpTokens:
        """Swap the code the browser came back with for a pass.

        `iss` is the sign-in server's name, as the browser brought it back (RFC 9207).
        It has to be the server's own, and it has to be there if the server says it
        sends it. Raises `McpFailure` if it is not, or if the server refuses.
        """
        if iss is not None and iss != auth.issuer:
            raise McpFailure("the sign-in came back from another server than the one asked")
        if iss is None and auth.sends_iss:
            raise McpFailure("the sign-in came back without the name of its server")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": registration.redirect_uri,
            "client_id": registration.client_id,
            "code_verifier": verifier,
        }
        answer = await self._ask_for_pass(auth, registration, form)
        if answer.status_code not in (200, 201):
            raise McpFailure(f"the sign-in server would not give a pass{_said(answer)}")
        return _tokens(answer, previous=None)

    async def renew(
        self, auth: McpAuthServer, registration: McpRegistration, refresh_token: str
    ) -> McpTokens:
        """A new pass, for the refresh token of the one that runs out.

        Raises `McpFailure`. When the sign-in server refuses the refresh token, the
        failure says `sign_in_needed`: only a sign-in in the browser lets us in again.
        When it cannot be reached or fails on its side, it does not: the pass may still
        be renewed later.
        """
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": registration.client_id,
        }
        answer = await self._ask_for_pass(auth, registration, form)
        if answer.status_code == 200:
            return _tokens(answer, previous=refresh_token)
        refused = answer.status_code in (400, 401)
        raise McpFailure(
            f"the sign-in server would not renew the pass{_said(answer)}",
            sign_in_needed=refused,
        )

    async def _ask_for_pass(
        self, auth: McpAuthServer, registration: McpRegistration, form: dict[str, str]
    ) -> httpx2.Response:
        """One request to the token endpoint, as the client registered (RFC 6749 §2.3.1)."""
        if auth.resource is not None:
            form["resource"] = auth.resource
        headers: dict[str, str] = {}
        method = registration.token_endpoint_auth_method
        secret = registration.client_secret
        if method == "client_secret_basic" and secret:
            pair = f"{quote(registration.client_id, safe='')}:{quote(secret, safe='')}"
            headers["Authorization"] = f"Basic {base64.b64encode(pair.encode()).decode()}"
        elif method == "client_secret_post" and secret:
            form["client_secret"] = secret
        async with self._sign_in_http() as http:
            answer = await _send(http, "POST", auth.token_endpoint, data=form, headers=headers)
            await answer.aread()
            return answer

    async def _protected_resource(
        self, http: httpx2.AsyncClient, url: str, challenge: McpChallenge | None
    ) -> tuple[str, ProtectedResourceMetadata] | None:
        """The server's protected resource metadata (RFC 9728), and where it was found.

        Looked for where its 401 said, then at the well-known addresses. None if it has
        none. Raises `McpFailure` if it names another resource than this server, or if
        it could not be read because a server failed.
        """
        named = None if challenge is None else challenge.resource_metadata
        failed: int | None = None
        for address in build_protected_resource_metadata_discovery_urls(named, url):
            answer = await _get(http, address)
            if answer.status_code >= 500 or answer.status_code == 429:
                failed = answer.status_code
            if (found := await handle_protected_resource_response(answer)) is not None:
                if not check_resource_allowed(
                    requested_resource=resource_url_from_server_url(url),
                    configured_resource=str(found.resource),
                ):
                    raise McpFailure("the server's sign-in is for another address")
                return address, found
        if failed is not None:
            # A server that failed says nothing of whether there is a sign-in.
            raise McpFailure(f"the server's sign-in could not be read: it answered {failed}")
        return None

    @contextlib.asynccontextmanager
    async def _sign_in_http(self) -> AsyncGenerator[httpx2.AsyncClient]:
        """A client for the requests of a sign-in. Redirects are not followed."""
        async with httpx2.AsyncClient(
            transport=self._transport, timeout=_SIGN_IN_SECONDS, follow_redirects=False
        ) as http:
            yield http

    @contextlib.asynccontextmanager
    async def _open_http(
        self, server: McpServer, extensions: Sequence[ClientExtension]
    ) -> AsyncGenerator[Client]:
        """A client for a server reached over Streamable HTTP, with its headers and pass."""

        async def note(response: httpx2.Response) -> None:
            if response.status_code >= 400:
                challenge = None
                if response.status_code == 401:
                    challenge = McpChallenge(
                        resource_metadata=extract_resource_metadata_from_www_auth(response),
                        scope=extract_scope_from_www_auth(response),
                    )
                self._refused[server] = _Refusal(response.status_code, challenge)

        headers = dict(server.headers)
        if server.token is not None:
            headers["Authorization"] = f"Bearer {server.token}"
        # The SDK's own timeouts: a long read, because a server may hold a reply open.
        timeout = httpx2.Timeout(30.0, read=300.0)
        async with (
            httpx2.AsyncClient(
                headers=headers,
                timeout=timeout,
                event_hooks={"response": [note]},
                transport=self._transport,
            ) as http,
            Client(
                streamable_http_client(server.url, http_client=http), extensions=extensions
            ) as client,
        ):
            yield client

    @contextlib.asynccontextmanager
    async def _using(self, server: McpServer) -> AsyncGenerator[Client]:
        """The server's client, for one request. Whatever goes wrong is an `McpFailure`."""
        link = self._links.get(server)
        if link is None or not link.alive:
            link = self._links[server] = _Link(self._opener(server, (_EVENTS_DECLARED,)))
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
            refusal = self._refused.pop(server, None)
            status = None if refusal is None else refusal.status
            raise McpFailure(
                _reason(exc, status),
                sign_in_needed=status == 401,
                challenge=None if refusal is None else refusal.challenge,
                code=_code(exc),
            ) from exc
        finally:
            link.users -= 1
            if link.retired:
                self._retire(link)

    def _retire(self, link: _Link) -> None:
        """Close a link once nothing is using it. It is no longer handed out."""
        link.retired = True
        if link.users > 0:
            self._retired.add(link)
            return
        self._retired.discard(link)
        closing = asyncio.create_task(link.close())
        self._closing.add(closing)
        closing.add_done_callback(self._closing.discard)


# Every client says it understands the events extension, as a client may (SEP-3415).
_EVENTS_DECLARED = advertise(EVENTS_EXTENSION)
# What a server answers for a method it does not have.
_METHOD_NOT_FOUND = -32601


async def _list_events(client: Client) -> list[McpEventType]:
    """The kinds of event a server serves, in its order. None if it has not the extension.

    A kind that names no delivery this backend knows is left out: it could not be
    listened for. A server that says it has the extension and then has no `events/list`
    is taken as having none.
    """
    if EVENTS_EXTENSION not in (client.server_capabilities.extensions or {}):
        return []
    events: list[McpEventType] = []
    cursor: str | None = None
    for _ in range(_LIST_PAGES_MAX):
        try:
            async with asyncio.timeout(_LIST_SECONDS):
                page = await client.session.send_request(
                    _ListEvents(params=_ListEventsParams(cursor=cursor)), _EventsListed
                )
        except MCPError as exc:
            if exc.code == _METHOD_NOT_FOUND:
                return []
            raise
        for kind in page.events:
            delivery = tuple(way for way in kind.delivery if way in _DELIVERIES)
            if kind.name and delivery:
                events.append(
                    McpEventType(kind.name, kind.description, delivery, kind.input_schema)
                )
        cursor = page.next_cursor
        if cursor is None:
            break
    return events


def _occurrence(event: _Occurrence) -> McpOccurrence:
    return McpOccurrence(
        event_id=event.event_id,
        name=event.name,
        at=_when(event.timestamp),
        data=event.data,
        cursor=event.cursor,
    )


def _when(timestamp: str | None) -> datetime | None:
    """An ISO 8601 time with its zone, or None if it is not one."""
    if timestamp is None:
        return None
    try:
        at = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def _code(exc: BaseException) -> int | None:
    """The protocol's error code a failure carries, if the server answered with one."""
    while isinstance(exc, BaseExceptionGroup):
        exc = cast("BaseExceptionGroup[BaseException]", exc).exceptions[0]
    return exc.code if isinstance(exc, MCPError) else None


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

# The ways of proving who we are at a token endpoint that a registration may ask for.
type _AuthMethod = Literal["none", "client_secret_post", "client_secret_basic", "private_key_jwt"]

# An OAuth error code: short, and safe to show. Anything else a server says is not shown.
_ERROR_CODE = re.compile(r"[a-z_]{1,40}")


async def _get(http: httpx2.AsyncClient, address: str) -> httpx2.Response:
    """One read of a sign-in's metadata. Raises `McpFailure` if it cannot be made."""
    request = create_oauth_metadata_request(address)
    try:
        answer = await http.send(request)
    except httpx2.HTTPError as exc:
        raise McpFailure(f"{urlsplit(address).netloc} could not be reached") from exc
    await answer.aread()
    return answer


async def _send(
    http: httpx2.AsyncClient, method: str, address: str, **sent: object
) -> httpx2.Response:
    """One request to a sign-in server. Raises `McpFailure` if it cannot be made."""
    try:
        answer = await http.request(method, address, **cast("dict[str, Any]", sent))
    except httpx2.TimeoutException as exc:
        raise McpFailure("the sign-in server did not answer in time") from exc
    except httpx2.HTTPError as exc:
        raise McpFailure("the sign-in server could not be reached") from exc
    await answer.aread()
    return answer


def _said(answer: httpx2.Response) -> str:
    """What a sign-in server's refusal says, for words: its status, and its error code.

    Nothing else of its answer is shown, as it may hold what was sent.
    """
    code = None
    with contextlib.suppress(ValueError):
        body = answer.json()
        if isinstance(body, dict):
            error = cast("dict[str, object]", body).get("error")
            if isinstance(error, str) and _ERROR_CODE.fullmatch(error):
                code = error
    return f" (it answered {answer.status_code}{f', {code}' if code else ''})"


def _tokens(answer: httpx2.Response, *, previous: str | None) -> McpTokens:
    """The pass in a token endpoint's answer. Raises `McpFailure` if there is none.

    A renewal that names no new refresh token keeps the one it was made with (RFC 6749
    §6). An answer that says nothing of how long the pass lasts leaves it unknown.
    """
    try:
        token = OAuthToken.model_validate_json(answer.content)
    except ValidationError as exc:
        raise McpFailure("the sign-in server's answer held no pass") from exc
    expires_at = None
    if token.expires_in is not None:
        expires_at = datetime.now(UTC) + timedelta(seconds=token.expires_in)
    return McpTokens(
        access_token=token.access_token,
        refresh_token=token.refresh_token or previous,
        expires_at=expires_at,
        scope=token.scope,
    )


def _origin(url: str) -> str:
    """`scheme://host[:port]` of an address: where a server without metadata is signed in."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _resource(url: str, prm: ProtectedResourceMetadata) -> str:
    """The resource a pass is asked for: the server's address, or the one its metadata
    names if that covers it (RFC 8707), as the SDK works it out."""
    resource = resource_url_from_server_url(url)
    named = str(prm.resource)
    if check_resource_allowed(requested_resource=resource, configured_resource=named):
        return named
    return resource


class _Unreached(Exception):
    """An icon's host did not answer, or answered with a failure of its own: try again later."""


async def _about(
    info: Implementation | None, server_url: str, transport: httpx2.AsyncBaseTransport | None
) -> McpAbout:
    """How a server shows itself, from what it said when its client was opened.

    The first `_ICONS_KEPT` of its icons that can be kept, in its order, out of the first
    `_ICONS_LOOKED_AT` it states, all fetched at once.
    """
    if info is None:
        return McpAbout()
    stated = (info.icons or [])[:_ICONS_LOOKED_AT]
    held = await asyncio.gather(
        *(_icon(icon, server_url, transport) for icon in stated), return_exceptions=True
    )
    for one in held:
        if isinstance(one, BaseException) and not isinstance(one, Exception):
            raise one  # a cancellation, not an icon's failure
        if isinstance(one, Exception) and not isinstance(one, _Unreached):
            # Not one `_icon` knows of. An icon never fails a listing: it is left out.
            _log.warning("an icon of %s was left out: %s", server_url, _reason(one))
    return McpAbout(
        icons=tuple(one for one in held if isinstance(one, McpIcon))[:_ICONS_KEPT],
        website_url=_website(info.website_url),
        unreached=any(isinstance(one, _Unreached) for one in held),
    )


async def _icon(
    icon: Icon, server_url: str, transport: httpx2.AsyncBaseTransport | None
) -> McpIcon | None:
    """An icon a server stated, held as a `data:` address. None if it cannot be kept.

    The MCP spec's rules for an icon's consumer: a `data:` address, or an `https:` one on
    the server's own domain (see `_same_site`), fetched with no credentials and no
    redirect. Its kind is told by its bytes, and the kind it was given as (the address's
    own, the answer's `Content-Type`, or the icon's `mimeType` when those say nothing)
    must not say otherwise. Raises `_Unreached` when its host did not answer.
    """
    if icon.src[:5].lower() == "data:":
        try:
            given, data = _data(icon.src)
        except ValueError:
            return None
    elif _same_site(icon.src, server_url):
        try:
            async with asyncio.timeout(_ICON_SECONDS):
                given, data = await _fetch(icon.src, transport)
        except (httpx2.TransportError, TimeoutError) as exc:
            raise _Unreached from exc
        except (httpx2.HTTPError, httpx2.InvalidURL, httpx2.StreamError, ValueError):
            # An address the client will not take, or an answer it cannot read.
            return None
    else:
        return None
    kind = _kind(data)
    if given in _SAYS_NOTHING:
        given = _mime(icon.mime_type or "")
    if kind is None or given not in (kind, *_SAYS_NOTHING):
        return None
    return McpIcon(
        src=f"data:{kind};base64,{base64.b64encode(data).decode()}",
        mime_type=kind,
        sizes=tuple(size for size in (icon.sizes or ()) if _SIZE.fullmatch(size))[:_ICON_SIZES_MAX],
        theme=icon.theme,
    )


# What says nothing of a picture's kind.
_SAYS_NOTHING = ("", "application/octet-stream", "binary/octet-stream")
# A size as the spec gives it: "48x48", or "any" for a picture that scales.
_SIZE = re.compile(r"any|[1-9][0-9]{0,4}x[1-9][0-9]{0,4}")
# What in an SVG would run, or draw a page: such an SVG is not kept.
_SVG_ACTIVE = re.compile(rb"<script|<foreignobject|javascript:|\bon[a-z]+\s*=", re.IGNORECASE)


def _mime(value: str) -> str:
    """A MIME type as compared here: without its parameters, in lower case, and with
    `image/jpg`, which the spec names as JPEG too, as `image/jpeg`."""
    kind = value.split(";", 1)[0].strip().lower()
    return "image/jpeg" if kind == "image/jpg" else kind


def _kind(data: bytes) -> str | None:
    """The kind of picture these bytes are, by their first bytes. None if not one kept.

    PNG and JPEG, which the spec says a client must take, and WebP and SVG, which it
    says it should. An SVG that holds a script, an event handler or a page is not kept.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    start = data.removeprefix(b"\xef\xbb\xbf").lstrip()
    if start.startswith(b"<") and re.search(rb"<svg[\s>]", start[:2048], re.IGNORECASE):
        return None if _SVG_ACTIVE.search(data) else "image/svg+xml"
    return None


def _same_site(src: str, server_url: str) -> bool:
    """Whether an icon's address may be fetched: `https:`, with no user or password, on
    the server's own host, a domain above it (`example.com` for `mcp.example.com`), or one
    below it. A domain above a host is its owner's, so no list of public suffixes is
    needed. An address by IP is the server's own only when it is the same."""
    try:
        icon, server = urlsplit(src), urlsplit(server_url)
        icon_host, server_host = icon.hostname, server.hostname
    except ValueError:
        return False
    if icon.scheme.lower() != "https" or not icon_host or not server_host:
        return False
    if icon.username is not None or icon.password is not None:
        return False
    if icon_host == server_host:
        return True
    if _is_ip(icon_host) or _is_ip(server_host):
        return False
    return server_host.endswith(f".{icon_host}") or icon_host.endswith(f".{server_host}")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _data(src: str) -> tuple[str, bytes]:
    """The kind a `data:` address gives and its bytes, in base64 or percent-encoded.

    Raises `ValueError` if it is not one, or is too long to be an icon kept here.
    """
    head, comma, payload = src[5:].partition(",")
    if not comma or len(payload) > _ICON_MAX_BYTES * 3:
        raise ValueError("not a data address an icon is kept from")
    kind, *params = head.split(";")
    if any(param.strip().lower() == "base64" for param in params):
        data = base64.b64decode("".join(payload.split()), validate=True)
    else:
        data = unquote_to_bytes(payload)
    if len(data) > _ICON_MAX_BYTES:
        raise ValueError("too large")
    return _mime(kind), data


async def _fetch(src: str, transport: httpx2.AsyncBaseTransport | None) -> tuple[str, bytes]:
    """The kind an icon's address answers with, and its bytes.

    A client of its own for each icon, so no cookie one answer sets goes with another;
    nothing of the server's is sent. Raises `_Unreached` for an answer of 429 or 500 and
    more, `ValueError` for any other but a 200, or one longer than `_ICON_MAX_BYTES`: no
    more than that is read.
    """
    async with (
        httpx2.AsyncClient(
            transport=transport, timeout=_ICON_SECONDS, follow_redirects=False
        ) as http,
        http.stream("GET", src) as answer,
    ):
        if answer.status_code == 429 or answer.status_code >= 500:
            raise _Unreached(f"it answered {answer.status_code}")
        if answer.status_code != 200:
            raise ValueError(f"it answered {answer.status_code}")
        data = bytearray()
        async for chunk in answer.aiter_bytes():
            data.extend(chunk)
            if len(data) > _ICON_MAX_BYTES:
                raise ValueError("too large")
    return _mime(answer.headers.get("content-type", "")), bytes(data)


def _website(url: str | None) -> str | None:
    """A server's website, if it is an http or https address of a usable length."""
    if url is None or len(url) > _WEBSITE_MAX_LENGTH:
        return None
    try:
        parts = urlsplit(url)
        usable = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return None
    return url if usable else None


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

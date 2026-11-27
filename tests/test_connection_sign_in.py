"""Connections that sign in through the user's browser (OAuth), and keep their pass good.

The tool server is a real MCP server run in this process and reached over HTTP through
the real adapter and the SDK's client, with a transport that hands each request to it.
Next to it is a fake sign-in server: its metadata, registration, the page the browser
signs in at, and the token endpoint. Nothing here reaches a real service.
"""

import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
import secrets
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon

from app.adapters.mcp_adp import McpAdapterClass, McpFailure, McpServer
from app.config import settings
from app.core.errors import InvalidError, UpstreamError
from app.entities.call_ent import CallEntityClass
from app.entities.connection_ent import Connection, ConnectionEntityClass, SignInOutcome
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.tools import Tool
from app.main import app
from app.routers.connections.models import ConnectionsModels
from app.routers.connections.service import (
    ConnectionsServiceV1Class,
    get_connections_service_v1,
)
from app.routers.link.service import LinkServiceV1Class
from tests.test_chat import _chat, _close, _DocDB, _docdb, _open
from tests.test_connections import _PNG_DATA, _server

_MCP = "https://signin.example.com/mcp"
_PRM = "https://signin.example.com/.well-known/oauth-protected-resource/mcp"
_AUTH = "https://auth.example.com"
_BARE = "https://bare.example.com/mcp"
_PLAIN = "https://plain.example.com/mcp"
_CALLBACK = "http://127.0.0.1:8000/v1/connections/sign-in/callback"
# Servers that list their tools to anyone and ask for a sign-in only when `close_issue`
# is called, as Gmail's does: one signed in to through `_AUTH`, one through a sign-in
# server that takes only clients registered by hand, and one that publishes nothing.
_OPEN = "https://open.example.com/mcp"
_OPEN_PRM = "https://open.example.com/.well-known/oauth-protected-resource/close_issue"
_HAND = "https://hand.example.com/mcp"
_HAND_PRM = "https://hand.example.com/.well-known/oauth-protected-resource/close_issue"
_HAND_AUTH = "https://hand-auth.example.com"
_SILENT = "https://silent.example.com/mcp"
# A server that asks for a sign-in at once, through a sign-in server that takes only
# clients registered by hand.
_LOCKED = "https://locked.example.com/mcp"
_LOCKED_PRM = "https://locked.example.com/.well-known/oauth-protected-resource/mcp"


class _Transport(httpx2.AsyncBaseTransport):
    def __init__(self, world: "_SignInWorld") -> None:
        self.world = world

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self.world.handle(request)


class _SignInWorld:
    """A tool server that takes a pass, its sign-in server, and a server that takes none.

    It records what it gave out (passes, refresh tokens, client ids and secrets), so a
    test can look for them where they must not be.
    """

    def __init__(self, *, auth_methods: list[str] | None = None) -> None:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        self.tools = _server()
        self.plain = _server(extra=True, icons=[Icon(src=_PNG_DATA)])
        self._tools = httpx2.ASGITransport(
            app=self.tools.streamable_http_app(
                json_response=True, stateless_http=True, transport_security=security
            )
        )
        self._plain = httpx2.ASGITransport(
            app=self.plain.streamable_http_app(
                json_response=True, stateless_http=True, transport_security=security
            )
        )
        self.transport = _Transport(self)
        self.auth_methods = ["none"] if auth_methods is None else auth_methods
        # How long a pass lasts, in seconds. None: the token endpoint does not say.
        self.lifetime: int | None = 3600
        self.gives_refresh = True
        # When set, the tool server answers a good pass with 403.
        self.forbids = False
        # When set, a renewal is answered with this status, as by a server in trouble.
        self.renewal_fails: int | None = None
        # When set, a renewal waits for it.
        self.renewing: asyncio.Event | None = None
        self.renewal_asked = asyncio.Event()
        self.passes: set[str] = set()
        self.refreshes: set[str] = set()
        self.clients: dict[str, dict[str, Any]] = {}
        self.secrets: list[str] = []
        self.given: list[str] = []  # every pass and refresh token given out
        self.codes: dict[str, dict[str, str]] = {}
        self.renewed = 0
        self.bearers: list[str] = []  # the pass each request to the tool server carried

    # ---- what the browser does

    def browse(self, address: str) -> dict[str, str]:
        """The user signs in at `address`. What the browser is sent back with."""
        assert address.startswith(f"{_AUTH}/authorize?")
        asked = dict(parse_qsl(urlsplit(address).query))
        client = self.clients[asked["client_id"]]
        assert asked["redirect_uri"] in client["redirect_uris"]
        assert asked["response_type"] == "code"
        assert asked["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(8)
        self.codes[code] = asked
        return {"code": code, "state": asked["state"], "iss": _AUTH}

    # ---- the servers

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        where = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        if where == _MCP:
            return await self._tool_server(request)
        if where == _PLAIN:
            return await self._plain.handle_async_request(request)
        if where == _BARE:
            return httpx2.Response(401)
        if where in (_OPEN, _HAND, _SILENT):
            return await self._open_server(request, where)
        if where == _LOCKED:
            challenge = f'Bearer resource_metadata="{_LOCKED_PRM}"'
            return httpx2.Response(401, headers={"WWW-Authenticate": challenge})
        if where == _LOCKED_PRM:
            return _json({"resource": _LOCKED, "authorization_servers": [_HAND_AUTH]})
        if where in (_OPEN_PRM, _HAND_PRM):
            # Gmail's names its sign-in server with a trailing slash, which the sign-in
            # server's own metadata does not have: the same server.
            mcp, auth = (_OPEN, _AUTH) if where == _OPEN_PRM else (_HAND, f"{_HAND_AUTH}/")
            return _json({"resource": mcp, "authorization_servers": [auth]})
        if where == f"{_HAND_AUTH}/.well-known/oauth-authorization-server":
            return _json(
                {
                    "issuer": _HAND_AUTH,
                    "authorization_endpoint": f"{_HAND_AUTH}/authorize",
                    "token_endpoint": f"{_HAND_AUTH}/token",
                    "code_challenge_methods_supported": ["S256"],
                }
            )
        if where == _PRM:
            return _json(
                {"resource": _MCP, "authorization_servers": [_AUTH], "scopes_supported": ["tools"]}
            )
        if where == f"{_AUTH}/.well-known/oauth-authorization-server":
            return _json(
                {
                    "issuer": _AUTH,
                    "authorization_endpoint": f"{_AUTH}/authorize",
                    "token_endpoint": f"{_AUTH}/token",
                    "registration_endpoint": f"{_AUTH}/register",
                    "scopes_supported": ["tools", "offline_access"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": self.auth_methods,
                    "authorization_response_iss_parameter_supported": True,
                }
            )
        if where == f"{_AUTH}/register":
            return self._register(json.loads(await request.aread()))
        if where == f"{_AUTH}/token":
            return await self._token(request)
        return httpx2.Response(404)

    async def _tool_server(self, request: httpx2.Request) -> httpx2.Response:
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.bearers.append(bearer)
        if bearer not in self.passes:
            challenge = f'Bearer resource_metadata="{_PRM}", scope="tools"'
            return httpx2.Response(401, headers={"WWW-Authenticate": challenge})
        if self.forbids:
            return httpx2.Response(403)
        return await self._tools.handle_async_request(request)

    async def _open_server(self, request: httpx2.Request, where: str) -> httpx2.Response:
        """Every request let in, but a call of `close_issue` without a pass."""
        body = json.loads(await request.aread() or b"{}")
        called = body.get("method") == "tools/call" and body["params"]["name"] == "close_issue"
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if called and bearer not in self.passes:
            prm = {_OPEN: _OPEN_PRM, _HAND: _HAND_PRM}.get(where)
            # Gmail's names where its sign-in is, and no scope.
            headers = (
                {} if prm is None else {"WWW-Authenticate": f'Bearer resource_metadata="{prm}"'}
            )
            return httpx2.Response(401, headers=headers)
        return await self._tools.handle_async_request(request)

    def _register(self, asked: dict[str, Any]) -> httpx2.Response:
        client_id = f"client-{secrets.token_hex(6)}"
        made = {**asked, "client_id": client_id}
        if asked.get("token_endpoint_auth_method") in ("client_secret_basic", "client_secret_post"):
            made["client_secret"] = f"secret-{secrets.token_hex(6)}"
            self.secrets.append(made["client_secret"])
        self.clients[client_id] = made
        return _json(made, 201)

    def _client(self, request: httpx2.Request, form: dict[str, str]) -> dict[str, Any] | None:
        """The client a token request is from, if it proved who it is as it registered."""
        client = self.clients.get(form.get("client_id", ""))
        if client is None:
            return None
        method = client.get("token_endpoint_auth_method")
        if method == "client_secret_basic":
            pair = f"{client['client_id']}:{client['client_secret']}".encode()
            if request.headers.get("authorization") != f"Basic {base64.b64encode(pair).decode()}":
                return None
        return client

    async def _token(self, request: httpx2.Request) -> httpx2.Response:
        form = dict(parse_qsl((await request.aread()).decode()))
        client = self._client(request, form)
        if client is None:
            return _json({"error": "invalid_client"}, 401)
        if form["grant_type"] == "authorization_code":
            asked = self.codes.pop(form["code"], None)
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(form["code_verifier"].encode()).digest()
            ).rstrip(b"=")
            if (
                asked is None
                or asked["client_id"] != client["client_id"]
                or asked["redirect_uri"] != form["redirect_uri"]
                or asked["code_challenge"] != challenge.decode()
                or form.get("resource") not in (_MCP, _OPEN)
            ):
                return _json({"error": "invalid_grant"}, 400)
            return self._pass()
        if form["grant_type"] == "refresh_token":
            self.renewal_asked.set()
            if self.renewing is not None:
                await self.renewing.wait()
            if self.renewal_fails is not None:
                return httpx2.Response(self.renewal_fails)
            if form["refresh_token"] not in self.refreshes:
                return _json({"error": "invalid_grant"}, 400)
            self.renewed += 1
            # A renewal that names no new refresh token: the old one stays good.
            return self._pass(refresh=False)
        return _json({"error": "unsupported_grant_type"}, 400)

    def _pass(self, *, refresh: bool = True) -> httpx2.Response:
        access = f"pass-{secrets.token_hex(8)}"
        self.passes.add(access)
        self.given.append(access)
        answer: dict[str, Any] = {"access_token": access, "token_type": "bearer"}
        if self.lifetime is not None:
            answer["expires_in"] = self.lifetime
        if refresh and self.gives_refresh:
            answer["refresh_token"] = f"refresh-{secrets.token_hex(8)}"
            self.refreshes.add(answer["refresh_token"])
            self.given.append(answer["refresh_token"])
        return _json(answer)

    def revoke(self) -> None:
        """Every pass given out stops letting us in."""
        self.passes.clear()


def _json(body: dict[str, Any], status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json=body)


class _World(SimpleNamespace):
    docdb: _DocDB
    servers: _SignInWorld
    mcp: McpAdapterClass
    connections: ConnectionEntityClass
    gem: GemConfig
    told: list[str]

    async def sign_in(self, connection: Connection) -> SignInOutcome:
        address = await self.connections.begin_sign_in(str(connection.id))
        back = self.servers.browse(address)
        return await self.connections.finish_sign_in(error=None, **back)

    async def tool(self, name: str) -> Tool:
        tools = await self.connections.tools_for(self.gem.id)
        return next(tool for tool in tools if tool.name == name)

    async def get(self, connection: Connection) -> Connection:
        return await self.connections.get(str(connection.id))

    def running_out(self, connection: Connection) -> None:
        """The connection's pass runs out within the minute a pass is renewed before."""
        soon = datetime.now(UTC) + timedelta(seconds=30)
        self.stored(connection)["oauth_tokens"]["expires_at"] = soon

    def stored(self, connection: Connection) -> dict[str, Any]:
        return next(d for d in self.docdb.collections["Connections"] if d["_id"] == connection.id)


async def _world(servers: _SignInWorld) -> AsyncGenerator[_World]:
    docdb, adapter = _docdb()
    mcp = McpAdapterClass(transport=servers.transport)
    gems = GemEntityClass(adapter)
    connections = ConnectionEntityClass(docdb=adapter, mcp=mcp, gems=gems)
    told: list[str] = []

    async def listener(connection_id: str) -> None:
        told.append(connection_id)

    connections.listen(listener)
    # The servers run in a task of their own: a fixture is set up and taken down in two
    # tasks, and what runs them has to be left by the task that entered it.
    up, down = asyncio.Event(), asyncio.Event()

    async def serve() -> None:
        async with servers.tools.session_manager.run(), servers.plain.session_manager.run():
            up.set()
            await down.wait()

    serving = asyncio.create_task(serve())
    await up.wait()
    yield _World(
        docdb=docdb,
        servers=servers,
        mcp=mcp,
        connections=connections,
        gem=await gems.create(name="Gem One", model="6 Luna"),
        told=told,
    )
    await mcp.close()
    down.set()
    await serving


@pytest.fixture
async def world() -> AsyncGenerator[_World]:
    async for made in _world(_SignInWorld()):
        yield made


async def _run(tool: Tool, **arguments: object) -> str:
    return await tool.run(json.dumps(arguments))


async def _told(world: _World) -> list[str]:
    # Listeners are told on their own: let them run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return world.told


_CLOSE = "tracker__close_issue"
_NEEDS = "This tool needs the user to sign in to Tracker again."


# ---- adding one


async def test_a_server_that_asks_for_a_sign_in_is_kept_needing_one(world: _World) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP)

    assert made.tools == [] and made.oauth is not None
    assert (made.oauth.issuer, made.oauth.resource) == (_AUTH, _MCP)
    # A refresh token is asked for, since the metadata allows it.
    assert made.oauth.grant_types == ["authorization_code", "refresh_token"]
    assert made.oauth.scope == "tools offline_access"
    assert made.signs_in and not made.signed_in()
    sent = ConnectionsServiceV1Class(world.connections)._connection_out(made)  # pyright: ignore[reportPrivateUsage]
    assert sent.sign_in == ConnectionsModels.V1.Output.SignIn(
        state="sign_in_needed", expires_at=None, renews=False
    )
    assert await world.connections.list_all() == [made]


async def test_a_401_with_nothing_published_is_still_refused(world: _World) -> None:
    with pytest.raises(UpstreamError) as refused:
        await world.connections.create(name="Bare", url=_BARE)

    assert refused.value.status_code == 502
    assert refused.value.message == (
        f"The server at {_BARE} could not be asked for its tools: "
        "the server answered 401: its headers do not let us in"
    )
    assert await world.connections.list_all() == []


async def test_a_401_to_the_user_s_own_authorization_header_is_still_refused(
    world: _World,
) -> None:
    with pytest.raises(UpstreamError, match="answered 401"):
        await world.connections.create(
            name="Tracker", url=_MCP, headers={"authorization": "Bearer wrong"}
        )

    assert await world.connections.list_all() == []
    assert world.servers.clients == {}


# ---- signing in


async def test_a_sign_in_keeps_the_pass_lists_the_tools_and_tells_the_app(
    world: _World,
) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP, gems="all")

    address = await world.connections.begin_sign_in(str(made.id))
    asked = dict(parse_qsl(urlsplit(address).query))
    assert (asked["redirect_uri"], asked["resource"], asked["scope"]) == (
        _CALLBACK,
        _MCP,
        "tools offline_access",
    )
    assert asked["prompt"] == "consent"
    back = world.servers.browse(address)
    outcome = await world.connections.finish_sign_in(error=None, **back)

    assert outcome == SignInOutcome(True, "Signed in to Tracker. You can go back to Cortex.")
    after = await world.get(made)
    assert [tool.name for tool in after.tools] == ["find_issues", "close_issue", "files.read"]
    assert after.signed_in() and after.renews
    assert after.oauth_tokens is not None and after.oauth_tokens.expires_at is not None
    left = after.oauth_tokens.expires_at - datetime.now(UTC)
    assert timedelta(minutes=59) < left <= timedelta(hours=1)
    assert await _told(world) == [str(made.id)]
    # The gem's tool runs with the pass.
    assert await _run(await world.tool(_CLOSE), number=4) == "Closed #4."


async def test_a_sign_in_that_is_not_known_used_twice_or_late_is_refused(world: _World) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP)
    refused = SignInOutcome(
        False,
        "This sign-in is not known here, or it took longer than 10 minutes. "
        "Start it again from Cortex.",
    )

    back = world.servers.browse(await world.connections.begin_sign_in(str(made.id)))
    for state in ("not-a-state", None):
        assert (
            await world.connections.finish_sign_in(
                state=state, code=back["code"], iss=_AUTH, error=None
            )
            == refused
        )
    assert (await world.connections.finish_sign_in(error=None, **back)).signed_in
    # The same answer brought again: the state was used.
    assert await world.connections.finish_sign_in(error=None, **back) == refused

    late = world.servers.browse(await world.connections.begin_sign_in(str(made.id)))
    pending = world.connections._pending  # pyright: ignore[reportPrivateUsage]
    pending[late["state"]] = dataclasses.replace(pending[late["state"]], until=time.monotonic() - 1)
    assert await world.connections.finish_sign_in(error=None, **late) == refused


async def test_a_sign_in_the_server_turned_down_says_so_and_is_used_up(world: _World) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP)
    back = world.servers.browse(await world.connections.begin_sign_in(str(made.id)))

    outcome = await world.connections.finish_sign_in(
        state=back["state"], code=None, iss=None, error="access_denied"
    )

    assert outcome == SignInOutcome(
        False, "The sign-in was not finished. The server said: access_denied"
    )
    again = await world.connections.finish_sign_in(error=None, **back)
    assert not again.signed_in
    assert not (await world.get(made)).signed_in()
    assert await _told(world) == []


async def test_a_sign_in_from_another_server_is_refused(world: _World) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP)
    back = world.servers.browse(await world.connections.begin_sign_in(str(made.id)))

    outcome = await world.connections.finish_sign_in(
        **{**back, "iss": "https://evil.example.com"}, error=None
    )

    assert outcome == SignInOutcome(
        False,
        "Signing in to Tracker failed: the sign-in came back from another server than the "
        "one asked.",
    )
    assert not (await world.get(made)).signed_in()


async def test_the_page_the_browser_is_shown_escapes_what_it_was_sent(world: _World) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP)
    service = ConnectionsServiceV1Class(world.connections)
    back = world.servers.browse(await world.connections.begin_sign_in(str(made.id)))

    page = await service.finish_sign_in(
        state=back["state"], code=None, iss=None, error="<script>alert(1)</script>"
    )

    body = bytes(page.body).decode()
    assert page.status_code == 400
    assert "<script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert page.headers["cache-control"] == "no-store"


def test_the_callback_always_answers_the_browser_with_a_page() -> None:
    connections = ConnectionEntityClass(
        docdb=cast("Any", None), mcp=cast("Any", None), gems=cast("Any", None)
    )
    app.dependency_overrides[get_connections_service_v1] = lambda: ConnectionsServiceV1Class(
        connections
    )
    client = TestClient(app)

    for query in ("", "?state=<b>x</b>&code=<i>y</i>", "?error=access_denied"):
        answer = client.get(f"/v1/connections/sign-in/callback{query}")
        assert answer.status_code == 400
        assert answer.headers["content-type"].startswith("text/html")
        assert "<b>" not in answer.text and "<i>" not in answer.text
        assert "not known here" in answer.text
    app.dependency_overrides.clear()


async def test_a_pass_with_no_lifetime_reads_as_unknown(world: _World) -> None:
    world.servers.lifetime = None
    world.servers.gives_refresh = False
    made = await world.connections.create(name="Tracker", url=_MCP)

    await world.sign_in(made)

    after = await world.get(made)
    sent = ConnectionsServiceV1Class(world.connections)._connection_out(after)  # pyright: ignore[reportPrivateUsage]
    # Unknown, not never: it is signed in until the server says otherwise.
    assert sent.sign_in == ConnectionsModels.V1.Output.SignIn(
        state="signed_in", expires_at=None, renews=False
    )


async def test_a_sign_in_cannot_be_begun_where_there_is_none(world: _World) -> None:
    plain = await world.connections.create(name="Plain", url=_PLAIN)
    with pytest.raises(InvalidError, match="lets Cortex in without a sign-in"):
        await world.connections.begin_sign_in(str(plain.id))

    keyed = await world.connections.update(
        str(plain.id), {"headers": {"Authorization": "Bearer key"}}
    )
    with pytest.raises(InvalidError, match="sends its own Authorization header"):
        await world.connections.begin_sign_in(str(keyed.id))


# ---- keeping the pass good


async def test_a_pass_about_to_run_out_is_renewed_without_the_user(world: _World) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP, gems="all")
    await world.sign_in(made)
    world.running_out(made)
    before = cast("Any", (await world.get(made)).oauth_tokens).access_token
    close = await world.tool(_CLOSE)

    assert await _run(close, number=4) == "Closed #4."

    after = (await world.get(made)).oauth_tokens
    assert world.servers.renewed == 1
    assert after is not None and after.access_token != before
    # The refresh token it was renewed with is kept: the answer named no new one.
    assert after.refresh_token in world.servers.refreshes
    # Renewing is not a change the app needs to hear of.
    assert await _told(world) == [str(made.id)]


async def test_only_one_renewal_runs_at_a_time(world: _World) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP, gems="all")
    await world.sign_in(made)
    world.running_out(made)
    close = await world.tool(_CLOSE)
    world.servers.renewing = asyncio.Event()

    calls = [asyncio.create_task(_run(close, number=n)) for n in range(3)]
    await world.servers.renewal_asked.wait()
    await asyncio.sleep(0.05)
    world.servers.renewing.set()
    results = await asyncio.gather(*calls)

    assert results == ["Closed #0.", "Closed #1.", "Closed #2."]
    assert world.servers.renewed == 1


async def test_a_pass_the_server_refuses_is_renewed_and_the_call_made_again(
    world: _World,
) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP, gems="all")
    await world.sign_in(made)
    close = await world.tool(_CLOSE)
    world.servers.revoke()

    assert await _run(close, number=4) == "Closed #4."
    assert world.servers.renewed == 1
    assert (await world.get(made)).signed_in()


@pytest.mark.parametrize("why", ["no refresh token", "refresh token refused"])
async def test_a_refused_call_marks_the_connection_tells_the_app_and_the_gem_fast(
    world: _World, why: str
) -> None:
    world.servers.gives_refresh = why != "no refresh token"
    made = await world.connections.create(name="Tracker", url=_MCP, gems="all")
    await world.sign_in(made)
    close = await world.tool(_CLOSE)
    world.servers.revoke()
    world.servers.refreshes.clear()

    started = time.monotonic()
    async with asyncio.timeout(3):
        said = await _run(close, number=4)

    assert said == _NEEDS
    assert time.monotonic() - started < 3
    after = await world.get(made)
    assert after.oauth_tokens is None and not after.signed_in()
    sent = ConnectionsServiceV1Class(world.connections)._connection_out(after)  # pyright: ignore[reportPrivateUsage]
    assert sent.sign_in is not None and sent.sign_in.state == "sign_in_needed"
    # Told once at the sign-in, and once now.
    assert await _told(world) == [str(made.id), str(made.id)]
    # The gem keeps being offered the tool, so a call's tool list does not move. A call
    # of it asks the server with no pass, as a server may let anyone use some tools,
    # and its 401 is answered at once.
    assert await world.tool(_CLOSE)
    async with asyncio.timeout(3):
        assert await _run(close, number=4) == _NEEDS


async def test_a_pass_past_its_time_that_cannot_be_renewed_needs_a_sign_in(
    world: _World,
) -> None:
    world.servers.gives_refresh = False
    made = await world.connections.create(name="Tracker", url=_MCP, gems="all")
    await world.sign_in(made)
    world.stored(made)["oauth_tokens"]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)

    # Worked out when it is read: no timer.
    assert not (await world.get(made)).signed_in()
    # Dropped, not sent: the server is asked with no pass.
    assert await _run(await world.tool(_CLOSE), number=4) == _NEEDS
    assert (await world.get(made)).oauth_tokens is None
    assert world.servers.bearers[-1] == ""
    assert await _told(world) == [str(made.id), str(made.id)]


async def test_a_sign_in_server_in_trouble_does_not_sign_the_user_out(world: _World) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP, gems="all")
    await world.sign_in(made)
    kept = (await world.get(made)).oauth_tokens
    world.servers.revoke()
    world.servers.renewal_fails = 503

    with pytest.raises(McpFailure) as failure:
        await _run(await world.tool(_CLOSE), number=4)

    assert not failure.value.sign_in_needed
    assert failure.value.reason == "the sign-in server would not renew the pass (it answered 503)"
    after = await world.get(made)
    assert after.oauth_tokens == kept and after.signed_in()
    assert await _told(world) == [str(made.id)]


async def test_two_connections_to_one_address_sign_in_on_their_own(world: _World) -> None:
    one = await world.connections.create(name="Tracker", url=_MCP)
    two = await world.connections.create(name="Tracker Too", url=_MCP)

    await world.sign_in(one)
    assert (await world.get(one)).signed_in()
    assert not (await world.get(two)).signed_in()

    await world.sign_in(two)
    first, second = await world.get(one), await world.get(two)
    assert first.oauth_client is not None and second.oauth_client is not None
    assert first.oauth_client.client_id != second.oauth_client.client_id
    assert first.oauth_tokens is not None and second.oauth_tokens is not None
    assert first.oauth_tokens.access_token != second.oauth_tokens.access_token


# ---- a server that asks for a sign-in only when a tool is called


async def test_a_call_s_401_finds_the_sign_in_and_the_open_tools_keep_working(
    world: _World,
) -> None:
    made = await world.connections.create(name="Open", url=_OPEN, gems="all")
    assert made.tools and made.oauth is None
    find, close = await world.tool("open__find_issues"), await world.tool("open__close_issue")
    assert await _run(find, text="login") == "2 issues mention login: #4 and #9."

    said = await _run(close, number=4)

    assert said == "This tool needs the user to sign in to Open."
    found = await world.get(made)
    assert found.oauth is not None and (found.oauth.issuer, found.oauth.resource) == (
        _AUTH,
        _OPEN,
    )
    assert found.oauth.resource_metadata == _OPEN_PRM
    assert found.tools == made.tools and found.signs_in and not found.signed_in()
    sent = ConnectionsServiceV1Class(world.connections)._connection_out(found)  # pyright: ignore[reportPrivateUsage]
    assert sent.sign_in is not None and sent.sign_in.state == "sign_in_needed"
    assert await _told(world) == [str(made.id)]
    # Needing a sign-in does not close what the server lets anyone use.
    assert await _run(find, text="login") == "2 issues mention login: #4 and #9."
    # A second refused call finds nothing new, and tells no one again.
    assert await _run(close, number=4) == said
    assert await _told(world) == [str(made.id)]

    # It lists its tools to anyone, so the sign-in begins from where the call was sent,
    # asking for the scope that was found.
    address = await world.connections.begin_sign_in(str(made.id))
    assert dict(parse_qsl(urlsplit(address).query))["scope"] == found.oauth.scope
    back = world.servers.browse(address)
    assert (await world.connections.finish_sign_in(error=None, **back)).signed_in
    assert await _run(close, number=4) == "Closed #4."


async def test_a_call_s_401_with_a_sign_in_only_by_hand_is_kept_but_cannot_begin(
    world: _World,
) -> None:
    made = await world.connections.create(name="Hand", url=_HAND, gems="all")

    assert await _run(await world.tool("hand__close_issue"), number=4) == (
        "This tool needs the user to sign in to Hand."
    )
    found = await world.get(made)
    assert found.oauth is not None and found.oauth.registration_endpoint is None
    with pytest.raises(UpstreamError, match="registered by hand"):
        await world.connections.begin_sign_in(str(made.id))


async def test_a_server_with_a_sign_in_only_by_hand_is_kept_needing_one(world: _World) -> None:
    made = await world.connections.create(name="Locked", url=_LOCKED)

    assert made.tools == [] and made.oauth is not None
    assert made.oauth.registration_endpoint is None
    assert made.signs_in and not made.signed_in()
    with pytest.raises(UpstreamError, match="registered by hand"):
        await world.connections.begin_sign_in(str(made.id))


async def test_a_call_s_401_with_no_sign_in_published_is_a_failure(world: _World) -> None:
    made = await world.connections.create(name="Silent", url=_SILENT, gems="all")

    with pytest.raises(McpFailure) as failure:
        await _run(await world.tool("silent__close_issue"), number=4)

    assert failure.value.sign_in_needed
    assert (await world.get(made)).oauth is None
    assert await _told(world) == []


async def test_a_403_is_not_a_sign_in(world: _World) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP, gems="all")
    await world.sign_in(made)
    world.servers.forbids = True

    with pytest.raises(McpFailure) as failure:
        await _run(await world.tool(_CLOSE), number=4)

    assert not failure.value.sign_in_needed
    assert "403" in failure.value.reason
    assert (await world.get(made)).signed_in()
    assert world.servers.renewed == 0
    assert await _told(world) == [str(made.id)]


async def test_a_401_is_a_typed_sign_in_needed_with_what_it_said(world: _World) -> None:
    with pytest.raises(McpFailure) as failure:
        await world.mcp.list_tools(McpServer(_MCP, token="not-a-pass"))

    assert failure.value.sign_in_needed
    assert failure.value.challenge is not None
    assert (failure.value.challenge.resource_metadata, failure.value.challenge.scope) == (
        _PRM,
        "tools",
    )


# ---- what changes a sign-in


async def test_a_new_backend_address_registers_again(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP)
    await world.sign_in(made)
    await world.connections.begin_sign_in(str(made.id))
    assert len(world.servers.clients) == 1  # the registration is used again

    monkeypatch.setattr(settings, "backend_url", "http://localhost:9000/")
    address = await world.connections.begin_sign_in(str(made.id))

    assert len(world.servers.clients) == 2
    new = "http://localhost:9000/v1/connections/sign-in/callback"
    assert dict(parse_qsl(urlsplit(address).query))["redirect_uri"] == new
    await world.connections.finish_sign_in(error=None, **world.servers.browse(address))
    client = (await world.get(made)).oauth_client
    assert client is not None and client.redirect_uri == new


async def test_a_new_address_drops_the_sign_in(world: _World) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP, gems="all")
    await world.sign_in(made)
    begun = world.servers.browse(await world.connections.begin_sign_in(str(made.id)))

    moved = await world.connections.update(str(made.id), {"url": _PLAIN})

    assert (moved.oauth, moved.oauth_client, moved.oauth_tokens) == (None, None, None)
    assert moved.tools[-1].name == "delete_everything"
    assert [icon.src for icon in moved.icons] == [_PNG_DATA]
    assert world.stored(made)["oauth_tokens"] is None
    # A sign-in begun for the old address brings nothing to the new one.
    assert not (await world.connections.finish_sign_in(error=None, **begun)).signed_in
    assert (await world.get(made)).oauth_tokens is None

    # Back to a server that asks for a sign-in: it needs one again, from the start.
    back = await world.connections.update(str(made.id), {"url": _MCP})
    assert back.oauth is not None and back.oauth_client is None and back.tools == []
    # The icons were the other server's too.
    assert (back.icons, back.website_url) == ([], None)
    assert world.stored(made)["icons"] == []
    assert not back.signed_in()


async def test_an_authorization_header_of_the_user_s_own_drops_the_sign_in(
    world: _World,
) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP)
    await world.sign_in(made)
    key = next(iter(world.servers.passes))  # a key the server takes

    keyed = await world.connections.update(
        str(made.id), {"headers": {"Authorization": f"Bearer {key}"}}
    )

    assert (keyed.oauth, keyed.oauth_client, keyed.oauth_tokens) == (None, None, None)
    assert len(keyed.tools) == 3


async def test_a_deleted_connection_drops_everything(world: _World) -> None:
    made = await world.connections.create(name="Tracker", url=_MCP)
    begun = world.servers.browse(await world.connections.begin_sign_in(str(made.id)))

    await world.connections.delete(str(made.id))

    assert world.docdb.collections["Connections"] == []
    assert not (await world.connections.finish_sign_in(error=None, **begun)).signed_in


# ---- what never leaves this backend


async def test_no_answer_frame_or_log_carries_what_lets_us_in(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    # A registration with a secret, sent the way that puts it in a header.
    async for world in _world(_SignInWorld(auth_methods=["client_secret_basic"])):
        chat = await _chat()
        link = LinkServiceV1Class(
            chat.chat,
            CallEntityClass(
                gems=chat.gems,
                darbar=DarbarEntityClass(cast("Any", chat.docdb)),
                voice=cast("Any", None),
                conversations=chat.conversations,
            ),
            world.connections,
        )
        socket, linked = await _open(link)
        service = ConnectionsServiceV1Class(world.connections)
        sent: list[str] = []

        made = await service.create_connection(
            ConnectionsModels.V1.Input.CreateConnection(name="Tracker", url=_MCP, gems="all")
        )
        started = await service.begin_sign_in(made.id)
        verifier = next(
            iter(world.connections._pending.values())  # pyright: ignore[reportPrivateUsage]
        ).verifier
        back = world.servers.browse(started.url)
        page = await service.finish_sign_in(error=None, **back)
        await socket.until("connection.changed")
        world.servers.lifetime = 30
        sent.append((await service.refresh_connection(made.id)).model_dump_json())
        sent.append((await service.get_connection(made.id)).model_dump_json())
        sent += [one.model_dump_json() for one in await service.list_connections()]
        sent.append(
            (
                await service.update_connection(
                    made.id, ConnectionsModels.V1.Input.UpdateConnection(name="Issues")
                )
            ).model_dump_json()
        )
        world.servers.revoke()
        world.servers.refreshes.clear()
        tool = await world.tool("tracker__find_issues")
        sent.append(await _run(tool, text="login"))
        await socket.until("connection.changed", 2)
        sent.append(made.model_dump_json())
        sent.append(bytes(page.body).decode())
        # The address the browser is sent to has to name the client, the state and the
        # PKCE challenge: it is searched for the rest only.
        begun = started.model_dump_json()
        sent.append(json.dumps(socket.frames))
        await _close(socket, linked)

        client = cast("Any", world.connections)._docdb
        stored = json.dumps(client.collections["Connections"], default=str)
        assert world.servers.given and world.servers.secrets and world.servers.clients
        everything = "\n".join(sent)
        logged = "\n".join(record.getMessage() for record in caplog.records)
        for secret in [*world.servers.given, *world.servers.secrets, verifier]:
            assert secret not in everything
            assert secret not in begun
            assert secret not in logged
        # Nothing else names the client or the state.
        for named in [*world.servers.clients, back["state"]]:
            assert named not in everything
            assert named not in logged
        assert {frame["type"] for frame in socket.frames} == {"link.ready", "connection.changed"}
        assert [f for f in socket.frames if f["type"] == "connection.changed"] == [
            {"type": "connection.changed", "id": made.id}
        ] * 2
        # It is all kept, though: in the database.
        assert world.servers.secrets[0] in stored


def test_a_server_prints_without_its_headers_or_pass() -> None:
    shown = repr(McpServer(_MCP, (("X-Key", "hidden-key"),), token="hidden-pass"))

    assert "hidden" not in shown


async def test_a_socket_that_has_gone_is_no_longer_told(world: _World) -> None:
    chat = await _chat()
    link = LinkServiceV1Class(
        chat.chat,
        CallEntityClass(
            gems=chat.gems,
            darbar=DarbarEntityClass(cast("Any", chat.docdb)),
            voice=cast("Any", None),
            conversations=chat.conversations,
        ),
        world.connections,
    )
    listeners = world.connections._listeners  # pyright: ignore[reportPrivateUsage]
    socket, linked = await _open(link)
    assert len(listeners) == 2  # the test's own, and the socket's
    await _close(socket, linked)

    assert len(listeners) == 1

"""Run an MCP server that asks for a sign-in only when a locked tool is called, for a live trial.

Run from the repository root:

    uv run python -m scripts.sign_in_demo_server         # http://127.0.0.1:8766/mcp
    uv run python -m scripts.sign_in_demo_server 9000    # another port

It is a small notes server, and its own sign-in server, the way Gmail's MCP server
behaves: anyone may open it and list its tools, `read_notes` works for anyone, and
`add_note` answers 401 with `WWW-Authenticate: Bearer resource_metadata="..."` until it
is called with a pass. Unlike Google's, its sign-in server lets a client register itself
(RFC 7591), so a sign-in can be followed to the end: the browser is shown an "Allow"
page, comes back with a code, and the code is swapped for a pass (PKCE, S256) that lasts
an hour and is renewed by a refresh token.

Add it to Cortex as a connection with only its address, open it to a gem, and ask the
gem to add a note. Everything it keeps is lost when it stops.
"""

import base64
import hashlib
import html
import json
import secrets
import sys
from typing import Any
from urllib.parse import urlencode

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
ORIGIN = f"http://127.0.0.1:{PORT}"
MCP_URL = f"{ORIGIN}/mcp"
PRM_URL = f"{ORIGIN}/.well-known/oauth-protected-resource/mcp"
LOCKED = frozenset({"add_note"})
LIFETIME = 3600

notes: list[str] = ["Buy milk."]
clients: dict[str, dict[str, Any]] = {}
codes: dict[str, dict[str, str]] = {}
passes: set[str] = set()
refreshes: set[str] = set()


def _server() -> MCPServer:
    server = MCPServer("notes")

    @server.tool(title="Read notes")
    async def read_notes() -> str:
        """Read every note. Anyone may."""
        return "\n".join(f"{n}. {text}" for n, text in enumerate(notes, 1)) or "No notes."

    @server.tool(title="Add a note")
    async def add_note(text: str) -> str:
        """Add a note. Needs the user to have signed in."""
        notes.append(text)
        return f"Added note {len(notes)}: {text}"

    return server


def _json(body: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _protected_resource() -> Response:
    return _json(
        {"resource": MCP_URL, "authorization_servers": [ORIGIN], "scopes_supported": ["notes"]}
    )


def _authorization_server() -> Response:
    return _json(
        {
            "issuer": ORIGIN,
            "authorization_endpoint": f"{ORIGIN}/authorize",
            "token_endpoint": f"{ORIGIN}/token",
            "registration_endpoint": f"{ORIGIN}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["notes"],
            "authorization_response_iss_parameter_supported": True,
        }
    )


async def _register(request: Request) -> Response:
    asked = await request.json()
    client_id = f"client-{secrets.token_hex(6)}"
    clients[client_id] = {**asked, "client_id": client_id, "token_endpoint_auth_method": "none"}
    print(f"registered {client_id} for {asked.get('redirect_uris')}")
    return _json(clients[client_id], 201)


async def _authorize(request: Request) -> Response:
    """GET: the page the browser signs in at. POST: the user allowed it."""
    asked = dict(request.query_params) if request.method == "GET" else dict(await request.form())
    client = clients.get(asked.get("client_id", ""))
    if client is None or asked.get("redirect_uri") not in client["redirect_uris"]:
        return HTMLResponse("<p>Unknown client, or an address it did not register.</p>", 400)
    if asked.get("code_challenge_method") != "S256" or not asked.get("code_challenge"):
        return HTMLResponse("<p>A sign-in here needs PKCE (S256).</p>", 400)
    if request.method == "GET":
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
            for k, v in asked.items()
        )
        return HTMLResponse(
            "<!doctype html><title>Notes demo sign-in</title>"
            "<body style='font-family:system-ui;max-width:28rem;margin:4rem auto'>"
            "<h2>Notes demo</h2><p>Cortex asks to add notes for you.</p>"
            f"<form method='post' action='/authorize'>{hidden}"
            "<button style='font-size:1.1rem;padding:.5rem 1.5rem'>Allow</button></form></body>"
        )
    code = secrets.token_urlsafe(16)
    codes[code] = asked
    back = {"code": code, "state": asked.get("state", ""), "iss": ORIGIN}
    print(f"signed in: code given to {asked['client_id']}")
    return RedirectResponse(f"{asked['redirect_uri']}?{urlencode(back)}", status_code=302)


def _pass() -> Response:
    access, refresh = f"pass-{secrets.token_hex(12)}", f"refresh-{secrets.token_hex(12)}"
    passes.add(access)
    refreshes.add(refresh)
    return _json(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": LIFETIME,
            "refresh_token": refresh,
            "scope": "notes",
        }
    )


async def _token(request: Request) -> Response:
    form = dict(await request.form())
    if form.get("client_id") not in clients:
        return _json({"error": "invalid_client"}, 401)
    if form.get("grant_type") == "authorization_code":
        asked = codes.pop(str(form.get("code")), None)
        verifier = str(form.get("code_verifier", "")).encode()
        digest = hashlib.sha256(verifier).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if (
            asked is None
            or asked["client_id"] != form["client_id"]
            or asked["redirect_uri"] != form.get("redirect_uri")
            or asked["code_challenge"] != challenge
            or form.get("resource", MCP_URL) != MCP_URL
        ):
            return _json({"error": "invalid_grant"}, 400)
        print("pass given")
        return _pass()
    if form.get("grant_type") == "refresh_token":
        if form.get("refresh_token") not in refreshes:
            return _json({"error": "invalid_grant"}, 400)
        print("pass renewed")
        return _pass()
    return _json({"error": "unsupported_grant_type"}, 400)


def main() -> None:
    server = _server()
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp_app = server.streamable_http_app(
        json_response=True, stateless_http=True, transport_security=security
    )
    routes = {
        "/.well-known/oauth-protected-resource/mcp": _protected_resource,
        "/.well-known/oauth-protected-resource": _protected_resource,
        "/.well-known/oauth-authorization-server": _authorization_server,
    }

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await mcp_app(scope, receive, send)
            return
        path = scope["path"]
        request = Request(scope, receive)
        if path in routes:
            response = routes[path]()
        elif path == "/register":
            response = await _register(request)
        elif path == "/authorize":
            response = await _authorize(request)
        elif path == "/token":
            response = await _token(request)
        elif path == "/mcp":
            # Read the whole request, to see whether it calls a locked tool, then hand
            # it on as it came.
            body = await request.body()
            message = json.loads(body) if body.strip().startswith(b"{") else {}
            called = message.get("method") == "tools/call"
            tool = (message.get("params") or {}).get("name") if called else None
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            if tool in LOCKED and bearer not in passes:
                print(f"{tool}: refused, no pass")
                challenge = f'Bearer resource_metadata="{PRM_URL}"'
                response = Response(status_code=401, headers={"WWW-Authenticate": challenge})
                await response(scope, receive, send)
                return
            if tool is not None:
                print(f"{tool}: called")
            sent = False

            async def replay() -> dict[str, Any]:
                nonlocal sent
                if sent:
                    return await receive()
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            await mcp_app(scope, replay, send)
            return
        else:
            response = Response(status_code=404)
        await response(scope, receive, send)

    sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue]
    print(f"Notes demo at {MCP_URL}: read_notes is open, add_note asks for a sign-in.")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on")


if __name__ == "__main__":
    main()

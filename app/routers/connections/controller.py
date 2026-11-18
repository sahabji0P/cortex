from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

from app.entities.connection_ent import SIGN_IN_CALLBACK_PATH
from app.routers.connections.models import ConnectionsModels
from app.routers.connections.service import ConnectionsServiceRegistry

# No sign-in, like the gems routes: this backend has one user for now.
router = APIRouter(prefix="/v1/connections", tags=["connections"])


@router.post("", status_code=201)
async def create_connection(
    payload: ConnectionsModels.V1.Input.CreateConnection,
    service: ConnectionsServiceRegistry.V1,
) -> ConnectionsModels.V1.Output.Connection:
    """Add a tool server. It is asked for its tools now, and refused (502) if it does not answer."""
    return await service.create_connection(payload)


@router.get("")
async def list_connections(
    service: ConnectionsServiceRegistry.V1,
) -> list[ConnectionsModels.V1.Output.Connection]:
    """Every connection, in the order they were added."""
    return await service.list_connections()


@router.get("/{connection_id}")
async def get_connection(
    connection_id: str, service: ConnectionsServiceRegistry.V1
) -> ConnectionsModels.V1.Output.Connection:
    return await service.get_connection(connection_id)


@router.patch("/{connection_id}")
async def update_connection(
    connection_id: str,
    payload: ConnectionsModels.V1.Input.UpdateConnection,
    service: ConnectionsServiceRegistry.V1,
) -> ConnectionsModels.V1.Output.Connection:
    """Change it, and who may use it. A new address or headers asks the server again."""
    return await service.update_connection(connection_id, payload)


@router.post("/{connection_id}/refresh")
async def refresh_connection(
    connection_id: str, service: ConnectionsServiceRegistry.V1
) -> ConnectionsModels.V1.Output.Connection:
    """Ask the server for its tools again, and keep what it lists now."""
    return await service.refresh_connection(connection_id)


@router.post("/{connection_id}/sign-in")
async def begin_sign_in(
    connection_id: str, service: ConnectionsServiceRegistry.V1
) -> ConnectionsModels.V1.Output.SignInStart:
    """Begin a sign-in to the server: the address to open in the user's browser.

    A 422 for a connection with its own Authorization header, or whose server lets us
    in without one. A 502 if the server's sign-in cannot be found or used.
    """
    return await service.begin_sign_in(connection_id)


# Where the user's browser comes back after a sign-in. It is a route on this backend,
# reached at `settings.backend_url` (by default http://127.0.0.1:8000), because the
# backend runs on the user's own Mac, where the browser is: the sign-in server sends the
# browser back to localhost. That has to be rethought when the backend is deployed, or
# runs on another machine than the browser. Every value is optional, so the browser is
# always answered with a page, never with JSON.
@router.get(SIGN_IN_CALLBACK_PATH.removeprefix(router.prefix), response_class=HTMLResponse)
async def finish_sign_in(
    service: ConnectionsServiceRegistry.V1,
    state: str | None = None,
    code: str | None = None,
    iss: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """A page for the browser: signed in, and the user can go back to Cortex, or why not."""
    return await service.finish_sign_in(state=state, code=code, iss=iss, error=error)


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(connection_id: str, service: ConnectionsServiceRegistry.V1) -> Response:
    await service.delete_connection(connection_id)
    return Response(status_code=204)

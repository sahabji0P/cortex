from fastapi import APIRouter, Response

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


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(connection_id: str, service: ConnectionsServiceRegistry.V1) -> Response:
    await service.delete_connection(connection_id)
    return Response(status_code=204)

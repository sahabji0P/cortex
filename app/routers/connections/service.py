from functools import cache
from typing import Annotated

from fastapi import Depends

from app.entities import EntitiesRegistry
from app.entities.connection_ent import Connection, ConnectionEntityClass, offered_name
from app.routers.connections.models import ConnectionsModels


class ConnectionsServiceV1Class:
    def __init__(self, connections: ConnectionEntityClass | None = None) -> None:
        # Tests hand in their own. The app uses the one the registry made at startup.
        self.connections = EntitiesRegistry.Connections if connections is None else connections

    @staticmethod
    def _connection_out(connection: Connection) -> ConnectionsModels.V1.Output.Connection:
        return ConnectionsModels.V1.Output.Connection(
            id=str(connection.id),
            key=connection.key,
            name=connection.name,
            url=connection.url,
            # The names only: what lets us in to the server is never sent back.
            headers=[header.name for header in connection.headers],
            enabled=connection.enabled,
            gems=connection.gems,
            tools=[
                ConnectionsModels.V1.Output.Tool(
                    name=tool.name,
                    title=tool.title,
                    description=tool.description,
                    offered_as=offered_name(connection.key, tool.name),
                    gems=connection.limit(tool.name),
                )
                for tool in connection.tools
            ],
            tools_left_out=connection.tools_left_out,
            checked_at=connection.checked_at,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
        )

    async def create_connection(
        self, payload: ConnectionsModels.V1.Input.CreateConnection
    ) -> ConnectionsModels.V1.Output.Connection:
        connection = await self.connections.create(
            name=payload.name,
            url=payload.url,
            headers=payload.headers,
            gems=payload.gems,
            enabled=payload.enabled,
        )
        return self._connection_out(connection)

    async def list_connections(self) -> list[ConnectionsModels.V1.Output.Connection]:
        return [self._connection_out(one) for one in await self.connections.list_all()]

    async def get_connection(self, connection_id: str) -> ConnectionsModels.V1.Output.Connection:
        return self._connection_out(await self.connections.get(connection_id))

    async def update_connection(
        self, connection_id: str, payload: ConnectionsModels.V1.Input.UpdateConnection
    ) -> ConnectionsModels.V1.Output.Connection:
        return self._connection_out(await self.connections.update(connection_id, payload.changes()))

    async def refresh_connection(
        self, connection_id: str
    ) -> ConnectionsModels.V1.Output.Connection:
        return self._connection_out(await self.connections.refresh(connection_id))

    async def delete_connection(self, connection_id: str) -> None:
        await self.connections.delete(connection_id)


# Cached, so every request gets the one service.
@cache
def get_connections_service_v1() -> ConnectionsServiceV1Class:
    return ConnectionsServiceV1Class()


class ConnectionsServiceRegistry:
    type V1 = Annotated[ConnectionsServiceV1Class, Depends(get_connections_service_v1)]

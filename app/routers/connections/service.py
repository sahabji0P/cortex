from functools import cache
from html import escape
from typing import Annotated

from fastapi import Depends
from fastapi.responses import HTMLResponse

from app.entities import EntitiesRegistry
from app.entities.connection_ent import Connection, ConnectionEntityClass, offered_name
from app.routers.connections.models import ConnectionsModels

# The page the browser is shown when a sign-in comes back. Every value put in it is
# escaped: some come from the address the browser was sent to.
_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:4rem auto}}</style>
</head>
<body><h1>{title}</h1><p>{text}</p></body>
</html>
"""


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
            sign_in=ConnectionsModels.V1.Output.SignIn(
                state="signed_in" if connection.signed_in() else "sign_in_needed",
                expires_at=None
                if connection.oauth_tokens is None
                else connection.oauth_tokens.expires_at,
                renews=connection.renews,
            )
            if connection.signs_in
            else None,
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

    async def begin_sign_in(self, connection_id: str) -> ConnectionsModels.V1.Output.SignInStart:
        return ConnectionsModels.V1.Output.SignInStart(
            url=await self.connections.begin_sign_in(connection_id)
        )

    async def finish_sign_in(
        self, *, state: str | None, code: str | None, iss: str | None, error: str | None
    ) -> HTMLResponse:
        outcome = await self.connections.finish_sign_in(
            state=state, code=code, iss=iss, error=error
        )
        title = "Signed in" if outcome.signed_in else "Not signed in"
        return HTMLResponse(
            _PAGE.format(title=escape(title), text=escape(outcome.text)),
            status_code=200 if outcome.signed_in else 400,
            # The page holds nothing worth keeping, and is not to be kept.
            headers={"Cache-Control": "no-store"},
        )


# Cached, so every request gets the one service.
@cache
def get_connections_service_v1() -> ConnectionsServiceV1Class:
    return ConnectionsServiceV1Class()


class ConnectionsServiceRegistry:
    type V1 = Annotated[ConnectionsServiceV1Class, Depends(get_connections_service_v1)]

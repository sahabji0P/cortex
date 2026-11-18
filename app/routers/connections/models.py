from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.entities.connection_ent import ConnectionHeaders, ConnectionName, ConnectionUrl, GemScope


class ConnectionsModels:
    class V1:
        class Input:
            class CreateConnection(BaseModel):
                name: ConnectionName
                # Where the server is: an http or https address, reached over
                # Streamable HTTP.
                url: ConnectionUrl
                # Sent with every request to the server, such as
                # `{"Authorization": "Bearer ..."}`. Their values are never sent back.
                headers: ConnectionHeaders = Field(default_factory=dict)
                # The gems it is open to: "all", or a list of ids. Left out, none.
                gems: GemScope = Field(default_factory=list[str])
                enabled: bool = True

            class UpdateConnection(BaseModel):
                """A partial change. A field left out keeps its value, and none may be null.

                `headers` replaces every header: the values kept are never sent to a
                client, so one cannot be changed and the rest left. `tool_gems` names
                only the tools whose limit changes: a list of gem ids limits the tool
                to them (an empty one turns it off), and null lifts its limit.
                """

                name: ConnectionName | None = None
                url: ConnectionUrl | None = None
                headers: ConnectionHeaders | None = None
                gems: GemScope | None = None
                enabled: bool | None = None
                tool_gems: dict[str, list[str] | None] | None = None

                @model_validator(mode="after")
                def _refuse_null(self) -> "ConnectionsModels.V1.Input.UpdateConnection":
                    for field in self.model_fields_set:
                        if getattr(self, field) is None:
                            raise ValueError(f"A connection's {field} cannot be null")
                    return self

                def changes(self) -> dict[str, object]:
                    """Only the fields that were sent."""
                    return self.model_dump(exclude_unset=True)

        class Output:
            class Tool(BaseModel):
                # As the server names it. This is the name `tool_gems` takes.
                name: str
                title: str | None
                description: str
                # As the model is told its name, and as a conversation's history has it.
                offered_as: str
                # The gems it is limited to, out of those the connection is open to.
                # Null for no limit: it is open to whoever the connection is.
                gems: list[str] | None

            class Connection(BaseModel):
                id: str
                # The start of its tools' names as the model is told them. Never changes.
                key: str
                name: str
                url: str
                # The names of the headers kept. Their values are never sent.
                headers: list[str]
                enabled: bool
                gems: Literal["all"] | list[str]
                # The server's tools as they were when it was last asked, at `checked_at`.
                tools: list["ConnectionsModels.V1.Output.Tool"]
                # How many tools the server listed that cannot be used here.
                tools_left_out: int
                checked_at: datetime
                created_at: datetime
                updated_at: datetime

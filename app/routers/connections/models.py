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
                # With no `Authorization` header, a server that asks for a sign-in is
                # kept, needing one.
                headers: ConnectionHeaders = Field(default_factory=dict)
                # The gems it is open to: "all", or a list of ids. Left out, none.
                gems: GemScope = Field(default_factory=list[str])
                enabled: bool = True
                # On, the gems find and load its tools by searching, instead of being
                # told of them all with every request.
                deferred: bool = True

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
                deferred: bool | None = None
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

            class Icon(BaseModel):
                """One of the server's own icons, as it stated it."""

                # Always a `data:` address: the picture itself, so the app never fetches
                # one. PNG, JPEG, WebP or SVG, at most 64 KB.
                src: str
                mime_type: str
                # As the server said, such as "48x48" or "any". Empty when it did not say.
                sizes: list[str]
                # The background it is made for: "light", "dark", or null for either.
                theme: Literal["light", "dark"] | None

            class SignIn(BaseModel):
                """A connection's sign-in, as far as the app needs to show it.

                Nothing that lets us in is here: not the pass, not what renews it, not
                the registration.
                """

                # "signed_in": it has a pass that may still let us in. "sign_in_needed":
                # it has none, or its pass ran out and cannot be renewed, or the server
                # refused it.
                state: Literal["signed_in", "sign_in_needed"]
                # When the pass runs out. Null when it is not known, or there is none:
                # a sign-in server that does not say leaves it unknown, not never.
                expires_at: datetime | None
                # Whether the pass is renewed without the user before it runs out.
                renews: bool

            class SignInStart(BaseModel):
                # The address to open in the user's browser. It works once, for 10 minutes.
                url: str

            class Connection(BaseModel):
                id: str
                # The start of its tools' names as the model is told them. Never changes.
                key: str
                name: str
                url: str
                # The names of the headers kept. Their values are never sent.
                headers: list[str]
                enabled: bool
                # Whether its tools are found and loaded by searching (`tool_search`).
                deferred: bool
                gems: Literal["all"] | list[str]
                # The server's tools as they were when it was last asked, at `checked_at`.
                tools: list["ConnectionsModels.V1.Output.Tool"]
                # How many tools the server listed that cannot be used here.
                tools_left_out: int
                # The server's own icons, at most three, in its order, as it stated them
                # when it was last asked. Empty when it stated none this backend holds.
                icons: list["ConnectionsModels.V1.Output.Icon"]
                # The server's website, as it stated it. Null when it did not.
                website_url: str | None
                # Null for a connection that does not sign in: it uses its headers only.
                sign_in: "ConnectionsModels.V1.Output.SignIn | None"
                checked_at: datetime
                created_at: datetime
                updated_at: datetime

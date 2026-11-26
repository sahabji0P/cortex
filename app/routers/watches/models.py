from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.entities.gem_event import WatchSource
from app.entities.watch_ent import WatchState


class WatchesModels:
    class V1:
        class Output:
            class Watch(BaseModel):
                """One of a gem's watches, as the app shows it: the contract says each field.

                Never with its webhook's address or secret: those are the gem's, and lie in
                its conversation.
                """

                id: str
                gem: str
                # What it is for, in the gem's own words.
                about: str
                source: WatchSource
                state: WatchState
                # Why it is failing or paused, or null.
                reason: str | None
                # For MCP events: the connection's id, the event, and its arguments.
                connection: str | None
                event: str | None
                arguments: dict[str, Any] | None
                # For a timer: how often, and when it is next due.
                every_minutes: int | None
                next_at: datetime | None
                # Whether posts to an address of ours wake it: a plain webhook.
                has_address: bool
                created_at: datetime
                last_event_at: datetime | None
                events: int

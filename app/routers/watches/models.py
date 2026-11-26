from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.entities.gem_event import WatchDelivery, WatchSource
from app.entities.watch_ent import WatchState


class WatchesModels:
    class V1:
        class Output:
            class Watch(BaseModel):
                """One of a gem's watches, as the app shows it: the contract says each field.

                Never with its webhook's address or secret: those come only from `Hook`,
                when the user asks to see them.
                """

                id: str
                gem: str
                # What it is for, in the gem's own words.
                about: str
                source: WatchSource
                # How its events reach us now: "push", "webhook" or "poll", or null.
                delivery: WatchDelivery | None
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
                # When it last brought an event, and how many it has brought: only those
                # kept, not one delivered again.
                last_event_at: datetime | None
                events: int
                # For a plain webhook: how many posts it refused for their signature, and
                # when it last did, and whether it takes only signed posts now. Never what
                # they are signed with.
                refused: int
                last_refused_at: datetime | None
                signed: bool

            class Hook(BaseModel):
                """A plain webhook watch's address and secret, for the user to see when they ask.

                What the gem was given to register it. The address ends in the watch's
                token, and the secret is another: a watch stored before watches kept a
                secret of its own has its token as its secret.
                """

                # Null while this backend has no public address (PUBLIC_URL).
                address: str | None
                secret: str
                content_type: str

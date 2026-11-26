"""
Gem events: what wakes a gem, and the seams between the parts that make one.

A gem can watch things (a `watch`): something that happens wakes it, and it does what
its instructions say. Three sources make events:

- a connection's MCP events (SEP-3415, the extension `io.modelcontextprotocol/events`),
  delivered by poll, by stream or by webhook, whichever the server offers,
- a plain webhook, such as GitHub's own, posted to an address of ours,
- a timer.

Each watch belongs to one gem. Whatever its source, an event that comes in is handed to
an `EventSink` as a `GemEvent`. The sink makes it part of the gem's one conversation: it
never cuts a reply, it waits for the gem's next turn and goes in ahead of what the user
says, and a gem that is quiet is woken by it.

This file holds only the shapes that cross between those parts, so each can be built
and tested on its own:

- `GemEvent` and `EventSink`: between every source and the gem's conversation.
- `McpEvents`, `McpEventKind` and `McpWatch`: between watches and the connections that
  serve MCP events.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from bson import ObjectId

__all__ = [
    "EVENT_TEXT_LIMIT",
    "EventSink",
    "GemEvent",
    "McpEventKind",
    "McpEvents",
    "McpWatch",
    "OnMcpCursor",
    "OnMcpEvent",
    "WatchSource",
]

# Where an event comes from.
type WatchSource = Literal["mcp", "webhook", "timer"]

# An event's `data` is cut to this many characters, as a connection's tool result is.
# THE OWNER HAS NOT RULED ON THIS NUMBER.
EVENT_TEXT_LIMIT = 20_000


@dataclass(frozen=True, slots=True)
class GemEvent:
    """One thing that happened, that a gem watches for."""

    # The gem whose watch it is.
    gem_id: ObjectId
    # The watch that caught it.
    watch_id: str
    source: WatchSource
    # What happened, as its source names it: an MCP event's name, a webhook's own name
    # for it (GitHub's `X-GitHub-Event`, else "webhook"), or "timer".
    name: str
    # What the watch is for, in the gem's own words, as it said when it made the watch.
    about: str
    # What happened, as text for the model: an MCP event's data or a webhook's body,
    # cut to `EVENT_TEXT_LIMIT`. Empty for a timer.
    data: str
    # When it happened, by its source, or when it came in if its source does not say.
    at: datetime
    # The source's own id for it, if it has one (an MCP `eventId`, a webhook's delivery
    # id), so the same event delivered twice can be known.
    event_id: str | None = None


class EventSink(Protocol):
    """Takes the events that come in, and makes them part of their gem's conversation."""

    async def arrived(self, event: GemEvent) -> None:
        """An event has come in. Returns once it is safely kept, before the gem answers it.

        Raises if it could not be kept: the source may then try again.
        """
        ...


@dataclass(frozen=True, slots=True)
class McpEventKind:
    """One kind of event a connection's server can send, as `events/list` lists it."""

    # The connection that serves it.
    connection_id: str
    # The connection's key, as its tools' names start with it.
    connection_key: str
    name: str
    description: str
    # The deliveries the server offers for it: any of "poll", "push" and "webhook".
    delivery: list[str]
    # The JSON schema of the arguments a watch gives it, such as a repo and a number.
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpWatch:
    """What a watch on a connection's MCP events is, as the connection runs it."""

    watch_id: str
    gem_id: ObjectId
    connection_id: str
    event: str
    arguments: dict[str, Any]
    # Where the last event left off, as the server gave it, or None to start from now.
    cursor: str | None = None


# Is told each MCP event of a watch: its name, its data as text, its own id, when it
# happened (None if the server does not say), and the cursor after it, which the watch
# keeps so that it resumes from there.
type OnMcpEvent = Callable[[str, str, str | None, datetime | None, str | None], Awaitable[None]]


# Is told a watch's cursor when it moves with no event: a poll that brought none, a
# stream's start or heartbeat, a webhook's renewal. The watch keeps it, so that it
# resumes from there and not from now.
type OnMcpCursor = Callable[[str], Awaitable[None]]


class McpEvents(Protocol):
    """The connections' side of MCP events: what they serve, and running a watch on one."""

    async def kinds_for(self, gem_id: ObjectId) -> list[McpEventKind]:
        """The kinds of event served by the connections open to the gem."""
        ...

    async def watch(
        self,
        watch: McpWatch,
        on_event: OnMcpEvent,
        hook_url: str | None,
        on_cursor: OnMcpCursor | None = None,
    ) -> None:
        """Run a watch until it is cancelled, telling `on_event` of each event in order,
        and `on_cursor` of the cursor when it moves with none.

        The delivery is chosen here from what the server offers: stream, then webhook
        (only with a `hook_url`, the public address its events are to be posted to),
        then poll. Runs for as long as the watch does, and comes back to the server
        after a drop from the last cursor. Raises if the watch cannot run at all, such
        as when the connection is gone, is no longer open to the gem, or does not serve
        the event: the watch is then failing, and says why.
        """
        ...

    async def posted(
        self, watch: McpWatch, headers: Mapping[str, str], body: bytes, on_event: OnMcpEvent
    ) -> bool:
        """A post came to a watch's webhook address: check it, and tell `on_event` of it.

        Returns False if it is not a post the server signed for this watch, and then
        nothing is told.
        """
        ...

"""
Conversation entity: each gem's one conversation, and who is adding to it.

A gem is one entity with one conversation. The database is the truth (see
`conversation_store.py`): every reply is made from what is stored, and what it adds is
stored after it. What is kept in memory, for each gem, is a `GemConversation`:

- who is adding to the conversation now: one writer at a time,
- the gem's tool calls that outlived their replies, until their results are told (see
  `late_results.py`): one `LateResults` for the gem, whoever made the call,
- the user's written turn that no reply has got through to yet (see `history.py`).

A gem with none of these is not kept. Running calls live in this process's memory, so a
restart loses them. A placeholder whose call is gone is closed with a stub saying so,
before the next reply is made.
"""

import asyncio
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from bson import ObjectId

from app.core.errors import NotFoundError
from app.entities.conversation_store import ConversationItem, ConversationStoreClass
from app.entities.gem_ent import GemEntityClass
from app.entities.history import Added, UserTurn
from app.entities.late_results import LateResult, LateResults, delivers, late_stub
from app.entities.loop import ToolCall
from app.entities.model import Item

__all__ = [
    "ConversationEntityClass",
    "GemConversation",
    "Message",
    "Writer",
    "gem_key",
    "stubs",
]

# The result a call lost to a restart is given, so its placeholder does not promise a
# result for ever. THE OWNER HAS NOT RULED ON THIS WORDING.
_LOST = (
    "This call was lost before it finished, and its result will not arrive. Make it again "
    "if it is still needed."
)


@dataclass(frozen=True, slots=True)
class Message:
    """One message of a gem's conversation, as it is shown."""

    role: Literal["user", "gem", "tool"]
    text: str
    at: datetime


class Writer(Protocol):
    """A reply being written to a gem, which holds the gem until it is stored."""

    # Set once the reply is stored and its last frame sent (or could not be).
    over: asyncio.Event

    def cut(self) -> None:
        """Cut the reply off where it is."""
        ...


class GemConversation:
    """What is kept in memory of one gem's conversation."""

    def __init__(self, results_came: Callable[[], None]) -> None:
        # The gem's calls that outlived their replies, until their results are told.
        # `results_came` is called when one comes in.
        self.late = LateResults(results_came)
        # The reply being written to the gem, until it is stored: one at a time.
        self.writer: Writer | None = None
        # Messages whose replies were cut or failed before they left anything in the
        # history. The next message joins them, and they are stored as one.
        self.turn = UserTurn()

    @property
    def idle(self) -> bool:
        """Whether nothing of it needs keeping."""
        return self.writer is None and not self.late.pending and not self.turn.open


class ConversationEntityClass:
    def __init__(self, *, gems: GemEntityClass, store: ConversationStoreClass) -> None:
        self._gems = gems
        self._store = store
        # What is kept of each gem's conversation, by `gem_key`.
        self._kept: dict[str, GemConversation] = {}
        self._quiet: Callable[[str], None] = _nobody

    def tell_results_with(self, quiet: Callable[[str], None]) -> None:
        """Who tells a gem's late results: `quiet` is given the gem's key when one comes in.

        That is chat: it writes the gem a reply about them, once nothing else is.
        """
        self._quiet = quiet

    def gem(self, key: str) -> GemConversation:
        """What is kept of a gem's conversation, made if there is none yet."""
        kept = self._kept.get(key)
        if kept is None:
            kept = self._kept[key] = GemConversation(lambda: self._quiet(key))
        return kept

    def find(self, key: str) -> GemConversation | None:
        """What is kept of a gem's conversation, if anything is."""
        return self._kept.get(key)

    def kept(self) -> Iterable[GemConversation]:
        return list(self._kept.values())

    def let_go(self, key: str) -> None:
        """Keep nothing for a gem whose conversation needs nothing kept."""
        kept = self._kept.get(key)
        if kept is not None and kept.idle:
            del self._kept[key]

    async def messages(self, gem_id: str) -> list[Message]:
        """A gem's conversation as it is shown, oldest first.

        Raises `NotFoundError` if there is no such gem. A gem whose config cannot be
        read still has its conversation shown.
        """
        if not await self._gems.exists(gem_id):
            raise NotFoundError("Gem")
        shown: list[Message] = []
        for stored in await self._store.load(ObjectId(gem_id)):
            item = stored.item
            if "role" in item:
                if item["role"] == "user":
                    shown.append(Message("user", item["content"], stored.at))
                elif item["role"] == "assistant":
                    shown.append(Message("gem", item["content"], stored.at))
            # Stubs and cancels are stored with nothing to show.
            elif item["type"] == "function_call" and stored.shown is not None:
                shown.append(Message("tool", stored.shown, stored.at))
        return shown

    async def load(self, gem_id: ObjectId, *, told: Sequence[LateResult] = ()) -> list[Item]:
        """The gem's conversation as it is stored, with the calls lost to a restart closed.

        Each lost call is closed with a stub, stored after the conversation. `told` are
        late results about to be added, whose calls are not lost.
        """
        stored = await self._store.load(gem_id)
        history = [entry.item for entry in stored]
        lost = self._lost(str(gem_id), stored, told)
        return history + await self.add(gem_id, history, stubs(lost))

    async def add(
        self, gem_id: ObjectId, history: Sequence[Item], added: Sequence[Added]
    ) -> list[Item]:
        """Store items after the conversation `history`, all or none. Return them."""
        await self._store.add(
            [
                ConversationItem(
                    gem_id=gem_id,
                    place=len(history) + offset,
                    at=one.at,
                    item=one.item,
                    shown=one.shown,
                    mark=one.mark,
                )
                for offset, one in enumerate(added)
            ]
        )
        return [one.item for one in added]

    async def forget(self, gem_id: ObjectId) -> None:
        """Forget a gem's stored conversation, as when the gem was deleted meanwhile."""
        await self._store.forget(gem_id)

    def _lost(
        self, key: str, stored: Sequence[ConversationItem], told: Sequence[LateResult]
    ) -> list[ToolCall]:
        """A stub for each call lost to a restart, closing its placeholder.

        Lost is a placeholder whose call is not running or waiting here (or being `told`
        now), that no stub has delivered, and that no `cancel_tool_call` stopped. Each is
        known by the mark stored with its result, never by the result's wording.
        """
        kept = self._kept.get(key)
        here = {late.call.call_id for late in told}
        items = [entry.item for entry in stored]
        calls = {
            item["call_id"]: item
            for item in items
            if "type" in item and item["type"] == "function_call"
        }
        delivered = {original for item in items if (original := delivers(item))}
        placeholders: list[str] = []
        cancelled: set[str | None] = set()
        for entry in stored:
            item = entry.item
            if "type" not in item or item["type"] != "function_call_output":
                continue
            if entry.mark == "placeholder":
                placeholders.append(item["call_id"])
            elif entry.mark == "cancelled" and item["call_id"] in calls:
                cancelled.add(_named(calls[item["call_id"]]["arguments"]))
        lost: list[ToolCall] = []
        for call_id in placeholders:
            if call_id not in calls or call_id in here or call_id in delivered:
                continue
            if call_id in cancelled or (kept is not None and kept.late.holds(call_id)):
                continue
            call = calls[call_id]
            lost.append(late_stub(ToolCall(call_id, call["name"], call["arguments"]), _LOST))
        return lost


def gem_key(gem_id: str) -> str:
    """The same gem by whichever spelling of its id. One that is not an id is kept as is."""
    return str(ObjectId(gem_id)) if ObjectId.is_valid(gem_id) else gem_id


def stubs(calls: Iterable[ToolCall]) -> list[Added]:
    """Stub calls and their results, as they are added. A stub shows nothing."""
    now = datetime.now(UTC)
    return [Added(item, now) for call in calls for item in call.items()]


def _named(arguments: str) -> str | None:
    """The call_id a `cancel_tool_call` named, if it can be read."""
    try:
        named = json.loads(arguments)["call_id"]
    except (ValueError, KeyError, TypeError):
        return None
    return str(named)


def _nobody(key: str) -> None:
    pass

"""The gem's log: its order, and what it holds beside the items of the conversation."""

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId

from app.entities.conversation_ent import ConversationEntityClass, Stored
from app.entities.conversation_store import ConversationStoreClass
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_log import LogEntityClass
from app.entities.history import Added, line, stubs
from app.entities.loop import ToolCall
from tests.test_chat import _docdb, _stored_items

_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _user(text: str) -> Added:
    return Added({"role": "user", "content": text}, _NOW)


async def _conversations() -> tuple[Any, ConversationStoreClass, ConversationEntityClass]:
    docdb, adapter = _docdb()
    store = ConversationStoreClass(adapter)
    return docdb, store, ConversationEntityClass(gems=GemEntityClass(adapter), store=store)


async def test_items_are_numbered_in_the_order_they_are_added() -> None:
    docdb, _, conversations = await _conversations()
    gem_id = ObjectId()

    stored = await conversations.add(gem_id, Stored([]), [_user("one"), _user("two")], "chat")
    await conversations.add(gem_id, stored, [_user("three")], "chat")

    assert [(d["seq"], d["place"]) for d in _stored_items(docdb)] == [(0, 0), (1, 1), (2, 2)]


async def test_each_gem_has_its_own_numbers() -> None:
    docdb, _, conversations = await _conversations()
    one, other = ObjectId(), ObjectId()

    await conversations.add(one, Stored([]), [_user("hello")], "chat")
    await conversations.add(other, Stored([]), [_user("hello")], "chat")

    assert [d["seq"] for d in _stored_items(docdb)] == [0, 0]


async def test_the_numbers_carry_on_after_a_restart() -> None:
    docdb, store, conversations = await _conversations()
    gem_id = ObjectId()
    stored = await conversations.add(gem_id, Stored([]), [_user("one"), _user("two")], "chat")

    # New entities over the same database, as after a restart.
    again = ConversationEntityClass(gems=conversations._gems, store=store)
    await again.add(gem_id, stored, [_user("three")], "chat")

    assert [d["seq"] for d in _stored_items(docdb)] == [0, 1, 2]


async def test_two_who_open_a_gems_log_at_once_do_not_hand_out_a_number_twice() -> None:
    _, store, conversations = await _conversations()
    gem_id = ObjectId()
    await conversations.add(gem_id, Stored([]), [_user("one")], "chat")
    log = LogEntityClass(store)

    await log.open(gem_id)
    first = log.take(gem_id)
    # A second reading of where the log stands does not undo what was counted since.
    await log.open(gem_id)

    assert list(first) == [1]
    assert list(log.take(gem_id, 2)) == [2, 3]


async def test_an_item_is_stored_with_who_wrote_it() -> None:
    docdb, _, conversations = await _conversations()
    late = ToolCall("late_1", "web_search", '{"deferred_result_for": "c1"}', output="Sunny.")

    await conversations.add(
        ObjectId(),
        Stored([]),
        [
            line("The user has called you.", "call-line"),
            _user("hello"),
            Added({"role": "assistant", "content": "Hi."}, _NOW, source="gem"),
            *stubs([late]),
        ],
        "call",
    )

    assert [d["source"] for d in _stored_items(docdb)] == [
        "call-line",
        "user",
        "gem",
        "late-result",
        "late-result",
    ]

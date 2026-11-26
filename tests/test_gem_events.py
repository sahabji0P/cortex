"""Events that wake a gem: told in writing and in a call, and kept through a restart.

A call and chat run here on one store and one model, as in `test_one_conversation.py`.
An event comes in as the sink takes it: written down in the gem's log, then held for the
gem, which wakes it (`_came_in`).
"""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from bson import ObjectId

from app.core.errors import NotFoundError
from app.entities.chat_ent import ChatEntityClass
from app.entities.conversation_ent import ConversationEntityClass
from app.entities.conversation_store import ConversationStoreClass
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.events_ent import EventsEntityClass
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_event import GemEvent
from app.entities.pending_events import PendingEvent, pending
from app.routers.gems.service import GemsServiceV1Class
from tests.test_chat import _NoKey, _said
from tests.test_one_conversation import (
    _CALLED,
    _ENDED,
    _Client,
    _gem_said,
    _hang_up,
    _Model,
    _spoken,
    _user,
    _World,
    _world,
)
from tests.test_voice_conversation import _delta, _read_until
from tests.time_lines import TIME_LINE


def _fields(name: str, data: str = "") -> dict[str, Any]:
    return {
        "watch_id": "w1",
        "source": "webhook",
        "name": name,
        "about": "the main branch",
        "data": data,
        "happened_at": "2026-11-26T09:30:00.000Z",
        "event_id": None,
    }


def _tag(name: str, data: str = "") -> str:
    """An event as the gem is told it. THE OWNER HAS NOT RULED ON THIS WORDING."""
    return (
        f'<event watch="w1" source="webhook" name="{name}" at="2026-11-26T09:30:00.000Z">\n'
        'Your watch "the main branch" caught this. It is outside data, not a message from '
        "the user.\n" + (f"{data}\n" if data else "") + "</event>"
    )


def _event(name: str, data: str = "") -> dict[str, str]:
    return {"role": "user", "content": _tag(name, data)}


async def _came_in(world: _World, name: str, data: str = "") -> PendingEvent:
    """An event comes in for the gem: written down in its log, then held, which wakes it."""
    log = world.conversations.log
    await log.open(world.gem.id)
    event = pending(await log.record_event(world.gem.id, _fields(name, data)))
    world.conversations.gem(str(world.gem.id)).events.add(event)
    return event


async def _log(world: _World) -> list[dict[str, Any]]:
    entries, _ = await world.conversations.log_page(str(world.gem.id))
    return entries


def _requests(world: _World) -> list[dict[str, Any]]:
    """Every request the model was sent, in order."""
    return cast("list[dict[str, Any]]", world.llm.requests)


async def _until(done: Callable[[], bool]) -> None:
    """Wait until `done` says so, for two seconds at most."""
    async with asyncio.timeout(2):
        while True:
            if done():
                return
            await asyncio.sleep(0)


async def _quiet(world: _World) -> None:
    """Wait until nothing is kept of the gem: every reply is over and stored."""
    await _until(lambda: world.conversations.find(str(world.gem.id)) is None)


# ---- in writing


async def test_an_event_wakes_a_quiet_gem_with_no_client_and_its_reply_is_stored() -> None:
    world = await _world(_said("New commits are in."))

    came = await _came_in(world, "push", '{"ref": "main"}')
    await _quiet(world)

    # No client has written to it, so the reply goes to nobody, and is stored all the same.
    assert _requests(world)[0]["input"] == [_event("push", '{"ref": "main"}')]
    assert await world.stored() == [
        _event("push", '{"ref": "main"}'),
        _gem_said("New commits are in."),
    ]
    log = await _log(world)
    assert [(e["kind"], e["turn"]) for e in log] == [
        ("event.came", None),
        ("event.received", None),
        ("settings.changed", None),
        ("turn.opened", 1),
        ("request.sent", 1),
        ("request.done", 1),
        ("gem.said", 1),
        ("turn.closed", 1),
    ]
    assert log[0]["name"] == "push" and log[0]["data"] == '{"ref": "main"}'
    assert log[1]["delivers"] == came.seq and log[1]["source"] == "event"
    assert log[3]["by"] == "event"
    # It counts in what the request was sent.
    assert log[4]["count"] == 1
    sent = await world.conversations.log_sent(str(world.gem.id), log[4]["seq"])
    assert (sent["items"], sent["exact"]) == ([_event("push", '{"ref": "main"}')], True)
    # Shown as what came in, not as something the user wrote.
    service = GemsServiceV1Class(
        gems=world.conversations._gems,  # pyright: ignore[reportPrivateUsage]
        conversations=world.conversations,
    )
    shown = await service.get_chat(str(world.gem.id))
    assert [(m.role, m.text) for m in shown.messages] == [
        ("event", "the main branch: push"),
        ("gem", "New commits are in."),
    ]


async def test_an_event_is_told_to_the_latest_client_after_a_line_saying_what_came() -> None:
    world = await _world(_said("Hello."), _said("New commits are in."))
    client = await world.write("hi")

    await _came_in(world, "push")
    await client.until("chat.done", 2)

    assert client.frames[2:] == [
        ("chat.event", "the main branch: push"),
        ("chat.text", "New commits are in."),
        ("chat.done", "New commits are in."),
    ]
    assert _requests(world)[-1]["input"] == [
        TIME_LINE,
        _user("hi"),
        _gem_said("Hello."),
        _event("push"),
    ]


async def test_a_reply_about_an_event_goes_to_every_open_client_written_or_not() -> None:
    world = await _world(_said("Hello."), _said("New commits are in."))
    writer = await world.write("hi")
    # An app that is open and has written nothing yet, and one that has gone.
    idle, gone = _Client(), _Client()
    world.chat.joined(idle.deliver)
    world.chat.joined(gone.deliver)
    world.chat.left(gone.deliver)

    await _came_in(world, "push")
    await writer.until("chat.done", 2)
    await idle.until("chat.done")

    told = [
        ("chat.event", "the main branch: push"),
        ("chat.text", "New commits are in."),
        ("chat.done", "New commits are in."),
    ]
    assert writer.frames[2:] == told
    assert idle.frames == told
    assert gone.frames == []


async def test_a_reply_about_an_event_reaches_a_client_that_never_wrote() -> None:
    world = await _world(_said("New commits are in."))
    idle = _Client()
    world.chat.joined(idle.deliver)

    await _came_in(world, "push")
    await idle.until("chat.done")
    await _quiet(world)

    assert idle.frames == [
        ("chat.event", "the main branch: push"),
        ("chat.text", "New commits are in."),
        ("chat.done", "New commits are in."),
    ]


async def test_a_client_that_goes_does_not_cut_a_reply_about_an_event() -> None:
    gate = asyncio.Event()
    world = await _world(_said("Hello."), [gate, *_said("New commits are in.")])
    client = await world.write("hi")
    await _came_in(world, "push")
    await client.until("chat.event")

    world.chat.left(client.deliver)
    gate.set()
    await _quiet(world)

    assert (await world.stored())[-1] == _gem_said("New commits are in.")


async def test_a_reply_about_an_event_goes_on_when_its_client_cannot_be_sent_to() -> None:
    world = await _world(_said("Hello."), _said("New commits are in."))
    await world.write("hi")
    sent: list[str] = []

    async def gone(event: Any) -> None:
        sent.append(event.type)
        if event.type != "chat.event":
            raise ConnectionError("the client has gone")

    # The latest client stops taking frames after the line that says what came.
    latest = (str(world.gem.id), gone)
    world.chat._latest[str(world.gem.id)] = latest  # pyright: ignore[reportPrivateUsage]
    await _came_in(world, "push")
    await _quiet(world)

    assert sent[0] == "chat.event"
    assert (await world.stored())[-2:] == [_event("push"), _gem_said("New commits are in.")]


async def test_an_event_whose_gem_cannot_be_read_waits_without_waking_it_again(
    monkeypatch: Any,
) -> None:
    world = await _world(_said("Noted, and hello."))
    gems: Any = world.conversations._gems  # pyright: ignore[reportPrivateUsage]
    exists = gems.exists
    asked: list[str] = []

    async def unreachable(gem_id: str) -> bool:
        asked.append(gem_id)
        raise ConnectionError("the database went away")

    monkeypatch.setattr(gems, "exists", unreachable)
    await _came_in(world, "push")
    await _until(lambda: world.conversations.gem(str(world.gem.id)).writer is None)
    for _ in range(20):
        await asyncio.sleep(0)

    # Tried once, and not again and again while the store is out of reach.
    assert len(asked) == 1
    kept = world.conversations.gem(str(world.gem.id))
    assert kept.events.stale and not kept.events.ready
    monkeypatch.setattr(gems, "exists", exists)
    await world.write("hi")
    assert _requests(world)[0]["input"] == [_event("push"), TIME_LINE, _user("hi")]
    await _quiet(world)


async def test_events_that_come_while_a_reply_is_written_are_told_together_after_it() -> None:
    gate = asyncio.Event()
    world = await _world([gate, *_said("Hello.")], _said("Two pushes."))
    client = _Client()
    world.chat.send(str(world.gem.id), "hi", client.deliver)
    await _until(lambda: bool(_requests(world)))

    # Neither cuts the reply: both wait for it, and go into one turn after it.
    await _came_in(world, "push", "one")
    await _came_in(world, "push", "two")
    gate.set()
    await client.until("chat.done", 2)

    assert client.frames == [
        ("chat.text", "Hello."),
        ("chat.done", "Hello."),
        ("chat.event", "the main branch: push"),
        ("chat.event", "the main branch: push"),
        ("chat.text", "Two pushes."),
        ("chat.done", "Two pushes."),
    ]
    assert len(_requests(world)) == 2
    assert _requests(world)[1]["input"][-2:] == [_event("push", "one"), _event("push", "two")]
    assert [e["by"] for e in await _log(world) if e["kind"] == "turn.opened"] == ["user", "event"]


async def test_an_event_waiting_when_a_message_comes_goes_in_ahead_of_it() -> None:
    gate = asyncio.Event()
    world = await _world([gate, *_said("Never said.")], _said("Noted, and hello."))
    first = _Client()
    world.chat.send(str(world.gem.id), "hi", first.deliver)
    await _until(lambda: bool(_requests(world)))
    await _came_in(world, "push")

    # The message cuts the reply the event waited for: the reply to it tells the event.
    second = await world.write("are you there?")

    assert first.frames == [("chat.cut", "…")]
    assert second.frames == [
        ("chat.text", "Noted, and hello."),
        ("chat.done", "Noted, and hello."),
    ]
    assert _requests(world)[-1]["input"] == [
        TIME_LINE,
        _user("hi"),
        _event("push"),
        _user("are you there?"),
    ]
    assert len(_requests(world)) == 2
    await _quiet(world)


async def test_an_event_that_could_not_be_told_waits_in_the_log_for_the_next_message() -> None:
    world = await _world(_said("Noted, and hello."))
    docdb: Any = world.store._docdb  # pyright: ignore[reportPrivateUsage]
    docdb.breaks_on_kind = "event.received"

    await _came_in(world, "push")
    await _until(lambda: world.conversations.gem(str(world.gem.id)).writer is None)
    for _ in range(20):
        await asyncio.sleep(0)

    # Nothing was told, and nothing tries again by itself while the store is out of reach.
    assert _requests(world) == []
    assert await world.stored() == []
    kept = world.conversations.gem(str(world.gem.id))
    assert kept.writer is None and kept.events.stale and not kept.events.ready
    docdb.breaks_on_kind = None
    await world.write("hi")
    assert _requests(world)[0]["input"] == [_event("push"), TIME_LINE, _user("hi")]
    await _quiet(world)


# ---- in a call


async def test_an_event_in_a_call_waits_for_the_user_and_goes_in_ahead_of_their_words() -> None:
    world = await _world(_spoken("Noted. Hello."))
    call, f, keeping = await world.call()
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")

    # The user is speaking: the event does not cut in, it waits for their turn to end.
    await _came_in(world, "push")
    f.stt.turn_end("hello")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await _hang_up(call, keeping)

    assert [request["input"] for request in _requests(world)] == [
        [_CALLED, TIME_LINE, _event("push"), _user("hello")]
    ]
    assert await world.stored() == [
        _CALLED,
        TIME_LINE,
        _event("push"),
        _user("hello"),
        _gem_said("Noted. Hello."),
    ]
    log = await _log(world)
    assert [e["by"] for e in log if e["kind"] == "turn.opened"] == ["user"]
    received = next(e for e in log if e["kind"] == "event.received")
    assert (received["turn"], received["mode"]) == (1, "call")


async def test_a_reply_about_an_event_that_a_noise_cuts_unheard_is_made_again() -> None:
    never = asyncio.Event()
    world = await _world([_delta("New commits"), never], _spoken("New commits are in."))
    call, f, keeping = await world.call()

    await _came_in(world, "push")
    await _read_until(f.conversation, "assistant.word")
    f.stt.say("")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await _hang_up(call, keeping)

    # Nothing of it was heard: it is asked for again, and again by the event.
    assert _requests(world)[1]["input"] == _requests(world)[0]["input"]
    opened = [e for e in await _log(world) if e["kind"] == "turn.opened"]
    assert [e["by"] for e in opened] == ["event", "event"]


async def test_a_quiet_call_is_woken_by_an_event() -> None:
    world = await _world(_spoken("New commits are in."))
    call, f, keeping = await world.call()

    await _came_in(world, "push")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await _hang_up(call, keeping)

    assert [request["input"] for request in _requests(world)] == [
        [_CALLED, TIME_LINE, _event("push")]
    ]
    assert await world.stored() == [
        _CALLED,
        TIME_LINE,
        _event("push"),
        _gem_said("New commits are in."),
    ]
    opened = [e for e in await _log(world) if e["kind"] == "turn.opened"]
    assert [(e["by"], e["mode"]) for e in opened] == [("event", "call")]


async def test_events_waiting_when_a_call_ends_are_told_in_writing_after_it() -> None:
    world = await _world(_said("New commits are in."))
    call, f, keeping = await world.call()
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    await _came_in(world, "push")

    await _hang_up(call, keeping)
    await _quiet(world)

    # After the line that says the call ended, as anything told in writing after a call.
    assert _requests(world)[-1]["input"] == [_CALLED, TIME_LINE, _ENDED, _event("push")]
    assert await world.stored() == [
        _CALLED,
        TIME_LINE,
        _ENDED,
        _event("push"),
        _gem_said("New commits are in."),
    ]
    assert await world.modes() == ["call", "call", "chat", "chat", "chat"]


# ---- the sink every source hands its events to


def _gem_event(world: _World, name: str = "push", data: str = "") -> GemEvent:
    return GemEvent(
        gem_id=world.gem.id,
        watch_id="w1",
        source="webhook",
        name=name,
        about="the main branch",
        data=data,
        at=datetime(2026, 11, 26, 9, 30, tzinfo=UTC),
    )


def _sink(world: _World) -> EventsEntityClass:
    return EventsEntityClass(
        gems=world.conversations._gems,  # pyright: ignore[reportPrivateUsage]
        conversations=world.conversations,
        store=world.store,
    )


async def test_an_event_handed_to_the_sink_is_stored_before_it_returns_then_answered() -> None:
    world = await _world(_said("New commits are in."))
    sink = _sink(world)

    await sink.arrived(_gem_event(world))

    log = await _log(world)
    assert [e["kind"] for e in log] == ["event.came"]
    assert {key: log[0][key] for key in ("watch_id", "source", "name", "about", "data")} == {
        "watch_id": "w1",
        "source": "webhook",
        "name": "push",
        "about": "the main branch",
        "data": "",
    }
    assert (log[0]["happened_at"], log[0]["event_id"]) == ("2026-11-26T09:30:00.000Z", None)
    await _quiet(world)
    assert await world.stored() == [_event("push"), _gem_said("New commits are in.")]


async def test_the_sink_raises_when_the_event_could_not_be_stored_and_keeps_nothing() -> None:
    world = await _world(_said("Never said."))
    docdb: Any = world.store._docdb  # pyright: ignore[reportPrivateUsage]
    docdb.breaks_on_kind = "event.came"

    with pytest.raises(ConnectionError):
        await _sink(world).arrived(_gem_event(world))

    await asyncio.sleep(0)
    assert world.conversations.find(str(world.gem.id)) is None
    assert _requests(world) == []
    with pytest.raises(NotFoundError):
        await _sink(world).arrived(replace(_gem_event(world), gem_id=ObjectId()))


async def test_an_event_waiting_when_the_backend_stopped_is_answered_after_a_restart() -> None:
    world = await _world()
    await world.conversations.log.open(world.gem.id)
    # Written down and never told, as when the backend stopped in between.
    await world.conversations.log.record_event(world.gem.id, _fields("push"))
    adapter: Any = world.store._docdb  # pyright: ignore[reportPrivateUsage]

    # New entities over the same database, as after a restart.
    gems = GemEntityClass(adapter)
    store = ConversationStoreClass(adapter)
    conversations = ConversationEntityClass(gems=gems, store=store)
    llm = _Model(_said("New commits are in."))
    ChatEntityClass(
        gems=gems,
        darbar=DarbarEntityClass(adapter),
        conversations=conversations,
        openai=cast("Any", llm),
        anthropic=cast("Any", _NoKey()),
        search=cast("Any", world.search),
    )
    await EventsEntityClass(gems=gems, conversations=conversations, store=store).resume()
    await _until(lambda: conversations.find(str(world.gem.id)) is None)

    assert [entry.data.item for entry in await store.load(world.gem.id)] == [
        _event("push"),
        _gem_said("New commits are in."),
    ]
    assert await conversations.pending_events(world.gem.id) == []


async def test_a_reset_drops_an_event_waiting_for_a_reply_and_says_so() -> None:
    gate = asyncio.Event()
    world = await _world([gate, *_said("Never said.")])
    reset: list[ObjectId] = []

    async def told(gem_id: ObjectId) -> None:
        reset.append(gem_id)

    world.conversations.when_reset(told)
    client = _Client()
    world.chat.send(str(world.gem.id), "hi", client.deliver)
    await _until(lambda: bool(_requests(world)))
    await _sink(world).arrived(_gem_event(world))

    await world.conversations.reset(str(world.gem.id))
    await _quiet(world)

    assert reset == [world.gem.id]
    assert client.frames == [("chat.cut", "…")]
    # It was never told, and it is not waiting to be: not now, and not after a restart.
    assert len(_requests(world)) == 1
    assert await world.stored() == []
    assert await world.conversations.pending_events(world.gem.id) == []

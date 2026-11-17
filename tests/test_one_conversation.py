"""A gem's one conversation, held by call and in writing.

A call and chat run here on one store, one model and one set of late results, with the
speech services and time faked as in `test_voice_conversation.py`. The model is that
file's fake: every request it is sent, whether from a call or from chat, has to start
with the whole of the request before it (`_assert_only_grew`), with only the user's open
turn allowed to gain a line.
"""

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.entities.call_ent import CallEntityClass, CallFault, OpenCall
from app.entities.chat_ent import ChatEntityClass, ChatEvent
from app.entities.conversation_ent import ConversationEntityClass
from app.entities.conversation_store import ConversationItem, ConversationStoreClass
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.history import Mode
from app.entities.late_results import LateResults
from app.entities.model import Item
from app.entities.voice_ent import VoiceConversation
from tests.test_chat import _NOW, _docdb, _NoKey, _said
from tests.test_voice_conversation import (
    _DELAY,
    _LLM,
    _STT,
    _TTS,
    _Clock,
    _completed,
    _delta,
    _Fixture,
    _let_audio_out,
    _message,
    _read_until,
    _Search,
    _search_call,
    _starts,
)

# The lines that say where the conversation changes mode, written out in full.
_CALLED = {
    "role": "developer",
    "content": "The user has called you. From here on the conversation is spoken.",
}
_ENDED = {
    "role": "developer",
    "content": "The call has ended. From here on the conversation is written.",
}

# What a call lost to a restart is closed with. THE OWNER HAS NOT RULED ON THIS WORDING.
_LOST = (
    "This call was lost before it finished, and its result will not arrive. Make it again "
    "if it is still needed."
)


def _user(text: str) -> dict[str, str]:
    return {"role": "user", "content": text}


def _gem_said(text: str) -> dict[str, str]:
    return {"role": "assistant", "content": text}


def _spoken(text: str) -> list[Any]:
    return [_delta(text), _completed(_message(text))]


class _Model(_LLM):
    """The model, for a call and for chat alike."""

    configured = True


class _Voice:
    """The voice entity, with each call's speech faked and every call on one clock."""

    def __init__(self, llm: _Model, search: _Search) -> None:
        self.llm = llm
        self.search = search
        self.clock = _Clock()
        self.calls: list[_Fixture] = []

    async def connect(self, gem: GemConfig, *, speaks_first: bool | None = None) -> "_Line":
        return _Line(self, gem, gem.speaks_first if speaks_first is None else speaks_first)


class _Line:
    def __init__(self, voice: _Voice, gem: GemConfig, speaks_first: bool) -> None:
        self._voice = voice
        self._gem = gem
        self._speaks_first = speaks_first

    def open(
        self,
        *,
        darbar: str | None = None,
        history: Sequence[Item] = (),
        last_mode: Mode = "chat",
        late: LateResults | None = None,
    ) -> VoiceConversation:
        voice = self._voice
        stt, tts = _STT(), _TTS()
        conversation = VoiceConversation(
            stt=cast("Any", stt),
            tts=cast("Any", tts),
            llm=voice.llm,
            search=cast("Any", voice.search),
            gem=self._gem,
            darbar=darbar,
            speaks_first=self._speaks_first,
            history=history,
            last_mode=last_mode,
            late=late,
            clock=voice.clock,
        )
        voice.calls.append(
            _Fixture(conversation=conversation, stt=stt, tts=tts, llm=voice.llm, clock=voice.clock)
        )
        return conversation

    async def close(self) -> None:
        pass


class _Client:
    """A client of chat: what it is sent, as (type, text)."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, str]] = []
        self._came = asyncio.Event()

    async def deliver(self, event: ChatEvent) -> None:
        self.frames.append((event.type, event.text))
        self._came.set()

    async def until(self, type_: str, count: int = 1) -> None:
        async with asyncio.timeout(2):
            while sum(kind == type_ for kind, _ in self.frames) < count:
                self._came.clear()
                await self._came.wait()


class _World(SimpleNamespace):
    store: ConversationStoreClass
    llm: _Model
    search: _Search
    conversations: ConversationEntityClass
    chat: ChatEntityClass
    voice: _Voice
    calls: CallEntityClass
    gem: GemConfig

    async def stored(self) -> list[Any]:
        return [entry.item for entry in await self.store.load(self.gem.id)]

    async def modes(self) -> list[str]:
        return [entry.mode for entry in await self.store.load(self.gem.id)]

    async def write(self, text: str, client: _Client | None = None) -> _Client:
        """Write to the gem, and wait until the reply is stored."""
        client = client or _Client()
        turn = self.chat.send(str(self.gem.id), text, client.deliver)
        async with asyncio.timeout(2):
            await turn.over.wait()
        return client

    async def call(self) -> tuple[OpenCall, _Fixture, asyncio.Task[None]]:
        """Open a call with the gem, and store what it settles as it goes, as the link does."""
        call = await self.calls.open(str(self.gem.id))
        return call, self.voice.calls[-1], asyncio.create_task(call.keep())


async def _hang_up(call: OpenCall, keeping: asyncio.Task[None]) -> None:
    """End a call as the link does: stop storing as it goes, then close it."""
    keeping.cancel()
    await asyncio.gather(keeping, return_exceptions=True)
    await call.close()


async def _world(*rounds: list[Any], search: _Search | None = None, **gem: Any) -> _World:
    _, adapter = _docdb()
    gems, store = GemEntityClass(adapter), ConversationStoreClass(adapter)
    conversations = ConversationEntityClass(gems=gems, store=store)
    llm = _Model(*rounds)
    search = search or _Search()
    chat = ChatEntityClass(
        gems=gems,
        darbar=DarbarEntityClass(adapter),
        conversations=conversations,
        openai=cast("Any", llm),
        anthropic=cast("Any", _NoKey()),
        search=cast("Any", search),
    )
    voice = _Voice(llm, search)
    calls = CallEntityClass(
        gems=gems,
        darbar=DarbarEntityClass(adapter),
        voice=cast("Any", voice),
        conversations=conversations,
    )
    made = await gems.create(**{"name": "Gem One", "model": "6 Luna", **gem})
    return _World(
        store=store,
        llm=llm,
        search=search,
        conversations=conversations,
        chat=chat,
        voice=voice,
        calls=calls,
        gem=made,
    )


async def _said_and_heard(f: _Fixture, text: str) -> None:
    """The user says `text`, and hears the whole reply to it."""
    f.stt.say(text)
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)


# ---- one conversation, by call and in writing


async def test_a_call_then_chat_then_a_call_are_one_conversation() -> None:
    world = await _world(
        _spoken("Hi there."),
        _spoken("Not much."),
        _said("Sunny."),
        _spoken("Bye."),
    )

    call, f, keeping = await world.call()
    await _said_and_heard(f, "hello")
    await _said_and_heard(f, "what's new")
    await _hang_up(call, keeping)
    after_the_call = await world.stored()
    client = await world.write("and the weather?")
    after_the_chat = await world.stored()
    call, f, keeping = await world.call()
    await _said_and_heard(f, "thanks")
    await _hang_up(call, keeping)

    # Every request started with the whole of the one before it, from the call to chat
    # and back: the model fake checks that on each.
    assert len(world.llm.requests) == 4
    assert after_the_call == [
        _CALLED,
        _user("hello"),
        _gem_said("Hi there."),
        _user("what's new"),
        _gem_said("Not much."),
    ]
    assert client.frames == [("chat.text", "Sunny."), ("chat.done", "Sunny.")]
    assert world.llm.requests[2]["input"] == [
        *after_the_call,
        _ENDED,
        _user("and the weather?"),
    ]
    assert after_the_chat == [
        *after_the_call,
        _ENDED,
        _user("and the weather?"),
        _gem_said("Sunny."),
    ]
    # The second call starts with everything the first call and the chat stored.
    assert world.llm.requests[3]["input"] == [*after_the_chat, _CALLED, _user("thanks")]
    assert await world.stored() == [*after_the_chat, _CALLED, _user("thanks"), _gem_said("Bye.")]
    assert await world.modes() == ["call"] * 5 + ["chat"] * 3 + ["call"] * 3
    # The conversation is shown whole, without our lines.
    shown = await world.conversations.messages(str(world.gem.id))
    assert [(message.role, message.text) for message in shown] == [
        ("user", "hello"),
        ("gem", "Hi there."),
        ("user", "what's new"),
        ("gem", "Not much."),
        ("user", "and the weather?"),
        ("gem", "Sunny."),
        ("user", "thanks"),
        ("gem", "Bye."),
    ]
    assert world.conversations._kept == {}


async def test_a_reply_cut_in_a_call_is_stored_as_heard_and_sent_so_to_chat() -> None:
    never = asyncio.Event()
    world = await _world(
        [_delta("Once upon a time there was a dragon."), never],
        _spoken("Sure."),
        _said("Hello."),
    )

    call, f, keeping = await world.call()
    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.clock.advance(_DELAY + 3.5)  # three and a half words in
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    f.stt.turn_end("never mind")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await _hang_up(call, keeping)
    await world.write("hi")

    stored = [
        _CALLED,
        _user("tell me a story"),
        _gem_said("Once upon a…"),
        _user("never mind"),
        _gem_said("Sure."),
    ]
    assert world.llm.requests[-1]["input"] == [*stored, _ENDED, _user("hi")]
    assert await world.stored() == [*stored, _ENDED, _user("hi"), _gem_said("Hello.")]


async def test_a_reply_cut_by_hanging_up_is_stored_as_heard() -> None:
    never = asyncio.Event()
    world = await _world([_delta("Once upon a time there was a dragon."), never])

    call, f, keeping = await world.call()
    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.clock.advance(_DELAY + 2.5)
    await _hang_up(call, keeping)

    assert await world.stored() == [_CALLED, _user("tell me a story"), _gem_said("Once upon…")]


@pytest.mark.parametrize("how", ["silence", "greeting unheard", "reply unheard"])
async def test_a_call_in_which_nothing_was_said_stores_nothing(how: str) -> None:
    never = asyncio.Event()
    unheard = [] if how == "silence" else [[_delta("Hi, how can I help?"), never]]
    world = await _world(*unheard, _said("Hello."), speaks_first=how == "greeting unheard")

    call, f, keeping = await world.call()
    if how == "reply unheard":
        f.stt.say("hello")
    if how != "silence":
        await _read_until(f.conversation, "assistant.delta")
    await _hang_up(call, keeping)

    # Our lines wait for something to follow them, and the user's open turn, with no
    # reply heard, is not stored.
    assert await world.stored() == []
    assert world.conversations._kept == {}
    # Written to next, the conversation is as it was: written, and empty. What the call
    # sent the model and never settled is not in that request: the one place a request
    # does not start with the whole of the one before it.
    sent_in_the_call = len(world.llm.requests)
    world.llm.requests.clear()
    await world.write("hi")
    assert sent_in_the_call == (0 if how == "silence" else 1)
    assert world.llm.requests[-1]["input"] == [_user("hi")]


# ---- tool calls that outlive a call, or a written reply


async def test_a_search_running_when_a_call_ends_is_stored_and_its_result_told_in_chat() -> None:
    finish = asyncio.Event()
    search_call = _search_call("weather in Pune")
    world = await _world(
        _said("Hello."),
        [_starts(search_call), _completed(search_call)],
        _said("It is sunny in Pune."),
        search=_Search(gate=finish),
    )
    client = await world.write("hi")

    call, f, keeping = await world.call()
    f.stt.say("weather in Pune?")
    await _read_until(f.conversation, "tool.call")
    await _hang_up(call, keeping)

    after_the_call = await world.stored()
    assert after_the_call[2:5] == [
        _CALLED,
        _user("weather in Pune?"),
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "web_search",
            "arguments": '{"query": "weather in Pune"}',
        },
    ]
    placeholder = (await world.store.load(world.gem.id))[-1]
    assert placeholder.item["type"] == "function_call_output"
    assert placeholder.mark == "placeholder"
    assert placeholder.mode == "call"
    # The search carries on after the call.
    assert world.conversations._kept[str(world.gem.id)].late.holds("call_1")

    finish.set()
    await client.until("chat.done", 2)

    # Told in writing, to the latest client, after the line that says the call ended.
    assert client.frames[2:] == [
        ("chat.result", "weather in Pune"),
        ("chat.text", "It is sunny in Pune."),
        ("chat.done", "It is sunny in Pune."),
    ]
    stub, result = world.llm.requests[-1]["input"][len(after_the_call) + 1 :]
    assert world.llm.requests[-1]["input"][: len(after_the_call) + 1] == [*after_the_call, _ENDED]
    assert stub["arguments"] == '{"deferred_result_for": "call_1"}'
    assert "Norris won in Las Vegas." in result["output"]
    assert await world.stored() == [
        *after_the_call,
        _ENDED,
        stub,
        result,
        _gem_said("It is sunny in Pune."),
    ]


async def test_a_result_after_a_call_with_nobody_to_tell_is_stored_after_one_line() -> None:
    finish = asyncio.Event()
    search_call = _search_call("weather in Pune")
    world = await _world(
        [_starts(search_call), _completed(search_call)],
        _said("It is sunny."),
        search=_Search(gate=finish),
    )

    call, f, keeping = await world.call()
    f.stt.say("weather in Pune?")
    await _read_until(f.conversation, "tool.call")
    await _hang_up(call, keeping)
    after_the_call = await world.stored()
    finish.set()
    async with asyncio.timeout(2):
        while True:
            if len(await world.stored()) > len(after_the_call):
                break
            await asyncio.sleep(0)
    await world.write("and?")

    # No client had written to the gem, so the result is only stored, in writing: the
    # line that says the call ended goes before it, once, and the next message follows.
    sent = world.llm.requests[-1]["input"]
    ended, stub, result, message = sent[len(after_the_call) :]
    assert sent[: len(after_the_call)] == after_the_call
    assert ended == _ENDED
    assert sent.count(_ENDED) == 1
    assert stub["arguments"] == '{"deferred_result_for": "call_1"}'
    assert "Norris won in Las Vegas." in result["output"]
    assert message == _user("and?")
    assert (await world.modes())[len(after_the_call) :] == ["chat"] * 5


async def test_a_search_left_running_in_writing_is_told_in_the_call_that_cuts_its_reply() -> None:
    finish = asyncio.Event()
    search_call = _search_call("weather in Pune")
    world = await _world(
        [_delta("Let me look."), _starts(search_call), _completed(search_call)],
        _spoken("It is sunny in Pune."),
        search=_Search(gate=finish),
    )
    client = _Client()
    world.chat.send(str(world.gem.id), "weather?", client.deliver)
    await client.until("chat.tool")

    # A call cuts the reply being written, as a new message does, and opens once it is
    # stored.
    call, f, keeping = await world.call()
    before_the_call = await world.stored()
    finish.set()
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await _hang_up(call, keeping)

    assert client.frames == [
        ("chat.text", "Let me look."),
        ("chat.tool", "weather in Pune"),
        ("chat.cut", "Let me look."),
    ]
    assert [item.get("role") or item["type"] for item in before_the_call] == [
        "user",
        "assistant",
        "function_call",
        "function_call_output",
    ]
    # The result is told in the call, with no user turn before it, and not in writing.
    stub, result = world.llm.requests[-1]["input"][len(before_the_call) + 1 :]
    assert world.llm.requests[-1]["input"][: len(before_the_call) + 1] == [
        *before_the_call,
        _CALLED,
    ]
    assert stub["arguments"] == '{"deferred_result_for": "call_1"}'
    assert "Norris won in Las Vegas." in result["output"]
    assert await world.stored() == [
        *before_the_call,
        _CALLED,
        stub,
        result,
        _gem_said("It is sunny in Pune."),
    ]
    assert len(client.frames) == 3


async def test_a_search_lost_to_a_restart_is_closed_before_the_call_s_first_reply() -> None:
    world = await _world(_spoken("Hello."))
    search_call: Item = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "web_search",
        "arguments": '{"query": "weather in Pune"}',
    }
    placeholder: Item = {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "Still running in the background.",
    }
    await world.store.add(
        [
            ConversationItem(gem_id=world.gem.id, place=place, at=_NOW, item=item, mark=mark)
            for place, (item, mark) in enumerate(
                [(_user("weather?"), None), (search_call, None), (placeholder, "placeholder")]
            )
        ]
    )

    call, f, keeping = await world.call()
    closed = await world.stored()
    await _said_and_heard(f, "hello")
    await _hang_up(call, keeping)

    # Closed as the call opened, before anything was said in it.
    stub, lost = closed[3:]
    assert stub["arguments"] == '{"deferred_result_for": "call_1"}'
    assert lost == {"type": "function_call_output", "call_id": stub["call_id"], "output": _LOST}
    assert world.llm.requests[0]["input"] == [*closed, _CALLED, _user("hello")]
    # Closing it changed nothing about how the user was talking.
    assert (await world.modes())[:5] == ["chat"] * 5


async def test_a_call_straight_after_a_call_opens_with_its_own_line() -> None:
    # Without it the gem could not tell two calls, perhaps hours apart, from one.
    world = await _world(_spoken("Hi there."), _spoken("Hello again."))

    call, f, keeping = await world.call()
    await _said_and_heard(f, "hello")
    await _hang_up(call, keeping)
    first = await world.stored()
    call, f, keeping = await world.call()
    await _said_and_heard(f, "it's me again")
    await _hang_up(call, keeping)

    assert first == [_CALLED, _user("hello"), _gem_said("Hi there.")]
    assert world.llm.requests[-1]["input"] == [*first, _CALLED, _user("it's me again")]
    assert await world.stored() == [
        *first,
        _CALLED,
        _user("it's me again"),
        _gem_said("Hello again."),
    ]


# ---- one writer at a time


async def test_a_written_turn_left_open_is_closed_before_a_call() -> None:
    pause = asyncio.Event()
    world = await _world([pause, _delta("Never said.")], _spoken("Hello."))
    client = _Client()
    world.chat.send(str(world.gem.id), "are you there?", client.deliver)
    async with asyncio.timeout(2):
        while True:
            if world.llm.requests:
                break
            await asyncio.sleep(0)

    call, f, keeping = await world.call()
    await _said_and_heard(f, "hello")
    await _hang_up(call, keeping)

    # The message was sent to the model, so the call follows it rather than drop it.
    assert client.frames == [("chat.cut", "…")]
    assert await world.stored() == [
        _user("are you there?"),
        _CALLED,
        _user("hello"),
        _gem_said("Hello."),
    ]


async def test_a_written_turn_left_open_stays_open_when_the_call_cannot_store_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The message was sent to the model. If the call that was to follow it does not
    # open, it must still be there for the next message to join, or the next request
    # would not start with the one before it.
    pause = asyncio.Event()
    world = await _world([pause, _delta("Never said.")], _said("Here I am."))
    client = _Client()
    world.chat.send(str(world.gem.id), "are you there?", client.deliver)
    async with asyncio.timeout(2):
        while True:
            if world.llm.requests:
                break
            await asyncio.sleep(0)

    async def out_of_reach(items: Sequence[ConversationItem]) -> None:
        raise ConnectionError("the database is out of reach")

    monkeypatch.setattr(world.store, "add", out_of_reach)
    with pytest.raises(CallFault, match="could not be stored"):
        await world.calls.open(str(world.gem.id))
    monkeypatch.undo()  # the database is back

    again = await world.write("hello?")

    assert client.frames == [("chat.cut", "…")]
    assert again.frames[-1] == ("chat.done", "Here I am.")
    assert world.llm.requests[-1]["input"] == [_user("are you there?\nhello?")]
    assert await world.stored() == [_user("are you there?\nhello?"), _gem_said("Here I am.")]

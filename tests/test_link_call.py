"""A call over the standing socket, with the voice conversation faked.

What a call does is tested in `test_voice_conversation.py`. These tests are about how it
travels: the frames, their order, the socket staying open, and above all that every way
a call can end closes its conversation, once. A conversation left open holds speech
connections open on the owner's paid account.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, cast

import pytest
from bson import ObjectId

from app.entities.call_ent import CallEntityClass
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GEM_CONFIG_VERSION
from app.entities.voice_ent import VoiceEvent
from app.routers.link.service import LinkServiceV1Class
from tests.test_chat import _Chat, _chat, _said
from tests.test_voice_conversation import _completed, _delta

if TYPE_CHECKING:
    from fastapi import WebSocket

_WAIT = 2.0


class _Conversation:
    """A voice conversation: records what it is fed, and says what the test puts out."""

    def __init__(self) -> None:
        # What `outputs()` yields, in order. None ends it; an exception is raised.
        self.out: asyncio.Queue[VoiceEvent | bytes | Exception | None] = asyncio.Queue()
        self.audio: list[bytes] = []
        self.delays: list[float] = []
        self.audio_fails: Exception | None = None
        self.closed = 0

    async def send_audio(self, pcm: bytes) -> None:
        if self.audio_fails is not None:
            raise self.audio_fails
        self.audio.append(pcm)

    def set_output_delay(self, seconds: float) -> None:
        self.delays.append(seconds)

    async def outputs(self) -> AsyncIterator[VoiceEvent | bytes]:
        while (item := await self.out.get()) is not None:
            if isinstance(item, Exception):
                raise item
            yield item

    async def close(self) -> None:
        self.closed += 1


class _Voice:
    """The voice entity. Records what each conversation was opened with.

    With `gate`, opening waits until it is set. With `finishes_anyway`, an opening that
    is cancelled returns its conversation all the same, as one does that had finished in
    the same moment the cancel came.
    """

    def __init__(self) -> None:
        self.asked: list[tuple[object, object, object, object]] = []
        self.made: list[_Conversation] = []
        self.gate: asyncio.Event | None = None
        self.finishes_anyway = False
        self.fails: Exception | None = None

    async def open_conversation(
        self, model=None, *, speaks_first=None, gem=None, darbar=None
    ) -> _Conversation:
        self.asked.append((model, speaks_first, gem, darbar))
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            # Swallowed on purpose: this fakes an opening that finished as it was cancelled.
            if not self.finishes_anyway:
                raise
        if self.fails is not None:
            raise self.fails
        conversation = _Conversation()
        self.made.append(conversation)
        return conversation


class _Socket:
    """A client of the standing socket. Binary frames it is sent are kept as bytes.

    A frame whose type is in `stalls` (or "bytes" for audio) is never taken, as by a
    client that has stopped reading. `close()` does not wake `receive()`.
    """

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.frames: list[dict[str, Any] | bytes] = []
        self.stalls: set[str] = set()
        self.gone = False
        self._came = asyncio.Event()

    async def accept(self) -> None:
        pass

    async def receive(self) -> dict[str, Any]:
        return await self._inbox.get()

    async def _take(self, frame: dict[str, Any] | bytes) -> None:
        if self.gone:
            raise RuntimeError("the socket is closed")
        if (frame["type"] if isinstance(frame, dict) else "bytes") in self.stalls:
            await asyncio.Event().wait()
        self.frames.append(frame)
        self._came.set()

    async def send_text(self, text: str) -> None:
        await self._take(json.loads(text))

    async def send_bytes(self, data: bytes) -> None:
        await self._take(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.gone = True

    def send(self, **frame: Any) -> None:
        self._inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(frame)})

    def mic(self, pcm: bytes) -> None:
        self._inbox.put_nowait({"type": "websocket.receive", "bytes": pcm})

    def hang_up(self) -> None:
        self.gone = True
        self._inbox.put_nowait({"type": "websocket.disconnect"})

    def types(self) -> list[str]:
        """What came, in order: each frame's type, or "bytes" for audio."""
        return [f["type"] if isinstance(f, dict) else "bytes" for f in self.frames]

    def frame(self, type_: str) -> dict[str, Any]:
        """The first frame of this type."""
        return next(f for f in self.frames if isinstance(f, dict) and f["type"] == type_)

    async def until(self, type_: str, count: int = 1) -> None:
        """Wait until `count` frames of this type have come."""
        async with asyncio.timeout(_WAIT):
            while self.types().count(type_) < count:
                self._came.clear()
                await self._came.wait()


async def _eventually(holds: Callable[[], bool]) -> None:
    """Let the other tasks run until `holds()` is true, failing after a while.

    The fakes record what they were handed as plain values, with nothing to wait on.
    """
    async with asyncio.timeout(_WAIT):
        while True:
            if holds():
                return
            await asyncio.sleep(0)


async def _settle() -> None:
    """Let every task that can run, run."""
    for _ in range(20):
        await asyncio.sleep(0)


class _Setup:
    def __init__(self, chat: _Chat, voice: _Voice) -> None:
        self.chat = chat
        self.voice = voice
        self.gem = chat.gem
        calls = CallEntityClass(
            gems=chat.gems,
            darbar=DarbarEntityClass(cast("Any", chat.docdb)),
            voice=cast("Any", voice),
        )
        self.service = LinkServiceV1Class(chat.chat, calls)


async def _setup(*rounds: list[Any]) -> _Setup:
    return _Setup(await _chat(*rounds), _Voice())


async def _open(setup: _Setup) -> tuple[_Socket, asyncio.Task[None]]:
    socket = _Socket()
    linked = asyncio.create_task(setup.service.link(cast("WebSocket", socket)))
    await socket.until("link.ready")
    return socket, linked


async def _close(socket: _Socket, linked: asyncio.Task[None]) -> None:
    socket.hang_up()
    async with asyncio.timeout(_WAIT):
        await linked


async def _in_call(setup: _Setup, **start: Any) -> tuple[_Socket, asyncio.Task[None]]:
    """A link with a call open on it."""
    socket, linked = await _open(setup)
    socket.send(type="call.start", **start)
    await socket.until("call.ready")
    return socket, linked


def _event(type_: str, text: str = "") -> VoiceEvent:
    return VoiceEvent.model_validate({"type": type_, "text": text})


# ---- a call, start to end


async def test_a_call_carries_audio_and_events_both_ways_and_ends_on_call_end() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup, gem=str(setup.gem.id))
    [conversation] = setup.voice.made

    socket.mic(b"\x01\x02")
    socket.mic(b"\x03\x04")
    conversation.out.put_nowait(_event("user.final", "hello"))
    conversation.out.put_nowait(b"\x05\x06")
    conversation.out.put_nowait(_event("assistant.delta", "Hi."))
    conversation.out.put_nowait(b"\x07\x08")
    conversation.out.put_nowait(_event("assistant.done", "Hi."))
    await socket.until("call.assistant.done")
    await _eventually(lambda: len(conversation.audio) == 2)
    socket.send(type="call.end")
    await socket.until("call.ended")

    assert conversation.audio == [b"\x01\x02", b"\x03\x04"]
    assert socket.frames == [
        {"type": "link.ready", "version": 2},
        {"type": "call.ready"},
        {"type": "call.user.final", "text": "hello"},
        b"\x05\x06",
        {"type": "call.assistant.delta", "text": "Hi."},
        b"\x07\x08",
        {"type": "call.assistant.done", "text": "Hi."},
        {"type": "call.ended", "text": ""},
    ]
    assert conversation.closed == 1
    assert not linked.done()
    await _close(socket, linked)
    assert conversation.closed == 1


@pytest.mark.parametrize(
    "type_",
    [
        "user.partial",
        "user.final",
        "assistant.delta",
        "assistant.done",
        "tool.call",
        "interrupt",
        "error",
    ],
)
async def test_every_event_of_a_call_goes_out_with_call_put_before_its_type(type_: str) -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)

    setup.voice.made[0].out.put_nowait(_event(type_, "some words"))
    await socket.until(f"call.{type_}")

    assert socket.frame(f"call.{type_}") == {"type": f"call.{type_}", "text": "some words"}
    await _close(socket, linked)


async def test_mic_audio_sent_while_the_call_opens_is_kept_and_fed_in_order() -> None:
    setup = await _setup()
    setup.voice.gate = asyncio.Event()
    socket, linked = await _open(setup)

    socket.send(type="call.start")
    for n in range(5):
        socket.mic(bytes([n]))
    await _eventually(lambda: len(setup.voice.asked) == 1)
    await _settle()
    setup.voice.gate.set()
    await socket.until("call.ready")
    socket.mic(bytes([5]))
    [conversation] = setup.voice.made
    await _eventually(lambda: len(conversation.audio) == 6)

    assert conversation.audio == [bytes([n]) for n in range(6)]
    await _close(socket, linked)


async def test_mic_audio_with_no_call_is_dropped_without_an_answer() -> None:
    setup = await _setup()
    socket, linked = await _open(setup)

    socket.mic(b"\x00\x01")
    # A frame that is answered, to know the one before it was read.
    socket.send(type="nothing")
    await socket.until("error")

    assert socket.types() == ["link.ready", "error"]
    assert setup.voice.asked == []
    await _close(socket, linked)


async def test_mic_audio_after_the_call_ended_is_dropped() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    socket.send(type="call.end")
    await socket.until("call.ended")

    socket.mic(b"\x00\x01")
    socket.send(type="nothing")
    await socket.until("error")

    assert conversation.audio == []
    assert socket.types() == ["link.ready", "call.ready", "call.ended", "error"]
    await _close(socket, linked)


async def test_the_playback_delay_reaches_the_call_even_when_it_is_sent_while_it_opens() -> None:
    setup = await _setup()
    setup.voice.gate = asyncio.Event()
    socket, linked = await _open(setup)

    socket.send(type="call.start")
    socket.send(type="call.playback", delay=0.25)
    await _eventually(lambda: len(setup.voice.asked) == 1)
    await _settle()
    setup.voice.gate.set()
    await socket.until("call.ready")
    socket.send(type="call.playback", delay=0.04)
    [conversation] = setup.voice.made
    await _eventually(lambda: len(conversation.delays) == 2)

    assert conversation.delays == [0.25, 0.04]
    await _close(socket, linked)


@pytest.mark.parametrize(
    ("start", "opened"),
    [
        # Without a gem: the model and who speaks first as asked, or left to the defaults.
        ({}, (None, None)),
        ({"model": "Haiku 5.5"}, ("Haiku 5.5", None)),
        ({"first": "user"}, (None, False)),
        ({"first": "assistant"}, (None, True)),
        ({"model": "6.1 Sol", "first": "assistant"}, ("6.1 Sol", True)),
        # Sent as null is the same as left out.
        ({"model": None, "first": None}, (None, None)),
    ],
)
async def test_a_call_without_a_gem_opens_as_asked(
    start: dict[str, Any], opened: tuple[object, object]
) -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup, **start)

    assert setup.voice.asked == [(*opened, None, None)]
    await _close(socket, linked)


@pytest.mark.parametrize(
    ("start", "opened"),
    [
        # Left out, each is the gem's to say. Sent, each wins for this call.
        ({}, (None, None)),
        ({"model": "6.1 Terra"}, ("6.1 Terra", None)),
        ({"first": "user"}, (None, False)),
        ({"first": "assistant"}, (None, True)),
        ({"model": "Haiku 5.5", "first": "user"}, ("Haiku 5.5", False)),
    ],
)
async def test_a_call_with_a_gem_opens_with_its_config_its_darbar_text_and_what_is_asked(
    start: dict[str, Any], opened: tuple[object, object]
) -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup, gem=str(setup.gem.id), **start)

    [(model, first, gem, darbar)] = setup.voice.asked
    assert (model, first) == opened
    assert cast("Any", gem).id == setup.gem.id
    assert darbar is not None and darbar.startswith("The user keeps a few assistants")
    await _close(socket, linked)


# ---- a call that does not open


@pytest.mark.parametrize(
    "start",
    [
        {"model": "GPT-2"},
        {"first": "nobody"},
        {"model": 5},
        {"gem": 5},
    ],
)
async def test_a_model_or_setting_that_is_not_offered_is_refused(start: dict[str, Any]) -> None:
    setup = await _setup()
    socket, linked = await _open(setup)

    socket.send(type="call.start", **start)
    await socket.until("call.refused")

    assert socket.frame("call.refused") == {
        "type": "call.refused",
        "text": "unknown model or setting",
    }
    assert setup.voice.asked == []
    assert not linked.done()
    await _close(socket, linked)


@pytest.mark.parametrize("gem_id", [str(ObjectId()), "not-an-id", ""])
async def test_a_gem_that_is_not_there_is_refused(gem_id: str) -> None:
    setup = await _setup()
    socket, linked = await _open(setup)

    socket.send(type="call.start", gem=gem_id)
    await socket.until("call.refused")

    assert socket.frame("call.refused")["text"] == "unknown gem"
    assert setup.voice.asked == []
    await _close(socket, linked)


async def test_a_model_whose_provider_has_no_key_is_refused_with_the_reason() -> None:
    setup = await _setup()
    setup.voice.fails = LookupError("no Anthropic key is set, so Haiku 5.5 cannot be used")
    socket, linked = await _open(setup)

    socket.send(type="call.start", model="Haiku 5.5")
    await socket.until("call.refused")

    assert socket.frame("call.refused")["text"] == (
        "no Anthropic key is set, so Haiku 5.5 cannot be used"
    )
    assert socket.types() == ["link.ready", "call.refused"]
    await _close(socket, linked)


async def test_a_gem_whose_config_cannot_be_read_fails_the_call() -> None:
    setup = await _setup()
    setup.chat.docdb.collections["Gems"][0]["version"] = GEM_CONFIG_VERSION + 1
    socket, linked = await _open(setup)

    socket.send(type="call.start", gem=str(setup.gem.id))
    await socket.until("call.failed")

    assert socket.frame("call.failed")["text"] == "the gem's config cannot be read"
    assert setup.voice.asked == []
    assert not linked.done()
    await _close(socket, linked)


async def test_speech_services_that_cannot_be_reached_fail_the_call() -> None:
    setup = await _setup()
    setup.voice.fails = ConnectionError("no route to the speech service")
    socket, linked = await _open(setup)

    socket.send(type="call.start")
    await socket.until("call.failed")

    assert socket.frame("call.failed")["text"] == "speech services unavailable"
    assert socket.types() == ["link.ready", "call.failed"]
    await _close(socket, linked)


async def test_after_a_call_that_did_not_open_another_can_be_started() -> None:
    setup = await _setup()
    socket, linked = await _open(setup)

    socket.send(type="call.start", gem="not-an-id")
    await socket.until("call.refused")
    socket.send(type="call.start")
    await socket.until("call.ready")

    assert socket.types() == ["link.ready", "call.refused", "call.ready"]
    await _close(socket, linked)


# ---- one call at a time


@pytest.mark.parametrize("opening", [False, True])
async def test_a_call_start_while_a_call_is_open_or_opening_gets_error_and_the_call_carries_on(
    opening: bool,
) -> None:
    setup = await _setup()
    socket, linked = await _open(setup)
    if opening:
        setup.voice.gate = asyncio.Event()

    socket.send(type="call.start")
    await _eventually(lambda: len(setup.voice.asked) == 1)
    socket.send(type="call.start", model="Haiku 5.5")
    await socket.until("error")
    if setup.voice.gate is not None:
        setup.voice.gate.set()
    await socket.until("call.ready")
    socket.mic(b"\x01")
    [conversation] = setup.voice.made
    await _eventually(lambda: conversation.audio == [b"\x01"])

    assert setup.voice.asked == [(None, None, None, None)]
    assert socket.frame("error")["text"] == "a call is already open"
    assert conversation.closed == 0
    await _close(socket, linked)


async def test_call_end_and_call_playback_with_no_call_are_ignored() -> None:
    setup = await _setup()
    socket, linked = await _open(setup)

    socket.send(type="call.end")
    socket.send(type="call.playback", delay=0.1)
    socket.send(type="nothing")
    await socket.until("error")

    assert socket.types() == ["link.ready", "error"]
    await _close(socket, linked)


async def test_a_playback_frame_that_cannot_be_read_gets_error() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)

    socket.send(type="call.playback", delay=-1)
    await socket.until("error")

    assert setup.voice.made[0].delays == []
    assert setup.voice.made[0].closed == 0
    await _close(socket, linked)


async def test_a_call_started_right_after_call_end_follows_the_first_call_s_last_frame() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    first = setup.voice.made[0]

    socket.send(type="call.end")
    socket.send(type="call.start", model="Haiku 5.5")
    await socket.until("call.ready", 2)

    assert socket.types() == ["link.ready", "call.ready", "call.ended", "call.ready"]
    assert first.closed == 1
    assert setup.voice.asked[1][0] == "Haiku 5.5"
    await _close(socket, linked)
    assert first.closed == 1
    assert setup.voice.made[1].closed == 1


# ---- every way out closes the conversation, once


async def test_the_link_dropping_ends_its_call() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)

    await _close(socket, linked)

    assert setup.voice.made[0].closed == 1


async def test_a_call_whose_conversation_ends_by_itself_gets_call_ended() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made

    conversation.out.put_nowait(None)
    await socket.until("call.ended")

    assert conversation.closed == 1
    assert not linked.done()
    # Nothing more is sent for the call, and another can be started.
    socket.send(type="call.start")
    await socket.until("call.ready", 2)
    assert socket.types() == ["link.ready", "call.ready", "call.ended", "call.ready"]
    await _close(socket, linked)
    assert conversation.closed == 1


async def test_a_failure_feeding_mic_audio_in_fails_the_call() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    conversation.audio_fails = ConnectionError("speech-to-text went away")

    socket.mic(b"\x01")
    await socket.until("call.failed")

    assert socket.frame("call.failed")["text"] == "the call broke: speech-to-text went away"
    assert conversation.closed == 1
    assert not linked.done()
    await _close(socket, linked)
    assert conversation.closed == 1


async def test_a_failure_in_the_conversation_s_outputs_fails_the_call() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made

    conversation.out.put_nowait(b"\x01")
    conversation.out.put_nowait(RuntimeError("text-to-speech went away"))
    await socket.until("call.failed")

    assert socket.types() == ["link.ready", "call.ready", "bytes", "call.failed"]
    assert socket.frame("call.failed")["text"] == "the call broke: text-to-speech went away"
    assert conversation.closed == 1
    assert not linked.done()
    await _close(socket, linked)
    assert conversation.closed == 1


async def test_a_cancelled_link_handler_ends_its_call() -> None:
    setup = await _setup()
    _, linked = await _in_call(setup)

    linked.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(_WAIT):
            await linked

    assert setup.voice.made[0].closed == 1


async def test_a_client_that_stops_reading_the_call_s_audio_ends_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.routers.link.service._SEND_SECONDS", 0.05)
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    socket.stalls.add("bytes")

    conversation.out.put_nowait(b"\x01")
    await _eventually(lambda: conversation.closed == 1)

    assert socket.gone
    assert "call.ended" not in socket.types()
    assert "call.failed" not in socket.types()
    await _close(socket, linked)
    assert conversation.closed == 1


async def test_a_client_that_stops_reading_chat_ends_its_call_even_if_nothing_wakes_the_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The socket closed by the timeout does not wake the link's loop here, and the call
    # is silent: only the timeout itself can end the call.
    monkeypatch.setattr("app.routers.link.service._SEND_SECONDS", 0.05)
    never = asyncio.Event()
    setup = await _setup([_delta("Once upon "), never])
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    socket.stalls.add("chat.text")

    socket.send(type="chat.send", gem=str(setup.gem.id), text="a story")
    await _eventually(lambda: conversation.closed == 1)

    assert socket.gone
    assert not linked.done()
    never.set()
    await _close(socket, linked)
    assert conversation.closed == 1


@pytest.mark.parametrize("finishes_anyway", [False, True])
async def test_call_end_while_the_call_opens_stops_the_opening_and_gets_call_ended(
    finishes_anyway: bool,
) -> None:
    setup = await _setup()
    setup.voice.gate = asyncio.Event()
    setup.voice.finishes_anyway = finishes_anyway
    socket, linked = await _open(setup)

    socket.send(type="call.start")
    await _eventually(lambda: len(setup.voice.asked) == 1)
    socket.send(type="call.end")
    await socket.until("call.ended")

    assert socket.types() == ["link.ready", "call.ended"]
    # Stopped before it opened anything, or, if it opened all the same, closed.
    assert [c.closed for c in setup.voice.made] == ([1] if finishes_anyway else [])
    assert not linked.done()
    await _close(socket, linked)
    assert [c.closed for c in setup.voice.made] == ([1] if finishes_anyway else [])


@pytest.mark.parametrize("finishes_anyway", [False, True])
async def test_the_link_dropping_while_the_call_opens_stops_the_opening(
    finishes_anyway: bool,
) -> None:
    setup = await _setup()
    setup.voice.gate = asyncio.Event()
    setup.voice.finishes_anyway = finishes_anyway
    socket, linked = await _open(setup)

    socket.send(type="call.start")
    await _eventually(lambda: len(setup.voice.asked) == 1)
    await _close(socket, linked)

    assert [c.closed for c in setup.voice.made] == ([1] if finishes_anyway else [])


async def test_a_call_hung_up_while_it_waits_for_the_one_before_it_never_opens() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    first = setup.voice.made[0]

    socket.send(type="call.end")
    socket.send(type="call.start")
    socket.send(type="call.end")
    await socket.until("call.ended", 2)

    assert socket.types() == ["link.ready", "call.ready", "call.ended", "call.ended"]
    assert len(setup.voice.asked) == 1
    assert first.closed == 1
    await _close(socket, linked)
    assert first.closed == 1


# ---- chat and a call on one socket


async def test_chat_is_read_and_answered_while_a_call_opens_and_runs() -> None:
    setup = await _setup(_said("Hi."), _said("Again."))
    setup.voice.gate = asyncio.Event()
    socket, linked = await _open(setup)

    socket.send(type="call.start")
    socket.send(type="chat.send", gem=str(setup.gem.id), text="hello")
    await socket.until("chat.done")
    setup.voice.gate.set()
    await socket.until("call.ready")
    [conversation] = setup.voice.made
    conversation.out.put_nowait(_event("user.final", "are you there"))
    socket.send(type="chat.send", gem=str(setup.gem.id), text="hello again")
    await socket.until("chat.done", 2)
    await socket.until("call.user.final")

    assert socket.frame("chat.done")["text"] == "Hi."
    assert conversation.closed == 0
    await _close(socket, linked)
    assert conversation.closed == 1


async def test_a_failed_chat_reply_does_not_disturb_the_call() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made

    socket.send(type="chat.send", gem=str(ObjectId()), text="hello")
    await socket.until("chat.error")
    conversation.out.put_nowait(b"\x01")
    socket.mic(b"\x02")
    await socket.until("bytes")
    await _eventually(lambda: conversation.audio == [b"\x02"])

    assert conversation.closed == 0
    assert "call.failed" not in socket.types()
    await _close(socket, linked)


@pytest.mark.parametrize("gem", ["not-an-id", None])
async def test_a_call_that_does_not_open_does_not_disturb_a_chat_reply(gem: str | None) -> None:
    pause = asyncio.Event()
    setup = await _setup([_delta("Once "), pause, _delta("upon a time."), _completed()])
    if gem is None:
        setup.voice.fails = ConnectionError("no route")
    socket, linked = await _open(setup)

    socket.send(type="chat.send", gem=str(setup.gem.id), text="a story")
    await socket.until("chat.text")
    socket.send(type="call.start", **({} if gem is None else {"gem": gem}))
    await socket.until("call.failed" if gem is None else "call.refused")
    pause.set()
    await socket.until("chat.done")

    assert socket.frame("chat.done")["text"] == "Once upon a time."
    assert not linked.done()
    await _close(socket, linked)

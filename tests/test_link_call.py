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
        # What it adds to the gem's conversation: nothing, unless a test adds to it.
        self.added: list[Any] = []
        self.grew = asyncio.Event()
        self.left_running: list[Any] = []
        self.audio: list[bytes] = []
        self.delays: list[float] = []
        self.audio_fails: Exception | None = None
        # With `audio_waits`, feeding audio in waits until it is set, as when speech
        # recognition takes none. `feeding` is how many frames are being fed in now.
        self.audio_waits: asyncio.Event | None = None
        self.feeding = 0
        self.closed = 0
        # Called as `close()` starts. With `close_waits`, closing then waits until it is
        # set, and with `close_fails` it raises once it has closed.
        self.closing: Callable[[], None] | None = None
        self.close_waits: asyncio.Event | None = None
        self.close_fails: Exception | None = None

    async def send_audio(self, pcm: bytes) -> None:
        if self.audio_fails is not None:
            raise self.audio_fails
        self.feeding += 1
        try:
            if self.audio_waits is not None:
                await self.audio_waits.wait()
        finally:
            self.feeding -= 1
        self.audio.append(pcm)

    def set_output_delay(self, seconds: float) -> None:
        self.delays.append(seconds)

    def results_came(self) -> None:
        pass

    async def outputs(self) -> AsyncIterator[VoiceEvent | bytes]:
        while (item := await self.out.get()) is not None:
            if isinstance(item, Exception):
                raise item
            yield item

    async def close(self) -> None:
        if self.closing is not None:
            self.closing()
        if self.close_waits is not None:
            await self.close_waits.wait()
        self.closed += 1
        if self.close_fails is not None:
            raise self.close_fails


class _Voice:
    """The voice entity. Records what each call was connected with, and the Darbar text
    its conversation was opened with (None until it is opened).

    With `gate`, connecting waits until it is set. With `finishes_anyway`, a connecting
    that is cancelled returns its line all the same, as one does that had finished in the
    same moment the cancel came. A line that is closed unopened is counted in `hung_up`.
    """

    def __init__(self) -> None:
        self.asked: list[tuple[object, object, object]] = []
        self.made: list[_Conversation] = []
        self.gate: asyncio.Event | None = None
        self.finishes_anyway = False
        self.fails: Exception | None = None
        self.hung_up = 0

    async def connect(self, gem: object, *, speaks_first: bool | None = None) -> "_Line":
        self.asked.append((speaks_first, gem, None))
        asked = len(self.asked) - 1
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            # Swallowed on purpose: this fakes an opening that finished as it was cancelled.
            if not self.finishes_anyway:
                raise
        if self.fails is not None:
            raise self.fails
        return _Line(self, asked)


class _Line:
    def __init__(self, voice: _Voice, asked: int) -> None:
        self._voice = voice
        self._asked = asked

    def open(self, *, darbar: str | None = None, **_: object) -> _Conversation:
        speaks_first, gem, _ = self._voice.asked[self._asked]
        self._voice.asked[self._asked] = (speaks_first, gem, darbar)
        conversation = _Conversation()
        self._voice.made.append(conversation)
        return conversation

    async def close(self) -> None:
        self._voice.hung_up += 1


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
            conversations=chat.conversations,
        )
        self.service = LinkServiceV1Class(chat.chat, calls)

    def start(self, socket: "_Socket", **fields: Any) -> None:
        """Send `call.start` with the gem's id, and any other fields given."""
        socket.send(type="call.start", gem=str(self.gem.id), **fields)


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
    setup.start(socket, **start)
    await socket.until("call.ready")
    return socket, linked


def _event(type_: str, text: str = "") -> VoiceEvent:
    return VoiceEvent.model_validate({"type": type_, "text": text})


# ---- a call, start to end


async def test_a_call_carries_audio_and_events_both_ways_and_ends_on_call_end() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
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
        {"type": "link.ready", "version": 3},
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

    setup.start(socket)
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

    setup.start(socket)
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
    ("start", "first"),
    [
        # Left out, or sent as null, it is the gem's to say. Sent, it wins for this call.
        ({}, None),
        ({"first": None}, None),
        ({"first": "user"}, False),
        ({"first": "assistant"}, True),
    ],
)
async def test_a_call_opens_with_its_gem_its_darbar_text_and_who_speaks_first_if_asked(
    start: dict[str, Any], first: bool | None
) -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup, **start)

    [(asked, gem, darbar)] = setup.voice.asked
    assert asked is first
    assert cast("Any", gem).id == setup.gem.id
    assert darbar is not None and darbar.startswith("The user keeps a few assistants")
    await _close(socket, linked)


# ---- a call that does not open


@pytest.mark.parametrize("start", [{}, {"gem": None}, {"gem": 5}])
async def test_a_start_that_names_no_gem_is_refused(start: dict[str, Any]) -> None:
    setup = await _setup()
    socket, linked = await _open(setup)

    socket.send(type="call.start", **start)
    await socket.until("call.refused")

    assert socket.frame("call.refused") == {
        "type": "call.refused",
        "text": "no gem, or an unknown setting",
    }
    assert setup.voice.asked == []
    assert not linked.done()
    await _close(socket, linked)


@pytest.mark.parametrize("first", ["nobody", 5])
async def test_a_setting_that_is_not_offered_is_refused(first: object) -> None:
    setup = await _setup()
    socket, linked = await _open(setup)

    setup.start(socket, first=first)
    await socket.until("call.refused")

    assert socket.frame("call.refused") == {
        "type": "call.refused",
        "text": "no gem, or an unknown setting",
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

    setup.start(socket)
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

    setup.start(socket)
    await socket.until("call.failed")

    assert socket.frame("call.failed")["text"] == "the gem's config cannot be read"
    assert setup.voice.asked == []
    assert not linked.done()
    await _close(socket, linked)


async def test_speech_services_that_cannot_be_reached_fail_the_call() -> None:
    setup = await _setup()
    setup.voice.fails = ConnectionError("no route to the speech service")
    socket, linked = await _open(setup)

    setup.start(socket)
    await socket.until("call.failed")

    assert socket.frame("call.failed")["text"] == "speech services unavailable"
    assert socket.types() == ["link.ready", "call.failed"]
    await _close(socket, linked)


async def test_after_a_call_that_did_not_open_another_can_be_started() -> None:
    setup = await _setup()
    socket, linked = await _open(setup)

    socket.send(type="call.start", gem="not-an-id")
    await socket.until("call.refused")
    setup.start(socket)
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

    setup.start(socket)
    await _eventually(lambda: len(setup.voice.asked) == 1)
    setup.start(socket, first="assistant")
    await socket.until("error")
    if setup.voice.gate is not None:
        setup.voice.gate.set()
    await socket.until("call.ready")
    socket.mic(b"\x01")
    [conversation] = setup.voice.made
    await _eventually(lambda: conversation.audio == [b"\x01"])

    assert [first for first, _, _ in setup.voice.asked] == [None]
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
    setup.start(socket, first="assistant")
    await socket.until("call.ready", 2)

    assert socket.types() == ["link.ready", "call.ready", "call.ended", "call.ready"]
    assert first.closed == 1
    assert setup.voice.asked[1][0] is True
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
    setup.start(socket)
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
    other = await setup.chat.gems.create(name="Gem Two", model="6 Luna")
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    socket.stalls.add("chat.text")

    socket.send(type="chat.send", gem=str(other.id), text="a story")
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

    setup.start(socket)
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

    setup.start(socket)
    await _eventually(lambda: len(setup.voice.asked) == 1)
    await _close(socket, linked)

    assert [c.closed for c in setup.voice.made] == ([1] if finishes_anyway else [])


async def test_a_call_hung_up_while_it_waits_for_the_one_before_it_never_opens() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    first = setup.voice.made[0]

    socket.send(type="call.end")
    setup.start(socket)
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
    other = await setup.chat.gems.create(name="Gem Two", model="6 Luna")
    setup.voice.gate = asyncio.Event()
    socket, linked = await _open(setup)

    # While speech is connecting, the call does not hold the gem yet: it is written to.
    setup.start(socket)
    socket.send(type="chat.send", gem=str(setup.gem.id), text="hello")
    await socket.until("chat.done")
    setup.voice.gate.set()
    await socket.until("call.ready")
    [conversation] = setup.voice.made
    conversation.out.put_nowait(_event("user.final", "are you there"))
    # Another gem is written to while the call runs.
    socket.send(type="chat.send", gem=str(other.id), text="hello again")
    await socket.until("chat.done", 2)
    await socket.until("call.user.final")

    assert [
        f["text"] for f in socket.frames if isinstance(f, dict) and f["type"] == "chat.done"
    ] == [
        "Hi.",
        "Again.",
    ]
    assert conversation.closed == 0
    await _close(socket, linked)
    assert conversation.closed == 1


@pytest.mark.parametrize("same_socket", [True, False])
async def test_a_message_to_a_gem_in_a_call_gets_chat_error_and_the_call_carries_on(
    same_socket: bool,
) -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    writer, writer_linked = (socket, linked) if same_socket else await _open(setup)

    writer.send(type="chat.send", gem=str(setup.gem.id), text="hello")
    await writer.until("chat.error")
    conversation.out.put_nowait(b"\x01")
    await socket.until("bytes")

    assert writer.frame("chat.error") == {
        "type": "chat.error",
        "gem": str(setup.gem.id),
        "text": "the gem is in a call",
    }
    assert await setup.chat.stored() == []
    assert setup.chat.llm.requests == []
    assert conversation.closed == 0
    assert "call.failed" not in socket.types()
    if not same_socket:
        await _close(writer, writer_linked)
    await _close(socket, linked)


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


@pytest.mark.parametrize("refused", [True, False])
async def test_a_call_that_does_not_open_does_not_disturb_a_chat_reply(refused: bool) -> None:
    pause = asyncio.Event()
    setup = await _setup([_delta("Once "), pause, _delta("upon a time."), _completed()])
    if not refused:
        setup.voice.fails = ConnectionError("no route")
    socket, linked = await _open(setup)

    socket.send(type="chat.send", gem=str(setup.gem.id), text="a story")
    await socket.until("chat.text")
    if refused:
        socket.send(type="call.start", gem="not-an-id")
    else:
        setup.start(socket)
    await socket.until("call.refused" if refused else "call.failed")
    pause.set()
    await socket.until("chat.done")

    assert socket.frame("chat.done")["text"] == "Once upon a time."
    assert not linked.done()
    await _close(socket, linked)


# ---- found in review


async def test_the_conversation_is_closed_before_the_call_s_last_frame() -> None:
    # So that when the app sees that frame, the speech connections are already released.
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    sent_by_then: list[list[str]] = []
    conversation.closing = lambda: sent_by_then.append(socket.types())

    socket.send(type="call.end")
    await socket.until("call.ended")

    assert sent_by_then == [["link.ready", "call.ready"]]
    await _close(socket, linked)


async def test_nothing_is_being_fed_into_the_conversation_when_it_is_closed() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    conversation.audio_waits = asyncio.Event()
    feeding_by_then: list[int] = []
    conversation.closing = lambda: feeding_by_then.append(conversation.feeding)
    socket.mic(b"\x01")
    await _eventually(lambda: conversation.feeding == 1)

    socket.send(type="call.end")
    await socket.until("call.ended")

    assert feeding_by_then == [0]
    assert conversation.closed == 1
    await _close(socket, linked)


async def test_a_call_hung_up_while_it_opens_gets_call_ended_even_if_the_opening_is_refused() -> (
    None
):
    setup = await _setup()
    setup.voice.gate = asyncio.Event()
    setup.voice.finishes_anyway = True  # the opening runs on past the hang-up
    setup.voice.fails = LookupError("no OpenAI key is set, so 6 Luna cannot be used")
    socket, linked = await _open(setup)
    setup.start(socket)
    await _eventually(lambda: len(setup.voice.asked) == 1)

    socket.send(type="call.end")
    await socket.until("call.ended")

    assert socket.types() == ["link.ready", "call.ended"]
    await _close(socket, linked)


async def test_a_refused_start_hung_up_behind_an_ending_call_gets_call_ended() -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    conversation.close_waits = asyncio.Event()

    socket.send(type="call.end")
    setup.start(socket, first="nobody")
    socket.send(type="call.end")
    await _settle()
    assert socket.types() == ["link.ready", "call.ready"]  # the first call is still closing
    conversation.close_waits.set()
    await socket.until("call.ended", 2)

    assert socket.types() == ["link.ready", "call.ready", "call.ended", "call.ended"]
    assert len(setup.voice.made) == 1
    await _close(socket, linked)


async def test_a_conversation_that_fails_to_close_still_ends_the_call_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    conversation.close_fails = RuntimeError("the speech connection would not close")

    socket.send(type="call.end")
    await socket.until("call.ended")

    assert conversation.closed == 1
    assert "did not close cleanly" in caplog.text
    assert "the speech connection would not close" in caplog.text
    # The link is unharmed, and another call can be started.
    setup.start(socket)
    await socket.until("call.ready", 2)
    await _close(socket, linked)


class _NoDarbar:
    async def read(self) -> None:
        raise ConnectionError("the database is out of reach")


async def test_a_darbar_text_that_cannot_be_read_fails_the_call_and_says_so() -> None:
    setup = await _setup()
    calls = CallEntityClass(
        gems=setup.chat.gems,
        darbar=cast("Any", _NoDarbar()),
        voice=cast("Any", setup.voice),
        conversations=setup.chat.conversations,
    )
    setup.service = LinkServiceV1Class(setup.chat.chat, calls)
    socket, linked = await _open(setup)

    socket.send(type="call.start", gem=str(setup.gem.id))
    await socket.until("call.failed")

    assert socket.frame("call.failed")["text"] == "the Darbar text could not be read"
    assert setup.voice.asked == []
    await _close(socket, linked)


async def test_mic_audio_that_is_not_taken_fails_the_call_once_too_much_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.routers.link.service._MIC_FRAMES_WAITING", 3)
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    conversation.audio_waits = asyncio.Event()

    socket.mic(b"\x01")
    await _eventually(lambda: conversation.feeding == 1)  # one is being fed in, and waits
    for frame in (b"\x02", b"\x03", b"\x04"):
        socket.mic(frame)  # three more wait their turn: as many as may
    await _settle()
    assert "call.failed" not in socket.types()
    socket.mic(b"\x05")
    await socket.until("call.failed")

    assert socket.frame("call.failed")["text"] == (
        "the call broke: speech recognition is not taking the audio"
    )
    assert conversation.closed == 1
    assert not linked.done()
    await _close(socket, linked)
    assert conversation.closed == 1


async def test_frames_read_after_the_client_stopped_reading_are_not_acted_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A client can have sent frames before it stopped reading. They are handed over
    # after the socket was closed on it, and a `call.start` among them opens nothing.
    monkeypatch.setattr("app.routers.link.service._SEND_SECONDS", 0.05)
    setup = await _setup()
    socket, linked = await _in_call(setup)
    [conversation] = setup.voice.made
    socket.stalls.add("bytes")
    conversation.out.put_nowait(b"\x01")
    await _eventually(lambda: conversation.closed == 1)
    assert socket.gone

    setup.start(socket)
    async with asyncio.timeout(_WAIT):
        await linked

    assert len(setup.voice.made) == 1
    assert conversation.closed == 1

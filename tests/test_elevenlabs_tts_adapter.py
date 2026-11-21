import asyncio
import base64
import json
from typing import Any, cast

import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.protocol import State

from app.adapters.elevenlabs_tts_adp import (
    ElevenLabsTTSContext,
    ElevenLabsTTSSession,
    ElevenLabsTTSWord,
)

_VOICE = "voice-1"


class _Socket:
    """A stand-in for the WebSocket: records what is sent, plays back queued frames."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        self.state = State.OPEN

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        return await self.frames.get()

    async def close(self) -> None:
        self.state = State.CLOSED

    def frame(self, context_id: str | None, **fields: Any) -> None:
        self.frames.put_nowait(json.dumps({"context_id": context_id, **fields}))


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _session(*sockets: _Socket, clock: _Clock | None = None) -> ElevenLabsTTSSession:
    first, *rest = sockets

    async def open_socket() -> Any:
        return rest.pop(0)

    return ElevenLabsTTSSession(
        cast("Any", first), open_socket, voice=_VOICE, clock=clock or _Clock()
    )


def _audio(seconds: float) -> str:
    return base64.b64encode(b"\x00\x00" * round(24000 * seconds)).decode()


def _alignment(text: str, starts_ms: list[float], durations_ms: list[float]) -> dict[str, Any]:
    return {
        "chars": list(text),
        "char_start_times_ms": starts_ms,
        "char_durations_ms": durations_ms,
    }


def _context_id(context: ElevenLabsTTSContext) -> str:
    return context._id  # pyright: ignore[reportPrivateUsage]  # the frames name it


async def _collect(context: ElevenLabsTTSContext) -> list[bytes | list[ElevenLabsTTSWord]]:
    return [event async for event in context.events()]


async def test_an_utterance_opens_with_its_voice_and_closes_after_its_text() -> None:
    socket = _Socket()
    context = _session(socket).context()

    await context.push("Hello ")
    await context.push("there.")
    await context.finish()

    cid = _context_id(context)
    assert socket.sent == [
        {"context_id": cid, "inputs": [{"text": "Hello ", "voice_id": _VOICE}], "voices": [_VOICE]},
        {"context_id": cid, "inputs": [{"text": "there.", "voice_id": _VOICE}]},
        {"context_id": cid, "close_context": True},
    ]


async def test_words_are_timed_from_where_their_frame_s_audio_begins() -> None:
    socket = _Socket()
    context = _session(socket).context()
    await context.push("Hi there, you.")
    await context.finish()
    cid = _context_id(context)
    # Audio with no alignment first, then frames whose timings start again at 0. "there,"
    # is split over two frames.
    socket.frame(cid, audio=_audio(0.5))
    socket.frame(
        cid,
        audio=_audio(1.0),
        alignment=_alignment("Hi th", [0, 100, 200, 300, 400], [100, 100, 100, 100, 100]),
    )
    socket.frame("an-earlier-one", audio=_audio(9.0))
    socket.frame(
        cid,
        audio=_audio(1.0),
        alignment=_alignment("ere, you.", [0, 50, 100, 150, 200, 250, 300, 350, 400], [50] * 9),
    )
    socket.frame(cid, is_final_audio_for_turn=True)
    socket.frame(cid, is_final=True)

    events = await _collect(context)

    audio = [event for event in events if isinstance(event, bytes)]
    assert [len(chunk) for chunk in audio] == [24000, 48000, 48000]
    words = [word for event in events if isinstance(event, list) for word in event]
    assert words == [
        ElevenLabsTTSWord("Hi", 0.5, 0.7),
        ElevenLabsTTSWord("there,", 0.8, 1.7),
        ElevenLabsTTSWord("you.", 1.75, 1.95),
    ]


async def test_an_error_frame_fails_the_utterance() -> None:
    socket = _Socket()
    context = _session(socket).context()
    await context.push("Hello.")
    socket.frame(None, error="quota_exceeded", message="Out of credits", code=1008)

    with pytest.raises(RuntimeError, match="Out of credits"):
        await _collect(context)


async def test_cancelling_a_finished_utterance_sends_nothing_more() -> None:
    # ElevenLabs closes the whole socket when a closed context is written to again.
    socket = _Socket()
    context = _session(socket).context()
    await context.push("Hello.")
    await context.finish()

    await context.cancel()

    assert sum("close_context" in message for message in socket.sent) == 1


async def test_cancelling_an_utterance_closes_it_once() -> None:
    socket = _Socket()
    context = _session(socket).context()
    await context.push("Hello")

    await context.cancel()
    await context.cancel()
    await context.finish()

    assert socket.sent[1:] == [{"context_id": _context_id(context), "close_context": True}]


async def test_a_cancelled_utterance_ends_without_its_remaining_audio() -> None:
    socket = _Socket()
    context = _session(socket).context()
    await context.push("Hello there.")
    socket.frame(_context_id(context), audio=_audio(0.5))
    events = context.events()
    assert isinstance(await anext(events), bytes)

    await context.cancel()
    socket.frame(_context_id(context), audio=_audio(0.5))

    assert [event async for event in events] == []


async def test_a_cancelled_utterance_ends_quietly_when_its_socket_closes() -> None:
    socket = _Socket()
    context = _session(socket).context()
    await context.push("Hello there.")
    await context.cancel()

    async def recv() -> str:
        raise ConnectionClosedOK(None, None)

    socket.recv = recv  # the session was closed, as stopping a reply does

    assert await _collect(context) == []


async def test_an_utterance_given_no_text_ends_at_once() -> None:
    socket = _Socket()
    context = _session(socket).context()

    events = asyncio.create_task(_collect(context))
    await context.finish()

    assert await events == []
    assert socket.sent == []


async def test_a_socket_idle_too_long_is_replaced_before_an_utterance() -> None:
    first, second, clock = _Socket(), _Socket(), _Clock()
    session = _session(first, second, clock=clock)
    await session.context().push("Let me check.")

    clock.now += 16.0  # a long tool call: ElevenLabs closes a socket at 20 s
    await session.context().push("Found it.")

    assert len(first.sent) == 1
    assert first.state is State.CLOSED
    assert second.sent[0]["inputs"][0]["text"] == "Found it."
    assert "voices" in second.sent[0]


async def test_a_socket_that_was_closed_is_replaced_before_an_utterance() -> None:
    first, second = _Socket(), _Socket()
    session = _session(first, second)
    first.state = State.CLOSED

    await session.context().push("Hello.")

    assert first.sent == []
    assert len(second.sent) == 1


async def test_a_socket_in_use_is_kept() -> None:
    first, second, clock = _Socket(), _Socket(), _Clock()
    session = _session(first, second, clock=clock)
    await session.context().push("One.")

    clock.now += 10.0
    await session.context().push("Two.")

    assert len(first.sent) == 2
    assert second.sent == []

"""
ElevenLabs adapter responsible for streaming text-to-speech over the Text to Dialogue
multi-context WebSocket.

Responsibilities:
- Hold the API key and open the WebSockets
- Run one context per utterance: text fragments in, raw PCM audio and word timings
  out, cancellable

Eleven v4 Turbo is served only on the Text to Dialogue sockets, not on the older
text-to-speech one. See
https://elevenlabs.io/docs/api-reference/text-to-dialogue/ttd-multi-websocket for the wire
protocol. What the docs leave out was found against the live API (2026-11-21):

- Each frame's alignment starts again at 0 and counts from where that frame's audio
  begins. It does not cover exactly its own audio, so adding up the alignments drifts:
  measured against the pauses in the audio, the frame's start was the closer of the two
  at 7 pauses of 8, and the running sum was up to 0.3 s early.
- The alignment's characters are the pushed text exactly, spaces and newlines included,
  so the words built from them are the whitespace-separated words of the text.
- Nothing stops an utterance being generated. `close_context` lets the rest of it come
  (19 s of audio arrived in 1.6 s after one was closed). Closing the socket is what
  stops it, and that takes about a tenth of a second.
- A socket that has been sent nothing for 20 s is closed by ElevenLabs. Text keeps it
  open: three utterances 12 s apart ran on one socket.
"""

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from types import MappingProxyType
from typing import Literal, NamedTuple
from urllib.parse import urlencode
from uuid import uuid4

from pydantic import BaseModel, JsonValue
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from app.config import settings

__all__ = [
    "ElevenLabsTTSAdapterClass",
    "ElevenLabsTTSContext",
    "ElevenLabsTTSSession",
    "ElevenLabsTTSVoice",
    "ElevenLabsTTSWord",
]

_URL = "wss://api.elevenlabs.io/v1/text-to-dialogue/multi-stream-input"
# Seconds without any frame from ElevenLabs before an utterance gives up.
_CONTEXT_TIMEOUT = 30.0
# ElevenLabs closes a socket it has been sent nothing for 20 s. One that has gone this
# long without text is replaced before it is used, rather than risk it closing mid-way.
_IDLE_LIMIT = 15.0

type ElevenLabsTTSVoice = Literal["female", "male"]


class ElevenLabsTTSWord(NamedTuple):
    """One word of an utterance, as ElevenLabs timed it."""

    text: str
    start: float  # seconds from the start of the utterance's audio
    end: float  # the same, for where the word ends


class _Types:
    """Group private ElevenLabs model, voice and audio settings."""

    type AvailableModels = Literal["Eleven v4 Turbo"]
    model_map = MappingProxyType[AvailableModels, str]({"Eleven v4 Turbo": "eleven_v4_turbo"})
    # Premade voices that worked with Eleven v4 Turbo on 2026-11-21. The key cannot read
    # a voice's details, so which is female and which male was not checked here.
    voice_map = MappingProxyType[ElevenLabsTTSVoice, str](
        {"female": "21m00Tcm4TlvDq8ikWAM", "male": "JBFqnCBsd6RMkjVDRZzb"}
    )
    # Raw 16-bit mono PCM: no container, so chunks can be played as they arrive.
    sample_rate = 24000
    bytes_per_sample = 2
    output_format = "pcm_24000"


class _Alignment(BaseModel):
    """When each character of a frame is spoken, in ms from the start of the frame's audio."""

    chars: list[str]
    char_start_times_ms: list[float]
    char_durations_ms: list[float]


class _Frame(BaseModel):
    """One message from ElevenLabs. Only the fields used here are read."""

    context_id: str | None = None
    audio: str | None = None  # base64 PCM
    alignment: _Alignment | None = None
    is_final: bool = False  # the context's last message
    error: JsonValue = None
    message: str | None = None


class _Words:
    """Build whole words from character timings, which arrive a few characters at a time."""

    def __init__(self) -> None:
        self._text = ""
        self._start = 0.0
        self._end = 0.0

    def feed(self, alignment: _Alignment, offset: float) -> list[ElevenLabsTTSWord]:
        """Take one frame's alignment; return the words it completes.

        `offset` is where the frame's audio begins, in seconds from the start of the
        utterance's audio. A word is complete once the whitespace after it is timed.
        """
        words: list[ElevenLabsTTSWord] = []
        for chars, start, duration in zip(
            alignment.chars, alignment.char_start_times_ms, alignment.char_durations_ms, strict=True
        ):
            for char in chars:
                if char.isspace():
                    words.extend(self.finish())
                    continue
                if not self._text:
                    self._start = offset + start / 1000
                self._text += char
                self._end = offset + (start + duration) / 1000
        return words

    def finish(self) -> list[ElevenLabsTTSWord]:
        """The word being built, if there is one: the text ends here."""
        if not self._text:
            return []
        word = ElevenLabsTTSWord(self._text, self._start, self._end)
        self._text = ""
        return [word]


class ElevenLabsTTSContext:
    """One utterance: push text fragments in order, iterate audio and word timings out."""

    def __init__(self, session: "ElevenLabsTTSSession", voice: str) -> None:
        self._session = session
        self._voice = voice
        self._id = uuid4().hex
        # The socket the utterance runs on, from its first text on. Set once.
        self._socket: ClientConnection | None = None
        self._started = asyncio.Event()  # the first text was sent, or none ever will be
        # Whether ElevenLabs was told the utterance is closed. Nothing more may be sent
        # for it then: ElevenLabs takes that as a protocol error and closes the socket.
        self._closed = False
        self._cancelled = False

    async def push(self, text: str) -> None:
        """Add the next fragment of text. ElevenLabs joins fragments into one utterance."""
        if self._closed:
            raise RuntimeError("Text was pushed to an utterance that was finished")
        message: dict[str, JsonValue] = {
            "context_id": self._id,
            "inputs": [{"text": text, "voice_id": self._voice}],
        }
        if self._socket is None:
            # The first message of a context opens it, and names its voice.
            self._socket = await self._session.socket_for_new_context()
            message["voices"] = [self._voice]
            await self._session.send(self._socket, message, text=True)
            self._started.set()
        else:
            await self._session.send(self._socket, message, text=True)

    async def finish(self) -> None:
        """Say that no more text is coming; the remaining audio still arrives."""
        if self._closed:
            return
        self._closed = True
        if self._socket is None:
            self._started.set()  # nothing was said, so there is nothing to hear
            return
        await self._session.send(self._socket, {"context_id": self._id, "close_context": True})

    async def events(self) -> AsyncIterator[bytes | list[ElevenLabsTTSWord]]:
        """Yield PCM chunks and word timings, as they arrive, until the utterance is done.

        The words are the whitespace-separated words of the pushed text, in order. Each
        timing arrives with the audio of the word after it, or at the end.
        """
        await self._started.wait()
        if self._socket is None:
            return
        words = _Words()
        received = 0  # bytes of this utterance's audio so far
        while True:
            try:
                raw = await asyncio.wait_for(self._socket.recv(), _CONTEXT_TIMEOUT)
            except TimeoutError as error:
                raise RuntimeError("ElevenLabs sent nothing for too long") from error
            except ConnectionClosed as error:
                if self._cancelled:
                    return  # closing the session is how a cancelled utterance is stopped
                raise RuntimeError(f"ElevenLabs closed the connection: {error}") from error
            if self._cancelled:
                return
            frame = _Frame.model_validate_json(raw)
            if frame.error is not None:
                raise RuntimeError(f"ElevenLabs speech synthesis failed: {frame.message}")
            if frame.context_id != self._id:
                continue  # left over from an utterance before this one
            offset = received / _Types.bytes_per_sample / _Types.sample_rate
            if frame.audio:
                audio = base64.b64decode(frame.audio)
                received += len(audio)
                yield audio
            if frame.alignment is not None and (done := words.feed(frame.alignment, offset)):
                yield done
            if frame.is_final:
                if rest := words.finish():
                    yield rest
                return

    async def cancel(self) -> None:
        """Take no more of this utterance.

        ElevenLabs cannot be told to stop generating it: whatever still comes for it is
        ignored, and closing the session is what stops it. `events()` ends.
        """
        self._cancelled = True
        if self._closed or self._socket is None:
            self._closed = True
            self._started.set()
            return
        self._closed = True
        with contextlib.suppress(ConnectionClosed):
            await self._session.send(self._socket, {"context_id": self._id, "close_context": True})


class ElevenLabsTTSSession:
    """The text-to-speech connection of one reply, which closes it when it is done.

    It is a socket that is replaced when ElevenLabs has closed it or soon would: an
    utterance said after a long tool call starts on a new one. An utterance starts only
    once the one before it is done, so the socket being replaced is never in use.
    """

    def __init__(
        self,
        socket: ClientConnection,
        open_socket: Callable[[], Awaitable[ClientConnection]],
        *,
        voice: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._socket = socket
        self._open_socket = open_socket
        self._voice = voice
        self._clock = clock
        self._texted_at = clock()  # when the socket was opened or last sent text

    def context(self) -> ElevenLabsTTSContext:
        """Start a new utterance on this connection."""
        return ElevenLabsTTSContext(self, self._voice)

    async def socket_for_new_context(self) -> ClientConnection:
        """The socket a new utterance runs on: the current one, unless it is dead or idle."""
        idle = self._clock() - self._texted_at > _IDLE_LIMIT
        if idle or self._socket.state is not State.OPEN:
            old, self._socket = self._socket, await self._open_socket()
            self._texted_at = self._clock()
            with contextlib.suppress(Exception):
                await old.close()
        return self._socket

    async def send(
        self, socket: ClientConnection, message: dict[str, JsonValue], *, text: bool = False
    ) -> None:
        """Send one message. `text` says it carries text, which keeps the socket open."""
        await socket.send(json.dumps(message))
        if text and socket is self._socket:
            self._texted_at = self._clock()

    async def close(self) -> None:
        """Close the WebSocket. Anything still being generated on it stops."""
        await self._socket.close()


class ElevenLabsTTSAdapterClass:
    """Open ElevenLabs text-to-speech connections for streaming speech synthesis."""

    sample_rate = _Types.sample_rate

    def __init__(self) -> None:
        self._api_key: str | None = None

    async def initialize(self) -> None:
        if settings.elevenlabs_api_key:
            self._api_key = settings.elevenlabs_api_key

    async def close(self) -> None:
        """Nothing to close: each session belongs to whoever opened it."""

    @property
    def api_key(self) -> str:
        if self._api_key is None:
            raise RuntimeError("ElevenLabs API key not configured")
        return self._api_key

    async def connect(
        self,
        *,
        model: _Types.AvailableModels = "Eleven v4 Turbo",
        voice: ElevenLabsTTSVoice = "female",
    ) -> ElevenLabsTTSSession:
        """Open a TTS WebSocket. The caller owns the session and must `close()` it."""
        query = urlencode(
            {
                "model_id": _Types.model_map[model],
                "output_format": _Types.output_format,
                "sync_alignment": "true",
            }
        )
        open_socket = partial(self._open, f"{_URL}?{query}")
        return ElevenLabsTTSSession(await open_socket(), open_socket, voice=_Types.voice_map[voice])

    async def _open(self, url: str) -> ClientConnection:
        return await connect(url, additional_headers={"xi-api-key": self.api_key})

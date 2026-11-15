"""
Speech for the voice entity: saying a reply, and knowing how much of it was heard.

Speech is generated several times faster than it plays, so the client always holds
audio it has not played yet, and drops it when the user interrupts. What the user
actually heard is worked out here from two things:

- Cartesia's word timings: when each word ends within its utterance's audio.
- `Playback`: the server's picture of the client's speaker, kept by applying the
  client's own scheduling rule to every chunk that is sent.
"""

import asyncio
import contextlib
import re
import time
from collections.abc import Callable

from app.adapters.cartesia_tts_adp import (
    CartesiaTTSAdapterClass,
    CartesiaTTSContext,
    CartesiaTTSSession,
    CartesiaTTSWord,
)

__all__ = ["Clock", "Playback", "Speech", "Utterance"]

type Clock = Callable[[], float]

# Speech audio is raw 16-bit mono PCM, so this converts a byte count to playing time.
_BYTES_PER_SECOND = CartesiaTTSAdapterClass.sample_rate * 2

# The soonest the client can be playing a chunk after it is sent: the trip there plus the
# client's own scheduling margin. Set on the long side on purpose. A wrong guess should
# say the user heard less than they did, never more: words they did not hear must not
# end up in the history.
_CLIENT_DELAY = 0.15


class Playback:
    """The server's picture of the client's speaker.

    The client plays audio chunks back to back in the order they arrive, so the moment
    each one starts playing can be worked out here without asking the client.
    """

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self.clock = clock
        self._free_at = 0.0

    def schedule(self, seconds: float) -> float:
        """Record a chunk of this length being sent now. Return when it starts playing."""
        start = max(self._free_at, self.clock() + _CLIENT_DELAY)
        self._free_at = start + seconds
        return start

    def reset(self) -> None:
        """The client dropped everything it had queued."""
        self._free_at = 0.0

    @property
    def ends_at(self) -> float:
        """When the client finishes playing everything sent so far."""
        return self._free_at


class Utterance:
    """One stretch of speech: its text, when each word ends, and when its audio plays."""

    def __init__(self, context: CartesiaTTSContext) -> None:
        self.context = context
        self.text = ""
        self.complete = False  # all of its audio has been sent to the client
        self._words: list[CartesiaTTSWord] = []
        self._chunks: list[tuple[float, float]] = []  # (when it starts playing, seconds)

    def add_audio(self, starts_at: float, seconds: float) -> None:
        self._chunks.append((starts_at, seconds))

    def add_words(self, words: list[CartesiaTTSWord]) -> None:
        self._words.extend(words)

    def heard(self, at: float) -> tuple[str, bool]:
        """What the client had played of this utterance at time `at`, and whether that is all."""
        played = sum(min(seconds, max(0.0, at - start)) for start, seconds in self._chunks)
        if self.complete and played >= sum(seconds for _, seconds in self._chunks):
            return self.text.strip(), True
        tokens = list(re.finditer(r"\S+", self.text))
        heard = 0
        for token, word in zip(tokens, self._words, strict=False):
            # A timing that does not line up with the text ends the count there. That can
            # only make the answer shorter.
            if word.end > played or word.text != token.group():
                break
            heard += 1
        if heard == 0:
            return "", False
        # Audio usually runs on a little after the last word. Once that word has played,
        # the utterance has been heard, whatever silence is still to come.
        whole = self.complete and heard == len(tokens)
        return self.text[: tokens[heard - 1].end()].strip(), whole


class Speech:
    """The spoken side of one reply: text in, audio sent to the client.

    Each reply gets its own text-to-speech connection, opened while the model is still
    thinking and closed when the reply ends. A connection kept open between replies
    would be dropped by Cartesia once the user stays quiet for a while.

    A reply is one utterance, or more when it searches: what is said before a search
    ("Let me check") is ended as its own utterance, because Cartesia drops an utterance
    that is left waiting for more text.
    """

    def __init__(
        self,
        tts: CartesiaTTSAdapterClass,
        send: Callable[[bytes], None],
        playback: Playback,
    ) -> None:
        self._send = send
        self._playback = playback
        self._connecting = asyncio.create_task(tts.connect())
        self._utterance: Utterance | None = None
        self._open = False  # whether the utterance still takes text
        self._forwarding: asyncio.Task[None] | None = None
        self._silenced = False
        self.text = ""  # everything said so far, whether or not it has been heard

    async def say(self, text: str) -> Utterance:
        """Speak the next piece of text. Return the utterance it became part of."""
        if self._utterance is None or not self._open:
            if self._forwarding is not None:
                # The previous utterance's audio goes out before this one's.
                await self._forwarding
            self._utterance = Utterance((await self._connecting).context())
            self._open = True
            self._forwarding = asyncio.create_task(self._forward(self._utterance))
        self.text += text
        self._utterance.text += text
        await self._utterance.context.push(text)
        return self._utterance

    async def _forward(self, utterance: Utterance) -> None:
        async for event in utterance.context.events():
            if self._silenced:
                return
            if isinstance(event, bytes):
                seconds = len(event) / _BYTES_PER_SECOND
                utterance.add_audio(self._playback.schedule(seconds), seconds)
                self._send(event)
            else:
                utterance.add_words(event)
        utterance.complete = True

    async def end_utterance(self) -> None:
        """No more text for this utterance. Its remaining audio is still sent."""
        if self._utterance is not None and self._open:
            self._open = False
            self.text += " "
            await self._utterance.context.finish()

    async def finish(self) -> None:
        """No more text at all. Return once all the audio has been sent."""
        try:
            await self.end_utterance()
            if self._forwarding is not None:
                await self._forwarding
        finally:
            await self._disconnect()

    def silence(self) -> None:
        """Send no more audio, from this instant. `stop()` does the cleaning up."""
        self._silenced = True

    async def stop(self) -> None:
        """Stop speaking and release the connection."""
        self.silence()
        if self._forwarding is not None:
            self._forwarding.cancel()
            await asyncio.gather(self._forwarding, return_exceptions=True)
        if self._utterance is not None:
            with contextlib.suppress(Exception):
                await self._utterance.context.cancel()
        await self._disconnect()

    async def _disconnect(self) -> None:
        self._connecting.cancel()
        (connected,) = await asyncio.gather(self._connecting, return_exceptions=True)
        if isinstance(connected, CartesiaTTSSession):
            with contextlib.suppress(Exception):
                await connected.close()

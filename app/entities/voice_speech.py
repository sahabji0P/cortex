"""
Speech for the voice entity: saying a reply, and knowing how much of it was heard.

Speech is generated several times faster than it plays, so the client always holds
audio it has not played yet, and drops it when the user interrupts. What the user
actually heard is worked out here from two things:

- Cartesia's word timings: when each word ends within its utterance's audio.
- `Playback`: the server's picture of the client's speaker, kept by applying the
  client's own scheduling rule to every chunk that is sent.

The same timings are what the client shows a reply's words by. Each word is handed on
with where it starts in the call's speech audio, so the client can show it at the moment
it is heard (see `Speech`).
"""

import asyncio
import contextlib
import re
import time
from collections import deque
from collections.abc import Callable

from app.adapters.cartesia_tts_adp import (
    CartesiaTTSAdapterClass,
    CartesiaTTSContext,
    CartesiaTTSSession,
    CartesiaTTSWord,
)

__all__ = ["Clock", "Playback", "Speech", "Standby", "Utterance"]

type Clock = Callable[[], float]

# Speech audio is raw 16-bit mono PCM, so these convert a byte count to samples and to
# playing time.
_SAMPLE_RATE = CartesiaTTSAdapterClass.sample_rate
_BYTES_PER_SAMPLE = 2
_BYTES_PER_SECOND = _SAMPLE_RATE * _BYTES_PER_SAMPLE

# The soonest the client can be playing a chunk after it is sent: the trip there plus the
# client's own scheduling margin. Set on the long side on purpose. A wrong guess should
# say the user heard less than they did, never more: words they did not hear must not
# end up in the history.
_CLIENT_DELAY = 0.15
# The most a client may add to that for its sound device. Bluetooth runs to a few tenths
# of a second; anything far beyond is taken as a mistake.
_MAX_OUTPUT_DELAY = 1.0


# A connection opened ahead of time is used only if it is no older than this. Cartesia
# closes one that sits idle for some minutes, and waiting is all a spare one does.
_STANDBY_MAX_AGE = 30.0


async def _release(connecting: asyncio.Task[CartesiaTTSSession]) -> None:
    """Close a connection, whether or not it has finished opening."""
    connecting.cancel()
    (connected,) = await asyncio.gather(connecting, return_exceptions=True)
    if isinstance(connected, CartesiaTTSSession):
        with contextlib.suppress(Exception):
            await connected.close()


class Standby:
    """One text-to-speech connection, opened ahead of the reply that will use it.

    Opening a connection takes a tenth of a second or two. A reply opens its own while
    the model is still thinking, which is nearly always soon enough. This covers the
    times it is not: the connection is opened when the call starts and again each time
    the user starts speaking, so that it is there before the reply is.
    """

    def __init__(self, tts: CartesiaTTSAdapterClass, clock: Clock = time.monotonic) -> None:
        self._tts = tts
        self._clock = clock
        self._connecting: asyncio.Task[CartesiaTTSSession] | None = None
        self._opened_at = 0.0
        self._releasing: set[asyncio.Task[None]] = set()

    def ready(self) -> None:
        """Make sure a usable connection is open or on its way. A reply is expected soon."""
        spare = self._connecting
        if spare is not None:
            failed = spare.done() and (spare.cancelled() or spare.exception() is not None)
            if not failed and self._clock() - self._opened_at <= _STANDBY_MAX_AGE:
                return
            releasing = asyncio.create_task(_release(spare))
            self._releasing.add(releasing)
            releasing.add_done_callback(self._releasing.discard)
        self._connecting = asyncio.create_task(self._tts.connect())
        self._opened_at = self._clock()

    def take(self) -> asyncio.Task[CartesiaTTSSession]:
        """Hand the connection over to a reply, which closes it when it is done."""
        self.ready()
        connecting, self._connecting = self._connecting, None
        assert connecting is not None
        return connecting

    async def close(self) -> None:
        """Close the connection nobody took, and wait for the ones being put away."""
        if self._connecting is not None:
            await _release(self._connecting)
            self._connecting = None
        await asyncio.gather(*self._releasing, return_exceptions=True)


class Playback:
    """The server's picture of the client's speaker.

    The client plays audio chunks back to back in the order they arrive, so the moment
    each one starts playing can be worked out here without asking the client.
    """

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self.clock = clock
        self._free_at = 0.0
        self._device_delay = 0.0
        # How much speech audio the call has sent so far, in bytes. It is never set
        # back: the client counts the audio it receives the same way, so a place in the
        # call's audio means the same to both.
        self.sent = 0

    def set_output_delay(self, seconds: float) -> None:
        """Take the client's word for how long its sound device holds audio before it is heard.

        A speaker adds a few hundredths of a second and Bluetooth a few tenths. Without
        this the user would be taken to have heard words still on their way to their ears.
        It is kept apart from the schedule, so that a figure reported part-way through a
        reply also applies to the audio already sent.
        """
        self._device_delay = min(max(seconds, 0.0), _MAX_OUTPUT_DELAY)

    def schedule(self, seconds: float) -> float:
        """Record a chunk of this length being sent now. Return when the client plays it."""
        start = max(self._free_at, self.clock() + _CLIENT_DELAY)
        self._free_at = start + seconds
        return start

    def reset(self) -> None:
        """The client dropped everything it had queued."""
        self._free_at = 0.0

    def hearing(self) -> float:
        """The moment on the schedule that the user is hearing right now.

        Sound the client is playing now reaches the user one device delay later, so what
        they hear now is what it played that long ago.
        """
        return self.clock() - self._device_delay

    @property
    def ends_at(self) -> float:
        """When the user has heard the last of everything sent so far."""
        return self._free_at + self._device_delay


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

    def delivered(self, at: float) -> tuple[str, bool]:
        """The same as `heard`: in a call, what was delivered is what was heard."""
        return self.heard(at)


class Speech:
    """The spoken side of one reply: text in, audio sent to the client.

    It is the surface a call's replies are delivered on (see `loop.py`), and each of
    its utterances is one part of a reply.

    Each reply gets its own text-to-speech connection and closes it when the reply
    ends. It is handed one that is open already or still opening (see `Standby`). A
    single connection kept for the whole conversation would be dropped by Cartesia once
    the user stays quiet for a while.

    A reply is one utterance, or more when it searches: what is said before a search
    ("Let me check") is ended as its own utterance, because Cartesia drops an utterance
    that is left waiting for more text.
    """

    def __init__(
        self,
        tts: CartesiaTTSAdapterClass,
        send: Callable[[bytes], None],
        playback: Playback,
        connecting: asyncio.Task[CartesiaTTSSession],
        word: Callable[[str, int], None] = lambda text, start: None,
    ) -> None:
        # `word` is given each word as Cartesia timed it, and the sample of the call's
        # speech audio it starts at: counted at the audio's own rate, over all the
        # speech sent since the call opened.
        self._tts = tts
        self._send = send
        self._word = word
        self._playback = playback
        self._connecting = connecting
        # Whether the connection has carried anything yet. One that was opened ahead of
        # time may have died while it waited, which only shows when it is first used.
        self._untried = True
        self._utterance: Utterance | None = None
        self._open = False  # whether the utterance still takes text
        self._forwarding: asyncio.Task[None] | None = None
        self._silenced = False
        self.text = ""  # everything said so far, whether or not it has been heard

    async def say(self, text: str) -> Utterance:
        """Speak the next piece of text. Return the utterance it became part of."""
        self.text += text
        if self._utterance is not None and self._open:
            self._utterance.text += text
            await self._utterance.context.push(text)
            return self._utterance
        if self._forwarding is not None:
            # The previous utterance's audio goes out before this one's.
            await self._forwarding
        try:
            return await self._begin(text)
        except Exception:
            if not self._untried:
                raise
            # Nothing has been said on this connection, so nothing is lost by opening
            # another and saying it there.
            await _release(self._connecting)
            self._connecting = asyncio.create_task(self._tts.connect())
            return await self._begin(text)
        finally:
            self._untried = False

    async def _begin(self, text: str) -> Utterance:
        """Start a new utterance with this text."""
        utterance = Utterance((await self._connecting).context())
        utterance.text = text
        forwarding = asyncio.create_task(self._forward(utterance))
        try:
            await utterance.context.push(text)
        except BaseException:
            forwarding.cancel()
            await asyncio.gather(forwarding, return_exceptions=True)
            raise
        self._utterance, self._open, self._forwarding = utterance, True, forwarding
        return utterance

    async def _forward(self, utterance: Utterance) -> None:
        # Where this utterance's audio begins in the call's. Nothing else sends audio
        # while it is forwarded: the utterance before it has all gone out, and a reply
        # that was cut sends no more.
        begins_at = self._playback.sent // _BYTES_PER_SAMPLE
        # Words whose audio has not been sent yet, each with the sample it starts at. A
        # word goes out only after the audio it starts in, so the client is never given
        # a place in audio that a cut then keeps from being sent.
        waiting: deque[tuple[str, int]] = deque()
        async for event in utterance.context.events():
            if self._silenced:
                return
            if isinstance(event, bytes):
                seconds = len(event) / _BYTES_PER_SECOND
                utterance.add_audio(self._playback.schedule(seconds), seconds)
                self._playback.sent += len(event)
                self._send(event)
            else:
                utterance.add_words(event)
                waiting.extend(
                    (word.text, begins_at + round(word.start * _SAMPLE_RATE)) for word in event
                )
            sent = self._playback.sent // _BYTES_PER_SAMPLE
            while waiting and waiting[0][1] < sent:
                self._word(*waiting.popleft())
        # A timing that points past the end of the audio is off by a rounding at most:
        # the word is placed on the last sample sent.
        last = max(begins_at, self._playback.sent // _BYTES_PER_SAMPLE - 1)
        for text, _ in waiting:
            self._word(text, last)
        utterance.complete = True

    async def end_part(self) -> None:
        """No more text for this utterance. Its remaining audio is still sent."""
        if self._utterance is not None and self._open:
            self._open = False
            self.text += " "
            await self._utterance.context.finish()

    async def finish(self) -> None:
        """No more text at all. Return once all the audio has been sent."""
        try:
            await self.end_part()
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
        await _release(self._connecting)

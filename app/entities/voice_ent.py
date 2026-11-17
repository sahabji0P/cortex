"""
Voice entity: a spoken conversation as a cascade of three services.

    mic audio ──▶ speech-to-text ──▶ model ──▶ text-to-speech ──▶ speech audio
                  (Cartesia)         (OpenAI or (Cartesia)
                                      Anthropic)

One `VoiceConversation` per call. The speech-to-text service also detects turns:

- `turn.end`   the user finished a sentence. Its transcript starts a reply: the model's
               text is pushed to text-to-speech as it streams, and the audio is forwarded.
- `turn.start` the user began speaking. Whatever reply is in progress is cancelled (model
               and speech), and the client is told to drop audio it has not played yet.

The model may call `web_search` (Parallel) before it answers. Which model it is is chosen
when the conversation opens, and everything here is the same for either provider: what
differs between them is in `model.py`. Running a reply's model calls and its searches
is not particular to a call either: that loop is in `loop.py`, and speech is the surface
the call's replies are delivered on.

The history is what the model is sent on every call, and it holds what the user heard,
not what was generated. Speech is generated much faster than it plays, so a reply only
joins the history once it has been heard in full or cut off. A reply that is cut off is
stored as the words that were heard, ending in "…". The rest is never sent back.

Nothing the model has been sent is changed afterwards, so each request starts with the
whole of the one before it. The one exception is the user's open turn, always the last
item, which can gain a line (see below).

A user turn stays open until some of the reply to it has been heard. Words that arrive
before that join the same turn on a new line: speech recognition often splits one
sentence in two, and the second half is not a new request.

A tool call is not stopped by an interruption. If the user speaks while one is running,
it carries on, as it does in writing (see `late_results.py`). What the user said is
answered first. The real result is then added at the next quiet moment, even if it came
in long before, and the model tells the user about it in a reply of its own, without
waiting to be asked.
"""

import asyncio
import contextlib
import re
import time
import unicodedata
from collections.abc import AsyncIterator
from typing import Literal

from pydantic import BaseModel

from app.adapters.anthropic_adp import AnthropicAdapterClass
from app.adapters.cartesia_stt_adp import CartesiaSTTAdapterClass, CartesiaSTTSession
from app.adapters.cartesia_tts_adp import CartesiaTTSAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass
from app.adapters.parallel_adp import ParallelAdapterClass
from app.config import settings
from app.entities.gem_ent import GemConfig
from app.entities.instructions import instructions
from app.entities.late_results import LateResults
from app.entities.loop import CUT_MARK, Delivered, Reply, RoundInput, run_reply
from app.entities.model import (
    LLM,
    MODELS,
    Item,
    ModelInfo,
    ModelName,
    is_openai,
    model_for,
)
from app.entities.tools import Toolbox, web_search_tool
from app.entities.voice_speech import Clock, Playback, Speech, Standby

__all__ = ["VoiceConversation", "VoiceEntityClass", "VoiceEvent"]

# Opens the history of a conversation in which the assistant is to speak first. The
# model's own words never come first: its opening line is an answer to this.
_GREETING_NOTE = (
    "The call has just connected and the user has not said anything yet. Greet them in "
    "one short sentence and ask what you can do for them."
)

# Added to the history when a turn with no words in it cut off a reply that was being
# heard. It records what happened and no more: what to do about it is in the
# instructions. Something has to stand there, because the history would otherwise end
# with the model's own words, and an Anthropic model takes no request that ends that way.
_NO_WORDS_NOTE = "The user made a sound there, with no words in it."

# A trailing fragment longer than this many words is never treated as noise.
_STRAY_MAX_WORDS = 2
# Nor is one that starts later than this after the user's sentence ended, once a reply
# to that sentence has been heard. Past that it is taken as the user speaking.
_STRAY_WINDOW_SECONDS = 3.0
# Latin, Devanagari and CJK sentence endings (the last two are the full-width ? and !).
_SENTENCE_ENDS = (".", "?", "!", "।", "。", "\uff1f", "\uff01")

# How often the wait for a quiet moment looks again. Playback can be cut short or added
# to while it waits, so it does not sleep through to a time worked out in advance.
_QUIET_CHECK_SECONDS = 0.1


class VoiceEvent(BaseModel):
    """One JSON message for the client. Speech audio travels separately, as binary.

    - `user.partial`    the user's words so far, while they are still speaking
    - `user.final`      the user's finished sentence; a reply follows
    - `assistant.delta` the next piece of the reply's text
    - `assistant.done`  the reply's audio has all been sent (full text in `text`)
    - `tool.call`       the assistant is searching the web (query in `text`)
    - `interrupt`       the user started speaking: stop playback and drop queued audio.
                        If that cut a reply short, `text` is what the history keeps of
                        it (just "…" if nothing); otherwise `text` is empty
    - `error`           something failed (message in `text`)
    """

    type: Literal[
        "user.partial",
        "user.final",
        "assistant.delta",
        "assistant.done",
        "tool.call",
        "interrupt",
        "error",
    ]
    text: str = ""


class _Reply(Reply):
    """One reply of the call's, delivered as speech."""

    def __init__(self, speech: Speech, *, brings_results: bool = False) -> None:
        super().__init__(speech)
        self.speech = speech
        # Whether this reply is the one that tells the user of a late search result.
        self.brings_results = brings_results


class VoiceConversation:
    """One live spoken conversation. Feed it mic audio; read events and speech audio."""

    def __init__(
        self,
        *,
        stt: CartesiaSTTSession,
        tts: CartesiaTTSAdapterClass,
        llm: LLM,
        search: ParallelAdapterClass,
        gem: GemConfig,
        darbar: str | None = None,
        model: ModelName = "6 Luna",
        speaks_first: bool = False,
        clock: Clock = time.monotonic,
    ) -> None:
        self._stt = stt
        self._tts = tts
        self._clock = clock
        self._playback = Playback(clock)
        # Everything the model sees, oldest first. It only ever grows at the end, and
        # each entry is final when it is added.
        self._history: list[Item] = []
        # The user's turn that has not been answered yet, as the pieces it arrived in.
        # It joins the history when some of the reply to it has been heard.
        self._turn: list[str] = []
        # Whether the turn the user has just opened cut a reply short, and whether any
        # of that reply had been heard.
        self._cut_a_reply = False
        self._cut_after_hearing = False
        # For telling noise from speech: the user's last words and when they ended, when
        # the turn now open began, and every writing system they have spoken in so far.
        self._said = ""
        self._said_at = 0.0
        self._turn_began_at = 0.0
        self._user_scripts: set[str] = set()
        self._out: asyncio.Queue[VoiceEvent | bytes | None] = asyncio.Queue()
        # Tool calls that outlived their replies, until their results are told. A result
        # that comes in is told at the next quiet moment.
        self._late = LateResults(self._announce_when_quiet)
        # `llm` has to be the adapter of the provider that `model` belongs to.
        # The gem is offered only the tools its config names. A name with no tool behind
        # it here is passed over, so a config saved before a tool went still opens a call.
        offers_search = search.configured and "web_search" in gem.tools
        self._tools = Toolbox([web_search_tool(search), self._late.tool] if offers_search else [])
        self._model = model_for(llm, model, self._tools.specs)
        # The instructions and the tool list are the start of every request, so they are
        # fixed for the whole conversation: a prompt cache only helps while that start
        # stays the same. How they are put together is in `instructions.py`.
        # The Darbar text is the one read when the call opened: a change to the setting
        # reaches the next call, never this one.
        self._instructions = instructions(
            darbar=darbar,
            gem=gem,
            model_needs=self._model.own_instructions,
        )
        # The task producing a reply, while one is being produced.
        self._producing: asyncio.Task[None] | None = None
        # The latest reply, until it joins the history. That happens when the user next
        # speaks, or when a late tool result is about to be answered: by then it has
        # either been heard in full or is being cut off.
        self._reply: _Reply | None = None
        self._user_speaking = False
        # Whether a late result is in the history that the user has heard no reply to
        # and has not spoken since. The reply about it is then made again.
        self._results_owed = False
        self._announcing: asyncio.Task[None] | None = None
        # The speech connection for the next reply, opened before it is needed.
        self._standby = Standby(tts, clock)
        self._standby.ready()
        self._listening = asyncio.create_task(self._listen())
        if speaks_first:
            # Otherwise nothing is said until the user has spoken.
            self._history.append({"role": "developer", "content": _GREETING_NOTE})
            self._producing = asyncio.create_task(self._respond())

    async def send_audio(self, pcm: bytes) -> None:
        """Feed mic audio: raw 16-bit mono PCM at 16 kHz."""
        await self._stt.send_audio(pcm)

    def set_output_delay(self, seconds: float) -> None:
        """Take the client's report of its sound device's delay (see `Playback`)."""
        self._playback.set_output_delay(seconds)

    async def outputs(self) -> AsyncIterator[VoiceEvent | bytes]:
        """Yield events and speech audio (raw 16-bit mono PCM at 24 kHz) in order."""
        while (item := await self._out.get()) is not None:
            yield item

    async def close(self) -> None:
        """End the conversation: stop any reply and release the speech connections."""
        # First, so that no result coming in from here on starts a reply.
        outliving = self._late.stop()
        tasks = [
            task
            for task in (self._listening, self._producing, self._announcing)
            if task is not None
        ]
        calls = self._reply.calls() if self._reply else []
        others = [call.running for call in calls if call.running is not None]
        for task in (*tasks, *others):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*others, *outliving, return_exceptions=True)
        await self._standby.close()
        with contextlib.suppress(Exception):
            await self._stt.close()

    def _emit(self, type_: str, text: str = "") -> None:
        self._out.put_nowait(VoiceEvent.model_validate({"type": type_, "text": text}))

    async def _listen(self) -> None:
        """Follow the user's turns for the life of the conversation."""
        try:
            async for event in self._stt.events():
                if event.type == "turn.start":
                    self._user_speaking = True
                    self._turn_began_at = self._clock()
                    await self._interrupt()
                    # A reply will be wanted when they finish: have its connection open.
                    self._standby.ready()
                elif event.type == "turn.update":
                    self._emit("user.partial", event.transcript)
                elif event.type == "turn.end":
                    await self._start_reply(event.transcript.strip())
                elif event.type == "error":
                    self._emit("error", event.message or "speech recognition failed")
        except Exception as exc:
            self._emit("error", f"speech recognition stopped: {exc}")
        finally:
            self._out.put_nowait(None)

    async def _interrupt(self) -> None:
        """The user is speaking: stop the reply, and record how much of it they heard."""
        producing, self._producing = self._producing, None
        if self._announcing is not None:
            self._announcing.cancel()
            self._announcing = None
        heard = self._settle()
        kept = heard.kept if heard is not None else None
        self._cut_a_reply = kept is not None
        self._cut_after_hearing = heard is not None and heard.any_text
        if producing is not None:
            producing.cancel()
        self._playback.reset()
        # Sent every time: the server cannot see the client's playback queue. Its text is
        # empty only when no reply was cut, so a reply cut before its first word shows
        # as the bare mark.
        self._emit("interrupt", "" if kept is None else kept or CUT_MARK)
        if producing is not None:
            await asyncio.gather(producing, return_exceptions=True)

    def _settle(self) -> Delivered | None:
        """Move the latest reply into the history as the user heard it, if there is one."""
        reply, self._reply = self._reply, None
        if reply is None:
            return None
        reply.speech.silence()
        for call in reply.calls():
            self._late.settle(call)
        heard = reply.delivered(self._playback.hearing())
        if reply.brings_results and (heard.any_text or (reply.finished and heard.kept is None)):
            # Heard at least in part, or produced in full and played to its end: the
            # user has been told. (One that was cut off or failed before it said
            # anything also has nothing "cut", but has told them nothing.)
            self._results_owed = False
        if heard.items and self._turn:
            # Some of the reply got through, so the turn it answers is closed.
            self._history.append({"role": "user", "content": "\n".join(self._turn)})
            self._turn = []
        self._history.extend(heard.items)
        return heard

    def _announce_when_quiet(self) -> None:
        """Make sure something is waiting to give the model its late results."""
        waiting = self._announcing is not None and not self._announcing.done()
        owed = self._late.ready or self._results_owed
        if owed and not self._user_speaking and not waiting:
            self._announcing = asyncio.create_task(self._announce_late_results())

    async def _announce_late_results(self) -> None:
        """Wait until nothing is being said, then have the model tell the user of late results.

        Nothing is being said once the reply in progress has been produced and played to
        the end. Only then is that reply final, so only then may another request be made.
        If the user speaks first, this is cancelled, and it starts again after they have
        been answered.

        If a noise cuts that reply off before any of it is heard, the history is as it
        was, so the same request is simply made again. If the user speaks first, what
        they said is answered, and telling them of the result is left to the model.
        """
        while True:
            if self._producing is not None and not self._producing.done():
                await asyncio.wait({self._producing})
            elif self._playback.ends_at > self._clock():
                await asyncio.sleep(_QUIET_CHECK_SECONDS)
            else:
                break
        self._settle()
        if self._late.ready:
            self._add_late_results()
            self._results_owed = True
        elif not self._results_owed:
            return  # the user has been told in the meantime
        self._producing = asyncio.create_task(self._respond(brings_results=True))

    def _close_turn(self) -> None:
        """Move the user's unanswered turn into the history, where it becomes final.

        Whatever is added next follows it, and what the user says next is a new turn.
        """
        if self._turn:
            self._history.append({"role": "user", "content": "\n".join(self._turn)})
            self._turn = []

    def _add_late_results(self) -> None:
        """Add each waiting result to the history, as a stub call followed by the result."""
        # An unanswered turn has already been sent to the model: the results follow it.
        self._close_turn()
        for late in self._late.take():
            self._history.extend(late.stub.items())

    async def _start_reply(self, transcript: str) -> None:
        """A turn ended: answer it, or put back what it interrupted if nothing was said."""
        if not self._user_speaking:
            # The turn ended without the start that normally comes first, so this is the
            # first that is known of it.
            self._turn_began_at = self._clock()
            if self._producing is not None or self._reply is not None:
                self._user_speaking = True
                await self._interrupt()
        # The user holds the floor until it is settled what follows their turn, so that a
        # late result cannot start a reply of its own in the middle of this.
        self._user_speaking = True
        transcript = _without_stray_tail(transcript, self._user_scripts)
        said = bool(transcript) and not self._is_noise(transcript)
        self._user_speaking = False
        # The reply that was to tell the user of a late result, cut off unheard, is not
        # "picked up where it stopped": it is made again, by the same path as before.
        retell = self._results_owed and not said
        if not said and not self._turn and (not self._cut_a_reply or retell):
            # Nothing was said and nothing heard was stopped: late results may be waiting.
            self._announce_when_quiet()
            return
        if said:
            # The result is in front of the model along with what the user now says.
            self._results_owed = False
            self._turn.append(transcript)
            self._said, self._said_at = transcript, self._clock()
            self._user_scripts |= _scripts(transcript)
            self._emit("user.final", transcript)
        elif self._cut_after_hearing:
            # Nothing was said, yet it stopped a reply that was being heard, which now
            # ends the history. (A reply cut before any of it was heard left the history
            # as it was, and is simply made again.)
            self._history.append({"role": "developer", "content": _NO_WORDS_NOTE})
        self._producing = asyncio.create_task(self._respond())
        # A result that came in while the user was speaking is not slipped in with their
        # turn: the model would answer what they said and never mention it. It waits
        # until this reply has been heard, and is then answered in a reply of its own.
        self._announce_when_quiet()

    async def _respond(self, *, brings_results: bool = False) -> None:
        """Produce one reply: model rounds (with searches in between), spoken as they stream."""
        speech = Speech(self._tts, self._out.put_nowait, self._playback, self._standby.take())
        reply = _Reply(speech, brings_results=brings_results)
        self._reply = reply
        try:
            await run_reply(
                reply,
                self._model,
                self._tools,
                instructions=self._instructions,
                next_round=self._next_round,
                said=lambda text: self._emit("assistant.delta", text),
                show=self._show_call,
            )
        except Exception as exc:
            self._emit("error", f"reply failed: {exc}")
        else:
            self._emit("assistant.done", reply.speech.text.strip())

    def _next_round(self) -> RoundInput:
        """What the reply's next model call is sent ahead of the reply's own items."""
        # No thinking before it speaks: in a conversation the wait is worse than the gain.
        # The exception is while the history ends in a search with no result yet: one
        # still running in the background, or finished and waiting to be delivered.
        # Asked without thinking, the OpenAI model searched for the same thing again in a
        # third to a half of measured runs where the user only said "hello?". With a
        # little thinking it was about one in twenty. That costs about half a second at
        # the median and 1.5 s for the slowest tenth, on every request made while this
        # holds. An Anthropic model is not asked to: see `model.py`.
        return RoundInput([*self._history, *self._open_turn()], think=self._late.pending)

    def _is_noise(self, transcript: str) -> bool:
        """Whether a turn's words are a stray fragment trailing the user's last sentence."""
        trailing = bool(self._turn) or (
            self._turn_began_at - self._said_at <= _STRAY_WINDOW_SECONDS
        )
        return trailing and _is_stray(transcript, self._said, self._user_scripts)

    def _open_turn(self) -> list[Item]:
        """The user's unanswered turn as one message, for a model call."""
        return [{"role": "user", "content": "\n".join(self._turn)}] if self._turn else []

    def _show_call(self, text: str) -> None:
        """Tell the client what a tool call is doing, such as what a search looks up."""
        self._emit("tool.call", text)


class VoiceEntityClass:
    def __init__(
        self,
        *,
        stt: CartesiaSTTAdapterClass,
        tts: CartesiaTTSAdapterClass,
        openai: OpenAIAdapterClass,
        anthropic: AnthropicAdapterClass,
        search: ParallelAdapterClass,
    ) -> None:
        self._stt = stt
        self._tts = tts
        self._openai = openai
        self._anthropic = anthropic
        self._search = search

    def models(self) -> list[tuple[ModelInfo, bool, bool]]:
        """Every model, with whether its provider has a key and whether it is the default."""
        ready = {"OpenAI": self._openai.configured, "Anthropic": self._anthropic.configured}
        return [(info, ready[info.provider], info.name == settings.voice_model) for info in MODELS]

    async def open_conversation(
        self,
        gem: GemConfig,
        *,
        speaks_first: bool | None = None,
        darbar: str | None = None,
    ) -> VoiceConversation:
        """Start listening. The caller owns the conversation and must `close()` it.

        The conversation is with `gem`: it says who the assistant is, which tools it may
        use, and which model answers, or the configured one if it names none. With
        `speaks_first` the gem greets the user as soon as the conversation opens, or
        waits for the user to speak; left out, the gem's config says which. `darbar` is
        the Darbar text the gem is told first, or None for the built-in one; it is kept
        for the whole conversation. Raises `LookupError` if the model's provider has no
        key.
        """
        model = gem.model or settings.voice_model
        if speaks_first is None:
            speaks_first = gem.speaks_first
        llm: LLM = self._openai if is_openai(model) else self._anthropic
        if not llm.configured:
            # Said now, not as a failed reply after the user has spoken.
            provider = "OpenAI" if is_openai(model) else "Anthropic"
            raise LookupError(f"no {provider} key is set, so {model} cannot be used")
        stt = await self._stt.connect_auto_turns()
        return VoiceConversation(
            stt=stt,
            tts=self._tts,
            llm=llm,
            search=self._search,
            gem=gem,
            darbar=darbar,
            model=model,
            speaks_first=speaks_first,
        )


def _is_stray(fragment: str, sentence_before: str, scripts_spoken: set[str]) -> bool:
    """Whether a fragment that trails a sentence is more likely noise than speech.

    Speech recognition is not tied to one language. It can turn a cough or a door into a
    word or two in some other script, which then arrives just after a finished sentence.

    Dropping a real word is the worse mistake: the reply it interrupted is resumed, so
    someone who said "stop" would be talked over. So this is narrow. Only a fragment of
    a word or two counts, only in a writing system this user has not spoken in at any
    point in the conversation, and only after a sentence that was finished.
    """
    if not sentence_before.rstrip().endswith(_SENTENCE_ENDS):
        return False
    if len(fragment.split()) > _STRAY_MAX_WORDS:
        return False
    scripts = _scripts(fragment)
    return bool(scripts) and scripts.isdisjoint(scripts_spoken)


def _without_stray_tail(transcript: str, scripts_spoken: set[str]) -> str:
    """Drop a stray fragment that the recognizer has run on to the end of a sentence.

    A noise that comes too soon after a sentence to be a turn of its own arrives inside
    the same transcript, as in "What is the capital of France? हूँ". The same narrow rule
    applies as for a fragment that arrives separately.
    """
    ends = re.escape("".join(_SENTENCE_ENDS))
    split = re.match(rf"(?s)(.*[{ends}])\s+(\S+(?:\s+\S+)?)\s*$", transcript)
    if split is None:
        return transcript
    sentence, tail = split.groups()
    spoken_in = _scripts(sentence)
    # A "sentence" with no letters in it is just punctuation: nothing to compare with.
    stray = bool(spoken_in) and _is_stray(tail, sentence, scripts_spoken | spoken_in)
    return sentence if stray else transcript


# Japanese is written in three scripts at once, so they count as one writing system.
_SAME_WRITING_SYSTEM = {"HIRAGANA": "CJK", "KATAKANA": "CJK", "KATAKANA-HIRAGANA": "CJK"}


def _scripts(text: str) -> set[str]:
    """The writing systems used in a text, by the first word of each letter's Unicode name."""
    # Full-width and half-width forms are folded first: a full-width "OK" is Latin, not a
    # new script.
    names = {
        unicodedata.name(letter, "?").split()[0]
        for letter in unicodedata.normalize("NFKC", text)
        if letter.isalpha()
    }
    return {_SAME_WRITING_SYSTEM.get(name, name) for name in names}

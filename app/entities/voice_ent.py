"""
Voice entity: a spoken conversation as a cascade of three services.

    mic audio ──▶ speech-to-text ──▶ model ──▶ text-to-speech ──▶ speech audio
                  (Cartesia)         (OpenAI or (Cartesia)
                                      Anthropic)

One `VoiceConversation` per call. It starts from the gem's conversation as it is stored,
since a gem has one conversation whether the user calls it or writes to it. Each thing
it adds to the history it hands to the gem's log at that moment, to be remembered (see
`gem_log.py`); whoever holds the call sees that it is stored (see `call_ent.py`). The
speech-to-text service also detects turns:

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
whole of the one before it.

What the user says joins the history when they have said it, as an item of its own.
Words that arrive before any of the reply to them has been heard are one more user item
after it: speech recognition often splits one sentence in two, and the model reads the
two halves together. The rule is the same in writing, and in one place (`history.py`).

A tool call is not stopped by an interruption. If the user speaks while one is running,
it carries on, as it does in writing (see `late_results.py`). When its real result comes
in, it is added at the next quiet moment, and the model tells the user about it in a
reply of its own, without waiting to be asked. If the user's turn ends while a result is
waiting, the result is added ahead of what they said, and one reply answers them and
tells the result. The same goes for a tool call left running by a written reply before
the call: the gem has one set of calls that outlive their replies. A call still running
when the call ends carries on too, and its result is told in writing.
"""

import asyncio
import contextlib
import re
import time
import unicodedata
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from app.adapters.anthropic_adp import AnthropicAdapterClass
from app.adapters.cartesia_stt_adp import CartesiaSTTAdapterClass, CartesiaSTTSession
from app.adapters.cartesia_tts_adp import CartesiaTTSAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass
from app.adapters.parallel_adp import ParallelAdapterClass
from app.config import settings
from app.entities.gem_ent import GemConfig
from app.entities.gem_log import Recording, TurnRecord
from app.entities.history import (
    Added,
    Mode,
    TimeTold,
    added_reply,
    line,
    mode_line,
    of_turn,
    said,
    time_line,
    told,
)
from app.entities.instructions import instruction_layers, joined
from app.entities.late_results import LateResults
from app.entities.loop import CUT_MARK, Delivered, Reply, RoundInput, ToolCall, run_reply
from app.entities.model import (
    LLM,
    MODELS,
    Item,
    ModelInfo,
    ModelName,
    is_openai,
    model_for,
)
from app.entities.tools import Tool, gem_toolbox
from app.entities.voice_speech import Clock, Playback, Speech, Standby

__all__ = ["VoiceConversation", "VoiceEntityClass", "VoiceEvent", "VoiceLine"]

# Opens a call in which the assistant is to speak first, after the line that says the
# user has called, if there is one. The model's own words never open a call: its
# opening line is an answer to this.
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
    - `assistant.word`  the next word of the reply, as it is spoken. `start` is the
                        sample it begins at in the call's speech audio (24 kHz, counted
                        over all the speech sent since the call opened)
    - `assistant.done`  the reply's audio has all been sent (full text in `text`)
    - `tool.call`       the assistant is using a tool (`text` says what: a search's query)
    - `interrupt`       the user started speaking: stop playback and drop queued audio.
                        If that cut a reply short, `text` is what the history keeps of
                        it (just "…" if nothing); otherwise `text` is empty
    - `error`           something failed (message in `text`)
    """

    type: Literal[
        "user.partial",
        "user.final",
        "assistant.word",
        "assistant.done",
        "tool.call",
        "interrupt",
        "error",
    ]
    text: str = ""
    # Only an `assistant.word` has one.
    start: int | None = None


class _Reply(Reply):
    """One reply of the call's, delivered as speech."""

    def __init__(self, speech: Speech, turn: TurnRecord, *, brings_results: bool = False) -> None:
        super().__init__(speech)
        self.speech = speech
        # Where what the reply does is written down.
        self.turn = turn
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
        history: Sequence[Item] = (),
        sent: Callable[[bool], Sequence[Item]] | None = None,
        last_mode: Mode = "chat",
        late: LateResults | None = None,
        connected: Sequence[Tool] = (),
        clock: Clock = time.monotonic,
        record: Recording | None = None,
        time_told: TimeTold | None = None,
    ) -> None:
        # `record` is where what happens in the call is written down (see `gem_log.py`).
        # Left out, nothing is. `time_told` holds the user's time zone, for the line that
        # gives the time after the call's opening line; left out, no such line is written.
        self._record = Recording(mode="call") if record is None else record
        # `history` is the gem's conversation as it is stored, and `last_mode` the mode
        # of its last item ("chat" if it is empty). `late` holds the gem's tool calls
        # that outlive their replies, those of written replies included; whoever holds
        # it calls `results_came` when a result comes in while the call is open. Left
        # out, the conversation holds its own, and stops its calls when it closes.
        # `connected` are the tools of the connections the gem may use, as they were when
        # the call opened. `sent`, if given, is asked once the conversation is described,
        # with whether anything changed, and gives the history as the model is to be
        # sent it (see `cold_start.py`). Left out, it is sent `history` as it is.
        self._stt = stt
        self._tts = tts
        self._clock = clock
        self._playback = Playback(clock)
        # Everything the model sees, oldest first. It only ever grows at the end, and
        # each entry is final when it is added.
        self._history: list[Item] = list(history)
        # Everything the call added to the history, in order, for the caller to store.
        # `grew` is set each time something is added.
        self.added: list[Added] = []
        self.grew = asyncio.Event()
        # The tool calls left running when their replies were cut, in order. Each has the
        # placeholder as its result in the history, and is held by `late`.
        self.left_running: list[ToolCall] = []
        self._closed = False
        # Whether the user has said something that no reply has got through to yet: none
        # of a reply to it has been heard, and none called a tool.
        self._unanswered = False
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
        # that comes in is told at the next quiet moment, or with the user's turn if it
        # ends first.
        self._owns_late = late is None
        self._late = LateResults(self.results_came) if late is None else late
        # `llm` has to be the adapter of the provider that `model` belongs to. The gem is
        # offered the tools its config names and those of its connections, as in
        # writing (see `tools.py`).
        self._tools = gem_toolbox(gem.tools, search, self._late.tool, self._history, connected)
        self._model = model_for(llm, model, self._tools.specs)
        # The instructions and the tool list are the start of every request, so they are
        # fixed for the whole conversation: a prompt cache only helps while that start
        # stays the same. How they are put together is in `instructions.py`.
        # The Darbar text is the one read when the call opened: a change to the setting
        # reaches the next call, never this one.
        layers = instruction_layers(
            darbar=darbar,
            gem=gem,
            model_needs=self._model.own_instructions,
        )
        self._instructions = joined(layers)
        # Said once, before the call's opening line is added: what differs from what was
        # last written down is written now, ahead of anything the call remembers. Only
        # the way to the provider can change while the call is open.
        changed = self._record.describe(
            model=model,
            via=lambda: getattr(llm, "via", "api"),
            layers=layers,
            tools=self._tools.tools,
        )
        if sent is not None:
            # Checked here, once: the call is sent its history so from start to end.
            self._history = list(sent(changed))
        # The task producing a reply, while one is being produced.
        self._producing: asyncio.Task[None] | None = None
        # The turn opened in the log for a reply whose task has not begun yet.
        self._unstarted: TurnRecord | None = None
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
        # The instructions say a line in the conversation marks when a call starts. Every
        # call opens with it, whatever the conversation held before.
        self._add(*mode_line("call", last_mode))
        if time_told is not None:
            self._add(time_line(time_told.zone))
        self._listening = asyncio.create_task(self._listen())
        if speaks_first:
            # Otherwise nothing is said until the user has spoken.
            turn = self._record.turn("gem")
            self._add(*of_turn([line(_GREETING_NOTE, "greeting")], turn.number))
            self._unstarted = turn
            self._producing = asyncio.create_task(self._respond(turn))
        # A result that came in before the call opened is told in it.
        self._announce_when_quiet()

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

    def results_came(self) -> None:
        """A late result has come in: told at the next quiet moment, or with the user's turn."""
        self._announce_when_quiet()

    async def close(self) -> None:
        """End the conversation: stop any reply and release the speech connections.

        The latest reply joins the history as it was heard, as when the user speaks over
        it. A tool call it left running carries on, with the placeholder as its result,
        unless the conversation holds its own late results: it then stops every call.
        """
        # First, so that no result coming in from here on starts a reply.
        self._closed = True
        tasks = [
            task
            for task in (self._listening, self._producing, self._announcing)
            if task is not None
        ]
        # Settled before it is stopped, as when the user speaks.
        self._settle()
        outliving = self._late.stop() if self._owns_late else []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*outliving, return_exceptions=True)
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
            # A reply asked for and stopped before it began has its turn open in the
            # log. Whoever settles is about to stop it, so the turn is closed here.
            unstarted, self._unstarted = self._unstarted, None
            if unstarted is not None:
                unstarted.closed(finished=False, kept=None)
            return None
        reply.speech.silence()
        for call in reply.calls():
            if self._late.settle(call):
                self.left_running.append(call)
                reply.turn.backgrounded(call)
        heard = reply.delivered(self._playback.hearing())
        if reply.brings_results and (heard.any_text or (reply.finished and heard.kept is None)):
            # Heard at least in part, or produced in full and played to its end: the
            # user has been told. (One that was cut off or failed before it said
            # anything also has nothing "cut", but has told them nothing.)
            self._results_owed = False
        self._keep(reply, heard.items)
        reply.turn.closed(finished=reply.finished, kept=heard.kept)
        return heard

    def _keep(self, reply: _Reply, items: list[Item]) -> None:
        """Add what a reply leaves in the history: these items, final as they stand."""
        if not items:
            return
        # Something of the reply got through: what the user said has been answered.
        self._unanswered = False
        kept = added_reply(items, reply.calls(), datetime.now(UTC))
        self._add(*of_turn(kept, reply.turn.number))

    def _announce_when_quiet(self) -> None:
        """Make sure something is waiting to give the model its late results."""
        waiting = self._announcing is not None and not self._announcing.done()
        owed = self._late.ready or self._results_owed
        if owed and not self._user_speaking and not waiting and not self._closed:
            self._announcing = asyncio.create_task(self._announce_late_results())

    async def _announce_late_results(self) -> None:
        """Wait until nothing is being said, then have the model tell the user of late results.

        Nothing is being said once the reply in progress has been produced and played to
        the end. Only then is that reply final, so only then may another request be made.
        If the user speaks first, this is cancelled: when their turn ends, results still
        waiting go in ahead of what they said.

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
        if not self._late.ready and not self._results_owed:
            return  # the user has been told in the meantime
        # The turn of the reply that tells them. Nothing is awaited from here on, so
        # the reply is either begun or, if it is stopped first, its turn is closed.
        turn = self._record.turn("background_result")
        if self._late.ready:
            self._add_late_results(turn)
            self._results_owed = True
        self._unstarted = turn
        self._producing = asyncio.create_task(self._respond(turn, brings_results=True))

    def _add_late_results(self, turn: TurnRecord) -> None:
        """Add each waiting result to the history, as a stub call followed by the result.

        What the user said before them has already been sent to the model: the results
        follow it, and what the user says next is a new turn after them. `turn` is the
        turn of the reply that tells them.
        """
        results = of_turn(told(self._late.take()), turn.number)
        if results:
            self._unanswered = False
        self._add(*results)

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
        spoke = bool(transcript) and not self._is_noise(transcript)
        self._user_speaking = False
        # The reply that was to tell the user of a late result, cut off unheard, is not
        # "picked up where it stopped": it is made again, by the same path as before.
        retell = self._results_owed and not spoke
        if not spoke and not self._unanswered and (not self._cut_a_reply or retell):
            # Nothing was said and nothing heard was stopped: late results may be waiting.
            self._announce_when_quiet()
            return
        # A reply follows from here on, whatever it answers.
        turn = self._record.turn("user")
        if spoke:
            # The result is in front of the model along with what the user now says.
            self._results_owed = False
            # Results waiting go in ahead of their words, so the one reply that answers
            # them tells the results too.
            if self._late.ready:
                self._add_late_results(turn)
            # Their words are final as they stand, so they join the history now.
            self._unanswered = True
            self._add(said(transcript, turn=turn.number))
            self._said, self._said_at = transcript, self._clock()
            self._user_scripts |= _scripts(transcript)
            self._emit("user.final", transcript)
        elif self._cut_after_hearing:
            # Nothing was said, yet it stopped a reply that was being heard, which now
            # ends the history. (A reply cut before any of it was heard left the history
            # as it was, and is simply made again.)
            self._add(*of_turn([line(_NO_WORDS_NOTE, "no-words")], turn.number))
        self._unstarted = turn
        self._producing = asyncio.create_task(self._respond(turn))
        # A result that comes in from here on, or one left waiting by a turn with no
        # words, waits until this reply has been heard, and is told in a reply of its own.
        self._announce_when_quiet()

    async def _respond(self, turn: TurnRecord, *, brings_results: bool = False) -> None:
        """Produce one reply: model rounds (with searches in between), spoken as they stream.

        `turn` is the reply's turn in the gem's log, opened by whoever asked for the
        reply.
        """
        # It has begun: from here on its turn is closed when the reply is settled.
        if self._unstarted is turn:
            self._unstarted = None
        speech = Speech(
            self._tts,
            self._out.put_nowait,
            self._playback,
            self._standby.take(),
            word=self._show_word,
        )
        reply = _Reply(speech, turn, brings_results=brings_results)
        self._reply = reply
        try:
            await run_reply(
                reply,
                self._model,
                self._tools,
                instructions=self._instructions,
                next_round=lambda: self._next_round(reply),
                # The client is not shown the model's text, which runs well ahead of
                # the audio. It is sent each word as it is spoken (see `_show_word`).
                said=lambda text: None,
                show=self._show_call,
                record=turn,
            )
        except Exception as exc:
            turn.failed(exc)
            self._emit("error", f"reply failed: {exc}")
        else:
            self._emit("assistant.done", reply.speech.text.strip())

    def _next_round(self, reply: _Reply) -> RoundInput:
        """What the reply's next model call is sent: the history, with the reply so far."""
        # What the reply has produced so far is final now: the model is about to be
        # sent it. So it joins the history here, before the request that carries it.
        # Each of its calls has its result by now: settling one says what that stands for.
        for call in reply.calls():
            self._late.settle(call)
        self._keep(reply, reply.send_back())
        # No thinking before it speaks: in a conversation the wait is worse than the gain.
        # The exception is while the history ends in a search with no result yet: one
        # still running in the background, or finished and waiting to be delivered.
        # Asked without thinking, the OpenAI model searched for the same thing again in a
        # third to a half of measured runs where the user only said "hello?". With a
        # little thinking it was about one in twenty. That costs about half a second at
        # the median and 1.5 s for the slowest tenth, on every request made while this
        # holds. An Anthropic model is not asked to: see `model.py`.
        return RoundInput(list(self._history), think=self._late.pending)

    def _is_noise(self, transcript: str) -> bool:
        """Whether a turn's words are a stray fragment trailing the user's last sentence."""
        trailing = self._unanswered or (
            self._turn_began_at - self._said_at <= _STRAY_WINDOW_SECONDS
        )
        return trailing and _is_stray(transcript, self._said, self._user_scripts)

    def _add(self, *added: Added) -> None:
        """Add items to the history, where they are final, and have the gem remember them."""
        for one in added:
            self._history.append(one.item)
            self.added.append(one)
            self._record.remember(one)
        if added:
            self.grew.set()

    def _show_word(self, text: str, start: int) -> None:
        """Send the client one word of the reply, and where it starts in the call's audio."""
        self._out.put_nowait(VoiceEvent(type="assistant.word", text=text, start=start))

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

    async def connect(self, gem: GemConfig, *, speaks_first: bool | None = None) -> "VoiceLine":
        """Connect speech recognition for a call with `gem`, before its conversation opens.

        The call answers with the gem's model, or the configured one if it names none.
        With `speaks_first` the gem greets the user as soon as the conversation opens, or
        waits for the user to speak; left out, the gem's config says which. Raises
        `LookupError` if the model's provider has no key: said now, not as a failed
        reply after the user has spoken. The caller owns the line: it opens the
        conversation on it, or closes it.
        """
        model = gem.model or settings.voice_model
        llm: LLM = self._openai if is_openai(model) else self._anthropic
        if not llm.configured:
            provider = "OpenAI" if is_openai(model) else "Anthropic"
            raise LookupError(f"no {provider} key is set, so {model} cannot be used")
        stt = await self._stt.connect_auto_turns()
        return VoiceLine(
            stt=stt,
            tts=self._tts,
            llm=llm,
            search=self._search,
            gem=gem,
            model=model,
            speaks_first=gem.speaks_first if speaks_first is None else speaks_first,
        )


class VoiceLine:
    """Speech recognition connected for a call, before its conversation opens."""

    def __init__(
        self,
        *,
        stt: CartesiaSTTSession,
        tts: CartesiaTTSAdapterClass,
        llm: LLM,
        search: ParallelAdapterClass,
        gem: GemConfig,
        model: ModelName,
        speaks_first: bool,
    ) -> None:
        self._stt = stt
        self._tts = tts
        self._llm = llm
        self._search = search
        self._gem = gem
        self._model: ModelName = model
        self._speaks_first = speaks_first
        self._opened = False

    def open(
        self,
        *,
        darbar: str | None = None,
        history: Sequence[Item] = (),
        sent: Callable[[bool], Sequence[Item]] | None = None,
        last_mode: Mode = "chat",
        late: LateResults | None = None,
        connected: Sequence[Tool] = (),
        record: Recording | None = None,
        time_told: TimeTold | None = None,
    ) -> VoiceConversation:
        """Start listening. The conversation owns the line from here, and the caller owns it.

        `darbar` is the Darbar text the gem is told first, or None for the built-in one;
        it is kept for the whole conversation. `history`, `sent`, `last_mode`, `late`,
        `connected`, `record` and `time_told` are as `VoiceConversation` takes them.
        """
        self._opened = True
        return VoiceConversation(
            stt=self._stt,
            tts=self._tts,
            llm=self._llm,
            search=self._search,
            gem=self._gem,
            darbar=darbar,
            model=self._model,
            speaks_first=self._speaks_first,
            history=history,
            sent=sent,
            last_mode=last_mode,
            late=late,
            connected=connected,
            record=record,
            time_told=time_told,
        )

    async def close(self) -> None:
        """Hang up a line no conversation was opened on."""
        if not self._opened:
            with contextlib.suppress(Exception):
                await self._stt.close()


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

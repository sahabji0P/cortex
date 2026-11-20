"""The voice cascade with all three services faked: turns in, speech and history out.

Time is faked too. The fake text-to-speech makes every word last exactly one second, and
the tests move a clock by hand, so each one can say how much of a reply the user heard.
"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.adapters.cartesia_tts_adp import CartesiaTTSSession, CartesiaTTSWord
from app.entities.gem_ent import GEM_CONFIG_VERSION, GemConfig
from app.entities.voice_ent import VoiceConversation, VoiceEvent, _is_stray, _without_stray_tail
from app.entities.voice_speech import Playback, Speech, Utterance

# The fake speech is 24 kHz 16-bit mono, like the real one.
_BYTES_PER_SECOND = 48000
# Playback starts this long after audio is sent (see voice_speech._CLIENT_DELAY).
_DELAY = 0.15

# The gem every call here is with: a plain stand-in, with no instructions of its own and
# the default tools. What it is told is tested in `test_voice_gem.py`.
_GEM = GemConfig.model_validate(
    {
        "version": GEM_CONFIG_VERSION,
        "name": "Gem One",
        "created_at": datetime(2026, 11, 17, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 17, tzinfo=UTC),
    }
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Item(SimpleNamespace):
    """A model output item, as the OpenAI SDK returns it."""


def _message(text: str) -> _Item:
    return _Item(type="message", role="assistant", content=[{"type": "output_text", "text": text}])


def _search_call(query: str, call_id: str = "call_1") -> _Item:
    return _Item(
        type="function_call",
        call_id=call_id,
        name="web_search",
        arguments=json.dumps({"query": query}),
    )


def _delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def _starts(item: _Item) -> SimpleNamespace:
    return SimpleNamespace(type="response.output_item.added", item=item)


def _completed(*output: _Item) -> SimpleNamespace:
    return SimpleNamespace(type="response.completed", response=SimpleNamespace(output=list(output)))


class _STT:
    """Speech-to-text: the test plays the user by queueing turn events."""

    def __init__(self) -> None:
        self._events: asyncio.Queue[SimpleNamespace | None] = asyncio.Queue()
        self.closed = False

    def turn_start(self) -> None:
        self._events.put_nowait(SimpleNamespace(type="turn.start"))

    def turn_end(self, transcript: str) -> None:
        self._events.put_nowait(SimpleNamespace(type="turn.end", transcript=transcript))

    def say(self, transcript: str) -> None:
        self.turn_start()
        self.turn_end(transcript)

    async def events(self):
        while (event := await self._events.get()) is not None:
            yield event

    async def send_audio(self, audio: bytes) -> None:
        pass

    async def close(self) -> None:
        self.closed = True
        self._events.put_nowait(None)


class _TTSContext:
    """Text-to-speech for one utterance. Every word takes exactly one second to say."""

    def __init__(self, *, dead: bool = False) -> None:
        self._events: asyncio.Queue[bytes | list[CartesiaTTSWord] | None] = asyncio.Queue()
        self._seconds = 0
        self._dead = dead
        self.pushed: list[str] = []
        self.finished = False
        self.cancelled = False

    async def push(self, text: str) -> None:
        if self._dead:
            raise ConnectionError("the connection was closed")
        self.pushed.append(text)
        words = text.split()
        if not words:
            return
        timings = [
            CartesiaTTSWord(word, self._seconds + n, self._seconds + n + 1)
            for n, word in enumerate(words)
        ]
        self._seconds += len(words)
        self._events.put_nowait(timings)
        self._events.put_nowait(b"\x00" * (_BYTES_PER_SECOND * len(words)))

    async def finish(self) -> None:
        self.finished = True
        self._events.put_nowait(None)

    async def events(self):
        while (event := await self._events.get()) is not None:
            yield event

    async def cancel(self) -> None:
        self.cancelled = True


class _TTSSession(CartesiaTTSSession):
    """One reply's text-to-speech connection."""

    def __init__(self, contexts: list[_TTSContext]) -> None:
        self._contexts = contexts
        self.closed = False
        self.dead = False  # dropped by the service while it sat unused

    def context(self) -> _TTSContext:
        context = _TTSContext(dead=self.dead)
        if not self.dead:
            self._contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True


class _TTS:
    def __init__(self) -> None:
        self.sessions: list[_TTSSession] = []
        self.contexts: list[_TTSContext] = []
        self.failures = 0  # how many of the next connections fail to open

    async def connect(self) -> _TTSSession:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("could not reach the speech service")
        self.sessions.append(_TTSSession(self.contexts))
        return self.sessions[-1]


class _LLM:
    """The model: each call plays the next scripted list of stream events.

    A script entry of `asyncio.Event` pauses the stream there until the test sets it.
    """

    def __init__(self, *rounds: list) -> None:
        self._rounds = list(rounds)
        self.requests: list[dict] = []

    async def stream_response(self, **request):
        sent = [dict(item) for item in request["input"]]
        if self.requests:
            _assert_only_grew(self.requests[-1]["input"], sent)
        self.requests.append({**request, "input": sent})
        script = self._rounds.pop(0)

        async def stream():
            for step in script:
                if isinstance(step, asyncio.Event):
                    await step.wait()
                else:
                    yield step

        return stream()


def _assert_only_grew(before: list[dict], after: list[dict]) -> None:
    """Each request has to start with the whole of the one before it.

    Changing anything the model has already been sent would break a prompt cache from
    that point on. One change is allowed: the user's open turn, which is always the last
    item, can gain a line.
    """
    *settled, last = before
    assert after[: len(settled)] == settled, "an earlier item was changed or removed"
    same_slot = after[len(settled)]
    if same_slot != last:
        assert last.get("role") == "user" == same_slot.get("role")
        assert same_slot["content"].startswith(last["content"] + "\n")


class _Search:
    """Web search. With a gate, searches wait for it; `only` limits that to matching queries."""

    def __init__(
        self,
        *,
        configured: bool = True,
        gate: asyncio.Event | None = None,
        only: str = "",
    ) -> None:
        self.configured = configured
        self.queries: list[str] = []
        self._gate = gate
        self._only = only

    async def search(self, *, objective: str, queries: list[str], mode: str):
        assert mode == "turbo"
        self.queries.append(objective)
        if self._gate is not None and self._only in objective:
            await self._gate.wait()
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    title="Race report",
                    url="https://example.com/race",
                    publish_date="2026-11-08",
                    excerpts=["Norris won in Las Vegas."],
                )
            ]
        )


class _Fixture(SimpleNamespace):
    conversation: VoiceConversation
    stt: _STT
    tts: _TTS
    llm: _LLM
    clock: _Clock


def _conversation(
    llm: _LLM, search: _Search | None = None, *, speaks_first: bool = False
) -> _Fixture:
    stt, tts, clock = _STT(), _TTS(), _Clock()
    conversation = VoiceConversation(
        stt=stt,
        tts=tts,
        llm=llm,
        search=search or _Search(),
        gem=_GEM,
        speaks_first=speaks_first,
        clock=clock,
    )
    return _Fixture(conversation=conversation, stt=stt, tts=tts, llm=llm, clock=clock)


async def _read_until(conversation: VoiceConversation, type_: str) -> list[VoiceEvent | bytes]:
    """Collect outputs up to and including the first event of the given type."""
    outputs: list[VoiceEvent | bytes] = []
    async with asyncio.timeout(2):
        async for output in conversation.outputs():
            outputs.append(output)
            if isinstance(output, VoiceEvent) and output.type == type_:
                return outputs
    raise AssertionError(f"conversation ended before {type_!r}")


async def _let_audio_out() -> None:
    """Give the tasks that forward speech a chance to run. The fakes never wait."""
    await asyncio.sleep(0.01)


def _event(outputs: list[VoiceEvent | bytes], type_: str) -> VoiceEvent:
    """The first event of the given type among the outputs."""
    return next(o for o in outputs if isinstance(o, VoiceEvent) and o.type == type_)


def _types(outputs: list[VoiceEvent | bytes]) -> list[str]:
    return ["audio" if isinstance(output, bytes) else output.type for output in outputs]


def _audio_seconds(outputs: list[VoiceEvent | bytes]) -> float:
    return sum(len(output) for output in outputs if isinstance(output, bytes)) / _BYTES_PER_SECOND


# The line a call's history opens with, written out in full.
_CALL_STARTED = {
    "role": "developer",
    "content": "The user has called you. From here on the conversation is spoken.",
}


def _after_the_call_start(request: dict) -> list[dict]:
    """A request's history after the line it opens with, which says a call has started."""
    first, *rest = request["input"]
    assert first == _CALL_STARTED
    return rest


def _shape(items: list[dict]) -> list[str]:
    """History items as short labels: the role of a message, or the type of anything else."""
    return [item.get("role") or item["type"] for item in items]


# ---- replying


async def test_a_finished_turn_is_answered_in_speech() -> None:
    llm = _LLM([_delta("Hi "), _delta("there."), _completed(_message("Hi there."))])
    f = _conversation(llm)

    f.stt.say("hello")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _types(outputs)[:2] == ["interrupt", "user.final"]
    assert _audio_seconds(outputs) == 2
    assert outputs[-1] == VoiceEvent(type="assistant.done", text="Hi there.")
    assert f.tts.contexts[0].finished
    assert f.tts.sessions[0].closed
    assert _after_the_call_start(llm.requests[0]) == [{"role": "user", "content": "hello"}]
    assert llm.requests[0]["store"] is False
    assert llm.requests[0]["model"] == "6 Luna"
    assert llm.requests[0]["reasoning"] == {"effort": "none"}
    await f.conversation.close()


def _words(outputs: list[VoiceEvent | bytes]) -> list[tuple[str, int | None]]:
    """Each word the client was sent, with the sample it starts at in the call's audio."""
    return [
        (output.text, output.start)
        for output in outputs
        if isinstance(output, VoiceEvent) and output.type == "assistant.word"
    ]


_RATE = _BYTES_PER_SECOND // 2  # samples in a second of speech


async def test_a_reply_reaches_the_client_as_words_timed_to_its_audio() -> None:
    llm = _LLM([_delta("Hi "), _delta("there."), _completed(_message("Hi there."))])
    f = _conversation(llm)

    f.stt.say("hello")
    outputs = await _read_until(f.conversation, "assistant.done")

    # The stand-in voice takes a second a word. The model's text is not sent by itself.
    assert _words(outputs) == [("Hi", 0), ("there.", _RATE)]
    assert outputs[-1] == VoiceEvent(type="assistant.done", text="Hi there.")
    await f.conversation.close()


async def test_a_word_is_placed_in_the_audio_of_the_whole_call() -> None:
    call = _search_call("who won the last F1 race")
    llm = _LLM(
        [_delta("Hi there."), _completed(_message("Hi there."))],
        [_delta("One moment."), _starts(call), _completed(_message("One moment."), call)],
        [_delta("Norris won."), _completed(_message("Norris won."))],
    )
    f = _conversation(llm)

    f.stt.say("hello")
    first = await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    f.stt.say("who won the last race")
    second = await _read_until(f.conversation, "assistant.done")

    # The count carries on from one reply to the next, and from the speech before a
    # search to the speech after it: it is every sample sent since the call opened.
    assert _words(first) == [("Hi", 0), ("there.", _RATE)]
    assert _words(second) == [
        ("One", 2 * _RATE),
        ("moment.", 3 * _RATE),
        ("Norris", 4 * _RATE),
        ("won.", 5 * _RATE),
    ]
    assert _audio_seconds(first + second) == 6
    await f.conversation.close()


async def test_the_count_carries_on_past_audio_the_client_dropped() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta(_STORY), never], [_delta("Sure."), _completed(_message("Sure."))])
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.clock.advance(_DELAY + 2.5)
    f.stt.say("stop")
    outputs = await _read_until(f.conversation, "assistant.done")

    # All of the story's audio was sent before the cut, and the client counts what it
    # receives, played or not. So the next reply's word starts after all of it.
    sent_before = len(_STORY.split())
    assert _words(outputs)[-1] == ("Sure.", sent_before * _RATE)
    await f.conversation.close()


async def test_a_word_is_sent_only_after_the_audio_it_starts_in() -> None:
    tts = _TTS()
    sent: list[bytes | tuple[str, int]] = []
    speech = Speech(
        tts,  # type: ignore[arg-type]
        sent.append,
        Playback(_Clock()),
        asyncio.create_task(tts.connect()),
        word=lambda text, start: sent.append((text, start)),
    )
    await speech.say("Hi")
    [context] = tts.contexts
    # The timing of a second word comes in, and half a second of its audio never does.
    context._events.put_nowait([CartesiaTTSWord("there.", 1.5, 2.0)])
    await _let_audio_out()

    assert [one for one in sent if not isinstance(one, bytes)] == [("Hi", 0)]
    assert isinstance(sent[0], bytes)  # the word followed its audio
    await speech.stop()
    assert ("there.", int(1.5 * _RATE)) not in sent


async def test_a_timing_that_comes_after_its_audio_goes_out_at_once() -> None:
    tts = _TTS()
    words: list[tuple[str, int]] = []
    speech = Speech(
        tts,  # type: ignore[arg-type]
        lambda audio: None,
        Playback(_Clock()),
        asyncio.create_task(tts.connect()),
        word=lambda text, start: words.append((text, start)),
    )
    await speech.say("Hi")
    [context] = tts.contexts
    context._events.put_nowait(b"\x00" * _BYTES_PER_SECOND)
    await _let_audio_out()
    assert words == [("Hi", 0)]

    context._events.put_nowait([CartesiaTTSWord("there.", 1.0, 2.0)])
    await _let_audio_out()

    assert words == [("Hi", 0), ("there.", _RATE)]
    # One timed past the end of the audio is placed on its last sample.
    context._events.put_nowait([CartesiaTTSWord("Bye.", 2.5, 3.0)])
    await speech.finish()
    assert words[-1] == ("Bye.", 2 * _RATE - 1)


async def test_a_reply_heard_to_the_end_is_stored_whole() -> None:
    llm = _LLM(
        [_delta("Hi there."), _completed(_message("Hi there."))],
        [_delta("Fine."), _completed(_message("Fine."))],
    )
    f = _conversation(llm)

    f.stt.say("hello")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    f.stt.say("how are you")
    outputs = await _read_until(f.conversation, "assistant.done")

    # Nothing was cut, so the interrupt event carries no heard text.
    assert outputs[0] == VoiceEvent(type="interrupt")
    assert _after_the_call_start(llm.requests[1]) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there."},
        {"role": "user", "content": "how are you"},
    ]
    await f.conversation.close()


async def test_a_model_failure_is_reported_and_the_conversation_goes_on() -> None:
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    llm = _LLM([failed], [_delta("Hi."), _completed(_message("Hi."))])
    f = _conversation(llm)

    f.stt.say("hello")
    outputs = await _read_until(f.conversation, "error")
    assert outputs[-1].text == "reply failed: overloaded"

    f.stt.say("hello again")
    outputs = await _read_until(f.conversation, "assistant.done")
    assert _audio_seconds(outputs) == 1
    await f.conversation.close()


@pytest.mark.parametrize("transcript", ["", "   "])
async def test_an_empty_transcript_starts_no_reply(transcript: str) -> None:
    llm = _LLM()
    f = _conversation(llm)

    f.stt.say(transcript)
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    outputs = await _read_until(f.conversation, "interrupt")

    # Nothing between the two turn starts: no user.final, so no reply was started.
    assert _types(outputs) == ["interrupt"]
    assert llm.requests == []
    await f.conversation.close()


# ---- interrupting: the history keeps what was heard


async def test_speaking_over_a_reply_keeps_only_the_words_that_were_heard() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("Once upon a time there was a dragon."), never],
        [_delta("Sure."), _completed(_message("Sure."))],
    )
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.clock.advance(_DELAY + 3.5)  # three and a half words in
    f.stt.turn_start()
    outputs = await _read_until(f.conversation, "interrupt")

    # The client is told what was heard, straight away.
    assert outputs[-1] == VoiceEvent(type="interrupt", text="Once upon a…")

    f.stt.turn_end("stop, just say sure")
    outputs = await _read_until(f.conversation, "assistant.done")
    # The old reply's speech was cancelled, and the only audio after the interrupt is
    # the new reply's one word.
    assert f.tts.contexts[0].cancelled
    assert f.tts.sessions[0].closed
    assert _audio_seconds(outputs) == 1
    assert _after_the_call_start(llm.requests[1]) == [
        {"role": "user", "content": "tell me a story"},
        {"role": "assistant", "content": "Once upon a…"},
        {"role": "user", "content": "stop, just say sure"},
    ]
    await f.conversation.close()


async def test_a_reply_still_playing_when_the_user_speaks_is_cut_the_same_way() -> None:
    llm = _LLM(
        [_delta("A long story indeed."), _completed(_message("A long story indeed."))],
        [_delta("Sure."), _completed(_message("Sure."))],
    )
    f = _conversation(llm)

    # The reply is finished on the server at once, but takes four seconds to play.
    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(_DELAY + 2.2)
    f.stt.say("stop")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert outputs[0] == VoiceEvent(type="interrupt", text="A long…")
    assert _after_the_call_start(llm.requests[1]) == [
        {"role": "user", "content": "tell me a story"},
        {"role": "assistant", "content": "A long…"},
        {"role": "user", "content": "stop"},
    ]
    await f.conversation.close()


async def test_a_reply_cut_before_any_of_it_played_leaves_no_trace() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("Once upon a time"), never],
        [_delta("Okay."), _completed(_message("Okay."))],
    )
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.stt.say("never mind")
    outputs = await _read_until(f.conversation, "assistant.done")

    # Cut before its first word: the client is told so with the bare mark.
    assert _event(outputs, "interrupt").text == "…"
    # None of the reply was heard, so nothing of it stands between what they said and
    # what they say now: two user items, each as it was said.
    assert _after_the_call_start(llm.requests[1]) == [
        {"role": "user", "content": "tell me a story"},
        {"role": "user", "content": "never mind"},
    ]
    await f.conversation.close()


async def test_a_turn_start_always_tells_the_client_to_stop_playing() -> None:
    llm = _LLM([_delta("Hi."), _completed(_message("Hi."))])
    f = _conversation(llm)

    f.stt.say("hello")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    f.stt.turn_start()
    outputs = await _read_until(f.conversation, "interrupt")

    assert _types(outputs) == ["interrupt"]
    await f.conversation.close()


async def test_closing_mid_reply_releases_both_speech_connections() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta("Once"), never])
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await f.conversation.close()

    assert f.stt.closed
    assert f.tts.contexts[0].cancelled
    assert f.tts.sessions[0].closed


# ---- who speaks first


async def test_by_default_nothing_is_said_until_the_user_has_spoken() -> None:
    llm = _LLM([_delta("Hi."), _completed(_message("Hi."))])
    f = _conversation(llm)

    await asyncio.sleep(0.05)
    assert llm.requests == []

    f.stt.say("hello")
    await _read_until(f.conversation, "assistant.done")
    assert _shape(_after_the_call_start(llm.requests[0])) == ["user"]
    await f.conversation.close()


@pytest.mark.parametrize("speaks_first", [False, True])
async def test_a_call_s_history_opens_with_the_line_that_says_a_call_has_started(
    speaks_first: bool,
) -> None:
    # The gem is told the same instructions in a call as in writing: this line is what
    # says the conversation is spoken. A greeting note, if any, follows it.
    llm = _LLM([_delta("Hi."), _completed(_message("Hi."))])
    f = _conversation(llm, speaks_first=speaks_first)

    if not speaks_first:
        f.stt.say("hello")
    await _read_until(f.conversation, "assistant.done")

    [first, second] = llm.requests[0]["input"]
    assert first == _CALL_STARTED
    assert second == (
        {
            "role": "developer",
            "content": "The call has just connected and the user has not said anything yet. "
            "Greet them in one short sentence and ask what you can do for them.",
        }
        if speaks_first
        else {"role": "user", "content": "hello"}
    )
    await f.conversation.close()


async def test_asked_to_speak_first_the_assistant_greets_before_the_user_says_anything() -> None:
    llm = _LLM(
        [_delta("Hi, "), _delta("how can I help?"), _completed(_message("Hi, how can I help?"))],
        [_delta("Paris."), _completed(_message("Paris."))],
    )
    f = _conversation(llm, speaks_first=True)

    outputs = await _read_until(f.conversation, "assistant.done")

    # The greeting answers a note of ours: the model's own words never open the history.
    assert _shape(_after_the_call_start(llm.requests[0])) == ["developer"]
    assert "has not said anything yet" in _after_the_call_start(llm.requests[0])[0]["content"]
    assert outputs[-1].text == "Hi, how can I help?"
    assert _audio_seconds(outputs) == 5
    assert "user.final" not in _types(outputs)

    # The conversation then goes on as any other.
    f.clock.advance(60)
    f.stt.say("capital of France?")
    await _read_until(f.conversation, "assistant.done")
    sent = _after_the_call_start(llm.requests[1])
    assert _shape(sent) == ["developer", "assistant", "user"]
    assert sent[1]["content"] == "Hi, how can I help?"
    await f.conversation.close()


async def test_a_greeting_the_user_talks_over_keeps_only_the_words_they_heard() -> None:
    llm = _LLM(
        [_delta("Hi, "), _delta("how can I help?"), _completed(_message("Hi, how can I help?"))],
        [_delta("Paris."), _completed(_message("Paris."))],
    )
    f = _conversation(llm, speaks_first=True)

    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(_DELAY + 2.5)  # "Hi, how" was heard
    f.stt.say("capital of France?")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _event(outputs, "interrupt").text == "Hi, how…"
    sent = _after_the_call_start(llm.requests[1])
    assert _shape(sent) == ["developer", "assistant", "user"]
    assert sent[1]["content"] == "Hi, how…"
    await f.conversation.close()


async def test_a_greeting_cut_off_before_a_word_of_it_was_heard_leaves_no_trace() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("Hi, "), never],
        [_delta("Paris."), _completed(_message("Paris."))],
    )
    f = _conversation(llm, speaks_first=True)

    await _read_until(f.conversation, "assistant.word")
    f.stt.say("capital of France?")  # before the greeting's first word has played
    outputs = await _read_until(f.conversation, "assistant.done")

    assert outputs[-1].text == "Paris."
    assert _shape(_after_the_call_start(llm.requests[1])) == ["developer", "user"]
    await f.conversation.close()


async def test_a_greeting_cut_off_by_a_noise_is_picked_up_again() -> None:
    llm = _LLM(
        [_delta("Hi, "), _delta("how can I help?"), _completed(_message("Hi, how can I help?"))],
        [_delta("can I help?"), _completed(_message("can I help?"))],
    )
    f = _conversation(llm, speaks_first=True)

    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(_DELAY + 2.5)
    f.stt.say("")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert outputs[-1].text == "can I help?"
    sent = _after_the_call_start(llm.requests[1])
    assert _shape(sent) == ["developer", "assistant", "developer"]
    assert sent[1]["content"] == "Hi, how…"
    assert "no words" in sent[2]["content"]
    await f.conversation.close()


# ---- the speech connection is opened ahead of the reply


async def test_a_speech_connection_is_open_before_anyone_speaks_and_the_reply_uses_it() -> None:
    llm = _LLM([_delta("Hi."), _completed(_message("Hi."))])
    f = _conversation(llm)

    await _let_audio_out()
    assert len(f.tts.sessions) == 1  # opened when the call started

    f.stt.say("hello")
    await _read_until(f.conversation, "assistant.done")

    assert len(f.tts.sessions) == 1  # and the reply spoke on that one
    assert "".join(f.tts.contexts[0].pushed) == "Hi."
    await f.conversation.close()


async def test_each_turn_start_has_a_connection_open_for_the_reply_to_come() -> None:
    llm = _LLM(
        [_delta("Hi."), _completed(_message("Hi."))],
        [_delta("Fine."), _completed(_message("Fine."))],
    )
    f = _conversation(llm)

    f.stt.say("hello")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(5)
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    await _let_audio_out()

    # The first reply closed its own. The next one's is open while the user still speaks.
    assert [session.closed for session in f.tts.sessions] == [True, False]
    f.stt.turn_end("how are you")
    await _read_until(f.conversation, "assistant.done")
    assert len(f.tts.sessions) == 2
    await f.conversation.close()


async def test_a_connection_that_has_waited_too_long_is_replaced_before_it_is_used() -> None:
    llm = _LLM([_delta("Hi."), _completed(_message("Hi."))])
    f = _conversation(llm)

    await _let_audio_out()
    f.clock.advance(120)  # nobody spoke for two minutes: the service may have dropped it
    f.stt.say("hello")
    await _read_until(f.conversation, "assistant.done")
    await _let_audio_out()

    assert len(f.tts.sessions) == 2
    assert f.tts.sessions[0].closed
    assert f.tts.contexts[0].pushed == ["Hi."]
    await f.conversation.close()


async def test_a_connection_that_failed_to_open_ahead_of_time_does_not_fail_the_reply() -> None:
    llm = _LLM([_delta("Hi."), _completed(_message("Hi."))])
    stt, tts, clock = _STT(), _TTS(), _Clock()
    tts.failures = 1
    conversation = VoiceConversation(
        stt=stt, tts=tts, llm=llm, search=_Search(), gem=_GEM, clock=clock
    )

    await _let_audio_out()
    stt.say("hello")
    outputs = await _read_until(conversation, "assistant.done")

    assert "error" not in _types(outputs)
    assert _audio_seconds(outputs) == 1
    await conversation.close()


async def test_a_connection_that_died_while_it_waited_is_replaced_when_first_used() -> None:
    llm = _LLM([_delta("Hi "), _delta("there."), _completed(_message("Hi there."))])
    f = _conversation(llm)

    await _let_audio_out()
    f.tts.sessions[0].dead = True
    f.stt.say("hello")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert "error" not in _types(outputs)
    assert _audio_seconds(outputs) == 2
    assert f.tts.sessions[0].closed
    assert f.tts.contexts[0].pushed == ["Hi ", "there."]
    await f.conversation.close()


async def test_closing_closes_a_connection_that_no_reply_took() -> None:
    f = _conversation(_LLM())

    await _let_audio_out()
    await f.conversation.close()

    assert [session.closed for session in f.tts.sessions] == [True]


# ---- interrupting: a note on what went unheard

_STORY = "Once upon a time there was a dragon who lived in a cave and guarded a great pile of gold."


async def _cut_the_story(f: _Fixture, then: str) -> list[dict]:
    """Start the story, cut it four words in, end the turn with `then`. Return the next request."""
    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.clock.advance(_DELAY + 4.5)
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    await _let_audio_out()
    f.stt.turn_end(then)
    await _read_until(f.conversation, "assistant.done")
    return _after_the_call_start(f.llm.requests[1])


async def test_a_long_unheard_part_leaves_nothing_but_the_cut() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta(_STORY), never], [_delta("Sure."), _completed(_message("Sure."))])
    f = _conversation(llm)

    sent = await _cut_the_story(f, "stop")

    # However much went unheard, the history gets the words heard and nothing of ours.
    assert _shape(sent) == ["user", "assistant", "user"]
    assert sent[1]["content"] == "Once upon a time…"
    assert len(llm.requests) == 2
    await f.conversation.close()


async def test_the_recognizer_opening_an_empty_turn_first_adds_only_that_it_had_no_words() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta(_STORY), never],
        [_delta("there was a dragon"), never],
        [_delta("Sure."), _completed(_message("Sure."))],
    )
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.clock.advance(_DELAY + 4.5)
    # Some recognizer settings report the start of speech as an empty turn of its own.
    f.stt.say("")
    # The rest of the story's words are still waiting to be read, ahead of the cut.
    await _read_until(f.conversation, "interrupt")
    await _read_until(f.conversation, "assistant.word")
    f.stt.say("stop")
    await _read_until(f.conversation, "assistant.done")

    sent = _after_the_call_start(llm.requests[2])
    assert _shape(sent) == ["user", "assistant", "developer", "user"]
    assert sent[1]["content"] == "Once upon a time…"
    assert "no words" in sent[2]["content"]
    await f.conversation.close()


async def test_a_turn_that_ends_without_starting_still_cuts_the_reply() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta(_STORY), never], [_delta("Sure."), _completed(_message("Sure."))])
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.clock.advance(_DELAY + 4.5)
    f.stt.turn_end("stop")
    await _read_until(f.conversation, "assistant.done")

    assert _shape(_after_the_call_start(llm.requests[1])) == ["user", "assistant", "user"]
    await f.conversation.close()


# ---- putting the user's turn together


async def test_words_that_trail_a_sentence_follow_it_as_an_item_of_their_own() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("I am"), never],
        [_delta("Nothing much."), _completed(_message("Nothing much."))],
        [_delta("Bye."), _completed(_message("Bye."))],
    )
    f = _conversation(llm)

    # Speech recognition split one sentence into two turns.
    f.stt.say("Hello, what's")
    await _read_until(f.conversation, "assistant.word")
    f.stt.say("happening?")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _event(outputs, "user.final").text == "happening?"
    # The first half was sent as it was said, and is not changed: the second follows it.
    assert _after_the_call_start(llm.requests[0]) == [{"role": "user", "content": "Hello, what's"}]
    assert _after_the_call_start(llm.requests[1]) == [
        {"role": "user", "content": "Hello, what's"},
        {"role": "user", "content": "happening?"},
    ]

    # Once the reply has been heard, the next words follow it.
    f.clock.advance(60)
    f.stt.say("bye")
    await _read_until(f.conversation, "assistant.done")
    assert _after_the_call_start(llm.requests[2]) == [
        {"role": "user", "content": "Hello, what's"},
        {"role": "user", "content": "happening?"},
        {"role": "assistant", "content": "Nothing much."},
        {"role": "user", "content": "bye"},
    ]
    await f.conversation.close()


async def test_a_stray_word_in_another_script_is_dropped_and_the_reply_starts_again() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("The capital"), never],
        [_delta("Paris."), _completed(_message("Paris."))],
    )
    f = _conversation(llm)

    f.stt.say("What is the capital of France?")
    await _read_until(f.conversation, "assistant.word")
    f.stt.say("हूँ")  # a noise, heard as one Devanagari word
    outputs = await _read_until(f.conversation, "assistant.done")

    # The noise never reaches the model or the screen, and the question is answered.
    assert "user.final" not in _types(outputs)
    assert _after_the_call_start(llm.requests[1]) == [
        {"role": "user", "content": "What is the capital of France?"},
    ]
    assert outputs[-1].text == "Paris."
    await f.conversation.close()


async def test_a_turn_with_no_words_does_not_leave_the_user_unanswered() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("The capital"), never],
        [_delta("Paris."), _completed(_message("Paris."))],
    )
    f = _conversation(llm)

    f.stt.say("What is the capital of France?")
    await _read_until(f.conversation, "assistant.word")
    f.stt.say("")  # a cough: a turn starts, cutting the reply, and ends with nothing in it
    outputs = await _read_until(f.conversation, "assistant.done")

    assert outputs[-1].text == "Paris."
    assert _after_the_call_start(llm.requests[1]) == [
        {"role": "user", "content": "What is the capital of France?"},
    ]
    await f.conversation.close()


async def test_a_reply_cut_off_by_a_noise_is_picked_up_again() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("Once upon a time there was a dragon."), never],
        [_delta("there was a dragon."), _completed(_message("there was a dragon."))],
    )
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.clock.advance(_DELAY + 4.5)
    f.stt.say("")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _event(outputs, "interrupt").text == "Once upon a time…"
    sent = _after_the_call_start(llm.requests[1])
    assert _shape(sent) == ["user", "assistant", "developer"]
    assert sent[1]["content"] == "Once upon a time…"
    assert "no words" in sent[2]["content"]
    await f.conversation.close()


async def test_a_stray_word_just_after_the_reply_starts_is_dropped_and_the_reply_resumes() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("The capital of France is Paris."), never],
        [_delta("of France is Paris."), _completed(_message("of France is Paris."))],
    )
    f = _conversation(llm)

    f.stt.say("What is the capital of France?")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.clock.advance(_DELAY + 2.5)  # two words of the reply heard, 2.65 s after the question
    f.stt.say("हूँ")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert "user.final" not in _types(outputs)
    sent = _after_the_call_start(llm.requests[1])
    assert _shape(sent) == ["user", "assistant", "developer"]
    assert sent[1]["content"] == "The capital…"
    assert "no words" in sent[2]["content"]
    await f.conversation.close()


async def test_the_same_word_well_into_the_reply_is_taken_as_the_user_speaking() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("Once upon a time there was a dragon who lived in a cave."), never],
        [_delta("ठीक है."), _completed(_message("ठीक है."))],
    )
    f = _conversation(llm)

    f.stt.say("Tell me a story.")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.clock.advance(_DELAY + 6.5)
    f.stt.say("रुको")  # "stop", six seconds in: far too late to be a trailing noise
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _event(outputs, "user.final").text == "रुको"
    assert _after_the_call_start(llm.requests[1])[-1] == {"role": "user", "content": "रुको"}
    await f.conversation.close()


async def test_a_word_from_a_turn_with_no_start_is_not_judged_by_the_turn_before() -> None:
    llm = _LLM(
        [_delta("Once."), _completed(_message("Once."))],
        [_delta("ठीक है."), _completed(_message("ठीक है."))],
    )
    f = _conversation(llm)

    f.stt.say("Tell me a story.")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    f.stt.turn_end("रुको")  # no turn start came first, a minute after the user's sentence
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _event(outputs, "user.final").text == "रुको"
    await f.conversation.close()


async def test_a_language_the_user_has_already_spoken_is_never_noise() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("नमस्ते."), _completed(_message("नमस्ते."))],
        [_delta("The capital"), never],
        [_delta("ठीक है."), _completed(_message("ठीक है."))],
    )
    f = _conversation(llm)

    f.stt.say("मुझे हिंदी में जवाब दो।")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    f.stt.say("What is the capital of France?")
    await _read_until(f.conversation, "assistant.word")
    f.stt.say("रुको")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _event(outputs, "user.final").text == "रुको"
    await f.conversation.close()


@pytest.mark.parametrize(
    ("fragment", "sentence_before", "stray"),
    [
        ("हूँ", "What is the capital of France?", True),
        ("да нет", "Tell me a story.", True),
        # Same script, or more than a word or two: taken as speech.
        ("please", "What is the capital of France?", False),
        ("मुझे हिंदी में बताओ", "What is the capital of France?", False),
        # The sentence before it was not finished, so it may be its other half.
        ("हूँ", "Hello, what's", False),
        # Nothing came before it: there is nothing to compare with.
        ("नमस्ते", "", False),
        # No letters at all is not evidence of anything.
        ("42", "What is the capital of France?", False),
    ],
)
def test_only_a_short_fragment_in_a_new_script_after_a_whole_sentence_is_stray(
    fragment: str, sentence_before: str, stray: bool
) -> None:
    assert _is_stray(fragment, sentence_before, {"LATIN"}) is stray


@pytest.mark.parametrize(
    ("transcript", "kept"),
    [
        ("What is the capital of France? हूँ", "What is the capital of France?"),
        ("Tell me a story. Then stop. да нет", "Tell me a story. Then stop."),
        # Same script, too long, or no finished sentence before it: left alone.
        (
            "What is the capital of France? Paris maybe",
            "What is the capital of France? Paris maybe",
        ),
        ("Say it. मुझे हिंदी में बताओ", "Say it. मुझे हिंदी में बताओ"),
        ("Hello what's हूँ", "Hello what's हूँ"),
        ("हूँ", "हूँ"),
        ("", ""),
        # Nothing but punctuation before it: left whole, for the fragment rule to judge.
        ("? हूँ", "? हूँ"),
        ("123. हाँ जी", "123. हाँ जी"),
        # One language in several scripts, or the same letters in a wider form.
        ("今日は晴れです。 カメラ", "今日は晴れです。 カメラ"),
        ("Is it ok? \uff2f\uff2b", "Is it ok? \uff2f\uff2b"),  # "OK" in full-width letters
    ],
)
def test_a_stray_word_run_on_to_the_end_of_a_sentence_is_cut_off(
    transcript: str, kept: str
) -> None:
    assert _without_stray_tail(transcript, set()) == kept


def test_a_tail_in_a_script_the_user_has_spoken_is_left_on_the_sentence() -> None:
    transcript = "What is the capital of France? रुको"
    assert _without_stray_tail(transcript, {"DEVANAGARI"}) == transcript


async def test_a_noise_turn_that_starts_with_punctuation_is_still_dropped() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta("The capital"), never], [_delta("Paris."), _completed(_message("Paris."))])
    f = _conversation(llm)

    f.stt.say("What is the capital of France?")
    await _read_until(f.conversation, "assistant.word")
    f.stt.say("? हूँ")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert "user.final" not in _types(outputs)
    assert _after_the_call_start(llm.requests[1]) == [
        {"role": "user", "content": "What is the capital of France?"},
    ]
    await f.conversation.close()


async def test_a_noise_word_inside_the_users_sentence_never_reaches_the_model() -> None:
    llm = _LLM([_delta("Paris."), _completed(_message("Paris."))])
    f = _conversation(llm)

    f.stt.say("What is the capital of France? हूँ")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _event(outputs, "user.final").text == "What is the capital of France?"
    assert _after_the_call_start(llm.requests[0]) == [
        {"role": "user", "content": "What is the capital of France?"},
    ]
    await f.conversation.close()


def test_a_script_spoken_at_any_point_in_the_conversation_is_not_stray() -> None:
    assert _is_stray("रुको", "What is the capital of France?", {"LATIN", "DEVANAGARI"}) is False


# ---- searching


async def test_a_search_runs_between_two_model_calls() -> None:
    call = _search_call("who won the last F1 race")
    llm = _LLM(
        [_starts(call), _completed(call)],
        [_delta("Norris won."), _completed(_message("Norris won."))],
    )
    search = _Search()
    f = _conversation(llm, search)

    f.stt.say("who won the last race")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert search.queries == ["who won the last F1 race"]
    assert VoiceEvent(type="tool.call", text="who won the last F1 race") in outputs
    assert [context.pushed for context in f.tts.contexts] == [["Norris won."]]
    assert all(context.finished for context in f.tts.contexts)
    assert _audio_seconds(outputs) == 2
    assert outputs[-1].text == "Norris won."
    assert len(f.tts.sessions) == 1
    # The second call gets the call and its result.
    sent = _after_the_call_start(llm.requests[1])
    assert _shape(sent) == ["user", "function_call", "function_call_output"]
    assert sent[1]["call_id"] == sent[2]["call_id"] == "call_1"
    assert "Norris won in Las Vegas." in sent[2]["output"]
    assert llm.requests[0]["tools"][0]["name"] == "web_search"
    await f.conversation.close()


async def test_a_model_that_announces_its_search_is_not_talked_over() -> None:
    call = _search_call("who won the last F1 race")
    llm = _LLM(
        [_delta("One moment."), _starts(call), _completed(_message("One moment."), call)],
        [_delta("Norris won."), _completed(_message("Norris won."))],
    )
    f = _conversation(llm)

    f.stt.say("who won the last race")
    await _read_until(f.conversation, "assistant.done")

    assert [context.pushed for context in f.tts.contexts] == [["One moment."], ["Norris won."]]
    await f.conversation.close()


async def test_without_a_search_key_the_tool_is_not_offered() -> None:
    llm = _LLM([_delta("Hi."), _completed(_message("Hi."))])
    f = _conversation(llm, _Search(configured=False))

    f.stt.say("hello")
    await _read_until(f.conversation, "assistant.done")

    assert llm.requests[0]["tools"] == []
    assert "web_search" not in llm.requests[0]["instructions"]
    await f.conversation.close()


async def test_a_reply_may_search_as_often_as_the_model_needs() -> None:
    searches = [[_completed(_search_call(f"query {n}", f"call_{n}"))] for n in range(4)]
    llm = _LLM(*searches, [_delta("Here it is."), _completed(_message("Here it is."))])
    search = _Search()
    f = _conversation(llm, search)

    f.stt.say("dig into this")
    outputs = await _read_until(f.conversation, "assistant.done")

    # A fourth search runs, and the model answers with its result in front of it.
    assert search.queries == ["query 0", "query 1", "query 2", "query 3"]
    *_, call, result = _after_the_call_start(llm.requests[-1])
    assert call["call_id"] == result["call_id"] == "call_3"
    assert "Norris won in Las Vegas." in result["output"]
    # No round forbids the tool.
    assert [request["tool_choice"] for request in llm.requests] == ["auto"] * 5
    # The start of every request is identical, so a prompt cache keeps working.
    assert len({request["instructions"] for request in llm.requests}) == 1
    assert all(request["tools"] == llm.requests[0]["tools"] for request in llm.requests)
    assert _audio_seconds(outputs) == 3
    await f.conversation.close()


async def test_interrupting_the_answer_keeps_the_search_and_the_words_heard() -> None:
    call = _search_call("who won the last F1 race")
    never = asyncio.Event()
    llm = _LLM(
        [_starts(call), _completed(call)],
        [_delta("Norris won the race."), never],
        [_delta("Sure."), _completed(_message("Sure."))],
    )
    f = _conversation(llm)

    f.stt.say("who won the last race")
    await _read_until(f.conversation, "tool.call")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.clock.advance(_DELAY + 1.5)
    f.stt.say("stop")
    await _read_until(f.conversation, "assistant.done")

    sent = _after_the_call_start(llm.requests[2])
    assert _shape(sent) == ["user", "function_call", "function_call_output", "assistant", "user"]
    assert sent[3]["content"] == "Norris…"
    await f.conversation.close()


async def test_what_was_said_before_a_search_is_kept_whole_once_the_model_has_seen_it() -> None:
    call = _search_call("who won the last F1 race")
    llm = _LLM(
        [_delta("One moment."), _starts(call), _completed(_message("One moment."), call)],
        [_delta("Norris won the race."), _completed(_message("Norris won the race."))],
        [_delta("Sure."), _completed(_message("Sure."))],
    )
    f = _conversation(llm)

    # Everything is generated at once; the user speaks one second into "One moment."
    f.stt.say("who won the last race")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(_DELAY + 1.2)
    f.stt.say("stop")
    outputs = await _read_until(f.conversation, "assistant.done")

    # "One moment." had already gone back to the model with the search result, so it
    # stays as it was. The answer was never heard and is dropped.
    assert outputs[0] == VoiceEvent(type="interrupt", text="One moment.")
    sent = _after_the_call_start(llm.requests[2])
    assert _shape(sent) == ["user", "assistant", "function_call", "function_call_output", "user"]
    assert sent[1]["content"] == "One moment."
    await f.conversation.close()


async def test_two_searches_in_one_round_are_each_given_their_own_kind_of_result() -> None:
    quick, slow = _search_call("quick one", "call_q"), _search_call("slow one", "call_s")
    finish = asyncio.Event()
    llm = _LLM(
        [_starts(quick), _completed(quick, slow)],
        [_delta("Still looking."), _completed(_message("Still looking."))],
    )
    search = _Search(gate=finish, only="slow")
    f = _conversation(llm, search)

    f.stt.say("look up two things")
    await _read_until(f.conversation, "tool.call")
    await _let_audio_out()
    f.stt.say("hello?")
    await _read_until(f.conversation, "assistant.done")

    sent = _after_the_call_start(llm.requests[1])
    outputs = {
        item["call_id"]: item["output"]
        for item in sent
        if item.get("type") == "function_call_output"
    }
    assert "Norris won in Las Vegas." in outputs["call_q"]
    assert "Still running" in outputs["call_s"]
    assert f.conversation._late.holds("call_s") and not f.conversation._late.holds("call_q")
    await f.conversation.close()


# ---- a search that is still running when the user speaks


def _slow_search_then(*rounds: list) -> tuple[_LLM, asyncio.Event]:
    """A model that searches first, and a search that hangs until the returned event is set."""
    call = _search_call("weather in Pune")
    return _LLM([_starts(call), _completed(call)], *rounds), asyncio.Event()


async def test_speaking_during_a_search_leaves_a_placeholder_and_the_search_running() -> None:
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))]
    )
    search = _Search(gate=finish)
    f = _conversation(llm, search)

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("hello, what's happening?")
    await _read_until(f.conversation, "assistant.done")

    sent = _after_the_call_start(llm.requests[1])
    assert _shape(sent) == ["user", "function_call", "function_call_output", "user"]
    assert sent[1]["call_id"] == sent[2]["call_id"] == "call_1"
    # The placeholder says the result comes later, names the call, and how to stop it.
    assert "Still running" in sent[2]["output"]
    assert '"deferred_result_for": "call_1"' in sent[2]["output"]
    assert "cancel_tool_call" in sent[2]["output"]
    # The search was not stopped: it is still waiting.
    assert search.queries == ["weather in Pune"]
    assert f.conversation._late.holds("call_1")
    await f.conversation.close()


async def test_a_late_result_is_given_to_the_model_once_nothing_is_being_said() -> None:
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("hello, what's happening?")
    await _read_until(f.conversation, "assistant.done")

    # The search finishes while "Still looking." is still playing: nothing happens yet.
    finish.set()
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 2

    # Once that reply has played out, the model answers the result with no user turn.
    f.clock.advance(60)
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "It is sunny."
    assert "interrupt" not in _types(outputs)
    # The model thinks before it speaks only while the search was still running: without
    # that it tends to search for the same thing again when the user just says "hello?".
    assert [request["reasoning"]["effort"] for request in llm.requests] == ["none", "low", "none"]

    sent = _after_the_call_start(llm.requests[2])
    assert _shape(sent) == [
        "user",
        "function_call",
        "function_call_output",
        "user",
        "assistant",
        "function_call",
        "function_call_output",
    ]
    assert sent[4]["content"] == "Still looking."
    # The stub is a call we wrote: a new id, the same tool, and the call it stands for.
    stub, result = sent[5], sent[6]
    assert stub["name"] == "web_search"
    assert json.loads(stub["arguments"]) == {"deferred_result_for": "call_1"}
    assert stub["call_id"] == result["call_id"] != "call_1"
    assert "Norris won in Las Vegas." in result["output"]
    await f.conversation.close()


async def test_a_result_that_lands_while_the_user_speaks_goes_in_ahead_of_their_words() -> None:
    llm, finish = _slow_search_then(
        [_delta("Four. And it is sunny."), _completed(_message("Four. And it is sunny."))],
        [_delta("Welcome."), _completed(_message("Welcome."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    # The user starts on something else, and the result comes back while they speak.
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()
    await asyncio.sleep(0.05)
    f.stt.turn_end("what is two plus two?")
    outputs = await _read_until(f.conversation, "assistant.done")

    # One reply answers them and tells the result: the stub and the result come before
    # their words, and the model is not shown the search as still running.
    assert outputs[-1].text == "Four. And it is sunny."
    asked = _after_the_call_start(llm.requests[1])
    assert _shape(asked) == [
        "user",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "user",
    ]
    assert "Still running" in asked[2]["output"]
    assert json.loads(asked[3]["arguments"]) == {"deferred_result_for": "call_1"}
    assert asked[3]["call_id"] == asked[4]["call_id"] != "call_1"
    assert "Norris won in Las Vegas." in asked[4]["output"]
    assert asked[5] == {"role": "user", "content": "what is two plus two?"}

    # Nothing is brought up afterwards, and their next turn is simply answered.
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 2
    f.stt.say("thanks")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 3
    await f.conversation.close()


async def test_a_reply_that_answers_with_a_result_is_made_again_if_noise_cuts_it_unheard() -> None:
    never = asyncio.Event()
    llm, finish = _slow_search_then(
        [_delta("Four."), never],
        [_delta("Four. And it is sunny."), _completed(_message("Four. And it is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()
    await asyncio.sleep(0.05)
    f.stt.turn_end("what is two plus two?")
    await _read_until(f.conversation, "assistant.word")
    f.stt.say("")
    outputs = await _read_until(f.conversation, "assistant.done")

    # Nothing of it was heard: the turn is still open after the result, and the same
    # request is made again.
    assert outputs[-1].text == "Four. And it is sunny."
    assert _after_the_call_start(llm.requests[2]) == _after_the_call_start(llm.requests[1])
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 3
    await f.conversation.close()


async def _interrupted_search_then_hello(f: _Fixture, finish: asyncio.Event) -> None:
    """A search is interrupted with "hello?", that is answered and heard, and the result lands."""
    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("hello?")
    await _read_until(f.conversation, "assistant.done")
    finish.set()
    f.clock.advance(60)


async def test_a_result_whose_reply_was_cut_off_unheard_by_noise_is_told_again() -> None:
    never = asyncio.Event()
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("It is"), never],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    await _interrupted_search_then_hello(f, finish)
    # The reply about the result starts, and a noise cuts it off before a word is heard.
    await _read_until(f.conversation, "assistant.word")
    f.stt.say("")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert outputs[-1].text == "It is sunny."
    # Nothing of it was heard, so the history is as it was and the request is the same.
    assert _after_the_call_start(llm.requests[3]) == _after_the_call_start(llm.requests[2])

    # Once it has been heard, the user is owed nothing more.
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 4
    await f.conversation.close()


async def test_a_result_whose_reply_the_user_talked_over_is_left_to_the_model() -> None:
    never = asyncio.Event()
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("It is"), never],
        [_delta("Four."), _completed(_message("Four."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    await _interrupted_search_then_hello(f, finish)
    await _read_until(f.conversation, "assistant.word")
    f.stt.say("what is two plus two?")
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "Four."

    # The model was sent the result and then the question, with nothing of ours added.
    asked = _after_the_call_start(llm.requests[3])
    assert _shape(asked)[-3:] == ["function_call", "function_call_output", "user"]
    assert [item for item in asked if item.get("role") == "developer"] == []
    # Nothing is brought up afterwards: telling the user is the model's to do.
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 4
    await f.conversation.close()


async def test_words_said_before_a_search_but_never_heard_do_not_count_as_telling() -> None:
    more = _search_call("forecast for tomorrow", "call_2")
    never = asyncio.Event()
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("It is sunny."), _starts(more), _completed(_message("It is sunny."), more)],
        [_delta("And tomorrow"), never],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish, only="weather"))

    await _interrupted_search_then_hello(f, finish)
    # The reply about the result says it, searches again and goes on, all before any of
    # it has played. Then a noise cuts it off.
    await _read_until(f.conversation, "tool.call")
    await _read_until(f.conversation, "assistant.word")
    f.stt.say("")

    # "It is sunny." is in the history, because the model was sent it. The user never
    # heard it, so the reply is made again.
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "It is sunny."
    assert len(llm.requests) == 5
    await f.conversation.close()


async def test_a_reply_about_a_result_that_says_nothing_is_not_made_again() -> None:
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_completed(_message(""))],
        [_delta("Four."), _completed(_message("Four."))],
        [_delta("Five."), _completed(_message("Five."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    await _interrupted_search_then_hello(f, finish)
    await _read_until(f.conversation, "assistant.done")  # the reply about the result, empty
    for question in ("what is two plus two?", "and two plus three?"):
        f.clock.advance(60)
        f.stt.say(question)
        await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await asyncio.sleep(0.25)

    # Two questions, two answers: nothing was brought up again in between or after.
    assert len(llm.requests) == 5
    notes = [i for i in _after_the_call_start(llm.requests[-1]) if i.get("role") == "developer"]
    assert notes == []
    await f.conversation.close()


async def test_a_result_is_still_owed_if_its_reply_was_cut_off_before_it_said_anything() -> None:
    never = asyncio.Event()
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [never],  # the model is still thinking about the result when a noise comes
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    await _interrupted_search_then_hello(f, finish)
    await asyncio.sleep(0.25)  # the reply about the result has been asked for
    assert len(llm.requests) == 3
    f.stt.say("")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert outputs[-1].text == "It is sunny."
    assert _after_the_call_start(llm.requests[3]) == _after_the_call_start(llm.requests[2])
    await f.conversation.close()


async def test_a_result_whose_reply_failed_is_there_when_the_user_next_speaks() -> None:
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [failed],
        [_delta("Four."), _completed(_message("Four."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    await _interrupted_search_then_hello(f, finish)
    await _read_until(f.conversation, "error")
    f.stt.say("what is two plus two?")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert outputs[-1].text == "Four."
    asked = _after_the_call_start(llm.requests[3])
    assert _shape(asked)[-3:] == ["function_call", "function_call_output", "user"]
    await f.conversation.close()


async def test_a_result_waiting_for_a_reply_to_be_heard_goes_in_with_the_turn_that_cuts_it() -> (
    None
):
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("Yes. It is sunny."), _completed(_message("Yes. It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("hello, what's happening?")
    await _read_until(f.conversation, "assistant.done")

    # The result lands while "Still looking." is playing, so it waits. The user speaks
    # over that reply before a word of it is heard.
    finish.set()
    await asyncio.sleep(0.05)
    f.stt.say("are you there?")
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "Yes. It is sunny."

    # The turn left open, already sent to the model, is closed; the result follows it,
    # and what they now said is the open turn.
    asked = _after_the_call_start(llm.requests[2])
    assert _shape(asked) == [
        "user",
        "function_call",
        "function_call_output",
        "user",
        "function_call",
        "function_call_output",
        "user",
    ]
    assert asked[3]["content"] == "hello, what's happening?"
    assert json.loads(asked[4]["arguments"]) == {"deferred_result_for": "call_1"}
    assert asked[6]["content"] == "are you there?"

    # The result is not brought up again in a reply of its own.
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 3
    await f.conversation.close()


async def test_a_late_result_is_still_brought_up_if_the_user_said_nothing_after_all() -> None:
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("hello, what's happening?")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)

    # A turn opens, the result lands, and the turn closes with no words in it.
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()
    await asyncio.sleep(0.05)
    f.stt.turn_end("")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert outputs[-1].text == "It is sunny."
    # The result went in once, and nothing claims a reply was cut by noise.
    assert _shape(_after_the_call_start(llm.requests[2]))[-3:] == [
        "assistant",
        "function_call",
        "function_call_output",
    ]
    await f.conversation.close()


async def test_a_wordless_turn_after_an_unanswered_one_is_not_blamed_on_noise() -> None:
    never = asyncio.Event()
    llm, finish = _slow_search_then(
        [_delta("Hmm"), never],
        [_delta("Yes."), _completed(_message("Yes."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("hello?")
    await _read_until(f.conversation, "assistant.word")
    # The user is heard to start again, the search finishes, and nothing was said.
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()
    await asyncio.sleep(0.05)
    f.stt.turn_end("")
    await _read_until(f.conversation, "assistant.done")

    # "hello?" is simply answered again. No reply had been heard, so nothing in the
    # history says one was cut off by noise.
    again = _after_the_call_start(llm.requests[2])
    assert _shape(again) == ["user", "function_call", "function_call_output", "user"]
    assert again[-1]["content"] == "hello?"

    # And the result is still brought up afterwards.
    f.clock.advance(60)
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "It is sunny."
    await f.conversation.close()


async def test_a_late_result_is_brought_up_once_even_if_two_turns_end_together() -> None:
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("So. It is sunny."), _completed(_message("So. It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("hello?")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()
    await asyncio.sleep(0.05)
    # A turn with no words, then straight away one with words and no start of its own.
    f.stt.turn_end("")
    f.stt.turn_end("so?")
    outputs = await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await asyncio.sleep(0.25)

    # One reply answers "so?" with the result in front of it, and nothing after that.
    assert outputs[-1].text == "So. It is sunny."
    assert len(llm.requests) == 3
    asked = _after_the_call_start(llm.requests[2])
    assert _shape(asked)[-3:] == ["function_call", "function_call_output", "user"]
    assert asked[-1]["content"] == "so?"
    await f.conversation.close()


async def test_a_turn_that_ends_without_starting_cannot_set_off_two_replies() -> None:
    llm, finish = _slow_search_then(
        [_delta("Okay."), _completed(_message("Okay."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    # No turn start this time, and the search finishes while the reply is being stopped.
    f.stt.turn_end("never mind")
    finish.set()
    await _read_until(f.conversation, "assistant.done")
    # One reply, which has the result in front of it along with what the user said.
    assert _shape(_after_the_call_start(llm.requests[1])) == [
        "user",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "user",
    ]

    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 2
    await f.conversation.close()


async def test_the_model_can_cancel_a_search_that_is_still_running() -> None:
    cancel = _Item(
        type="function_call",
        call_id="call_2",
        name="cancel_tool_call",
        arguments=json.dumps({"call_id": "call_1"}),
    )
    llm, finish = _slow_search_then(
        [_starts(cancel), _completed(cancel)],
        [_delta("Stopped."), _completed(_message("Stopped."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("never mind, stop that")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert outputs[-1].text == "Stopped."
    assert _after_the_call_start(llm.requests[2])[-1] == {
        "type": "function_call_output",
        "call_id": "call_2",
        "output": "Cancelled.",
    }
    # The cancelled search never reports back.
    finish.set()
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 3
    assert not f.conversation._late.pending
    await f.conversation.close()


async def test_a_result_that_landed_while_the_user_spoke_is_no_longer_pending() -> None:
    llm, finish = _slow_search_then(
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()  # the search finishes while the user is still speaking
    await asyncio.sleep(0.05)
    f.stt.turn_end("hello?")
    await _read_until(f.conversation, "assistant.done")

    # The model is shown the result with the user's turn, so no search is left without
    # one, and it is not asked to think.
    asked = llm.requests[1]
    assert "Norris won in Las Vegas." in _after_the_call_start(asked)[-2]["output"]
    assert asked["reasoning"]["effort"] == "none"
    assert not f.conversation._late.pending
    await f.conversation.close()


async def test_the_model_can_cancel_a_result_that_is_waiting_to_be_delivered() -> None:
    cancel = _Item(
        type="function_call",
        call_id="call_2",
        name="cancel_tool_call",
        arguments=json.dumps({"call_id": "call_1"}),
    )
    held = asyncio.Event()
    llm, finish = _slow_search_then(
        [held, _starts(cancel), _completed(cancel)],
        [_delta("Stopped."), _completed(_message("Stopped."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("never mind, stop that")
    await asyncio.sleep(0.05)
    # The search finishes while the reply that will cancel it is being made: its result
    # waits, with no place in the history yet.
    finish.set()
    await asyncio.sleep(0.05)
    assert f.conversation._late.ready
    held.set()
    await _read_until(f.conversation, "assistant.done")

    assert _after_the_call_start(llm.requests[2])[-1]["output"] == "Cancelled."
    # The result the user called off is never brought up.
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 3
    assert not f.conversation._late.pending
    await f.conversation.close()


async def test_cancelling_a_result_already_in_the_history_finds_nothing_running() -> None:
    cancel = _Item(
        type="function_call",
        call_id="call_2",
        name="cancel_tool_call",
        arguments=json.dumps({"call_id": "call_1"}),
    )
    llm, finish = _slow_search_then(
        [_starts(cancel), _completed(cancel)],
        [_delta("It had already come."), _completed(_message("It had already come."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()  # the search finishes while the user is still speaking
    await asyncio.sleep(0.05)
    f.stt.turn_end("never mind, stop that")
    await _read_until(f.conversation, "assistant.done")

    # The result went in with the user's turn, so there is nothing left to stop.
    asked = _after_the_call_start(llm.requests[2])
    assert json.loads(asked[3]["arguments"]) == {"deferred_result_for": "call_1"}
    assert asked[-1]["output"] == "Nothing is running under that call_id."
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 3
    await f.conversation.close()


class _SetsThenGoes(asyncio.Event):
    """A step of a scripted reply that sets another event and does not wait."""

    def __init__(self, other: asyncio.Event) -> None:
        super().__init__()
        self._other = other

    async def wait(self) -> bool:
        self._other.set()
        return True


async def test_a_search_cancelled_in_the_instant_it_finishes_is_not_told() -> None:
    # The search finishes in the same step the model cancels it in, too late to be
    # stopped. The model has been told "Cancelled.", so its result is dropped all the same.
    cancel = _Item(
        type="function_call",
        call_id="call_2",
        name="cancel_tool_call",
        arguments=json.dumps({"call_id": "call_1"}),
    )
    finish = asyncio.Event()
    llm, _ = _slow_search_then(
        [_SetsThenGoes(finish), _starts(cancel), _completed(cancel)],
        [_delta("Stopped."), _completed(_message("Stopped."))],
    )
    search = _Search(gate=finish)
    f = _conversation(llm, search)

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("never mind, stop that")
    await _read_until(f.conversation, "assistant.done")

    assert _after_the_call_start(llm.requests[2])[-1]["output"] == "Cancelled."
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    # It did finish: it was not stopped. It is never told all the same.
    assert search.queries == ["weather in Pune"]
    assert len(llm.requests) == 3
    assert not f.conversation._late.pending
    await f.conversation.close()


async def test_cancelling_a_call_that_is_not_running_says_so() -> None:
    cancel = _Item(
        type="function_call",
        call_id="call_9",
        name="cancel_tool_call",
        arguments=json.dumps({"call_id": "call_unknown"}),
    )
    llm = _LLM([_completed(cancel)], [_delta("Okay."), _completed(_message("Okay."))])
    f = _conversation(llm)

    f.stt.say("stop the search")
    await _read_until(f.conversation, "assistant.done")

    assert (
        _after_the_call_start(llm.requests[1])[-1]["output"]
        == "Nothing is running under that call_id."
    )
    await f.conversation.close()


async def test_closing_stops_a_search_its_reply_is_still_waiting_for() -> None:
    llm, finish = _slow_search_then()
    search = _Search(gate=finish)
    f = _conversation(llm, search)

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    search_task = f.conversation._reply.calls()[0].running
    await f.conversation.close()

    assert search_task.cancelled()


async def test_a_search_that_finishes_as_the_conversation_closes_starts_nothing() -> None:
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))]
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("hello?")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)

    # The search has finished, but nothing has acted on that yet when the close begins.
    finish.set()
    await asyncio.sleep(0)
    await f.conversation.close()
    await asyncio.sleep(0.25)

    assert len(llm.requests) == 2
    assert len(f.tts.sessions) == 2


async def test_closing_stops_a_search_that_outlived_its_reply() -> None:
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))]
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("hello?")
    await _read_until(f.conversation, "assistant.done")
    [call] = [part for part in f.conversation._late._running.values()]
    search_task = call.running
    await f.conversation.close()

    assert search_task.cancelled()


# ---- how much was heard


def _utterance(text: str, seconds_per_word: float = 1.0, *, complete: bool = True) -> Utterance:
    """An utterance whose audio starts playing at time 0, sent as one chunk per word."""
    utterance = Utterance(context=None)
    utterance.text = text
    for n, word in enumerate(text.split()):
        utterance.add_words(
            [CartesiaTTSWord(word, n * seconds_per_word, (n + 1) * seconds_per_word)]
        )
        utterance.add_audio(n * seconds_per_word, seconds_per_word)
    utterance.complete = complete
    return utterance


@pytest.mark.parametrize(
    ("at", "heard"),
    [
        (0.0, ""),
        (0.99, ""),
        (1.0, "Paris"),
        (2.5, "Paris is"),
        (3.99, "Paris is the"),
    ],
)
def test_a_word_counts_as_heard_once_its_audio_has_finished(at: float, heard: str) -> None:
    utterance = _utterance("Paris is the capital.")

    assert utterance.heard(at) == (heard, False)


def test_an_utterance_is_whole_only_when_all_its_audio_has_played() -> None:
    utterance = _utterance("Paris is the capital.")

    assert utterance.heard(4.0) == ("Paris is the capital.", True)

    # Still being generated: even with all the audio so far played, more is coming.
    growing = _utterance("Paris is the", complete=False)
    assert growing.heard(10.0) == ("Paris is the", False)


def test_silence_after_the_last_word_does_not_make_a_reply_look_cut() -> None:
    utterance = _utterance("Paris is the capital.")
    utterance.add_audio(4.0, 0.3)  # the speech service's audio runs on after the last word

    # The user speaks 0.1 s after the last word, with 0.2 s of that silence still to play.
    assert utterance.heard(4.1) == ("Paris is the capital.", True)
    assert utterance.heard(3.9) == ("Paris is the", False)


def test_a_word_timing_that_does_not_match_the_text_ends_the_count_there() -> None:
    utterance = Utterance(context=None)
    utterance.text = "It costs $5 million."
    utterance.add_audio(0.0, 5.0)
    # The speech service spelled the amount out, so its words stop lining up with ours.
    spoken = ["It", "costs", "five", "dollars", "million."]
    utterance.add_words([CartesiaTTSWord(word, float(n), n + 1.0) for n, word in enumerate(spoken)])

    assert utterance.heard(4.5) == ("It costs", False)


def test_a_slow_sound_device_puts_the_user_behind_the_schedule() -> None:
    clock = _Clock()
    playback = Playback(clock)
    started = playback.schedule(2.0)
    assert playback.hearing() == clock.now

    # Bluetooth, say. Reported after the audio was sent, and it still applies to it.
    playback.set_output_delay(0.25)
    assert playback.schedule(1.0) == started + 2.0
    assert playback.hearing() == pytest.approx(clock.now - 0.25)
    assert playback.ends_at == pytest.approx(started + 3.0 + 0.25)

    # A report that makes no sense is held to sane limits rather than trusted.
    playback.set_output_delay(30)
    assert playback.hearing() == pytest.approx(clock.now - 1.0)
    playback.set_output_delay(-5)
    assert playback.hearing() == clock.now
    # Dropping the queue does not forget the device.
    playback.set_output_delay(0.25)
    playback.reset()
    assert playback.hearing() == pytest.approx(clock.now - 0.25)


async def test_a_delay_reported_mid_reply_counts_for_the_words_already_sent() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta("Once upon a time there was a dragon."), never])
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    # All eight words have been sent. Only now does the browser know its real delay.
    f.conversation.set_output_delay(1.0)
    f.clock.advance(_DELAY + 3.5)  # three and a half words played, but heard a second later
    f.stt.turn_start()
    outputs = await _read_until(f.conversation, "interrupt")

    assert outputs[-1].text == "Once upon…"
    await f.conversation.close()


def test_playback_runs_chunks_back_to_back_and_starts_over_after_a_reset() -> None:
    clock = _Clock()
    playback = Playback(clock)

    first = playback.schedule(2.0)
    second = playback.schedule(1.0)
    assert first == clock.now + _DELAY
    assert second == first + 2.0
    assert playback.ends_at == first + 3.0

    # After a gap longer than the queue, the next chunk cannot start in the past.
    clock.advance(10)
    assert playback.schedule(1.0) == clock.now + _DELAY

    playback.reset()
    assert playback.schedule(1.0) == clock.now + _DELAY

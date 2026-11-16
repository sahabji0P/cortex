"""The voice cascade with all three services faked: turns in, speech and history out.

Time is faked too. The fake text-to-speech makes every word last exactly one second, and
the tests move a clock by hand, so each one can say how much of a reply the user heard.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.adapters.cartesia_tts_adp import CartesiaTTSSession, CartesiaTTSWord
from app.entities import voice_ent
from app.entities.voice_ent import VoiceConversation, VoiceEvent, _is_stray, _without_stray_tail
from app.entities.voice_speech import Playback, Utterance
from app.routers.voice.service import VoiceServiceV1Class

# The fake speech is 24 kHz 16-bit mono, like the real one.
_BYTES_PER_SECOND = 48000
# Playback starts this long after audio is sent (see voice_speech._CLIENT_DELAY).
_DELAY = 0.15


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
        timings = [CartesiaTTSWord(word, self._seconds + n + 1) for n, word in enumerate(words)]
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
        # Labelling the unheard part of a cut reply is a separate, one-shot call.
        self.label: str | Exception = "the rest of the story"
        self.label_gate: asyncio.Event | None = None
        self.labelled: list[str] = []

    async def text_response(self, **request) -> str:
        self.labelled.append(request["input"])
        if self.label_gate is not None:
            await self.label_gate.wait()
        if isinstance(self.label, Exception):
            raise self.label
        return self.label

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
    that point on. Two changes are allowed. The user's open turn, which is always the
    last item, can gain a line. And an old search result can be replaced by the cleared
    marker: same call, nothing else touched.
    """
    *settled, last = before
    for was, now in zip(settled, after, strict=False):
        if was != now:
            assert was.get("type") == "function_call_output", "an earlier item was changed"
            assert now == {**was, "output": voice_ent._CLEARED}, "an earlier item was changed"
    assert len(after) >= len(settled), "an earlier item was removed"
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


def _conversation(llm: _LLM, search: _Search | None = None) -> _Fixture:
    stt, tts, clock = _STT(), _TTS(), _Clock()
    conversation = VoiceConversation(
        stt=stt, tts=tts, llm=llm, search=search or _Search(), clock=clock
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
    assert llm.requests[0]["input"] == [{"role": "user", "content": "hello"}]
    assert llm.requests[0]["store"] is False
    assert llm.requests[0]["model"] == "6 Luna"
    assert llm.requests[0]["reasoning"] == {"effort": "none"}
    await f.conversation.close()


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
    assert llm.requests[1]["input"] == [
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
    await _read_until(f.conversation, "assistant.delta")
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
    assert llm.requests[1]["input"] == [
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
    assert llm.requests[1]["input"] == [
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
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.stt.say("never mind")
    outputs = await _read_until(f.conversation, "assistant.done")

    # Cut before its first word: the client is told so with the bare mark.
    assert _event(outputs, "interrupt").text == "…"
    # None of the reply was heard, so the user's turn is still the same, open turn.
    assert llm.requests[1]["input"] == [
        {"role": "user", "content": "tell me a story\nnever mind"},
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
    await _read_until(f.conversation, "assistant.delta")
    await f.conversation.close()

    assert f.stt.closed
    assert f.tts.contexts[0].cancelled
    assert f.tts.sessions[0].closed


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
    conversation = VoiceConversation(stt=stt, tts=tts, llm=llm, search=_Search(), clock=clock)

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
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.clock.advance(_DELAY + 4.5)
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    await _let_audio_out()
    f.stt.turn_end(then)
    await _read_until(f.conversation, "assistant.done")
    return f.llm.requests[1]["input"]


async def test_a_long_unheard_remainder_is_named_by_subject_but_not_repeated() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta(_STORY), never], [_delta("Sure."), _completed(_message("Sure."))])
    f = _conversation(llm)

    sent = await _cut_the_story(f, "stop")

    assert _shape(sent) == ["user", "assistant", "developer", "user"]
    assert sent[1]["content"] == "Once upon a time…"
    assert "did not hear the rest" in sent[2]["content"]
    assert "about: the rest of the story" in sent[2]["content"]
    # Only the unheard words were given to the labeller, and none of them are in the note.
    assert llm.labelled == [
        "there was a dragon who lived in a cave and guarded a great pile of gold."
    ]
    assert "dragon" not in sent[2]["content"]
    await f.conversation.close()


async def test_a_short_unheard_remainder_gets_no_note() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("Once upon a time there was a dragon."), never],
        [_delta("Sure."), _completed(_message("Sure."))],
    )
    f = _conversation(llm)

    sent = await _cut_the_story(f, "stop")

    assert _shape(sent) == ["user", "assistant", "user"]
    assert llm.labelled == []
    await f.conversation.close()


async def test_a_note_that_is_not_ready_when_the_turn_ends_is_left_out(monkeypatch) -> None:
    monkeypatch.setattr(voice_ent, "_LABEL_WAIT_SECONDS", 0.01)
    never = asyncio.Event()
    llm = _LLM([_delta(_STORY), never], [_delta("Sure."), _completed(_message("Sure."))])
    llm.label_gate = asyncio.Event()  # the labeller never answers
    f = _conversation(llm)

    sent = await _cut_the_story(f, "stop")

    # The reply to the user is not held up, and the history is just the plain cut.
    assert _shape(sent) == ["user", "assistant", "user"]
    assert len(llm.labelled) == 1
    assert f.conversation._labelling is None
    assert not [task for task in asyncio.all_tasks() if "_label" in repr(task.get_coro())]
    await f.conversation.close()


async def test_closing_while_a_turn_end_waits_for_the_note_stops_the_labelling(monkeypatch) -> None:
    monkeypatch.setattr(voice_ent, "_LABEL_WAIT_SECONDS", 5)
    never = asyncio.Event()
    llm = _LLM([_delta(_STORY), never])
    llm.label_gate = asyncio.Event()
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.clock.advance(_DELAY + 4.5)
    f.stt.say("stop")
    await _read_until(f.conversation, "interrupt")
    await asyncio.sleep(0.05)  # the turn's end is now waiting for the label
    labelling = f.conversation._labelling
    await f.conversation.close()

    assert labelling is not None
    assert labelling.cancelled()


async def test_closing_while_the_user_is_still_speaking_stops_the_labelling() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta(_STORY), never])
    llm.label_gate = asyncio.Event()
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.clock.advance(_DELAY + 4.5)
    f.stt.turn_start()  # the label is asked for, and the user has not finished speaking
    await _read_until(f.conversation, "interrupt")
    await asyncio.sleep(0.05)
    labelling = f.conversation._labelling
    await f.conversation.close()

    assert labelling is not None
    assert labelling.cancelled()


async def test_a_note_survives_the_recognizer_opening_an_empty_turn_first() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta(_STORY), never],
        [_delta("there was a dragon"), never],
        [_delta("Sure."), _completed(_message("Sure."))],
    )
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.clock.advance(_DELAY + 4.5)
    # Some recognizer settings report the start of speech as an empty turn of its own.
    f.stt.say("")
    await _read_until(f.conversation, "assistant.delta")
    f.stt.say("stop")
    await _read_until(f.conversation, "assistant.done")

    sent = llm.requests[2]["input"]
    assert _shape(sent) == ["user", "assistant", "developer", "developer", "user"]
    assert sent[1]["content"] == "Once upon a time…"
    assert "about: the rest of the story" in sent[3]["content"]
    await f.conversation.close()


async def test_a_note_is_dropped_once_a_later_reply_has_been_heard_in_full() -> None:
    call = _search_call("slow one")
    finish, never = asyncio.Event(), asyncio.Event()
    llm = _LLM(
        [_delta(_STORY), _starts(call), _completed(_message(_STORY), call)],
        [_delta("Go on."), _completed(_message("Go on."))],
        [_delta("It found"), never],
        [_delta("Noon."), _completed(_message("Noon."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    # A story is cut (a note is asked for) by a turn with no words, so it is resumed.
    f.stt.say("tell me a story")
    await _read_until(f.conversation, "tool.call")
    await _let_audio_out()
    f.clock.advance(_DELAY + 4.5)
    f.stt.say("")
    await _read_until(f.conversation, "assistant.done")
    # The resumed reply is heard to the end. Then the search result comes in late and
    # is answered, and the user speaks over that answer before hearing any of it.
    f.clock.advance(60)
    finish.set()
    await _read_until(f.conversation, "assistant.delta")
    f.stt.say("what time is it")
    await _read_until(f.conversation, "assistant.done")

    # The note was about the story, which has since been picked up and heard.
    notes = [
        item["content"] for item in llm.requests[-1]["input"] if item.get("role") == "developer"
    ]
    assert not [note for note in notes if "did not hear the rest" in note]
    assert len(llm.labelled) == 1
    await f.conversation.close()


async def test_a_turn_that_ends_without_starting_does_not_wait_for_a_note() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta(_STORY), never], [_delta("Sure."), _completed(_message("Sure."))])
    llm.label_gate = asyncio.Event()  # a label would never come
    f = _conversation(llm)

    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.clock.advance(_DELAY + 4.5)
    f.stt.turn_end("stop")
    async with asyncio.timeout(voice_ent._LABEL_WAIT_SECONDS / 2):
        await _read_until(f.conversation, "assistant.done")

    assert _shape(llm.requests[1]["input"]) == ["user", "assistant", "user"]
    assert llm.labelled == []
    await f.conversation.close()


async def test_only_the_unheard_words_of_the_answer_go_to_the_labeller() -> None:
    call = _search_call("who won the last F1 race")
    answer = (
        "Norris won the race in Brazil ahead of two other drivers and it was very close indeed."
    )
    never = asyncio.Event()
    llm = _LLM(
        [_starts(call), _completed(call)],
        [_delta(answer), never],
        [_delta("Sure."), _completed(_message("Sure."))],
    )
    f = _conversation(llm)

    f.stt.say("who won the last race")
    await _read_until(f.conversation, "tool.call")
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.clock.advance(_DELAY + 3 + 2.5)  # all of "Let me check." and two words of the answer
    f.stt.say("stop")
    await _read_until(f.conversation, "assistant.done")

    # Not "Let me check.", which stays in the history, and not the two words heard.
    assert llm.labelled == [
        "the race in Brazil ahead of two other drivers and it was very close indeed."
    ]
    assert _shape(llm.requests[2]["input"])[-3:] == ["assistant", "developer", "user"]
    await f.conversation.close()


async def test_a_failed_label_leaves_the_plain_cut() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta(_STORY), never], [_delta("Sure."), _completed(_message("Sure."))])
    llm.label = RuntimeError("overloaded")
    f = _conversation(llm)

    sent = await _cut_the_story(f, "stop")

    assert _shape(sent) == ["user", "assistant", "user"]
    await f.conversation.close()


async def test_a_reply_cut_by_noise_is_resumed_without_a_note_on_the_remainder() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta(_STORY), never], [_delta("there was"), _completed(_message("there was"))])
    f = _conversation(llm)

    sent = await _cut_the_story(f, "")

    # The model is about to say the rest itself, so naming it would only be clutter.
    assert _shape(sent) == ["user", "assistant", "developer"]
    assert "background noise" in sent[2]["content"]
    await f.conversation.close()


# ---- putting the user's turn together


async def test_words_that_trail_a_sentence_join_the_same_turn_on_a_new_line() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("I am"), never],
        [_delta("Nothing much."), _completed(_message("Nothing much."))],
        [_delta("Bye."), _completed(_message("Bye."))],
    )
    f = _conversation(llm)

    # Speech recognition split one sentence into two turns.
    f.stt.say("Hello, what's")
    await _read_until(f.conversation, "assistant.delta")
    f.stt.say("happening?")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _event(outputs, "user.final").text == "happening?"
    assert llm.requests[1]["input"] == [{"role": "user", "content": "Hello, what's\nhappening?"}]

    # Once the reply has been heard, the turn is closed: the next words are a new turn.
    f.clock.advance(60)
    f.stt.say("bye")
    await _read_until(f.conversation, "assistant.done")
    assert llm.requests[2]["input"] == [
        {"role": "user", "content": "Hello, what's\nhappening?"},
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
    await _read_until(f.conversation, "assistant.delta")
    f.stt.say("हूँ")  # a noise, heard as one Devanagari word
    outputs = await _read_until(f.conversation, "assistant.done")

    # The noise never reaches the model or the screen, and the question is answered.
    assert "user.final" not in _types(outputs)
    assert llm.requests[1]["input"] == [
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
    await _read_until(f.conversation, "assistant.delta")
    f.stt.say("")  # a cough: a turn starts, cutting the reply, and ends with nothing in it
    outputs = await _read_until(f.conversation, "assistant.done")

    assert outputs[-1].text == "Paris."
    assert llm.requests[1]["input"] == [
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
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.clock.advance(_DELAY + 4.5)
    f.stt.say("")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _event(outputs, "interrupt").text == "Once upon a time…"
    sent = llm.requests[1]["input"]
    assert _shape(sent) == ["user", "assistant", "developer"]
    assert sent[1]["content"] == "Once upon a time…"
    assert "background noise" in sent[2]["content"]
    await f.conversation.close()


async def test_a_stray_word_just_after_the_reply_starts_is_dropped_and_the_reply_resumes() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("The capital of France is Paris."), never],
        [_delta("of France is Paris."), _completed(_message("of France is Paris."))],
    )
    f = _conversation(llm)

    f.stt.say("What is the capital of France?")
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.clock.advance(_DELAY + 2.5)  # two words of the reply heard, 2.65 s after the question
    f.stt.say("हूँ")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert "user.final" not in _types(outputs)
    sent = llm.requests[1]["input"]
    assert _shape(sent) == ["user", "assistant", "developer"]
    assert sent[1]["content"] == "The capital…"
    assert "background noise" in sent[2]["content"]
    await f.conversation.close()


async def test_the_same_word_well_into_the_reply_is_taken_as_the_user_speaking() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("Once upon a time there was a dragon who lived in a cave."), never],
        [_delta("ठीक है."), _completed(_message("ठीक है."))],
    )
    f = _conversation(llm)

    f.stt.say("Tell me a story.")
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    f.clock.advance(_DELAY + 6.5)
    f.stt.say("रुको")  # "stop", six seconds in: far too late to be a trailing noise
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _event(outputs, "user.final").text == "रुको"
    assert llm.requests[1]["input"][-1] == {"role": "user", "content": "रुको"}
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
    await _read_until(f.conversation, "assistant.delta")
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
    await _read_until(f.conversation, "assistant.delta")
    f.stt.say("? हूँ")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert "user.final" not in _types(outputs)
    assert llm.requests[1]["input"] == [
        {"role": "user", "content": "What is the capital of France?"},
    ]
    await f.conversation.close()


async def test_a_noise_word_inside_the_users_sentence_never_reaches_the_model() -> None:
    llm = _LLM([_delta("Paris."), _completed(_message("Paris."))])
    f = _conversation(llm)

    f.stt.say("What is the capital of France? हूँ")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert _event(outputs, "user.final").text == "What is the capital of France?"
    assert llm.requests[0]["input"] == [
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
    # The model went straight to searching, so we say "Let me check." for it. That is
    # its own utterance, finished before the search runs, and heard before the answer.
    assert [context.pushed for context in f.tts.contexts] == [["Let me check."], ["Norris won."]]
    assert all(context.finished for context in f.tts.contexts)
    assert _audio_seconds(outputs) == 5
    assert outputs[-1].text == "Let me check. Norris won."
    assert len(f.tts.sessions) == 1
    # The second call gets what was said, the call and its result.
    sent = llm.requests[1]["input"]
    assert _shape(sent) == ["user", "assistant", "function_call", "function_call_output"]
    assert sent[1]["content"] == "Let me check."
    assert sent[2]["call_id"] == sent[3]["call_id"] == "call_1"
    assert "Norris won in Las Vegas." in sent[3]["output"]
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


async def test_after_three_searches_the_model_has_to_answer() -> None:
    searches = [[_completed(_search_call(f"query {n}", f"call_{n}"))] for n in range(3)]
    llm = _LLM(*searches, [_delta("Here it is."), _completed(_message("Here it is."))])
    search = _Search()
    f = _conversation(llm, search)

    f.stt.say("dig into this")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert search.queries == ["query 0", "query 1", "query 2"]
    assert [request["tool_choice"] for request in llm.requests] == ["auto", "auto", "auto", "none"]
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
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    # "Let me check." takes three seconds; the answer starts right after it.
    f.clock.advance(_DELAY + 3 + 1.5)
    f.stt.say("stop")
    await _read_until(f.conversation, "assistant.done")

    sent = llm.requests[2]["input"]
    assert _shape(sent) == [
        "user",
        "assistant",
        "function_call",
        "function_call_output",
        "assistant",
        "user",
    ]
    assert sent[1]["content"] == "Let me check."
    assert sent[4]["content"] == "Norris…"
    await f.conversation.close()


async def test_what_was_said_before_a_search_is_kept_whole_once_the_model_has_seen_it() -> None:
    call = _search_call("who won the last F1 race")
    llm = _LLM(
        [_starts(call), _completed(call)],
        [_delta("Norris won the race."), _completed(_message("Norris won the race."))],
        [_delta("Sure."), _completed(_message("Sure."))],
    )
    f = _conversation(llm)

    # Everything is generated at once; the user speaks one second into "Let me check."
    f.stt.say("who won the last race")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(_DELAY + 1.2)
    f.stt.say("stop")
    outputs = await _read_until(f.conversation, "assistant.done")

    # "Let me check." had already gone back to the model with the search result, so it
    # stays as it was. The answer was never heard and is dropped.
    assert outputs[0] == VoiceEvent(type="interrupt", text="Let me check.")
    sent = llm.requests[2]["input"]
    assert _shape(sent) == ["user", "assistant", "function_call", "function_call_output", "user"]
    assert sent[1]["content"] == "Let me check."
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

    sent = llm.requests[1]["input"]
    outputs = {
        item["call_id"]: item["output"]
        for item in sent
        if item.get("type") == "function_call_output"
    }
    assert "Norris won in Las Vegas." in outputs["call_q"]
    assert "Still running" in outputs["call_s"]
    assert list(f.conversation._outliving) == ["call_s"]
    await f.conversation.close()


# ---- clearing old search results


def _searching_turns(count: int) -> list[list]:
    """Scripted model calls for `count` turns that each search once and then answer."""
    rounds: list[list] = []
    for n in range(count):
        call = _search_call(f"query {n}", f"call_{n}")
        rounds += [
            [_starts(call), _completed(call)],
            [_delta("Done."), _completed(_message("Done."))],
        ]
    return rounds


async def _ask(f: _Fixture, text: str) -> None:
    f.stt.say(text)
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)


def _results(request: dict) -> list[str]:
    """Each search result in a request: "full" or "cleared"."""
    return [
        "cleared" if item["output"] == voice_ent._CLEARED else "full"
        for item in request["input"]
        if item.get("type") == "function_call_output"
    ]


async def test_old_search_results_are_cleared_in_one_batch_between_turns() -> None:
    llm = _LLM(*_searching_turns(9), [_delta("Bye."), _completed(_message("Bye."))])
    f = _conversation(llm)

    for n in range(9):
        await _ask(f, f"question {n}")
    await _ask(f, "bye")

    def at_the_start_of_turn(n: int) -> list[str]:
        return _results(llm.requests[2 * n])

    # Seven full results are allowed to pile up. The turn after that, all but the newest
    # two are cleared at once, and nothing is cleared again for several turns.
    assert at_the_start_of_turn(6) == ["full"] * 6
    assert at_the_start_of_turn(7) == ["cleared"] * 5 + ["full"] * 2
    assert at_the_start_of_turn(8) == ["cleared"] * 5 + ["full"] * 3
    assert _results(llm.requests[-1]) == ["cleared"] * 5 + ["full"] * 4
    # A cleared result keeps its place and its call, so the history is still valid.
    sent = llm.requests[-1]["input"]
    assert [item["call_id"] for item in sent if item.get("type") == "function_call"] == [
        item["call_id"] for item in sent if item.get("type") == "function_call_output"
    ]
    await f.conversation.close()


async def test_a_result_the_model_has_not_seen_is_never_cleared() -> None:
    quick, slow = _search_call("quick one", "call_q"), _search_call("slow one", "call_s")
    llm = _LLM(
        *_searching_turns(6),
        [_starts(quick), _completed(quick, slow)],
        [_delta("Still looking."), _completed(_message("Still looking."))],
    )
    f = _conversation(llm, _Search(gate=asyncio.Event(), only="slow"))

    for n in range(6):
        await _ask(f, f"question {n}")
    # Two searches at once. One is back, but the model has not been shown its result yet,
    # because the reply is still waiting for the other when the user speaks.
    f.stt.say("look up two things")
    await _read_until(f.conversation, "tool.call")
    await _let_audio_out()
    f.stt.say("hello?")
    await _read_until(f.conversation, "assistant.done")

    # Seven real results are in the history, over the limit of six. But the model has
    # been sent only six of them, so nothing is cleared.
    assert _results(llm.requests[-1]).count("full") == 8  # seven results and a placeholder
    assert "cleared" not in _results(llm.requests[-1])
    await f.conversation.close()


async def test_placeholders_and_cancellations_do_not_push_real_results_out() -> None:
    slow = _search_call("slow one", "call_slow")
    cancel = _Item(
        type="function_call",
        call_id="call_c",
        name="cancel_tool_call",
        arguments=json.dumps({"call_id": "call_slow"}),
    )
    llm = _LLM(
        *_searching_turns(5),
        [_starts(slow), _completed(slow)],
        [_completed(cancel)],
        [_delta("Stopped."), _completed(_message("Stopped."))],
        [_delta("Bye."), _completed(_message("Bye."))],
    )
    f = _conversation(llm, _Search(gate=asyncio.Event(), only="slow"))

    for n in range(5):
        await _ask(f, f"question {n}")
    f.stt.say("look this up")
    await _read_until(f.conversation, "tool.call")
    await _ask(f, "never mind, stop that")
    await _ask(f, "bye")

    # Seven outputs are in the history, but only five are search results.
    outputs = [
        item for item in llm.requests[-1]["input"] if item.get("type") == "function_call_output"
    ]
    assert len(outputs) == 7
    assert "cleared" not in _results(llm.requests[-1])
    await f.conversation.close()


async def test_nothing_is_cleared_while_a_reply_is_being_made() -> None:
    three = [[_completed(_search_call(f"more {n}", f"call_m{n}"))] for n in range(3)]
    llm = _LLM(*_searching_turns(5), *three, [_delta("Here."), _completed(_message("Here."))])
    f = _conversation(llm)

    for n in range(5):
        await _ask(f, f"question {n}")
    await _ask(f, "dig deeper")

    # The last reply searched three more times, taking it to eight full results. All of
    # its own model calls saw every one of them in full.
    assert _results(llm.requests[-1]) == ["full"] * 8
    await f.conversation.close()


async def test_a_placeholder_for_a_search_still_running_is_never_cleared() -> None:
    slow = _search_call("slow one", "call_slow")
    llm = _LLM(
        [_starts(slow), _completed(slow)],
        [_delta("Still looking."), _completed(_message("Still looking."))],
        *_searching_turns(8),
    )
    f = _conversation(llm, _Search(gate=asyncio.Event(), only="slow"))

    f.stt.say("look this up")
    await _read_until(f.conversation, "tool.call")
    await _ask(f, "hello?")
    for n in range(8):
        await _ask(f, f"question {n}")

    kept = llm.requests[-1]["input"]
    placeholder = next(
        item for item in kept if item.get("call_id") == "call_slow" and "output" in item
    )
    assert "Still running" in placeholder["output"]
    assert "cleared" in _results(llm.requests[-1])
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
    await _let_audio_out()
    f.clock.advance(_DELAY + 3)  # "Let me check." was heard in full
    f.stt.say("hello, what's happening?")
    await _read_until(f.conversation, "assistant.done")

    sent = llm.requests[1]["input"]
    assert _shape(sent) == ["user", "assistant", "function_call", "function_call_output", "user"]
    assert sent[1]["content"] == "Let me check."
    assert sent[2]["call_id"] == sent[3]["call_id"] == "call_1"
    # The placeholder says the result comes later, names the call, and how to stop it.
    assert "Still running" in sent[3]["output"]
    assert '"deferred_result_for": "call_1"' in sent[3]["output"]
    assert "cancel_tool_call" in sent[3]["output"]
    # The search was not stopped: it is still waiting.
    assert search.queries == ["weather in Pune"]
    assert f.conversation._outliving["call_1"].running.done() is False
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

    sent = llm.requests[2]["input"]
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
    assert f.conversation._stubs == {stub["call_id"]: "call_1"}
    await f.conversation.close()


async def test_a_result_that_lands_while_the_user_speaks_is_brought_up_after_their_answer() -> None:
    llm, finish = _slow_search_then(
        [_delta("Four."), _completed(_message("Four."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
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

    # First their question is answered, with the search still shown as running.
    assert outputs[-1].text == "Four."
    asked = llm.requests[1]["input"]
    assert _shape(asked) == ["user", "function_call", "function_call_output", "user"]
    assert "Still running" in asked[2]["output"]

    # Then, with nobody saying anything, the result is given to the model and spoken.
    f.clock.advance(60)
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "It is sunny."
    assert "user.final" not in _types(outputs)
    told = llm.requests[2]["input"]
    assert _shape(told)[-4:] == ["user", "assistant", "function_call", "function_call_output"]
    assert json.loads(told[-2]["arguments"]) == {"deferred_result_for": "call_1"}
    assert "Norris won in Las Vegas." in told[-1]["output"]

    # They have now heard it. Their next turn is answered and nothing is brought up again.
    f.clock.advance(60)
    f.stt.say("thanks")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 4
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
    await _read_until(f.conversation, "assistant.delta")
    f.stt.say("")
    outputs = await _read_until(f.conversation, "assistant.done")

    assert outputs[-1].text == "It is sunny."
    again = llm.requests[3]["input"]
    assert _shape(again)[-3:] == ["function_call", "function_call_output", "developer"]
    assert "has not been told to the user" in again[-1]["content"]

    # Once it has been heard, the user is owed nothing more.
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 4
    await f.conversation.close()


async def test_a_result_whose_reply_the_user_talked_over_is_told_after_their_answer() -> None:
    never = asyncio.Event()
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("It is"), never],
        [_delta("Four."), _completed(_message("Four."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    await _interrupted_search_then_hello(f, finish)
    await _read_until(f.conversation, "assistant.delta")
    f.stt.say("what is two plus two?")
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "Four."

    f.clock.advance(60)
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "It is sunny."
    assert "has not been told to the user" in llm.requests[4]["input"][-1]["content"]
    await f.conversation.close()


async def test_the_reminder_follows_a_question_that_got_no_answer() -> None:
    never = asyncio.Event()
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("It is"), never],
        [failed],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    await _interrupted_search_then_hello(f, finish)
    await _read_until(f.conversation, "assistant.delta")
    # The user talks over the reply about the result, and the answer to them fails.
    f.stt.say("what is two plus two?")
    await _read_until(f.conversation, "error")
    outputs = await _read_until(f.conversation, "assistant.done")

    # Their question stays where the model saw it, and the reminder comes after it.
    assert outputs[-1].text == "It is sunny."
    assert _shape(llm.requests[4]["input"])[-2:] == ["user", "developer"]
    await f.conversation.close()


async def test_words_said_before_a_search_but_never_heard_do_not_count_as_telling() -> None:
    more = _search_call("forecast for tomorrow", "call_2")
    never = asyncio.Event()
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("It is sunny."), _starts(more), _completed(_message("It is sunny."), more)],
        [_delta("And tomorrow"), never],
        [_delta("Four."), _completed(_message("Four."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish, only="weather"))

    await _interrupted_search_then_hello(f, finish)
    # The reply about the result says it, searches again and goes on, all before any of
    # it has played. Then the user speaks.
    await _read_until(f.conversation, "tool.call")
    await _read_until(f.conversation, "assistant.delta")
    f.stt.say("what is two plus two?")
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "Four."

    # "It is sunny." is in the history, because the model was sent it. The user never
    # heard it, so they are still told.
    f.clock.advance(60)
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "It is sunny."
    await f.conversation.close()


async def test_a_reply_about_a_result_that_says_nothing_does_not_set_off_reminders() -> None:
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
    reminders = [i for i in llm.requests[-1]["input"] if i.get("role") == "developer"]
    assert reminders == []
    await f.conversation.close()


async def test_a_result_is_still_owed_if_its_reply_was_cut_off_before_it_said_anything() -> None:
    never = asyncio.Event()
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [never],  # the model is still thinking about the result when the user speaks
        [_delta("Four."), _completed(_message("Four."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    await _interrupted_search_then_hello(f, finish)
    await asyncio.sleep(0.25)  # the reply about the result has been asked for
    assert len(llm.requests) == 3
    f.stt.say("what is two plus two?")
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "Four."

    f.clock.advance(60)
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "It is sunny."
    await f.conversation.close()


async def test_a_result_is_still_owed_if_its_reply_failed() -> None:
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [failed],
        [_delta("Four."), _completed(_message("Four."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    await _interrupted_search_then_hello(f, finish)
    await _read_until(f.conversation, "error")
    f.stt.say("what is two plus two?")
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "Four."

    f.clock.advance(60)
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "It is sunny."
    await f.conversation.close()


async def test_a_result_is_brought_up_again_only_once() -> None:
    never = asyncio.Event()
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("It is"), never],
        [_delta("Four."), _completed(_message("Four."))],
        [_delta("It is"), never],
        [_delta("Five."), _completed(_message("Five."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    await _interrupted_search_then_hello(f, finish)
    # Twice the reply about the result is talked over before any of it is heard.
    for question in ("what is two plus two?", "and two plus three?"):
        await _read_until(f.conversation, "assistant.delta")
        f.stt.say(question)
        await _read_until(f.conversation, "assistant.done")
        f.clock.advance(60)
    await asyncio.sleep(0.25)

    # It was tried once more after the first time, and then left: the result is in the
    # history for whenever the user asks.
    assert len(llm.requests) == 6
    reminders = [i for i in llm.requests[-1]["input"] if i.get("role") == "developer"]
    assert len(reminders) == 1
    await f.conversation.close()


async def test_a_result_waiting_its_turn_keeps_waiting_while_the_user_talks() -> None:
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("Yes."), _completed(_message("Yes."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("hello, what's happening?")
    await _read_until(f.conversation, "assistant.done")

    # The result lands while "Still looking." is playing, so it waits. The user speaks
    # over that reply: they are answered first, and the result still waits.
    finish.set()
    await asyncio.sleep(0.05)
    f.stt.say("are you there?")
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "Yes."
    assert _shape(llm.requests[2]["input"])[-1] == "user"

    # Once that answer has been heard, the result gets its own reply.
    f.clock.advance(60)
    outputs = await _read_until(f.conversation, "assistant.done")
    assert outputs[-1].text == "It is sunny."
    assert _shape(llm.requests[3]["input"])[-2:] == ["function_call", "function_call_output"]
    assert len(llm.requests) == 4
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
    assert _shape(llm.requests[2]["input"])[-3:] == [
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
    await _read_until(f.conversation, "assistant.delta")
    # The user is heard to start again, the search finishes, and nothing was said.
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()
    await asyncio.sleep(0.05)
    f.stt.turn_end("")
    await _read_until(f.conversation, "assistant.done")

    # "hello?" is simply answered again. No reply had been heard, so nothing in the
    # history says one was cut off by noise.
    again = llm.requests[2]["input"]
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
        [_delta("So."), _completed(_message("So."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
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
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    outputs = await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await asyncio.sleep(0.25)

    # One answer to "so?", one reply bringing up the result, and nothing after that.
    assert outputs[-1].text == "It is sunny."
    assert len(llm.requests) == 4
    await f.conversation.close()


async def test_a_turn_that_ends_without_starting_cannot_set_off_two_replies() -> None:
    llm, finish = _slow_search_then(
        [_delta("Okay."), _completed(_message("Okay."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    # No turn start this time, and the search finishes while the reply is being stopped.
    f.stt.turn_end("never mind")
    finish.set()
    await _read_until(f.conversation, "assistant.done")
    # Only the answer to the user is under way. The result waits for it to be heard.
    assert len(llm.requests) == 2
    assert _shape(llm.requests[1]["input"]) == [
        "user",
        "function_call",
        "function_call_output",
        "user",
    ]

    f.clock.advance(60)
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 3
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

    # Cancelling is not a search, so nobody says "Let me check." for it.
    assert outputs[-1].text == "Stopped."
    assert llm.requests[2]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_2",
        "output": "Cancelled.",
    }
    # The cancelled search never reports back.
    finish.set()
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 3
    assert f.conversation._outliving == {}
    await f.conversation.close()


async def test_a_result_that_landed_while_the_user_spoke_still_counts_as_pending() -> None:
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
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

    # The model is shown the placeholder and not yet the result, exactly as if the search
    # were still running. So it is asked to think here too.
    asked = llm.requests[1]
    assert "Still running" in asked["input"][-2]["output"]
    assert asked["reasoning"]["effort"] == "low"

    f.clock.advance(60)
    await _read_until(f.conversation, "assistant.done")
    assert llm.requests[2]["reasoning"]["effort"] == "none"
    await f.conversation.close()


async def test_the_model_can_cancel_a_result_that_is_waiting_to_be_delivered() -> None:
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
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()  # the search finishes while the user is still speaking
    await asyncio.sleep(0.05)
    f.stt.turn_end("never mind, stop that")
    await _read_until(f.conversation, "assistant.done")

    assert llm.requests[2]["input"][-1]["output"] == "Cancelled."
    # The result the user called off is never brought up.
    f.clock.advance(60)
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 3
    assert f.conversation._late == []
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

    assert llm.requests[1]["input"][-1]["output"] == "Nothing is running under that call_id."
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
    search_task = f.conversation._outliving["call_1"].running
    await f.conversation.close()

    assert search_task.cancelled()


# ---- how much was heard


def _utterance(text: str, seconds_per_word: float = 1.0, *, complete: bool = True) -> Utterance:
    """An utterance whose audio starts playing at time 0, sent as one chunk per word."""
    utterance = Utterance(context=None)
    utterance.text = text
    for n, word in enumerate(text.split()):
        utterance.add_words([CartesiaTTSWord(word, (n + 1) * seconds_per_word)])
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
    utterance.add_words([CartesiaTTSWord(word, n + 1.0) for n, word in enumerate(spoken)])

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
    await _read_until(f.conversation, "assistant.delta")
    await _let_audio_out()
    # All eight words have been sent. Only now does the browser know its real delay.
    f.conversation.set_output_delay(1.0)
    f.clock.advance(_DELAY + 3.5)  # three and a half words played, but heard a second later
    f.stt.turn_start()
    outputs = await _read_until(f.conversation, "interrupt")

    assert outputs[-1].text == "Once upon…"
    await f.conversation.close()


class _Socket:
    """A client WebSocket that plays back a fixed list of frames, then disconnects."""

    def __init__(self, *frames: dict) -> None:
        self._frames = [*frames, {"type": "websocket.disconnect"}]

    async def receive(self) -> dict:
        return self._frames.pop(0)


class _Listener:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.delays: list[float] = []

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    def set_output_delay(self, seconds: float) -> None:
        self.delays.append(seconds)


async def test_the_service_takes_audio_and_the_playback_report_and_ignores_the_rest() -> None:
    listener = _Listener()
    socket = _Socket(
        {"type": "websocket.receive", "bytes": b"\x01\x02"},
        {"type": "websocket.receive", "text": json.dumps({"type": "playback", "delay": 0.18})},
        {"type": "websocket.receive", "text": json.dumps({"type": "playback", "delay": -1})},
        {"type": "websocket.receive", "text": json.dumps({"type": "something else"})},
        {"type": "websocket.receive", "text": "not json"},
        {"type": "websocket.receive", "bytes": b"\x03"},
    )

    await VoiceServiceV1Class._relay_mic(socket, listener)

    assert listener.audio == [b"\x01\x02", b"\x03"]
    assert listener.delays == [0.18]


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

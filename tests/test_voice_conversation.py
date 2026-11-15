"""The voice cascade with all three services faked: turns in, speech and history out.

Time is faked too. The fake text-to-speech makes every word last exactly one second, and
the tests move a clock by hand, so each one can say how much of a reply the user heard.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.adapters.cartesia_tts_adp import CartesiaTTSSession, CartesiaTTSWord
from app.entities.voice_ent import VoiceConversation, VoiceEvent, _is_stray
from app.entities.voice_speech import Playback, Utterance

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

    def __init__(self) -> None:
        self._events: asyncio.Queue[bytes | list[CartesiaTTSWord] | None] = asyncio.Queue()
        self._seconds = 0
        self.pushed: list[str] = []
        self.finished = False
        self.cancelled = False

    async def push(self, text: str) -> None:
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

    def context(self) -> _TTSContext:
        self._contexts.append(_TTSContext())
        return self._contexts[-1]

    async def close(self) -> None:
        self.closed = True


class _TTS:
    def __init__(self) -> None:
        self.sessions: list[_TTSSession] = []
        self.contexts: list[_TTSContext] = []

    async def connect(self) -> _TTSSession:
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
    that point on. The one allowed change is the user's open turn, which is always the
    last item and can gain a line.
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

    assert _event(outputs, "interrupt").text == ""
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


@pytest.mark.parametrize(
    ("fragment", "turn_so_far", "stray"),
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
    fragment: str, turn_so_far: str, stray: bool
) -> None:
    assert _is_stray(fragment, turn_so_far) is stray


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


async def test_a_late_result_that_lands_while_the_user_speaks_goes_ahead_of_their_turn() -> None:
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

    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 2  # no reply of its own while the user has the floor

    f.stt.turn_end("so is it sunny?")
    await _read_until(f.conversation, "assistant.done")
    assert _shape(llm.requests[2]["input"])[-4:] == [
        "assistant",
        "function_call",
        "function_call_output",
        "user",
    ]
    assert len(llm.requests) == 3
    await f.conversation.close()


async def test_the_user_speaking_takes_over_from_a_late_result_waiting_its_turn() -> None:
    llm, finish = _slow_search_then(
        [_delta("Still looking."), _completed(_message("Still looking."))],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    f.stt.say("hello, what's happening?")
    await _read_until(f.conversation, "assistant.done")

    # The result lands while "Still looking." is playing, so it waits. Then the user
    # speaks over that reply: the wait is off, and their turn carries the result.
    finish.set()
    await asyncio.sleep(0.05)
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    await asyncio.sleep(0.25)
    assert len(llm.requests) == 2

    f.stt.turn_end("so is it sunny?")
    await _read_until(f.conversation, "assistant.done")
    assert _shape(llm.requests[2]["input"])[-3:] == [
        "function_call",
        "function_call_output",
        "user",
    ]
    assert len(llm.requests) == 3
    await f.conversation.close()


async def test_a_late_result_is_still_answered_if_the_user_said_nothing_after_all() -> None:
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

    # A noise opens a turn, the result lands, and the turn closes with no words in it.
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


async def test_a_late_result_follows_a_turn_the_model_has_already_been_shown() -> None:
    never = asyncio.Event()
    llm, finish = _slow_search_then(
        [_delta("Hmm"), never],
        [_delta("It is sunny."), _completed(_message("It is sunny."))],
    )
    f = _conversation(llm, _Search(gate=finish))

    f.stt.say("what's the weather")
    await _read_until(f.conversation, "tool.call")
    # A new turn is sent to the model, but the user speaks again before hearing a reply.
    f.stt.say("hello?")
    await _read_until(f.conversation, "assistant.delta")
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()
    await asyncio.sleep(0.05)
    f.stt.turn_end("are you there?")
    await _read_until(f.conversation, "assistant.done")

    # "hello?" stays where the model saw it. The result comes after it, not before.
    sent = llm.requests[2]["input"]
    assert _shape(sent)[-4:] == ["user", "function_call", "function_call_output", "user"]
    assert sent[-4]["content"] == "hello?"
    assert sent[-1]["content"] == "are you there?"
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


def test_a_word_timing_that_does_not_match_the_text_ends_the_count_there() -> None:
    utterance = Utterance(context=None)
    utterance.text = "It costs $5 million."
    utterance.add_audio(0.0, 5.0)
    # The speech service spelled the amount out, so its words stop lining up with ours.
    spoken = ["It", "costs", "five", "dollars", "million."]
    utterance.add_words([CartesiaTTSWord(word, n + 1.0) for n, word in enumerate(spoken)])

    assert utterance.heard(4.5) == ("It costs", False)


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

"""The voice cascade with all three services faked: turns in, speech and history out."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.adapters.cartesia_tts_adp import CartesiaTTSSession
from app.entities.voice_ent import VoiceConversation, VoiceEvent


class _Item(SimpleNamespace):
    """A model output item, as the OpenAI SDK returns it."""

    def to_dict(self) -> dict[str, object]:
        return dict(vars(self))


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
    """Text-to-speech for one reply: one audio chunk per pushed fragment.

    The chunk is the fragment's own bytes, or `seconds` of silence when that is given.
    """

    def __init__(self, seconds: float = 0) -> None:
        self._seconds = seconds
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.pushed: list[str] = []
        self.finished = False
        self.cancelled = False

    async def push(self, text: str) -> None:
        self.pushed.append(text)
        silence = b"\x00" * int(48000 * self._seconds)  # 24 kHz, 16-bit
        self._chunks.put_nowait(silence or text.encode())

    async def finish(self) -> None:
        self.finished = True
        self._chunks.put_nowait(None)

    async def audio(self):
        while (chunk := await self._chunks.get()) is not None:
            yield chunk

    async def cancel(self) -> None:
        self.cancelled = True


class _TTSSession(CartesiaTTSSession):
    """One reply's text-to-speech connection."""

    def __init__(self, contexts: list[_TTSContext], seconds: float) -> None:
        self._contexts = contexts
        self._seconds = seconds
        self.closed = False

    def context(self) -> _TTSContext:
        self._contexts.append(_TTSContext(self._seconds))
        return self._contexts[-1]

    async def close(self) -> None:
        self.closed = True


class _TTS:
    def __init__(self, seconds_per_fragment: float = 0) -> None:
        self._seconds = seconds_per_fragment
        self.sessions: list[_TTSSession] = []
        self.contexts: list[_TTSContext] = []

    async def connect(self) -> _TTSSession:
        self.sessions.append(_TTSSession(self.contexts, self._seconds))
        return self.sessions[-1]


class _LLM:
    """The model: each call plays the next scripted list of stream events.

    A script entry of `asyncio.Event` pauses the stream there until the test sets it.
    """

    def __init__(self, *rounds: list) -> None:
        self._rounds = list(rounds)
        self.requests: list[dict] = []

    async def stream_response(self, **request):
        self.requests.append({**request, "input": list(request["input"])})
        script = self._rounds.pop(0)

        async def stream():
            for step in script:
                if isinstance(step, asyncio.Event):
                    await step.wait()
                else:
                    yield step

        return stream()


class _Search:
    def __init__(self, *, configured: bool = True, gate: asyncio.Event | None = None) -> None:
        self.configured = configured
        self.queries: list[str] = []
        self._gate = gate

    async def search(self, *, objective: str, queries: list[str], mode: str):
        assert mode == "turbo"
        self.queries.append(objective)
        if self._gate is not None:
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


def _conversation(llm: _LLM, search: _Search | None = None, tts: _TTS | None = None):
    stt, tts = _STT(), tts or _TTS()
    conversation = VoiceConversation(stt=stt, tts=tts, llm=llm, search=search or _Search())
    return conversation, stt, tts


async def _read_until(conversation: VoiceConversation, type_: str) -> list[VoiceEvent | bytes]:
    """Collect outputs up to and including the first event of the given type."""
    outputs: list[VoiceEvent | bytes] = []
    async with asyncio.timeout(2):
        async for output in conversation.outputs():
            outputs.append(output)
            if isinstance(output, VoiceEvent) and output.type == type_:
                return outputs
    raise AssertionError(f"conversation ended before {type_!r}")


async def _let_it_play() -> None:
    """Wait out the reply's audio. The fakes' chunks are a few bytes: microseconds of sound."""
    await asyncio.sleep(0.01)


def _types(outputs: list[VoiceEvent | bytes]) -> list[str]:
    return ["audio" if isinstance(output, bytes) else output.type for output in outputs]


def _audio(outputs: list[VoiceEvent | bytes]) -> bytes:
    return b"".join(output for output in outputs if isinstance(output, bytes))


async def test_a_finished_turn_is_answered_in_speech() -> None:
    llm = _LLM([_delta("Hi "), _delta("there."), _completed(_message("Hi there."))])
    conversation, stt, tts = _conversation(llm)

    stt.say("hello")
    outputs = await _read_until(conversation, "assistant.done")

    assert _types(outputs)[:2] == ["interrupt", "user.final"]
    assert _audio(outputs) == b"Hi there."
    assert outputs[-1] == VoiceEvent(type="assistant.done", text="Hi there.")
    assert tts.contexts[0].finished
    assert tts.sessions[0].closed
    assert llm.requests[0]["input"] == [{"role": "user", "content": "hello"}]
    assert llm.requests[0]["store"] is False
    assert llm.requests[0]["model"] == "6 Luna"
    assert llm.requests[0]["reasoning"] == {"effort": "none"}
    await conversation.close()


async def test_the_next_turn_sends_the_whole_conversation_so_far() -> None:
    llm = _LLM(
        [_delta("Hi."), _completed(_message("Hi."))],
        [_delta("Fine."), _completed(_message("Fine."))],
    )
    conversation, stt, _tts = _conversation(llm)

    stt.say("hello")
    await _read_until(conversation, "assistant.done")
    await _let_it_play()
    stt.say("how are you")
    await _read_until(conversation, "assistant.done")

    sent = llm.requests[1]["input"]
    assert [item.get("role") for item in sent] == ["user", "assistant", "user"]
    assert sent[2] == {"role": "user", "content": "how are you"}
    await conversation.close()


async def test_a_search_runs_between_two_model_calls() -> None:
    call = _search_call("who won the last F1 race")
    llm = _LLM(
        [_starts(call), _completed(call)],
        [_delta("Norris won."), _completed(_message("Norris won."))],
    )
    search = _Search()
    conversation, stt, tts = _conversation(llm, search)

    stt.say("who won the last race")
    outputs = await _read_until(conversation, "assistant.done")

    assert search.queries == ["who won the last F1 race"]
    assert VoiceEvent(type="tool.call", text="who won the last F1 race") in outputs
    # The model went straight to searching, so we say "Let me check." for it. That is
    # its own utterance, finished before the search runs, and heard before the answer.
    assert [context.pushed for context in tts.contexts] == [["Let me check."], ["Norris won."]]
    assert all(context.finished for context in tts.contexts)
    assert _audio(outputs) == b"Let me check.Norris won."
    assert outputs[-1].text == "Let me check. Norris won."
    assert len(tts.sessions) == 1
    # The second call gets the first call's output and the search result.
    sent = llm.requests[1]["input"]
    assert [item.get("type", item.get("role")) for item in sent] == [
        "user",
        "function_call",
        "function_call_output",
    ]
    assert sent[2]["call_id"] == "call_1"
    assert "Norris won in Las Vegas." in sent[2]["output"]
    assert llm.requests[0]["tools"][0]["name"] == "web_search"
    await conversation.close()


async def test_a_model_that_announces_its_search_is_not_talked_over() -> None:
    call = _search_call("who won the last F1 race")
    llm = _LLM(
        [_delta("One moment."), _starts(call), _completed(_message("One moment."), call)],
        [_delta("Norris won."), _completed(_message("Norris won."))],
    )
    conversation, stt, tts = _conversation(llm)

    stt.say("who won the last race")
    await _read_until(conversation, "assistant.done")

    assert [context.pushed for context in tts.contexts] == [["One moment."], ["Norris won."]]
    await conversation.close()


async def test_without_a_search_key_the_tool_is_not_offered() -> None:
    llm = _LLM([_delta("Hi."), _completed(_message("Hi."))])
    conversation, stt, _tts = _conversation(llm, _Search(configured=False))

    stt.say("hello")
    await _read_until(conversation, "assistant.done")

    assert llm.requests[0]["tools"] == []
    assert "web_search" not in llm.requests[0]["instructions"]
    await conversation.close()


async def test_speaking_over_a_reply_stops_it() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_delta("Once upon a time"), never, _delta(" there was"), _completed()],
        [_delta("Sure."), _completed(_message("Sure."))],
    )
    conversation, stt, tts = _conversation(llm)

    stt.say("tell me a story")
    await _read_until(conversation, "assistant.delta")
    stt.turn_start()
    outputs = await _read_until(conversation, "interrupt")

    # The speech stream was cancelled, and nothing after the interrupt belongs to it.
    assert tts.contexts[0].cancelled
    assert not tts.contexts[0].finished
    assert tts.sessions[0].closed
    assert _types(outputs)[-1] == "interrupt"

    stt.turn_end("stop, just say sure")
    outputs = await _read_until(conversation, "assistant.done")
    assert _audio(outputs) == b"Sure."

    # The model is told what it had said and that it was cut off.
    sent = llm.requests[1]["input"]
    assert [item["role"] for item in sent] == ["user", "assistant", "developer", "user"]
    assert sent[1]["content"] == "Once upon a time"
    assert "interrupted" in sent[2]["content"]
    await conversation.close()


async def test_interrupting_a_search_leaves_no_half_finished_tool_call() -> None:
    never = asyncio.Event()
    llm = _LLM(
        [_completed(_search_call("weather in Pune"))],
        [_delta("Okay."), _completed(_message("Okay."))],
    )
    search = _Search(gate=never)
    conversation, stt, _tts = _conversation(llm, search)

    stt.say("what's the weather")
    await _read_until(conversation, "tool.call")
    stt.say("never mind")
    await _read_until(conversation, "assistant.done")

    # A function_call without its output would be rejected by the API.
    sent = llm.requests[1]["input"]
    assert sent == [
        {"role": "user", "content": "what's the weather"},
        {"role": "user", "content": "never mind"},
    ]
    await conversation.close()


async def test_a_turn_start_always_tells_the_client_to_stop_playing() -> None:
    llm = _LLM([_delta("Hi."), _completed(_message("Hi."))])
    conversation, stt, _tts = _conversation(llm)

    stt.say("hello")
    await _read_until(conversation, "assistant.done")
    # The reply is finished here, but the client may still be playing its audio.
    stt.turn_start()
    outputs = await _read_until(conversation, "interrupt")

    assert _types(outputs) == ["interrupt"]
    await conversation.close()


async def test_interrupting_a_reply_that_is_still_playing_is_noted_for_the_model() -> None:
    llm = _LLM(
        [_delta("A long story."), _completed(_message("A long story."))],
        [_delta("Sure."), _completed(_message("Sure."))],
    )
    # The reply is finished on the server at once, but takes 30 seconds to play.
    conversation, stt, _tts = _conversation(llm, tts=_TTS(seconds_per_fragment=30))

    stt.say("tell me a story")
    await _read_until(conversation, "assistant.done")
    stt.say("stop")
    await _read_until(conversation, "assistant.done")

    sent = llm.requests[1]["input"]
    assert [item.get("role") for item in sent] == ["user", "assistant", "developer", "user"]
    assert "In full it takes about 30 seconds to say." in sent[2]["content"]
    assert "They did not hear the rest." in sent[2]["content"]
    await conversation.close()


async def test_a_reply_that_was_heard_in_full_is_not_noted_as_interrupted() -> None:
    llm = _LLM(
        [_delta("Hi."), _completed(_message("Hi."))],
        [_delta("Fine."), _completed(_message("Fine."))],
    )
    conversation, stt, _tts = _conversation(llm)

    stt.say("hello")
    await _read_until(conversation, "assistant.done")
    await _let_it_play()
    stt.say("how are you")
    await _read_until(conversation, "assistant.done")

    assert [item.get("role") for item in llm.requests[1]["input"]] == ["user", "assistant", "user"]
    await conversation.close()


async def test_a_model_failure_is_reported_and_the_conversation_goes_on() -> None:
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    llm = _LLM([failed], [_delta("Hi."), _completed(_message("Hi."))])
    conversation, stt, _tts = _conversation(llm)

    stt.say("hello")
    outputs = await _read_until(conversation, "error")
    assert outputs[-1].text == "reply failed: overloaded"

    stt.say("hello again")
    outputs = await _read_until(conversation, "assistant.done")
    assert _audio(outputs) == b"Hi."
    await conversation.close()


async def test_closing_mid_reply_releases_both_speech_connections() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta("Once"), never])
    conversation, stt, tts = _conversation(llm)

    stt.say("tell me a story")
    await _read_until(conversation, "assistant.delta")
    await conversation.close()

    assert stt.closed
    assert tts.contexts[0].cancelled
    assert tts.sessions[0].closed


async def test_after_three_searches_the_model_has_to_answer() -> None:
    searches = [[_completed(_search_call(f"query {n}", f"call_{n}"))] for n in range(3)]
    llm = _LLM(*searches, [_delta("Here it is."), _completed(_message("Here it is."))])
    search = _Search()
    conversation, stt, _tts = _conversation(llm, search)

    stt.say("dig into this")
    outputs = await _read_until(conversation, "assistant.done")

    assert search.queries == ["query 0", "query 1", "query 2"]
    assert [request["tool_choice"] for request in llm.requests] == ["auto", "auto", "auto", "none"]
    # The start of every request is identical, so a prompt cache keeps working.
    assert len({request["instructions"] for request in llm.requests}) == 1
    assert all(request["tools"] == llm.requests[0]["tools"] for request in llm.requests)
    assert _audio(outputs) == b"Here it is."


@pytest.mark.parametrize("transcript", ["", "   "])
async def test_an_empty_transcript_starts_no_reply(transcript: str) -> None:
    llm = _LLM()
    conversation, stt, _tts = _conversation(llm)

    stt.say(transcript)
    stt.turn_start()
    await _read_until(conversation, "interrupt")
    outputs = await _read_until(conversation, "interrupt")

    # Nothing between the two turn starts: no user.final, so no reply was started.
    assert _types(outputs) == ["interrupt"]
    assert llm.requests == []
    await conversation.close()

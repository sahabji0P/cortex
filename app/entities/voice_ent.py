"""
Voice entity: a spoken conversation as a cascade of three services.

    mic audio ──▶ speech-to-text ──▶ model ──▶ text-to-speech ──▶ speech audio
                  (Cartesia)         (OpenAI)   (Cartesia)

One `VoiceConversation` per client. The speech-to-text service also detects turns:

- `turn.end`   the user finished a sentence. Its transcript starts a reply: the model's
               text is pushed to text-to-speech as it streams, and the audio is forwarded.
- `turn.start` the user began speaking. Whatever reply is in progress is cancelled (model
               and speech), and the client is told to drop audio it has not played yet.

The model may call `web_search` (Parallel) before it answers.
"""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import BaseModel

from app.adapters.cartesia_stt_adp import CartesiaSTTAdapterClass, CartesiaSTTSession
from app.adapters.cartesia_tts_adp import (
    CartesiaTTSAdapterClass,
    CartesiaTTSContext,
    CartesiaTTSSession,
)
from app.adapters.openai_adp import OpenAIAdapterClass, ResponseInputItemParam
from app.adapters.parallel_adp import ParallelAdapterClass, SearchResult

__all__ = ["VoiceConversation", "VoiceEntityClass", "VoiceEvent"]

# How many times one reply may search before it has to answer.
_MAX_SEARCHES = 3

_INSTRUCTIONS = """\
You are Cortex, a voice assistant in a live spoken conversation. Everything you write \
is read aloud by a text-to-speech voice. Today is {today}.

- Talk like a person on a call: short, natural sentences. Keep to one or two sentences \
unless the user asks for more.
- Use plain words only: no markdown, lists, emoji, links or symbols. Write numbers the \
way they are said.
- The user's words come from speech recognition and may contain mistakes. Go with the \
likely meaning, and ask when it is unclear.
- The user can interrupt you at any time. When they do, drop what you were saying and \
answer what they just said.
"""

_SEARCH_INSTRUCTIONS = """\
- For anything recent, or that you are not sure of, call web_search straight away. The \
user hears "Let me check" while it runs, so do not announce the search yourself. Then \
answer from the results and name the source.
"""

# Spoken by us the moment the model starts a search without having said anything, so the
# user is not left in silence. Models do this themselves only some of the time.
_SEARCHING_LINE = "Let me check."

# Speech audio is raw 16-bit mono PCM, so this converts a byte count to playing time.
_SPEECH_BYTES_PER_SECOND = CartesiaTTSAdapterClass.sample_rate * 2


class VoiceEvent(BaseModel):
    """One JSON message for the client. Speech audio travels separately, as binary.

    - `user.partial`    the user's words so far, while they are still speaking
    - `user.final`      the user's finished sentence; a reply follows
    - `assistant.delta` the next piece of the reply's text
    - `assistant.done`  the reply's audio has all been sent (full text in `text`)
    - `tool.call`       the assistant is searching the web (query in `text`)
    - `interrupt`       the user started speaking: stop playback and drop queued audio
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


class _Speech:
    """The spoken side of one reply: text in, audio forwarded to the client.

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
        out: asyncio.Queue[VoiceEvent | bytes | None],
    ) -> None:
        self._out = out
        self._connecting = asyncio.create_task(tts.connect())
        self._utterance: CartesiaTTSContext | None = None
        self._open = False  # whether the utterance still takes text
        self._forwarding: asyncio.Task[None] | None = None
        self._first_audio_at: float | None = None
        self._audio_seconds = 0.0
        self._finished = False
        self.text = ""

    async def say(self, text: str) -> None:
        """Speak the next piece of text, starting a new utterance if none is open."""
        if self._utterance is None or not self._open:
            if self._forwarding is not None:
                # The previous utterance's audio goes out before this one's.
                await self._forwarding
            self._utterance = (await self._connecting).context()
            self._open = True
            self._forwarding = asyncio.create_task(self._forward(self._utterance))
        self.text += text
        await self._utterance.push(text)

    async def _forward(self, utterance: CartesiaTTSContext) -> None:
        async for audio in utterance.audio():
            if self._first_audio_at is None:
                self._first_audio_at = time.monotonic()
            self._audio_seconds += len(audio) / _SPEECH_BYTES_PER_SECOND
            self._out.put_nowait(audio)

    async def end_utterance(self) -> None:
        """No more text for this utterance. Its remaining audio is still forwarded."""
        if self._utterance is not None and self._open:
            self._open = False
            self.text += " "
            await self._utterance.finish()

    async def finish(self) -> None:
        """No more text at all. Return once all the audio has been forwarded."""
        try:
            await self.end_utterance()
            if self._forwarding is not None:
                await self._forwarding
            self._finished = True
        finally:
            await self._disconnect()

    async def stop(self) -> None:
        """Stop speaking now and forward no more audio."""
        if self._forwarding is not None:
            self._forwarding.cancel()
            await asyncio.gather(self._forwarding, return_exceptions=True)
        if self._utterance is not None:
            with contextlib.suppress(Exception):
                await self._utterance.cancel()
        await self._disconnect()

    def interrupted_note(self) -> ResponseInputItemParam | None:
        """What to tell the model if the user starts speaking now; None if they heard it all.

        Speech is generated faster than it plays, so the client is usually still playing
        a reply after its last audio was sent. How much was heard is estimated from the
        clock, taking playback to start when the first audio is sent.
        """
        heard = 0.0 if self._first_audio_at is None else time.monotonic() - self._first_audio_at
        if self._finished and heard >= self._audio_seconds:
            return None
        how_far = f"after hearing about {heard:.0f} seconds of it" if heard >= 1 else "as it began"
        in_full = (
            f" In full it takes about {self._audio_seconds:.0f} seconds to say."
            if self._finished
            else ""
        )
        return {
            "role": "developer",
            "content": f"The user interrupted your last reply {how_far}.{in_full} "
            "They did not hear the rest.",
        }

    async def _disconnect(self) -> None:
        self._connecting.cancel()
        (connected,) = await asyncio.gather(self._connecting, return_exceptions=True)
        if isinstance(connected, CartesiaTTSSession):
            with contextlib.suppress(Exception):
                await connected.close()


class VoiceConversation:
    """One live spoken conversation. Feed it mic audio; read events and speech audio."""

    def __init__(
        self,
        *,
        stt: CartesiaSTTSession,
        tts: CartesiaTTSAdapterClass,
        llm: OpenAIAdapterClass,
        search: ParallelAdapterClass,
    ) -> None:
        self._stt = stt
        self._tts = tts
        self._llm = llm
        self._search = search
        # Everything the model sees, oldest first. Only complete exchanges are added:
        # a tool call without its result would make the next request invalid.
        self._history: list[ResponseInputItemParam] = []
        self._out: asyncio.Queue[VoiceEvent | bytes | None] = asyncio.Queue()
        # The instructions and the tool list are the start of every request, so they are
        # fixed for the whole conversation: a prompt cache only helps while that start
        # stays the same.
        self._instructions = _INSTRUCTIONS.format(today=datetime.now(UTC).strftime("%A, %d %B %Y"))
        self._tools = [_WEB_SEARCH_TOOL] if search.configured else []
        if search.configured:
            self._instructions += _SEARCH_INSTRUCTIONS
        self._reply: asyncio.Task[None] | None = None
        # The last reply that finished here. The client may still be playing it.
        self._spoken: _Speech | None = None
        self._listening = asyncio.create_task(self._listen())

    async def send_audio(self, pcm: bytes) -> None:
        """Feed mic audio: raw 16-bit mono PCM at 16 kHz."""
        await self._stt.send_audio(pcm)

    async def outputs(self) -> AsyncIterator[VoiceEvent | bytes]:
        """Yield events and speech audio (raw 16-bit mono PCM at 24 kHz) in order."""
        while (item := await self._out.get()) is not None:
            yield item

    async def close(self) -> None:
        """End the conversation: stop any reply and release the speech connections."""
        tasks = [task for task in (self._listening, self._reply) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._stt.close()

    def _emit(self, type_: str, text: str = "") -> None:
        self._out.put_nowait(VoiceEvent.model_validate({"type": type_, "text": text}))

    async def _listen(self) -> None:
        """Follow the user's turns for the life of the conversation."""
        try:
            async for event in self._stt.events():
                if event.type == "turn.start":
                    await self._interrupt()
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
        """The user is speaking: stop the reply in progress, on the server and the client."""
        reply, self._reply = self._reply, None
        spoken, self._spoken = self._spoken, None
        if reply is not None and not reply.done():
            # Still being produced: cancelling it records what was said so far.
            reply.cancel()
            await asyncio.gather(reply, return_exceptions=True)
        elif spoken is not None and (note := spoken.interrupted_note()) is not None:
            # Finished here, but the client was still playing it.
            self._history.append(note)
        # Sent every time: the server cannot see the client's playback queue.
        self._emit("interrupt")

    async def _start_reply(self, transcript: str) -> None:
        if not transcript:
            return
        # Recorded before the reply starts, so it survives if the reply is interrupted.
        self._history.append({"role": "user", "content": transcript})
        self._emit("user.final", transcript)
        self._reply = asyncio.create_task(self._respond())

    async def _respond(self) -> None:
        """Produce one reply: model rounds (with searches in between), spoken as they stream."""
        speech = _Speech(self._tts, self._out)
        # This reply's own items. They join the history only if the reply completes.
        items: list[ResponseInputItemParam] = []
        try:
            for round_number in range(_MAX_SEARCHES + 1):
                # In the last round the model may not search again: it has to answer.
                must_answer = round_number == _MAX_SEARCHES
                searching = await self._model_round(items, speech, must_answer=must_answer)
                if not searching:
                    break
            await speech.finish()
        except asyncio.CancelledError:
            await speech.stop()
            note = speech.interrupted_note()
            if (spoken := speech.text.strip()) and note is not None:
                self._history.append({"role": "assistant", "content": spoken})
                self._history.append(note)
            raise
        except Exception as exc:
            await speech.stop()
            self._emit("error", f"reply failed: {exc}")
        else:
            self._history.extend(items)
            self._spoken = speech
            self._emit("assistant.done", speech.text.strip())

    async def _model_round(
        self,
        items: list[ResponseInputItemParam],
        speech: _Speech,
        *,
        must_answer: bool,
    ) -> bool:
        """Run one model call, speaking its text. Return True if it searched (so: call again)."""
        stream = await self._llm.stream_response(
            model="6 Luna",
            # No thinking before it speaks: in a conversation the wait is worse than the gain.
            reasoning={"effort": "none"},
            input=[*self._history, *items],
            instructions=self._instructions,
            tools=self._tools,
            # Forbidding the tool this way leaves the tool list, and so the cache, intact.
            tool_choice="none" if must_answer else "auto",
            # Stateless: nothing is stored at OpenAI, so reasoning has to be sent back.
            include=["reasoning.encrypted_content"],
            store=False,
        )
        output = None
        async for event in stream:
            if event.type == "response.output_text.delta":
                await speech.say(event.delta)
                self._emit("assistant.delta", event.delta)
            elif event.type == "response.output_item.added":
                if event.item.type == "function_call" and not speech.text:
                    await speech.say(_SEARCHING_LINE)
                    self._emit("assistant.delta", _SEARCHING_LINE)
            elif event.type == "response.completed":
                output = event.response.output
            elif event.type == "response.failed":
                error = event.response.error
                raise RuntimeError(error.message if error else "the model call failed")
            elif event.type == "response.incomplete":
                raise RuntimeError("the model stopped before finishing")
            elif event.type == "error":
                raise RuntimeError(event.message)
        if output is None:
            raise RuntimeError("the model stream ended early")

        items.extend(cast("ResponseInputItemParam", item.to_dict()) for item in output)
        calls = [item for item in output if item.type == "function_call"]
        if not calls:
            return False
        # Whatever was said before searching ("Let me check") is complete as it stands.
        await speech.end_utterance()
        for call in calls:
            result = await self._run_tool(call.name, call.arguments)
            items.append(
                {"type": "function_call_output", "call_id": call.call_id, "output": result}
            )
        return True

    async def _run_tool(self, name: str, arguments: str) -> str:
        """Run one tool call. A failure goes back to the model as text, so it can say so."""
        if name != "web_search":
            return f"Unknown tool: {name}"
        try:
            query = str(json.loads(arguments)["query"])
            self._emit("tool.call", query)
            # The fastest mode: the user is waiting in silence while this runs.
            result = await self._search.search(objective=query, queries=[query], mode="turbo")
        except Exception as exc:
            return f"The search failed: {exc}"
        return _format_search(result)


class VoiceEntityClass:
    def __init__(
        self,
        *,
        stt: CartesiaSTTAdapterClass,
        tts: CartesiaTTSAdapterClass,
        llm: OpenAIAdapterClass,
        search: ParallelAdapterClass,
    ) -> None:
        self._stt = stt
        self._tts = tts
        self._llm = llm
        self._search = search

    async def open_conversation(self) -> VoiceConversation:
        """Start listening. The caller owns the conversation and must `close()` it."""
        stt = await self._stt.connect_auto_turns()
        return VoiceConversation(stt=stt, tts=self._tts, llm=self._llm, search=self._search)


_WEB_SEARCH_TOOL = OpenAIAdapterClass.Tools.function(
    name="web_search",
    description="Search the web for current or factual information.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look up, as a full question."}
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)


def _format_search(result: SearchResult) -> str:
    """Render search results as plain text for the model."""
    if not result.results:
        return "No results."
    pages = [
        "\n".join(
            [
                f"{page.title or page.url} ({page.url})",
                *([f"Published: {page.publish_date}"] if page.publish_date else []),
                *page.excerpts,
            ]
        )
        for page in result.results
    ]
    return "\n\n".join(pages)

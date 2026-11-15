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

The history is what the model is sent on every call, and it holds what the user heard,
not what was generated. Speech is generated much faster than it plays, so a reply only
joins the history once it has been heard in full or cut off. A reply that is cut off is
stored as the words that were heard, ending in "…". The rest is never sent back.
"""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from app.adapters.cartesia_stt_adp import CartesiaSTTAdapterClass, CartesiaSTTSession
from app.adapters.cartesia_tts_adp import CartesiaTTSAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass, ResponseInputItemParam
from app.adapters.parallel_adp import ParallelAdapterClass, SearchResult
from app.entities.voice_speech import Clock, Playback, Speech, Utterance

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
- An earlier reply of yours that ends in "…" was cut off there by the user. They heard \
nothing after that point, so do not assume they know the rest. Never end a reply with \
"…" yourself.
"""

_SEARCH_INSTRUCTIONS = """\
- For anything recent, or that you are not sure of, call web_search straight away. The \
user hears "Let me check" while it runs, so do not announce the search yourself. Then \
answer from the results and name the source.
"""

# Spoken by us the moment the model starts a search without having said anything, so the
# user is not left in silence. Models do this themselves only some of the time.
_SEARCHING_LINE = "Let me check."

# Ends the stored text of a reply that the user cut off.
_CUT_MARK = "…"


class VoiceEvent(BaseModel):
    """One JSON message for the client. Speech audio travels separately, as binary.

    - `user.partial`    the user's words so far, while they are still speaking
    - `user.final`      the user's finished sentence; a reply follows
    - `assistant.delta` the next piece of the reply's text
    - `assistant.done`  the reply's audio has all been sent (full text in `text`)
    - `tool.call`       the assistant is searching the web (query in `text`)
    - `interrupt`       the user started speaking: stop playback and drop queued audio.
                        If that cut a reply short, `text` is the part that was heard
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


@dataclass(slots=True)
class _ToolCall:
    call_id: str
    name: str
    arguments: str
    output: str | None = None  # None while the tool is still running

    def items(self) -> list[ResponseInputItemParam]:
        return [
            {
                "type": "function_call",
                "call_id": self.call_id,
                "name": self.name,
                "arguments": self.arguments,
            },
            {
                "type": "function_call_output",
                "call_id": self.call_id,
                "output": self.output or "",
            },
        ]


class _Reply:
    """One reply while it is produced and played: what was said and what was looked up."""

    def __init__(self, speech: Speech) -> None:
        self.speech = speech
        self._parts: list[Utterance | _ToolCall] = []

    async def say(self, text: str) -> None:
        utterance = await self.speech.say(text)
        if not self._parts or self._parts[-1] is not utterance:
            self._parts.append(utterance)

    def add_call(self, call: _ToolCall) -> None:
        self._parts.append(call)

    def items(self) -> list[ResponseInputItemParam]:
        """Everything produced so far, for this reply's own next model call."""
        items: list[ResponseInputItemParam] = []
        for part in self._parts:
            if isinstance(part, _ToolCall):
                items.extend(part.items())
            elif text := part.text.strip():
                items.append({"role": "assistant", "content": text})
        return items

    def heard(self, at: float) -> tuple[list[ResponseInputItemParam], str | None]:
        """The reply as the user had heard it by time `at`, for the history.

        Also returns the heard text if the reply was cut short, or None if it was heard
        in full. Finished tool calls are kept either way: they happened, and the history
        has to stay a valid chain of calls and results.
        """
        items: list[ResponseInputItemParam] = []
        spoken: list[str] = []
        cut = False
        for part in self._parts:
            if isinstance(part, _ToolCall):
                if part.output is not None:
                    items.extend(part.items())
            elif not cut:
                text, whole = part.heard(at)
                if not whole:
                    cut = True
                    text = f"{text}{_CUT_MARK}" if text else ""
                if text:
                    spoken.append(text)
                    items.append({"role": "assistant", "content": text})
        return items, " ".join(spoken) if cut else None


class VoiceConversation:
    """One live spoken conversation. Feed it mic audio; read events and speech audio."""

    def __init__(
        self,
        *,
        stt: CartesiaSTTSession,
        tts: CartesiaTTSAdapterClass,
        llm: OpenAIAdapterClass,
        search: ParallelAdapterClass,
        clock: Clock = time.monotonic,
    ) -> None:
        self._stt = stt
        self._tts = tts
        self._llm = llm
        self._search = search
        self._clock = clock
        self._playback = Playback(clock)
        # Everything the model sees, oldest first. It only ever grows at the end, and
        # each entry is final when it is added.
        self._history: list[ResponseInputItemParam] = []
        self._out: asyncio.Queue[VoiceEvent | bytes | None] = asyncio.Queue()
        # The instructions and the tool list are the start of every request, so they are
        # fixed for the whole conversation: a prompt cache only helps while that start
        # stays the same.
        self._instructions = _INSTRUCTIONS.format(today=datetime.now(UTC).strftime("%A, %d %B %Y"))
        self._tools = [_WEB_SEARCH_TOOL] if search.configured else []
        if search.configured:
            self._instructions += _SEARCH_INSTRUCTIONS
        # The task producing a reply, while one is being produced.
        self._producing: asyncio.Task[None] | None = None
        # The latest reply, until it joins the history. That happens when the user next
        # speaks: by then it has either been heard in full or is being cut off.
        self._reply: _Reply | None = None
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
        tasks = [task for task in (self._listening, self._producing) if task is not None]
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
        """The user is speaking: stop the reply, and record how much of it they heard."""
        producing, self._producing = self._producing, None
        heard = self._settle()
        if producing is not None:
            producing.cancel()
        self._playback.reset()
        # Sent every time: the server cannot see the client's playback queue.
        self._emit("interrupt", heard or "")
        if producing is not None:
            await asyncio.gather(producing, return_exceptions=True)

    def _settle(self) -> str | None:
        """Move the latest reply into the history as the user heard it.

        Return the heard text if that cut the reply short, else None.
        """
        reply, self._reply = self._reply, None
        if reply is None:
            return None
        reply.speech.silence()
        items, heard = reply.heard(self._clock())
        self._history.extend(items)
        return heard

    async def _start_reply(self, transcript: str) -> None:
        if not transcript:
            return
        if self._producing is not None or self._reply is not None:
            # A turn ended without the start that normally comes first.
            await self._interrupt()
        self._history.append({"role": "user", "content": transcript})
        self._emit("user.final", transcript)
        self._producing = asyncio.create_task(self._respond())

    async def _respond(self) -> None:
        """Produce one reply: model rounds (with searches in between), spoken as they stream."""
        reply = _Reply(Speech(self._tts, self._out.put_nowait, self._playback))
        self._reply = reply
        try:
            for round_number in range(_MAX_SEARCHES + 1):
                # In the last round the model may not search again: it has to answer.
                must_answer = round_number == _MAX_SEARCHES
                if not await self._model_round(reply, must_answer=must_answer):
                    break
            await reply.speech.finish()
        except asyncio.CancelledError:
            await reply.speech.stop()
            raise
        except Exception as exc:
            await reply.speech.stop()
            self._emit("error", f"reply failed: {exc}")
        else:
            self._emit("assistant.done", reply.speech.text.strip())

    async def _model_round(self, reply: _Reply, *, must_answer: bool) -> bool:
        """Run one model call, speaking its text. Return True if it searched (so: call again)."""
        stream = await self._llm.stream_response(
            model="6 Luna",
            # No thinking before it speaks: in a conversation the wait is worse than the gain.
            reasoning={"effort": "none"},
            input=[*self._history, *reply.items()],
            instructions=self._instructions,
            tools=self._tools,
            # Forbidding the tool this way leaves the tool list, and so the cache, intact.
            tool_choice="none" if must_answer else "auto",
            store=False,
        )
        output = None
        async for event in stream:
            if event.type == "response.output_text.delta":
                await reply.say(event.delta)
                self._emit("assistant.delta", event.delta)
            elif event.type == "response.output_item.added":
                if event.item.type == "function_call" and not reply.speech.text:
                    await reply.say(_SEARCHING_LINE)
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

        calls = [
            _ToolCall(item.call_id, item.name, item.arguments)
            for item in output
            if item.type == "function_call"
        ]
        if not calls:
            return False
        # Whatever was said before searching ("Let me check") is complete as it stands.
        await reply.speech.end_utterance()
        for call in calls:
            reply.add_call(call)
            call.output = await self._run_tool(call.name, call.arguments)
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

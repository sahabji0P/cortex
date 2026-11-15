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

A user turn stays open until some of the reply to it has been heard. Words that arrive
before that join the same turn on a new line: speech recognition often splits one
sentence in two, and the second half is not a new request.

A search is not stopped by an interruption. If the user speaks while one is running, its
call gets a placeholder result so the history stays valid, and the search carries on.
When it finishes, the real result is added at the next quiet moment as a call written
by us (a stub that names the original call) followed by that result, and the model
replies to it without waiting for the user.
"""

import asyncio
import contextlib
import json
import time
import unicodedata
import uuid
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
- If the user interrupts while a search is running, the search carries on in the \
background. Do not search for the same thing again: say it is still coming, and answer \
whatever else they asked. The result reaches you by itself, as a web_search call with \
"deferred_result_for" in its arguments. You did not make that call: it only delivers the \
late result. Then tell the user what it found, unless they no longer want it.
"""

# Stands in for the result of a call that was still running when the user spoke.
_PLACEHOLDER = (
    "Still running. The user interrupted before this finished, so it carries on in the "
    "background. Do not make this call again. Its result will reach you by itself, as a "
    'separate call with "deferred_result_for": "{call_id}". If the user no longer wants '
    "it, stop it with cancel_tool_call and that call_id."
)

# Added to the history when a turn with no words in it cut a reply off.
_NOISE_NOTE = (
    "That interruption was background noise, not the user speaking. Continue your reply "
    'from the word after the "…". Do not repeat anything that comes before it.'
)

# A trailing fragment longer than this many words is never treated as noise.
_STRAY_MAX_WORDS = 2
# Latin, Devanagari and CJK sentence endings (the last two are the full-width ? and !).
_SENTENCE_ENDS = (".", "?", "!", "।", "。", "\uff1f", "\uff01")

# How often the wait for a quiet moment looks again. Playback can be cut short or added
# to while it waits, so it does not sleep through to a time worked out in advance.
_QUIET_CHECK_SECONDS = 0.1

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
    running: asyncio.Task[str] | None = None

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

    def calls(self) -> list[_ToolCall]:
        return [part for part in self._parts if isinstance(part, _ToolCall)]

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
        in full. Tool calls are kept either way: they happened, and the history has to
        stay a valid chain of calls and results.
        """
        items: list[ResponseInputItemParam] = []
        spoken: list[str] = []
        cut = False
        for part in self._parts:
            if isinstance(part, _ToolCall):
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
        # The user's turn that has not been answered yet, as the pieces it arrived in.
        # It joins the history when some of the reply to it has been heard.
        self._turn: list[str] = []
        # Whether the turn the user has just opened cut a reply short.
        self._cut_a_reply = False
        self._out: asyncio.Queue[VoiceEvent | bytes | None] = asyncio.Queue()
        # The instructions and the tool list are the start of every request, so they are
        # fixed for the whole conversation: a prompt cache only helps while that start
        # stays the same.
        self._instructions = _INSTRUCTIONS.format(today=datetime.now(UTC).strftime("%A, %d %B %Y"))
        self._tools = [_WEB_SEARCH_TOOL, _CANCEL_TOOL] if search.configured else []
        if search.configured:
            self._instructions += _SEARCH_INSTRUCTIONS
        # The task producing a reply, while one is being produced.
        self._producing: asyncio.Task[None] | None = None
        # The latest reply, until it joins the history. That happens when the user next
        # speaks, or when a late tool result is about to be answered: by then it has
        # either been heard in full or is being cut off.
        self._reply: _Reply | None = None
        self._user_speaking = False
        # Calls still running after their reply was cut off, by call id.
        self._outliving: dict[str, _ToolCall] = {}
        # Results of such calls, waiting for a quiet moment to be given to the model.
        self._late: list[tuple[_ToolCall, str]] = []
        self._announcing: asyncio.Task[None] | None = None
        # The calls in the history that we wrote to carry a late result: stub call id to
        # the id of the call the model really made.
        self._stubs: dict[str, str] = {}
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
        tasks = [
            task
            for task in (self._listening, self._producing, self._announcing)
            if task is not None
        ]
        searches = [call.running for call in self._outliving.values() if call.running is not None]
        for task in (*tasks, *searches):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*searches, return_exceptions=True)
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
                    await self._interrupt()
                elif event.type == "turn.update":
                    self._emit("user.partial", event.transcript)
                elif event.type == "turn.end":
                    self._user_speaking = False
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
        heard = self._settle()
        self._cut_a_reply = heard is not None
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
        for call in reply.calls():
            if call.output is None:
                self._outlive(call)
        items, heard = reply.heard(self._clock())
        if items and self._turn:
            # Some of the reply got through, so the turn it answers is closed.
            self._history.append({"role": "user", "content": "\n".join(self._turn)})
            self._turn = []
        self._history.extend(items)
        return heard

    def _outlive(self, call: _ToolCall) -> None:
        """Let a call that is still running carry on without the reply that made it."""
        running = call.running
        if running is None or running.done():
            # It finished in the instant before the reply was cut: nothing is late.
            call.output = running.result() if running and not running.cancelled() else ""
            return
        call.output = _PLACEHOLDER.format(call_id=call.call_id)
        self._outliving[call.call_id] = call
        running.add_done_callback(lambda finished: self._finished_late(call, finished))

    def _finished_late(self, call: _ToolCall, finished: asyncio.Task[str]) -> None:
        self._outliving.pop(call.call_id, None)
        if finished.cancelled():
            return
        self._late.append((call, finished.result()))
        self._announce_when_quiet()

    def _announce_when_quiet(self) -> None:
        """Make sure something is waiting to give the model its late results."""
        waiting = self._announcing is not None and not self._announcing.done()
        if self._late and not self._user_speaking and not waiting:
            self._announcing = asyncio.create_task(self._announce_late_results())

    async def _announce_late_results(self) -> None:
        """Wait until nothing is being said, then let the model answer its late results.

        Nothing is being said once the reply in progress has been produced and played to
        the end. Only then is that reply final, so only then may another request be made.
        If the user speaks first, this is cancelled and their turn carries the results.
        """
        while True:
            if self._producing is not None and not self._producing.done():
                await asyncio.wait({self._producing})
            elif self._playback.ends_at > self._clock():
                await asyncio.sleep(_QUIET_CHECK_SECONDS)
            else:
                break
        self._settle()
        self._add_late_results()
        self._producing = asyncio.create_task(self._respond())

    def _add_late_results(self) -> None:
        """Add each late result to the history as a stub call followed by the result.

        A result has to follow its own call directly, and the real call already has its
        placeholder. So we write a call ourselves that names the real one.
        """
        for call, output in self._late:
            stub = _ToolCall(
                call_id=f"late_{uuid.uuid4().hex[:16]}",
                name=call.name,
                arguments=json.dumps({"deferred_result_for": call.call_id}),
                output=output,
            )
            self._stubs[stub.call_id] = call.call_id
            self._history.extend(stub.items())
        self._late.clear()

    async def _start_reply(self, transcript: str) -> None:
        """A turn ended: answer it, or put back what it interrupted if nothing was said."""
        if self._producing is not None or self._reply is not None:
            # A turn ended without the start that normally comes first.
            await self._interrupt()
        said = bool(transcript) and not _is_stray(transcript, "\n".join(self._turn))
        if not said and not self._turn and not self._cut_a_reply:
            # Nothing was said and nothing was stopped: only late results may be waiting.
            self._announce_when_quiet()
            return
        # Results that came in while the user was speaking go in ahead of what follows.
        self._add_late_results()
        if said:
            self._turn.append(transcript)
            self._emit("user.final", transcript)
        elif not self._turn:
            # Nothing was said, yet it stopped a reply that was being heard. (If the
            # user's own turn is still unanswered, it is simply answered again.)
            self._history.append({"role": "developer", "content": _NOISE_NOTE})
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
            input=[*self._history, *self._open_turn(), *reply.items()],
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
                searching = event.item.type == "function_call" and event.item.name == "web_search"
                if searching and not reply.speech.text:
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
        # Each call runs as its own task, so that interrupting the reply does not stop it.
        for call in calls:
            reply.add_call(call)
            call.running = asyncio.create_task(self._run_tool(call.name, call.arguments))
        for call in calls:
            if call.running is not None:
                call.output = await asyncio.shield(call.running)
        return True

    def _open_turn(self) -> list[ResponseInputItemParam]:
        """The user's unanswered turn as one message, for a model call."""
        return [{"role": "user", "content": "\n".join(self._turn)}] if self._turn else []

    async def _run_tool(self, name: str, arguments: str) -> str:
        """Run one tool call. A failure goes back to the model as text, so it can say so."""
        try:
            if name == "web_search":
                query = str(json.loads(arguments)["query"])
                self._emit("tool.call", query)
                # The fastest mode: the user is waiting in silence while this runs.
                found = await self._search.search(objective=query, queries=[query], mode="turbo")
                return _format_search(found)
            if name == "cancel_tool_call":
                return self._cancel_call(str(json.loads(arguments)["call_id"]))
        except Exception as exc:
            return f"{name} failed: {exc}"
        return f"Unknown tool: {name}"

    def _cancel_call(self, call_id: str) -> str:
        """Stop a call that outlived its reply. Its result will never arrive."""
        # The model may name the call by a stub that already delivered its result.
        call = self._outliving.get(self._stubs.get(call_id, call_id))
        if call is None or call.running is None:
            return "Nothing is running under that call_id."
        call.running.cancel()
        return "Cancelled."


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


_CANCEL_TOOL = OpenAIAdapterClass.Tools.function(
    name="cancel_tool_call",
    description="Stop a tool call that is still running after the user interrupted it.",
    parameters={
        "type": "object",
        "properties": {
            "call_id": {"type": "string", "description": "The call_id of the running call."}
        },
        "required": ["call_id"],
        "additionalProperties": False,
    },
)


def _is_stray(fragment: str, turn_so_far: str) -> bool:
    """Whether a fragment that trails an unanswered turn is more likely noise than speech.

    Speech recognition is not tied to one language. It can turn a cough or a door into a
    word or two in some other script, which then arrives just after a finished sentence.
    This is kept narrow, because people do mix languages: only a fragment of a word or
    two, in a script the sentence before it did not use, counts.
    """
    if not turn_so_far.rstrip().endswith(_SENTENCE_ENDS):
        return False
    if len(fragment.split()) > _STRAY_MAX_WORDS:
        return False
    scripts = _scripts(fragment)
    return bool(scripts) and scripts.isdisjoint(_scripts(turn_so_far))


def _scripts(text: str) -> set[str]:
    """The writing systems used in a text, by the first word of each letter's Unicode name."""
    return {unicodedata.name(letter, "?").split()[0] for letter in text if letter.isalpha()}


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

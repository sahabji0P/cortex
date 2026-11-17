"""Check the AI and pipeline part of the voice cascade, with no audio involved.

Run from the repository root (needs PARALLEL_API_KEY and the model's own key in .env):

    uv run python -m scripts.voice_pipeline_check              # the default model, 6 Luna
    uv run python -m scripts.voice_pipeline_check "Haiku 5.5"  # or any other model's name

The model and the web search are real. Speech is faked with the test suite's stand-ins:
every word takes exactly one second to say and the clock is moved by hand, so each
scenario decides how much of a reply was "heard". Every request the model is sent is
recorded and checked for the rule that earlier input is never changed.

Each line of output is PASS or CHECK. CHECK means look at it: a model's wording varies,
and some checks are about what it said. A scenario that does not finish is a CHECK too,
and the rest still run.
"""

import asyncio
import contextlib
import json
import re
import sys

from app.adapters.anthropic_adp import AnthropicAdapterClass
from app.adapters.cartesia_tts_adp import CartesiaTTSSession, CartesiaTTSWord
from app.adapters.openai_adp import OpenAIAdapterClass
from app.adapters.parallel_adp import ParallelAdapterClass
from app.entities import voice_ent
from app.entities.gem_ent import DEFAULT_GEM_INSTRUCTIONS
from app.entities.model import RoundDone, TextDelta, is_openai
from app.entities.voice_ent import VoiceConversation, VoiceEvent
from tests import test_voice_conversation as T

DELAY = T._DELAY
MODEL = sys.argv[1] if len(sys.argv) > 1 else "6 Luna"
RESULTS = []  # (scenario, verdict, detail)
# A gem as a new one is made: the style checks below are of its default instructions.
GEM = T._GEM.model_copy(update={"instructions": DEFAULT_GEM_INSTRUCTIONS})
OPEN = []  # conversations not yet closed, so that a scenario that fails can be cleaned up


class Recorder:
    """Every request the model is sent and what it answered, whichever provider it is.

    It sits between the conversation and its model, so what it writes down is the
    history as the conversation holds it, before any provider's translation.
    """

    def __init__(self):
        self.requests = []
        self._model = None

    def wrap(self, model):
        self._model = model
        return self

    async def round(self, items, *, instructions, think):
        sent = [dict(i) for i in items]
        if self.requests:
            T._assert_only_grew(self.requests[-1]["input"], sent)
        record = {
            "input": sent,
            "text": "",
            "calls": [],
            "usage": None,  # (input tokens, of which read from the cache)
        }
        self.requests.append(record)
        events = self._model.round(items, instructions=instructions, think=think)
        async with contextlib.aclosing(events):
            async for event in events:
                if isinstance(event, TextDelta):
                    record["text"] += event.text
                elif isinstance(event, RoundDone):
                    record["calls"] = [(call.name, call.arguments) for call in event.calls]
                yield event

    def used(self, input_tokens, cached_tokens):
        """The provider's count for the request in progress."""
        if self.requests:
            self.requests[-1]["usage"] = (input_tokens, cached_tokens)


class OpenAITap:
    """The OpenAI adapter, reporting each request's token counts to the recorder."""

    def __init__(self, real, recorder):
        self._real, self._recorder = real, recorder

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def stream_response(self, **kw):
        stream = await self._real.stream_response(**kw)

        async def tap():
            async with contextlib.aclosing(stream):
                async for event in stream:
                    if event.type == "response.completed" and event.response.usage:
                        usage = event.response.usage
                        self._recorder.used(
                            usage.input_tokens, usage.input_tokens_details.cached_tokens
                        )
                    yield event

        return tap()


class AnthropicTap:
    """The Anthropic adapter, reporting each request's token counts to the recorder."""

    def __init__(self, real, recorder):
        self._real, self._recorder = real, recorder

    def __getattr__(self, name):
        return getattr(self._real, name)

    def stream_message(self, **kw):
        return _TappedStream(self._real.stream_message(**kw), self._recorder)


class _TappedStream:
    def __init__(self, manager, recorder):
        self._manager, self._recorder, self._stream = manager, recorder, None

    async def __aenter__(self):
        self._stream = await self._manager.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._manager.__aexit__(*exc)

    def __aiter__(self):
        return self._stream.__aiter__()

    async def get_final_message(self):
        message = await self._stream.get_final_message()
        usage = message.usage
        read = usage.cache_read_input_tokens or 0
        written = usage.cache_creation_input_tokens or 0
        self._recorder.used(usage.input_tokens + read + written, read)
        return message


class Search:
    """The real search. Its first call can be held back, and all of them made to fail."""

    configured = True

    def __init__(self, real):
        self._real, self.gate, self.fail, self.count = real, None, False, 0
        self._held = False

    async def search(self, **kw):
        self.count += 1
        if self.fail:
            raise RuntimeError("search service is down")
        if self.gate is not None and not self._held:
            # Only one search is held back. If the model searches again while it waits,
            # that search runs, as it would for a real user.
            self._held = True
            await self.gate.wait()
        return await self._real.search(**kw)


class Utter:
    """Fake speech for one utterance: one second per word, like the test suite's, but fed by a real
    model's token stream, so a word is only "spoken" once the stream has moved past it."""

    def __init__(self):
        self._events = asyncio.Queue()
        self._buffer = ""
        self._seconds = 0
        self.cancelled = False

    def _speak(self, words):
        if not words:
            return
        self._events.put_nowait(
            [CartesiaTTSWord(w, self._seconds + n + 1) for n, w in enumerate(words)]
        )
        self._seconds += len(words)
        self._events.put_nowait(b"\x00" * (48000 * len(words)))

    async def push(self, text):
        self._buffer += text
        parts = self._buffer.split()
        if not parts:
            return
        if self._buffer[-1].isspace():
            self._speak(parts)
            self._buffer = ""
        else:
            self._speak(parts[:-1])
            self._buffer = parts[-1]

    async def finish(self):
        self._speak(self._buffer.split())
        self._buffer = ""
        self._events.put_nowait(None)

    async def cancel(self):
        self.cancelled = True

    async def events(self):
        while (event := await self._events.get()) is not None:
            yield event


class Line(CartesiaTTSSession):
    def __init__(self):
        pass

    def context(self):
        return Utter()

    async def close(self):
        pass


class Voice:
    async def connect(self):
        return Line()


class Talk:
    def __init__(self, llm, search, *, speaks_first=False):
        self.llm, self.search = Recorder(), Search(search)
        self.stt, self.tts, self.clock = T._STT(), Voice(), T._Clock()
        tap = OpenAITap if is_openai(MODEL) else AnthropicTap
        self.c = VoiceConversation(
            stt=self.stt,
            tts=self.tts,
            llm=tap(llm, self.llm),
            search=self.search,
            gem=GEM,
            model=MODEL,
            speaks_first=False,
            clock=self.clock,
        )
        self.c._model = self.llm.wrap(self.c._model)
        if speaks_first:
            # Started here, not by the conversation, so that the greeting is recorded too.
            self.c._history.append({"role": "developer", "content": voice_ent._GREETING_NOTE})
            self.c._producing = asyncio.create_task(self.c._respond())
        self.events = []
        OPEN.append(self)

    async def until(self, kind, seconds=60):
        async with asyncio.timeout(seconds):
            async for out in self.c.outputs():
                if isinstance(out, VoiceEvent):
                    self.events.append((out.type, out.text))
                    if out.type == kind:
                        return out.text
        raise AssertionError(f"ended before {kind}")

    async def ask(self, text, heard=True):
        """Say something, wait for the full reply, and (by default) let it be heard to the end."""
        self.stt.say(text)
        reply = await self.until("assistant.done")
        if heard:
            self.clock.advance(600)
        return reply

    def errors(self):
        return [t for k, t in self.events if k == "error"]

    def history(self):
        return [*self.c._history, *self.c._open_turn()]

    async def close(self):
        if self in OPEN:
            OPEN.remove(self)
        if self.search.gate is not None:
            self.search.gate.set()
        await self.c.close()


def shape(items):
    return [i.get("role") or i["type"] for i in items]


def note(name, ok, detail):
    RESULTS.append((name, "PASS" if ok else "CHECK", detail))
    print(f"{'PASS ' if ok else 'CHECK'} {name}: {detail}", flush=True)


STORY = "Tell me a long story about a dragon. At least ten sentences."


async def scenario_1(llm, par, replies):
    """context across turns"""
    t = Talk(llm, par)
    a = await t.ask("What is the capital of France?")
    b = await t.ask("And of Japan?")
    replies += [a, b]
    note("context across turns", "Paris" in a and "Tokyo" in b and not t.errors(), f"{a!r} / {b!r}")
    await t.close()


async def scenario_2(llm, par, replies):
    """a web search, answered with its source"""
    t = Talk(llm, par)
    a = await t.ask("Who won the most recent Formula One race?")
    replies.append(a)
    calls = [c for r in t.llm.requests for c in r["calls"]]
    note(
        "web search and answer",
        bool(calls) and len(a) > 20 and not t.errors(),
        f"{len(calls)} search call(s); {a[:120]!r}",
    )
    await t.close()


async def scenario_3(llm, par, replies):
    """a cut reply: the model only knows the words that were heard"""
    t = Talk(llm, par)
    t.stt.say(STORY)
    await t.until("assistant.delta")
    full = await t.until("assistant.done")
    t.clock.advance(DELAY + 6.5)
    t.stt.turn_start()
    heard = await t.until("interrupt")
    await asyncio.sleep(1.8)  # a spoken sentence takes this long
    t.stt.turn_end("What were the exact last words I heard from you? Quote them.")
    a = await t.until("assistant.done")
    replies.append(a)
    unheard = full[len(heard) - 1 :]
    leaked = [
        w
        for w in re.findall(r"[A-Za-z]{6,}", unheard)[:40]
        # A word of the user's own request is not something only the unheard part held.
        if w.lower() in a.lower() and w.lower() not in (heard + STORY).lower()
    ]
    note(
        "cut reply: only heard words are known",
        len(heard.split()) == 6 and heard.endswith("…") and not leaked and not t.errors(),
        f"heard {heard!r}; model said {a[:130]!r}; unheard words repeated: {leaked or 'none'}",
    )
    hist = t.history()
    stored = ([i["content"] for i in hist if i.get("role") == "assistant"] or [""])[0]
    note(
        "cut reply: history holds the prefix, not the story",
        stored == heard,
        f"stored {stored!r} ({len(full.split())} words were generated)",
    )
    # 4. nothing of ours follows the cut (the line that opens the call is first), and the
    # model still knows the rest went unheard
    notes = [i["content"] for i in hist[1:] if i.get("role") == "developer"]
    t.clock.advance(600)
    a = await t.ask("Did I hear your whole story, yes or no?")
    replies.append(a)
    note(
        "cut reply: model knows the rest was not heard",
        not notes and re.search(r"\bno\b|not|didn.t|interrupt|cut", a.lower()) is not None,
        f"notes added {len(notes)}; model said {a[:110]!r}",
    )
    await t.close()


async def scenario_5(llm, par, replies):
    """your case: an unrelated question over a running search, then the result unprompted"""
    t = Talk(llm, par)
    t.search.gate = asyncio.Event()
    t.stt.say("Who won the most recent Formula One race?")
    await t.until("tool.call")
    t.stt.say("By the way, what is two plus two?")
    a = await t.until("assistant.done")
    replies.append(a)
    t.search.gate.set()
    t.clock.advance(600)
    before = len(t.events)
    b = await t.until("assistant.done")
    replies.append(b)
    no_user_turn = "user.final" not in [k for k, _ in t.events[before:]]
    h = t.history()
    stub = [i for i in h if str(i.get("call_id", "")).startswith("late_") and "arguments" in i]
    note(
        "late result: question answered first",
        re.search(r"four|4", a.lower()) is not None,
        f"{a!r}",
    )
    note(
        "late result: brought up unprompted, in the spec's order",
        no_user_turn and len(b) > 20 and len(stub) == 1 and t.search.count == 1 and not t.errors(),
        f"{b[:120]!r}; history {shape(h)}; searches started {t.search.count}",
    )
    t.clock.advance(600)
    c = await t.ask("Thanks.")
    replies.append(c)
    note(
        "late result: not brought up again afterwards",
        len(t.llm.requests) == 4 and not t.errors(),
        f"{len(t.llm.requests)} model calls in all; last reply {c!r}",
    )
    await t.close()


async def scenario_6(llm, par, replies):
    """the model cancels a search the user no longer wants"""
    t = Talk(llm, par)
    t.search.gate = asyncio.Event()
    t.stt.say("Who won the most recent Formula One race?")
    await t.until("tool.call")
    t.stt.say("Never mind, cancel that search. I don't need it.")
    a = await t.until("assistant.done")
    replies.append(a)
    cancels = [c for r in t.llm.requests for c in r["calls"] if c[0] == "cancel_tool_call"]
    t.search.gate.set()
    t.clock.advance(600)
    await asyncio.sleep(1.5)
    note(
        "cancel: model stops the unwanted search",
        bool(cancels) and not t.c._late.pending and not t.errors(),
        f"cancel calls {cancels}; reply {a!r}; model calls after that: {len(t.llm.requests)}",
    )
    await t.close()


async def scenario_7(llm, par, replies):
    """a wordless turn: the reply is picked up where it stopped"""
    t = Talk(llm, par)
    t.stt.say(STORY)
    await t.until("assistant.delta")
    full = await t.until("assistant.done")
    t.clock.advance(DELAY + 6.5)
    t.stt.turn_start()
    heard = await t.until("interrupt")
    t.stt.turn_end("")
    resumed = await t.until("assistant.done")
    replies.append(resumed)
    first_words = " ".join(full.split()[:4]).lower()
    note(
        "noise: reply resumed, not restarted",
        not resumed.lower().startswith(first_words) and len(resumed) > 30 and not t.errors(),
        f"cut at {heard[-40:]!r}; resumed with {resumed[:70]!r}",
    )
    await t.close()


async def scenario_8(llm, par, replies):
    """one sentence split in two by the recognizer"""
    t = Talk(llm, par)
    t.stt.say("Tell me the name of")
    await t.until("assistant.delta")
    t.stt.say("the capital of France.")
    a = await t.until("assistant.done")
    replies.append(a)
    last_user = [i for i in t.llm.requests[-1]["input"] if i.get("role") == "user"][-1]["content"]
    note(
        "merged turn: answered as one sentence",
        "Paris" in a and last_user == "Tell me the name of\nthe capital of France.",
        f"model saw {last_user!r}; said {a!r}",
    )
    await t.close()


async def scenario_9(llm, par, replies):
    """the reply about a late result is talked over before any of it is heard"""
    t = Talk(llm, par)
    t.search.gate = asyncio.Event()
    t.stt.say("Who won the most recent Formula One race?")
    await t.until("tool.call")
    t.stt.say("Hello?")
    await t.until("assistant.done")
    t.search.gate.set()
    t.clock.advance(600)
    await t.until("assistant.delta")  # the reply about the result has begun
    t.stt.say("Quick question: what is three plus three?")
    a = await t.until("assistant.done")
    replies.append(a)
    # Telling the user of the result as well is asked of the model in its instructions,
    # so the reply is shown in full for a person to judge.
    note(
        "late result: the question is answered after its reply was talked over",
        re.search(r"six|6", a.lower()) is not None and t.search.count == 1 and not t.errors(),
        f"{a!r}; searches started {t.search.count}",
    )
    await t.close()


async def scenario_10(llm, par, replies):
    """many searches: the conversation stays coherent, and the provider reuses the request's
    unchanged start"""
    t = Talk(llm, par)
    cities = ["Oslo", "Lima", "Cairo", "Rome", "Seoul", "Delhi", "Tokyo", "Paris", "Quito"]
    for city in cities:
        replies.append(await t.ask(f"Search the web: what is the population of {city}?"))
    a = await t.ask("Which city did I ask about first, and what did you tell me about it?")
    replies.append(a)
    usage = [r["usage"] for r in t.llm.requests if r["usage"]]
    note(
        "many searches: every result kept, conversation still coherent",
        "Oslo" in a and not t.errors(),
        f"{t.search.count} searches; model said {a[:110]!r}",
    )
    cached_share = sum(c for _, c in usage[-8:]) / max(1, sum(i for i, _ in usage[-8:]))
    note(
        "prompt cache: unchanged start of the request is reused",
        any(c > 0 for _, c in usage),
        f"last 8 calls: {cached_share:.0%} of input tokens served from cache; "
        f"per call (input, cached) {usage[-6:]}",
    )
    await t.close()


async def scenario_11(llm, par, replies):
    """Hindi"""
    t = Talk(llm, par)
    a = await t.ask("भारत की राजधानी क्या है?")
    replies.append(a)
    note("another language", ("दिल्ली" in a or "Delhi" in a) and not t.errors(), f"{a!r}")
    await t.close()


async def scenario_12(llm, par, replies):
    """the search service is down"""
    t = Talk(llm, par)
    t.search.fail = True
    a = await t.ask("Who won the most recent Formula One race?")
    replies.append(a)
    note(
        "search failure: model says so and carries on",
        len(a) > 10 and not t.errors(),
        f"{a[:140]!r}",
    )
    b = await t.ask("Okay. What is the capital of France?")
    replies.append(b)
    note("search failure: next turn works", "Paris" in b, f"{b!r}")
    await t.close()


async def scenario_13(llm, par, replies):
    """a reply that needs several searches makes them all"""
    t = Talk(llm, par)
    a = await t.ask(
        "Search the web separately, one search each, for the population of Oslo, Lima, "
        "Cairo, Rome and Seoul, then tell me all five."
    )
    replies.append(a)
    note(
        "many searches: reply ends with an answer",
        len(a) > 30 and not t.errors(),
        f"{t.search.count} searches over {len(t.llm.requests)} model calls; {a[:110]!r}",
    )
    await t.close()


async def scenario_14(llm, par, replies):
    """a stray word in another script never reaches the model"""
    t = Talk(llm, par)
    t.stt.say("What is the capital of France?")
    await t.until("assistant.delta")
    t.stt.say("हूँ")
    a = await t.until("assistant.done")
    replies.append(a)
    seen = json.dumps([r["input"] for r in t.llm.requests], ensure_ascii=False)
    note("noise word: dropped before the model", "हूँ" not in seen and "Paris" in a, f"{a!r}")
    await t.close()


async def scenario_15(llm, par, replies):
    """the user only checks in while a search is running"""
    t = Talk(llm, par)
    t.search.gate = asyncio.Event()
    t.stt.say("Who won the most recent Formula One race?")
    await t.until("tool.call")
    a = await t.ask("Hello?")
    replies.append(a)
    calls = [c[0] for r in t.llm.requests[1:] for c in r["calls"]]
    note(
        "running search: a check-in starts no second search and cancels nothing",
        t.search.count == 1 and not calls and not t.errors(),
        f"{a!r}; searches started {t.search.count}; calls after the first: {calls or 'none'}",
    )
    await t.close()


async def scenario_16(llm, par, replies):
    """the assistant speaks first"""
    t = Talk(llm, par, speaks_first=True)
    greeting = await t.until("assistant.done")
    t.clock.advance(600)
    said_first = "user.final" not in [k for k, _ in t.events]
    a = await t.ask("What is the capital of France?")
    b = await t.ask("Who won the most recent Formula One race?")
    replies += [greeting, a, b]
    greetings = sum(1 for r in (a, b) if re.search(r"\b(hi|hello|hey)\b", r.lower()))
    note(
        "speaks first: greets, then carries on as usual",
        said_first
        and 0 < len(greeting.split()) <= 25
        and "Paris" in a
        and t.search.count >= 1
        and len(b) > 20
        and not greetings
        and not t.errors(),
        f"greeting {greeting!r}; then {a!r}; then {b[:80]!r}; searches {t.search.count}; "
        f"greeted again in {greetings} later replies",
    )
    await t.close()


def style(replies):
    """Checks across every reply the model produced."""
    marks = [
        r
        for r in replies
        if re.search(r"[*_#`\[\]]|https?://|www\.|^\s*[-•]\s|^\s*\d+[.)]\s", r, re.M)
    ]
    note(
        "spoken style: no markdown, lists or links",
        not marks,
        f"{len(replies)} replies; with markup: {[m[:60] for m in marks] or 'none'}",
    )
    ends = [r for r in replies if r.rstrip().endswith(("…", "..."))]
    note(
        "model never ends a reply with the cut mark itself",
        not ends,
        f"{len(ends)} of {len(replies)}",
    )
    words = sorted(len(r.split()) for r in replies if r) or [0]
    note(
        "reply length for speech",
        True,
        f"median {words[len(words) // 2]} words, longest {words[-1]}",
    )


SCENARIOS = [
    scenario_1,
    scenario_2,
    scenario_3,
    scenario_5,
    scenario_6,
    scenario_7,
    scenario_8,
    scenario_9,
    scenario_10,
    scenario_11,
    scenario_12,
    scenario_13,
    scenario_14,
    scenario_15,
    scenario_16,
]


async def main():
    llm = OpenAIAdapterClass() if is_openai(MODEL) else AnthropicAdapterClass()
    par = ParallelAdapterClass()
    await asyncio.gather(llm.initialize(), par.initialize())
    replies = []  # every reply the model produced, for the style checks
    for scenario in SCENARIOS:
        try:
            await scenario(llm, par, replies)
        except Exception as exc:
            # Mostly a timeout: the model did something the scenario did not allow for.
            # It is shown as something to look at, and the rest still run.
            note(scenario.__doc__, False, f"did not finish: {exc!r}")
            for talk in list(OPEN):
                await talk.close()
    style(replies)
    print(f"\n{MODEL}: {sum(1 for _, v, _ in RESULTS if v == 'PASS')} of {len(RESULTS)} passed")
    await asyncio.gather(llm.close(), par.close())


if __name__ == "__main__":
    asyncio.run(main())

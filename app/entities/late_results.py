"""
Late results: tool calls that outlive the reply that made them, the same in a call and in
writing.

When a reply is cut off while one of its tool calls is running, the call is left to
run. It gets the placeholder below as its result, so the history stays a valid chain of
calls and results. When it finishes, its real result waits here until the conversation
tells it. It is then added after everything else, as a call we write ourselves (a stub
that names the real call) followed by the result. The model can stop a call still
running, or a result not told yet, with `cancel_tool_call`.

A conversation holds one `LateResults`. What is its own is when a waiting result is told,
and how the reply about it is delivered: a call tells it once nothing is being said or
played, and chat once no reply is being written to the gem.
"""

import asyncio
import functools
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from app.entities.loop import ToolCall
from app.entities.model import Item, ToolSpec
from app.entities.tools import Tool

__all__ = [
    "CANCELLED",
    "CANCEL_TOOL",
    "NOTHING_RUNNING",
    "PLACEHOLDER",
    "LateResult",
    "LateResults",
    "delivers",
    "late_stub",
]

# Stands in for the result of a call that was still running when the user cut in.
PLACEHOLDER = (
    "Still running in the background, under call_id {call_id}. It has not failed. Only if "
    "the user says they no longer want this, stop it with cancel_tool_call and that "
    "call_id. Otherwise leave it alone: its result will reach you by itself, as a separate "
    'call with "deferred_result_for": "{call_id}". Until then, do not search for this '
    "again, in any wording."
)

CANCEL_TOOL = ToolSpec(
    name="cancel_tool_call",
    description=(
        "Stop a search that is still running in the background. Call this only when the "
        "user says they no longer want its result, and always call it then: nothing is "
        "cancelled unless this is called."
    ),
    parameters={
        "type": "object",
        "properties": {
            "call_id": {"type": "string", "description": "The call_id of the running call."}
        },
        "required": ["call_id"],
        "additionalProperties": False,
    },
)

# What `cancel_tool_call` answers: something was stopped, or there was nothing to stop.
CANCELLED = "Cancelled."
NOTHING_RUNNING = "Nothing is running under that call_id."


@dataclass(frozen=True, slots=True)
class LateResult:
    """A late result, ready to be added to the history."""

    call: ToolCall  # the call the model made, which was left running
    stub: ToolCall  # the call we write to carry its result, with the result as its output


class LateResults:
    """The tool calls of one conversation that outlived their replies, until they are told.

    `ready` is called each time a result comes in and waits here to be told.
    """

    def __init__(self, ready: Callable[[], None]) -> None:
        self._ready = ready
        # Calls still running after their reply was cut off, by call id.
        self._running: dict[str, ToolCall] = {}
        # Results of such calls that have come in and are waiting to be told, in order.
        self._waiting: list[tuple[ToolCall, str]] = []

    @property
    def tool(self) -> Tool:
        """`cancel_tool_call`, for this conversation's toolbox."""
        return Tool(CANCEL_TOOL, self._cancel)

    @property
    def pending(self) -> bool:
        """Whether a call has no result in the history yet: still running, or waiting."""
        return bool(self._running or self._waiting)

    @property
    def ready(self) -> bool:
        """Whether a result is waiting to be told."""
        return bool(self._waiting)

    def holds(self, call_id: str) -> bool:
        """Whether the call with this id is running here, or its result waiting."""
        return call_id in self._running or any(call.call_id == call_id for call, _ in self._waiting)

    def settle(self, call: ToolCall) -> bool:
        """Make a call of a reply that is over final. Return whether it was left running.

        A call still running is not stopped: its result is the placeholder, and its real
        result waits here once it comes. One that finished in the instant before keeps
        its own result: nothing is late.
        """
        if call.output is not None:
            return False
        running = call.running
        if running is None or running.done():
            call.output = running.result() if running and not running.cancelled() else ""
            return False
        call.output = PLACEHOLDER.format(call_id=call.call_id)
        self._running[call.call_id] = call
        running.add_done_callback(functools.partial(self._finished, call))
        return True

    def take(self) -> list[LateResult]:
        """The results waiting to be told, in the order they came, each with its stub.

        They are no longer held here: the caller adds them to the history.
        """
        taken, self._waiting = self._waiting, []
        return [LateResult(call, late_stub(call, output)) for call, output in taken]

    def forget(self, calls: Sequence[ToolCall]) -> None:
        """Stop these calls and drop their results: no history holds their placeholders."""
        for call in calls:
            self._running.pop(call.call_id, None)
            self._waiting = [late for late in self._waiting if late[0] is not call]
            if call.running is not None:
                call.running.cancel()

    def stop(self) -> list[asyncio.Task[str]]:
        """Stop every call still running and drop every result. Return the tasks stopped."""
        tasks = [call.running for call in self._running.values() if call.running is not None]
        self._running.clear()
        self._waiting.clear()
        for task in tasks:
            task.cancel()
        return tasks

    def _finished(self, call: ToolCall, finished: asyncio.Task[str]) -> None:
        if self._running.pop(call.call_id, None) is None:
            # Stopped first. It may have finished in that same instant, too late to be
            # stopped: its result is dropped all the same, since the model has been told
            # it was cancelled.
            return
        if finished.cancelled():
            return
        self._waiting.append((call, finished.result()))
        self._ready()

    async def _cancel(self, arguments: str) -> str:
        """Stop a call that outlived its reply, or drop its result if it has come in.

        This is `cancel_tool_call`. The call's result never reaches the model.
        """
        call_id = str(json.loads(arguments)["call_id"])
        for index, (call, _) in enumerate(self._waiting):
            if call.call_id == call_id:
                del self._waiting[index]
                return CANCELLED
        # Taken out of the running ones here and now. If it finishes in this same
        # instant, too late to be stopped, `_finished` finds it gone and drops its result.
        call = self._running.pop(call_id, None)
        if call is None or call.running is None:
            return NOTHING_RUNNING
        call.running.cancel()
        return CANCELLED


def late_stub(call: ToolCall, output: str) -> ToolCall:
    """The call we write to carry `output`, the late result of `call`.

    A result has to follow its own call directly, and the real call already has its
    placeholder. So this call names the real one, and `output` is its result.
    """
    return ToolCall(
        call_id=f"late_{uuid.uuid4().hex[:16]}",
        name=call.name,
        arguments=json.dumps({"deferred_result_for": call.call_id}),
        tool=call.tool,
        output=output,
    )


def delivers(item: Item) -> str | None:
    """The id of the call whose late result a history item carries, if it is such a stub."""
    if "type" not in item or item["type"] != "function_call":
        return None
    try:
        arguments = json.loads(item["arguments"])
    except ValueError:
        return None
    if not isinstance(arguments, dict):
        return None
    found = cast("dict[str, object]", arguments).get("deferred_result_for")
    return found if isinstance(found, str) else None

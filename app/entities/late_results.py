"""
Late results: what a search that outlives the reply that made it is given, in a call and
in writing alike.

When a reply is cut off while one of its searches is running, the search is left to
run. Its call gets the placeholder below as its result, so the history stays a valid
chain of calls and results. When the search finishes, its real result is added after
everything else, as a call we write ourselves (a stub that names the real call)
followed by the result. The model can stop a running search with `cancel_tool_call`.
"""

import json
import uuid
from typing import cast

from app.entities.loop import ToolCall
from app.entities.model import Item, ToolSpec

__all__ = [
    "CANCELLED",
    "CANCEL_TOOL",
    "NOTHING_RUNNING",
    "PLACEHOLDER",
    "delivers",
    "late_stub",
]

# Stands in for the result of a call that was still running when the user spoke.
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
# A call has the same words in its own code.
CANCELLED = "Cancelled."
NOTHING_RUNNING = "Nothing is running under that call_id."


def late_stub(call: ToolCall, output: str) -> ToolCall:
    """The call we write to carry `output`, the late result of `call`, in writing.

    A call builds its stubs the same way in its own code.

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

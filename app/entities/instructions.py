"""
The instructions a gem's model is given, the same whichever way the gem is reached.

They are in two parts: the environment, which is the Darbar text and the same for every
gem, and the gem's own name and instructions. What the model needs of its own follows.
The instructions are the start of every request, and a prompt cache only helps while
that start stays the same, so they go in order from what changes least to what changes
most:

1. the Darbar text: how things are here, for every gem
2. the gem: who this gem is, and its own instructions
3. what the model itself needs

Nothing in them changes by itself: only an edit of the Darbar text or the gem, or a
change of model. The date and the time are not in them: a line of ours in the
conversation gives them (see `history.py`).

Each part is a paragraph of its own, followed by a blank line. An empty part adds
nothing, not even the blank line.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.entities.gem_ent import GemConfig

__all__ = ["DEFAULT_DARBAR", "Layer", "instruction_layers", "instructions", "joined"]

# How things are here, told to every gem first, until the user writes their own (see
# `darbar_ent.py`). It says what a call and writing are, what a reply ending in "…" means
# and what a late tool result is. A DRAFT THE OWNER HAS NOT READ YET.
DEFAULT_DARBAR = """\
The user keeps a few assistants of their own, which they call gems. Each gem has its \
own name and its own work. The place they share is called the Darbar.

The user reaches you in one conversation, by calling you or by writing to you.
- A call is a live spoken conversation. Everything you write is read aloud by a \
text-to-speech voice. The user's words come from speech recognition and may contain \
mistakes: go with the likely meaning, and ask when it is unclear. A line in the \
conversation says when a call starts and when it ends.
- Outside a call the conversation is written: the user reads what you write in a chat \
window.
- The user can interrupt you at any time, by speaking in a call or by sending a new \
message in writing. An earlier reply of yours that ends in "…" was cut off at that point. \
The user heard or read nothing after it, so do not assume they know the rest. Never end a \
reply with "…" yourself.
- A tool call that is still running when the user interrupts carries on in the \
background. Do not make the same call again, and answer whatever else they asked. Only \
if they say they no longer want it, call cancel_tool_call: nothing \
is cancelled unless you make that call. The result reaches you by itself, as a call with \
"deferred_result_for" in its arguments. You did not make that call: it only delivers the \
late result. When it arrives, tell the user what it found straight away, without being \
asked, unless they have said they no longer want it. If they say something else before \
you have told them, answer that and then tell them. If you have already told them the \
same thing, do not repeat it: say in a few words that the result confirms it."""


@dataclass(frozen=True, slots=True)
class Layer:
    """One part of the instructions. `text` is empty when the layer has nothing to say."""

    name: str
    text: str


def instruction_layers(
    *,
    darbar: str | None = None,
    gem: GemConfig,
    model_needs: str,
) -> list[Layer]:
    """The layers of a gem's instructions, in the order they are sent.

    `darbar` is the Darbar text the user wrote, or None for the built-in one. An empty
    one tells the gem nothing. `model_needs` is what the model adds of its own.
    """
    return [
        Layer("darbar", _paragraph(DEFAULT_DARBAR if darbar is None else darbar)),
        Layer("gem", f"You are {gem.name}.\n\n" + _paragraph(gem.instructions)),
        Layer("model", _paragraph(model_needs)),
    ]


def instructions(
    *,
    darbar: str | None = None,
    gem: GemConfig,
    model_needs: str,
) -> str:
    """The whole instructions, as the model is sent them."""
    return joined(instruction_layers(darbar=darbar, gem=gem, model_needs=model_needs))


def joined(layers: Sequence[Layer]) -> str:
    """The layers as one text, as the model is sent them."""
    return "".join(layer.text for layer in layers)


def _paragraph(text: str) -> str:
    """A text as a paragraph of the instructions, or nothing if it is blank.

    Space around it is dropped, so the parts stay one blank line apart. Everything
    between its first and last letter is sent as it was written.
    """
    text = text.strip()
    return f"{text}\n\n" if text else ""

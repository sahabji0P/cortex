"""
The instructions a conversation's model is given, put together in layers.

The instructions are the start of every request, and a prompt cache only helps while
that start stays the same. So they are fixed for the whole conversation, and the
layers go in order from what changes least to what changes most:

1. the Darbar: what is true for every gem
2. the gem: who this gem is
3. how the conversation is carried: the spoken rules, or the written ones
4. the tools' rules, for the tools that are offered
5. what the model itself needs
6. the date

A layer that is a paragraph of its own ends with a blank line, so the next one starts
after it. A layer of rules is more bullets, and carries on the list before it. An empty
layer adds nothing.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.entities.gem_ent import GemConfig

__all__ = ["DEFAULT_DARBAR", "Layer", "instruction_layers", "instructions"]

# What every gem is told of where it is, until the user writes their own (see
# `darbar_ent.py`). The blank line after it is added where it is put in, so a text the
# user writes gets the same.
DEFAULT_DARBAR = """\
The user keeps a few assistants of their own, which they call gems. Each gem has its \
own name and its own work. The place they share is called the Darbar."""

# How a call is carried. The gem has already said who it is. Its rules follow after a
# blank line.
_SPOKEN = """\
You are in a live spoken conversation. Everything you write is read aloud by a \
text-to-speech voice.

"""

_SPOKEN_RULES = """\
- Talk like a person on a call: short, natural sentences. Keep to one or two sentences \
unless the user asks for more.
- Use plain words only: no markdown, lists, emoji, links or symbols. Write numbers the \
way they are said.
- The user's words come from speech recognition and may contain mistakes. Go with the \
likely meaning, and ask when it is unclear.
- The user can interrupt you at any time, the way people do in conversation. If they ask \
or say something, answer that first, then go back to what you were saying if it is still \
worth finishing. If it was only a sound, or a word like "yes" or "okay", carry on from \
where you stopped.
- An earlier reply of yours that ends in "…" was cut off at that point. The user heard \
nothing after it, so do not assume they know the rest. When you carry on, continue from \
there instead of starting again. Never end a reply with "…" yourself.
"""

# How a written conversation is carried. The gem has already said who it is.
_WRITTEN = """\
You are in a written conversation. The user reads what you write in a chat window.

"""

_WRITING_STYLE = """\
- Write the way a person writes a message: plainly, and only as long as the answer needs.
"""

# This rule goes when the app can show formatting.
_PLAIN_TEXT = """\
- Write plain text, with no markdown or other formatting marks. The window shows your \
words exactly as you write them.
"""

_CUT_IN_WRITING = """\
- An earlier reply of yours that ends in "…" was cut off at that point, because the user \
sent a new message. They read nothing after it, so do not assume they know the rest. \
Never end a reply with "…" yourself.
"""

_WRITTEN_RULES = _WRITING_STYLE + _PLAIN_TEXT + _CUT_IN_WRITING

# What follows the opening of the last search rule, the same in a call and in writing:
# a search carries on after the user has cut its reply, and its result comes later.
_SEARCH_CARRIES_ON = """\
the search carries on in the background. Do not search for the same thing again: say it \
is still coming, and answer whatever else they asked. Only if they say they no longer \
want it, call cancel_tool_call: nothing is cancelled unless you make that call. The \
result reaches you by itself, as a call with "deferred_result_for" in its arguments. You \
did not make that call: it only delivers the late result. When it arrives, tell the user \
what it found straight away, without being asked, unless they have said they no longer \
want it. If they say something else before you have told them, answer that and then tell \
them. If you have already told them the same thing, do not repeat it: say in a few words \
that the search confirms it.
"""

# The search rules for writing. As in a call, a search carries on in the background
# when a new message cuts the reply that made it, so the last rule is the call's, with
# a new message in place of an interruption.
_WRITTEN_SEARCH_RULES = (
    """\
- When what you are about to do will keep the user waiting, such as looking something \
up, first say in a few words what you are going to do, the way a person would.
- When you answer from search results, name the source.
- If the user sends a new message while a search is running, """
    + _SEARCH_CARRIES_ON
)

_SEARCH_RULES = (
    """\
- When what you are about to do will keep the user waiting for more than a second, such \
as looking something up, first say in a few words what you are going to do, the way a \
person would, and do it in that same turn: the user hears nothing else while they wait.
- When you answer from search results, name the source.
- If the user interrupts while a search is running, """
    + _SEARCH_CARRIES_ON
)

# The last line of the instructions. It is the one part that differs from one day to the
# next, so everything before it is the same in every conversation.
_TODAY = """
Today is {today}.
"""


@dataclass(frozen=True, slots=True)
class Layer:
    """One part of the instructions. `text` is empty when the layer has nothing to say."""

    name: str
    text: str


def instruction_layers(
    *,
    darbar: str | None = None,
    gem: GemConfig,
    search: bool,
    written: bool = False,
    model_needs: str,
    now: datetime | None = None,
) -> list[Layer]:
    """The layers of a conversation's instructions, in the order they are sent.

    `search` says whether web search is offered, and `model_needs` is what the model
    adds of its own.

    `darbar` is the Darbar text the user wrote, or None for the built-in one. The gem is
    told it first. An empty one tells it nothing.
    `written` gives the rules for writing in place of the spoken ones.
    """
    today = (now or datetime.now(UTC)).strftime("%A, %d %B %Y")
    return [
        Layer("darbar", _darbar(darbar)),
        Layer("gem", _gem(gem)),
        Layer("written", _WRITTEN + _WRITTEN_RULES)
        if written
        else Layer("spoken", _SPOKEN + _SPOKEN_RULES),
        Layer("tools", "" if not search else _WRITTEN_SEARCH_RULES if written else _SEARCH_RULES),
        Layer("model", model_needs),
        Layer("date", _TODAY.format(today=today)),
    ]


def instructions(
    *,
    darbar: str | None = None,
    gem: GemConfig,
    search: bool,
    written: bool = False,
    model_needs: str,
    now: datetime | None = None,
) -> str:
    """The whole instructions, as the model is sent them."""
    layers = instruction_layers(
        darbar=darbar,
        gem=gem,
        search=search,
        written=written,
        model_needs=model_needs,
        now=now,
    )
    return "".join(layer.text for layer in layers)


def _darbar(darbar: str | None) -> str:
    """The Darbar text, as one paragraph: the built-in one when the user has written none."""
    # Space around it is dropped, as for a gem's own text, so the layers stay one blank
    # line apart. An empty text adds nothing, not even the blank line.
    text = (DEFAULT_DARBAR if darbar is None else darbar).strip()
    return f"{text}\n\n" if text else ""


def _gem(gem: GemConfig) -> str:
    """Who the gem is: its name, then its own instructions as its owner wrote them."""
    # Space around the gem's own text is dropped, so the layers stay one blank line
    # apart. Everything between its first and last letter is sent as stored.
    own = gem.instructions.strip()
    return f"You are {gem.name}.\n\n" + (f"{own}\n\n" if own else "")

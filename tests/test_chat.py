"""Written chat with a gem: what it is told, what is stored, and the standing socket.

The expected instructions are written out here in full, not built from the module's
constants, so any change to what the model reads shows up as a failing test.
"""

from datetime import UTC, datetime

import pytest

from app.entities.gem_ent import GemConfig
from app.entities.instructions import instruction_layers, instructions

_NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

_KUBER_IN_WRITING = (
    "The user keeps a few assistants of their own, which they call gems. Each gem has its "
    "own name and its own work. The place they share is called the Darbar.\n"
    "\n"
    "You are Kuber.\n"
    "\n"
    "You look after the user's personal finances.\n"
    "\n"
    "You are in a written conversation. The user reads what you write in a chat window.\n"
    "\n"
    "- Write the way a person writes a message: plainly, and only as long as the answer "
    "needs.\n"
    "- Write plain text, with no markdown or other formatting marks. The window shows your "
    "words exactly as you write them.\n"
    '- An earlier reply of yours that ends in "…" was cut off at that point, because the '
    "user sent a new message. They read nothing after it, so do not assume they know the "
    'rest. Never end a reply with "…" yourself.\n'
)
_WRITTEN_SEARCH_RULES = (
    "- When what you are about to do will keep the user waiting, such as looking something "
    "up, first say in a few words what you are going to do, the way a person would.\n"
    "- When you answer from search results, name the source.\n"
)
_TODAY = "\nToday is Tuesday, 17 November 2026.\n"


def _kuber(**fields: object) -> GemConfig:
    return GemConfig.model_validate(
        {
            "name": "Kuber",
            "instructions": "You look after the user's personal finances.",
            "created_at": _NOW,
            "updated_at": _NOW,
            **fields,
        }
    )


# ---- what a gem is told in writing


@pytest.mark.parametrize(
    ("search", "expected"),
    [
        (True, _KUBER_IN_WRITING + _WRITTEN_SEARCH_RULES + "- A need of the model's.\n" + _TODAY),
        (False, _KUBER_IN_WRITING + "- A need of the model's.\n" + _TODAY),
    ],
)
def test_a_gem_in_writing_is_told_the_written_rules(search: bool, expected: str) -> None:
    told = instructions(
        gem=_kuber(),
        search=search,
        written=True,
        model_needs="- A need of the model's.\n",
        now=_NOW,
    )
    assert told == expected


def test_the_written_layers_take_the_place_of_the_spoken_ones() -> None:
    layers = instruction_layers(gem=_kuber(), search=True, written=True, model_needs="", now=_NOW)
    assert [layer.name for layer in layers] == [
        "darbar",
        "gem",
        "written",
        "tools",
        "model",
        "date",
    ]
    told = "".join(layer.text for layer in layers)
    # Nothing of a call's: no speech, and no searches that carry on in the background.
    assert "spoken" not in told
    assert "cancel_tool_call" not in told


def test_writing_is_always_with_a_gem() -> None:
    with pytest.raises(ValueError, match="always with a gem"):
        instructions(gem=None, search=False, written=True, model_needs="")

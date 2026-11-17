"""
Opening a call: everything before its audio starts to flow.

A call is asked for with three settings: a gem, a model and who speaks first. Opening
it reads the gem's config and the Darbar text, works out who speaks first, and opens the
voice conversation. Both ways a call can reach the backend use this one opener: the old
call socket and the standing socket. Only what they do with the outcome differs.

Two kinds of "no" are told apart, because the app is told them apart:

- a refusal (`CallRefused`): the call was not opened because of what was asked, such as
  a gem that is not there or a model whose provider has no key. Asking again the same
  way gets the same answer.
- a fault: any other exception, raised as it is. The backend could not open the call,
  as when a gem's stored config cannot be read or a speech service is out of reach.
  `fault_reason` puts it in words for the app.
"""

from pydantic import ValidationError

from app.core.errors import NotFoundError
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.model import ModelName
from app.entities.voice_ent import VoiceConversation, VoiceEntityClass

__all__ = ["CallEntityClass", "CallRefused", "fault_reason"]


class CallRefused(Exception):
    """The call was not opened because of what was asked. `reason` says why, in words."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def fault_reason(exc: BaseException) -> str:
    """What to tell the app when a call could not be opened through a fault of ours."""
    if isinstance(exc, ValidationError):
        # The gem is there, but its stored config cannot be read here, as when a newer
        # cortex wrote it.
        return "the gem's config cannot be read"
    return "speech services unavailable"


class CallEntityClass:
    def __init__(
        self,
        *,
        gems: GemEntityClass,
        darbar: DarbarEntityClass,
        voice: VoiceEntityClass,
    ) -> None:
        self._gems = gems
        self._darbar = darbar
        self._voice = voice

    async def open(
        self,
        gem: str | None = None,
        *,
        model: ModelName | None = None,
        speaks_first: bool | None = None,
    ) -> VoiceConversation:
        """Open a call. The caller owns the conversation and must `close()` it.

        `gem` is a gem's id, or None for a call with Cortex. `model` and `speaks_first`
        win over the gem's when they are given; left out, they are the gem's to say, and
        without a gem they are the configured model and the user speaking first.

        Raises `CallRefused` for a gem that is not there (or an id that is not an id) and
        for a model whose provider has no key. Anything else is a fault, raised as it is.
        """
        config: GemConfig | None = None
        # Read once, here, so the call keeps the text it opened with. A call without a
        # gem is not told it, so it does not read it.
        darbar: str | None = None
        if gem is not None:
            try:
                config = await self._gems.get(gem)
            except NotFoundError:
                raise CallRefused("unknown gem") from None
            darbar = (await self._darbar.read()).text
        try:
            # Nothing is awaited after this returns, so a caller that cancels the opening
            # either gets the conversation back, to close, or none was made.
            return await self._voice.open_conversation(
                model, speaks_first=speaks_first, gem=config, darbar=darbar
            )
        except LookupError as exc:
            # The model is offered, but its provider has no key here.
            raise CallRefused(str(exc)) from exc

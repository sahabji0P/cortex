"""
Opening a call: everything before its audio starts to flow.

A call is always with a gem, and may say who speaks first. Opening it reads the gem's
config and the Darbar text, works out who speaks first, and opens the voice
conversation. The standing socket (`app/routers/link`) holds the call from there.

Two kinds of "no" are told apart, because the app is told them apart:

- a refusal (`CallRefused`): the call was not opened because of what was asked, such as
  a gem that is not there or a model whose provider has no key. Asking again the same
  way gets the same answer.
- a fault (`CallFault`): the backend could not open the call, as when a gem's stored
  config cannot be read or a speech service is out of reach. It says which in words,
  for the app, and carries the failure itself.
"""

from pydantic import ValidationError

from app.core.errors import NotFoundError
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemEntityClass
from app.entities.voice_ent import VoiceConversation, VoiceEntityClass

__all__ = ["CallEntityClass", "CallFault", "CallRefused"]


class CallRefused(Exception):
    """The call was not opened because of what was asked. `reason` says why, in words."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CallFault(Exception):
    """The call could not be opened through a fault of ours.

    `reason` says what could not be done, in words. `cause` is the failure itself.
    """

    def __init__(self, reason: str, cause: Exception) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


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

    async def open(self, gem: str, *, speaks_first: bool | None = None) -> VoiceConversation:
        """Open a call with a gem. The caller owns the conversation and must `close()` it.

        `gem` is the gem's id. Its config says which model answers. `speaks_first` wins
        over the gem's when it is given; left out, it is the gem's to say.

        Raises `CallRefused` for a gem that is not there (or an id that is not an id) and
        for a model whose provider has no key. Anything else that goes wrong is raised as
        a `CallFault`, which says what could not be read or reached.
        """
        try:
            config = await self._gems.get(gem)
        except NotFoundError:
            raise CallRefused("unknown gem") from None
        except ValidationError as exc:
            # The gem is there, but its stored config cannot be read here, as when a
            # newer cortex wrote it.
            raise CallFault("the gem's config cannot be read", exc) from exc
        except Exception as exc:
            raise CallFault("the gem could not be read", exc) from exc
        try:
            # Read once, here, so the call keeps the text it opened with.
            darbar = (await self._darbar.read()).text
        except Exception as exc:
            raise CallFault("the Darbar text could not be read", exc) from exc
        try:
            # Nothing is awaited after this returns, so a caller that cancels the opening
            # either gets the conversation back, to close, or none was made.
            return await self._voice.open_conversation(
                config, speaks_first=speaks_first, darbar=darbar
            )
        except LookupError as exc:
            # The gem's model is offered, but its provider has no key here.
            raise CallRefused(str(exc)) from exc
        except Exception as exc:
            # A speech service is unreachable or not configured.
            raise CallFault("speech services unavailable", exc) from exc

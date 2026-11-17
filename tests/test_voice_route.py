"""The old call socket, now that it opens its call through the call entity.

How it reads `?gem=`, `?model=` and `?first=` is in `test_voice_gem.py`. These are the
two other ways an opening can go: a refusal closes the socket with 1008 and the reason,
and a fault closes it with 1011 and is raised.
"""

from typing import TYPE_CHECKING, cast

import pytest

from app.routers.voice.service import VoiceServiceV1Class
from tests.test_darbar import _darbar
from tests.test_gems import _gems
from tests.test_voice_gem import _ServiceSocket

if TYPE_CHECKING:
    from fastapi import WebSocket

    from app.entities.voice_ent import VoiceEntityClass


class _FailingVoice:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def open_conversation(self, model=None, *, speaks_first=None, gem=None, darbar=None):
        raise self.failure


def _service(failure: Exception) -> VoiceServiceV1Class:
    gems, _ = _gems()
    voice = cast("VoiceEntityClass", _FailingVoice(failure))
    return VoiceServiceV1Class(voice, gems, _darbar()[0])


async def test_a_model_whose_provider_has_no_key_closes_the_socket_with_1008() -> None:
    reason = "no Anthropic key is set, so Haiku 5.5 cannot be used"
    socket = _ServiceSocket({"model": "Haiku 5.5"})

    await _service(LookupError(reason)).converse(cast("WebSocket", socket))

    assert socket.closed == (1008, reason)


async def test_speech_services_that_cannot_be_reached_close_the_socket_with_1011() -> None:
    socket = _ServiceSocket({})

    with pytest.raises(ConnectionError, match="no route"):
        await _service(ConnectionError("no route")).converse(cast("WebSocket", socket))

    assert socket.closed == (1011, "speech services unavailable")

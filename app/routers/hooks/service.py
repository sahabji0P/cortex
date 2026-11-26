import logging
from functools import cache
from typing import Annotated

from fastapi import Depends, Request, Response

from app.entities import EntitiesRegistry
from app.entities.watch_ent import HookOutcome, WatchEntityClass

# The longest body a post may have. A longer one is refused (413), and not read further.
_BODY_MAX_BYTES = 1024 * 1024

# What each outcome is answered with. Every answer is empty: what was posted is never
# sent back, and nothing is said of a watch.
_STATUS: dict[HookOutcome, int] = {"taken": 202, "ignored": 200, "refused": 401, "paused": 409}

_log = logging.getLogger(__name__)


class HooksServiceV1Class:
    def __init__(self, watches: WatchEntityClass | None = None) -> None:
        # Tests hand in their own. The app uses the one the registry made at startup.
        self.watches = EntitiesRegistry.Watches if watches is None else watches

    async def hook(self, token: str, request: Request) -> Response:
        """Hand a post to the watch whose address it came to. Nothing of it is logged."""
        watch = await self.watches.find_hook(token)
        if watch is None:
            return Response(status_code=404)
        body = await _body(request)
        if body is None:
            return Response(status_code=413)
        try:
            outcome = await self.watches.hooked(watch, request.headers, body)
        except Exception as exc:
            # Not kept, so the sender may try again. What went wrong is named, never what
            # was posted.
            _log.warning("a post to watch %s could not be kept: %s", watch.id, type(exc).__name__)
            return Response(status_code=503)
        return Response(status_code=_STATUS[outcome])


async def _body(request: Request) -> bytes | None:
    """The body of the post, or None if it is longer than a post may be."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _BODY_MAX_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > _BODY_MAX_BYTES:
            return None
    return bytes(body)


# Cached, so every request gets the one service.
@cache
def get_hooks_service_v1() -> HooksServiceV1Class:
    return HooksServiceV1Class()


class HooksServiceRegistry:
    type V1 = Annotated[HooksServiceV1Class, Depends(get_hooks_service_v1)]

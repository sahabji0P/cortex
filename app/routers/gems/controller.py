from typing import Annotated, Any

from fastapi import APIRouter, Query, Response

from app.routers.gems.models import GemsModels
from app.routers.gems.service import GemsServiceRegistry

# No sign-in, like the voice routes: this backend has one user for now.
router = APIRouter(prefix="/v1/gems", tags=["gems"])


@router.post("", status_code=201)
async def create_gem(
    payload: GemsModels.V1.Input.CreateGem,
    service: GemsServiceRegistry.V1,
) -> GemsModels.V1.Output.Gem:
    return await service.create_gem(payload)


@router.get("")
async def list_gems(service: GemsServiceRegistry.V1) -> list[GemsModels.V1.Output.Gem]:
    """Every gem, in the order they were made."""
    return await service.list_gems()


@router.get("/{gem_id}")
async def get_gem(gem_id: str, service: GemsServiceRegistry.V1) -> GemsModels.V1.Output.Gem:
    return await service.get_gem(gem_id)


@router.get("/{gem_id}/chat")
async def get_chat(gem_id: str, service: GemsServiceRegistry.V1) -> GemsModels.V1.Output.Chat:
    """The gem's conversation so far, calls included, oldest first, for showing on screen."""
    return await service.get_chat(gem_id)


@router.post("/{gem_id}/reset", status_code=204)
async def reset_gem(gem_id: str, service: GemsServiceRegistry.V1) -> Response:
    """Reset the gem: it starts again, knowing nothing from before. Its config stays.

    A reply being written is cut first. Refused with 409 while the gem is in a call.
    The gem's log keeps everything, with the reset as an entry of its own.
    """
    await service.reset_gem(gem_id)
    return Response(status_code=204)


@router.post("/{gem_id}/compact")
async def compact_gem(
    gem_id: str, service: GemsServiceRegistry.V1
) -> GemsModels.V1.Output.Compacted:
    """Compact the gem's conversation now: what came before `through` is sent as `summary`.

    Allowed whenever the gem is between turns, warm or cold. A reply being written is
    not cut: it is done first. Refused with 409 while the gem is in a call, or when
    there is nothing to compact. 502 if the summary could not be written: nothing
    changes then.
    """
    return await service.compact_gem(gem_id)


@router.get("/{gem_id}/context")
async def get_context(gem_id: str, service: GemsServiceRegistry.V1) -> GemsModels.V1.Output.Context:
    """Where the gem's context stands: its size, when its cache expires, and when to compact."""
    return await service.get_context(gem_id)


@router.get("/{gem_id}/log")
async def get_log(
    gem_id: str,
    service: GemsServiceRegistry.V1,
    before: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> GemsModels.V1.Output.Log:
    """A page of the gem's log, oldest first: everything it remembers and everything that happened.

    The latest page, or with `before` the entries just before that number. To read
    back from the end, ask again with `before` set to the `seq` of a page's first entry.
    """
    return await service.get_log(gem_id, before=before, limit=limit)


@router.get("/{gem_id}/log/{seq}")
async def get_log_entry(gem_id: str, seq: int, service: GemsServiceRegistry.V1) -> dict[str, Any]:
    """One entry of the gem's log, in full: the link trims a long one (`partial`)."""
    return await service.get_log_entry(gem_id, seq)


@router.get("/{gem_id}/log/{seq}/sent")
async def get_log_sent(gem_id: str, seq: int, service: GemsServiceRegistry.V1) -> dict[str, Any]:
    """Everything the request written down as entry `seq` was sent: items, instructions, tools."""
    return await service.get_log_sent(gem_id, seq)


@router.patch("/{gem_id}")
async def update_gem(
    gem_id: str,
    payload: GemsModels.V1.Input.UpdateGem,
    service: GemsServiceRegistry.V1,
) -> GemsModels.V1.Output.Gem:
    return await service.update_gem(gem_id, payload)


@router.delete("/{gem_id}", status_code=204)
async def delete_gem(gem_id: str, service: GemsServiceRegistry.V1) -> Response:
    await service.delete_gem(gem_id)
    return Response(status_code=204)

from functools import cache
from typing import Annotated

from bson import ObjectId
from fastapi import Depends

from app.core.errors import NotFoundError
from app.entities import EntitiesRegistry
from app.entities.gem_ent import GemEntityClass
from app.entities.watch_ent import Watch, WatchEntityClass
from app.routers.watches.models import WatchesModels


class WatchesServiceV1Class:
    def __init__(
        self, watches: WatchEntityClass | None = None, gems: GemEntityClass | None = None
    ) -> None:
        # Tests hand in their own. The app uses the ones the registry made at startup.
        self.watches = EntitiesRegistry.Watches if watches is None else watches
        self.gems = EntitiesRegistry.Gems if gems is None else gems

    @staticmethod
    def _watch_out(watch: Watch) -> WatchesModels.V1.Output.Watch:
        mcp = watch.mcp if watch.source == "mcp" else None
        return WatchesModels.V1.Output.Watch(
            id=str(watch.id),
            gem=str(watch.gem_id),
            about=watch.about,
            source=watch.source,
            delivery=watch.delivery,
            state=watch.state,
            reason=watch.reason,
            connection=None if mcp is None else mcp.connection_id,
            event=None if mcp is None else mcp.event,
            arguments=None if mcp is None else mcp.arguments,
            every_minutes=watch.every_minutes,
            next_at=watch.due_at,
            # Whether it has an address only: the address holds its secret.
            has_address=watch.source == "webhook",
            created_at=watch.created_at,
            last_event_at=watch.last_event_at,
            events=watch.events,
        )

    async def _gem(self, gem_id: str) -> ObjectId:
        if not await self.gems.exists(gem_id):
            raise NotFoundError("Gem")
        return ObjectId(gem_id)

    async def list_watches(self, gem_id: str) -> list[WatchesModels.V1.Output.Watch]:
        watches = await self.watches.list_for(await self._gem(gem_id))
        return [self._watch_out(watch) for watch in watches]

    async def remove_watch(self, gem_id: str, watch_id: str) -> None:
        # The user removes it from the app: the gem is told, so it stops counting on it.
        await self.watches.remove(await self._gem(gem_id), watch_id, by="user", tell_gem=True)


# Cached, so every request gets the one service.
@cache
def get_watches_service_v1() -> WatchesServiceV1Class:
    return WatchesServiceV1Class()


class WatchesServiceRegistry:
    type V1 = Annotated[WatchesServiceV1Class, Depends(get_watches_service_v1)]

from functools import cache
from typing import Annotated, Any

from fastapi import Depends

from app.entities import EntitiesRegistry
from app.entities.compaction_ent import CompactionEntityClass
from app.entities.connection_ent import ConnectionEntityClass
from app.entities.conversation_ent import ConversationEntityClass, gem_key
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.routers.gems.models import GemsModels


class GemsServiceV1Class:
    def __init__(
        self,
        gems: GemEntityClass | None = None,
        conversations: ConversationEntityClass | None = None,
        connections: ConnectionEntityClass | None = None,
        compactions: CompactionEntityClass | None = None,
    ) -> None:
        # Tests hand in their own. The app uses the ones the registry made at startup.
        self.gems = EntitiesRegistry.Gems if gems is None else gems
        self._conversations = conversations
        self._compactions = compactions
        # Left out with gems handed in, a deleted gem is not taken out of any connection.
        self._connections = (
            EntitiesRegistry.Connections if gems is None and connections is None else connections
        )

    @property
    def conversations(self) -> ConversationEntityClass:
        # Looked up when it is first needed, so a test of the gems alone need not make it.
        if self._conversations is None:
            return EntitiesRegistry.Conversations
        return self._conversations

    @property
    def compactions(self) -> CompactionEntityClass:
        if self._compactions is None:
            return EntitiesRegistry.Compaction
        return self._compactions

    @staticmethod
    def _gem_out(gem: GemConfig) -> GemsModels.V1.Output.Gem:
        return GemsModels.V1.Output.Gem(
            id=str(gem.id),
            version=gem.version,
            name=gem.name,
            description=gem.description,
            image=gem.image,
            instructions=gem.instructions,
            model=gem.model,
            speaks_first=gem.speaks_first,
            tools=gem.tools,
            voice=gem.voice,
            speech_speed=gem.speech_speed,
            created_at=gem.created_at,
            updated_at=gem.updated_at,
        )

    async def create_gem(self, payload: GemsModels.V1.Input.CreateGem) -> GemsModels.V1.Output.Gem:
        gem = await self.gems.create(
            name=payload.name,
            description=payload.description,
            image=payload.image,
            instructions=payload.instructions,
            model=payload.model,
            speaks_first=payload.speaks_first,
            tools=payload.tools,
            voice=payload.voice,
            speech_speed=payload.speech_speed,
        )
        return self._gem_out(gem)

    async def list_gems(self) -> list[GemsModels.V1.Output.Gem]:
        return [self._gem_out(gem) for gem in await self.gems.list_all()]

    async def get_gem(self, gem_id: str) -> GemsModels.V1.Output.Gem:
        return self._gem_out(await self.gems.get(gem_id))

    async def update_gem(
        self,
        gem_id: str,
        payload: GemsModels.V1.Input.UpdateGem,
    ) -> GemsModels.V1.Output.Gem:
        return self._gem_out(await self.gems.update(gem_id, payload.changes()))

    async def delete_gem(self, gem_id: str) -> None:
        await self.gems.delete(gem_id)
        if self._connections is not None:
            # No connection is left naming a gem that is gone.
            await self._connections.forget_gem(gem_key(gem_id))

    async def get_chat(self, gem_id: str) -> GemsModels.V1.Output.Chat:
        return GemsModels.V1.Output.Chat(
            messages=[
                GemsModels.V1.Output.ChatMessage(
                    role=message.role, text=message.text, at=message.at
                )
                for message in await self.conversations.messages(gem_id)
            ]
        )

    async def reset_gem(self, gem_id: str) -> None:
        await self.conversations.reset(gem_id)

    async def compact_gem(self, gem_id: str) -> GemsModels.V1.Output.Compacted:
        compacted = await self.compactions.compact(gem_id)
        return GemsModels.V1.Output.Compacted(summary=compacted.summary, through=compacted.through)

    async def get_context(self, gem_id: str) -> GemsModels.V1.Output.Context:
        context = await self.compactions.context(gem_id)
        return GemsModels.V1.Output.Context(
            tokens=context.tokens,
            cache_until=context.cache_until,
            compact_from=context.compact_from,
        )

    async def get_log(
        self, gem_id: str, *, before: int | None, limit: int
    ) -> GemsModels.V1.Output.Log:
        entries, settings = await self.conversations.log_page(gem_id, before=before, limit=limit)
        return GemsModels.V1.Output.Log(entries=entries, settings=settings)

    async def get_log_sent(self, gem_id: str, seq: int) -> dict[str, Any]:
        return await self.conversations.log_sent(gem_id, seq)

    async def get_log_entry(self, gem_id: str, seq: int) -> dict[str, Any]:
        return await self.conversations.log_entry(gem_id, seq)


# Cached, so every request gets the one service.
@cache
def get_gems_service_v1() -> GemsServiceV1Class:
    return GemsServiceV1Class()


class GemsServiceRegistry:
    type V1 = Annotated[GemsServiceV1Class, Depends(get_gems_service_v1)]

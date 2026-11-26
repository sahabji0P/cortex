from collections.abc import Mapping

from bson import ObjectId

from app.adapters import AdapterRegistry
from app.entities.call_ent import CallEntityClass
from app.entities.chat_ent import ChatEntityClass
from app.entities.connection_ent import Connection, ConnectionEntityClass
from app.entities.conversation_ent import ConversationEntityClass
from app.entities.conversation_store import ConversationStoreClass, LogEntry
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.gem_event import GemEvent, McpEventKind, McpWatch, OnMcpEvent
from app.entities.gem_log import LogEntityClass
from app.entities.providers_ent import ProviderEntityClass
from app.entities.user_ent import User, UserEntityClass
from app.entities.voice_ent import VoiceEntityClass
from app.entities.watch_ent import Watch, WatchEntityClass


class _NoEventSink:
    """Stands in for the event sink until it is built: no event can be kept yet."""

    async def arrived(self, event: GemEvent) -> None:
        raise RuntimeError("events cannot be kept yet")


class _NoMcpEvents:
    """Stands in for the connections' MCP events until they are built: none are served."""

    async def kinds_for(self, gem_id: ObjectId) -> list[McpEventKind]:
        return []

    async def watch(self, watch: McpWatch, on_event: OnMcpEvent, hook_url: str | None) -> None:
        raise RuntimeError("MCP events are not served yet")

    async def posted(
        self, watch: McpWatch, headers: Mapping[str, str], body: bytes, on_event: OnMcpEvent
    ) -> bool:
        return False


class EntitiesRegistry:
    @classmethod
    async def initialize(cls, adapter: AdapterRegistry) -> None:
        """Initialize application entities."""
        await adapter.DocDB.create_index(User, [("email", 1)], unique=True)
        # The gem list is shown in the order the gems were made.
        await adapter.DocDB.create_index(GemConfig, [("created_at", 1)], name="gems_created")
        # A gem's log is read in order, whole or in pages, and no two entries share a
        # number. What the gem remembers is read from it by kind, in that same order.
        await adapter.DocDB.create_index(
            LogEntry,
            [("gem_id", 1), ("seq", 1)],
            unique=True,
            name="gem_log_in_order",
            only={"seq": {"$type": "number"}},
        )
        # A connection's key starts its tools' names, so no two may share one.
        await adapter.DocDB.create_index(
            Connection, [("key", 1)], unique=True, name="connections_key"
        )
        # A post to a webhook finds its watch by the token in its address. Timers have none.
        await adapter.DocDB.create_index(
            Watch,
            [("token", 1)],
            unique=True,
            name="watches_token",
            only={"token": {"$type": "string"}},
        )
        # A gem's watches are listed, the newest first.
        await adapter.DocDB.create_index(
            Watch, [("gem_id", 1), ("created_at", -1)], name="watches_of_gem"
        )
        cls.Users = UserEntityClass(
            docdb=adapter.DocDB,
            cache=adapter.Cache,
        )
        cls.Gems = GemEntityClass(docdb=adapter.DocDB)
        cls.Darbar = DarbarEntityClass(docdb=adapter.DocDB)
        cls.Connections = ConnectionEntityClass(docdb=adapter.DocDB, mcp=adapter.Mcp, gems=cls.Gems)
        cls.Providers = ProviderEntityClass(
            docdb=adapter.DocDB, openai=adapter.OpenAI, proxy=adapter.CLIProxy
        )
        await cls.Providers.restore()
        cls.ConversationStore = ConversationStoreClass(docdb=adapter.DocDB)
        cls.Log = LogEntityClass(cls.ConversationStore)
        cls.Conversations = ConversationEntityClass(
            gems=cls.Gems, store=cls.ConversationStore, log=cls.Log
        )
        cls.Chat = ChatEntityClass(
            gems=cls.Gems,
            darbar=cls.Darbar,
            conversations=cls.Conversations,
            openai=adapter.OpenAI,
            anthropic=adapter.Anthropic,
            search=adapter.Parallel,
            connections=cls.Connections,
        )
        cls.Voice = VoiceEntityClass(
            stt=adapter.CartesiaSTT,
            tts=adapter.CartesiaTTS,
            openai=adapter.OpenAI,
            anthropic=adapter.Anthropic,
            search=adapter.Parallel,
        )
        cls.Call = CallEntityClass(
            gems=cls.Gems,
            darbar=cls.Darbar,
            voice=cls.Voice,
            conversations=cls.Conversations,
            connections=cls.Connections,
        )
        # Run from the app's start (`start`) until it stops (`close`).
        cls.Watches = WatchEntityClass(
            docdb=adapter.DocDB,
            gems=cls.Gems,
            log=cls.Log,
            sink=_NoEventSink(),
            mcp_events=_NoMcpEvents(),
        )


__all__ = ["EntitiesRegistry"]

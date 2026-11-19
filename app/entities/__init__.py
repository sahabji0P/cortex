from app.adapters import AdapterRegistry
from app.entities.call_ent import CallEntityClass
from app.entities.chat_ent import ChatEntityClass
from app.entities.connection_ent import Connection, ConnectionEntityClass
from app.entities.conversation_ent import ConversationEntityClass
from app.entities.conversation_store import ConversationItem, ConversationStoreClass
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.gem_log import LogEntityClass
from app.entities.providers_ent import ProviderEntityClass
from app.entities.user_ent import User, UserEntityClass
from app.entities.voice_ent import VoiceEntityClass


class EntitiesRegistry:
    @classmethod
    async def initialize(cls, adapter: AdapterRegistry) -> None:
        """Initialize application entities."""
        await adapter.DocDB.create_index(User, [("email", 1)], unique=True)
        # The gem list is shown in the order the gems were made.
        await adapter.DocDB.create_index(GemConfig, [("created_at", 1)], name="gems_created")
        # A gem's conversation is read whole, in order: the items of its log. Only an
        # item has a place.
        await adapter.DocDB.create_index(
            ConversationItem,
            [("gem_id", 1), ("place", 1)],
            unique=True,
            name="gem_log_items_in_order",
            only={"kind": "item"},
        )
        # A gem's log is read in pages, in order, and no two entries share a number.
        await adapter.DocDB.create_index(
            ConversationItem,
            [("gem_id", 1), ("seq", 1)],
            unique=True,
            name="gem_log_in_order",
            only={"seq": {"$type": "number"}},
        )
        # A connection's key starts its tools' names, so no two may share one.
        await adapter.DocDB.create_index(
            Connection, [("key", 1)], unique=True, name="connections_key"
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


__all__ = ["EntitiesRegistry"]

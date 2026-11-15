from app.adapters import AdapterRegistry
from app.entities.conversation_ent import (
    ConversationEntityClass,
    Project,
    Session,
    SessionTurn,
)
from app.entities.pipeline_agent_sdk import AgentSDKPipelineEntityClass
from app.entities.stt_ent import STTEntityClass
from app.entities.user_ent import User, UserEntityClass
from app.entities.voice_ent import VoiceEntityClass


class EntitiesRegistry:
    @classmethod
    async def initialize(cls, adapter: AdapterRegistry) -> None:
        """Initialize application entities."""
        await adapter.DocDB.create_index(User, [("email", 1)], unique=True)
        await adapter.DocDB.create_index(Project, [("updated_at", -1)])
        await adapter.DocDB.create_index(
            Session,
            [("project_id", 1), ("updated_at", -1)],
            name="sessions_project_updated",
        )
        await adapter.DocDB.create_index(
            SessionTurn,
            [("session_id", 1), ("created_at", 1)],
            name="session_turns_session_created",
        )
        cls.Users = UserEntityClass(
            docdb=adapter.DocDB,
            cache=adapter.Cache,
        )
        cls.Conversations = ConversationEntityClass(docdb=adapter.DocDB)
        cls.STT = STTEntityClass(stt=adapter.CartesiaSTT)
        cls.AgentSDKPipeline = AgentSDKPipelineEntityClass()
        cls.Voice = VoiceEntityClass(
            stt=adapter.CartesiaSTT,
            tts=adapter.CartesiaTTS,
            llm=adapter.OpenAI,
            search=adapter.Parallel,
        )


__all__ = ["EntitiesRegistry"]

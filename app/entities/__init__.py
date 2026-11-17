from app.adapters import AdapterRegistry
from app.entities.pipeline_agent_sdk import AgentSDKPipelineEntityClass
from app.entities.user_ent import User, UserEntityClass
from app.entities.voice_ent import VoiceEntityClass


class EntitiesRegistry:
    @classmethod
    async def initialize(cls, adapter: AdapterRegistry) -> None:
        """Initialize application entities."""
        await adapter.DocDB.create_index(User, [("email", 1)], unique=True)
        cls.Users = UserEntityClass(
            docdb=adapter.DocDB,
            cache=adapter.Cache,
        )
        cls.AgentSDKPipeline = AgentSDKPipelineEntityClass()
        cls.Voice = VoiceEntityClass(
            stt=adapter.CartesiaSTT,
            tts=adapter.CartesiaTTS,
            openai=adapter.OpenAI,
            anthropic=adapter.Anthropic,
            search=adapter.Parallel,
        )


__all__ = ["EntitiesRegistry"]

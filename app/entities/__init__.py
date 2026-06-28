from app.adapters import AdapterRegistry
from app.entities.pipeline_llm import LLMPipelineEntityClass
from app.entities.tools_gemini import GeminiToolsEntityClass
from app.entities.tools_openai import OpenAIToolsEntityClass
from app.entities.user_ent import User, UserEntityClass


class EntitiesRegistry:
    @classmethod
    async def initialize(cls, adapter: AdapterRegistry) -> None:
        """Initialize application entities and shared tool builders."""
        await adapter.DocDB.create_index(User, [("email", 1)], unique=True)
        cls.Users = UserEntityClass(
            docdb=adapter.DocDB,
            cache=adapter.Cache,
        )
        cls.OpenAITools = OpenAIToolsEntityClass()
        cls.GeminiTools = GeminiToolsEntityClass()
        cls.LLMPipeline = LLMPipelineEntityClass(
            openai=adapter.OpenAI,
            gemini=adapter.Gemini,
        )


__all__ = ["EntitiesRegistry"]

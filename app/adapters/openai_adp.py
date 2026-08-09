from collections.abc import Iterable
from types import MappingProxyType
from typing import Literal, cast

from openai import AsyncOpenAI, AsyncStream, Omit, omit
from openai.types.responses import (
    EasyInputMessageParam,
    ResponseIncludable,
    ResponseInputParam,
    ResponseStreamEvent,
    ResponseTextConfigParam,
    ToolParam,
    WebSearchToolParam,
    response_create_params,
    tool_param,
    web_search_tool_param,
)

# Re-exported (see __all__) so the pipeline can isinstance-narrow web-search citations.
from openai.types.responses.response_output_text import AnnotationURLCitation
from openai.types.shared_params import Reasoning as OpenAIThinking

from app.config import settings

__all__ = ["AnnotationURLCitation", "OpenAIAdapterClass", "OpenAIThinking"]


class _Types:
    """Group private OpenAI adapter model types."""

    type AvailableModels = Literal["5.4", "5.4 Chat", "5.4 Mini", "Luna"]
    model_map = MappingProxyType[AvailableModels, str](
        {
            "5.4": "gpt-5.4",
            "5.4 Chat": "gpt-5.4-chat-latest",
            "5.4 Mini": "gpt-5.4-mini",
            "Luna": "gpt-5.6-luna",
        }
    )


class OpenAIAdapterClass:
    @staticmethod
    def build_text_input(
        messages: Iterable[tuple[Literal["user", "assistant"], str]],
        prompt: str,
    ) -> ResponseInputParam:
        """Build provider-native Responses input from normalized persisted text."""
        result: list[EasyInputMessageParam] = [
            {"type": "message", "role": role, "content": content} for role, content in messages
        ]
        result.append({"type": "message", "role": "user", "content": prompt})
        return cast("ResponseInputParam", result)

    class Tools:
        """Server-side tool builders for the Responses API."""

        @staticmethod
        def web_search(
            *,
            filters: web_search_tool_param.Filters | None | Omit = omit,
            search_context_size: Literal["low", "medium", "high"] | Omit = omit,
            user_location: web_search_tool_param.UserLocation | None | Omit = omit,
        ) -> WebSearchToolParam:
            """Web search built-in tool."""
            tool: WebSearchToolParam = {"type": "web_search"}

            if not isinstance(filters, Omit):
                tool["filters"] = filters
            if not isinstance(search_context_size, Omit):
                tool["search_context_size"] = search_context_size
            if not isinstance(user_location, Omit):
                tool["user_location"] = user_location

            return tool

        @staticmethod
        def code_interpreter(
            *,
            container: tool_param.CodeInterpreterContainer | Omit = omit,
            file_ids: list[str] | Omit = omit,
            memory_limit: Literal["1g", "4g", "16g", "64g"] | None | Omit = omit,
            network_policy: tool_param.CodeInterpreterContainerCodeInterpreterToolAutoNetworkPolicy
            | Omit = omit,
        ) -> tool_param.CodeInterpreter:
            """Code interpreter built-in tool (auto-creates a container by default)."""
            if isinstance(container, Omit):
                auto_container: tool_param.CodeInterpreterContainerCodeInterpreterToolAuto = {
                    "type": "auto"
                }
                if not isinstance(file_ids, Omit):
                    auto_container["file_ids"] = file_ids
                if not isinstance(memory_limit, Omit):
                    auto_container["memory_limit"] = memory_limit
                if not isinstance(network_policy, Omit):
                    auto_container["network_policy"] = network_policy
                container = auto_container

            return {"type": "code_interpreter", "container": container}

    def __init__(self) -> None:
        """Create an empty OpenAI async client holder."""
        self._client: AsyncOpenAI | None = None

    async def initialize(self) -> None:
        """Initialize the AsyncOpenAI client when an API key is configured."""
        if not settings.openai_api_key:
            return

        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def close(self) -> None:
        """Close the AsyncOpenAI client if it was initialized."""
        if self._client is not None:
            await self._client.close()

    async def stream_response(
        self,
        *,
        model: _Types.AvailableModels,
        input: str | ResponseInputParam,
        include: list[ResponseIncludable] | None | Omit = omit,
        instructions: str | None | Omit = omit,
        max_output_tokens: int | None | Omit = omit,
        max_tool_calls: int | None | Omit = omit,
        parallel_tool_calls: bool | None | Omit = omit,
        previous_response_id: str | None | Omit = omit,
        context_management: Iterable[response_create_params.ContextManagement] | Omit = omit,
        store: bool | None | Omit = omit,
        stream_options: response_create_params.StreamOptions | None | Omit = omit,
        temperature: float | None | Omit = omit,
        text: ResponseTextConfigParam | Omit = omit,
        tool_choice: response_create_params.ToolChoice | Omit = omit,
        tools: Iterable[ToolParam] | Omit = omit,
        reasoning: OpenAIThinking | None | Omit = omit,
    ) -> AsyncStream[ResponseStreamEvent]:
        """Create a streamed OpenAI Responses API request and return raw SDK events."""
        return await self.client.responses.create(
            model=_Types.model_map[model],
            input=input,
            include=include,
            instructions=instructions,
            max_output_tokens=max_output_tokens,
            max_tool_calls=max_tool_calls,
            parallel_tool_calls=parallel_tool_calls,
            previous_response_id=previous_response_id,
            context_management=context_management,
            store=store,
            stream=True,
            stream_options=stream_options,
            temperature=temperature,
            text=text,
            tool_choice=tool_choice,
            tools=tools,
            reasoning=reasoning,
        )

    @property
    def client(self) -> AsyncOpenAI:
        """Return the initialized AsyncOpenAI client."""
        if self._client is None:
            raise RuntimeError("OpenAI client not initialized or API key not configured")
        return self._client

from collections.abc import Iterable
from types import MappingProxyType
from typing import Literal

from openai import AsyncOpenAI, AsyncStream, Omit, omit
from openai.types.responses import (
    ResponseInputParam,
    ResponseStreamEvent,
    ResponseTextConfigParam,
    ToolParam,
    response_create_params,
)

from app.config import settings


class _Types:
    """Group private OpenAI adapter model types."""

    type AvailableModels = Literal["5.4", "5.4 Chat", "5.4 Mini"]
    model_map = MappingProxyType[AvailableModels, str](
        {
            "5.4": "gpt-5.4",
            "5.4 Chat": "gpt-5.4-chat-latest",
            "5.4 Mini": "gpt-5.4-mini",
        }
    )


class OpenAIAdapterClass:
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
        instructions: str | None | Omit = omit,
        max_output_tokens: int | None | Omit = omit,
        max_tool_calls: int | None | Omit = omit,
        parallel_tool_calls: bool | None | Omit = omit,
        previous_response_id: str | None | Omit = omit,
        stream_options: response_create_params.StreamOptions | None | Omit = omit,
        temperature: float | None | Omit = omit,
        text: ResponseTextConfigParam | Omit = omit,
        tool_choice: response_create_params.ToolChoice | Omit = omit,
        tools: Iterable[ToolParam] | Omit = omit,
    ) -> AsyncStream[ResponseStreamEvent]:
        """Create a streamed OpenAI Responses API request and return raw SDK events."""
        return await self.client.responses.create(
            model=_Types.model_map[model],
            input=input,
            instructions=instructions,
            max_output_tokens=max_output_tokens,
            max_tool_calls=max_tool_calls,
            parallel_tool_calls=parallel_tool_calls,
            previous_response_id=previous_response_id,
            stream=True,
            stream_options=stream_options,
            temperature=temperature,
            text=text,
            tool_choice=tool_choice,
            tools=tools,
        )

    @property
    def client(self) -> AsyncOpenAI:
        """Return the initialized AsyncOpenAI client."""
        if self._client is None:
            raise RuntimeError("OpenAI client not initialized or API key not configured")
        return self._client

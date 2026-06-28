from collections.abc import Iterable
from typing import Literal

from openai import AsyncOpenAI, AsyncStream, Omit, omit
from openai.types.responses import (
    FileSearchToolParam,
    ResponseInputParam,
    ResponseStreamEvent,
    ResponseTextConfigParam,
    ToolParam,
    ToolSearchToolParam,
    WebSearchPreviewToolParam,
    WebSearchToolParam,
    file_search_tool_param,
    response_create_params,
    tool_param,
    web_search_preview_tool_param,
    web_search_tool_param,
)
from openai.types.shared_params import ResponsesModel

from app.config import settings


class OpenAIAdapterClass:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None

    async def initialize(self) -> None:
        if not settings.openai_api_key:
            return

        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def stream_response(
        self,
        *,
        model: ResponsesModel,
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
        return await self.client.responses.create(
            model=model,
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

    @staticmethod
    def web_search_tool(
        *,
        filters: web_search_tool_param.Filters | None | Omit = omit,
        search_context_size: Literal["low", "medium", "high"] | Omit = omit,
        user_location: web_search_tool_param.UserLocation | None | Omit = omit,
    ) -> WebSearchToolParam:
        tool: WebSearchToolParam = {"type": "web_search"}

        if not isinstance(filters, Omit):
            tool["filters"] = filters
        if not isinstance(search_context_size, Omit):
            tool["search_context_size"] = search_context_size
        if not isinstance(user_location, Omit):
            tool["user_location"] = user_location

        return tool

    @staticmethod
    def web_search_preview_tool(
        *,
        search_content_types: list[Literal["text", "image"]] | Omit = omit,
        search_context_size: Literal["low", "medium", "high"] | Omit = omit,
        user_location: web_search_preview_tool_param.UserLocation | None | Omit = omit,
    ) -> WebSearchPreviewToolParam:
        tool: WebSearchPreviewToolParam = {"type": "web_search_preview"}

        if not isinstance(search_content_types, Omit):
            tool["search_content_types"] = search_content_types
        if not isinstance(search_context_size, Omit):
            tool["search_context_size"] = search_context_size
        if not isinstance(user_location, Omit):
            tool["user_location"] = user_location

        return tool

    @staticmethod
    def file_search_tool(
        *,
        vector_store_ids: list[str],
        filters: file_search_tool_param.Filters | None | Omit = omit,
        max_num_results: int | Omit = omit,
        ranking_options: file_search_tool_param.RankingOptions | Omit = omit,
    ) -> FileSearchToolParam:
        tool: FileSearchToolParam = {
            "type": "file_search",
            "vector_store_ids": vector_store_ids,
        }

        if not isinstance(filters, Omit):
            tool["filters"] = filters
        if not isinstance(max_num_results, Omit):
            tool["max_num_results"] = max_num_results
        if not isinstance(ranking_options, Omit):
            tool["ranking_options"] = ranking_options

        return tool

    @staticmethod
    def code_interpreter_tool(
        *,
        container: tool_param.CodeInterpreterContainer | Omit = omit,
        file_ids: list[str] | Omit = omit,
        memory_limit: Literal["1g", "4g", "16g", "64g"] | None | Omit = omit,
        network_policy: tool_param.CodeInterpreterContainerCodeInterpreterToolAutoNetworkPolicy
        | Omit = omit,
    ) -> tool_param.CodeInterpreter:
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

    @staticmethod
    def image_generation_tool(
        *,
        action: Literal["generate", "edit", "auto"] | Omit = omit,
        background: Literal["transparent", "opaque", "auto"] | Omit = omit,
        input_fidelity: Literal["high", "low"] | None | Omit = omit,
        input_image_mask: tool_param.ImageGenerationInputImageMask | Omit = omit,
        model: str | Omit = omit,
        moderation: Literal["auto", "low"] | Omit = omit,
        output_compression: int | Omit = omit,
        output_format: Literal["png", "webp", "jpeg"] | Omit = omit,
        partial_images: int | Omit = omit,
        quality: Literal["low", "medium", "high", "auto"] | Omit = omit,
        size: str | Omit = omit,
    ) -> tool_param.ImageGeneration:
        tool: tool_param.ImageGeneration = {"type": "image_generation"}

        if not isinstance(action, Omit):
            tool["action"] = action
        if not isinstance(background, Omit):
            tool["background"] = background
        if not isinstance(input_fidelity, Omit):
            tool["input_fidelity"] = input_fidelity
        if not isinstance(input_image_mask, Omit):
            tool["input_image_mask"] = input_image_mask
        if not isinstance(model, Omit):
            tool["model"] = model
        if not isinstance(moderation, Omit):
            tool["moderation"] = moderation
        if not isinstance(output_compression, Omit):
            tool["output_compression"] = output_compression
        if not isinstance(output_format, Omit):
            tool["output_format"] = output_format
        if not isinstance(partial_images, Omit):
            tool["partial_images"] = partial_images
        if not isinstance(quality, Omit):
            tool["quality"] = quality
        if not isinstance(size, Omit):
            tool["size"] = size

        return tool

    @staticmethod
    def local_shell_tool() -> tool_param.LocalShell:
        return {"type": "local_shell"}

    @staticmethod
    def tool_search_tool(
        *,
        description: str | None | Omit = omit,
        execution: Literal["server", "client"] | Omit = omit,
        parameters: object | None | Omit = omit,
    ) -> ToolSearchToolParam:
        tool: ToolSearchToolParam = {"type": "tool_search"}

        if not isinstance(description, Omit):
            tool["description"] = description
        if not isinstance(execution, Omit):
            tool["execution"] = execution
        if not isinstance(parameters, Omit):
            tool["parameters"] = parameters

        return tool

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            raise RuntimeError("OpenAI client not initialized or API key not configured")
        return self._client

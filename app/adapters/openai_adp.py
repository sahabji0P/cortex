import inspect
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Literal, cast

from openai import AsyncOpenAI, AsyncStream, Omit, omit, pydantic_function_tool
from openai.types.chat import ChatCompletionFunctionToolParam
from openai.types.responses import (
    EasyInputMessageParam,
    FunctionToolParam,
    ParsedResponse,
    ResponseIncludable,
    ResponseInputItemParam,
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
from openai.types.responses.response_input_param import FunctionCallOutput
from openai.types.responses.response_output_text import AnnotationURLCitation
from openai.types.responses.tool_param import ParseableToolParam
from openai.types.shared_params import Reasoning as OpenAIThinking
from pydantic import BaseModel

from app.config import settings

__all__ = [
    "AnnotationURLCitation",
    "FunctionCallOutput",
    "OpenAIAdapterClass",
    "OpenAIThinking",
    "OpenAIVia",
    "ResponseInputItemParam",
]

type ToolHandler = Callable[..., object | Awaitable[object]]

# How OpenAI is reached: straight, with the API key, or through a proxy such as
# CLIProxyAPI signed in with a ChatGPT account.
type OpenAIVia = Literal["api", "proxy"]


class _Types:
    """Group private OpenAI adapter model types."""

    type AvailableModels = Literal[
        "5.4", "5.4 Chat", "5.4 Mini", "Luna", "6 Luna", "6.1 Terra", "6.1 Sol"
    ]
    model_map = MappingProxyType[AvailableModels, str](
        {
            "5.4": "gpt-5.4",
            "5.4 Chat": "gpt-5.4-chat-latest",
            "5.4 Mini": "gpt-5.4-mini",
            "Luna": "gpt-5.6-luna",
            "6 Luna": "gpt-6-luna",
            "6.1 Terra": "gpt-6.1-terra",
            "6.1 Sol": "gpt-6.1-sol",
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
        """Tool builders for the Responses API."""

        @staticmethod
        def function(
            *,
            name: str,
            description: str,
            parameters: dict[str, object],
            strict: bool = True,
        ) -> FunctionToolParam:
            """Build a function tool the caller runs itself, strict unless told otherwise.

            The model's call arrives as a `function_call` output item; send the result
            back as a `function_call_output` input item with the same `call_id`.

            A strict tool's arguments always fit its schema, which must then keep to
            the subset the API allows. A schema written elsewhere, such as a tool
            server's, may not: sent with `strict` off, the arguments are the model's
            best effort.
            """
            return {
                "type": "function",
                "name": name,
                "description": description,
                "parameters": parameters,
                "strict": strict,
            }

        @staticmethod
        def pydantic_function(
            arguments_model: type[BaseModel],
            *,
            name: str | None = None,
            description: str | None = None,
        ) -> ChatCompletionFunctionToolParam:
            """Build a strict function tool whose arguments parse into a Pydantic model."""
            return pydantic_function_tool(
                arguments_model,
                name=name,
                description=description,
            )

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
        # One client for each way to OpenAI that is set up, and the one in use.
        self._clients: dict[OpenAIVia, AsyncOpenAI] = {}
        self._client: AsyncOpenAI | None = None
        self._via: OpenAIVia = "api"

    async def initialize(self) -> None:
        """Make a client for each way that is set up: the API key, the proxy, or both.

        The API key is used when it is set up, until `use` picks the other way: the
        proxy is the user's to choose, and the only way when it alone is set up.
        """
        if settings.openai_api_key:
            self._clients["api"] = AsyncOpenAI(api_key=settings.openai_api_key)
        if settings.openai_proxy_url:
            self._clients["proxy"] = AsyncOpenAI(
                base_url=settings.openai_proxy_url, api_key=settings.openai_proxy_key
            )
        if self._clients:
            self.use("api" if "api" in self._clients else "proxy")

    def use(self, via: OpenAIVia) -> None:
        """Send the next requests the given way. Requests already running are left alone."""
        if via not in self._clients:
            raise ValueError(f"OpenAI through {via!r} is not set up")
        self._client = self._clients[via]
        self._via = via

    @property
    def via(self) -> OpenAIVia:
        """The way in use. With nothing set up, "api"."""
        return self._via

    def is_set_up(self, via: OpenAIVia) -> bool:
        """Whether that way has a client: its key or its address is in the settings."""
        return via in self._clients

    async def close(self) -> None:
        """Close every client that was made."""
        for client in self._clients.values():
            await client.close()

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
    ) -> AsyncGenerator[ResponseStreamEvent]:
        """Create a streamed OpenAI Responses API request and return its raw SDK events.

        To stop the request early, close what is returned (`contextlib.aclosing`). That
        closes the connection there and then. A stream that is only dropped stays open,
        with the model still writing, until it is garbage collected.
        """
        stream = await self.client.responses.create(
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
        return self._events(stream)

    @staticmethod
    async def _events(
        stream: AsyncStream[ResponseStreamEvent],
    ) -> AsyncGenerator[ResponseStreamEvent]:
        async with stream:
            async for event in stream:
                yield event

    async def text_response(
        self,
        *,
        model: _Types.AvailableModels,
        input: str | ResponseInputParam,
        instructions: str | None | Omit = omit,
        max_output_tokens: int | None | Omit = omit,
        reasoning: OpenAIThinking | None | Omit = omit,
    ) -> str:
        """Create one stateless, non-streamed Responses API request and return its text."""
        response = await self.client.responses.create(
            model=_Types.model_map[model],
            input=input,
            instructions=instructions,
            max_output_tokens=max_output_tokens,
            reasoning=reasoning,
            store=False,
        )
        return response.output_text

    async def structured_response[OutputModelT: BaseModel](
        self,
        *,
        model: _Types.AvailableModels,
        input: str | ResponseInputParam,
        output_model: type[OutputModelT],
        tools: Iterable[ParseableToolParam] = (),
        tool_handlers: Mapping[str, ToolHandler] | None = None,
        instructions: str | None | Omit = omit,
        initial_tool_choice: response_create_params.ToolChoice | Omit = omit,
        parallel_tool_calls: bool | None | Omit = omit,
        reasoning: OpenAIThinking | None | Omit = omit,
        max_tool_rounds: int = 8,
    ) -> ParsedResponse[OutputModelT]:
        """Run custom tools until the model returns the requested Pydantic output.

        Built-in tools execute on OpenAI's servers. Custom function calls are dispatched
        through ``tool_handlers`` and their outputs are sent back to the model. The entire
        exchange is stateless (``store=False``), so every model output item—including any
        encrypted reasoning item—is preserved in the next request.

        ``initial_tool_choice`` applies only to the first request. This lets an experiment
        force a tool call once and then return to automatic selection for the final answer.
        """
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be at least 1")

        input_items: list[ResponseInputItemParam]
        if isinstance(input, str):
            input_items = [{"role": "user", "content": input}]
        else:
            input_items = list(input)

        selected_tools = tuple(tools)
        handlers = tool_handlers or {}

        for round_index in range(max_tool_rounds + 1):
            response = await self.client.responses.parse(
                model=_Types.model_map[model],
                input=cast("ResponseInputParam", input_items),
                instructions=instructions,
                tools=selected_tools,
                text_format=output_model,
                tool_choice=initial_tool_choice if round_index == 0 else omit,
                parallel_tool_calls=parallel_tool_calls,
                include=["reasoning.encrypted_content"],
                reasoning=reasoning,
                store=False,
            )

            function_calls = [item for item in response.output if item.type == "function_call"]
            if not function_calls:
                if response.output_parsed is not None:
                    return response
                raise RuntimeError(
                    "OpenAI completed without a function call or parsed structured output "
                    f"(status={response.status})"
                )

            if round_index == max_tool_rounds:
                raise RuntimeError(f"OpenAI exceeded {max_tool_rounds} custom tool rounds")

            # Reasoning-model tool loops must replay every output item, not just the call.
            for item in response.output:
                input_item = item.to_dict()
                # responses.parse() adds this SDK-only convenience field. It is not part
                # of the API input schema and must not be replayed to /v1/responses.
                input_item.pop("parsed_arguments", None)
                input_items.append(cast("ResponseInputItemParam", input_item))

            for call in function_calls:
                handler = handlers.get(call.name)
                if handler is None:
                    raise KeyError(f"No tool handler registered for {call.name!r}")

                arguments = call.parsed_arguments
                if arguments is None:
                    arguments = json.loads(call.arguments)

                try:
                    tool_result = handler(arguments)
                    if inspect.isawaitable(tool_result):
                        tool_result = await cast("Awaitable[object]", tool_result)
                except Exception as exc:
                    raise RuntimeError(f"Tool {call.name!r} failed") from exc

                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": self._serialize_tool_output(tool_result),
                    }
                )

        raise AssertionError("unreachable")

    @staticmethod
    def _serialize_tool_output(value: object) -> str:
        """Serialize local tool results into the Responses function-output contract."""
        if isinstance(value, str):
            return value
        if isinstance(value, BaseModel):
            return value.model_dump_json()
        return json.dumps(value, default=str)

    @property
    def configured(self) -> bool:
        """Whether an API key was provided."""
        return self._client is not None

    @property
    def client(self) -> AsyncOpenAI:
        """Return the initialized AsyncOpenAI client."""
        if self._client is None:
            raise RuntimeError("OpenAI client not initialized or API key not configured")
        return self._client

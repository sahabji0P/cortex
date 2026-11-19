import inspect
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Literal, cast

from anthropic import AsyncAnthropic, Omit, omit, transform_schema
from anthropic.lib.streaming import AsyncMessageStreamManager
from anthropic.types import (
    CacheControlEphemeralParam,
    CodeExecutionTool20260521Param,
    Message,
    MessageParam,
    MessageTokensCount,
    OutputConfigParam,
    ParsedMessage,
    TextBlockParam,
    ThinkingConfigParam,
    ToolChoiceParam,
    ToolParam,
    ToolReferenceBlockParam,
    ToolResultBlockParam,
    ToolUnionParam,
    ToolUseBlock,
    WebFetchTool20260209Param,
    WebSearchTool20260209Param,
)
from anthropic.types.message_count_tokens_tool_param import MessageCountTokensToolParam
from anthropic.types.user_location_param import UserLocationParam
from pydantic import BaseModel, ValidationError

from app.config import settings

# Re-exported so callers can type a conversation without importing the SDK themselves.
__all__ = [
    "AnthropicAdapterClass",
    "AnthropicRefusalError",
    "MessageParam",
    "Omit",
    "OutputConfigParam",
    "ThinkingConfigParam",
    "ToolResultBlockParam",
    "ToolUnionParam",
    "omit",
]

type ToolHandler = Callable[..., object | Awaitable[object]]

# A request that is not streamed has to finish inside the SDK's HTTP timeout, so its
# reply is kept shorter. A streamed one has no such limit.
_MAX_TOKENS = 16_000
_MAX_TOKENS_STREAMED = 64_000


class AnthropicRefusalError(RuntimeError):
    """The model declined the request (`stop_reason: "refusal"`).

    `category` is the safety classifier's reason when the API gives one.
    """

    def __init__(self, message: Message) -> None:
        details = message.stop_details
        self.category = details.category if details else None
        self.explanation = details.explanation if details else None
        super().__init__(
            f"Claude declined the request (category={self.category}): {self.explanation}"
        )


class _Types:
    """Group private Anthropic adapter model types."""

    type AvailableModels = Literal["Fable 5.2", "Opus 5.5", "Sonnet 5.5", "Haiku 5.5"]
    model_map = MappingProxyType[AvailableModels, str](
        {
            "Fable 5.2": "claude-fable-5-2",
            "Opus 5.5": "claude-opus-5-5",
            "Sonnet 5.5": "claude-sonnet-5-5",
            "Haiku 5.5": "claude-haiku-5-5",
        }
    )


class AnthropicAdapterClass:
    """The Claude Messages API. Every request is stateless: the caller sends the history.

    Three things differ from the OpenAI adapter and are easy to trip over:

    - `max_tokens` is required by the API, so every method has a default for it.
    - Thinking is on by default on every model here and cannot always be turned off:
      `{"type": "disabled"}` is rejected by Fable 5.2, Opus 5.5 and Sonnet 5.5. Lower
      `output_config={"effort": ...}` instead; on Sonnet 5.5 `{"type": "between_tools"}`
      is the nearest thing to off. Haiku 5.5 accepts `disabled`.
    - Nothing is cached unless the request asks for it with `cache_control`.
    """

    @staticmethod
    def build_text_input(
        messages: Iterable[tuple[Literal["user", "assistant"], str]],
        prompt: str,
    ) -> list[MessageParam]:
        """Build Messages API input from normalized persisted text."""
        result: list[dict[str, str]] = [
            {"role": role, "content": content} for role, content in messages
        ]
        result.append({"role": "user", "content": prompt})
        return cast("list[MessageParam]", result)

    class Tools:
        """Tool builders for the Messages API."""

        @staticmethod
        def function(
            *,
            name: str,
            description: str,
            parameters: dict[str, object],
            eager_input_streaming: bool = False,
            strict: bool = True,
            defer_loading: bool = False,
        ) -> ToolParam:
            """Build a function tool the caller runs itself, strict unless told otherwise.

            The model's call arrives as a `tool_use` content block; send the result back
            as a `tool_result` block with the same `tool_use_id`, in the user message
            that directly follows. A strict tool's `parameters` must set
            `additionalProperties: false` and keep to the subset the API allows. A schema
            written elsewhere, such as a tool server's, may not: sent with `strict` off,
            the input is the model's best effort.

            With `eager_input_streaming` the call's input streams as it is written
            instead of arriving whole. The API then no longer checks it against the
            schema, so the caller has to.

            A tool with `defer_loading` is not shown to the model until a search loads
            it: a `tool_result` holding a `reference` to it. It stays in `tools` on
            every request, and is left out of the start of the prompt, so the prompt
            cache holds. It may not carry `cache_control`.
            """
            tool: ToolParam = {
                "name": name,
                "description": description,
                "input_schema": parameters,
            }
            if strict:
                tool["strict"] = True
            if eager_input_streaming:
                tool["eager_input_streaming"] = True
            if defer_loading:
                tool["defer_loading"] = True
            return tool

        @staticmethod
        def reference(name: str) -> ToolReferenceBlockParam:
            """Loads the tool called `name`, as a block of a search's `tool_result`.

            The tool must be in the request's `tools`, or the request is refused.
            """
            return {"type": "tool_reference", "tool_name": name}

        @staticmethod
        def pydantic_function(
            arguments_model: type[BaseModel],
            *,
            name: str,
            description: str | None = None,
        ) -> ToolParam:
            """Build a strict function tool whose input schema comes from a Pydantic model.

            Pass the same model to `structured_response(argument_models=...)` to have the
            handler receive a validated instance instead of a dict.
            """
            return {
                "name": name,
                "description": description or arguments_model.__doc__ or "",
                "input_schema": transform_schema(arguments_model),
                "strict": True,
            }

        @staticmethod
        def web_search(
            *,
            max_uses: int | Omit = omit,
            allowed_domains: list[str] | Omit = omit,
            blocked_domains: list[str] | Omit = omit,
            user_location: UserLocationParam | Omit = omit,
        ) -> WebSearchTool20260209Param:
            """Web search, run on Anthropic's servers. Give allowed or blocked domains, not both.

            This version filters results with code execution of its own, so do not also
            offer `code_execution` in the same request.
            """
            tool: WebSearchTool20260209Param = {"type": "web_search_20260209", "name": "web_search"}
            if not isinstance(max_uses, Omit):
                tool["max_uses"] = max_uses
            if not isinstance(allowed_domains, Omit):
                tool["allowed_domains"] = allowed_domains
            if not isinstance(blocked_domains, Omit):
                tool["blocked_domains"] = blocked_domains
            if not isinstance(user_location, Omit):
                tool["user_location"] = user_location
            return tool

        @staticmethod
        def web_fetch(
            *,
            max_uses: int | Omit = omit,
            allowed_domains: list[str] | Omit = omit,
            blocked_domains: list[str] | Omit = omit,
            max_content_tokens: int | Omit = omit,
            citations: bool | Omit = omit,
        ) -> WebFetchTool20260209Param:
            """Web fetch, run on Anthropic's servers. It only fetches URLs already in the conversation."""  # noqa: E501
            tool: WebFetchTool20260209Param = {"type": "web_fetch_20260209", "name": "web_fetch"}
            if not isinstance(max_uses, Omit):
                tool["max_uses"] = max_uses
            if not isinstance(allowed_domains, Omit):
                tool["allowed_domains"] = allowed_domains
            if not isinstance(blocked_domains, Omit):
                tool["blocked_domains"] = blocked_domains
            if not isinstance(max_content_tokens, Omit):
                tool["max_content_tokens"] = max_content_tokens
            if not isinstance(citations, Omit):
                tool["citations"] = {"enabled": citations}
            return tool

        @staticmethod
        def code_execution() -> CodeExecutionTool20260521Param:
            """Code execution in a sandbox on Anthropic's servers."""
            return {"type": "code_execution_20260521", "name": "code_execution"}

    def __init__(self) -> None:
        """Create an empty Anthropic async client holder."""
        self._client: AsyncAnthropic | None = None

    async def initialize(self) -> None:
        """Initialize the AsyncAnthropic client when an API key is configured."""
        if not settings.anthropic_api_key:
            return

        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def close(self) -> None:
        """Close the AsyncAnthropic client if it was initialized."""
        if self._client is not None:
            await self._client.close()

    def stream_message(
        self,
        *,
        model: _Types.AvailableModels,
        messages: Iterable[MessageParam],
        system: str | Iterable[TextBlockParam] | Omit = omit,
        max_tokens: int = _MAX_TOKENS_STREAMED,
        thinking: ThinkingConfigParam | Omit = omit,
        output_config: OutputConfigParam | Omit = omit,
        tools: Iterable[ToolUnionParam] | Omit = omit,
        tool_choice: ToolChoiceParam | Omit = omit,
        cache_control: CacheControlEphemeralParam | None | Omit = omit,
        stop_sequences: list[str] | Omit = omit,
        speed: Literal["standard", "fast"] | None | Omit = omit,
    ) -> AsyncMessageStreamManager[None]:
        """Open a streamed Messages API request. Nothing is sent until it is entered.

        Use it as `async with adapter.stream_message(...) as stream:`. Iterating `stream`
        gives the SDK's events (`text` for reply text, `input_json` for tool input,
        `content_block_stop` for each finished block) and `await stream.get_final_message()`
        gives the whole message afterwards. Leaving the block closes the connection, so
        cancelling the task that is reading it stops the model call.

        Check the final message's `stop_reason` before using its content: `refusal`
        and `max_tokens` both arrive as an ordinary, successful response.

        An empty `tools` is sent as no tools at all, and `tool_choice` is then left out
        with it: the API takes a tool choice only together with tools.
        """
        offered = omit if isinstance(tools, Omit) else list(tools)
        if not offered:
            offered = tool_choice = omit
        return self.client.messages.stream(
            model=_Types.model_map[model],
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            thinking=thinking,
            output_config=output_config,
            tools=offered,
            tool_choice=tool_choice,
            cache_control=cache_control,
            stop_sequences=stop_sequences,
            speed=speed,
        )

    async def text_response(
        self,
        *,
        model: _Types.AvailableModels,
        messages: str | Iterable[MessageParam],
        system: str | Iterable[TextBlockParam] | Omit = omit,
        max_tokens: int = _MAX_TOKENS,
        thinking: ThinkingConfigParam | Omit = omit,
        output_config: OutputConfigParam | Omit = omit,
    ) -> str:
        """Create one non-streamed Messages API request and return its text.

        Thinking is paid for out of `max_tokens`, so a small limit on a model that
        thinks can leave no room for the answer. That, and any other reply cut off at
        the limit, is returned as it stands; a refusal raises `AnthropicRefusalError`.
        """
        response = await self.client.messages.create(
            model=_Types.model_map[model],
            messages=self._as_messages(messages),
            system=system,
            max_tokens=max_tokens,
            thinking=thinking,
            output_config=output_config,
        )
        if response.stop_reason == "refusal":
            raise AnthropicRefusalError(response)
        return "".join(block.text for block in response.content if block.type == "text")

    async def structured_response[OutputModelT: BaseModel](
        self,
        *,
        model: _Types.AvailableModels,
        messages: str | Iterable[MessageParam],
        output_model: type[OutputModelT],
        tools: Iterable[ToolUnionParam] = (),
        tool_handlers: Mapping[str, ToolHandler] | None = None,
        argument_models: Mapping[str, type[BaseModel]] | None = None,
        system: str | Iterable[TextBlockParam] | Omit = omit,
        max_tokens: int = _MAX_TOKENS,
        thinking: ThinkingConfigParam | Omit = omit,
        output_config: OutputConfigParam | Omit = omit,
        initial_tool_choice: ToolChoiceParam | Omit = omit,
        max_tool_rounds: int = 8,
    ) -> ParsedMessage[OutputModelT]:
        """Run custom tools until the model returns the requested Pydantic output.

        Server tools run on Anthropic's servers. Custom `tool_use` calls are dispatched
        through `tool_handlers`, and all of one round's results go back in a single
        user message. A handler gets the call's input as a dict, or as an instance of
        the model registered for that tool in `argument_models`. Each round's whole
        reply is sent back unchanged, thinking blocks included: the API rejects a
        history in which they have been dropped or edited.

        `initial_tool_choice` applies only to the first request. Forcing a call there
        (`any` or `tool`) is rejected by Fable 5.2, Opus 5.5 and Sonnet 5.5; name the
        tool in the prompt instead.

        The answer is `response.parsed_output`. Raises `AnthropicRefusalError` if the
        model declines, and `RuntimeError` if it runs out of `max_tokens` or of rounds.
        """
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be at least 1")

        history = self._as_messages(messages)
        selected_tools = tuple(tools)
        handlers = tool_handlers or {}
        models = argument_models or {}

        for round_index in range(max_tool_rounds + 1):
            response = await self.client.messages.parse(
                model=_Types.model_map[model],
                messages=history,
                system=system,
                max_tokens=max_tokens,
                thinking=thinking,
                output_config=output_config,
                output_format=output_model,
                tools=selected_tools or omit,
                tool_choice=initial_tool_choice if round_index == 0 else omit,
            )

            if response.stop_reason == "refusal":
                raise AnthropicRefusalError(response)
            if response.stop_reason == "max_tokens":
                # A tool input cut off here still parses, as a partial object: never run it.
                raise RuntimeError(f"Claude ran out of max_tokens ({max_tokens}) before finishing")

            calls = [block for block in response.content if block.type == "tool_use"]
            paused = response.stop_reason == "pause_turn"
            if not calls and not paused:
                if response.parsed_output is not None:
                    return response
                raise RuntimeError(
                    "Claude finished without a tool call or parsed structured output "
                    f"(stop_reason={response.stop_reason})"
                )

            if round_index == max_tool_rounds:
                raise RuntimeError(f"Claude exceeded {max_tool_rounds} tool rounds")

            history.append(self._replayable(response))
            # A server tool that paused has nothing to answer: sending the turn back
            # as it is makes the server carry on with it.
            if calls:
                results = [await self._run_tool(call, handlers, models) for call in calls]
                history.append({"role": "user", "content": results})

        raise AssertionError("unreachable")

    async def count_tokens(
        self,
        *,
        model: _Types.AvailableModels,
        messages: str | Iterable[MessageParam],
        system: str | Iterable[TextBlockParam] | Omit = omit,
        tools: Iterable[MessageCountTokensToolParam] | Omit = omit,
        thinking: ThinkingConfigParam | Omit = omit,
    ) -> MessageTokensCount:
        """Count the input tokens a request would use, without running it."""
        return await self.client.messages.count_tokens(
            model=_Types.model_map[model],
            messages=self._as_messages(messages),
            system=system,
            tools=tools,
            thinking=thinking,
        )

    @staticmethod
    def _as_messages(messages: str | Iterable[MessageParam]) -> list[MessageParam]:
        """Turn a bare prompt into a one-message conversation, and copy any other."""
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        return list(messages)

    @staticmethod
    def _replayable[OutputModelT: BaseModel](response: ParsedMessage[OutputModelT]) -> MessageParam:
        """The reply as the assistant message the API accepts back as input."""
        blocks: list[dict[str, object]] = []
        for block in response.content:
            item = block.to_dict()
            # messages.parse() adds this SDK-only convenience field to text blocks. It
            # is not part of the API input schema.
            item.pop("parsed_output", None)
            blocks.append(item)
        return cast("MessageParam", {"role": "assistant", "content": blocks})

    async def _run_tool(
        self,
        call: ToolUseBlock,
        handlers: Mapping[str, ToolHandler],
        models: Mapping[str, type[BaseModel]],
    ) -> ToolResultBlockParam:
        """Run one custom tool call and build the `tool_result` block that answers it."""
        handler = handlers.get(call.name)
        if handler is None:
            raise KeyError(f"No tool handler registered for {call.name!r}")

        arguments: object = call.input
        if (arguments_model := models.get(call.name)) is not None:
            try:
                arguments = arguments_model.model_validate(call.input)
            except ValidationError as exc:
                # The model wrote the input, so the model is told, and can try again.
                return {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": f"Invalid input for {call.name}: {exc}",
                    "is_error": True,
                }

        try:
            tool_result = handler(arguments)
            if inspect.isawaitable(tool_result):
                tool_result = await cast("Awaitable[object]", tool_result)
        except Exception as exc:
            raise RuntimeError(f"Tool {call.name!r} failed") from exc

        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": self._serialize_tool_output(tool_result),
        }

    @staticmethod
    def _serialize_tool_output(value: object) -> str:
        """Serialize local tool results into the text a `tool_result` block carries."""
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
    def client(self) -> AsyncAnthropic:
        """Return the initialized AsyncAnthropic client."""
        if self._client is None:
            raise RuntimeError("Anthropic client not initialized or API key not configured")
        return self._client

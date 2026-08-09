from collections.abc import AsyncIterator
from typing import Literal

from bson import ObjectId
from openai import omit
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.anthropic_adp import AnthropicAdapterClass, AnthropicModel
from app.adapters.gemini_adp import (
    CodeExecutionCallDelta,
    CodeExecutionResultDelta,
    GeminiAdapterClass,
    GeminiBuiltinTool,
    TextAnnotationDelta,
    TextDelta,
    URLCitation,
)
from app.adapters.openai_adp import AnnotationURLCitation, OpenAIAdapterClass, OpenAIThinking
from app.entities.conversation_ent import ConversationEntityClass, SessionContext

type Provider = Literal["openai", "gemini", "anthropic"]
type ChunkType = Literal["delta", "info", "source", "image", "done", "error"]
type GeminiMode = Literal["generate_content", "interactions"]

_DEFAULT_GEMINI_TOOLS: tuple[GeminiBuiltinTool, ...] = (
    "google_search",
    "google_maps",
    "url_context",
)


class Source(BaseModel):
    """A cited source from a provider's grounding metadata."""

    url: str
    title: str | None = None


class ImageOutput(BaseModel):
    """An image produced by a server-side tool (e.g. a code interpreter chart)."""

    url: str


class StreamChunk(BaseModel):
    """One normalized event in the unified stream, regardless of source provider.

    - `delta`  : incremental output text (in `text`)
    - `info`   : a server-side/built-in tool event (label in `info`, e.g.
                 "google_search", "response.web_search_call.searching")
    - `source` : cited sources from provider grounding (in `sources`)
                 and optional Maps widget context
    - `image`  : images produced by a server-side tool (in `images`)
    - `done`   : the provider finished
    - `error`  : the provider failed (message in `text`)
    """

    provider: Provider
    type: ChunkType
    text: str = ""
    info: str | None = None
    tool_name: str | None = Field(default=None, serialization_alias="toolName")
    tool_input: str | None = Field(default=None, serialization_alias="toolInput")
    tool_output: str | None = Field(default=None, serialization_alias="toolOutput")
    tool_language: str | None = Field(default=None, serialization_alias="toolLanguage")
    is_error: bool | None = Field(default=None, serialization_alias="isError")
    sources: list[Source] = Field(default_factory=list)
    images: list[ImageOutput] = Field(default_factory=list)
    maps_widget_context_token: str | None = Field(
        default=None,
        serialization_alias="mapsWidgetContextToken",
    )
    provider_response_id: str | None = Field(default=None, exclude=True)

    model_config = ConfigDict(serialize_by_alias=True)


class LLMPipelineEntityClass:
    """Routes a prompt to OpenAI, Gemini, and/or Anthropic and yields a unified stream.

    Providers run in the order requested (sequentially, not concurrently). Built-in
    server-side tools are enabled for each provider; text arrives as `delta` chunks and
    tool/search/code activity as `info` chunks.
    """

    def __init__(
        self,
        openai: OpenAIAdapterClass,
        gemini: GeminiAdapterClass,
        anthropic: AnthropicAdapterClass,
        conversations: ConversationEntityClass,
    ) -> None:
        """Initialize the pipeline with provider adapters."""
        self._openai = openai
        self._gemini = gemini
        self._anthropic = anthropic
        self._conversations = conversations

    async def run(
        self,
        prompt: str,
        providers: list[Provider],
        *,
        gemini_mode: GeminiMode = "generate_content",
        gemini_tools: list[GeminiBuiltinTool] | None = None,
        anthropic_model: AnthropicModel = "Sonnet 5",
        openai_thinking: OpenAIThinking | None = None,
        session_id: str,
    ) -> AsyncIterator[StreamChunk]:
        """Run the prompt through the requested providers in order."""
        async with self._conversations.session_run_lock(session_id):
            selected_gemini_tools = gemini_tools or list(_DEFAULT_GEMINI_TOOLS)
            turns = await self._conversations.get_session_turns(session_id)
            contexts = {
                provider: self._conversations.build_context(turns, provider)
                for provider in providers
            }
            request_id = ObjectId()
            for provider in providers:
                context = contexts[provider]
                if provider == "gemini":
                    if gemini_mode == "interactions":
                        stream = self._gemini_stream_via_interactions(
                            prompt,
                            selected_gemini_tools,
                            context,
                        )
                    else:
                        stream = self._gemini_stream(prompt, selected_gemini_tools, context)
                elif provider == "anthropic":
                    stream = self._anthropic_stream(prompt, anthropic_model, context)
                else:
                    stream = self._openai_stream(prompt, openai_thinking, context)

                async for chunk in self._persist_stream(
                    stream,
                    provider=provider,
                    session_id=session_id,
                    request_id=request_id,
                    prompt=prompt,
                ):
                    yield chunk

    async def _persist_stream(
        self,
        stream: AsyncIterator[StreamChunk],
        *,
        provider: Provider,
        session_id: str,
        request_id: ObjectId,
        prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        """Persist one compact, ordered parts document for a provider run."""
        turn = await self._conversations.create_turn(
            provider=provider,
            session_id=session_id,
            request_id=request_id,
            prompt=prompt,
        )
        parts = turn.parts.copy()
        try:
            async for chunk in stream:
                self._capture_part(parts, chunk)
                yield chunk
        finally:
            await self._conversations.save_turn_parts_shielded(turn.id, parts)

    @staticmethod
    def _capture_part(parts: list[dict[str, str]], chunk: StreamChunk) -> None:
        """Convert a normalized stream chunk into flat, provider-agnostic parts."""
        part = LLMPipelineEntityClass._flat_chunk_part(chunk)
        if chunk.type == "delta":
            # An empty delta dumps without a "text" key (exclude_defaults) — force it
            # so accumulation on the next delta never KeyErrors.
            if parts and parts[-1].get("type") == "text":
                parts[-1]["text"] = parts[-1].get("text", "") + chunk.text
            else:
                part["type"] = "text"
                part.setdefault("text", "")
                parts.append(part)
            return

        if chunk.type == "source":
            for source in chunk.sources:
                source_part = {**part, "url": source.url}
                if source.title is not None:
                    source_part["title"] = source.title
                parts.append(source_part)
            if not chunk.sources and chunk.maps_widget_context_token is not None:
                parts.append(part)
            return

        if chunk.type == "image":
            parts.extend({**part, "url": image.url} for image in chunk.images)
            return

        parts.append(part)

    @staticmethod
    def _text_messages(context: SessionContext) -> list[tuple[Literal["user", "assistant"], str]]:
        """Unpack persisted context into the (role, text) pairs adapters build inputs from."""
        return [(message.role, message.content) for message in context.messages]

    @staticmethod
    def _flat_chunk_part(chunk: StreamChunk) -> dict[str, str]:
        """Keep every scalar chunk field without hard-coding provider-specific keys."""
        data = chunk.model_dump(
            mode="json",
            by_alias=True,
            exclude={"provider", "sources", "images"},
            exclude_none=True,
            exclude_defaults=True,
        )
        return {
            key: ("true" if value else "false") if isinstance(value, bool) else str(value)
            for key, value in data.items()
        }

    async def _gemini_stream(
        self,
        prompt: str,
        gemini_tools: list[GeminiBuiltinTool],
        context: SessionContext,
    ) -> AsyncIterator[StreamChunk]:
        """Convert a Gemini generateContent stream into normalized stream chunks.

        This is the active Gemini path for Cortex listen. Gemini grounding gives
        `delta` text plus `info` and `source` chunks.
        """
        try:
            stream = self._gemini.stream_generate_content(
                self._gemini.build_generate_content_input(
                    self._text_messages(context),
                    prompt,
                ),
                model="3.5 Flash",
                tools=self._gemini.Tools.generate_content_from_names(gemini_tools),
                # Live mode wants the fastest first token; "minimal" is the lowest
                # thinking level Gemini 3.x supports (no full off switch).
                thinking_level="minimal",
            )
            async for chunk in stream:
                text = self._gemini.extract_text(chunk)
                if text:
                    yield StreamChunk(provider="gemini", type="delta", text=text)
                for event in self._gemini.extract_tool_events(chunk):
                    yield StreamChunk(provider="gemini", type="info", info=event)
                sources = [
                    Source(url=url, title=title)
                    for url, title in self._gemini.extract_grounding_sources(chunk)
                ]
                maps_widget_context_token = self._gemini.extract_maps_widget_context_token(chunk)
                if sources or maps_widget_context_token:
                    yield StreamChunk(
                        provider="gemini",
                        type="source",
                        sources=sources,
                        maps_widget_context_token=maps_widget_context_token,
                    )
        except Exception as exc:  # surface provider errors as a stream event, don't crash the run
            yield StreamChunk(provider="gemini", type="error", text=str(exc))
            return
        yield StreamChunk(provider="gemini", type="done")

    async def _gemini_stream_via_interactions(
        self,
        prompt: str,
        gemini_tools: list[GeminiBuiltinTool],
        context: SessionContext,
    ) -> AsyncIterator[StreamChunk]:
        """Convert a Gemini Interactions SSE stream into normalized stream chunks.

        Used for explicit Interactions experiments such as code execution tools.
        Stateless: the full transcript is rebuilt from Mongo on every request, so
        no interaction is stored or continued provider-side.
        """
        try:
            stream = await self._gemini.stream_model_interaction_events(
                self._gemini.build_interaction_input(
                    self._text_messages(context),
                    prompt,
                ),
                model="3.5 Flash",
                tools=self._gemini.Tools.from_names(gemini_tools),
                store=False,
            )
            async for event in stream:
                if event.event_type == "step.delta":
                    delta = event.delta
                    if isinstance(delta, TextDelta):
                        yield StreamChunk(provider="gemini", type="delta", text=delta.text)
                    elif isinstance(delta, TextAnnotationDelta):
                        sources = [
                            Source(url=ann.url, title=ann.title)
                            for ann in delta.annotations or []
                            if isinstance(ann, URLCitation) and ann.url
                        ]
                        if sources:
                            yield StreamChunk(provider="gemini", type="source", sources=sources)
                    elif isinstance(delta, CodeExecutionCallDelta):
                        yield StreamChunk(
                            provider="gemini",
                            type="info",
                            info=delta.type,
                            tool_name="code_execution",
                            tool_input=delta.arguments.code,
                            tool_language=(
                                str(delta.arguments.language)
                                if delta.arguments.language is not None
                                else None
                            ),
                        )
                    elif isinstance(delta, CodeExecutionResultDelta):
                        yield StreamChunk(
                            provider="gemini",
                            type="info",
                            info=delta.type,
                            tool_name="code_execution",
                            tool_output=delta.result,
                            is_error=delta.is_error,
                        )
                    else:
                        # tool calls, search/code/url results, thoughts — informational.
                        yield StreamChunk(provider="gemini", type="info", info=delta.type)
                elif event.event_type == "error":
                    message = (event.error.message if event.error else None) or "Gemini error"
                    yield StreamChunk(provider="gemini", type="error", text=message)
                    return
                elif event.event_type == "interaction.status_update" and str(event.status) in {
                    "failed",
                    "cancelled",
                    "incomplete",
                    "budget_exceeded",
                }:
                    yield StreamChunk(
                        provider="gemini",
                        type="error",
                        text=f"Gemini interaction ended with status: {event.status}",
                    )
                    return
        except Exception as exc:  # surface provider errors as a stream event, don't crash the run
            yield StreamChunk(provider="gemini", type="error", text=str(exc))
            return
        yield StreamChunk(provider="gemini", type="done")

    async def _anthropic_stream(
        self,
        prompt: str,
        model: AnthropicModel,
        context: SessionContext,
    ) -> AsyncIterator[StreamChunk]:
        """Convert an Anthropic Messages SSE stream into normalized stream chunks.

        Claude runs on Vertex AI, where web search (basic variant) is the only
        available server-side tool. Search queries stream as `info` chunks with
        `tool_input`; cited sources arrive via citation deltas as `source` chunks.
        """
        try:
            stream = await self._anthropic.stream_messages(
                model=model,
                input=self._anthropic.build_text_messages(
                    self._text_messages(context),
                    prompt,
                ),
                max_tokens=16000,
                tools=[self._anthropic.Tools.web_search()],
            )
            # Server tool blocks stream sequentially; this tracks the open one so
            # input_json_delta fragments can be attributed to it.
            active_tool: str | None = None
            async for event in stream:
                if event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield StreamChunk(provider="anthropic", type="delta", text=delta.text)
                    elif delta.type == "citations_delta":
                        citation = delta.citation
                        if citation.type == "web_search_result_location" and citation.url:
                            yield StreamChunk(
                                provider="anthropic",
                                type="source",
                                sources=[Source(url=citation.url, title=citation.title)],
                            )
                    elif delta.type == "input_json_delta" and active_tool:
                        yield StreamChunk(
                            provider="anthropic",
                            type="info",
                            info=delta.type,
                            tool_name=active_tool,
                            tool_input=delta.partial_json,
                        )
                elif event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "server_tool_use":
                        active_tool = block.name
                        yield StreamChunk(
                            provider="anthropic",
                            type="info",
                            info=block.type,
                            tool_name=block.name,
                        )
                    elif block.type == "web_search_tool_result":
                        content = block.content
                        if isinstance(content, list):
                            yield StreamChunk(
                                provider="anthropic",
                                type="info",
                                info=block.type,
                                tool_name="web_search",
                                tool_output=f"{len(content)} results",
                            )
                        else:  # WebSearchToolResultError
                            yield StreamChunk(
                                provider="anthropic",
                                type="info",
                                info=block.type,
                                tool_name="web_search",
                                tool_output=content.error_code,
                                is_error=True,
                            )
                elif event.type == "content_block_stop":
                    active_tool = None
                elif event.type == "message_delta" and event.delta.stop_reason == "refusal":
                    yield StreamChunk(
                        provider="anthropic",
                        type="error",
                        text="Claude declined this request (stop_reason: refusal).",
                    )
                    return
        except Exception as exc:  # surface provider errors as a stream event, don't crash the run
            yield StreamChunk(provider="anthropic", type="error", text=str(exc))
            return
        yield StreamChunk(provider="anthropic", type="done")

    async def _openai_stream(
        self,
        prompt: str,
        thinking: OpenAIThinking | None,
        context: SessionContext,
    ) -> AsyncIterator[StreamChunk]:
        """Convert an OpenAI Responses SSE stream into normalized stream chunks.

        Stateless: the full transcript is rebuilt from Mongo on every request, so
        no response is stored or continued provider-side.
        """
        try:
            stream = await self._openai.stream_response(
                model="Luna",
                input=self._openai.build_text_input(
                    self._text_messages(context),
                    prompt,
                ),
                # outputs are null in the stream unless explicitly included
                include=["code_interpreter_call.outputs"],
                tools=[
                    self._openai.Tools.web_search(),
                    self._openai.Tools.code_interpreter(),
                ],
                reasoning=thinking if thinking is not None else omit,
                store=False,
            )
            async for event in stream:
                if event.type == "response.output_text.delta":
                    yield StreamChunk(provider="openai", type="delta", text=event.delta)
                elif event.type == "response.output_text.annotation.added":
                    annotation = event.annotation
                    if isinstance(annotation, AnnotationURLCitation):
                        yield StreamChunk(
                            provider="openai",
                            type="source",
                            sources=[Source(url=annotation.url, title=annotation.title)],
                        )
                elif event.type == "response.code_interpreter_call_code.delta":
                    yield StreamChunk(
                        provider="openai",
                        type="info",
                        info=event.type,
                        tool_name="code_interpreter",
                        tool_input=event.delta,
                        tool_language="python",
                    )
                elif event.type == "response.output_item.done":
                    item = event.item
                    if item.type == "code_interpreter_call":
                        outputs = item.outputs or []
                        logs = "".join(out.logs for out in outputs if out.type == "logs")
                        yield StreamChunk(
                            provider="openai",
                            type="info",
                            info=event.type,
                            tool_name="code_interpreter",
                            tool_output=logs,
                            is_error=item.status == "failed",
                        )
                        images = [
                            ImageOutput(url=out.url) for out in outputs if out.type == "image"
                        ]
                        if images:
                            yield StreamChunk(
                                provider="openai",
                                type="image",
                                info=event.type,
                                tool_name="code_interpreter",
                                images=images,
                            )
                    elif item.type == "web_search_call":
                        action = item.action
                        if action.type == "search":
                            query = action.query or ", ".join(action.queries or [])
                        elif action.type == "open_page":
                            query = action.url or ""
                        else:
                            query = f"find '{action.pattern}' in {action.url}"
                        yield StreamChunk(
                            provider="openai",
                            type="info",
                            info=event.type,
                            tool_name="web_search",
                            tool_input=query,
                            is_error=item.status == "failed",
                        )
                elif event.type == "error":
                    yield StreamChunk(provider="openai", type="error", text=event.message)
                    return
                elif event.type == "response.failed":
                    error = event.response.error
                    yield StreamChunk(
                        provider="openai",
                        type="error",
                        text=(error.message if error is not None else "OpenAI response failed"),
                    )
                    return
                elif event.type == "response.incomplete":
                    details = event.response.incomplete_details
                    reason = details.reason if details is not None else "unknown"
                    yield StreamChunk(
                        provider="openai",
                        type="error",
                        text=f"OpenAI response incomplete: {reason}",
                    )
                    return
                elif "_call" in event.type:
                    # web_search/code_interpreter/file_search/etc. lifecycle — informational.
                    yield StreamChunk(provider="openai", type="info", info=event.type)
        except Exception as exc:  # surface provider errors as a stream event, don't crash the run
            yield StreamChunk(provider="openai", type="error", text=str(exc))
            return
        yield StreamChunk(provider="openai", type="done")


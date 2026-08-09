from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

from google import genai
from google.genai import types

# Runtime re-exports (see __all__) so entities can isinstance-narrow interaction deltas
# without importing the private `_gaos` path themselves — the adapter stays the SDK boundary.
from google.genai._gaos.types.interactions.codeexecutioncalldelta import (  # pyright: ignore[reportPrivateImportUsage]
    CodeExecutionCallDelta,
)
from google.genai._gaos.types.interactions.codeexecutionresultdelta import (  # pyright: ignore[reportPrivateImportUsage]
    CodeExecutionResultDelta,
)
from google.genai._gaos.types.interactions.textannotationdelta import (  # pyright: ignore[reportPrivateImportUsage]
    TextAnnotationDelta,
)
from google.genai._gaos.types.interactions.textdelta import (  # pyright: ignore[reportPrivateImportUsage]
    TextDelta,
)
from google.genai._gaos.types.interactions.urlcitation import (  # pyright: ignore[reportPrivateImportUsage]
    URLCitation,
)

from app.config import settings

__all__ = [
    "CodeExecutionCallDelta",
    "CodeExecutionResultDelta",
    "GeminiAdapterClass",
    "GeminiBuiltinTool",
    "TextAnnotationDelta",
    "TextDelta",
    "URLCitation",
]

type GeminiBuiltinTool = Literal[
    "google_search",
    "google_maps",
    "url_context",
    "code_execution",
    "file_search",
]

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    # NOTE: `google.genai._gaos.*` is a PRIVATE SDK surface (Interactions API types).
    # Written against google-genai==2.10.0 — re-verify these import paths and type
    # names on any SDK upgrade; they are not covered by the public API contract.
    from google.genai._gaos.models import (  # pyright: ignore[reportPrivateImportUsage]
        createinteraction as create_interaction_types,
    )
    from google.genai._gaos.types import (  # pyright: ignore[reportPrivateImportUsage]
        interactions as interaction_types,
    )
    from google.genai._gaos.utils import (  # pyright: ignore[reportPrivateImportUsage]
        eventstreaming,
    )
    from google.genai.client import AsyncClient

    type GenerateContentStream = AsyncIterator[types.GenerateContentResponse]
    type InteractionInput = interaction_types.InteractionsInputParam
    type InteractionStep = interaction_types.StepParam
    type InteractionTool = interaction_types.ToolParam
    type InteractionGoogleSearchSearchType = interaction_types.GoogleSearchSearchType
    type InteractionResponseModality = interaction_types.ResponseModality
    type InteractionResponseFormat = interaction_types.CreateModelInteractionResponseFormatParam
    type InteractionEnvironment = interaction_types.CreateModelInteractionEnvironmentParam
    type InteractionGenerationConfig = interaction_types.GenerationConfigParam
    type InteractionModel = interaction_types.Model
    type ModelInteractionBody = interaction_types.CreateModelInteractionParam
    type ModelInteractionRequest = create_interaction_types.CreateInteractionRequestParam
    type ModelInteractionStream = eventstreaming.AsyncStream[interaction_types.InteractionSSEEvent]


class _Types:
    """Group private Gemini adapter model types."""

    type AvailableModels = Literal["3.5 Flash", "3.1 Flash Lite"]
    model_map = MappingProxyType[AvailableModels, str](
        {
            "3.5 Flash": "gemini-3.5-flash",
            "3.1 Flash Lite": "gemini-3.1-flash-lite",
        }
    )


class _Helpers:
    """Group private Gemini adapter helper functions."""

    @staticmethod
    def build_model_interaction_create_request(
        input: "InteractionInput",
        *,
        model: _Types.AvailableModels,
        stream: bool,
        store: bool | None,
        background: bool | None,
        system_instruction: str | None,
        tools: "list[InteractionTool] | None",
        response_modalities: "list[InteractionResponseModality] | None",
        response_format: "InteractionResponseFormat | None",
        environment: "InteractionEnvironment | None",
        generation_config: "InteractionGenerationConfig | None",
        previous_interaction_id: str | None,
        cached_content: str | None,
        api_version: str | None,
    ) -> "ModelInteractionRequest":
        """Build the generated SDK request body for a Gemini model interaction."""
        body: ModelInteractionBody = {
            "model": cast("InteractionModel", _Types.model_map[model]),
            "input": input,
        }

        if stream:
            body["stream"] = stream
        if store is not None:
            body["store"] = store
        if background is not None:
            body["background"] = background
        if system_instruction is not None:
            body["system_instruction"] = system_instruction
        if tools is not None:
            body["tools"] = tools
        if response_modalities is not None:
            body["response_modalities"] = response_modalities
        if response_format is not None:
            body["response_format"] = response_format
        if environment is not None:
            body["environment"] = environment
        if generation_config is not None:
            body["generation_config"] = generation_config
        if previous_interaction_id is not None:
            body["previous_interaction_id"] = previous_interaction_id
        if cached_content is not None:
            body["cached_content"] = cached_content

        request: ModelInteractionRequest = {"body": body}
        if api_version is not None:
            request["api_version"] = api_version

        return request


class GeminiAdapterClass:
    """Gemini through separate async Vertex and Developer API clients.

    Vertex handles generateContent with GCP credentials. The Gemini Developer API
    handles Interactions with an API key because that endpoint is not available on
    the current Vertex path.
    """

    @staticmethod
    def build_generate_content_input(
        messages: list[tuple[Literal["user", "assistant"], str]],
        prompt: str,
    ) -> list[types.Content]:
        """Build provider-native generateContent history from persisted text."""
        contents = [
            types.Content(
                role="model" if role == "assistant" else "user",
                parts=[types.Part(text=content)],
            )
            for role, content in messages
        ]
        contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))
        return contents

    @staticmethod
    def build_interaction_input(
        messages: list[tuple[Literal["user", "assistant"], str]],
        prompt: str,
    ) -> "InteractionInput":
        """Build provider-native Interactions steps from persisted text."""
        # One cast for the whole list: the conditional "type" widens the literal to str.
        steps = cast(
            "list[InteractionStep]",
            [
                {
                    "type": "model_output" if role == "assistant" else "user_input",
                    "content": [{"type": "text", "text": content}],
                }
                for role, content in messages
            ],
        )
        steps.append(
            {
                "type": "user_input",
                "content": [{"type": "text", "text": prompt}],
            }
        )
        return cast("InteractionInput", steps)

    class Tools:
        """Server-side tool builders for the Interactions API (private `_gaos` shapes)."""

        @staticmethod
        def google_search(
            search_types: "list[InteractionGoogleSearchSearchType] | None" = None,
        ) -> "InteractionTool":
            """Google Search grounding tool."""
            tool: InteractionTool = {"type": "google_search"}
            if search_types is not None:
                tool["search_types"] = search_types
            return tool

        @staticmethod
        def code_execution() -> "InteractionTool":
            """Code execution tool."""
            return {"type": "code_execution"}

        @staticmethod
        def file_search(
            *,
            file_search_store_names: list[str] | None = None,
            top_k: int | None = None,
            metadata_filter: str | None = None,
        ) -> "InteractionTool":
            """File Search tool."""
            tool: InteractionTool = {"type": "file_search"}
            if file_search_store_names is not None:
                tool["file_search_store_names"] = file_search_store_names
            if top_k is not None:
                tool["top_k"] = top_k
            if metadata_filter is not None:
                tool["metadata_filter"] = metadata_filter
            return tool

        @staticmethod
        def url_context() -> "InteractionTool":
            """URL context tool."""
            return {"type": "url_context"}

        @staticmethod
        def google_maps(
            *,
            enable_widget: bool | None = None,
            latitude: float | None = None,
            longitude: float | None = None,
        ) -> "InteractionTool":
            """Google Maps tool."""
            tool: InteractionTool = {"type": "google_maps"}
            if enable_widget is not None:
                tool["enable_widget"] = enable_widget
            if latitude is not None:
                tool["latitude"] = latitude
            if longitude is not None:
                tool["longitude"] = longitude
            return tool

        @staticmethod
        def from_names(tool_names: list[GeminiBuiltinTool]) -> "list[InteractionTool]":
            """Build Interactions API tools from stable frontend-facing names."""
            tools: list[InteractionTool] = []
            for tool_name in tool_names:
                if tool_name == "google_search":
                    tools.append(GeminiAdapterClass.Tools.google_search())
                elif tool_name == "google_maps":
                    tools.append(GeminiAdapterClass.Tools.google_maps(enable_widget=True))
                elif tool_name == "url_context":
                    tools.append(GeminiAdapterClass.Tools.url_context())
                elif tool_name == "code_execution":
                    tools.append(GeminiAdapterClass.Tools.code_execution())
                elif tool_name == "file_search":
                    tools.append(GeminiAdapterClass.Tools.file_search())
            return tools

        @staticmethod
        def google_search_grounding() -> types.Tool:
            """Google Search grounding tool for the generateContent API.

            Distinct from `google_search()` above: that returns an Interactions-API tool
            dict; this returns the typed `types.Tool` shape generateContent expects.
            """
            return types.Tool(google_search=types.GoogleSearch())

        @staticmethod
        def url_context_grounding() -> types.Tool:
            """URL context tool for the generateContent API (fetches/reads given URLs)."""
            return types.Tool(url_context=types.UrlContext())

        @staticmethod
        def google_maps_grounding(
            *,
            enable_widget: bool | None = None,
        ) -> types.Tool:
            """Google Maps tool for the generateContent API."""
            return types.Tool(google_maps=types.GoogleMaps(enable_widget=enable_widget))

        @staticmethod
        def code_execution_grounding() -> types.Tool:
            """Code execution tool for the generateContent API (runs generated Python)."""
            return types.Tool(code_execution=types.ToolCodeExecution())

        @staticmethod
        def generate_content_from_names(tool_names: list[GeminiBuiltinTool]) -> list[types.Tool]:
            """Build generateContent API tools from stable frontend-facing names."""
            tools: list[types.Tool] = []
            for tool_name in tool_names:
                if tool_name == "google_search":
                    tools.append(GeminiAdapterClass.Tools.google_search_grounding())
                elif tool_name == "google_maps":
                    tools.append(
                        GeminiAdapterClass.Tools.google_maps_grounding(enable_widget=True),
                    )
                elif tool_name == "url_context":
                    tools.append(GeminiAdapterClass.Tools.url_context_grounding())
                elif tool_name == "code_execution":
                    tools.append(GeminiAdapterClass.Tools.code_execution_grounding())
                elif tool_name == "file_search":
                    tools.append(types.Tool(file_search=types.FileSearch()))
            return tools

    @staticmethod
    def extract_text(chunk: types.GenerateContentResponse) -> str:
        """Concatenate the model-output text parts of a generateContent chunk.

        Read directly off parts (not `chunk.text`) to avoid the SDK's non-text-part
        warning and to exclude `thought` summaries from the user-facing delta.
        """
        text = ""
        for candidate in chunk.candidates or []:
            content = candidate.content
            for part in (content.parts if content else None) or []:
                if part.text and not part.thought:
                    text += part.text
        return text

    @staticmethod
    def extract_tool_events(chunk: types.GenerateContentResponse) -> list[str]:
        """Return info labels for built-in tool activity in a generateContent chunk.

        Surfaces active built-ins as `info` events. Grounding citations are reported
        separately via `extract_grounding_sources`.
        """
        events: list[str] = []

        def append_event(label: str) -> None:
            if label not in events:
                events.append(label)

        for candidate in chunk.candidates or []:
            content = candidate.content
            for part in (content.parts if content else None) or []:
                if part.executable_code is not None:
                    append_event("code_execution")
                if part.code_execution_result is not None:
                    append_event("code_execution_result")
            meta = candidate.grounding_metadata
            if meta is not None:
                grounding_chunks = meta.grounding_chunks or []
                if meta.web_search_queries or any(
                    grounding_chunk.web is not None for grounding_chunk in grounding_chunks
                ):
                    append_event("google_search")
                if meta.google_maps_widget_context_token or any(
                    grounding_chunk.maps is not None for grounding_chunk in grounding_chunks
                ):
                    append_event("google_maps")
            if candidate.url_context_metadata is not None:
                append_event("url_context")
        return events

    @staticmethod
    def extract_grounding_sources(
        chunk: types.GenerateContentResponse,
    ) -> list[tuple[str, str | None]]:
        """Pull (url, title) citations from a generateContent chunk's grounding metadata.

        Keeps the SDK grounding-metadata shape behind the adapter boundary so the pipeline
        works in plain (url, title) tuples. Grounding typically lands on the final chunk(s).
        """
        sources: list[tuple[str, str | None]] = []
        for candidate in chunk.candidates or []:
            meta = candidate.grounding_metadata
            if meta is None:
                continue
            for grounding_chunk in meta.grounding_chunks or []:
                web = grounding_chunk.web
                if web is not None and web.uri:
                    sources.append((web.uri, web.title))
                maps = grounding_chunk.maps
                if maps is not None and maps.uri:
                    sources.append((maps.uri, maps.title))
        return sources

    @staticmethod
    def extract_maps_widget_context_token(chunk: types.GenerateContentResponse) -> str | None:
        """Pull the Google Maps widget context token from generateContent metadata."""
        for candidate in chunk.candidates or []:
            meta = candidate.grounding_metadata
            if meta is not None and meta.google_maps_widget_context_token:
                return meta.google_maps_widget_context_token
        return None

    def __init__(self) -> None:
        """Create empty Gemini async client holders."""
        self._vertex_client: AsyncClient | None = None
        self._interactions_client: AsyncClient | None = None

    async def initialize(self) -> None:
        """Initialize the Vertex client and optional Developer API client."""
        self._vertex_client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.vertex_region,
        ).aio
        if settings.gemini_api_key:
            self._interactions_client = genai.Client(api_key=settings.gemini_api_key).aio

    async def close(self) -> None:
        """Close both Gemini async clients."""
        vertex_client = self._vertex_client
        interactions_client = self._interactions_client
        self._vertex_client = None
        self._interactions_client = None

        if vertex_client is not None:
            await vertex_client.aclose()
        if interactions_client is not None:
            await interactions_client.aclose()

    @property
    def vertex_client(self) -> "AsyncClient":
        """Return the initialized async Vertex client."""
        if self._vertex_client is None:
            raise RuntimeError("Gemini Vertex client not initialized")
        return self._vertex_client

    @property
    def interactions_client(self) -> "AsyncClient":
        """Return the initialized async Gemini Developer API client."""
        if self._interactions_client is None:
            raise RuntimeError(
                "Gemini Interactions client not initialized: set GEMINI_API_KEY",
            )
        return self._interactions_client

    async def stream_generate_content(
        self,
        # ContentListUnion is the SDK's own type; its union has an untyped (PIL) member.
        contents: types.ContentListUnion,  # pyright: ignore[reportUnknownParameterType]
        *,
        model: _Types.AvailableModels,
        tools: list[types.Tool] | None = None,
        thinking_level: Literal["minimal", "low", "medium", "high"] | None = None,
        config: types.GenerateContentConfig | None = None,
    ) -> "GenerateContentStream":
        """Stream Gemini generation chunks with raw SDK response metadata.

        `thinking_level` is ignored when an explicit `config` is given. Gemini 3.x
        models cannot turn thinking fully off; "minimal" is the lowest level.
        """
        if config is None:
            # list is invariant; the SDK wants list[ToolUnion], we hold list[Tool].
            config = types.GenerateContentConfig(
                tools=cast("types.ToolListUnion | None", tools),
                thinking_config=(
                    types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking_level))
                    if thinking_level is not None
                    else None
                ),
            )

        stream = await self.vertex_client.models.generate_content_stream(
            model=_Types.model_map[model],
            contents=contents,
            config=config,
        )
        async for chunk in stream:
            yield chunk

    async def stream_model_interaction_events(
        self,
        input: "InteractionInput",
        *,
        model: _Types.AvailableModels,
        store: bool | None = None,
        background: bool | None = None,
        system_instruction: str | None = None,
        tools: "list[InteractionTool] | None" = None,
        response_modalities: "list[InteractionResponseModality] | None" = None,
        response_format: "InteractionResponseFormat | None" = None,
        environment: "InteractionEnvironment | None" = None,
        generation_config: "InteractionGenerationConfig | None" = None,
        previous_interaction_id: str | None = None,
        cached_content: str | None = None,
        api_version: str | None = None,
    ) -> "ModelInteractionStream":
        """Create a Gemini model interaction and stream its SSE lifecycle events."""
        request = _Helpers.build_model_interaction_create_request(
            input=input,
            model=model,
            stream=True,
            store=store,
            background=background,
            system_instruction=system_instruction,
            tools=tools,
            response_modalities=response_modalities,
            response_format=response_format,
            environment=environment,
            generation_config=generation_config,
            previous_interaction_id=previous_interaction_id,
            cached_content=cached_content,
            api_version=api_version,
        )
        return cast(
            "ModelInteractionStream",
            await self.interactions_client.interactions.create(request=request),
        )

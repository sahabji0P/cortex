from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

from google import genai
from google.genai import types

# Runtime re-export (see __all__) so entities can isinstance-narrow interaction deltas
# without importing the private `_gaos` path themselves — the adapter stays the SDK boundary.
from google.genai._gaos.types.interactions.textdelta import (  # pyright: ignore[reportPrivateImportUsage]
    TextDelta,
)

from app.config import settings

__all__ = ["GeminiAdapterClass", "TextDelta"]

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
    type InteractionTool = interaction_types.ToolParam
    type InteractionResponseModality = interaction_types.ResponseModality
    type InteractionResponseFormat = interaction_types.CreateModelInteractionResponseFormatParam
    type InteractionEnvironment = interaction_types.CreateModelInteractionEnvironmentParam
    type InteractionGenerationConfig = interaction_types.GenerationConfigParam
    type InteractionModel = interaction_types.Model
    type ModelInteraction = interaction_types.Interaction
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
    """Gemini via Vertex AI. Auth is GCP ADC — shares project/region with Anthropic.

    `.client` is the async client; adapter methods wrap generateContent and Interactions.
    """

    def __init__(self) -> None:
        """Create an empty Gemini async client holder."""
        self._client: AsyncClient | None = None

    async def initialize(self) -> None:
        """Initialize the Gemini async client for Vertex AI."""
        # AsyncClient is only reachable via Client(...).aio — keep it, drop the sync wrapper.
        self._client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.vertex_region,
        ).aio

    async def close(self) -> None:
        """Complete Gemini adapter shutdown."""
        # google-genai holds no long-lived connection that needs explicit teardown.
        return

    @property
    def client(self) -> "AsyncClient":
        """Return the initialized Gemini async client."""
        if self._client is None:
            raise RuntimeError("Gemini client not initialized")
        return self._client

    async def stream_generate_content(
        self,
        # ContentListUnion is the SDK's own type; its union has an untyped (PIL) member.
        contents: types.ContentListUnion,  # pyright: ignore[reportUnknownParameterType]
        *,
        model: _Types.AvailableModels,
        tools: list[types.Tool] | None = None,
        config: types.GenerateContentConfig | None = None,
    ) -> "GenerateContentStream":
        """Stream Gemini generation chunks with raw SDK response metadata."""
        if config is None:
            config = types.GenerateContentConfig(tools=tools)

        stream = await self.client.models.generate_content_stream(
            model=_Types.model_map[model],
            contents=contents,
            config=config,
        )
        async for chunk in stream:
            yield chunk

    async def create_model_interaction(
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
    ) -> "ModelInteraction":
        """Create a non-streaming Gemini model interaction."""
        request = _Helpers.build_model_interaction_create_request(
            input=input,
            model=model,
            stream=False,
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
        return cast("ModelInteraction", await self.client.interactions.create(request=request))

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
            await self.client.interactions.create(request=request),
        )

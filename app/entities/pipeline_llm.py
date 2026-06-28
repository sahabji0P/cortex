from collections.abc import AsyncIterator
from typing import Literal

from pydantic import BaseModel

from app.adapters.gemini_adp import GeminiAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass

type Provider = Literal["openai", "gemini"]


class StreamChunk(BaseModel):
    """One normalized event in the unified stream, regardless of source provider."""

    provider: Provider
    type: Literal["delta", "done", "error"]
    text: str = ""


class LLMPipelineEntityClass:
    """Routes a prompt to OpenAI, Gemini, or both and yields a single unified stream.

    Providers run in the order requested (sequentially, not concurrently). Each
    provider's deltas are tagged with its name and terminated by a `done` event,
    so a downstream SSE endpoint can forward `StreamChunk`s straight to the client.
    """

    def __init__(self, openai: OpenAIAdapterClass, gemini: GeminiAdapterClass) -> None:
        """Initialize the pipeline with provider adapters."""
        self._openai = openai
        self._gemini = gemini

    async def run(self, prompt: str, providers: list[Provider]) -> AsyncIterator[StreamChunk]:
        """Run the prompt through the requested providers in order."""
        for provider in providers:
            stream = self._gemini_stream if provider == "gemini" else self._openai_stream
            async for chunk in stream(prompt):
                yield chunk

    async def _gemini_stream(self, prompt: str) -> AsyncIterator[StreamChunk]:
        """Convert a Gemini stream into normalized stream chunks."""
        try:
            async for chunk in self._gemini.stream(prompt, model="3.5 Flash"):
                if chunk.text:
                    yield StreamChunk(provider="gemini", type="delta", text=chunk.text)
        except Exception as exc:  # surface provider errors as a stream event, don't crash the run
            yield StreamChunk(provider="gemini", type="error", text=str(exc))
            return
        yield StreamChunk(provider="gemini", type="done")

    async def _openai_stream(self, prompt: str) -> AsyncIterator[StreamChunk]:
        """Convert an OpenAI Responses stream into normalized stream chunks."""
        try:
            stream = await self._openai.stream_response(model="5.4", input=prompt)
            async for event in stream:
                if event.type == "response.output_text.delta":
                    yield StreamChunk(provider="openai", type="delta", text=event.delta)
        except Exception as exc:  # surface provider errors as a stream event, don't crash the run
            yield StreamChunk(provider="openai", type="error", text=str(exc))
            return
        yield StreamChunk(provider="openai", type="done")

"""
How a provider's models are reached. Only OpenAI has a choice: straight with the API
key, or through a proxy such as CLIProxyAPI signed in with a ChatGPT account (see
`openai_proxy_url` in the settings).

The user's choice is one document in the `Providers` collection. With none, the proxy is
used when it is set up. The choice is put back on the adapter when the backend starts,
unless that way is no longer set up.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.docdb_adp import DocDBAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass, OpenAIVia
from app.core.times import UtcDatetime

__all__ = ["OpenAIRoute", "OpenAIRouteSetting", "OpenAIWayNotSetUpError", "ProviderEntityClass"]

# The one document's id. Another provider with a choice would get its own.
_OPENAI = "openai"

# What is missing, said in words, for a way that is not set up.
_NOT_SET_UP: dict[OpenAIVia, str] = {
    "api": "OpenAI's API key is not set up: OPENAI_API_KEY is empty in the backend's .env",
    "proxy": "no proxy is set up for OpenAI: OPENAI_PROXY_URL is empty in the backend's .env",
}


class OpenAIRouteSetting(BaseModel):
    """The way to OpenAI the user chose, as it is stored."""

    __collection_name__: ClassVar[str] = "Providers"

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default=_OPENAI, alias="_id")
    via: OpenAIVia
    updated_at: UtcDatetime


@dataclass(frozen=True, slots=True)
class OpenAIRoute:
    """The way in use, and which ways are set up."""

    via: OpenAIVia
    api: bool
    proxy: bool


class OpenAIWayNotSetUpError(Exception):
    """The chosen way has nothing set up in the backend's settings."""


class ProviderEntityClass:
    """Read and change how OpenAI is reached."""

    def __init__(self, docdb: DocDBAdapterClass, openai: OpenAIAdapterClass) -> None:
        self._docdb = docdb
        self._openai = openai

    async def restore(self) -> None:
        """Put the stored choice back on the adapter, when that way is still set up."""
        stored = await self._docdb.get(OpenAIRouteSetting, filters={"id": _OPENAI})
        if stored is not None and self._openai.is_set_up(stored.via):
            self._openai.use(stored.via)

    def openai_route(self) -> OpenAIRoute:
        return OpenAIRoute(
            via=self._openai.via,
            api=self._openai.is_set_up("api"),
            proxy=self._openai.is_set_up("proxy"),
        )

    async def set_openai_via(self, via: OpenAIVia) -> OpenAIRoute:
        """Store the choice and use it from the next request on."""
        if not self._openai.is_set_up(via):
            raise OpenAIWayNotSetUpError(_NOT_SET_UP[via])
        setting = OpenAIRouteSetting(via=via, updated_at=datetime.now(UTC))
        await self._docdb.update(
            OpenAIRouteSetting, {"id": _OPENAI}, setting.model_dump(exclude={"id"}), upsert=True
        )
        self._openai.use(via)
        return self.openai_route()

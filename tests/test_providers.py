"""How OpenAI is reached: the user's choice, kept, put back at startup, and refused
when that way is not set up."""

from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel

from app.adapters.openai_adp import OpenAIAdapterClass
from app.core.errors import ConflictError
from app.entities.providers_ent import OpenAIRoute, OpenAIRouteSetting, ProviderEntityClass
from app.routers.providers.models import ProvidersModels
from app.routers.providers.service import ProvidersServiceV1Class

if TYPE_CHECKING:
    from app.adapters.docdb_adp import DocDBAdapterClass


class _DocDBFake:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    async def get[T: BaseModel](
        self, collection: type[T], *, filters: MutableMapping[str, Any]
    ) -> T | None:
        assert collection is OpenAIRouteSetting
        document = self.documents.get(filters["id"])
        return None if document is None else collection.model_validate(document)

    async def update(
        self,
        collection: type[BaseModel],
        filters: MutableMapping[str, Any],
        update_data: MutableMapping[str, Any],
        *,
        upsert: bool = False,
    ) -> int:
        assert collection is OpenAIRouteSetting
        assert upsert
        self.documents.setdefault(filters["id"], {"_id": filters["id"]}).update(update_data)
        return 1


async def _openai(monkeypatch: pytest.MonkeyPatch, *, api: bool, proxy: bool) -> OpenAIAdapterClass:
    settings = "app.adapters.openai_adp.settings"
    monkeypatch.setattr(f"{settings}.openai_api_key", "sk-openai" if api else "")
    monkeypatch.setattr(f"{settings}.openai_proxy_url", "http://127.0.0.1:8317/v1" if proxy else "")
    monkeypatch.setattr(f"{settings}.openai_proxy_key", "proxy-key")
    adapter = OpenAIAdapterClass()
    await adapter.initialize()
    return adapter


@pytest.mark.asyncio
async def test_the_choice_is_kept_and_put_back_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    docdb = _DocDBFake()
    openai = await _openai(monkeypatch, api=True, proxy=True)
    providers = ProviderEntityClass(cast("DocDBAdapterClass", docdb), openai)
    assert providers.openai_route() == OpenAIRoute(via="proxy", api=True, proxy=True)

    assert await providers.set_openai_via("api") == OpenAIRoute(via="api", api=True, proxy=True)
    assert openai.client.api_key == "sk-openai"

    # A restart: a new adapter starts on the proxy, and the stored choice is put back.
    openai = await _openai(monkeypatch, api=True, proxy=True)
    assert openai.via == "proxy"
    await ProviderEntityClass(cast("DocDBAdapterClass", docdb), openai).restore()
    assert openai.via == "api"

    # A stored way that is no longer set up is left aside.
    openai = await _openai(monkeypatch, api=False, proxy=True)
    await ProviderEntityClass(cast("DocDBAdapterClass", docdb), openai).restore()
    assert openai.via == "proxy"


@pytest.mark.asyncio
async def test_a_way_not_set_up_is_refused_with_409(monkeypatch: pytest.MonkeyPatch) -> None:
    docdb = _DocDBFake()
    openai = await _openai(monkeypatch, api=True, proxy=False)
    service = ProvidersServiceV1Class(ProviderEntityClass(cast("DocDBAdapterClass", docdb), openai))

    with pytest.raises(ConflictError) as refused:
        await service.set_openai(ProvidersModels.V1.Input.SetOpenAIRoute(via="proxy"))
    assert refused.value.status_code == 409
    assert "OPENAI_PROXY_URL" in refused.value.message
    assert docdb.documents == {}
    assert await service.get_openai() == ProvidersModels.V1.Output.OpenAIRoute(
        via="api", api=True, proxy=False
    )

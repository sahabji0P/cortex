"""How OpenAI is reached: the user's choice, kept, put back at startup, and refused
when that way is not set up. And the proxy's ChatGPT account, changed by a sign-in."""

import asyncio
from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from pydantic import BaseModel

from app.adapters.cliproxy_adp import CLIProxyAdapterClass, CLIProxyError, ProxyLogin
from app.adapters.openai_adp import OpenAIAdapterClass
from app.core.errors import ConflictError
from app.entities import providers_ent
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


def _login(name: str, modified: str, *, plan: str = "free", active: bool = True) -> ProxyLogin:
    return ProxyLogin(
        name=name,
        email=f"{name}@example.com",
        plan=plan,
        active=active,
        problem=None if active else "unauthorized",
        modified=modified,
    )


class _ProxyFake:
    """The proxy's management API: its logins, and one sign-in the test finishes."""

    def __init__(self, *logins: ProxyLogin, configured: bool = True) -> None:
        self.configured = configured
        self.logins = list(logins)
        self.status: tuple[str, str | None] = ("wait", None)
        self.down = False

    async def chatgpt_logins(self) -> list[ProxyLogin]:
        if self.down:
            raise CLIProxyError("the proxy did not answer")
        return list(self.logins)

    async def start_chatgpt_sign_in(self) -> tuple[str, str]:
        return "https://auth.example/authorize", "state-1"

    async def sign_in_status(self, state: str) -> tuple[str, str | None]:
        assert state == "state-1"
        return self.status

    async def delete_login(self, name: str) -> None:
        self.logins = [login for login in self.logins if login.name != name]


def _providers(
    docdb: _DocDBFake, openai: OpenAIAdapterClass, proxy: _ProxyFake | None = None
) -> ProviderEntityClass:
    return ProviderEntityClass(
        cast("DocDBAdapterClass", docdb),
        openai,
        cast("CLIProxyAdapterClass", proxy or _ProxyFake(configured=False)),
    )


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
    providers = _providers(docdb, openai)
    assert await providers.openai_route() == OpenAIRoute(
        via="api", api=True, proxy=True, manageable=False, accounts=None
    )

    assert (await providers.set_openai_via("proxy")).via == "proxy"
    assert openai.client.api_key == "proxy-key"

    # A restart: a new adapter starts on the API key, and the stored choice is put back.
    openai = await _openai(monkeypatch, api=True, proxy=True)
    assert openai.via == "api"
    await _providers(docdb, openai).restore()
    assert openai.via == "proxy"

    # A stored way that is no longer set up is left aside.
    openai = await _openai(monkeypatch, api=True, proxy=False)
    await _providers(docdb, openai).restore()
    assert openai.via == "api"


@pytest.mark.asyncio
async def test_a_way_not_set_up_is_refused_with_409(monkeypatch: pytest.MonkeyPatch) -> None:
    docdb = _DocDBFake()
    openai = await _openai(monkeypatch, api=True, proxy=False)
    service = ProvidersServiceV1Class(_providers(docdb, openai))

    with pytest.raises(ConflictError) as refused:
        await service.set_openai(ProvidersModels.V1.Input.SetOpenAIRoute(via="proxy"))
    assert refused.value.status_code == 409
    assert "OPENAI_PROXY_URL" in refused.value.message
    assert docdb.documents == {}
    assert await service.get_openai() == ProvidersModels.V1.Output.OpenAIRoute(
        via="api", api=True, proxy=False, manageable=False, accounts=None
    )

    # Nor can an account be changed without the proxy's management key.
    with pytest.raises(ConflictError) as refused:
        await service.start_sign_in()
    assert "OPENAI_PROXY_MANAGEMENT_KEY" in refused.value.message


@pytest.mark.asyncio
async def test_a_finished_sign_in_replaces_the_proxys_other_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers_ent, "_SIGN_IN_ASK_SECONDS", 0.01)
    proxy = _ProxyFake(_login("old", "t1", plan="team", active=False), _login("kept", "t1"))
    providers = _providers(_DocDBFake(), await _openai(monkeypatch, api=True, proxy=True), proxy)
    service = ProvidersServiceV1Class(providers)

    shown = await service.get_openai()
    assert shown.manageable
    assert [(a.email, a.plan, a.active) for a in shown.accounts or []] == [
        ("old@example.com", "team", False),
        ("kept@example.com", "free", True),
    ]

    started = await service.start_sign_in()
    assert (started.url, started.state) == ("https://auth.example/authorize", "state-1")
    assert (await service.get_sign_in("state-1")).status == "wait"

    # The user signs in again to the account whose pass was refused: its login is
    # written again, and it becomes the only one.
    proxy.logins[0] = _login("old", "t2", plan="team")
    proxy.status = ("ok", None)
    await asyncio.sleep(0.05)
    done = await service.get_sign_in("state-1")
    assert (done.status, done.email) == ("ok", "old@example.com")
    assert [login.name for login in proxy.logins] == ["old"]
    await providers.close()


@pytest.mark.asyncio
async def test_a_failed_sign_in_removes_nothing_and_a_silent_proxy_keeps_the_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers_ent, "_SIGN_IN_ASK_SECONDS", 0.01)
    proxy = _ProxyFake(_login("kept", "t1"))
    providers = _providers(_DocDBFake(), await _openai(monkeypatch, api=True, proxy=True), proxy)

    _, state = await providers.start_sign_in()
    proxy.status = ("error", "the sign-in was refused")
    await asyncio.sleep(0.05)
    failed = providers.sign_in(state)
    assert failed is not None
    assert (failed.status, failed.reason) == ("error", "the sign-in was refused")
    assert [login.name for login in proxy.logins] == ["kept"]
    assert providers.sign_in("never-started") is None

    proxy.down = True
    route = await providers.openai_route()
    assert (route.via, route.manageable, route.accounts) == ("api", True, None)


@pytest.mark.asyncio
async def test_an_answer_that_is_not_the_management_api_is_an_error_not_a_crash() -> None:
    adapter = CLIProxyAdapterClass()
    adapter._client = httpx.AsyncClient(
        base_url="http://proxy.test/v0/management",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="<html>not it</html>")),
    )
    for ask in (
        adapter.chatgpt_logins(),
        adapter.sign_in_status("s"),
        adapter.start_chatgpt_sign_in(),
    ):
        with pytest.raises(CLIProxyError, match="could not be read"):
            await ask
    await adapter.close()

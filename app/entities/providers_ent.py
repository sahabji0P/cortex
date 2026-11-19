"""
How a provider's models are reached. Only OpenAI has a choice: straight with the API
key, or through a proxy such as CLIProxyAPI signed in with a ChatGPT account (see
`openai_proxy_url` in the settings).

The user's choice is one document in the `Providers` collection. With none, the API key
is used when it is set up. The choice is put back on the adapter when the backend starts,
unless that way is no longer set up.

The proxy's ChatGPT account can be changed from here too, when the proxy's management
key is set up. A sign-in is started on the proxy, the user finishes it in the browser,
and the browser goes back to the proxy, which keeps the new login. This backend only
watches for the end of it, and then removes the proxy's other ChatGPT logins, so the
account just signed in is the one every request uses.
"""

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.cliproxy_adp import CLIProxyAdapterClass, CLIProxyError, ProxyLogin
from app.adapters.docdb_adp import DocDBAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass, OpenAIVia
from app.core.times import UtcDatetime

__all__ = [
    "OpenAIRoute",
    "OpenAIRouteSetting",
    "OpenAIWayNotSetUpError",
    "ProviderEntityClass",
    "ProxyNotManageableError",
    "ProxyUnreachableError",
    "SignIn",
]

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


# How long a sign-in is waited for, and how often the proxy is asked about it.
_SIGN_IN_SECONDS = 600.0
_SIGN_IN_ASK_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class OpenAIRoute:
    """The way in use, which ways are set up, and the proxy's ChatGPT accounts."""

    via: OpenAIVia
    api: bool
    proxy: bool
    # Whether the proxy's accounts can be seen and changed from here: its management key
    # is set up.
    manageable: bool
    # The proxy's ChatGPT logins. None when they are not known: not manageable, or the
    # proxy did not answer.
    accounts: list[ProxyLogin] | None


@dataclass(slots=True)
class SignIn:
    """A sign-in to the proxy with a ChatGPT account, from its start to its end."""

    status: Literal["wait", "ok", "error"] = "wait"
    # Why it failed, in words. None unless `status` is "error".
    reason: str | None = None
    # The account signed in with. None until `status` is "ok".
    email: str | None = None
    # The logins the proxy held when the sign-in started, to tell the new one by.
    before: dict[str, str] = field(default_factory=dict[str, str])


class OpenAIWayNotSetUpError(Exception):
    """The chosen way has nothing set up in the backend's settings."""


class ProxyNotManageableError(Exception):
    """The proxy's management key is not set up, so its accounts cannot be changed."""


class ProxyUnreachableError(Exception):
    """The proxy did not answer, or refused the management key."""


class ProviderEntityClass:
    """Read and change how OpenAI is reached, and the proxy's ChatGPT account."""

    def __init__(
        self, docdb: DocDBAdapterClass, openai: OpenAIAdapterClass, proxy: CLIProxyAdapterClass
    ) -> None:
        self._docdb = docdb
        self._openai = openai
        self._proxy = proxy
        # Sign-ins by their state, kept for as long as the backend runs, and the tasks
        # that watch the ones still open.
        self._sign_ins: dict[str, SignIn] = {}
        self._watching: set[asyncio.Task[None]] = set()

    async def restore(self) -> None:
        """Put the stored choice back on the adapter, when that way is still set up."""
        stored = await self._docdb.get(OpenAIRouteSetting, filters={"id": _OPENAI})
        if stored is not None and self._openai.is_set_up(stored.via):
            self._openai.use(stored.via)

    async def close(self) -> None:
        """Stop watching the sign-ins still open."""
        for watching in list(self._watching):
            watching.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watching

    async def openai_route(self) -> OpenAIRoute:
        accounts = None
        if self._proxy.configured:
            # The proxy being down must not take the choice of way with it.
            with contextlib.suppress(CLIProxyError):
                accounts = await self._proxy.chatgpt_logins()
        return OpenAIRoute(
            via=self._openai.via,
            api=self._openai.is_set_up("api"),
            proxy=self._openai.is_set_up("proxy"),
            manageable=self._proxy.configured,
            accounts=accounts,
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
        return await self.openai_route()

    async def start_sign_in(self) -> tuple[str, str]:
        """Start a sign-in on the proxy. Returns the address for the browser and the state
        to ask about it by (`sign_in`)."""
        if not self._proxy.configured:
            raise ProxyNotManageableError(
                "the proxy's accounts cannot be changed from here: "
                "OPENAI_PROXY_MANAGEMENT_KEY is empty in the backend's .env"
            )
        try:
            before = {login.name: login.modified for login in await self._proxy.chatgpt_logins()}
            url, state = await self._proxy.start_chatgpt_sign_in()
        except CLIProxyError as error:
            raise ProxyUnreachableError(str(error)) from error
        sign_in = SignIn(before=before)
        self._sign_ins[state] = sign_in
        watching = asyncio.create_task(self._watch(state, sign_in))
        self._watching.add(watching)
        watching.add_done_callback(self._watching.discard)
        return url, state

    def sign_in(self, state: str) -> SignIn | None:
        """How that sign-in stands. None for a state this backend did not start."""
        return self._sign_ins.get(state)

    async def _watch(self, state: str, sign_in: SignIn) -> None:
        """Ask the proxy until the sign-in ends, then keep only the new login."""
        try:
            async with asyncio.timeout(_SIGN_IN_SECONDS):
                while True:
                    status, reason = await self._proxy.sign_in_status(state)
                    if status == "error":
                        sign_in.status, sign_in.reason = "error", reason
                        return
                    if status == "ok":
                        break
                    await asyncio.sleep(_SIGN_IN_ASK_SECONDS)
            sign_in.email = await self._keep_only_the_new_login(sign_in.before)
            sign_in.status = "ok"
        except TimeoutError:
            sign_in.status, sign_in.reason = "error", "the sign-in was not finished in time"
        except CLIProxyError as error:
            sign_in.status, sign_in.reason = "error", str(error)

    async def _keep_only_the_new_login(self, before: dict[str, str]) -> str | None:
        """Remove every ChatGPT login but the one just written. Returns its account.

        The new login is the one that was not there before, or was written again: a
        sign-in to an account the proxy already held rewrites its login. When none can be
        told apart, nothing is removed.
        """
        logins = await self._proxy.chatgpt_logins()
        new = [login for login in logins if before.get(login.name) != login.modified]
        if len(new) != 1:
            return None
        for login in logins:
            if login.name != new[0].name:
                await self._proxy.delete_login(login.name)
        return new[0].email

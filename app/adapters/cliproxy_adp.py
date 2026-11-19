"""
CLIProxyAPI's management API: the ChatGPT logins the OpenAI proxy uses.

Only when the proxy is CLIProxyAPI and its management key is set (`remote-management.
secret-key` in the proxy's config, the same value as `openai_proxy_management_key`).
The API sits next to the proxy's OpenAI one: `<proxy>/v0/management` for a proxy
address of `<proxy>/v1`.
"""

from dataclasses import dataclass
from typing import Any, Literal, cast

import httpx

from app.config import settings

__all__ = ["CLIProxyAdapterClass", "CLIProxyError", "ProxyLogin", "SignInStatus"]

_TIMEOUT_SECONDS = 10.0

type SignInStatus = Literal["wait", "ok", "error"]


@dataclass(frozen=True, slots=True)
class ProxyLogin:
    """One ChatGPT login the proxy holds."""

    # The proxy's name for it, which is also how it is deleted.
    name: str
    email: str
    # The ChatGPT plan, such as "free", "plus" or "team". None when the proxy does not say.
    plan: str | None
    # False when the proxy cannot use it, such as when its pass was refused.
    active: bool
    # What is wrong with it, in the proxy's words. None when nothing is.
    problem: str | None
    # When the login was last written: a new sign-in to the same account rewrites it.
    modified: str


class CLIProxyError(Exception):
    """The proxy did not answer, or answered with an error, such as a refused key."""


class CLIProxyAdapterClass:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        if not (settings.openai_proxy_url and settings.openai_proxy_management_key):
            return
        base = settings.openai_proxy_url.rstrip("/").removesuffix("/v1")
        self._client = httpx.AsyncClient(
            base_url=f"{base}/v0/management",
            headers={"Authorization": f"Bearer {settings.openai_proxy_management_key}"},
            timeout=_TIMEOUT_SECONDS,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    @property
    def configured(self) -> bool:
        return self._client is not None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("the proxy's management key is not set up")
        return self._client

    async def _ask(
        self, method: str, path: str, *, params: dict[str, str] | None = None
    ) -> httpx.Response:
        """One request to the proxy. The sender decides what its status means."""
        try:
            return await self.client.request(method, path, params=params)
        except httpx.HTTPError as error:
            raise CLIProxyError(f"the proxy did not answer: {error}") from error

    @staticmethod
    def _answered(response: httpx.Response) -> httpx.Response:
        if response.is_error:
            raise CLIProxyError(f"the proxy answered {response.status_code}")
        return response

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        """The answer's JSON object. Anything else is not the management API talking."""
        try:
            body = response.json()
        except ValueError as error:
            raise CLIProxyError("the proxy's answer could not be read") from error
        if not isinstance(body, dict):
            raise CLIProxyError("the proxy's answer could not be read")
        return cast("dict[str, Any]", body)

    async def chatgpt_logins(self) -> list[ProxyLogin]:
        """The proxy's ChatGPT (Codex) logins."""
        response = self._answered(await self._ask("GET", "/auth-files"))
        files = cast("list[dict[str, Any]]", self._body(response).get("files", []))
        return [
            ProxyLogin(
                name=file["name"],
                email=file.get("email") or file.get("account") or file["name"],
                plan=cast("dict[str, str]", file.get("id_token") or {}).get("plan_type"),
                active=not file.get("disabled") and not file.get("unavailable"),
                problem=file.get("status_message") or None,
                modified=file.get("modtime") or "",
            )
            for file in files
            if file.get("provider") == "codex"
        ]

    async def start_chatgpt_sign_in(self) -> tuple[str, str]:
        """Start a sign-in. Returns the address to open in the browser and its state.

        The browser comes back to the proxy itself, which keeps the new login.
        """
        response = self._answered(
            await self._ask("GET", "/codex-auth-url", params={"is_webui": "true"})
        )
        body = self._body(response)
        if not (body.get("url") and body.get("state")):
            raise CLIProxyError("the proxy started no sign-in")
        return body["url"], body["state"]

    async def sign_in_status(self, state: str) -> tuple[SignInStatus, str | None]:
        """Whether that sign-in is still waiting, went through, or failed (with why)."""
        response = await self._ask("GET", "/get-auth-status", params={"state": state})
        body = self._body(response)
        status = body.get("status")
        if status in ("wait", "ok"):
            return status, None
        return "error", body.get("error") or f"the proxy answered {response.status_code}"

    async def delete_login(self, name: str) -> None:
        response = await self._ask("DELETE", "/auth-files", params={"name": name})
        # A login already gone is not an error.
        if response.status_code != 404:
            self._answered(response)

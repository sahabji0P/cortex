from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # A local .env may still carry settings for things that were removed.
        extra="ignore",
    )

    # MongoDB
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_db_name: str = "cortex"

    # Redis
    redis_url: str = "redis://localhost:6379"

    # Auth
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 60
    jwt_algorithm: str = "HS256"

    # App
    environment: Literal["local", "staging", "production"] = "local"
    debug: bool = True
    # The address the user's browser reaches this backend at. A connection's sign-in
    # sends the browser back to this backend, at `{backend_url}/v1/connections/sign-in/
    # callback`, so it has to be reachable from the browser. The default works because
    # the backend runs on the user's own Mac. Rethink it when the backend is deployed or
    # runs on another machine. Changing it makes each connection register again.
    backend_url: str = "http://127.0.0.1:8000"

    # The model a gem answers with, in a call or in writing, when its config names none:
    # "6 Luna", "6.1 Terra" or "6.1 Sol" (OpenAI); "Haiku 5.5", "Sonnet 5.5" or "Opus 5.5"
    # (Anthropic). Its provider needs a key.
    voice_model: Literal[
        "6 Luna", "6.1 Terra", "6.1 Sol", "Haiku 5.5", "Sonnet 5.5", "Opus 5.5"
    ] = "6 Luna"

    # OpenAI
    openai_api_key: str = ""

    # OpenAI through a proxy that speaks its API, such as CLIProxyAPI signed in with a
    # ChatGPT account. Empty = no proxy. With both this and `openai_api_key` set, the user
    # chooses between them (`/v1/providers/openai`); the API key is used until they do.
    openai_proxy_url: str = ""
    openai_proxy_key: str = ""
    # CLIProxyAPI's management key (`remote-management.secret-key` in its config). With it
    # the app can see the proxy's ChatGPT logins and sign in with another account.
    # Empty = the logins are neither shown nor changed from here.
    openai_proxy_management_key: str = ""

    # Anthropic: a key for the Claude API itself (platform.claude.com), not a cloud
    # provider's.
    anthropic_api_key: str = ""

    # Cartesia (speech-to-text and text-to-speech)
    cartesia_api_key: str = ""

    # ElevenLabs (text-to-speech). Empty = it cannot be used.
    elevenlabs_api_key: str = ""

    # Parallel (web search tool for the voice pipeline). Empty = the tool is not offered.
    parallel_api_key: str = ""


settings = Settings()

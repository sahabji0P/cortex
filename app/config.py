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

    # The model a gem answers with, in a call or in writing, when its config names none:
    # "6 Luna", "6.1 Terra" or "6.1 Sol" (OpenAI); "Haiku 5.5", "Sonnet 5.5" or "Opus 5.5"
    # (Anthropic). Its provider needs a key.
    voice_model: Literal[
        "6 Luna", "6.1 Terra", "6.1 Sol", "Haiku 5.5", "Sonnet 5.5", "Opus 5.5"
    ] = "6 Luna"

    # OpenAI
    openai_api_key: str = ""

    # Anthropic: a key for the Claude API itself (platform.claude.com), not a cloud
    # provider's.
    anthropic_api_key: str = ""

    # Cartesia (speech-to-text and text-to-speech)
    cartesia_api_key: str = ""

    # Parallel (web search tool for the voice pipeline). Empty = the tool is not offered.
    parallel_api_key: str = ""


settings = Settings()

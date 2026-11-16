from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # A local .env may still carry keys for providers that were removed.
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

    # Google Cloud / Vertex AI, used only by the Claude Agent SDK pipeline.
    gcp_project_id: str = ""
    vertex_region: str = "global"  # "global" (recommended), "us"/"eu", or a specific region
    # Optional path to a GCP service-account key JSON, handed to the spawned harness.
    # Leave empty to fall back to gcloud ADC.
    gcp_credentials_path: str = ""

    # OpenAI (the model the voice pipeline uses)
    openai_api_key: str = ""

    # Anthropic (the Claude Messages API adapter; nothing calls it yet)
    anthropic_api_key: str = ""

    # Cartesia (speech-to-text and text-to-speech)
    cartesia_api_key: str = ""

    # Parallel (web search tool for the voice pipeline). Empty = the tool is not offered.
    parallel_api_key: str = ""


settings = Settings()

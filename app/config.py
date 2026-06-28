from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
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

    # Google Cloud / Vertex AI (auth via GCP ADC — `gcloud auth application-default login`)
    # Both Anthropic (Claude) and Gemini run through Vertex with this project/region.
    gcp_project_id: str = ""
    vertex_region: str = "global"  # "global" (recommended), "us"/"eu", or a specific region

    # Default models (override per call as needed)
    anthropic_model: str = "claude-opus-4-8"  # bare ID on Vertex, no provider prefix
    gemini_model: str = "gemini-2.5-flash"

    # OpenAI
    openai_api_key: str = ""


settings = Settings()

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
    # Anthropic and Gemini generateContent run through Vertex with this project/region.
    gcp_project_id: str = ""
    vertex_region: str = "global"  # "global" (recommended), "us"/"eu", or a specific region
    # Optional path to a GCP service-account key JSON. When set, it's exported as
    # GOOGLE_APPLICATION_CREDENTIALS at startup so the Vertex clients authenticate
    # via this key. Leave empty to fall back to gcloud ADC.
    gcp_credentials_path: str = ""

    # Gemini Developer API (used by the Interactions endpoint).
    gemini_api_key: str = ""

    # OpenAI
    openai_api_key: str = ""

    # Cartesia
    cartesia_api_key: str = ""


settings = Settings()

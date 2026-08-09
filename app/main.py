import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import routers
from app.adapters import AdapterRegistry
from app.config import settings
from app.core.errors import AppError
from app.entities import EntitiesRegistry


def _apply_gcp_credentials() -> None:
    """Point Vertex auth at a service-account key file when one is configured.

    Exports settings.gcp_credentials_path as GOOGLE_APPLICATION_CREDENTIALS so the
    Anthropic + Gemini Vertex clients authenticate via the key instead of gcloud
    ADC. An already-set env var wins; a missing/blank path is a no-op (ADC path).
    Must run before AdapterRegistry.initialize() constructs the Vertex clients.
    """
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or not settings.gcp_credentials_path:
        return
    path = Path(settings.gcp_credentials_path).expanduser()
    if path.is_file():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    _apply_gcp_credentials()

    adapter = AdapterRegistry()
    await adapter.initialize()

    await EntitiesRegistry.initialize(adapter)

    _app.state.adapter = adapter

    yield

    await adapter.close()


app = FastAPI(
    title="Cortex",
    description="Experimentation platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.debug else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Session-ID"],
)

# Dynamically include all routers grouped in app.routers (each carries its own prefix).
for group_name in routers.__all__:
    for router in cast("tuple[APIRouter, ...]", getattr(routers, group_name)):
        app.include_router(router)


@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}

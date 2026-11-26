import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import routers
from app.adapters import AdapterRegistry
from app.config import settings
from app.core.errors import AppError
from app.entities import EntitiesRegistry
from app.entities.watch_ent import HOOKS_PATH


class _HookTokensHidden(logging.Filter):
    """Keeps a webhook's token out of the access log, which prints each request's path.

    The token is the secret that lets a post in, so the log shows `/v1/hooks/…` instead.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_hidden(arg) for arg in args)
        return True


def _hidden(arg: object) -> object:
    if isinstance(arg, str) and arg.startswith(f"{HOOKS_PATH}/"):
        return f"{HOOKS_PATH}/…"
    return arg


logging.getLogger("uvicorn.access").addFilter(_HookTokensHidden())


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    adapter = AdapterRegistry()
    await adapter.initialize()

    await EntitiesRegistry.initialize(adapter)
    # Gems with events waiting since before the restart are woken.
    await EntitiesRegistry.resume()
    await EntitiesRegistry.Watches.start()

    _app.state.adapter = adapter

    yield

    await EntitiesRegistry.Watches.close()
    # What happened last is still on its way to the database: it is waited for.
    await EntitiesRegistry.Log.written()
    await EntitiesRegistry.Providers.close()
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


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's own 422, without what was sent where that may hold a secret.

    A refused field is answered with the input it was refused for. For a connection
    that is its headers, whose values are never to be sent back. What is posted to a
    watch's webhook is never sent back either.
    """
    errors = exc.errors()
    if request.url.path.startswith(("/v1/connections", HOOKS_PATH)):
        errors = [{k: v for k, v in error.items() if k != "input"} for error in errors]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}

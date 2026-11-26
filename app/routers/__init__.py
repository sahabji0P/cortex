from fastapi import APIRouter

from app.routers.connections.controller import router as connections_router
from app.routers.darbar.controller import router as darbar_router
from app.routers.gems.controller import router as gems_router
from app.routers.hooks.controller import router as hooks_router
from app.routers.link.controller import router as link_router
from app.routers.providers.controller import router as providers_router
from app.routers.user.controller import router as user_router
from app.routers.voice.controller import router as voice_router
from app.routers.watches.controller import router as watches_router

# Grouped router collections, included dynamically at startup (see main.py).
# Each name in __all__ maps to a tuple of routers that carry their own full prefix.
V1: tuple[APIRouter, ...] = (
    user_router,
    voice_router,
    gems_router,
    watches_router,
    darbar_router,
    connections_router,
    providers_router,
    hooks_router,
    link_router,
)

__all__ = ["V1"]

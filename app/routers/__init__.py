from fastapi import APIRouter

from app.routers.cortex.controller import router as cortex_router
from app.routers.user.controller import router as user_router

# Grouped router collections, included dynamically at startup (see main.py).
# Each name in __all__ maps to a tuple of routers that carry their own full prefix.
V1: tuple[APIRouter, ...] = (user_router, cortex_router)

__all__ = ["V1"]

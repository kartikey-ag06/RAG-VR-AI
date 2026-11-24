"""create_app - builds the FastAPI application shell: middleware, CORS and the liveness check.

Routes are still added by app.py and the feature modules; later Phase 3 steps move them behind the Core.
"""
from typing import Iterable, Tuple, Type

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .settings import Settings


def create_app(settings: Settings, middleware: Iterable[Tuple[Type, dict]] = (), title: str = "VR_RAG backend") -> FastAPI:
    """`middleware` = [(MiddlewareClass, kwargs), ...], added in order, all INSIDE CORS.

    CORS is added last so it is the outermost layer: an error answered by inner middleware (429, 413) still
    carries CORS headers and the browser can show its message.
    """
    app = FastAPI(title=title)
    for cls, kwargs in middleware:
        app.add_middleware(cls, **(kwargs or {}))
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=settings.cors_origin_regex,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After", "X-Request-ID", "ETag"],
        max_age=600,
    )

    @app.get("/healthz")
    def healthz():
        """Cheap liveness check for Render / uptime monitors: no disk, no database, no AI."""
        return {"ok": True}

    app.state.settings = settings
    return app

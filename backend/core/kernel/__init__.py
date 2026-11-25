"""core.kernel - start-up of the backend: settings, the service container and the app factory (Phase 3)."""
from .app_factory import create_app
from .container import Container, ServiceError
from .settings import Settings

__all__ = ["Settings", "Container", "ServiceError", "create_app"]

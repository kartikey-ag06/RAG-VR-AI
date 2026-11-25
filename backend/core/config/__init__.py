"""core.config - central settings with scopes (global, tenant, tenant+module) and history (Phase 4)."""
from .store import ConfigError, ConfigStore, scope_id

__all__ = ["ConfigStore", "ConfigError", "scope_id"]

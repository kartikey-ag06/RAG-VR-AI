"""core.admin - the control plane API (/api/core/*): tenants and API keys. Admin only (scope "admin")."""
from .routes import register_admin

__all__ = ["register_admin"]

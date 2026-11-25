"""TenantContext - sets the current tenant for each HTTP request from the caller's identity (Phase 4 part 2b).

It never rejects a request: endpoints decide access (401/403). A caller whose credentials cannot be resolved
(e.g. a wrong API key) runs as DEFAULT_TENANT here - and the endpoint then refuses the bad key itself.
"""
from starlette.requests import Request

from core.data.tenancy import DEFAULT_TENANT, reset_tenant, set_tenant


class TenantContext:
    def __init__(self, app, identity):
        self.app, self.identity = app, identity

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            tenant = self.identity.resolve(Request(scope))[0].tenant_id
        except Exception:
            tenant = DEFAULT_TENANT
        token = set_tenant(tenant)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_tenant(token)

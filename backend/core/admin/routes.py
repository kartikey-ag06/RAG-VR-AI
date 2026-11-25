"""Control plane: /api/core/tenants and /api/core/keys (Phase 4 part 2a).

Everything here needs the "admin" scope: ADMIN_TOKEN, an admin user, an API key with scope "admin", or an unprotected
local server. A new key's secret is returned ONCE in the create response; afterwards only its hash exists.
"""
from fastapi import Body, Request
from fastapi.responses import JSONResponse

from core.sdk.context import PolicyDenied


def register_admin(app, identity, tenants, keys):
    def guard(req):
        try:
            identity.require(req, "admin")
            return None
        except PolicyDenied as e:
            return JSONResponse({"error": e.reason}, e.status)

    def bad(msg):
        return JSONResponse({"error": str(msg)}, 400)

    @app.get("/api/core/tenants")
    def list_tenants(req: Request):
        return guard(req) or {"tenants": tenants.list()}

    @app.post("/api/core/tenants")
    def create_tenant(req: Request, body: dict = Body(...)):
        if (g := guard(req)) is not None:
            return g
        try:
            return {"tenant": tenants.create(body.get("id"), body.get("name"))}
        except ValueError as e:
            return bad(e)

    @app.get("/api/core/keys")
    def list_keys(req: Request, tenant_id: str = ""):
        return guard(req) or {"keys": keys.list(tenant_id or None)}

    @app.post("/api/core/keys")
    def create_key(req: Request, body: dict = Body(...)):
        if (g := guard(req)) is not None:
            return g
        try:
            rec, secret = keys.create(body.get("name", ""), body.get("scopes") or [], body.get("tenant_id") or "default")
        except ValueError as e:
            return bad(e)
        return {"key": rec, "secret": secret, "note": "store this key now - it is not shown again"}

    @app.delete("/api/core/keys/{key_id}")
    def revoke_key(key_id: str, req: Request):
        if (g := guard(req)) is not None:
            return g
        return {"revoked": keys.revoke(key_id)} if keys.kv.kv_get("apikey", key_id) else JSONResponse(
            {"error": "no such key"}, 404)

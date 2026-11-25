"""Control plane (Phase 4): tenants, API keys, settings, modules, limits and the audit log.

Everything here needs the "admin" scope: ADMIN_TOKEN, an admin user, an API key with scope "admin", or an unprotected
local server. A new key's secret is returned ONCE in the create response; afterwards only its hash exists.
Every change made here is written to the audit log.

    GET/POST   /api/core/tenants                      list / create organisations
    GET/POST   /api/core/keys, DELETE /api/core/keys/{id}
    GET        /api/core/config                       schema + stored values (?tenant_id= to filter)
    PUT/DELETE /api/core/config                       {key, value, tenant_id?, module?} set / remove one value
    GET        /api/core/config/history
    GET        /api/core/packs?tenant_id=             modules and whether they are ON for that tenant
    PUT        /api/core/tenants/{t}/packs/{p}        {enabled: true|false}
    GET/PUT    /api/core/tenants/{t}/limits           {ai_daily: n}
    GET        /api/core/audit                        ?tenant_id= &action= &limit=
"""
from fastapi import Body, Request
from fastapi.responses import JSONResponse

from core.config import ConfigError
from core.config.store import check_value
from core.sdk.context import PolicyDenied

LIMIT_KEYS = {"ai_daily": "limits.ai_daily"}


def register_admin(app, identity, tenants, keys, config=None, modules=None, audit=None):
    def guard(req):
        try:
            identity.require(req, "admin")
            return None
        except PolicyDenied as e:
            return JSONResponse({"error": e.reason}, e.status)

    def actor(req):
        try:
            p = identity.resolve(req)[0]
            return f"{p.kind}:{p.name or p.id or 'anonymous'}"
        except Exception:
            return "unknown"

    def log(req, action, target, tenant_id=None, **details):
        if audit is not None:
            audit.record(action, actor(req), target, tenant_id=tenant_id, details=details)

    def bad(msg, status=400):
        return JSONResponse({"error": str(msg)}, status)

    def no_tenant(t):
        return tenants.get(t) is None

    # ------------------------------------------------------------ tenants
    @app.get("/api/core/tenants")
    def list_tenants(req: Request):
        return guard(req) or {"tenants": tenants.list()}

    @app.post("/api/core/tenants")
    def create_tenant(req: Request, body: dict = Body(...)):
        if (g := guard(req)) is not None:
            return g
        try:
            t = tenants.create(body.get("id"), body.get("name"))
        except ValueError as e:
            return bad(e)
        log(req, "tenant.create", t["id"], tenant_id=t["id"], name=t.get("name"))
        return {"tenant": t}

    # ------------------------------------------------------------ API keys
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
        log(req, "apikey.create", rec["id"], tenant_id=rec["tenant_id"], name=rec["name"], scopes=rec["scopes"])
        return {"key": rec, "secret": secret, "note": "store this key now - it is not shown again"}

    @app.delete("/api/core/keys/{key_id}")
    def revoke_key(key_id: str, req: Request):
        if (g := guard(req)) is not None:
            return g
        rec = keys.kv.kv_get("apikey", key_id)
        if not rec:
            return bad("no such key", 404)
        done = keys.revoke(key_id)
        log(req, "apikey.revoke", key_id, tenant_id=rec.get("tenant_id"), name=rec.get("name"))
        return {"revoked": done}

    if config is None or modules is None or audit is None:
        return

    # ------------------------------------------------------------ settings
    @app.get("/api/core/config")
    def config_get(req: Request, tenant_id: str = ""):
        if (g := guard(req)) is not None:
            return g
        schema = {k: {kk: vv for kk, vv in v.items()} for k, v in sorted(config.schema.items())}
        return {"schema": schema, "values": config.overrides(tenant_id or None)}

    @app.put("/api/core/config")
    def config_put(req: Request, body: dict = Body(...)):
        if (g := guard(req)) is not None:
            return g
        t = body.get("tenant_id") or None
        if t and no_tenant(t):
            return bad(f"no tenant {t!r}", 404)
        if "value" not in body:
            return bad("value is required")
        try:
            rec = config.set(str(body.get("key", "")), body["value"], t, body.get("module") or None, actor(req))
        except ConfigError as e:
            return bad(e)
        log(req, "config.set", rec["id"], tenant_id=t or "default", key=rec["key"], scope=rec["scope"],
            version=rec["version"])
        return {"config": rec}

    @app.delete("/api/core/config")
    def config_delete(req: Request, key: str, tenant_id: str = "", module: str = ""):
        if (g := guard(req)) is not None:
            return g
        try:
            removed = config.unset(key, tenant_id or None, module or None, actor(req))
        except ConfigError as e:
            return bad(e)
        if not removed:
            return bad("no such value", 404)
        log(req, "config.unset", key, tenant_id=tenant_id or "default", module=module or None)
        return {"removed": True}

    @app.get("/api/core/config/history")
    def config_history(req: Request, key: str = "", limit: int = 100):
        return guard(req) or {"history": config.history(key or None, max(1, min(1000, limit)))}

    # ------------------------------------------------------------ modules on / off
    @app.get("/api/core/packs")
    def packs(req: Request, tenant_id: str = "default"):
        if (g := guard(req)) is not None:
            return g
        if no_tenant(tenant_id):
            return bad(f"no tenant {tenant_id!r}", 404)
        return {"tenant_id": tenant_id, "packs": modules.list(tenant_id)}

    @app.put("/api/core/tenants/{tenant_id}/packs/{pack_id}")
    def pack_toggle(tenant_id: str, pack_id: str, req: Request, body: dict = Body(...)):
        if (g := guard(req)) is not None:
            return g
        if no_tenant(tenant_id):
            return bad(f"no tenant {tenant_id!r}", 404)
        if pack_id not in modules.modules:
            return bad(f"no module {pack_id!r}", 404)
        if not isinstance(body.get("enabled"), bool):
            return bad("enabled must be true or false")
        rec = modules.set_enabled(pack_id, tenant_id, body["enabled"], actor(req))
        log(req, "module.enable" if body["enabled"] else "module.disable", pack_id, tenant_id=tenant_id)
        return {"pack": rec}

    # ------------------------------------------------------------ limits
    @app.get("/api/core/tenants/{tenant_id}/limits")
    def limits_get(tenant_id: str, req: Request):
        if (g := guard(req)) is not None:
            return g
        if no_tenant(tenant_id):
            return bad(f"no tenant {tenant_id!r}", 404)
        return {"tenant_id": tenant_id, "limits": {n: config.get(k, tenant_id) for n, k in LIMIT_KEYS.items()}}

    @app.put("/api/core/tenants/{tenant_id}/limits")
    def limits_put(tenant_id: str, req: Request, body: dict = Body(...)):
        if (g := guard(req)) is not None:
            return g
        if no_tenant(tenant_id):
            return bad(f"no tenant {tenant_id!r}", 404)
        unknown = set(body) - set(LIMIT_KEYS)
        if unknown:
            return bad(f"unknown limit(s): {', '.join(sorted(unknown))}")
        try:
            for name, value in body.items():
                check_value(config.schema[LIMIT_KEYS[name]], value)
            for name, value in body.items():
                config.set(LIMIT_KEYS[name], value, tenant_id, None, actor(req))
        except ConfigError as e:
            return bad(e)
        log(req, "limits.set", tenant_id, tenant_id=tenant_id, **body)
        return limits_get(tenant_id, req)

    # ------------------------------------------------------------ audit
    @app.get("/api/core/audit")
    def audit_list(req: Request, tenant_id: str = "", action: str = "", limit: int = 200):
        return guard(req) or {"events": audit.list(tenant_id or None, action or None, limit)}

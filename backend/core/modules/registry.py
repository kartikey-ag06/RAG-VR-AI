"""ModuleRegistry - which feature modules exist, and which are switched ON for each tenant (Phase 4).

A module = one feature area with its URL prefixes, e.g. "meetings" = /api/meetings + /api/teleprompter.
The core (search, /api/v1, MCP, files, admin) is not a module: it can never be switched off.

Modules are ON by default (so nothing changes until an admin switches one off). The ModuleGate middleware answers
404 for every URL of a module that is OFF for the caller's tenant - the feature simply does not exist for them.
Phase 5 turns these entries into pack manifests; the switch and the gate stay the same.
"""
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from core.data.tenancy import DEFAULT_TENANT, TENANT_ID_RE, current_tenant

MODULE_ID = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
CACHE_SECONDS = 5


@dataclass(frozen=True)
class Module:
    id: str
    name: str
    kind: str                      # "capability" (generic) | "pack" (sector-specific)
    prefixes: Tuple[str, ...]      # URL prefixes it serves, e.g. ("/api/meetings", "/api/teleprompter")
    description: str = ""
    default_on: bool = True


@dataclass
class ModuleRegistry:
    kv: object
    modules: Dict[str, Module] = field(default_factory=dict)
    _cache: tuple = (0.0, {})
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, m: Module) -> None:
        if not MODULE_ID.match(m.id):
            raise ValueError(f"bad module id {m.id!r}")
        for p in m.prefixes:
            if not p.startswith("/") or p.endswith("/"):
                raise ValueError(f"bad prefix {p!r}: start with '/', no trailing '/'")
            for other in self.modules.values():
                if p in other.prefixes:
                    raise ValueError(f"prefix {p} already belongs to module {other.id}")
        self.modules[m.id] = m

    def module_for_path(self, path: str) -> Optional[Module]:
        """The module whose LONGEST prefix matches the path at a '/' boundary (so /api/calendar/sources belongs to
        the module that registered it, not to the one with /api/calendar)."""
        best, best_len = None, -1
        for m in self.modules.values():
            for p in m.prefixes:
                if (path == p or path.startswith(p + "/")) and len(p) > best_len:
                    best, best_len = m, len(p)
        return best

    # ------------------------------------------------------------ on / off per tenant
    def _states(self) -> dict:
        t, states = self._cache
        if time.time() - t > CACHE_SECONDS:
            states = {r["id"]: r for r in self.kv.kv_list("module_state")}
            self._cache = (time.time(), states)
        return states

    def enabled(self, module_id: str, tenant_id: Optional[str] = None) -> bool:
        m = self.modules.get(module_id)
        if not m:
            return False
        rec = self._states().get(f"{tenant_id or current_tenant()}|{module_id}")
        return m.default_on if rec is None else bool(rec["enabled"])

    def set_enabled(self, module_id: str, tenant_id: str, enabled: bool, actor: str = "") -> dict:
        if module_id not in self.modules:
            raise ValueError(f"unknown module {module_id!r}")
        if not TENANT_ID_RE.match(tenant_id or ""):
            raise ValueError(f"bad tenant id {tenant_id!r}")
        rid = f"{tenant_id}|{module_id}"
        rec = {"id": rid, "tenant_id": tenant_id, "module": module_id, "enabled": bool(enabled),
               "updated": time.time(), "by": actor}
        with self._lock:
            self.kv.kv_put("module_state", rid, rec)
            self._cache = (0.0, {})
        return rec

    def list(self, tenant_id: str = DEFAULT_TENANT) -> list:
        return [{"id": m.id, "name": m.name, "kind": m.kind, "prefixes": list(m.prefixes),
                 "description": m.description, "enabled": self.enabled(m.id, tenant_id)}
                for m in sorted(self.modules.values(), key=lambda x: x.id)]


class ModuleGate:
    """ASGI middleware: 404 for any URL of a module that is OFF for the current tenant.
    Must run INSIDE TenantContext (add it to the app BEFORE TenantContext, so TenantContext ends up outside)."""

    def __init__(self, app, registry: ModuleRegistry):
        self.app, self.registry = app, registry

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            m = self.registry.module_for_path(scope.get("path", ""))
            if m is not None and not self.registry.enabled(m.id):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4404})
                    return
                body = (b'{"error":"not found","detail":"module ' + m.id.encode() +
                        b' is not enabled for this tenant"}')
                await send({"type": "http.response.start", "status": 404,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)

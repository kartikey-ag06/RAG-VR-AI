"""ConfigStore - central, versioned settings (Phase 4, plan section 6).

Every setting is a KEY declared in a schema (name, type, default, description). A value can be set at 3 scopes;
the narrowest one that is set wins:

    default (schema)  <  global  <  tenant  <  tenant + module

Values are changed through the control plane (/api/core/config) without a redeploy; reads are cached for a few
seconds. Every change is kept in a history (who, when, old -> new) - see also the audit log.
Unknown keys and values of the wrong type are refused: a typo must not silently do nothing.
"""
import re
import threading
import time
from typing import Any, Dict, Optional

from core.data.tenancy import TENANT_ID_RE, current_tenant

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
MODULE_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "list": list, "object": dict}
CACHE_SECONDS = 5


class ConfigError(ValueError):
    pass


def check_value(spec: dict, value: Any) -> Any:
    t = spec["type"]
    if t in ("integer", "number") and isinstance(value, bool):
        raise ConfigError(f"expected {t}, got boolean")
    if not isinstance(value, TYPES[t]):
        raise ConfigError(f"expected {t}, got {type(value).__name__}")
    if t == "string" and len(value) > spec.get("max_length", 20000):
        raise ConfigError("text too long")
    if "min" in spec and value < spec["min"]:
        raise ConfigError(f"must be at least {spec['min']}")
    if "max" in spec and value > spec["max"]:
        raise ConfigError(f"must be at most {spec['max']}")
    return value


def scope_id(tenant_id: Optional[str] = None, module: Optional[str] = None) -> str:
    """'global' | 'tenant:<t>' | 'tenant:<t>:module:<m>'"""
    if module and not tenant_id:
        raise ConfigError("a module setting needs a tenant")
    if tenant_id and not TENANT_ID_RE.match(tenant_id):
        raise ConfigError(f"bad tenant id {tenant_id!r}")
    if module and not MODULE_RE.match(module):
        raise ConfigError(f"bad module id {module!r}")
    if not tenant_id:
        return "global"
    return f"tenant:{tenant_id}" + (f":module:{module}" if module else "")


class ConfigStore:
    def __init__(self, kv):
        self.kv = kv
        self.schema: Dict[str, dict] = {}
        self._cache = (0.0, {})
        self._lock = threading.Lock()

    # ------------------------------------------------------------ schema
    def declare(self, key: str, type_: str, default: Any = None, description: str = "", **limits) -> None:
        if not KEY_RE.match(key):
            raise ConfigError(f"bad config key {key!r}")
        if type_ not in TYPES:
            raise ConfigError(f"unknown type {type_!r}")
        spec = {"type": type_, "default": default, "description": description, **limits}
        if default is not None:
            check_value(spec, default)
        self.schema[key] = spec

    # ------------------------------------------------------------ read
    def _values(self) -> dict:
        t, values = self._cache
        if time.time() - t > CACHE_SECONDS:
            values = {r["id"]: r for r in self.kv.kv_list("config")}
            self._cache = (time.time(), values)
        return values

    def get(self, key: str, tenant_id: Optional[str] = None, module: Optional[str] = None) -> Any:
        if key not in self.schema:
            raise ConfigError(f"unknown config key {key!r}")
        tenant_id = tenant_id or current_tenant()
        values = self._values()
        scopes = ([scope_id(tenant_id, module)] if module else []) + [scope_id(tenant_id), "global"]
        for s in scopes:
            rec = values.get(f"{s}|{key}")
            if rec is not None:
                return rec["value"]
        return self.schema[key]["default"]

    def overrides(self, tenant_id: Optional[str] = None) -> list:
        """Every stored value (all scopes), or only those that apply to one tenant."""
        out = sorted(self._values().values(), key=lambda r: r["id"])
        if tenant_id:
            out = [r for r in out if r["scope"] == "global" or r["scope"].startswith(f"tenant:{tenant_id}")]
        return out

    # ------------------------------------------------------------ write
    def set(self, key: str, value: Any, tenant_id: Optional[str] = None, module: Optional[str] = None,
            actor: str = "") -> dict:
        if key not in self.schema:
            raise ConfigError(f"unknown config key {key!r}")
        check_value(self.schema[key], value)
        s = scope_id(tenant_id, module)
        rid = f"{s}|{key}"
        with self._lock:
            old = self.kv.kv_get("config", rid)
            rec = {"id": rid, "scope": s, "key": key, "value": value, "version": (old or {}).get("version", 0) + 1,
                   "updated": time.time(), "by": actor}
            self.kv.kv_put("config", rid, rec)
            self._history(rid, old, rec, actor)
            self._cache = (0.0, {})
        return rec

    def unset(self, key: str, tenant_id: Optional[str] = None, module: Optional[str] = None, actor: str = "") -> bool:
        s = scope_id(tenant_id, module)
        rid = f"{s}|{key}"
        with self._lock:
            old = self.kv.kv_get("config", rid)
            if not old:
                return False
            self.kv.kv_delete("config", rid)
            self._history(rid, old, None, actor)
            self._cache = (0.0, {})
        return True

    def rollback(self, key: str, version: int, tenant_id: Optional[str] = None, module: Optional[str] = None,
                 actor: str = "") -> dict:
        """Restore an earlier value from config_history as a new version (append-only history)."""
        if key not in self.schema:
            raise ConfigError(f"unknown config key {key!r}")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ConfigError("version must be a positive integer")
        s = scope_id(tenant_id, module)
        rid = f"{s}|{key}"
        with self._lock:
            rows = sorted((r for r in self.kv.kv_list("config_history") if r.get("config_id") == rid),
                          key=lambda r: r["id"], reverse=True)
            target = next((r for r in rows if r.get("version") == version and not r.get("removed")), None)
            if target is None:
                raise LookupError(f"version {version} not found for {key!r}")
            value = target["new"]
            check_value(self.schema[key], value)
            old = self.kv.kv_get("config", rid)
            rec = {"id": rid, "scope": s, "key": key, "value": value, "version": (old or {}).get("version", 0) + 1,
                   "updated": time.time(), "by": actor}
            self.kv.kv_put("config", rid, rec)
            self._history(rid, old, rec, actor, rollback_to=version)
            self._cache = (0.0, {})
        return rec

    def history(self, key: Optional[str] = None, limit: int = 100) -> list:
        rows = sorted(self.kv.kv_list("config_history"), key=lambda r: r["id"], reverse=True)   # newest first
        return [r for r in rows if key in (None, r["key"])][:limit]

    def _history(self, rid, old, new, actor, rollback_to: Optional[int] = None):
        hid = f"{time.time_ns():020d}"
        entry = {
            "id": hid, "config_id": rid, "key": rid.split("|", 1)[1], "ts": time.time(), "by": actor,
            "old": (old or {}).get("value"), "new": (new or {}).get("value"),
            "version": (new or {}).get("version"), "removed": new is None,
        }
        if rollback_to is not None:
            entry["rollback_to"] = rollback_to
        self.kv.kv_put("config_history", hid, entry)

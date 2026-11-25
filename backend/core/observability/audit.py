"""Audit log - who did what, when (Phase 4). Enterprise and government buyers ask for exactly this.

Recorded: API keys created/revoked, tenants created, config changed, modules switched, users added/removed,
documents uploaded/changed/deleted. Never recorded: secrets (key secrets, tokens, passwords, connection strings).
Entries are append-only through this class (there is no update or delete method).
"""
import secrets
import time
from typing import Optional

from core.data.tenancy import current_tenant

SECRET_WORDS = ("secret", "token", "password", "api_key", "apikey", "database_url")


def _clean(details: Optional[dict]) -> dict:
    """Drop anything that looks like a secret; shorten long values."""
    out = {}
    for k, v in (details or {}).items():
        if any(w in str(k).lower() for w in SECRET_WORDS):
            continue
        if isinstance(v, str) and len(v) > 200:
            v = v[:200] + "..."
        out[str(k)] = v
    return out


class AuditLog:
    def __init__(self, kv):
        self.kv = kv

    def record(self, action: str, actor: str = "", target: str = "", tenant_id: Optional[str] = None,
               details: Optional[dict] = None) -> dict:
        eid = f"{time.time_ns():020d}-{secrets.token_hex(3)}"
        e = {"id": eid, "ts": time.time(), "tenant_id": tenant_id or current_tenant(), "actor": actor or "unknown",
             "action": action, "target": str(target)[:200], "details": _clean(details)}
        self.kv.kv_put("audit", eid, e)
        return e

    def list(self, tenant_id: Optional[str] = None, action: Optional[str] = None, limit: int = 200) -> list:
        rows = sorted(self.kv.kv_list("audit"), key=lambda r: r["id"], reverse=True)      # newest first
        rows = [r for r in rows if tenant_id in (None, r["tenant_id"]) and action in (None, r["action"])]
        return rows[:max(1, min(1000, limit))]

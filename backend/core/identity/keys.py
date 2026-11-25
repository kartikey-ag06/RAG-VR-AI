"""API keys and tenants (Phase 4 part 2a, ADR 0002 + 0003).

API key = "vrk_<id>.<secret>"
  - <id> (12 hex characters) finds the record fast; <secret> (32 random URL-safe characters) proves it.
  - Only a SHA-256 hash of the secret is stored. The full key is shown ONCE, when it is created.
  - Each key belongs to one tenant and carries a list of scopes (permissions, see core/sdk/scopes.py).
  - Revoked keys stay in storage (for the audit trail) but never verify again.

Tenant = one customer organisation. Until part 2b every existing user and all existing data belong to "default".
Both live in the small-record store (KeyValueStore): kinds "apikey" and "tenant".
"""
import hashlib
import re
import secrets
import threading
import time
from typing import Iterable, Optional

from core.sdk.scopes import CORE_SCOPES

KEY_PREFIX = "vrk_"
KEY_RE = re.compile(r"^vrk_([0-9a-f]{12})\.([A-Za-z0-9_-]{20,64})$")
TENANT_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
DEFAULT_TENANT = "default"
LAST_USED_EVERY = 300          # seconds: write last_used at most this often (a write per request would be costly)


def _sha(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


PACK_SCOPES: dict = {}          # scopes defined by loaded packs ("<pack_id>:<action>"), added by the loader


def allow_pack_scopes(scopes: dict) -> None:
    PACK_SCOPES.update(scopes)


def known_scopes() -> dict:
    return {**CORE_SCOPES, **PACK_SCOPES}


def check_scopes(scopes: Iterable[str]) -> list:
    out = []
    for s in scopes:
        s = str(s).strip()
        if s not in CORE_SCOPES and s not in PACK_SCOPES:
            raise ValueError(f"unknown scope {s!r}; allowed: {sorted(known_scopes())}")
        if s not in out:
            out.append(s)
    if not out:
        raise ValueError("a key needs at least one scope")
    return out


class Tenants:
    def __init__(self, kv):
        self.kv = kv

    def ensure_default(self) -> dict:
        t = self.kv.kv_get("tenant", DEFAULT_TENANT)
        if not t:
            t = {"id": DEFAULT_TENANT, "name": "Default", "created": time.time()}
            self.kv.kv_put("tenant", DEFAULT_TENANT, t)
        return t

    def get(self, tenant_id: str) -> Optional[dict]:
        return self.kv.kv_get("tenant", tenant_id) if tenant_id else None

    def list(self) -> list:
        return sorted(self.kv.kv_list("tenant"), key=lambda t: t["id"])

    def create(self, tenant_id: str, name: str) -> dict:
        tenant_id = str(tenant_id or "").strip().lower()
        if not TENANT_RE.match(tenant_id):
            raise ValueError("tenant id: 2-40 characters, lower-case letters, digits and '-', starting with a letter")
        if self.get(tenant_id):
            raise ValueError(f"tenant {tenant_id!r} already exists")
        t = {"id": tenant_id, "name": str(name or tenant_id).strip()[:80], "created": time.time()}
        self.kv.kv_put("tenant", tenant_id, t)
        return t


class ApiKeys:
    def __init__(self, kv, tenants: Tenants):
        self.kv, self.tenants = kv, tenants
        self._lock = threading.Lock()

    def create(self, name: str, scopes: Iterable[str], tenant_id: str = DEFAULT_TENANT) -> tuple:
        """-> (public record, full key). The full key is returned only here - store it safely."""
        scopes = check_scopes(scopes)
        if not self.tenants.get(tenant_id):
            raise ValueError(f"unknown tenant {tenant_id!r}")
        kid, secret = secrets.token_hex(6), secrets.token_urlsafe(24)
        rec = {"id": kid, "name": str(name or "key").strip()[:60], "tenant_id": tenant_id, "scopes": scopes,
               "secret_sha": _sha(secret), "created": time.time(), "last_used": None, "revoked": None}
        self.kv.kv_put("apikey", kid, rec)
        return self.public(rec), f"{KEY_PREFIX}{kid}.{secret}"

    def verify(self, key: str) -> Optional[dict]:
        """The key's record if it is valid and not revoked, else None. Constant-time comparison."""
        m = KEY_RE.match(str(key or "").strip())
        if not m:
            return None
        rec = self.kv.kv_get("apikey", m.group(1))
        if not rec or rec.get("revoked") or not secrets.compare_digest(rec.get("secret_sha", ""), _sha(m.group(2))):
            return None
        now = time.time()
        if not rec.get("last_used") or now - rec["last_used"] > LAST_USED_EVERY:
            with self._lock:
                rec = dict(rec, last_used=now)
                self.kv.kv_put("apikey", rec["id"], rec)
        return rec

    def list(self, tenant_id: Optional[str] = None) -> list:
        return [self.public(r) for r in self.kv.kv_list("apikey") if tenant_id in (None, r.get("tenant_id"))]

    def revoke(self, key_id: str) -> bool:
        rec = self.kv.kv_get("apikey", key_id)
        if not rec or rec.get("revoked"):
            return False
        self.kv.kv_put("apikey", key_id, dict(rec, revoked=time.time()))
        return True

    @staticmethod
    def public(rec: dict) -> dict:
        return {k: v for k, v in rec.items() if k != "secret_sha"}

    @staticmethod
    def looks_like_key(token: str) -> bool:
        return str(token or "").startswith(KEY_PREFIX)

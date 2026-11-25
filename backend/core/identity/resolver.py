"""Identity - answers "who is calling, for which tenant, with which scopes?" for every request (ADR 0003).

One place for every kind of login, so endpoints check SCOPES instead of each re-implementing token rules:

  caller sends                                  -> principal                         scopes
  Authorization: Bearer vrk_<id>.<secret>       -> kind "service", the key's tenant  the key's scopes
  Authorization: Bearer <MCP_TOKEN>             -> kind "service", default tenant    SERVICE_SCOPES
  X-VR-Token: <ADMIN_TOKEN> / an admin user     -> kind "user", default tenant       {"admin"} (= everything)
  X-VR-Token: <user token>                      -> kind "user", default tenant       USER_SCOPES
  nothing, on an unprotected local server       -> kind "anonymous"                  {"admin"} (local development)
  nothing, on a protected server                -> kind "anonymous"                  {"knowledge:read"} (public docs)

A key that LOOKS like an API key but is wrong or revoked is refused (401) - it never falls back to anonymous,
so a typo cannot silently turn into "logged out" behaviour.

The old Auth object is passed in (core/ must not import old modules, ADR 0001).
"""
import os
import secrets
from typing import Optional, Tuple

from core.sdk.context import PolicyDenied, Principal

from .keys import DEFAULT_TENANT, ApiKeys

USER_SCOPES = frozenset({"knowledge:read", "ai:ask", "ai:vision", "ai:speech"})
SERVICE_SCOPES = frozenset({"knowledge:read", "ai:ask", "ai:vision"})        # what MCP_TOKEN could always do


class Identity:
    def __init__(self, keys: ApiKeys, auth, env=None):
        """auth: the backend's Auth (users, ADMIN_TOKEN, local/protected checks)."""
        self.keys, self.auth, self.env = keys, auth, env if env is not None else os.environ

    def _bearer(self, req) -> str:
        h = req.headers.get("authorization", "")
        return h[7:].strip() if h.startswith("Bearer ") else ""

    def resolve(self, req) -> Tuple[Principal, Optional[dict]]:
        """-> (principal, user record or None). The user record is what document permissions (groups) use."""
        bearer, token = self._bearer(req), self.auth.token_of(req)
        for cand in (bearer, token):
            if self.keys.looks_like_key(cand):
                rec = self.keys.verify(cand)
                if not rec:
                    raise PolicyDenied("invalid or revoked API key", 401)
                return Principal(tenant_id=rec["tenant_id"], kind="service", id=rec["id"], name=rec["name"],
                                 scopes=frozenset(rec["scopes"])), None
        mcp = self.env.get("MCP_TOKEN", "")
        if mcp and bearer and secrets.compare_digest(bearer, mcp):
            return Principal(tenant_id=DEFAULT_TENANT, kind="service", id="mcp-token", name="MCP_TOKEN",
                             scopes=SERVICE_SCOPES), None
        user = self.auth.user_of(req)
        if self.auth.is_admin(req):
            kind = "user" if (user or token) else "anonymous"
            return Principal(tenant_id=DEFAULT_TENANT, kind=kind, id=(user or {}).get("id", "admin"),
                             name=(user or {}).get("name", "admin"), groups=tuple((user or {}).get("groups", [])),
                             scopes=frozenset({"admin"})), user
        if user:
            return Principal(tenant_id=DEFAULT_TENANT, kind="user", id=user["id"], name=user.get("name", ""),
                             groups=tuple(user.get("groups", [])), scopes=USER_SCOPES), user
        scopes = USER_SCOPES if not self.auth.admin_required(req) else frozenset({"knowledge:read"})
        return Principal(tenant_id=DEFAULT_TENANT, kind="anonymous", scopes=scopes), None

    def require(self, req, scope: str) -> Tuple[Principal, Optional[dict]]:
        """Resolve and check one scope. 401 when nobody is logged in, 403 when logged in without the scope."""
        p, user = self.resolve(req)
        if not p.has(scope):
            if p.kind == "anonymous":
                raise PolicyDenied("unauthorized", 401)
            raise PolicyDenied(f"this key or user lacks the scope {scope!r}", 403)
        return p, user

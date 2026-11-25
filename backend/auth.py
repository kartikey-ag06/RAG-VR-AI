"""
auth.py - simple users + document-level permissions (RBAC).

Off by default (everyone sees every document, like before). It switches on as soon as one user exists:
  - a document with no groups is visible to everyone;
  - a document with groups (e.g. ["maintenance", "plant-2"]) is visible only to users in one of those groups;
  - role "admin" sees everything and may change documents, users and connectors.
Users send their token in the X-VR-Token header (or ?token=). Tokens are stored only as SHA-256 hashes.

ADMIN_TOKEN (environment variable) is an extra admin key for servers on the internet (Render).
"""
import hashlib
import os
import secrets
import time

from core.data.tenancy import current_tenant, doc_tenant

LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


def _h(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Auth:
    def __init__(self, kv):
        self.kv = kv
        self._cache = (0, [])

    def users(self):
        t, us = self._cache
        if time.time() - t > 5:
            us = self.kv.kv_list("user")
            self._cache = (time.time(), us)
        return us

    @property
    def enabled(self):
        return bool(self.users())

    def token_of(self, req):
        return (req.headers.get("x-vr-token") or req.query_params.get("token") or "").strip()

    def user_of(self, req):
        tok = self.token_of(req)
        if not tok:
            return None
        h = _h(tok)
        return next((u for u in self.users() if u.get("token_sha") == h), None)

    def can_see(self, doc_groups, user):
        if not self.enabled or not doc_groups:
            return True
        if not user:
            return False
        return user.get("role") == "admin" or bool(set(user.get("groups", [])) & set(doc_groups))

    def can_see_doc(self, entry, user):
        """Document permission: same TENANT as the current request (Phase 4) AND the group rule above."""
        if not entry or doc_tenant(entry) != current_tenant():
            return False
        return self.can_see(entry.get("groups", []), user)

    def is_local(self, req):
        if not req or not req.client:
            return False
        host = req.client.host or ""
        xff = req.headers.get("x-forwarded-for")
        if xff:
            client_ip = xff.split(",")[0].strip()
            if client_ip not in LOCAL:
                return False
        return host in LOCAL

    def is_admin(self, req):
        tok = self.token_of(req)
        admin_env = os.environ.get("ADMIN_TOKEN", "")
        if admin_env and tok and secrets.compare_digest(tok, admin_env):
            return True
        u = self.user_of(req)
        if u and u.get("role") == "admin":
            return True
        # no users and no admin key configured: open ONLY when strictly local
        return not self.enabled and not admin_env and self.is_local(req)

    def admin_required(self, req=None):
        """True when this server is protected (users/ADMIN_TOKEN exist, running on Render, or remote request)."""
        if self.enabled or bool(os.environ.get("ADMIN_TOKEN")) or bool(os.environ.get("RENDER")):
            return True
        if req is not None and not self.is_local(req):
            return True
        return False

    def require_user(self, req):
        """True if the caller is an authenticated user or admin, or if server is running unprotected locally."""
        if not self.admin_required(req):
            return True
        return bool(self.user_of(req) or self.is_admin(req))

    def create_user(self, name, groups, role="user"):
        token = secrets.token_urlsafe(18)
        uid = secrets.token_hex(6)
        rec = {"id": uid, "name": name[:60], "groups": [str(g).strip()[:40] for g in groups if str(g).strip()][:20],
               "role": "admin" if role == "admin" else "user", "token_sha": _h(token), "created": time.time()}
        self.kv.kv_put("user", uid, rec)
        self._cache = (0, [])
        return rec, token

    def delete_user(self, uid):
        self.kv.kv_delete("user", uid)
        self._cache = (0, [])

    def public(self, u):
        return {k: v for k, v in u.items() if k != "token_sha"}

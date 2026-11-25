"""The current tenant of a request, and which record kinds belong to a tenant (Phase 4 part 2b, ADR 0002).

A small ASGI middleware (core.identity.middleware) sets the tenant once per request from the caller's identity.
The data layer reads it here, so feature modules need no changes: a reminder saved during a request from tenant
"acme" lands in acme's records, and a list from tenant "default" never returns it.

Python contextvars are per request: FastAPI copies them into the worker threads that run plain `def` endpoints.
Code outside any request (start-up, background indexing) sees DEFAULT_TENANT.
"""
import re
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_TENANT = "default"
# login / system records: needed BEFORE the tenant is known, or keyed by a globally unique id
GLOBAL_KINDS = frozenset({"user", "apikey", "tenant", "embsig",                 # logins, embedding cache
                          "config", "config_history", "module_state", "audit"})       # control plane (Phase 4)
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")

_current = ContextVar("vr_rag_tenant", default=DEFAULT_TENANT)


def current_tenant() -> str:
    return _current.get()


@contextmanager
def use_tenant(tenant_id: str):
    """Run a block as `tenant_id` (tests, admin tools, background jobs for one tenant)."""
    if not TENANT_ID_RE.match(tenant_id or ""):
        raise ValueError(f"bad tenant id {tenant_id!r}")
    token = _current.set(tenant_id)
    try:
        yield tenant_id
    finally:
        _current.reset(token)


def set_tenant(tenant_id: str):
    """Low level, for the middleware: returns a token for reset_tenant()."""
    return _current.set(tenant_id if TENANT_ID_RE.match(tenant_id or "") else DEFAULT_TENANT)


def reset_tenant(token) -> None:
    _current.reset(token)


def check_kind(kind: str) -> str:
    """Record kinds become file names (local) and keys (Postgres): only simple lower-case names are allowed."""
    if not KIND_RE.match(str(kind)):
        raise ValueError(f"bad record kind {kind!r}")
    return kind


def tenant_for_kind(kind: str) -> str:
    """The tenant a record of this kind is stored under: global kinds always under DEFAULT_TENANT."""
    return DEFAULT_TENANT if check_kind(kind) in GLOBAL_KINDS else current_tenant()


def doc_tenant(entry: dict) -> str:
    """Documents uploaded before Phase 4 have no tenant_id: they belong to DEFAULT_TENANT."""
    return (entry or {}).get("tenant_id") or DEFAULT_TENANT

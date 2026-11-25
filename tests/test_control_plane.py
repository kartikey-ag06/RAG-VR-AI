"""Phase 4 control plane: config store, modules on/off per tenant, AI budget per tenant, audit log, prompts."""
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_v1
import app as backend
from core.ai.prompts import PromptRegistry
from core.config import ConfigError, ConfigStore
from core.data import LocalKV
from core.data.tenancy import use_tenant
from core.modules import Module, ModuleRegistry
from core.observability import AuditLog
from core.sdk.context import BudgetExceeded

client = TestClient(backend.app)
ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"X-VR-Token": "admin-secret"}


@pytest.fixture
def kv(tmp_path):
    return LocalKV(tmp_path)


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    yield ADMIN


@pytest.fixture
def acme():
    tenants = backend.SERVICES.get("tenants")
    if not tenants.get("acme"):
        tenants.create("acme", "Acme Ltd")
    yield "acme"
    for m in backend.MODULES.modules:                                   # switch everything back on
        backend.MODULES.set_enabled(m, "acme", True)
    for key in ("limits.ai_daily", "ai.chat_model", "prompts.answer.system"):
        backend.CONFIG.unset(key, "acme")


def key_for(tenant, scopes, admin=ADMIN):
    r = client.post("/api/core/keys", json={"name": "cp-test", "scopes": scopes, "tenant_id": tenant}, headers=admin)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["secret"]}


# ---------------------------------------------------------------- ConfigStore (unit)
def test_config_scopes_narrowest_wins(kv):
    c = ConfigStore(kv)
    c.declare("search.top_k", "integer", 5, "results", min=1, max=50)
    assert c.get("search.top_k", "t1") == 5                                  # schema default
    c.set("search.top_k", 8)
    assert c.get("search.top_k", "t1") == 8                                  # global
    c.set("search.top_k", 12, "t1")
    assert c.get("search.top_k", "t1") == 12 and c.get("search.top_k", "t2") == 8    # tenant
    c.set("search.top_k", 20, "t1", "meetings")
    assert c.get("search.top_k", "t1", "meetings") == 20 and c.get("search.top_k", "t1") == 12
    assert c.unset("search.top_k", "t1") and c.get("search.top_k", "t1") == 8
    with use_tenant("t2"):
        assert c.get("search.top_k") == 8                                    # tenant comes from the request


def test_config_validates_and_versions(kv):
    c = ConfigStore(kv)
    c.declare("limits.calls", "integer", 0, min=0)
    c.declare("feature.on", "boolean", False)
    for bad in [("limits.calls", "10"), ("limits.calls", -1), ("limits.calls", True), ("feature.on", 1),
                ("nope.key", 1)]:
        with pytest.raises(ConfigError):
            c.set(*bad)
    with pytest.raises(ConfigError):
        c.set("limits.calls", 1, None, "meetings")                           # module needs a tenant
    with pytest.raises(ConfigError):
        c.set("limits.calls", 1, "Bad Tenant!")
    with pytest.raises(ConfigError):
        c.declare("NoDots", "string")
    assert c.set("limits.calls", 1, actor="me")["version"] == 1
    assert c.set("limits.calls", 2, actor="me")["version"] == 2
    h = c.history("limits.calls")
    assert [(x["old"], x["new"]) for x in h] == [(1, 2), (None, 1)] and h[0]["by"] == "me"


# ---------------------------------------------------------------- ModuleRegistry (unit)
def test_module_paths_and_switch(kv):
    r = ModuleRegistry(kv)
    r.add(Module("assistant", "A", "capability", ("/api/calendar", "/api/notes")))
    r.add(Module("calendar_sync", "C", "capability", ("/api/calendar/sources",)))
    assert r.module_for_path("/api/calendar").id == "assistant"
    assert r.module_for_path("/api/calendar/sources/x").id == "calendar_sync"    # longest prefix wins
    assert r.module_for_path("/api/notesX") is None                              # only at a '/' boundary
    assert r.module_for_path("/api/v1/ask") is None
    with pytest.raises(ValueError):
        r.add(Module("other", "O", "capability", ("/api/notes",)))               # a prefix has one owner
    assert r.enabled("assistant", "t1")                                          # ON by default
    r.set_enabled("assistant", "t1", False)
    assert not r.enabled("assistant", "t1") and r.enabled("assistant", "t2")
    with pytest.raises(ValueError):
        r.set_enabled("nope", "t1", False)


# ---------------------------------------------------------------- AuditLog + PromptRegistry (unit)
def test_audit_drops_secrets(kv):
    a = AuditLog(kv)
    a.record("apikey.create", "admin:x", "k1", "t1", {"name": "n", "secret": "vrk_x.y", "api_key": "z",
                                                       "database_url": "postgres://u:p@h", "note": "x" * 500})
    a.record("module.disable", "admin:x", "meetings", "t2")
    e = a.list("t1")[0]
    assert set(e["details"]) == {"name", "note"} and len(e["details"]["note"]) < 210
    assert [x["action"] for x in a.list()] == ["module.disable", "apikey.create"]      # newest first
    assert [x["target"] for x in a.list(action="module.disable")] == ["meetings"]
    assert not hasattr(a, "delete") and not hasattr(a, "update")                       # append-only


def test_prompt_registry_per_tenant(kv):
    c = ConfigStore(kv)
    p = PromptRegistry(c)
    p.register("answer.system", "DEFAULT")
    c.set("prompts.answer.system", "ACME TEXT", "acme")
    assert p.get("answer.system", "acme") == "ACME TEXT" and p.get("answer.system", "other") == "DEFAULT"


def test_prompt_defaults_unchanged():
    """Defaults are the exact texts from before (the v1_ask_messages golden checks the full prompt too)."""
    assert backend.CONFIG.schema["prompts.answer.system"]["default"] == api_v1.SMART_SYSTEM
    assert backend.CONFIG.schema["prompts.answer.vision_rule"]["default"] == api_v1.VISION_RULE


# ---------------------------------------------------------------- the real app: control-plane endpoints
def test_new_endpoints_are_admin_only(protected):
    for method, url in [("get", "/api/core/config"), ("get", "/api/core/packs"), ("get", "/api/core/audit"),
                        ("get", "/api/core/config/history"), ("get", "/api/core/tenants/default/limits")]:
        assert getattr(client, method)(url).status_code == 401, url
    reader = key_for("default", ["knowledge:read"])
    assert client.get("/api/core/audit", headers=reader).status_code == 403
    assert client.put("/api/core/tenants/default/packs/meetings", json={"enabled": False},
                      headers=reader).status_code == 403
    assert backend.MODULES.enabled("meetings", "default")


def test_config_endpoint(protected, acme):
    r = client.put("/api/core/config", json={"key": "ai.chat_model", "value": "llama-test", "tenant_id": acme},
                   headers=ADMIN)
    assert r.status_code == 200 and r.json()["config"]["scope"] == "tenant:acme"
    assert backend.CONFIG.get("ai.chat_model", acme) == "llama-test"
    assert backend.CONFIG.get("ai.chat_model", "default") == ""
    got = client.get("/api/core/config", params={"tenant_id": acme}, headers=ADMIN).json()
    assert "limits.ai_daily" in got["schema"] and any(v["id"] == "tenant:acme|ai.chat_model" for v in got["values"])
    for body, status in [({"key": "ai.chat_model", "value": 5}, 400), ({"key": "no.such", "value": 1}, 400),
                         ({"key": "ai.chat_model"}, 400),
                         ({"key": "ai.chat_model", "value": "x", "tenant_id": "ghost"}, 404)]:
        assert client.put("/api/core/config", json=body, headers=ADMIN).status_code == status, body
    assert client.delete("/api/core/config", params={"key": "ai.chat_model", "tenant_id": acme},
                         headers=ADMIN).json() == {"removed": True}
    hist = client.get("/api/core/config/history", params={"key": "ai.chat_model"}, headers=ADMIN).json()["history"]
    assert hist[0]["removed"] and hist[1]["new"] == "llama-test"


def test_module_gate_per_tenant(protected, acme):
    acme_key = key_for(acme, ["knowledge:read"])
    assert client.get("/api/notes", headers=acme_key).status_code != 404
    r = client.put(f"/api/core/tenants/{acme}/packs/assistant", json={"enabled": False}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["pack"]["enabled"] is False
    off = client.get("/api/notes", headers={**acme_key, "Origin": "http://localhost:5173"})
    assert off.status_code == 404 and "assistant" in off.json()["detail"]
    assert off.headers.get("access-control-allow-origin")                          # CORS still on the 404
    assert client.get("/api/calendar/sources", headers=acme_key).status_code != 404  # other module, still ON
    assert client.get("/api/notes", headers=ADMIN).status_code != 404               # tenant default unaffected
    assert client.post("/api/v1/search", json={"query": "turbine"}, headers=acme_key).status_code == 200  # core
    packs = client.get("/api/core/packs", params={"tenant_id": acme}, headers=ADMIN).json()["packs"]
    assert {p["id"]: p["enabled"] for p in packs}["assistant"] is False
    assert any(p["kind"] == "pack" for p in packs)
    for url, body, status in [(f"/api/core/tenants/{acme}/packs/nope", {"enabled": False}, 404),
                              ("/api/core/tenants/ghost/packs/assistant", {"enabled": False}, 404),
                              (f"/api/core/tenants/{acme}/packs/assistant", {"enabled": "no"}, 400)]:
        assert client.put(url, json=body, headers=ADMIN).status_code == status, url


def test_every_module_prefix_is_a_real_route():
    """A typo in a prefix would make the switch do nothing - every prefix must match at least one route."""
    paths = [getattr(r, "path", "") for r in backend.app.routes]
    for m in backend.MODULES.modules.values():
        for p in m.prefixes:
            if p == f"/api/v1/packs/{m.id}":
                continue                       # reserved for the module's SDK routes; may still be empty
            assert any(x == p or x.startswith(p + "/") for x in paths), f"{m.id}: no route under {p}"


def test_core_routes_are_never_gated():
    for path in ["/api/v1/ask", "/api/v1/search", "/api/search", "/api/files", "/api/core/keys", "/mcp",
                 "/api/admin/users", "/api/status", "/healthz", "/rag/index.json"]:
        assert backend.MODULES.module_for_path(path) is None, path


# ---------------------------------------------------------------- per-tenant AI budget
def test_budget_counts_per_tenant(monkeypatch):
    backend.CONFIG.set("limits.ai_daily", 2, "acme")
    try:
        with use_tenant("acme"):
            backend.AI.spend()
            backend.AI.spend()
            with pytest.raises(BudgetExceeded) as e:
                backend.AI.spend()
            assert e.value.status == 429
        with use_tenant("default"):
            for _ in range(5):
                backend.AI.spend()                                            # no limit for default
    finally:
        backend.CONFIG.unset("limits.ai_daily", "acme")


def test_budget_on_routes(protected, acme, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    r = client.put(f"/api/core/tenants/{acme}/limits", json={"ai_daily": 1}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["limits"] == {"ai_daily": 1}
    assert client.put(f"/api/core/tenants/{acme}/limits", json={"ai_daily": -1}, headers=ADMIN).status_code == 400
    assert client.put(f"/api/core/tenants/{acme}/limits", json={"other": 1}, headers=ADMIN).status_code == 400
    backend.AI._spent[(acme, time.strftime("%Y-%m-%d", time.gmtime()))] = 1           # today's call is used
    asker = key_for(acme, ["knowledge:read", "ai:ask"])
    out = client.post("/api/v1/ask", json={"question": "turbine power unit"}, headers=asker).json()
    assert out["mode"] == "offline" and "daily AI limit" in out["note"]               # still answers from docs
    backend.CONFIG.set("limits.ai_daily", 1, "default")          # /api/grok: ADMIN_TOKEN works in tenant default
    try:
        backend.AI._spent[("default", time.strftime("%Y-%m-%d", time.gmtime()))] = 1
        g = client.post("/api/grok", json={"messages": [{"role": "user", "content": "hi"}]}, headers=ADMIN)
        assert g.status_code == 429 and g.headers.get("retry-after") and "daily AI limit" in g.json()["error"]
    finally:
        backend.CONFIG.unset("limits.ai_daily", "default")


# ---------------------------------------------------------------- audit through the app
def test_audit_records_admin_actions(protected, acme):
    before = time.time()
    key_for(acme, ["knowledge:read"])
    client.put(f"/api/core/tenants/{acme}/packs/meetings", json={"enabled": False}, headers=ADMIN)
    client.put("/api/core/config", json={"key": "limits.ai_daily", "value": 3, "tenant_id": acme}, headers=ADMIN)
    events = client.get("/api/core/audit", params={"tenant_id": acme}, headers=ADMIN).json()["events"]
    new = [e for e in events if e["ts"] >= before]
    assert {"apikey.create", "module.disable", "config.set"} <= {e["action"] for e in new}
    assert all(e["actor"] not in ("unknown", "") and "anonymous" not in e["actor"] for e in new), new
    blob = str(events)
    assert "vrk_" not in blob and "admin-secret" not in blob                         # never secrets


def test_no_deprecated_startup_hook():
    src = (ROOT / "backend" / "app.py").read_text("utf-8")
    assert not re.search(r"@app\.on_event", src)
    assert backend.app.router.lifespan_context is not None


def test_documents_audited_and_tenant_checked(protected, acme):
    up = client.post("/api/files", files={"files": ("cp-audit.txt", b"control plane audit sample text " * 30,
                                                    "text/plain")}, headers=ADMIN)
    assert up.status_code == 200, up.text
    uid = up.json()["added"][0]["uid"]
    t0 = time.time()                                         # let indexing finish before deleting (no race noise)
    while time.time() - t0 < 60 and not (backend.STORE.state == "idle" and
                                          any(d["uid"] == uid for d in backend.STORE.snapshot["index"]["docs"])):
        time.sleep(0.1)
    try:
        acme_admin = key_for(acme, ["admin", "knowledge:read", "knowledge:write"])
        assert client.delete(f"/api/files/{uid}", headers=acme_admin).status_code in (403, 404)   # not acme's
        assert client.patch(f"/api/files/{uid}", json={"title": "hacked"}, headers=acme_admin).status_code in (403, 404)
        assert backend.STORE.doc_entry(uid)["title"] != "hacked"
        assert client.delete(f"/api/files/{uid}", headers=ADMIN).status_code == 200
    finally:
        backend.STORE.delete(uid)
    acts = {(e["action"], e["target"]) for e in backend.AUDIT.list("default", limit=50)}
    assert ("document.upload", uid) in acts and ("document.delete", uid) in acts

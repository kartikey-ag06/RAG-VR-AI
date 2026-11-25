"""Phase 7 validation: Area A (Central control) & Area B (Cross-tenant + cross-pack isolation)."""
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as backend
from core.data.tenancy import use_tenant
from core.sdk.context import PolicyDenied

client = TestClient(backend.app)
ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"X-VR-Token": "admin-secret"}


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
    for mod in ("industrial_field_ops", "demo_pack"):
        if mod in backend.MODULES.modules:
            backend.MODULES.set_enabled(mod, "acme", True)
            backend.MODULES.set_enabled(mod, "default", True)


def key_for(tenant, scopes):
    r = client.post("/api/core/keys", json={"name": "phase7", "scopes": scopes, "tenant_id": tenant}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["secret"]}


# ================================================================ A. Central control
def test_config_rollback_end_to_end(protected, acme):
    """Set v1 -> v2 -> rollback to v1: value is v1, history records rollback, audit + event emitted."""
    key = "limits.ai_daily"
    try:
        # Non-admin cannot call rollback
        non_admin = key_for(acme, ["knowledge:read"])
        assert client.post("/api/core/config/rollback", json={"key": key, "tenant_id": acme, "version": 1},
                           headers=non_admin).status_code == 403
        assert client.post("/api/core/config/rollback", json={"key": key, "tenant_id": acme, "version": 1}).status_code == 401

        r1 = client.put("/api/core/config", json={"key": key, "value": 10, "tenant_id": acme}, headers=ADMIN)
        assert r1.status_code == 200
        v1 = r1.json()["config"]["version"]

        r2 = client.put("/api/core/config", json={"key": key, "value": 99, "tenant_id": acme}, headers=ADMIN)
        assert r2.status_code == 200 and r2.json()["config"]["version"] == v1 + 1
        assert backend.CONFIG.get(key, acme) == 99

        before_ts = time.time()
        rb = client.post("/api/core/config/rollback", json={"key": key, "tenant_id": acme, "version": v1}, headers=ADMIN)
        assert rb.status_code == 200, rb.text
        cfg = rb.json()["config"]
        assert cfg["value"] == 10 and cfg["version"] == v1 + 2
        assert backend.CONFIG.get(key, acme) == 10

        # History shows the rollback
        hist = client.get("/api/core/config/history", params={"key": key, "limit": 20}, headers=ADMIN).json()["history"]
        latest = hist[0]
        assert latest["old"] == 99 and latest["new"] == 10 and latest.get("rollback_to") == v1

        # Audit log has config.rollback
        audit = client.get("/api/core/audit", params={"tenant_id": acme, "action": "config.rollback"}, headers=ADMIN).json()["events"]
        assert any(e["ts"] >= before_ts and e["details"].get("restored_version") == v1 for e in audit)

        # Event config.changed was emitted (never containing the value)
        assert backend.BUS.flush(5)
        ev = [e for e in backend.BUS.recent(acme) if e["name"] == "config.changed" and e["data"].get("key") == key]
        assert ev and "value" not in ev[0]["data"]

        # Invalid rollback inputs fail cleanly
        assert client.post("/api/core/config/rollback", json={"key": key, "tenant_id": acme, "version": 9999},
                           headers=ADMIN).status_code == 404
        assert client.post("/api/core/config/rollback", json={"key": key, "tenant_id": acme, "version": "bad"},
                           headers=ADMIN).status_code == 400
    finally:
        backend.CONFIG.unset(key, acme)


def test_kill_switch_full_lifecycle(protected, acme):
    """Switching a pack off hides new URLs, aliases, MCP tools, events and report kinds; switching back on restores all."""
    a_key = key_for(acme, ["knowledge:read", "ai:ask"])
    d_key = key_for("default", ["knowledge:read", "ai:ask"])
    pack_url = "/api/v1/packs/industrial_field_ops/summary"

    # OFF for acme
    assert client.put(f"/api/core/tenants/{acme}/packs/industrial_field_ops",
                      json={"enabled": False}, headers=ADMIN).status_code == 200

    for url in (pack_url, "/procops", "/api/procops/summary", "/api/machines"):
        assert client.get(url, headers=a_key).status_code == 404, url
        assert client.get(url, headers=d_key).status_code == 200, f"default affected at {url}"

    # MCP tools hidden for acme, still present for default
    acme_tools = [t["name"] for t in client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                                 headers=a_key).json()["result"]["tools"]]
    def_tools = [t["name"] for t in client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                                headers=d_key).json()["result"]["tools"]]
    assert not any(n.startswith("industrial_field_ops") for n in acme_tools)
    assert "industrial_field_ops_friction" in def_tools

    # Event delivery stopped for acme
    with use_tenant(acme):
        backend.KERNEL.journal.archive("industrial_field_ops.questions")
    client.post("/api/v1/ask", json={"question": "kill switch silent question"}, headers=a_key)
    assert backend.BUS.flush(5)
    with use_tenant(acme):
        assert backend.KERNEL.journal.list("industrial_field_ops.questions") == []

    # Report kinds hidden for acme
    acme_admin = key_for(acme, ["admin"])
    acme_kinds = {k["kind"] for k in client.get("/api/sessions/kinds", headers=acme_admin).json()["kinds"]}
    assert "summary" in acme_kinds and not ({"inspection", "incident", "workorder"} & acme_kinds)

    # ON again -> everything restored without restart
    assert client.put(f"/api/core/tenants/{acme}/packs/industrial_field_ops",
                      json={"enabled": True}, headers=ADMIN).status_code == 200
    for url in (pack_url, "/procops", "/api/procops/summary", "/api/machines"):
        assert client.get(url, headers=a_key).status_code == 200, f"not restored: {url}"
    restored_tools = [t["name"] for t in client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                                     headers=a_key).json()["result"]["tools"]]
    assert "industrial_field_ops_friction" in restored_tools
    restored_kinds = [k["kind"] for k in client.get("/api/sessions/kinds", headers=acme_admin).json()["kinds"]]
    assert {"inspection", "incident", "workorder"} <= set(restored_kinds)
    client.post("/api/v1/ask", json={"question": "after restore question"}, headers=a_key)
    assert backend.BUS.flush(5)
    with use_tenant(acme):
        qs = [e["q"] for e in backend.KERNEL.journal.list("industrial_field_ops.questions")]
        assert "after restore question" in qs


def test_broken_pack_isolated_and_shown_in_admin(protected):
    """broken_pack (register() raises) never stops server; its URLs return 404; /admin & /api/core/packs show error."""
    assert client.get("/healthz").status_code == 200
    assert client.post("/api/v1/search", json={"query": "insider trading"}).status_code == 200
    assert client.get("/api/v1/packs/broken_pack/anything", headers=ADMIN).status_code == 404

    packs = client.get("/api/core/packs", headers=ADMIN).json()["packs"]
    bp = next(p for p in packs if p["id"] == "broken_pack")
    assert bp["state"] == "failed" and "boom at start-up" in bp["error"]
    one = client.get("/api/core/packs/broken_pack", headers=ADMIN)
    assert one.status_code == 200 and one.json()["state"] == "failed"
    assert client.get("/admin", headers=ADMIN).status_code == 200


# ================================================================ B. Isolation
def test_cross_tenant_full_isolation(protected, acme):
    """Tenant default cannot see acme's docs, search hits, citations, kv, journal, heatmap, MCP, index.json, or audit."""
    token = "zxqacmeunique991"
    text = (f"Acme {token} calibration procedure step one. " * 10).encode()
    entry = backend.STORE.add(f"acme-{uuid.uuid4().hex[:6]}.txt", text, tenant_id=acme)
    uid = entry["uid"]
    t0 = time.time()
    while time.time() - t0 < 30:
        if any(d["uid"] == uid for d in backend.STORE.snapshot["index"]["docs"]) and backend.STORE.state == "idle":
            break
        time.sleep(0.1)
    try:
        a_key = key_for(acme, ["knowledge:read", "ai:ask"])
        d_key = key_for("default", ["knowledge:read", "ai:ask"])

        # Seed acme kv note + acme question (which feeds journal + ProcOps heatmap)
        assert client.post("/api/v1/packs/demo_pack/notes",
                           json={"id": "acme-note-1", "text": f"secret {token}"}, headers=a_key).status_code == 200
        a_ask = client.post("/api/v1/ask", json={"question": f"what is {token}?"}, headers=a_key).json()
        assert backend.BUS.flush(5)
        assert any(c["doc_uid"] == uid for c in a_ask["citations"])

        # Default cannot see acme's doc in documents, search, or ask citations
        d_docs = client.get("/api/v1/documents", headers=d_key).json()["documents"]
        assert uid not in json.dumps(d_docs)
        d_search = client.post("/api/v1/search", json={"query": token}, headers=d_key).json()["passages"]
        assert all(p["doc_uid"] != uid for p in d_search)
        d_ask = client.post("/api/v1/ask", json={"question": token}, headers=d_key).json()
        assert all(c["doc_uid"] != uid for c in d_ask["citations"])

        # Default cannot see acme's pack kv records
        d_notes = client.get("/api/v1/packs/demo_pack/notes", headers=d_key).json()["notes"]
        assert all(n.get("id") != "acme-note-1" for n in d_notes)

        # Default cannot see acme's journal or ProcOps heatmap (acme asked "what is {token}?")
        with use_tenant("default"):
            def_journal = backend.KERNEL.journal.list("industrial_field_ops.questions")
            assert all(f"what is {token}" not in e.get("q", "").lower() for e in def_journal)
        d_summary = client.get("/api/procops/summary", headers=d_key).json()
        assert all(f"what is {token}" not in x["q"].lower() for x in d_summary["analytics"]["mostAsked"])

        # Default MCP results exclude acme docs and acme friction
        mcp_search = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                               "params": {"name": "search_documents", "arguments": {"query": token}}},
                                 headers=d_key).json()["result"]["content"][0]["text"]
        assert token not in mcp_search
        mcp_fric = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                             "params": {"name": "industrial_field_ops_friction", "arguments": {"limit": 10}}},
                               headers=d_key).json()["result"]["content"][0]["text"]
        assert token not in mcp_fric

        # /rag/index.json vocabulary for default excludes acme's unique token
        d_idx = client.get("/rag/index.json", headers=d_key).json()
        assert token not in json.dumps(d_idx).lower()

        # Non-admin key cannot read audit entries
        assert client.get("/api/core/audit", headers=d_key).status_code == 403
        assert client.get("/api/core/audit", headers=a_key).status_code == 403
    finally:
        backend.STORE.delete(uid)
        with use_tenant(acme):
            backend.KERNEL.kv.kv_delete("demo_pack.note", "acme-note-1")
            backend.KERNEL.journal.archive("industrial_field_ops.questions")


def test_cross_pack_isolation(protected):
    """A pack cannot read another pack's kv/journal, call undeclared pack functions, use undeclared scopes, or get ctx.legacy."""
    demo_ctx = backend.LOADER.contexts["demo_pack"]
    ind_ctx = backend.LOADER.contexts["industrial_field_ops"]

    # 1. kv and journal are strictly namespaced by module_id
    with use_tenant("default"):
        ind_ctx.kv.put("default", "probe", "p1", {"secret": "ind-only"})
        ind_ctx.journal.append("default", "probe", {"msg": "ind-journal"})
        try:
            assert demo_ctx.kv.get("default", "probe", "p1") is None
            assert demo_ctx.journal.list("default", "probe") == []
        finally:
            ind_ctx.kv.delete("default", "probe", "p1")
            ind_ctx.journal.archive("default", "probe")

    # 2. Cannot call a function of a pack not in depends_on
    with pytest.raises(PolicyDenied) as exc:
        demo_ctx.call("default", "other_pack.fn")
    assert exc.value.status == 403

    # 3. Cannot use a scope it did not declare in manifest
    k = key_for("default", ["knowledge:read", "ai:vision", "admin"])
    assert client.get("/api/v1/packs/demo_pack/vision", headers=k).status_code == 403

    # 4. Packs never get ctx.legacy
    assert client.get("/api/v1/packs/demo_pack/legacy", headers=k).status_code == 403

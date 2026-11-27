"""Phase 13B Gate: warehouse_qc pack auto-discovery, tenant isolation, and Phase 12 approval loop."""
import json

import pytest
from fastapi.testclient import TestClient

import app as backend
from core.data.tenancy import use_tenant
from core.sdk.events import Event
from tests.conftest import FakeResp

client = TestClient(backend.app)
ADMIN = {"X-VR-Token": "admin-secret"}


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_wqc_test")
    monkeypatch.setattr(
        backend.HTTP,
        "get",
        lambda *a, **kw: FakeResp({"data": [{"id": "llama-3.3-70b-versatile"}]}),
    )
    yield ADMIN


@pytest.fixture
def tenants_ab():
    tenants = backend.SERVICES.get("tenants")
    for tid, name in (("wqc-a", "Warehouse A"), ("wqc-b", "Warehouse B")):
        if not tenants.get(tid):
            tenants.create(tid, name)
        backend.MODULES.set_enabled("warehouse_qc", tid, True)
    yield "wqc-a", "wqc-b"
    for tid in ("wqc-a", "wqc-b"):
        backend.MODULES.set_enabled("warehouse_qc", tid, True)


def _key(tenant: str, scopes: list[str]) -> dict:
    r = client.post("/api/core/keys", json={"name": "wqc-key", "scopes": scopes, "tenant_id": tenant}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["secret"]}


# ---------------------------------------------------------------- 1. Auto-discovery & tenant kill switch
def test_warehouse_qc_autodiscovered_and_tenant_toggleable(protected, tenants_ab):
    t_a, t_b = tenants_ab
    assert "warehouse_qc" in backend.LOADER.manifests
    all_tools = backend.AGENT_TOOLS.all_tools()
    assert {"warehouse_qc_lookup_lot", "warehouse_qc_summary", "warehouse_qc_quarantine_lot"} <= set(all_tools.keys())

    hdr_a = _key(t_a, ["knowledge:read", "ai:ask"])
    hdr_b = _key(t_b, ["knowledge:read", "ai:ask"])

    assert client.get("/api/v1/packs/warehouse_qc/summary", headers=hdr_a).status_code == 200
    assert client.get("/api/v1/packs/warehouse_qc/summary", headers=hdr_b).status_code == 200

    # Disable for Tenant A only
    backend.MODULES.set_enabled("warehouse_qc", t_a, False)
    r_disabled = client.get("/api/v1/packs/warehouse_qc/summary", headers=hdr_a)
    assert r_disabled.status_code == 404

    p_a, _ = backend.IDENTITY.resolve(type("Req", (), {"headers": hdr_a, "query_params": {}})())
    avail_a = {t.name for t in backend.AGENT_TOOLS.available_for(t_a, p_a)}
    assert "warehouse_qc_lookup_lot" not in avail_a
    assert "warehouse_qc_quarantine_lot" not in avail_a

    # Tenant B remains unaffected
    assert client.get("/api/v1/packs/warehouse_qc/summary", headers=hdr_b).status_code == 200


# ---------------------------------------------------------------- 2. Event subscriber telemetry
def test_warehouse_qc_question_asked_subscriber_records_tenant_telemetry(protected, tenants_ab):
    t_a, t_b = tenants_ab
    hdr_a = _key(t_a, ["knowledge:read"])
    hdr_b = _key(t_b, ["knowledge:read"])

    backend.BUS.publish(Event("question.asked", t_a, {"question": "Check LOT-42 seal torque", "door": "v1"}))
    backend.BUS.publish(Event("question.asked", t_a, {"question": "Check LOT-42 seal torque", "door": "v1"}))
    assert backend.BUS.flush(timeout=5.0)

    sum_a = client.get("/api/v1/packs/warehouse_qc/summary", headers=hdr_a).json()
    assert sum_a["total_questions"] >= 2
    assert any(q["question"] == "Check LOT-42 seal torque" and q["count"] >= 2 for q in sum_a["top_questions"])

    sum_b = client.get("/api/v1/packs/warehouse_qc/summary", headers=hdr_b).json()
    assert not any(q["question"] == "Check LOT-42 seal torque" for q in sum_b["top_questions"])


# ---------------------------------------------------------------- 3. Phase 12 Agent + Human Approval end-to-end
def test_warehouse_qc_quarantine_requires_approval_and_isolates_tenants(protected, tenants_ab, monkeypatch):
    t_a, t_b = tenants_ab
    hdr_a = _key(t_a, ["knowledge:read", "ai:ask", "warehouse_qc:quarantine", "warehouse_qc:approve"])
    hdr_b = _key(t_b, ["knowledge:read"])

    step = {"n": 0}

    def fake_post(*a, **kw):
        step["n"] += 1
        if step["n"] == 1:
            return FakeResp({"choices": [{"message": {"content": "", "tool_calls": [
                {
                    "id": "tc_wqc_1",
                    "type": "function",
                    "function": {
                        "name": "warehouse_qc_quarantine_lot",
                        "arguments": json.dumps({"lot_id": "LOT-42", "reason": "damaged seal"}),
                    },
                }
            ]}}]})
        return FakeResp({"choices": [{"message": {"content": "LOT-42 has been quarantined."}}]})

    monkeypatch.setattr(backend.HTTP, "post", fake_post)

    # 1. Agent run pauses before executing write tool
    r1 = client.post("/api/v1/agent/run", json={"question": "Quarantine LOT-42 for damaged seal"}, headers=hdr_a)
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    assert d1["status"] == "awaiting_approval"
    assert d1["tool"] == "warehouse_qc_quarantine_lot"
    aid = d1["approval_id"]

    # LOT-42 is NOT quarantined yet
    lots_before = client.get("/api/v1/packs/warehouse_qc/lots", headers=hdr_a).json()["lots"]
    assert not any(l.get("lot_id") == "LOT-42" and l.get("status") == "quarantined" for l in lots_before)

    # 2. Human approves
    r2 = client.post(f"/api/v1/agent/approvals/{aid}/approve", headers=hdr_a)
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "completed"

    # 3. Verify quarantined in lookup_lot tool, REST /lots, and AuditLog
    with use_tenant(t_a):
        lookup_raw = backend.AGENT_TOOLS.all_tools()["warehouse_qc_lookup_lot"].handler(None, {"lot_id": "LOT-42"})
    lookup = json.loads(lookup_raw)
    assert lookup["status"] == "quarantined"
    assert lookup["reason"] == "damaged seal"

    audit_rows = backend.AUDIT.list(limit=20, tenant_id=t_a)
    assert any(row.get("action") == "agent.approved" and row.get("target") == "warehouse_qc_quarantine_lot"
               for row in audit_rows)

    # 4. Tenant B cannot see Tenant A's LOT-42 quarantine
    lots_b = client.get("/api/v1/packs/warehouse_qc/lots", headers=hdr_b).json()["lots"]
    assert not any(l.get("lot_id") == "LOT-42" for l in lots_b)

    # 5. Release LOT-42 back to active
    rel = client.post("/api/v1/packs/warehouse_qc/lots/LOT-42/release", json={"note": "re-sealed"}, headers=hdr_a)
    assert rel.status_code == 200, rel.text
    assert rel.json()["status"] == "active"

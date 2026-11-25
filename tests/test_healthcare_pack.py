"""Phase 7 validation: Area F - Second sector pack (healthcare_sop) with ZERO Core changes."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as backend
from core.data.tenancy import use_tenant

client = TestClient(backend.app)
ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"X-VR-Token": "admin-secret"}
PACK = "/api/v1/packs/healthcare_sop"


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    yield ADMIN


@pytest.fixture
def acme():
    tenants = backend.SERVICES.get("tenants")
    if not tenants.get("acme"):
        tenants.create("acme", "Acme Health")
    yield "acme"
    for t in ("acme", "default"):
        backend.MODULES.set_enabled("healthcare_sop", t, True)


@pytest.fixture
def clean_topics():
    def wipe():
        for t in ("default", "acme"):
            with use_tenant(t):
                backend.KERNEL.journal.archive("healthcare_sop.topics")
    wipe()
    yield
    wipe()


def key_for(tenant, scopes):
    r = client.post("/api/core/keys", json={"name": "hc-test", "scopes": scopes, "tenant_id": tenant}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["secret"]}


def test_healthcare_pack_loaded_sdk_only():
    st = backend.LOADER.status["healthcare_sop"]
    assert st["state"] == "loaded" and st["kind"] == "pack" and not st["legacy"]


def test_healthcare_pack_end_to_end(protected, clean_topics, monkeypatch):
    monkeypatch.setattr(backend.AI, "complete", lambda msgs, max_tokens=500, fast=False: "1. Verify compliance (p. 1).")
    k = key_for("default", ["knowledge:read", "ai:ask"])

    # Checklist endpoint: ctx.search + ctx.llm + registered prompt + config max_steps
    res = client.post(f"{PACK}/checklist", json={"topic": "insider trading"}, headers=k)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["topic"] == "insider trading" and body["max_steps"] == 6
    assert "1. Verify compliance" in body["checklist"] and isinstance(body["citations"], list)

    # Config key works per tenant
    client.put("/api/core/config", json={"key": "packs.healthcare_sop.max_steps", "value": 3, "tenant_id": "default",
                                          "module": "healthcare_sop"}, headers=ADMIN)
    try:
        res2 = client.post(f"{PACK}/checklist", json={"topic": "insider trading"}, headers=k).json()
        assert res2["max_steps"] == 3
    finally:
        backend.CONFIG.unset("packs.healthcare_sop.max_steps", "default", "healthcare_sop")

    # Pack scope healthcare_sop:signoff enforced
    assert client.post(f"{PACK}/signoff", json={"checklist_id": "1"}, headers=k).status_code == 403
    signer = key_for("default", ["healthcare_sop:signoff"])
    assert client.post(f"{PACK}/signoff", json={"checklist_id": "1"}, headers=signer).json()["signed_off"] is True

    # Report kind registered
    kinds = [x["kind"] for x in client.get("/api/sessions/kinds", headers=ADMIN).json()["kinds"]]
    assert "clinical_audit" in kinds

    # question.asked listener -> topics journal -> GET /topics + MCP tool
    client.post("/api/v1/ask", json={"question": "sterile glove protocol"}, headers=k)
    assert backend.BUS.flush(5)
    topics = client.get(f"{PACK}/topics", headers=k).json()["topics"]
    assert any(t["topic"] == "sterile glove protocol" for t in topics)

    mcp_out = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "healthcare_sop_topics", "arguments": {"limit": 5}}},
                          headers=k).json()["result"]["content"][0]["text"]
    assert "sterile glove protocol" in mcp_out


def test_healthcare_pack_tenant_isolation_and_kill_switch(protected, acme, clean_topics):
    a = key_for(acme, ["knowledge:read", "ai:ask"])
    d = key_for("default", ["knowledge:read", "ai:ask"])

    client.post("/api/v1/ask", json={"question": "acme clinical triage"}, headers=a)
    assert backend.BUS.flush(5)
    assert any(t["topic"] == "acme clinical triage" for t in client.get(f"{PACK}/topics", headers=a).json()["topics"])
    assert all(t["topic"] != "acme clinical triage" for t in client.get(f"{PACK}/topics", headers=d).json()["topics"])

    # Kill switch off for acme
    client.put(f"/api/core/tenants/{acme}/packs/healthcare_sop", json={"enabled": False}, headers=ADMIN)
    assert client.get(f"{PACK}/topics", headers=a).status_code == 404
    assert client.get(f"{PACK}/topics", headers=d).status_code == 200
    a_tools = [t["name"] for t in client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                              headers=a).json()["result"]["tools"]]
    assert "healthcare_sop_topics" not in a_tools
    acme_admin = key_for(acme, ["admin"])
    assert "clinical_audit" not in [x["kind"] for x in client.get("/api/sessions/kinds", headers=acme_admin).json()["kinds"]]


def test_core_works_without_healthcare_pack_folder(tmp_path):
    """Delete backend/packs/healthcare_sop -> Core and all other modules still start and work cleanly."""
    repo = tmp_path / "repo"
    skip = {ROOT / "backend" / "data", ROOT / "backend" / "packs" / "healthcare_sop"}

    def ignore(folder, names):
        return [n for n in names if n == "__pycache__" or Path(folder) / n in skip]

    shutil.copytree(ROOT / "backend", repo / "backend", ignore=ignore)
    assert not (repo / "backend" / "packs" / "healthcare_sop").exists()
    for name in ("docs", "glasses_app"):
        if (ROOT / name).exists():
            (repo / name).symlink_to(ROOT / name)
    data = tmp_path / "data"
    shutil.copytree(os.environ["VR_RAG_DATA_DIR"], data)
    script = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
import app as b
from fastapi.testclient import TestClient
for _ in range(600):
    if b.STORE.state == "idle" and b.STORE.snapshot["index"]["docs"]:
        break
    time.sleep(0.1)
c = TestClient(b.app)
out = {
  "pack_loaded": "healthcare_sop" in b.LOADER.status,
  "health": c.get("/healthz").status_code,
  "topics": c.get("/api/v1/packs/healthcare_sop/topics").status_code,
  "kinds": [k["kind"] for k in c.get("/api/sessions/kinds").json()["kinds"]],
  "mcp_tools": [t["name"] for t in c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]],
}
print("RESULT" + __import__("json").dumps(out))
'''
    env = {k: v for k, v in os.environ.items() if k not in ("VR_RAG_PACK_DIRS", "ADMIN_TOKEN")}
    env.update(VR_RAG_DATA_DIR=str(data), VR_RAG_NO_DB="1", VR_RAG_NO_ENVFILE="1")
    r = subprocess.run([sys.executable, "-c", script, str(repo / "backend")], cwd=repo / "backend", env=env,
                       capture_output=True, text=True, timeout=300)
    line = next((x for x in r.stdout.splitlines() if x.startswith("RESULT")), None)
    assert line, r.stdout[-2000:] + r.stderr[-3000:]
    out = json.loads(line[6:])
    assert out["pack_loaded"] is False
    assert out["health"] == 200
    assert out["topics"] == 404
    assert "clinical_audit" not in out["kinds"]
    assert "healthcare_sop_topics" not in out["mcp_tools"]

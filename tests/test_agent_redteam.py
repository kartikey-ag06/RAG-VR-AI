"""Phase 12 Red-Team Gate: 7 adversarial tests for Core Agents."""
import json

import pytest
from fastapi.testclient import TestClient

import app as backend
from core.agents import AgentTool
from tests.conftest import FakeResp

client = TestClient(backend.app)
ADMIN = {"X-VR-Token": "admin-secret"}


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_agent_test")
    yield ADMIN


@pytest.fixture
def acme():
    tenants = backend.SERVICES.get("tenants")
    if not tenants.get("acme"):
        tenants.create("acme", "Acme")
    yield "acme"
    backend.MODULES.set_enabled("industrial_field_ops", "acme", True)
    backend.MODULES.set_enabled("industrial_field_ops", "default", True)


@pytest.fixture
def write_tool():
    executed = []

    def handler(_req, args):
        executed.append(args)
        return json.dumps({"written": True, "args": args})

    tool = AgentTool(
        name="test_write_action",
        description="Mutating test tool requiring write scope and human approval.",
        parameters={"type": "object", "properties": {"target": {"type": "string"}}},
        read_only=False,
        required_scope="knowledge:write",
        module_id=None,
        handler=handler,
    )
    backend.AGENT_TOOLS.register(tool)
    yield executed
    backend.AGENT_TOOLS.unregister("test_write_action")


def _key(tenant: str, scopes: list[str]):
    r = client.post("/api/core/keys", json={"name": "agent-rt", "scopes": scopes, "tenant_id": tenant}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["secret"]}


def _mock_models(monkeypatch):
    monkeypatch.setattr(backend.HTTP, "get", lambda *a, **kw: FakeResp({"data": [{"id": "llama-3.3-70b-versatile"}]}))


# ---------------------------------------------------------------- 1. Indirect prompt injection
def test_redteam_indirect_prompt_injection_never_auto_executes_write(protected, write_tool, monkeypatch):
    _mock_models(monkeypatch)
    step = {"n": 0}

    def fake_post(url, *a, **kw):
        step["n"] += 1
        if step["n"] == 1:
            return FakeResp({"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "search_documents", "arguments": '{"query":"insider"}'}}
            ]}}]})
        # Model gets tricked by injected document text and requests test_write_action
        return FakeResp({"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "test_write_action", "arguments": '{"target":"pwned"}'}}
        ]}}]})

    monkeypatch.setattr(backend.HTTP, "post", fake_post)
    k = _key("default", ["knowledge:read", "knowledge:write", "ai:ask"])
    r = client.post("/api/v1/agent/run", json={"question": "summarize doc"}, headers=k)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "awaiting_approval" and body["tool"] == "test_write_action"
    assert write_tool == []                                   # NEVER auto-executed


# ---------------------------------------------------------------- 2. Loop exhaustion / hard step limit
def test_redteam_loop_exhaustion_stops_at_max_steps(protected, monkeypatch):
    _mock_models(monkeypatch)
    backend.CONFIG.set("limits.ai_daily", 20, "default")

    def always_tool(url, *a, **kw):
        return FakeResp({"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "list_documents", "arguments": "{}"}}
        ]}}]})

    monkeypatch.setattr(backend.HTTP, "post", always_tool)
    try:
        k = _key("default", ["knowledge:read", "ai:ask"])
        r = client.post("/api/v1/agent/run", json={"question": "loop forever", "max_steps": 3}, headers=k)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "step_limit_reached"
        assert len(body["steps"]) == 3
        assert backend.AI.ledger.daily_summary("default")["calls"] == 3
    finally:
        backend.CONFIG.unset("limits.ai_daily", "default")


# ---------------------------------------------------------------- 3. Cross-tenant approval hijack
def test_redteam_cross_tenant_approval_hijack_blocked(protected, acme, write_tool, monkeypatch):
    _mock_models(monkeypatch)

    def req_write(url, *a, **kw):
        return FakeResp({"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "test_write_action", "arguments": '{"target":"t1"}'}}
        ]}}]})

    monkeypatch.setattr(backend.HTTP, "post", req_write)
    k_def = _key("default", ["knowledge:read", "knowledge:write", "ai:ask"])
    aid = client.post("/api/v1/agent/run", json={"question": "do write"}, headers=k_def).json()["approval_id"]

    k_acme_admin = _key(acme, ["admin"])
    assert client.get(f"/api/v1/agent/approvals/{aid}", headers=k_acme_admin).status_code == 404
    assert client.post(f"/api/v1/agent/approvals/{aid}/approve", headers=k_acme_admin).status_code == 404
    assert client.post(f"/api/v1/agent/approvals/{aid}/reject", headers=k_acme_admin).status_code == 404
    assert write_tool == []


# ---------------------------------------------------------------- 4. Scope escalation blocked + audited
def test_redteam_scope_escalation_blocked_and_audited(protected, write_tool, monkeypatch):
    _mock_models(monkeypatch)

    def req_write(url, *a, **kw):
        return FakeResp({"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "test_write_action", "arguments": '{"target":"x"}'}}
        ]}}]})

    monkeypatch.setattr(backend.HTTP, "post", req_write)
    k_read = _key("default", ["knowledge:read", "ai:ask"])    # lacks knowledge:write
    r = client.post("/api/v1/agent/run", json={"question": "escalate"}, headers=k_read)
    assert r.status_code == 403
    assert write_tool == []
    events = client.get("/api/core/audit", params={"action": "agent.tool_denied"}, headers=ADMIN).json()["events"]
    assert any(e["target"] == "test_write_action" for e in events)


# ---------------------------------------------------------------- 5. Disabled-pack kill switch
def test_redteam_disabled_pack_blocks_tool_for_tenant(protected, acme, monkeypatch):
    _mock_models(monkeypatch)
    backend.MODULES.set_enabled("industrial_field_ops", acme, False)

    def call_ind(url, *a, **kw):
        return FakeResp({"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "industrial_field_ops_friction", "arguments": "{}"}}
        ]}}]})

    monkeypatch.setattr(backend.HTTP, "post", call_ind)
    k_acme = _key(acme, ["knowledge:read", "ai:ask"])
    assert client.post("/api/v1/agent/run", json={"question": "friction"}, headers=k_acme).status_code == 404

    # Default tenant still has the pack enabled and succeeds
    step = {"n": 0}

    def ok_default(url, *a, **kw):
        step["n"] += 1
        if step["n"] == 1:
            return call_ind(url)
        return FakeResp({"choices": [{"message": {"content": "done"}}]})

    monkeypatch.setattr(backend.HTTP, "post", ok_default)
    k_def = _key("default", ["knowledge:read", "ai:ask"])
    r = client.post("/api/v1/agent/run", json={"question": "friction"}, headers=k_def)
    assert r.status_code == 200 and r.json()["status"] == "completed"


# ---------------------------------------------------------------- 6. Budget exhaustion mid-loop
def test_redteam_budget_exhaustion_mid_loop_returns_429(protected, monkeypatch):
    _mock_models(monkeypatch)
    backend.CONFIG.set("limits.ai_daily", 1, "default")

    def one_tool(url, *a, **kw):
        return FakeResp({"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "list_documents", "arguments": "{}"}}
        ]}}]})

    monkeypatch.setattr(backend.HTTP, "post", one_tool)
    try:
        k = _key("default", ["knowledge:read", "ai:ask"])
        r = client.post("/api/v1/agent/run", json={"question": "two steps"}, headers=k)
        assert r.status_code == 429 and r.headers.get("retry-after") == "3600"
    finally:
        backend.CONFIG.unset("limits.ai_daily", "default")


# ---------------------------------------------------------------- 7. Approval replay attack
def test_redteam_approval_replay_returns_409(protected, write_tool, monkeypatch):
    _mock_models(monkeypatch)
    step = {"n": 0}

    def fake_post(url, *a, **kw):
        step["n"] += 1
        if step["n"] == 1:
            return FakeResp({"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "test_write_action", "arguments": '{"target":"once"}'}}
            ]}}]})
        return FakeResp({"choices": [{"message": {"content": "all done"}}]})

    monkeypatch.setattr(backend.HTTP, "post", fake_post)
    k = _key("default", ["knowledge:read", "knowledge:write", "ai:ask"])
    aid = client.post("/api/v1/agent/run", json={"question": "write once"}, headers=k).json()["approval_id"]

    r1 = client.post(f"/api/v1/agent/approvals/{aid}/approve", headers=k)
    assert r1.status_code == 200 and r1.json()["status"] == "completed"
    assert len(write_tool) == 1

    # Replay attempt must return 409 and NOT execute write_tool again
    r2 = client.post(f"/api/v1/agent/approvals/{aid}/approve", headers=k)
    assert r2.status_code == 409
    assert len(write_tool) == 1

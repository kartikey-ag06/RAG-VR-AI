"""Phase 4 part 2a: tenants, scoped API keys, one identity resolver, the control plane (/api/core/*)."""
import time

import pytest
from fastapi.testclient import TestClient

import app as backend
from core.data import LocalKV
from core.identity import DEFAULT_TENANT, ApiKeys, Identity, Tenants
from core.identity.keys import LAST_USED_EVERY
from core.sdk.context import PolicyDenied

client = TestClient(backend.app)


@pytest.fixture
def store(tmp_path):
    kv = LocalKV(tmp_path)
    tenants = Tenants(kv)
    tenants.ensure_default()
    return kv, tenants, ApiKeys(kv, tenants)


# ---------------------------------------------------------------- keys + tenants (unit)
def test_key_is_shown_once_and_stored_as_hash(store):
    kv, tenants, keys = store
    rec, secret = keys.create("glasses fleet", ["knowledge:read", "ai:ask", "ai:ask"])
    assert secret.startswith(f"vrk_{rec['id']}.") and rec["scopes"] == ["knowledge:read", "ai:ask"]
    raw = kv.kv_get("apikey", rec["id"])
    assert secret.split(".", 1)[1] not in str(raw) and "secret_sha" in raw and "secret_sha" not in rec
    assert keys.verify(secret)["tenant_id"] == DEFAULT_TENANT
    assert all("secret_sha" not in k for k in keys.list())


def test_wrong_tampered_and_revoked_keys_fail(store):
    _, _, keys = store
    rec, secret = keys.create("k", ["knowledge:read"])
    for bad in ("", "vrk_", secret[:-1] + ("A" if secret[-1] != "A" else "B"), secret.replace("vrk_", "vrx_"),
                f"vrk_{'0' * 12}.{secret.split('.')[1]}", secret + " extra"):
        assert keys.verify(bad) is None, bad
    assert keys.revoke(rec["id"]) is True and keys.verify(secret) is None and keys.revoke(rec["id"]) is False
    assert keys.list()[0]["revoked"]


def test_key_validation_and_tenants(store):
    _, tenants, keys = store
    with pytest.raises(ValueError, match="unknown scope"):
        keys.create("k", ["root:all"])
    with pytest.raises(ValueError, match="at least one scope"):
        keys.create("k", [])
    with pytest.raises(ValueError, match="unknown tenant"):
        keys.create("k", ["ai:ask"], tenant_id="acme")
    tenants.create("acme", "Acme Ltd")
    rec, secret = keys.create("acme glasses", ["ai:ask"], tenant_id="acme")
    assert keys.verify(secret)["tenant_id"] == "acme" and [k["id"] for k in keys.list("acme")] == [rec["id"]]
    for bad in ("A", "1abc", "has space", "x" * 41, "default"):
        with pytest.raises(ValueError):
            tenants.create(bad, "x")
    assert [t["id"] for t in tenants.list()] == ["acme", "default"]


def test_last_used_is_throttled(store):
    kv, _, keys = store
    rec, secret = keys.create("k", ["ai:ask"])
    keys.verify(secret)
    first = kv.kv_get("apikey", rec["id"])["last_used"]
    keys.verify(secret)
    assert kv.kv_get("apikey", rec["id"])["last_used"] == first                 # no write on every request
    kv.kv_put("apikey", rec["id"], dict(kv.kv_get("apikey", rec["id"]), last_used=time.time() - LAST_USED_EVERY - 1))
    keys.verify(secret)
    assert kv.kv_get("apikey", rec["id"])["last_used"] > first


# ---------------------------------------------------------------- resolver (unit, fake requests)
class Req:
    def __init__(self, headers=None):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.query_params = {}


class FakeAuth:
    def __init__(self, users=(), protected=True, admin_tokens=()):
        self.users, self.protected, self.admin_tokens = list(users), protected, set(admin_tokens)

    def token_of(self, req):
        return req.headers.get("x-vr-token", "")

    def user_of(self, req):
        return next((u for u in self.users if u["token"] == self.token_of(req)), None)

    def is_admin(self, req):
        u = self.user_of(req)
        return self.token_of(req) in self.admin_tokens or bool(u and u["role"] == "admin") or not self.protected

    def admin_required(self, req=None):
        return self.protected


def test_resolver_matrix(store):
    _, _, keys = store
    _, k_read = keys.create("reader", ["knowledge:read"])
    users = [{"id": "u1", "name": "tech", "token": "t-user", "role": "user", "groups": ["ops"]},
             {"id": "u2", "name": "boss", "token": "t-admin", "role": "admin", "groups": []}]
    idn = Identity(keys, FakeAuth(users, protected=True, admin_tokens={"ADMIN"}), env={"MCP_TOKEN": "mcp-secret"})

    p, user = idn.resolve(Req({"Authorization": f"Bearer {k_read}"}))
    assert p.kind == "service" and p.scopes == {"knowledge:read"} and user is None
    p, _ = idn.resolve(Req({"X-VR-Token": k_read}))                            # a key in X-VR-Token works too
    assert p.kind == "service"
    p, _ = idn.resolve(Req({"Authorization": "Bearer mcp-secret"}))
    assert p.id == "mcp-token" and p.has("ai:ask") and not p.has("admin")
    p, user = idn.resolve(Req({"X-VR-Token": "t-user"}))
    assert p.kind == "user" and p.groups == ("ops",) and p.has("ai:ask") and not p.has("admin") and user["id"] == "u1"
    assert idn.resolve(Req({"X-VR-Token": "t-admin"}))[0].has("admin")
    assert idn.resolve(Req({"X-VR-Token": "ADMIN"}))[0].has("admin")
    p, _ = idn.resolve(Req())
    assert p.kind == "anonymous" and p.scopes == {"knowledge:read"}
    with pytest.raises(PolicyDenied) as e:
        idn.resolve(Req({"Authorization": "Bearer vrk_000000000000.wrongwrongwrongwrongwrong"}))
    assert e.value.status == 401                                                 # never falls back to anonymous


def test_require_401_vs_403(store):
    _, _, keys = store
    _, k_read = keys.create("reader", ["knowledge:read"])
    idn = Identity(keys, FakeAuth(protected=True), env={})
    with pytest.raises(PolicyDenied) as e:
        idn.require(Req(), "ai:ask")
    assert e.value.status == 401
    with pytest.raises(PolicyDenied) as e:
        idn.require(Req({"Authorization": f"Bearer {k_read}"}), "ai:ask")
    assert e.value.status == 403 and "ai:ask" in e.value.reason
    assert idn.require(Req(), "knowledge:read")[0].kind == "anonymous"
    local = Identity(keys, FakeAuth(protected=False), env={})
    assert local.require(Req(), "admin")[0].has("admin")                         # unprotected local dev


# ---------------------------------------------------------------- the real app: /api/v1, MCP, control plane
@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    yield {"X-VR-Token": "admin-secret"}


def make_key(admin, scopes, tenant="default"):
    r = client.post("/api/core/keys", json={"name": "test", "scopes": scopes, "tenant_id": tenant}, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()["key"]["id"], {"Authorization": "Bearer " + r.json()["secret"]}


def test_control_plane_is_admin_only(protected):
    assert client.get("/api/core/keys").status_code == 401
    assert client.post("/api/core/keys", json={"name": "x", "scopes": ["ai:ask"]}).status_code == 401
    rec, token = backend.AUTH.create_user("tech", ["ops"], "user")
    try:
        assert client.get("/api/core/keys", headers={"X-VR-Token": token}).status_code == 403
    finally:
        backend.AUTH.delete_user(rec["id"])
    kid, hdr = make_key(protected, ["knowledge:read"])
    assert client.get("/api/core/keys", headers=hdr).status_code == 403            # a key without "admin"
    listed = client.get("/api/core/keys", headers=protected).json()["keys"]
    assert any(k["id"] == kid for k in listed) and all("secret_sha" not in k for k in listed)
    assert client.post("/api/core/keys", json={"name": "x", "scopes": ["bogus"]}, headers=protected).status_code == 400
    assert client.delete("/api/core/keys/nope", headers=protected).status_code == 404


def test_tenants_api(protected):
    assert any(t["id"] == "default" for t in client.get("/api/core/tenants", headers=protected).json()["tenants"])
    tid = "t" + str(int(time.time() * 1000))[-8:]
    assert client.post("/api/core/tenants", json={"id": tid, "name": "Test Co"}, headers=protected).json()["tenant"]["id"] == tid
    assert client.post("/api/core/tenants", json={"id": tid}, headers=protected).status_code == 400  # duplicate
    kid, _ = make_key(protected, ["knowledge:read"], tenant=tid)
    assert [k["id"] for k in client.get(f"/api/core/keys?tenant_id={tid}", headers=protected).json()["keys"]] == [kid]


def test_v1_scopes_with_keys(protected, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    _, reader = make_key(protected, ["knowledge:read"])
    _, asker = make_key(protected, ["knowledge:read", "ai:ask"])
    assert client.post("/api/v1/search", json={"query": "penalty"}, headers=reader).status_code == 200
    r = client.post("/api/v1/ask", json={"question": "penalty"}, headers=reader)
    assert r.status_code == 403 and "ai:ask" in r.json()["error"]
    assert client.post("/api/v1/scan", json={"image": "x"}, headers=asker).status_code == 403   # needs ai:vision
    assert client.post("/api/v1/ask", json={"question": "penalty"}).status_code == 401           # nobody
    bad = {"Authorization": "Bearer vrk_000000000000.notarealsecretnotarealsecret"}
    assert client.post("/api/v1/search", json={"query": "penalty"}, headers=bad).status_code == 401


def test_revoked_key_stops_working(protected):
    kid, hdr = make_key(protected, ["knowledge:read"])
    assert client.get("/api/v1/documents", headers=hdr).status_code == 200
    assert client.delete(f"/api/core/keys/{kid}", headers=protected).json() == {"revoked": True}
    assert client.get("/api/v1/documents", headers=hdr).status_code == 401


def test_mcp_with_keys(protected, monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "mcp-secret")
    _, reader = make_key(protected, ["knowledge:read"])
    _, asker = make_key(protected, ["knowledge:read", "ai:ask"])
    _, ai_only = make_key(protected, ["ai:ask"])
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ask_documents", "arguments": {"question": "penalty"}}}
    lst = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    assert client.post("/mcp", json=lst, headers=reader).status_code == 200
    res = client.post("/mcp", json=call, headers=reader).json()["result"]
    assert res["isError"] and "ai:ask" in res["content"][0]["text"]
    assert "lacks" not in client.post("/mcp", json=call, headers=asker).json()["result"]["content"][0]["text"]
    assert client.post("/mcp", json=lst, headers=ai_only).status_code == 403          # needs knowledge:read
    assert client.post("/mcp", json=lst).status_code == 401
    assert client.post("/mcp", json=lst, headers={"Authorization": "Bearer mcp-secret"}).status_code == 200  # old way

"""Phase 5: the integration framework - manifests, register(ctx), the event bus, pack routes, MCP tools, kill switch.

Test packs live in tests/fixtures/packs (loaded through VR_RAG_PACK_DIRS, see conftest.py):
    demo_pack    uses every part of the SDK          demo_caller  calls demo_pack through ctx.call
    broken_pack  register() raises                   future_pack  needs Core SDK 2.x
    orphan_pack  depends on a missing module         legacy_pack  a pack that asks for legacy access (not allowed)
"""
import ast
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as backend
from core.data import LocalKV
from core.modules import ModuleRegistry
from core.plugins import EventBus, PackLoader
from core.sdk import Event

client = TestClient(backend.app)
ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"X-VR-Token": "admin-secret"}
TOGGLED = ["demo_pack", "demo_caller", "meetings", "vision"]


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
    for t in ("acme", "default"):
        for m in TOGGLED:
            backend.MODULES.set_enabled(m, t, True)
    backend.CONFIG.unset("packs.demo_pack.greeting", "acme")


def key_for(tenant, scopes):
    r = client.post("/api/core/keys", json={"name": "phase5", "scopes": scopes, "tenant_id": tenant}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["secret"]}


def mcp(method, params=None, headers=None):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                    headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def flush():
    assert backend.BUS.flush(5), "events were not delivered in time"


# ---------------------------------------------------------------- loading
def test_loader_states():
    st = {k: v["state"] for k, v in backend.LOADER.status.items()}
    for cap in ("assistant", "calendar_sync", "connectors", "sessions", "expert", "navigation", "meetings", "vision"):
        assert st[cap] == "loaded", (cap, backend.LOADER.status[cap])
    assert st["demo_pack"] == "loaded" and st["demo_caller"] == "loaded"
    assert st["broken_pack"] == "failed" and "boom at start-up" in backend.LOADER.status["broken_pack"]["error"]
    assert st["future_pack"] == "incompatible"
    assert st["orphan_pack"] == "missing_dependency"
    assert st["legacy_pack"] == "invalid"
    names = list(backend.LOADER.status)                    # dependencies load first
    assert names.index("calendar_sync") < names.index("assistant")
    assert names.index("connectors") < names.index("sessions")
    assert names.index("demo_pack") < names.index("demo_caller")


def test_broken_pack_is_switched_off_and_server_lives():
    assert client.get("/api/v1/packs/broken_pack/x").status_code == 404     # its route exists but is gated
    assert not backend.MODULES.enabled("broken_pack", "default")
    assert client.get("/healthz").json() == {"ok": True}


def test_loader_rejects_cycles(tmp_path):
    for a, b in (("cyc_one", "cyc_two"), ("cyc_two", "cyc_one")):
        d = tmp_path / a
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({"id": a, "name": a + " x", "version": "1.0.0", "kind": "pack",
                                                     "core": ">=1.0,<2.0", "depends_on": [b]}))
        (d / "__init__.py").write_text("def register(ctx):\n    pass\n")
    kernel = backend.Kernel(**{**backend.KERNEL.__dict__, "modules": ModuleRegistry(LocalKV(tmp_path)),
                               "tools": {}, "functions": {}})
    st = PackLoader(backend.app, kernel, [tmp_path]).load_all()
    assert {s["state"] for s in st.values()} <= {"invalid", "missing_dependency"} and "cycle" in json.dumps(st)


# ---------------------------------------------------------------- pack routes + policy gate
def test_pack_routes_and_scopes(protected, acme):
    reader = key_for(acme, ["knowledge:read"])
    r = client.get("/api/v1/packs/demo_pack/hello", headers=reader)
    assert r.status_code == 200 and r.json() == {"tenant": "acme", "greeting": "hello"}       # manifest default
    assert client.get("/api/v1/packs/demo_pack/hello").status_code == \
        client.post("/api/v1/search", json={"query": "x"}).status_code                       # same read rule as Core
    assert client.post("/api/v1/packs/demo_pack/greet", json={"name": "x"}).status_code == 401  # nobody: no AI
    assert client.get("/api/v1/packs/demo_pack/hello", headers=key_for(acme, ["ai:ask"])).status_code == 403
    r = client.get("/api/v1/packs/demo_pack/vision", headers=key_for(acme, ["ai:vision", "knowledge:read"]))
    assert r.status_code == 403 and "did not declare" in r.json()["error"]                    # pack never declared it
    r = client.get("/api/v1/packs/demo_pack/legacy", headers=reader)
    assert r.status_code == 403 and "not a legacy" in r.json()["error"]                       # packs: SDK only


def test_pack_scope_can_be_given_to_keys(protected, acme):
    approver = key_for(acme, ["demo_pack:approve"])                       # a scope the PACK defined
    assert client.post("/api/v1/packs/demo_pack/approve", headers=approver).json() == {"approved": True}
    assert client.post("/api/v1/packs/demo_pack/approve", headers=key_for(acme, ["knowledge:read"])).status_code == 403
    r = client.post("/api/core/keys", json={"name": "x", "scopes": ["ghost_pack:do"]}, headers=ADMIN)
    assert r.status_code == 400


def test_pack_config_per_tenant(protected, acme):
    reader = key_for(acme, ["knowledge:read"])
    other = key_for("default", ["knowledge:read"])
    r = client.put("/api/core/config", json={"key": "packs.demo_pack.greeting", "value": "namaste", "tenant_id": acme},
                   headers=ADMIN)
    assert r.status_code == 200
    assert client.get("/api/v1/packs/demo_pack/hello", headers=reader).json()["greeting"] == "namaste"
    assert client.get("/api/v1/packs/demo_pack/hello", headers=other).json()["greeting"] == "hello"
    assert client.put("/api/core/config", json={"key": "packs.demo_pack.max_notes", "value": "x"},
                      headers=ADMIN).status_code == 400                    # type from the manifest


def test_pack_records_are_namespaced_per_tenant(protected, acme):
    a, d = key_for(acme, ["knowledge:read"]), key_for("default", ["knowledge:read"])
    assert client.post("/api/v1/packs/demo_pack/notes", json={"id": "a1", "text": "acme only"}, headers=a).status_code == 200
    assert [n["id"] for n in client.get("/api/v1/packs/demo_pack/notes", headers=a).json()["notes"]] == ["a1"]
    assert client.get("/api/v1/packs/demo_pack/notes", headers=d).json()["notes"] == []       # other tenant: nothing
    from core.data.tenancy import use_tenant
    with use_tenant(acme):
        assert backend.KV.kv_get("demo_pack.note", "a1")["text"] == "acme only"              # stored as <pack>.<kind>
        assert backend.KV.kv_get("note", "a1") is None
        backend.KV.kv_delete("demo_pack.note", "a1")


def test_search_and_answer_through_ctx(protected, acme):
    d = key_for("default", ["knowledge:read", "ai:ask"])
    ps = client.post("/api/v1/packs/demo_pack/search", json={"query": "insider trading penalty"}, headers=d).json()
    assert ps["passages"] and {"doc_uid", "page", "text", "image_url"} <= set(ps["passages"][0])
    ans = client.post("/api/v1/packs/demo_pack/ask", json={"question": "insider trading penalty"}, headers=d).json()
    assert ans["mode"] == "offline"                                       # no AI key in tests: still answers
    reader_only = key_for("default", ["knowledge:read"])
    assert client.post("/api/v1/packs/demo_pack/ask", json={"question": "x"}, headers=reader_only).status_code == 403


def test_llm_uses_registered_prompt(protected, acme, monkeypatch):
    seen = {}
    monkeypatch.setattr(backend.AI, "complete", lambda messages, max_tokens=700, fast=False: seen.update(m=messages) or "hi!")
    r = client.post("/api/v1/packs/demo_pack/greet", json={"name": "Asha"}, headers=key_for(acme, ["ai:ask"]))
    assert r.json() == {"text": "hi!"}
    assert seen["m"][0]["content"] == "You greet Asha in one short sentence. Unknown stays {unknown}."
    assert seen["m"][1] == {"role": "user", "content": "hi"}
    assert backend.CONFIG.schema["prompts.demo_pack.greet"]["default"].startswith("You greet")   # tenant-overridable


def test_call_between_modules(protected, acme):
    k = key_for(acme, ["knowledge:read"])
    from core.data.tenancy import use_tenant
    try:
        client.post("/api/v1/packs/demo_pack/notes", json={"id": "c1"}, headers=k)
        assert client.get("/api/v1/packs/demo_caller/count", headers=k).json() == {"count": 1}
        assert client.get("/api/v1/packs/demo_caller/sneaky", headers=k).status_code == 403    # not a dependency
        backend.MODULES.set_enabled("demo_pack", acme, False)
        assert client.get("/api/v1/packs/demo_caller/count", headers=k).status_code == 404     # target switched off
    finally:
        with use_tenant(acme):
            backend.KV.kv_delete("demo_pack.note", "c1")                  # leave no notes for other tests


# ---------------------------------------------------------------- THE GATE: switch off -> routes + MCP tools gone
def test_kill_switch_hides_routes_and_mcp_tools(protected, acme):
    k = key_for(acme, ["knowledge:read", "ai:ask"])
    names = [t["name"] for t in mcp("tools/list", headers=k)["tools"]]
    assert "demo_pack_echo" in names and "ask_documents" in names
    out = mcp("tools/call", {"name": "demo_pack_echo", "arguments": {"text": "hi"}}, headers=k)
    assert out["content"][0]["text"] == "echo: hi" and not out["isError"]
    assert client.get("/api/v1/packs/demo_pack/hello", headers=k).status_code == 200

    r = client.put(f"/api/core/tenants/{acme}/packs/demo_pack", json={"enabled": False}, headers=ADMIN)
    assert r.status_code == 200                                            # no restart, no redeploy

    assert client.get("/api/v1/packs/demo_pack/hello", headers=k).status_code == 404
    assert "demo_pack_echo" not in [t["name"] for t in mcp("tools/list", headers=k)["tools"]]
    out = mcp("tools/call", {"name": "demo_pack_echo", "arguments": {"text": "hi"}}, headers=k)
    assert out["isError"]
    other = key_for("default", ["knowledge:read"])                          # other tenant: still there
    assert "demo_pack_echo" in [t["name"] for t in mcp("tools/list", headers=other)["tools"]]

    client.put(f"/api/core/tenants/{acme}/packs/meetings", json={"enabled": False}, headers=ADMIN)
    assert client.get("/api/meetings", headers=k).status_code == 404       # a capability's OLD url too


def test_switching_vision_off_also_stops_scan(protected, acme):
    k = key_for(acme, ["ai:vision", "knowledge:read"])
    client.put(f"/api/core/tenants/{acme}/packs/vision", json={"enabled": False}, headers=ADMIN)
    r = client.post("/api/v1/scan", json={"image": "data:image/jpeg;base64,AAAA"}, headers=k)
    assert r.status_code == 404 and "vision" in r.json()["detail"]
    assert client.post("/api/vision/describe", json={}, headers=k).status_code == 404


# ---------------------------------------------------------------- events
def test_question_asked_reaches_the_pack(protected, acme):
    k = key_for(acme, ["knowledge:read", "ai:ask"])
    out = client.post("/api/v1/ask", json={"question": "zebra stripes question"}, headers=k).json()
    assert out["mode"] == "offline"
    flush()
    seen = client.get("/api/v1/packs/demo_pack/seen", headers=k).json()["seen"]
    assert any(s["question"] == "zebra stripes question" and s["door"] == "v1" and s["tenant"] == acme for s in seen)
    other = key_for("default", ["knowledge:read"])
    assert not any(s["question"] == "zebra stripes question"
                   for s in client.get("/api/v1/packs/demo_pack/seen", headers=other).json()["seen"])


def test_stream_publishes_once_and_disabled_pack_hears_nothing(protected, acme):
    k = key_for(acme, ["knowledge:read", "ai:ask"])
    before = backend.BUS.stats["published"]
    r = client.post("/api/v1/ask/stream", json={"question": "stream once please"}, headers=k)
    assert r.text.count("event: done") == 1
    flush()
    assert backend.BUS.stats["published"] == before + 1
    backend.MODULES.set_enabled("demo_pack", acme, False)
    client.post("/api/v1/ask", json={"question": "while switched off"}, headers=k)
    flush()
    backend.MODULES.set_enabled("demo_pack", acme, True)
    seen = [s["question"] for s in client.get("/api/v1/packs/demo_pack/seen", headers=k).json()["seen"]]
    assert "stream once please" in seen and "while switched off" not in seen


def test_listener_errors_are_contained_and_shown(protected, acme):
    backend.emit("document.deleted", {"doc_uid": "x"}, acme)
    flush()
    assert backend.BUS.errors["demo_pack"]["event"] == "document.deleted"
    ev = client.get("/api/core/events", params={"tenant_id": acme}, headers=ADMIN).json()
    assert ev["stats"]["failed"] >= 1 and "question.asked" in ev["subscribers"]
    p = client.get("/api/core/packs/demo_pack", params={"tenant_id": acme}, headers=ADMIN).json()
    assert p["state"] == "loaded" and p["last_error"]["event"] == "document.deleted"


def test_admin_actions_publish_events(protected, acme):
    client.put(f"/api/core/tenants/{acme}/packs/meetings", json={"enabled": False}, headers=ADMIN)
    client.put("/api/core/config", json={"key": "limits.ai_daily", "value": 7, "tenant_id": acme}, headers=ADMIN)
    backend.CONFIG.unset("limits.ai_daily", acme)
    flush()
    names = [e["name"] for e in client.get("/api/core/events", params={"tenant_id": acme}, headers=ADMIN).json()["events"]]
    assert "pack.disabled" in names and "config.changed" in names
    changed = next(e for e in backend.BUS.recent(acme) if e["name"] == "config.changed")
    assert "value" not in changed["data"]                                 # values never travel in events


def test_pack_events_must_be_declared():
    ctx = backend.LOADER.contexts["demo_pack"]
    with pytest.raises(Exception):
        ctx.events.publish("default", "demo_pack.deleted_everything", {})   # not in "publishes"
    with pytest.raises(ValueError):
        ctx.events.subscribe("document.added")                              # not in "subscribes"
    with pytest.raises(KeyError):
        ctx.config.get("default", "not_declared")


def test_event_bus_unit():
    on = {"m_on"}
    bus = EventBus(enabled=lambda m, t: m in on, log=logging.getLogger("t"))
    got = []
    bus.subscribe("question.asked", lambda e: got.append(("m_on", e.tenant_id)), "m_on")
    bus.subscribe("question.asked", lambda e: got.append(("m_off", e.tenant_id)), "m_off")
    bus.subscribe("question.asked", lambda e: 1 / 0, "m_bad_on")
    on.add("m_bad_on")
    bus.subscribe("question.asked", lambda e: got.append(("self", e.tenant_id)), "m_src")
    on.add("m_src")
    bus.publish(Event("question.asked", "acme", {}, source="m_src"))
    assert bus.flush(5)
    assert got == [("m_on", "acme")]                                       # off skipped, own event not echoed
    assert bus.stats["failed"] == 1 and bus.stats["skipped_disabled"] == 1
    small = EventBus(max_queue=1)
    small._start = lambda: None                                            # no worker: the queue stays full
    small.publish(Event("question.asked", "acme"))
    small.publish(Event("question.asked", "acme"))
    assert small.stats["dropped"] == 1                                     # never blocks the caller


# ---------------------------------------------------------------- control plane + admin page
def test_packs_endpoint_lists_everything(protected):
    packs = {p["id"]: p for p in client.get("/api/core/packs", headers=ADMIN).json()["packs"]}
    assert packs["demo_pack"]["mcp_tools"] == ["demo_pack_echo"] and packs["demo_pack"]["version"] == "0.1.0"
    assert packs["future_pack"]["state"] == "incompatible" and packs["future_pack"]["enabled"] is False
    assert packs["meetings"]["legacy"] is True and "/api/meetings" in packs["meetings"]["routes_prefixes"]
    assert packs["industrial_field_ops"]["state"] == "loaded" and not packs["industrial_field_ops"]["legacy"]
    assert not any(p.get("manual") for p in packs.values())               # every module comes from a manifest now
    assert client.get("/api/core/packs/nope", headers=ADMIN).status_code == 404
    assert client.get("/api/core/events").status_code == 401


def test_admin_page_has_pack_and_settings_sections():
    html = client.get("/admin").text
    assert "Packs &amp; capabilities" in html and 'id="config"' in html and "/api/core/packs" in html


# ---------------------------------------------------------------- boundaries (ADR 0001 / 0006)
def _core_imports(py: Path):
    for node in ast.walk(ast.parse(py.read_text("utf-8"))):
        mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
            [node.module] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else []
        yield from (m for m in mods if m == "core" or m.startswith("core."))


def test_packs_use_only_the_sdk():
    dirs = [ROOT / "backend" / "packs", ROOT / "tests" / "fixtures" / "packs"]
    for py in [p for d in dirs if d.exists() for p in d.rglob("*.py")]:
        for m in _core_imports(py):
            assert m == "core.sdk" or m.startswith("core.sdk."), f"{py}: imports {m} (packs may import core.sdk only)"


def test_capabilities_are_declared_legacy():
    for mf in (ROOT / "backend" / "capabilities").glob("*/manifest.json"):
        m = json.loads(mf.read_text())
        assert m["kind"] == "capability" and m["legacy"] is True and m["legacy_routes"], mf

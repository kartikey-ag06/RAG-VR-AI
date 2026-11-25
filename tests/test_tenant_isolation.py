"""Phase 4 part 2b: one customer (tenant) must never see another's data - through ANY door.

Setup: tenant "acme" owns one document with a word no SEBI order contains ("zebracorn"), and has an API key.
The 10 seed documents belong to tenant "default". Each test tries a different way in.
"""
import json
import re
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as backend
from core.data import LocalKV
from core.data.migrations import MIGRATIONS
from core.data.tenancy import DEFAULT_TENANT, current_tenant, tenant_for_kind, use_tenant

client = TestClient(backend.app)
ROOT = Path(__file__).resolve().parent.parent
SECRET_WORD = "zebracorn"


def wait_ready(uid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if any(d["uid"] == uid for d in backend.STORE.snapshot["index"]["docs"]) and backend.STORE.state == "idle":
            return
        time.sleep(0.1)
    raise AssertionError("document was not indexed")


@pytest.fixture(scope="module")
def acme():
    tenants, keys = backend.SERVICES.get("tenants"), backend.SERVICES.get("keys")
    if not tenants.get("acme"):
        tenants.create("acme", "Acme Ltd")
    rec, secret = keys.create("acme glasses", ["knowledge:read", "ai:ask"], tenant_id="acme")
    text = (f"Acme {SECRET_WORD} turbine manual. Step 1: isolate the {SECRET_WORD} power unit. " * 20).encode()
    entry = backend.STORE.add(f"acme-{uuid.uuid4().hex[:6]}.txt", text, tenant_id="acme")
    wait_ready(entry["uid"])
    yield {"uid": entry["uid"], "hdr": {"Authorization": f"Bearer {secret}"}, "text": text, "name": entry["name"]}
    backend.STORE.delete(entry["uid"])
    keys.revoke(rec["id"])


def uids(resp):
    return {p["doc_uid"] for p in resp.json().get("passages", resp.json().get("documents", []))}


# ---------------------------------------------------------------- documents and search
def test_search_sees_only_own_tenant(acme):
    mine = client.post("/api/v1/search", json={"query": SECRET_WORD, "k": 10}, headers=acme["hdr"])
    assert uids(mine) == {acme["uid"]}
    assert acme["uid"] not in uids(client.post("/api/v1/search", json={"query": SECRET_WORD, "k": 20}))
    sebi = client.post("/api/v1/search", json={"query": "penalty insider trading", "k": 20}, headers=acme["hdr"])
    assert uids(sebi) <= {acme["uid"]}                                        # none of default's 10 documents
    old = client.post("/api/search", json={"query": SECRET_WORD, "k": 20}).json()   # the older endpoint too
    assert all(h["uid"] != acme["uid"] for h in old["hits"])


def test_document_lists_and_counts(acme):
    assert uids(client.get("/api/v1/documents", headers=acme["hdr"])) == {acme["uid"]}
    default_docs = uids(client.get("/api/v1/documents"))
    assert acme["uid"] not in default_docs and len(default_docs) == 10
    assert all(f["uid"] != acme["uid"] for f in client.get("/api/files").json()["files"])
    assert client.get("/api/grok/status", headers=acme["hdr"]).json()["docs"] == 1
    assert client.get("/api/grok/status").json()["docs"] == 10
    assert client.get("/api/v1/health", headers=acme["hdr"]).json()["documents"] == 1


def test_pages_and_images(acme):
    u = acme["uid"]
    assert client.get(f"/api/v1/documents/{u}/pages/1").status_code == 404
    assert client.get(f"/api/v1/documents/{u}/pages/1", headers=acme["hdr"]).status_code == 200
    pos = next(d["id"] for d in backend.STORE.snapshot["index"]["docs"] if d["uid"] == u)
    assert client.get(f"/rag/pages/{pos}_1.jpg").status_code == 404


def test_rag_index_hides_other_tenants(acme):
    ix = client.get("/rag/index.json").json()
    mine = client.get("/rag/index.json", headers=acme["hdr"]).json()
    pos = next(d["id"] for d in backend.STORE.snapshot["index"]["docs"] if d["uid"] == acme["uid"])
    assert ix["docs"][pos]["title"] == "(restricted)" and ix["docs"][pos]["uid"] == ""
    assert SECRET_WORD not in json.dumps(ix)
    assert mine["docs"][pos]["uid"] == acme["uid"] and SECRET_WORD in json.dumps(mine)
    assert all(d["title"] == "(restricted)" for d in mine["docs"] if d["id"] != pos)
    assert "tenant_id" not in json.dumps(ix) and "tenant_id" not in json.dumps(mine)    # internal field
    e1 = client.get("/rag/index.json").headers["etag"]
    assert e1 != client.get("/rag/index.json", headers=acme["hdr"]).headers["etag"]       # separate cache entries


def test_passage_refs_cannot_reach_another_tenant(acme, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    sent = []

    class R:
        status_code, text = 200, "{}"

        def json(self):
            return {"choices": [{"message": {"content": "SOURCE: GENERAL\n\nok"}}]}
    monkeypatch.setattr(backend.HTTP, "post", lambda url, json=None, **k: sent.append(json) or R())
    i = next(n for n, c in enumerate(backend.STORE.snapshot["index"]["chunks"])
             if backend.STORE.snapshot["index"]["docs"][c["doc"]]["uid"] == acme["uid"])
    ref = {"i": i, "doc_uid": acme["uid"], "page": 1}
    client.post("/api/v1/ask", json={"question": "q", "passages": [ref]})
    assert SECRET_WORD not in sent[-1]["messages"][1]["content"]
    client.post("/api/v1/ask", json={"question": "q", "passages": [ref]}, headers=acme["hdr"])
    assert SECRET_WORD in sent[-1]["messages"][1]["content"]


def test_mcp_tools(acme):
    def call(name, args, hdr=None):
        return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": name, "arguments": args}}, headers=hdr or {}).json()["result"]
    listed = json.loads(call("list_documents", {}, acme["hdr"])["content"][0]["text"])
    assert [d["doc_uid"] for d in listed] == [acme["uid"]]
    assert acme["uid"] not in call("list_documents", {})["content"][0]["text"]
    assert call("get_page", {"doc_uid": acme["uid"], "page": 1})["isError"] is True
    assert SECRET_WORD in call("get_page", {"doc_uid": acme["uid"], "page": 1}, acme["hdr"])["content"][0]["text"]
    assert call("get_page_image", {"doc_uid": acme["uid"], "page": 1})["isError"] is True


def test_duplicate_check_is_per_tenant(acme):
    other = backend.STORE.add("same-bytes.txt", acme["text"], tenant_id=DEFAULT_TENANT)   # no "already uploaded as acme's"
    try:
        assert other["uid"] != acme["uid"]
        with pytest.raises(ValueError, match=re.escape(acme["name"])):
            backend.STORE.add("again.txt", acme["text"], tenant_id="acme")              # same tenant: still caught
    finally:
        backend.STORE.delete(other["uid"])


# ---------------------------------------------------------------- small records (notes, sessions, ...) and events
def test_notes_are_per_tenant_end_to_end(acme):
    n = client.post("/api/notes", json={"text": f"acme secret {uuid.uuid4().hex}"}, headers=acme["hdr"]).json()
    assert any(x["id"] == n["id"] for x in client.get("/api/notes", headers=acme["hdr"]).json()["notes"])
    assert all(x["id"] != n["id"] for x in client.get("/api/notes").json()["notes"])


def test_kv_scoping_rules(tmp_path):
    kv = LocalKV(tmp_path)
    kv.kv_put("note", "1", {"t": "default note"})
    kv.kv_put("user", "u1", {"name": "global user"})
    with use_tenant("acme"):
        assert current_tenant() == "acme" and kv.kv_get("note", "1") is None and kv.kv_list("note") == []
        kv.kv_put("note", "1", {"t": "acme note"})
        assert kv.kv_get("user", "u1") == {"name": "global user"}               # login records are global
        assert tenant_for_kind("apikey") == DEFAULT_TENANT and tenant_for_kind("session") == "acme"
    assert kv.kv_get("note", "1") == {"t": "default note"} and current_tenant() == DEFAULT_TENANT
    assert (tmp_path / "app_data" / "note.json").exists()                        # default: same file as before
    assert (tmp_path / "app_data" / "tenants" / "acme" / "note.json").exists()
    for bad in ("../x", "a/b", "Note", "", "x" * 41):
        with pytest.raises(ValueError):
            kv.kv_put(bad, "1", {})
    with pytest.raises(ValueError):
        with use_tenant("../etc"):
            pass


def test_journal_is_per_tenant(tmp_path):
    from core.data import LocalJournal
    log = LocalJournal(tmp_path)
    log.append("industrial_field_ops.questions", {"q": "default question"})
    with use_tenant("acme"):
        log.append("industrial_field_ops.questions", {"q": "acme question"})
        assert [e["q"] for e in log.list("industrial_field_ops.questions")] == ["acme question"]
        log.archive("industrial_field_ops.questions")
        assert log.list("industrial_field_ops.questions") == []
    assert [e["q"] for e in log.list("industrial_field_ops.questions")] == ["default question"]   # acme's archive
    with pytest.raises(ValueError):
        log.append("../escape", {})


def test_middleware_tenant_reaches_worker_threads(acme):
    """Plain `def` endpoints run in worker threads: the tenant must travel with them (contextvars)."""
    seen = {}

    @backend.app.get("/__test/tenant")
    def _probe():
        seen["t"] = current_tenant()
        return {"t": seen["t"]}
    routes = backend.app.router.routes
    routes.insert(0, routes.pop())        # before the static app mounted at "/", which would answer first
    try:
        assert client.get("/__test/tenant", headers=acme["hdr"]).json()["t"] == "acme"
        assert client.get("/__test/tenant").json()["t"] == DEFAULT_TENANT
        bad = {"Authorization": "Bearer vrk_000000000000.wrongwrongwrongwrongwrong"}
        assert client.get("/__test/tenant", headers=bad).json()["t"] == DEFAULT_TENANT   # never "acme" by accident
    finally:
        backend.app.router.routes = [r for r in backend.app.router.routes if getattr(r, "path", "") != "/__test/tenant"]


# ---------------------------------------------------------------- guards
def test_no_group_only_permission_checks_left():
    """Every document check must go through can_see_doc (tenant + groups). A bare can_see(groups, ...) would skip
    the tenant - this test fails if one comes back."""
    bad = []
    for py in (ROOT / "backend").rglob("*.py"):
        if py.name == "auth.py":                 # can_see_doc itself: checks the tenant first, then the groups
            continue
        for n, line in enumerate(py.read_text("utf-8").splitlines(), 1):
            if re.search(r"\.can_see\(\s*\w+(\[[^\]]+\])?\.get\(\"groups\"", line) or 'if not d.get("groups")]' in line:
                bad.append(f"{py.relative_to(ROOT)}:{n}")
    assert bad == [], bad


def test_migration_list_is_append_only_and_tenant_aware():
    versions = [m[0] for m in MIGRATIONS]
    assert versions == sorted(versions) and len(set(versions)) == len(versions)
    sql = " ".join(" ".join(m[2]) for m in MIGRATIONS)
    assert "PRIMARY KEY (tenant_id, kind, id)" in sql and "events ADD COLUMN IF NOT EXISTS tenant_id" in sql

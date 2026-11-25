"""Phase 6: Industrial Field Ops is the first real pack (backend/packs/industrial_field_ops).

Gate: the pack works end to end, AND removing its folder leaves the Core and every other capability working.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as backend
from core.data import LocalJournal
from core.data.tenancy import use_tenant

client = TestClient(backend.app)
ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"X-VR-Token": "admin-secret"}
PACK = "/api/v1/packs/industrial_field_ops"


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
        backend.MODULES.set_enabled("industrial_field_ops", t, True)


@pytest.fixture
def clean_log():
    """Every test starts with an empty question log and original pages, for both tenants."""
    def wipe():
        for t in ("default", "acme"):
            with use_tenant(t):
                backend.KERNEL.journal.archive("industrial_field_ops.questions")
        for d in backend.STORE.list():
            if backend.STORE.revisions(d["uid"])["history"]:
                backend.STORE.clear_revisions(d["uid"])
    wipe()
    yield
    wipe()


def key_for(tenant, scopes):
    r = client.post("/api/core/keys", json={"name": "phase6", "scopes": scopes, "tenant_id": tenant}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["secret"]}


def ask(question, headers, **extra):
    out = client.post("/api/v1/ask", json={"question": question, **extra}, headers=headers).json()
    assert backend.BUS.flush(5)
    return out


def summary(headers):
    return client.get(f"{PACK}/summary", headers=headers).json()


def open_questions(s):
    return {(d["uid"], p["page"]): [q["q"] for q in p["questions"]] for d in s["docs"] for p in d["hot"] if p["open"]}


# ---------------------------------------------------------------- loaded as a real pack
def test_loaded_as_pack_with_sdk_only():
    st = backend.LOADER.status["industrial_field_ops"]
    assert st["state"] == "loaded" and st["kind"] == "pack" and not st["legacy"]
    assert not (ROOT / "backend" / "procops.py").exists()                     # the old built-in module is gone
    src = (ROOT / "backend" / "app.py").read_text("utf-8")
    assert "import procops" not in src and 'Module("industrial_field_ops"' not in src  # nothing wired by hand


# ---------------------------------------------------------------- Run: the Core event fills the question log
def test_question_asked_lands_on_the_cited_page(protected, clean_log):
    k = key_for("default", ["knowledge:read", "ai:ask"])
    out = ask("insider trading penalty amount", k)
    assert out["citations"], "offline answers still cite the best pages"
    first = (out["citations"][0]["doc_uid"], out["citations"][0]["page"])
    oq = open_questions(summary(k))
    assert oq[first] == ["insider trading penalty amount"]
    with use_tenant("default"):
        entry = backend.KERNEL.journal.list("industrial_field_ops.questions")[-1]
    assert entry["source"] == "server" and entry["door"] == "v1" and entry["hits"] >= 1


def test_machine_tag_travels_with_the_question(protected, clean_log):
    k = key_for("default", ["knowledge:read", "ai:ask"])
    ask("how do I bleed the pump", k, machine_id="PUMP-7731")
    with use_tenant("default"):
        assert backend.KERNEL.journal.list("industrial_field_ops.questions")[-1]["machine"] == "PUMP-7731"


def test_old_glasses_report_is_deduplicated(protected, clean_log):
    k = key_for("default", ["knowledge:read", "ai:ask"])
    out = ask("what is the trading window", k)
    uid, page = out["citations"][0]["doc_uid"], out["citations"][0]["page"]
    old = client.post("/api/procops/event", json={"q": "[live ai: pump] What is the trading window?", "uid": uid,
                                                  "page": page, "mode": "pdf"}, headers=k).json()
    assert old == {"ok": True, "duplicate": True}                            # same question: counted once
    new = client.post("/api/procops/event", json={"q": "a question only the client saw", "uid": uid, "page": page},
                      headers=k).json()
    assert new == {"ok": True}
    assert client.post("/api/procops/event", json="raw string", headers=k).status_code == 400
    oq = open_questions(summary(k))
    assert sorted(oq[(uid, page)]) == ["a question only the client saw", "what is the trading window"]


def test_question_log_is_per_tenant(protected, acme, clean_log):
    a = key_for(acme, ["knowledge:read", "ai:ask"])
    ask("acme private question", a)
    with use_tenant(acme):
        assert [e["q"] for e in backend.KERNEL.journal.list("industrial_field_ops.questions")] == ["acme private question"]
    d = key_for("default", ["knowledge:read"])
    s = summary(d)
    assert all("acme private question" not in qs for qs in open_questions(s).values())
    assert all(x["q"] != "acme private question" for x in s["analytics"]["mostAsked"])


# ---------------------------------------------------------------- Detect + Fix: draft, approve, reset
def test_draft_approve_reset(protected, clean_log, monkeypatch):
    k = key_for("default", ["knowledge:read", "ai:ask"])
    out = ask("which regulation was broken", k)
    uid, page = out["citations"][0]["doc_uid"], out["citations"][0]["page"]
    seen = {}

    def fake_ai(messages, max_tokens=700, fast=False):
        seen["messages"] = messages
        return ('```json\n{"issue": "unclear", "revised_text": "Step 1. The regulation is clearly named here now.", '
                '"change_summary": ["named it"], "checks": [{"check": "Facts kept", "pass": true, "note": ""}]}\n```')
    monkeypatch.setattr(backend.AI, "complete", fake_ai)

    assert client.post(f"{PACK}/draft", json={"uid": uid, "page": page}, headers=k).status_code == 403   # no approve scope
    d = client.post("/api/procops/draft", json={"uid": uid, "page": page}, headers=ADMIN).json()           # old URL
    assert d["revised_text"].startswith("Step 1") and d["next_version"] == "1.1"
    assert "which regulation was broken" in seen["messages"][1]["content"]
    assert '{"issue":' in seen["messages"][0]["content"]                     # JSON example in the prompt kept as is

    a = client.post(f"{PACK}/approve", json={"uid": uid, "page": page, "text": d["revised_text"], "approver": "QA"},
                    headers=ADMIN)
    assert a.status_code == 200 and a.json()["approved"]["version"] == "1.1"
    assert backend.BUS.flush(5)
    assert any(e["name"] == "document.updated" and e["data"]["doc_uid"] == uid for e in backend.BUS.recent("default"))
    s = summary(k)
    hot = next(p for dd in s["docs"] if dd["uid"] == uid for p in dd["hot"] if p["page"] == page)
    assert hot["rev"] == "1.1" and hot["open"] == 0 and hot["before"] == 1   # the fix closed the open question

    r = client.post(f"{PACK}/reset", json={"events": True}, headers=ADMIN).json()
    assert uid in r["restored"] and r["eventsArchived"]
    assert open_questions(summary(k)) == {}


def test_reset_never_touches_another_tenant(protected, acme, clean_log):
    uid = backend.STORE.list()[0]["uid"]                                     # a document of tenant "default"
    backend.STORE.add_revision(uid, 1, "A fixed page text that is long enough.", approver="QA")
    acme_admin = key_for(acme, ["admin"])
    assert client.post(f"{PACK}/reset", json={}, headers=acme_admin).json()["restored"] == []
    assert backend.STORE.revisions(uid)["history"]                           # default's fix is still there


# ---------------------------------------------------------------- machine tags + reports + MCP
def test_machine_routes_old_and_new(protected):
    d = key_for("default", ["knowledge:read"])
    assert client.get("/api/machines", headers=d).json() == client.get(f"{PACK}/machines", headers=d).json()
    r = client.post("/api/machines/resolve", json={"text": "nothing"}, headers=d).json()
    assert r["machine_id"] is None


def test_report_kinds_come_from_the_pack(protected, acme):
    kinds = {k["kind"] for k in client.get("/api/sessions/kinds", headers=ADMIN).json()["kinds"]}
    assert {"inspection", "incident", "workorder", "summary"} <= kinds
    client.put(f"/api/core/tenants/{acme}/packs/industrial_field_ops", json={"enabled": False}, headers=ADMIN)
    acme_admin = key_for(acme, ["admin"])
    off_kinds = {k["kind"] for k in client.get("/api/sessions/kinds", headers=acme_admin).json()["kinds"]}
    assert "summary" in off_kinds and not ({"inspection", "incident", "workorder"} & off_kinds)


def test_mcp_tools(protected, clean_log):
    k = key_for("default", ["knowledge:read", "ai:ask"])
    ask("trading window closure rules", k)

    def call(name, args):
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}}, headers=k).json()["result"]
        return r["content"][0]["text"]
    assert "trading window closure rules" in call("industrial_field_ops_friction", {"limit": 3})
    assert "No known machine" in call("industrial_field_ops_resolve_machine", {"text": "zzz"})


# ---------------------------------------------------------------- kill switch per tenant
def test_switched_off_for_one_tenant(protected, acme, clean_log):
    client.put(f"/api/core/tenants/{acme}/packs/industrial_field_ops", json={"enabled": False}, headers=ADMIN)
    a = key_for(acme, ["knowledge:read", "ai:ask"])
    for url in ("/procops", "/api/procops/summary", "/api/machines", f"{PACK}/summary"):
        assert client.get(url, headers=a).status_code == 404, url
    ask("asked while switched off", a)
    with use_tenant(acme):
        assert backend.KERNEL.journal.list("industrial_field_ops.questions") == []      # not even logged
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=a).json()
    assert not any(t["name"].startswith("industrial_field_ops") for t in tools["result"]["tools"])
    assert client.get("/procops", headers=ADMIN).status_code == 200                       # default: still on


# ---------------------------------------------------------------- data from before Phase 6
def test_old_local_question_log_is_moved(tmp_path, monkeypatch):
    (tmp_path / "procops").mkdir()
    (tmp_path / "procops" / "events.jsonl").write_text(json.dumps({"q": "old default q", "ts": "2026-01-01"}) + "\n")
    (tmp_path / "procops" / "events.acme.jsonl").write_text(json.dumps({"q": "old acme q", "ts": "2026-01-01"}) + "\n")
    monkeypatch.setattr(backend, "DATA_DIR", tmp_path)
    j = LocalJournal(tmp_path)
    backend._import_old_question_log(j)
    backend._import_old_question_log(j)                                      # second start: nothing doubles
    assert [e["q"] for e in j.list("industrial_field_ops.questions")] == ["old default q"]
    with use_tenant("acme"):
        assert [e["q"] for e in j.list("industrial_field_ops.questions")] == ["old acme q"]
    assert (tmp_path / "procops" / "events.jsonl.imported").exists()


# ---------------------------------------------------------------- THE GATE: delete the folder, everything else works
def test_core_works_without_the_pack_folder(tmp_path):
    repo = tmp_path / "repo"
    skip = {ROOT / "backend" / "data", ROOT / "backend" / "packs" / "industrial_field_ops"}

    def ignore(folder, names):            # the pack folder and the local data folder only - nothing else
        return [n for n in names if n == "__pycache__" or Path(folder) / n in skip]
    shutil.copytree(ROOT / "backend", repo / "backend", ignore=ignore)
    assert not (repo / "backend" / "packs" / "industrial_field_ops").exists()
    for name in ("docs", "glasses_app"):
        if (ROOT / name).exists():
            (repo / name).symlink_to(ROOT / name)
    data = tmp_path / "data"
    shutil.copytree(os.environ["VR_RAG_DATA_DIR"], data)                      # already indexed by conftest
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
  "pack_loaded": "industrial_field_ops" in b.LOADER.status,
  "others": sorted(k for k, v in b.LOADER.status.items() if v["state"] == "loaded"),
  "health": c.get("/healthz").status_code,
  "search": c.post("/api/v1/search", json={"query": "insider trading"}).status_code,
  "ask_mode": c.post("/api/v1/ask", json={"question": "insider trading"}).json().get("mode"),
  "notes": c.get("/api/notes").status_code,
  "procops": c.get("/procops").status_code,
  "machines": c.get("/api/machines").status_code,
  "kinds": [k["kind"] for k in c.get("/api/sessions/kinds").json()["kinds"]],
  "mcp_tools": [t["name"] for t in c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]],
}
b.BUS.flush(5)
out["listener_errors"] = b.BUS.stats["failed"]
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
    assert {"assistant", "calendar_sync", "connectors", "sessions", "expert", "navigation", "meetings", "vision"} <= set(out["others"])
    assert out["health"] == 200 and out["search"] == 200 and out["ask_mode"] == "offline" and out["notes"] == 200
    assert out["procops"] == 404 and out["machines"] == 404
    assert "summary" in out["kinds"] and not ({"inspection", "incident", "workorder"} & set(out["kinds"]))
    assert "ask_documents" in out["mcp_tools"] and not any(t.startswith("industrial") for t in out["mcp_tools"])
    assert out["listener_errors"] == 0

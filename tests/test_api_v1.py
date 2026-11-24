"""Tests for the multi-glasses API (/api/v1), the MCP door (/mcp) and the HUD web app (/hud).

Groq is faked, so these run without an API key or internet:  python -m pytest -q tests/test_api_v1.py
"""
import json
import sys
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app as backend  # noqa: E402
import api_v1  # noqa: E402

client = TestClient(backend.app)


class FakeResp:
    def __init__(self, payload=None, lines=None, status=200):
        self.status_code, self._payload, self._lines, self.encoding = status, payload, lines or [], None
        self.text = json.dumps(payload or {})

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def raise_for_status(self):
        pass

    def close(self):
        pass


ANSWER = "SOURCE: PDF [2, 1]\n\nThe order imposed a **penalty** under the SEBI Act [2]. See also [1]."


@pytest.fixture
def fake_groq(monkeypatch):
    """A Groq key + a fake Groq API that always gives ANSWER (streamed in 3 pieces when asked)."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    sent = []

    def post(url, json=None, headers=None, timeout=None, stream=False, **kw):
        sent.append(json)
        if stream:
            parts = [ANSWER[:20], ANSWER[20:50], ANSWER[50:]]
            lines = ["data: " + __import__("json").dumps({"choices": [{"delta": {"content": p}}]}) for p in parts]
            return FakeResp(lines=lines + ["data: [DONE]"])
        return FakeResp({"choices": [{"message": {"content": ANSWER}}]})

    monkeypatch.setattr(backend.HTTP, "post", post)
    monkeypatch.setattr(backend.HTTP, "get", lambda *a, **k: FakeResp({"data": [{"id": "llama-3.3-70b-versatile"}]}))
    backend._MODELS.update(ids=None, t=0)
    return sent


def first_doc():
    return client.get("/api/v1/documents").json()["documents"][0]


# ---------------------------------------------------------------- pure helpers
def test_parse_answer_pdf_and_general():
    hits = [{"uid": "a"}, {"uid": "b"}, {"uid": "c"}]
    body, mode, cited = api_v1.parse_answer(ANSWER, hits)
    assert mode == "pdf" and cited == [1, 0]
    assert body.startswith("The order") and "**" not in body and "SOURCE" not in body
    body, mode, cited = api_v1.parse_answer("SOURCE: GENERAL\n\nParis is the capital [1].", hits)
    assert mode == "general" and cited == []
    _, mode, cited = api_v1.parse_answer("SOURCE: PDF [9]\n\nnothing valid", hits)   # out-of-range citation
    assert mode == "general" and cited == []


def test_style_rules():
    assert "25 words" in api_v1.style_rules("spoken", "")
    assert "Hindi" in api_v1.style_rules("full", "Hindi")
    assert "Hindi" not in api_v1.style_rules("full", "English")


# ---------------------------------------------------------------- /api/v1 without AI
def test_health_and_documents():
    h = client.get("/api/v1/health").json()
    assert h["api"] == "v1" and h["documents"] >= 1 and h["doors"]["mcp"] == "/mcp"
    d = first_doc()
    assert len(d["doc_uid"]) == 12 and d["pages"] >= 1


def test_search_returns_page_images():
    r = client.post("/api/v1/search", json={"query": "penalty", "k": 3})
    assert r.status_code == 200
    ps = r.json()["passages"]
    assert 1 <= len(ps) <= 3
    assert ps[0]["image_url"].startswith("/api/v1/documents/") and ps[0]["n"] == 1
    assert client.post("/api/v1/search", json={}).status_code == 400


def test_ask_offline_without_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr(backend, "KEY_FILE", Path("/nonexistent/key.txt"))
    a = client.post("/api/v1/ask", json={"question": "penalty"}).json()
    assert a["mode"] == "offline" and a["citations"]
    assert client.post("/api/v1/ask", json={"question": "  "}).status_code == 400


def test_page_text_and_resized_image():
    uid = first_doc()["doc_uid"]
    p = client.get(f"/api/v1/documents/{uid}/pages/1").json()
    assert p["page"] == 1 and p["text"] and p["image_url_small"].endswith("?w=600")
    im = client.get(f"/api/v1/documents/{uid}/pages/1.jpg?w=600")
    assert im.status_code == 200 and im.headers["content-type"] == "image/jpeg"
    from PIL import Image
    assert Image.open(BytesIO(im.content)).size[0] == 600
    assert client.get(f"/api/v1/documents/{uid}/pages/9999").status_code == 404
    assert client.get("/api/v1/documents/../pages/1").status_code == 404
    assert client.get("/api/v1/documents/zzzzzzzzzzzz/pages/1.jpg").status_code == 404


def test_scan_rejects_bad_photo():
    r = client.post("/api/v1/scan", json={"image": "not-a-photo"})
    assert r.status_code in (400, 501)


# ---------------------------------------------------------------- /api/v1 with (fake) Groq
def test_ask_with_ai_cites_pages(fake_groq):
    a = client.post("/api/v1/ask", json={"question": "What penalty was imposed?", "style": "spoken", "lang": "Hindi"}).json()
    assert a["mode"] == "pdf"
    assert [c["n"] for c in a["citations"]] == [1, 2] and a["citations"][0]["image_url"].endswith(".jpg")
    assert "**" not in a["answer"]
    system = fake_groq[-1]["messages"][0]["content"]
    assert "25 words" in system and "Hindi" in system


def test_ask_stream_events(fake_groq):
    r = client.post("/api/v1/ask/stream", json={"question": "What penalty was imposed?"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [b.split("\n") for b in r.text.strip().split("\n\n")]
    names = [e[0].replace("event: ", "") for e in events]
    assert names[0] == "meta" and names[-1] == "done" and names.count("delta") == 3
    done = json.loads(events[-1][1][6:])
    assert done["mode"] == "pdf" and done["citations"]


# ---------------------------------------------------------------- access control
def test_protected_server_needs_token(monkeypatch, fake_groq):
    monkeypatch.setenv("RENDER", "1")                       # behaves like the public Render deployment
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")       # (without it, requests from this computer count as admin)
    monkeypatch.setenv("MCP_TOKEN", "svc-secret")
    ask = {"question": "penalty?"}
    # reading stays open (public documents only), exactly like /api/search and /rag/pages
    assert client.post("/api/v1/search", json={"query": "penalty"}).status_code == 200
    assert client.get("/api/v1/health").status_code == 200
    # AI calls need a token, exactly like /api/grok
    assert client.post("/api/v1/ask", json=ask).status_code == 401
    assert client.post("/api/v1/ask/stream", json=ask).status_code == 401
    assert client.post("/api/v1/scan", json={"image": "x"}).status_code == 401
    assert client.post("/api/v1/ask", json=ask, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/v1/ask", json=ask, headers={"Authorization": "Bearer svc-secret"}).status_code == 200
    assert client.post("/api/v1/ask", json=ask, headers={"X-VR-Token": "admin-secret"}).status_code == 200


# ---------------------------------------------------------------- MCP door
def mcp(method, params=None, mid=1, headers=None):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}},
                       headers=headers or {}).json()


def test_mcp_tools_and_page_image():
    tools = {t["name"]: t for t in mcp("tools/list")["result"]["tools"]}
    assert {"list_documents", "search_documents", "get_page", "get_page_image", "ask_documents"} <= set(tools)
    assert all(t["annotations"]["readOnlyHint"] for t in tools.values())
    uid = first_doc()["doc_uid"]
    res = mcp("tools/call", {"name": "get_page_image", "arguments": {"doc_uid": uid, "page": 1, "width": 400}})["result"]
    assert not res["isError"] and res["content"][1]["type"] == "image" and res["content"][1]["mimeType"] == "image/jpeg"
    assert "/api/v1/documents/" in res["content"][0]["text"]


def test_mcp_ask_uses_v1_pipeline(fake_groq):
    res = mcp("tools/call", {"name": "ask_documents", "arguments": {"question": "penalty?", "style": "glance"}})["result"]
    text = res["content"][0]["text"]
    assert "From the documents" in text and "Sources:" in text and "/api/v1/documents/" in text
    assert "3 short numbered points" in fake_groq[-1]["messages"][0]["content"]


def test_mcp_token(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "svc-secret")
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert mcp("ping", headers={"Authorization": "Bearer svc-secret"})["result"] == {}


# ---------------------------------------------------------------- HUD door (Meta Ray-Ban Display)
def test_hud_page():
    r = client.get("/hud")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    assert "mrbd-web-app-capable" in html and "document.modelContext" in html and "/api/v1/ask" in html
    for reserved in ("openUrl", "goBack", "goForward", "reload", "getCurrentUrl", "getPageTitle", "getPageText"):
        assert f"name: '{reserved}'" not in html


def test_group_restricted_document_is_hidden():
    """A document limited to a group must not reach anonymous / service callers through any v1 or MCP door."""
    import time
    store, auth = backend.STORE, backend.AUTH
    uid = first_doc()["doc_uid"]
    rec, token = auth.create_user("hr-tester", ["hr"], "user")

    def groups_now():
        return next(d.get("groups", []) for d in store.snapshot["index"]["docs"] if d["uid"] == uid)

    def wait_for(want):
        for _ in range(100):
            if groups_now() == want:
                return
            time.sleep(0.1)
        raise AssertionError("index did not rebuild")

    try:
        store.update_meta(uid, groups=["hr"])
        wait_for(["hr"])
        anon = [d["doc_uid"] for d in client.get("/api/v1/documents").json()["documents"]]
        assert uid not in anon
        assert client.get(f"/api/v1/documents/{uid}/pages/1").status_code == 404
        assert client.get(f"/api/v1/documents/{uid}/pages/1.jpg").status_code == 404
        hits = client.post("/api/v1/search", json={"query": "penalty order", "k": 20}).json()["passages"]
        assert all(p["doc_uid"] != uid for p in hits)
        res = mcp("tools/call", {"name": "get_page_image", "arguments": {"doc_uid": uid, "page": 1}})["result"]
        assert res["isError"]
        # the user in group "hr" does see it
        mine = [d["doc_uid"] for d in client.get("/api/v1/documents", headers={"X-VR-Token": token}).json()["documents"]]
        assert uid in mine
        assert client.get(f"/api/v1/documents/{uid}/pages/1", headers={"X-VR-Token": token}).status_code == 200
    finally:
        store.update_meta(uid, groups=[])
        auth.delete_user(rec["id"])
        wait_for([])

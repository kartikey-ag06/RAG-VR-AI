"""Characterization tests: they record what the app does TODAY, before the Core Engine refactor (Phase 3+).

They are not a spec of what is ideal. They are a safety net: if moving code into core/ changes a search result, a prompt,
a response shape or a header, one of these fails and we decide on purpose whether that change is wanted.

Golden files (tests/golden/*.json) hold exact outputs. After an INTENDED change, regenerate them:
    UPDATE_GOLDEN=1 python -m pytest -q tests/test_characterization.py
and review the diff in git before committing.

Tests marked "CHANGES IN PHASE 4" document behaviour the plan will change on purpose (e.g. /api/grok accepting any
client system prompt). When Phase 4 lands, update those tests together with the change.
"""
import gzip
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as backend
import mcp_server
from indexer import STOP, tokenize

client = TestClient(backend.app)
GOLDEN = Path(__file__).resolve().parent / "golden"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"

QUERIES = ["penalty", "insider trading", "diluted EPS non disclosure", "Reliance Petroleum", "Bombay High Court appeal",
           "13,00,00,000", "15HB"]


def golden(name, value):
    """Compare value with tests/golden/<name>.json (or write it when UPDATE_GOLDEN=1)."""
    f = GOLDEN / f"{name}.json"
    text = json.dumps(value, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    if UPDATE or not f.exists():
        GOLDEN.mkdir(exist_ok=True)
        f.write_text(text, "utf-8")
        if not UPDATE:
            pytest.skip(f"golden file {f.name} was missing and has been created - review and commit it")
        return
    assert json.loads(f.read_text("utf-8")) == json.loads(text), f"output differs from {f.name} (UPDATE_GOLDEN=1 to accept)"


# ---------------------------------------------------------------- fake Groq that records every request
class Resp:
    def __init__(self, payload=None, lines=None, status=200):
        self.status_code, self._p, self._lines, self.encoding = status, payload, lines or [], None
        self.text = json.dumps(payload or {})

    def json(self):
        return self._p

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def raise_for_status(self):
        pass

    def close(self):
        pass


@pytest.fixture
def groq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    calls = []
    state = {"answer": "SOURCE: PDF [1]\n\nA penalty was imposed [1].", "status": 200}

    def post(url, json=None, stream=False, **kw):
        calls.append({"url": url, "json": json})
        if state["status"] != 200:
            return Resp({"error": "x"}, status=state["status"])
        if stream:
            a = state["answer"]
            pieces = [a[:10], a[10:]]
            return Resp(lines=["data: " + __import__("json").dumps({"choices": [{"delta": {"content": p}}]}) for p in pieces]
                        + ["data: [DONE]"])
        return Resp({"choices": [{"message": {"role": "assistant", "content": state["answer"]}}]})

    monkeypatch.setattr(backend.HTTP, "post", post)
    return {"calls": calls, "state": state}


def no_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)


# ---------------------------------------------------------------- search (the engine Phase 3 moves into core/knowledge)
def test_search_shape_keyword_mode():
    r = client.post("/api/search", json={"query": "penalty", "k": 5})
    assert r.status_code == 200
    j = r.json()
    # no embedding model in tests -> keyword (TF-IDF) mode, exactly like a server whose model failed to load
    assert j["mode"] == "keyword" and j["embeddings"] in ("warming", "failed")
    assert set(j) >= {"version", "hits", "machine", "machineMatched", "conf", "mode"}
    assert 1 <= len(j["hits"]) <= 5
    assert set(j["hits"][0]) == {"i", "score", "kw", "sem", "doc", "page", "uid", "title", "text"}
    assert j["hits"][0]["sem"] is None
    scores = [h["score"] for h in j["hits"]]
    assert scores == sorted(scores, reverse=True)


def test_search_empty_and_unknown_words():
    assert client.post("/api/search", json={"query": ""}).json()["mode"] == "none"
    assert client.post("/api/search", json={"query": "zzqqxxnotaword"}).json()["hits"] == []
    assert client.post("/api/search", json={"query": "x", "k": "abc"}).status_code == 400


def test_search_results_golden():
    """Exact top results per query (titles, pages, rounded scores). Doc uids are random per run, so not compared."""
    out = {}
    for q in QUERIES:
        hits = backend.run_search(q, 5)["hits"]
        out[q] = [{"title": h["title"], "page": h["page"], "score": round(h["score"], 4)} for h in hits]
    golden("search_keyword", out)


def test_tokenizer_and_stopwords_golden():
    """The stop-list currently contains SEBI words (sector content in shared code). Phase 3 moves them to config;
    the legal demo must still tokenize exactly like this."""
    assert {"sebi", "noticee", "noticees", "order"} <= STOP
    golden("tokenize", {s: tokenize(s) for s in [
        "SEBI imposed a penalty of Rs. 13,00,00,000 on the Noticee under Section 15HB.",
        "The Order of the Hon'ble Securities Appellate Tribunal (SAT) dated 2017-03.",
        "Diluted EPS was not disclosed by RIL and group entities."]})


# ---------------------------------------------------------------- the index the glasses download
def test_rag_index_shape_etag_and_gzip():
    r = client.get("/rag/index.json")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    ix = r.json()
    assert set(ix) == {"version", "docs", "vocab", "idf", "chunks"}
    assert len(ix["docs"]) == len(backend.STORE.snapshot["index"]["docs"]) and len(ix["vocab"]) == len(ix["idf"])
    assert set(ix["docs"][0]) == {"id", "file", "title", "pages", "type", "uid", "rev", "tags", "groups", "figs"}
    assert set(ix["chunks"][0]) == {"doc", "page", "text", "terms", "weights"}
    etag = r.headers["etag"]
    assert etag.startswith('"') and "no-cache" in r.headers["cache-control"]
    assert client.get("/rag/index.json", headers={"If-None-Match": etag}).status_code == 304
    gz = client.get("/rag/index.json", headers={"Accept-Encoding": "gzip"})
    assert gz.headers.get("content-encoding") == "gzip"
    assert json.loads(gz.content if gz.content[:1] == b"{" else gzip.decompress(gz.content))["version"] == ix["version"]


def test_index_version_and_files_list():
    v = client.get("/api/index/version").json()
    assert set(v) == {"version", "docs", "chunks", "state"} and v["docs"] >= 1
    f = client.get("/api/files").json()
    assert set(f) == {"files", "version", "state", "chunks", "embeddings"}
    assert {"uid", "name", "title", "status", "pages", "tags", "groups"} <= set(f["files"][0])


# ---------------------------------------------------------------- AI endpoints (move into core/ai)
def test_status_shape(monkeypatch):
    no_key(monkeypatch)
    s = client.get("/api/grok/status").json()
    assert s["configured"] is False and s["provider"] == "" and s["storage"] == "local files"
    assert s["search"].startswith("keyword")
    assert "mcp" in s["features"] and "workflows" in s["features"]


def test_grok_without_key_is_400(monkeypatch):
    no_key(monkeypatch)
    r = client.post("/api/grok", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400 and "No API key" in r.json()["error"]


def test_grok_validates_messages(groq):
    assert client.post("/api/grok", json={"messages": []}).status_code == 400
    assert client.post("/api/grok", json={"messages": [{"role": "tool", "content": "x"}]}).status_code == 400
    assert client.post("/api/grok", json={"messages": [{"role": "user", "content": "x" * 60001}]}).status_code == 400


def test_grok_passes_client_system_prompt_and_caches(groq):
    """CHANGES IN PHASE 4: today /api/grok forwards ANY client system prompt to the AI (governance gap)."""
    msgs = [{"role": "system", "content": "You are a pirate."}, {"role": "user", "content": "hello"}]
    a = client.post("/api/grok", json={"messages": msgs}).json()
    assert a["choices"][0]["message"]["content"].startswith("SOURCE")
    sent = groq["calls"][-1]["json"]
    assert sent["messages"] == msgs and sent["temperature"] == 0.2 and sent["max_tokens"] == 700
    assert sent["model"] == "llama-3.3-70b-versatile"
    n = len(groq["calls"])
    again = client.post("/api/grok", json={"messages": msgs}).json()
    assert again.get("cached") is True and len(groq["calls"]) == n          # second time: from the answer cache


def test_grok_provider_errors(groq):
    groq["state"]["status"] = 429
    r = client.post("/api/grok", json={"messages": [{"role": "user", "content": "rate?"}]})
    assert r.status_code == 503 and r.headers["retry-after"] == "30"
    groq["state"]["status"] = 500
    assert client.post("/api/grok", json={"messages": [{"role": "user", "content": "boom?"}]}).status_code == 502


def test_grok_stream_plain_text(groq):
    r = client.post("/api/grok/stream", json={"messages": [{"role": "user", "content": "stream me"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text == groq["state"]["answer"]
    again = client.post("/api/grok/stream", json={"messages": [{"role": "user", "content": "stream me"}]})
    assert again.headers.get("x-cache") == "hit"


def test_v1_ask_prompt_golden(groq):
    """The exact system prompt + excerpts the one RAG pipeline sends. Phase 4 moves prompts into a registry;
    for the legal demo the text sent must stay identical."""
    for style in ("full", "glance", "spoken"):
        client.post("/api/v1/ask", json={"question": "What penalty was imposed?", "style": style})
    sent = [c["json"]["messages"] for c in groq["calls"]]
    assert len(sent) == 3 and all(m[0]["role"] == "system" and m[1]["role"] == "user" for m in sent)

    def strip_uids(m):          # excerpt text includes titles/pages only, no uids - safe to compare
        return [{"role": x["role"], "content": x["content"]} for x in m]
    golden("v1_ask_messages", {s: strip_uids(m) for s, m in zip(("full", "glance", "spoken"), sent)})


def test_v1_ask_general_answer(groq):
    groq["state"]["answer"] = "SOURCE: GENERAL\n\nParis is the capital of France."
    a = client.post("/api/v1/ask", json={"question": "capital of France?"}).json()
    assert a["mode"] == "general" and a["citations"] == [] and a["answer"] == "Paris is the capital of France."
    assert set(a) >= {"answer", "mode", "citations", "confidence", "search_mode"}


def test_v1_ask_ai_error_falls_back_offline(groq):
    groq["state"]["status"] = 500
    a = client.post("/api/v1/ask", json={"question": "penalty"}).json()
    assert a["mode"] == "offline" and a["note"].startswith("AI error")


# ---------------------------------------------------------------- MCP protocol basics
def test_mcp_initialize_and_tool_list_golden():
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "t", "version": "1"}}}).json()
    assert r["result"]["protocolVersion"] == "2025-06-18" and r["result"]["serverInfo"]["name"] == "vr-rag"
    golden("mcp_tools", sorted(mcp_server.TOOLS, key=lambda t: t["name"]))
    unknown = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "no/such"}).json()
    assert "error" in unknown


def test_mcp_search_documents():
    res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "search_documents", "arguments": {"query": "penalty", "k": 2}}}).json()
    assert res["result"]["isError"] is False and res["result"]["content"][0]["type"] == "text"


# ---------------------------------------------------------------- machine tags (moves to the Industrial pack)
def test_machine_tag_resolve_and_filter():
    store = backend.STORE
    uid = store.snapshot["index"]["docs"][0]["uid"]

    def tags_now():
        return next(d.get("tags", []) for d in store.snapshot["index"]["docs"] if d["uid"] == uid)

    def wait_for(want):
        for _ in range(100):
            if tags_now() == want:
                return
            time.sleep(0.1)
        raise AssertionError("index did not rebuild")

    try:
        store.update_meta(uid, tags=["PUMP-7731"])
        wait_for(["PUMP-7731"])
        m = client.post("/api/machines/resolve", json={"text": "Asset tag: pump 7731 / line 2"}).json()
        assert m["machine_id"] == "PUMP-7731" and m["docs"][0]["uid"] == uid
        assert client.post("/api/machines/resolve", json={"text": "nothing here"}).json()["machine_id"] is None
        listed = client.get("/api/machines").json()["machines"]
        assert {"machine_id": "PUMP-7731", "docs": [{"uid": uid, "title": m["docs"][0]["title"], "id": m["docs"][0]["id"]}]} in listed
        res = client.post("/api/search", json={"query": "penalty", "machine": "pump7731"}).json()
        assert res["machineMatched"] is True and all(h["uid"] == uid for h in res["hits"])
        res = client.post("/api/search", json={"query": "penalty", "machine": "NOPE-1"}).json()
        assert res["machineMatched"] is False and res["hits"]                 # unknown machine -> search everything
    finally:
        store.update_meta(uid, tags=[])
        wait_for([])

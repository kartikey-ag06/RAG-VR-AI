"""Tests for backend/limits.py (rate limits, body limits, AI budget/concurrency, caller identity).
Hermetic: a tiny app wrapped in LoadGuard - no network, no Groq key, no documents. Run: python -m pytest -q tests"""
import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import limits  # noqa: E402


def make_client(**limit_overrides):
    """A fresh app + fresh counters, with the given LIMITS entries changed for this test."""
    limits.BUCKETS = limits.Buckets()
    limits.BUDGET = limits.DailyBudget()
    for k, v in limit_overrides.items():
        limits.LIMITS[k] = v
    app = FastAPI()
    app.add_middleware(limits.LoadGuard)

    @app.get("/api/thing")
    def thing():
        return {"ok": True}

    @app.post("/api/notes")
    async def notes(req: Request):
        return {"len": len(await req.body())}

    @app.post("/api/grok")
    def grok():
        time.sleep(0.3)                     # a "slow AI call"
        return {"ok": True}

    @app.post("/api/grok/stream")
    def stream():
        def gen():
            for i in range(3):
                yield f"part{i} "
        return StreamingResponse(gen(), media_type="text/plain")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise RuntimeError("bug")

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def restore_limits():
    saved = dict(limits.LIMITS), limits.AI_DAILY_PER_CALLER, limits.AI_DAILY_TOTAL, limits.AI_MAX_CONCURRENT, limits.AI_QUEUE_WAIT
    yield
    limits.LIMITS.clear(); limits.LIMITS.update(saved[0])
    limits.AI_DAILY_PER_CALLER, limits.AI_DAILY_TOTAL, limits.AI_MAX_CONCURRENT, limits.AI_QUEUE_WAIT = saved[1:]


# ---------------------------------------------------------------- pure functions
def test_route_groups():
    g = limits.route_group
    assert g("POST", "/api/grok") == "ai" and g("POST", "/api/grok/stream") == "ai"
    assert g("POST", "/api/vision/describe") == "ai" and g("POST", "/api/teleprompter/script") == "ai"
    assert g("POST", "/api/meetings/abc/stop") == "ai"          # the summary uses the AI
    assert g("POST", "/api/meetings/abc/segment") == "write"
    assert g("POST", "/api/v1/ask") == "ai" and g("POST", "/api/v1/ask/stream") == "ai" and g("POST", "/api/v1/scan") == "ai"
    assert g("POST", "/mcp") == "mcp" and g("GET", "/api/v1/documents") == "read"
    assert g("POST", "/api/stt") == "stt" and g("POST", "/api/files") == "upload"
    assert g("GET", "/api/files") == "read" and g("DELETE", "/api/notes/1") == "write"
    assert g("GET", "/rag/pages/d0_p1.jpg") is None and g("GET", "/assets/x.js") is None
    assert g("GET", "/healthz") is None and g("OPTIONS", "/api/grok") is None and g("GET", "/upload") is None


def test_bucket_refills_over_time():
    b = limits.Buckets()
    assert [b.take("k", 60, 2, now=0) for _ in range(3)] == [0, 0, 1]   # burst 2, then wait ~1 s
    assert b.take("k", 60, 2, now=1.0) == 0                            # 60/min = one token per second


def test_bucket_memory_is_bounded():
    b = limits.Buckets(max_keys=100)
    for i in range(1000):
        b.take(f"k{i}", 60, 5)
    assert len(b.b) == 100


def test_client_ip_cannot_be_spoofed_with_proxy_hops(monkeypatch):
    scope = {"client": ("10.0.0.5", 1), "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.7")]}
    monkeypatch.setattr(limits, "PROXY_HOPS", 0)
    assert limits.client_ip(scope) == "10.0.0.5"             # direct: ignore the header
    monkeypatch.setattr(limits, "PROXY_HOPS", 1)
    assert limits.client_ip(scope) == "203.0.113.7"          # the entry our proxy added, not the faked first one


def test_token_callers_are_separate_from_ip():
    a = {"client": ("1.1.1.1", 1), "headers": [(b"x-vr-token", b"alice")]}
    b = {"client": ("1.1.1.1", 1), "headers": [(b"x-vr-token", b"bob")]}
    assert limits.caller_key(a) != limits.caller_key(b) and limits.caller_key(a).startswith("t:")
    assert "alice" not in limits.caller_key(a)                # tokens are hashed, never logged in clear


# ---------------------------------------------------------------- middleware behaviour
def test_rate_limit_returns_429_with_retry_after():
    c = make_client(read=(60, 3))
    codes = [c.get("/api/thing").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    r = c.get("/api/thing")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and "try again" in r.json()["error"]


def test_each_user_has_own_limit():
    c = make_client(read=(60, 2))
    for _ in range(2):
        assert c.get("/api/thing", headers={"X-VR-Token": "alice"}).status_code == 200
    assert c.get("/api/thing", headers={"X-VR-Token": "alice"}).status_code == 429
    assert c.get("/api/thing", headers={"X-VR-Token": "bob"}).status_code == 200


def test_health_and_static_are_never_limited():
    c = make_client(read=(1, 1))
    assert all(c.get("/healthz").status_code == 200 for _ in range(20))


def test_body_too_large_is_refused_before_reading():
    c = make_client()
    r = c.post("/api/notes", content=b"x" * (limits.MB + 10), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert c.post("/api/notes", content=b"{}", headers={"content-type": "application/json"}).json() == {"len": 2}


def test_body_too_large_without_content_length():
    c = make_client()

    def chunks():
        for _ in range(3):
            yield b"x" * (600 * 1024)       # 1.8 MB in pieces, sent chunked (no Content-Length)
    r = c.post("/api/notes", content=chunks())
    assert r.status_code == 413


def test_ai_daily_budget():
    c = make_client(ai=(1000, 1000))
    limits.AI_DAILY_PER_CALLER = 2
    codes = [c.post("/api/grok/stream").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert c.post("/api/grok/stream", headers={"X-VR-Token": "other"}).status_code == 200


def test_server_wide_ai_cap():
    c = make_client(ai=(1000, 1000))
    limits.AI_DAILY_TOTAL = 3
    codes = [c.post("/api/grok/stream", headers={"X-VR-Token": f"u{i}"}).status_code for i in range(4)]
    assert codes == [200, 200, 200, 429]


def test_ai_concurrency_cap_returns_503_when_queue_wait_ends():
    c = make_client(ai=(1000, 1000))
    limits.AI_MAX_CONCURRENT, limits.AI_QUEUE_WAIT = 1, 0.05
    results = []

    def call(i):
        results.append(c.post("/api/grok", headers={"X-VR-Token": f"u{i}"}).status_code)
    ts = [threading.Thread(target=call, args=(i,)) for i in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results).count(200) >= 1 and 503 in results


def test_streaming_passes_through_intact():
    c = make_client(ai=(1000, 1000))
    r = c.post("/api/grok/stream")
    assert r.status_code == 200 and r.text == "part0 part1 part2 " and r.headers.get("x-request-id")


def test_unhandled_error_gives_request_id():
    c = make_client()
    r = c.get("/boom")
    assert r.status_code == 500


def test_snapshot_has_numbers():
    make_client().get("/api/thing")
    s = limits.snapshot()
    assert s["requests"] >= 1 and "ai_calls_today" in s and "rates_per_min" in s

"""Pre-Phase 8 baseline regression tests for /api/stt, vision.py, and provider capability flags."""
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as backend
import vision as vision_mod
from core.sdk.context import BudgetExceeded
from tests.conftest import FakeResp

client = TestClient(backend.app)
TINY_JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="


def _with_models(monkeypatch, models):
    monkeypatch.setattr(
        backend.HTTP,
        "get",
        lambda *a, **kw: FakeResp({"data": [{"id": m} for m in models]}),
    )


# ---------------------------------------------------------------- 1. /api/stt & AI.transcribe()
def test_stt_happy_path_and_multipart_payload(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_stt_test")
    _with_models(monkeypatch, ["llama-3.3-70b-versatile", "whisper-large-v3-turbo"])
    seen: list[dict[str, Any]] = []

    def fake_post(url, *a, **kw):
        seen.append({"url": url, "files": kw.get("files"), "data": kw.get("data"), "headers": kw.get("headers")})
        return FakeResp({"text": "check valve pressure"})

    monkeypatch.setattr(backend.HTTP, "post", fake_post)
    r = client.post(
        "/api/stt?lang=en-US&prompt=Hey+Glasses",
        content=b"x" * 900,
        headers={"Content-Type": "audio/webm"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "check valve pressure"}
    assert len(seen) == 1
    call = seen[0]
    assert isinstance(call["url"], str) and call["url"].endswith("/audio/transcriptions")
    assert call["files"] is not None and call["data"] is not None
    assert call["files"]["file"][0] == "speech.webm"
    assert call["files"]["file"][2] == "audio/webm"
    assert call["data"]["model"] == "whisper-large-v3-turbo"
    assert call["data"]["language"] == "en"
    assert call["data"]["prompt"] == "Hey Glasses"


def test_stt_rejects_short_audio(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_stt_short")
    r = client.post("/api/stt", content=b"short", headers={"Content-Type": "audio/webm"})
    assert r.status_code == 400
    assert "too short" in r.json()["error"].lower()


def test_stt_rejects_non_groq_provider(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "xai-stt-test")
    called = []
    monkeypatch.setattr(backend.HTTP, "post", lambda *a, **kw: called.append(a))

    r = client.post("/api/stt", content=b"x" * 900, headers={"Content-Type": "audio/webm"})
    assert r.status_code == 400
    assert "groq" in r.json()["error"].lower()
    assert called == []


def test_stt_budget_exceeded_returns_429(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_stt_budget")
    monkeypatch.setattr(backend.AI, "spend", lambda: (_ for _ in ()).throw(BudgetExceeded("daily limit reached")))
    r = client.post("/api/stt", content=b"x" * 900, headers={"Content-Type": "audio/webm"})
    assert r.status_code == 429
    assert r.headers.get("retry-after") == "3600"
    assert "daily limit reached" in r.json()["error"]


# ---------------------------------------------------------------- 2. vision.call() & /api/vision/describe
def test_vision_describe_happy_path_and_spend(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_vis_test")
    _with_models(monkeypatch, ["meta-llama/llama-4-scout-17b-16e-instruct"])
    backend.CONFIG.set("limits.ai_daily", 5, "default")
    seen: list[dict[str, Any]] = []

    def fake_post(url, *a, **kw):
        seen.append({"url": url, "json": kw.get("json")})
        return FakeResp({
            "choices": [{
                "message": {
                    "content": '{"object": "Gauge", "details": "Analog dial", "text_seen": "100 PSI", "keywords": "gauge psi"}'
                }
            }]
        })

    monkeypatch.setattr(backend.HTTP, "post", fake_post)
    try:
        r = client.post("/api/vision/describe", json={"image": TINY_JPEG, "hint": "pump"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["object"] == "Gauge"
        assert body["text_seen"] == "100 PSI"
        assert body["model"] == "meta-llama/llama-4-scout-17b-16e-instruct"
        assert len(seen) == 1
        payload = seen[0]["json"]
        assert payload is not None
        content = payload["messages"][0]["content"]
        assert content[1] == {"type": "image_url", "image_url": {"url": TINY_JPEG}}
        day = time.strftime("%Y-%m-%d", time.gmtime())
        assert backend.AI._spent.get(("default", day)) == 1
    finally:
        backend.CONFIG.unset("limits.ai_daily", "default")


def test_vision_retries_once_on_429_with_retry_hint(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_vis_retry")
    _with_models(monkeypatch, ["meta-llama/llama-4-scout-17b-16e-instruct"])
    sleeps = []
    calls = []
    monkeypatch.setattr(vision_mod.time, "sleep", lambda s: sleeps.append(s))

    def fake_post(url, *a, **kw):
        calls.append(url)
        if len(calls) == 1:
            return FakeResp({"error": "Rate limit, try again in 0.1s"}, status=429)
        return FakeResp({"choices": [{"message": {"content": '{"object": "Valve", "details": "Ok", "text_seen": "", "keywords": "valve"}'}}]})

    monkeypatch.setattr(backend.HTTP, "post", fake_post)
    r = client.post("/api/vision/describe", json={"image": TINY_JPEG})
    assert r.status_code == 200, r.text
    assert r.json()["object"] == "Valve"
    assert len(calls) == 2
    assert sleeps == [pytest.approx(0.6)]


def test_vision_rejects_non_groq_provider(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "xai-vis-test")
    called = []
    monkeypatch.setattr(backend.HTTP, "post", lambda *a, **kw: called.append(a))

    r = client.post("/api/vision/describe", json={"image": TINY_JPEG})
    assert r.status_code == 502
    assert "groq" in r.json()["error"].lower()
    assert called == []


# ---------------------------------------------------------------- 3. capability gating (/api/v1/health & /api/grok/status)
def test_capability_flags_reflect_provider(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_cap_test")
    _with_models(monkeypatch, ["llama-3.3-70b-versatile", "meta-llama/llama-4-scout-17b-16e-instruct"])
    h_groq = client.get("/api/v1/health").json()
    s_groq = client.get("/api/grok/status").json()
    assert h_groq["photo"] is True
    assert s_groq["provider"] == "groq" and s_groq["stt"] is True

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "xai-cap-test")
    h_xai = client.get("/api/v1/health").json()
    s_xai = client.get("/api/grok/status").json()
    assert h_xai["photo"] is False
    assert s_xai["provider"] == "grok" and s_xai["stt"] is False

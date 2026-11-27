"""Phase 11B tests: AILedger, two-instance shared budgets, token capture, failover single-charge, and /api/core/ai/usage."""
from fastapi.testclient import TestClient

import app as backend
from core.ai import AIGateway, AILedger, estimate_cost_usd
from core.config import ConfigStore
from core.data import LocalKV
from core.sdk.context import BudgetExceeded
from tests.conftest import FakeResp

client = TestClient(backend.app)
ADMIN = {"X-VR-Token": "admin-secret"}


# ---------------------------------------------------------------- 1. Cost estimation
def test_estimate_cost_usd_known_and_fallback():
    c_known = estimate_cost_usd("groq", "llama-3.3-70b-versatile", 1_000_000, 1_000_000)
    assert c_known == pytest.approx(0.59 + 0.79)
    c_fb = estimate_cost_usd("custom", "unknown-model-x", 1_000_000, 1_000_000)
    assert c_fb == pytest.approx(4.0)


import pytest  # noqa: E402


# ---------------------------------------------------------------- 2. Two-instance shared budget gate
def test_two_gateways_share_call_and_cost_limits(tmp_path):
    cfg = ConfigStore(LocalKV(tmp_path))
    cfg.declare("limits.ai_daily", "integer", 0, min=0)
    cfg.declare("ai.daily_cost_usd", "number", 0.0, min=0.0)
    cfg.declare("ai.provider", "string", "")
    cfg.declare("ai.region", "string", "")
    cfg.declare("ai.fallback", "string", "")
    cfg.declare("ai.chat_model", "string", "")

    shared_ledger = AILedger(db=None)

    class FakeHttp:
        def get(self, *a, **kw):
            return FakeResp({"data": [{"id": "gpt-4o"}]})

        def post(self, *a, **kw):
            return FakeResp({
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 500_000, "completion_tokens": 500_000},
            })

    gw1 = AIGateway(FakeHttp(), {"GROQ_API_KEY": "gsk_1"}, tmp_path / "k1", tmp_path / "m1", config=cfg, ledger=shared_ledger)
    gw2 = AIGateway(FakeHttp(), {"GROQ_API_KEY": "gsk_2"}, tmp_path / "k2", tmp_path / "m2", config=cfg, ledger=shared_ledger)

    # Combined calls across 2 instances cannot exceed call_limit=3
    cfg.set("limits.ai_daily", 3, "default")
    assert gw1.complete([{"role": "user", "content": "1"}]) == "ok"
    assert gw2.complete([{"role": "user", "content": "2"}]) == "ok"
    assert gw1.complete([{"role": "user", "content": "3"}]) == "ok"
    with pytest.raises(BudgetExceeded):
        gw2.complete([{"role": "user", "content": "4"}])

    # Cost limit enforcement across instances
    shared_ledger.clear_memory()
    cfg.set("limits.ai_daily", 100, "default")
    cfg.set("ai.daily_cost_usd", 0.50, "default")
    assert gw1.complete([{"role": "user", "content": "1"}]) == "ok"  # ~0.69 > 0.50
    with pytest.raises(BudgetExceeded):
        gw2.complete([{"role": "user", "content": "2"}])


# ---------------------------------------------------------------- 3. Token capture across complete, stream, vision & /api/core/ai/usage
def test_token_capture_complete_stream_vision_and_admin_endpoint(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_ledger")
    monkeypatch.setattr(
        backend.HTTP,
        "get",
        lambda *a, **kw: FakeResp({"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "meta-llama/llama-4-scout-17b-16e-instruct"}]}),
    )

    def fake_post(url, *a, **kw):
        if kw.get("stream"):
            r = FakeResp({})
            r.iter_lines = lambda decode_unicode=True: iter([
                'data: {"choices":[{"delta":{"content":"hi"}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":45}}',
                "data: [DONE]",
            ])
            return r
        return FakeResp({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 45},
        })

    monkeypatch.setattr(backend.HTTP, "post", fake_post)

    backend.AI.complete([{"role": "user", "content": "chat"}])
    assert "".join(backend.AI.stream([{"role": "user", "content": "stream"}])) == "hi"
    backend.AI.vision("data:image/jpeg;base64,AA==", system="", user="v", model="meta-llama/llama-4-scout-17b-16e-instruct")

    r = client.get("/api/core/ai/usage", headers=ADMIN)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["calls"] == 3
    assert s["prompt_tokens"] == 360
    assert s["completion_tokens"] == 135
    assert s["cost_usd"] > 0.0
    assert client.get("/api/core/ai/usage").status_code == 401


# ---------------------------------------------------------------- 4. Failover single-charge + usage under succeeding provider
def test_failover_single_charge_and_records_succeeding_provider(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock_tok")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fb")
    backend.CONFIG.set("ai.provider", "bedrock", "default")
    backend.CONFIG.set("ai.fallback", "groq", "default")
    backend.CONFIG.set("limits.ai_daily", 5, "default")
    monkeypatch.setattr(backend.HTTP, "get", lambda *a, **kw: FakeResp({"data": [{"id": "llama-3.3-70b-versatile"}]}))

    def fake_post(url, *a, **kw):
        if "bedrock-mantle" in url:
            return FakeResp({"error": "down"}, status=503)
        return FakeResp({
            "choices": [{"message": {"content": "from groq"}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 50},
        })

    monkeypatch.setattr(backend.HTTP, "post", fake_post)
    try:
        assert backend.AI.complete([{"role": "user", "content": "hi"}]) == "from groq"
        summary = backend.AI.ledger.daily_summary("default")
        assert summary["calls"] == 1
        assert summary["prompt_tokens"] == 200 and summary["completion_tokens"] == 50
        assert backend.AI.ledger._rows[-1]["provider"] == "groq"
    finally:
        backend.CONFIG.unset("ai.provider", "default")
        backend.CONFIG.unset("ai.fallback", "default")
        backend.CONFIG.unset("limits.ai_daily", "default")

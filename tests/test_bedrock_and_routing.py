"""Phase 9 tests: BedrockProvider, per-tenant provider routing, cache isolation, failover, and eval harness."""
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

import app as backend
from core.ai.providers import BedrockProvider, resolve_provider
from core.data.tenancy import use_tenant
from core.sdk.context import BudgetExceeded
from tests.conftest import FakeResp
from tests.eval.runner import evaluate, load_questions


# ---------------------------------------------------------------- 1. BedrockProvider adapter
def test_bedrock_provider_chat_stream_vision(monkeypatch):
    seen = []

    class FakeHttp:
        def get(self, url, **kw):
            seen.append(("GET", url, kw))
            return FakeResp({"data": [{"id": "openai.gpt-oss-120b"}, {"id": "amazon.titan-embed-text-v2"}]})

        def post(self, url, **kw):
            seen.append(("POST", url, kw))
            if kw.get("stream"):
                return FakeResp(lines=['data: {"choices":[{"delta":{"content":"hi"}}]}', "data: [DONE]"])
            return FakeResp({"choices": [{"message": {"content": "bedrock reply"}}]})

    p = resolve_provider("ab_bedrock_tok", FakeHttp(), provider_name="bedrock", region="ap-south-1")
    assert isinstance(p, BedrockProvider)
    assert p.base_url == "https://bedrock-mantle.ap-south-1.api.aws/v1"
    assert p.supports("chat") and p.supports("stream") and p.supports("vision") and not p.supports("transcribe")

    models = p.list_models("ab_bedrock_tok")
    assert p.chat_model(models) == "openai.gpt-oss-120b"
    payload = p.chat_payload([{"role": "user", "content": "hi"}], "openai.gpt-oss-120b")
    r = p.post_chat("ab_bedrock_tok", payload)
    assert r.json()["choices"][0]["message"]["content"] == "bedrock reply"
    txt, used_model = p.vision("ab_bedrock_tok", "data:image/jpeg;base64,AA==", system="", user="what?", model="openai.gpt-oss-120b")
    assert txt == "bedrock reply" and used_model == "openai.gpt-oss-120b"
    assert seen[0][1] == "https://bedrock-mantle.ap-south-1.api.aws/v1/models"
    assert seen[1][2]["headers"] == {"Authorization": "Bearer ab_bedrock_tok"}


# ---------------------------------------------------------------- 2. Side-by-side multi-tenant isolation
def test_side_by_side_tenant_provider_and_cache_isolation(monkeypatch):
    tenants = backend.SERVICES.get("tenants")
    if not tenants.get("acme"):
        tenants.create("acme", "Acme")

    monkeypatch.setenv("GROQ_API_KEY", "gsk_tenant_default")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock_tenant_acme")

    def fake_get(url, *a, **kw):
        if "bedrock-mantle" in url:
            return FakeResp({"data": [{"id": "openai.gpt-oss-120b"}]})
        return FakeResp({"data": [{"id": "llama-3.3-70b-versatile"}]})

    def fake_post(url, *a, **kw):
        tag = "from-bedrock" if "bedrock-mantle" in url else "from-groq"
        return FakeResp({"choices": [{"message": {"content": tag}}]})

    monkeypatch.setattr(backend.HTTP, "get", fake_get)
    monkeypatch.setattr(backend.HTTP, "post", fake_post)
    backend.CONFIG.set("ai.provider", "groq", "default")
    backend.CONFIG.set("ai.provider", "bedrock", "acme")
    backend.CONFIG.set("ai.region", "us-east-1", "acme")
    try:
        def call_as(tenant):
            with use_tenant(tenant):
                m = backend.AI.chat_model()
                k = backend.AI.cache_key([{"role": "user", "content": "same prompt"}])
                ans = backend.AI.complete([{"role": "user", "content": "same prompt"}])
                return m, k, ans

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_def = pool.submit(call_as, "default")
            f_acme = pool.submit(call_as, "acme")
            m_def, k_def, a_def = f_def.result()
            m_acme, k_acme, a_acme = f_acme.result()

        assert m_def == "llama-3.3-70b-versatile" and a_def == "from-groq"
        assert m_acme == "openai.gpt-oss-120b" and a_acme == "from-bedrock"
        assert k_def != k_acme
    finally:
        backend.CONFIG.unset("ai.provider", "default")
        backend.CONFIG.unset("ai.provider", "acme")
        backend.CONFIG.unset("ai.region", "acme")


# ---------------------------------------------------------------- 3. Per-tenant ai.chat_model beats grok_model.txt
def test_per_tenant_chat_model_beats_model_file(tmp_path, monkeypatch):
    mf = tmp_path / "grok_model.txt"
    mf.write_text("file-override-model\n", "utf-8")
    monkeypatch.setattr(backend, "MODEL_FILE", mf)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock_tok")
    backend.CONFIG.set("ai.provider", "bedrock", "default")
    backend.CONFIG.set("ai.chat_model", "tenant-bedrock-model", "default")
    try:
        assert backend.AI.chat_model() == "tenant-bedrock-model"
        backend.CONFIG.unset("ai.chat_model", "default")
        assert backend.AI.chat_model() == "file-override-model"
    finally:
        backend.CONFIG.unset("ai.provider", "default")
        backend.CONFIG.unset("ai.chat_model", "default")


# ---------------------------------------------------------------- 4. Automatic failover (spend counted ONCE)
def test_failover_on_outage_counts_spend_once(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock_tok")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fb")
    backend.CONFIG.set("ai.provider", "bedrock", "default")
    backend.CONFIG.set("ai.fallback", "groq", "default")
    backend.CONFIG.set("limits.ai_daily", 5, "default")
    calls = []

    def fake_post(url, *a, **kw):
        calls.append(url)
        if "bedrock-mantle" in url:
            raise requests.Timeout("bedrock timeout")
        return FakeResp({"choices": [{"message": {"content": "groq fallback answer"}}]})

    monkeypatch.setattr(backend.HTTP, "post", fake_post)
    try:
        ans = backend.AI.complete([{"role": "user", "content": "hi"}])
        assert ans == "groq fallback answer"
        assert len(calls) == 2 and "bedrock-mantle" in calls[0] and "api.groq.com" in calls[1]
        # spend() was called ONCE despite 2 provider attempts
        assert sum(backend.AI._spent.values()) == 1

        # Stream pre-first-chunk failover also works
        def fake_stream_post(url, *a, **kw):
            if "bedrock-mantle" in url:
                return FakeResp({"error": "upstream down"}, status=503)
            resp = FakeResp({})
            resp.iter_lines = lambda decode_unicode=True: iter(['data: {"choices":[{"delta":{"content":"ok"}}]}', "data: [DONE]"])
            return resp

        monkeypatch.setattr(backend.HTTP, "post", fake_stream_post)
        assert "".join(backend.AI.stream([{"role": "user", "content": "hi"}])) == "ok"
    finally:
        backend.CONFIG.unset("ai.provider", "default")
        backend.CONFIG.unset("ai.fallback", "default")
        backend.CONFIG.unset("limits.ai_daily", "default")


# ---------------------------------------------------------------- 5. No failover on BudgetExceeded or 400
def test_no_failover_on_budget_exceeded_or_400(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock_tok")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fb")
    backend.CONFIG.set("ai.provider", "bedrock", "default")
    backend.CONFIG.set("ai.fallback", "groq", "default")
    backend.CONFIG.set("limits.ai_daily", 1, "default")
    calls = []

    def fake_400(url, *a, **kw):
        calls.append(url)
        return FakeResp({"error": "bad request"}, status=400)

    monkeypatch.setattr(backend.HTTP, "post", fake_400)
    try:
        # 400 from primary must NOT fail over to groq
        with pytest.raises(RuntimeError, match="HTTP 400"):
            backend.AI.complete([{"role": "user", "content": "hi"}])
        assert len(calls) == 1

        # BudgetExceeded (limit=1 already spent above) must NOT fail over
        with pytest.raises(BudgetExceeded):
            backend.AI.complete([{"role": "user", "content": "hi2"}])
        assert len(calls) == 1
    finally:
        backend.CONFIG.unset("ai.provider", "default")
        backend.CONFIG.unset("ai.fallback", "default")
        backend.CONFIG.unset("limits.ai_daily", "default")


# ---------------------------------------------------------------- 6. Eval harness verification
def test_eval_harness_scores_offline():
    items = load_questions()
    assert len(items) == 10

    def mock_ask(q, style):
        return {
            "answer": "SEBI order on Reliance Industries EPS trading window court abate Section 15H penalty.",
            "mode": "pdf",
            "citations": [{"n": 1, "page": 31}, {"n": 2, "page": 1}, {"n": 3, "page": 11}],
        }

    report = evaluate(mock_ask, items, provider="mock")
    assert report["count"] == 10
    assert report["citation_recall"] >= 0.8
    assert report["format_compliance"] == 1.0
    assert report["fact_hit_rate"] == 1.0

"""Phase 10 tests: Microsoft FoundryProvider, Entra/API-key auth, 3-cloud isolation, failover, and eval CLI."""
import sys
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

import app as backend
from core.ai.providers import FoundryProvider, normalize_foundry_endpoint, resolve_provider
from core.data.tenancy import use_tenant
from tests.conftest import FakeResp
from tests.eval.runner import main as eval_main


# ---------------------------------------------------------------- 1. URL normalization & dual auth headers
def test_foundry_url_normalization_and_auth_headers(monkeypatch):
    assert normalize_foundry_endpoint("https://my-res.services.ai.azure.com/") == "https://my-res.services.ai.azure.com/openai/v1"
    assert normalize_foundry_endpoint("https://my-res.services.ai.azure.com/openai/v1/") == "https://my-res.services.ai.azure.com/openai/v1"
    assert normalize_foundry_endpoint("https://my-res.services.ai.azure.com/v1") == "https://my-res.services.ai.azure.com/v1"

    seen = []

    class FakeHttp:
        def get(self, url, **kw):
            seen.append(("GET", url, kw.get("headers")))
            return FakeResp({"data": [{"id": "gpt-4o"}]})

        def post(self, url, **kw):
            seen.append(("POST", url, kw.get("headers")))
            return FakeResp({"choices": [{"message": {"content": "foundry ok"}}]})

    # API key mode -> api-key header, NO Authorization header
    p_key = resolve_provider("az_key_123", FakeHttp(), provider_name="foundry",
                             env={"AZURE_FOUNDRY_ENDPOINT": "https://res.services.ai.azure.com"})
    assert isinstance(p_key, FoundryProvider)
    assert p_key.base_url == "https://res.services.ai.azure.com/openai/v1"
    p_key.list_models("az_key_123")
    assert seen[-1][2] == {"api-key": "az_key_123"}

    # Entra ID token mode -> Authorization: Bearer, NO api-key header
    p_tok = resolve_provider("entra_tok_456", FakeHttp(), provider_name="foundry",
                             env={"AZURE_FOUNDRY_ENDPOINT": "https://res.services.ai.azure.com",
                                  "AZURE_FOUNDRY_AUTH_TOKEN": "entra_tok_456"})
    p_tok.list_models("entra_tok_456")
    assert seen[-1][2] == {"Authorization": "Bearer entra_tok_456"}

    # Entra ID DefaultAzureCredential lazy-import mode
    fake_az = types.ModuleType("azure")
    fake_id = types.ModuleType("azure.identity")

    class FakeCred:
        def get_token(self, scope):
            assert scope == "https://ai.azure.com/.default"
            return type("Tok", (), {"token": "lazy_entra_tok", "expires_on": 9999999999.0})()

    fake_id.DefaultAzureCredential = FakeCred
    fake_az.identity = fake_id
    monkeypatch.setitem(sys.modules, "azure", fake_az)
    monkeypatch.setitem(sys.modules, "azure.identity", fake_id)

    p_lazy = resolve_provider("__entra__", FakeHttp(), provider_name="foundry",
                              env={"AZURE_FOUNDRY_ENDPOINT": "https://res.services.ai.azure.com",
                                   "AZURE_FOUNDRY_USE_ENTRA": "1"})
    p_lazy.list_models("__entra__")
    assert seen[-1][2] == {"Authorization": "Bearer lazy_entra_tok"}


# ---------------------------------------------------------------- 2. Chat, stream, and vision
def test_foundry_chat_stream_and_vision():
    class FakeHttp:
        def post(self, url, **kw):
            if kw.get("stream"):
                r = FakeResp({})
                r.iter_lines = lambda decode_unicode=True: iter(['data: {"choices":[{"delta":{"content":"az"}}]}', "data: [DONE]"])
                return r
            return FakeResp({"choices": [{"message": {"content": "foundry answer"}}]})

    p = FoundryProvider(FakeHttp(), endpoint="https://res.services.ai.azure.com", auth_mode="api_key", default_model="gpt-4o")
    assert p.chat_model([]) == "gpt-4o"
    payload = p.chat_payload([{"role": "user", "content": "hi"}], "gpt-4o")
    assert p.post_chat("k1", payload).json()["choices"][0]["message"]["content"] == "foundry answer"
    txt, m = p.vision("k1", "data:image/jpeg;base64,AA==", system="", user="what?", model="gpt-4o")
    assert txt == "foundry answer" and m == "gpt-4o"


# ---------------------------------------------------------------- 3. 3-Cloud side-by-side isolation
def test_three_cloud_side_by_side_tenant_isolation(monkeypatch):
    tenants = backend.SERVICES.get("tenants")
    for tid, name in (("acme", "Acme"), ("contoso", "Contoso")):
        if not tenants.get(tid):
            tenants.create(tid, name)

    monkeypatch.setenv("GROQ_API_KEY", "gsk_3cloud")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock_3cloud")
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://contoso.services.ai.azure.com")
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "az_3cloud")

    def fake_get(url, *a, **kw):
        if "bedrock-mantle" in url:
            return FakeResp({"data": [{"id": "openai.gpt-oss-120b"}]})
        if "services.ai.azure.com" in url:
            return FakeResp({"data": [{"id": "gpt-4o"}]})
        return FakeResp({"data": [{"id": "llama-3.3-70b-versatile"}]})

    def fake_post(url, *a, **kw):
        if "bedrock-mantle" in url:
            return FakeResp({"choices": [{"message": {"content": "ans-bedrock"}}]})
        if "services.ai.azure.com" in url:
            return FakeResp({"choices": [{"message": {"content": "ans-foundry"}}]})
        return FakeResp({"choices": [{"message": {"content": "ans-groq"}}]})

    monkeypatch.setattr(backend.HTTP, "get", fake_get)
    monkeypatch.setattr(backend.HTTP, "post", fake_post)

    backend.CONFIG.set("ai.provider", "groq", "default")
    backend.CONFIG.set("ai.provider", "bedrock", "acme")
    backend.CONFIG.set("ai.region", "us-east-1", "acme")
    backend.CONFIG.set("ai.provider", "foundry", "contoso")
    try:
        def run_for(t):
            with use_tenant(t):
                return (
                    backend.AI.chat_model(),
                    backend.AI.cache_key([{"role": "user", "content": "q"}]),
                    backend.AI.complete([{"role": "user", "content": "q"}]),
                )

        with ThreadPoolExecutor(max_workers=3) as pool:
            f1 = pool.submit(run_for, "default")
            f2 = pool.submit(run_for, "acme")
            f3 = pool.submit(run_for, "contoso")
            m1, k1, a1 = f1.result()
            m2, k2, a2 = f2.result()
            m3, k3, a3 = f3.result()

        assert (m1, a1) == ("llama-3.3-70b-versatile", "ans-groq")
        assert (m2, a2) == ("openai.gpt-oss-120b", "ans-bedrock")
        assert (m3, a3) == ("gpt-4o", "ans-foundry")
        assert len({k1, k2, k3}) == 3
    finally:
        for t in ("default", "acme", "contoso"):
            backend.CONFIG.unset("ai.provider", t)
        backend.CONFIG.unset("ai.region", "acme")


# ---------------------------------------------------------------- 4. Cross-cloud failover (spend once)
def test_cross_cloud_failover_foundry_to_bedrock(monkeypatch):
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://contoso.services.ai.azure.com")
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "az_key")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock_key")
    backend.CONFIG.set("ai.provider", "foundry", "default")
    backend.CONFIG.set("ai.fallback", "bedrock", "default")
    backend.CONFIG.set("limits.ai_daily", 5, "default")
    calls = []

    def fake_post(url, *a, **kw):
        calls.append(url)
        if "services.ai.azure.com" in url:
            return FakeResp({"error": "foundry down"}, status=503)
        return FakeResp({"choices": [{"message": {"content": "bedrock rescue"}}]})

    monkeypatch.setattr(backend.HTTP, "post", fake_post)
    try:
        assert backend.AI.complete([{"role": "user", "content": "hi"}]) == "bedrock rescue"
        assert len(calls) == 2 and "services.ai.azure.com" in calls[0] and "bedrock-mantle" in calls[1]
        assert sum(backend.AI._spent.values()) == 1
    finally:
        backend.CONFIG.unset("ai.provider", "default")
        backend.CONFIG.unset("ai.fallback", "default")
        backend.CONFIG.unset("limits.ai_daily", "default")


# ---------------------------------------------------------------- 5. Eval CLI --provider foundry --mock
def test_eval_cli_accepts_foundry_mock(capsys):
    rc = eval_main(["--provider", "foundry", "--mock"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"count": 10' in out

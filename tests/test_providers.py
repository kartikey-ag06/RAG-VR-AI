"""Unit tests for core.ai.providers (Phase 8)."""
import pytest

from core.ai import AIGateway
from core.ai.providers import (
    GroqProvider,
    Provider,
    ProviderCapabilities,
    XAIProvider,
    resolve_provider,
)


class _DummyHttp:
    def get(self, *a, **kw):
        raise AssertionError("unexpected get")

    def post(self, *a, **kw):
        raise AssertionError("unexpected post")


def test_provider_capabilities_and_resolve_provider():
    http = _DummyHttp()
    assert resolve_provider(None, http) is None
    assert resolve_provider("", http) is None

    groq = resolve_provider("gsk_123", http)
    assert isinstance(groq, GroqProvider) and isinstance(groq, Provider)
    assert groq.name == "groq"
    assert groq.capabilities == ProviderCapabilities(chat=True, stream=True, vision=True, embed=False, transcribe=True)
    assert groq.supports("vision") and groq.supports("transcribe") and not groq.supports("embed")

    xai = resolve_provider("xai-123", http)
    assert isinstance(xai, XAIProvider) and isinstance(xai, Provider)
    assert xai.name == "grok"
    assert xai.capabilities == ProviderCapabilities(chat=True, stream=True, vision=False, embed=False, transcribe=False)
    assert xai.supports("chat") and xai.supports("stream")
    assert not xai.supports("vision") and not xai.supports("transcribe") and not xai.supports("embed")


def test_embed_raises_not_implemented():
    http = _DummyHttp()
    for p in (GroqProvider(http), XAIProvider(http)):
        with pytest.raises(NotImplementedError):
            p.embed("key", ["hello"])


def test_gateway_supports_and_active_provider(tmp_path):
    kf, mf = tmp_path / "k.txt", tmp_path / "m.txt"
    g_none = AIGateway(_DummyHttp(), {}, key_file=kf, model_file=mf)
    assert g_none.active_provider() is None
    assert not g_none.supports("chat") and not g_none.supports("vision")

    g_groq = AIGateway(_DummyHttp(), {"GROQ_API_KEY": "gsk_abc"}, key_file=kf, model_file=mf)
    assert isinstance(g_groq.active_provider(), GroqProvider)
    assert g_groq.supports("chat") and g_groq.supports("vision") and g_groq.supports("transcribe")
    assert g_groq.provider() == {"name": "groq", "base": "https://api.groq.com/openai/v1"}

    g_xai = AIGateway(_DummyHttp(), {"XAI_API_KEY": "xai-abc"}, key_file=kf, model_file=mf)
    assert isinstance(g_xai.active_provider(), XAIProvider)
    assert g_xai.supports("chat") and not g_xai.supports("vision") and not g_xai.supports("transcribe")
    assert g_xai.provider() == {"name": "grok", "base": "https://api.x.ai/v1"}

"""Provider registry / resolver for Core AI (Phases 8-10)."""
from typing import Optional

from .base import Provider, ProviderCapabilities
from .bedrock import BedrockProvider, resolve_bedrock_region
from .foundry import (
    FoundryProvider,
    normalize_foundry_endpoint,
    resolve_foundry_auth_mode,
    resolve_foundry_endpoint,
)
from .openai_compat import GroqProvider, OpenAICompatProvider, XAIProvider


def resolve_provider(
    key: Optional[str],
    http,
    provider_name: str = "",
    region: str = "",
    env=None,
) -> Optional[Provider]:
    """Return the Provider adapter for an explicit provider name or key prefix, or None if unconfigured."""
    if not key:
        return None
    name = (provider_name or "").strip().lower()
    if name == "groq":
        return GroqProvider(http)
    if name == "grok":
        return XAIProvider(http)
    if name == "bedrock":
        return BedrockProvider(http, region=resolve_bedrock_region(region, env))
    if name == "foundry":
        ep = resolve_foundry_endpoint(region, env)
        if not ep:
            return None
        env_map = env or {}
        default_model = env_map.get("AZURE_FOUNDRY_MODEL") or env_map.get("AZURE_OPENAI_DEPLOYMENT") or ""
        auth_mode = resolve_foundry_auth_mode(key, env_map)
        return FoundryProvider(http, endpoint=ep, auth_mode=auth_mode, default_model=default_model, env=env_map)
    if name:
        return None
    if key.startswith("gsk_"):
        return GroqProvider(http)
    return XAIProvider(http)


__all__ = [
    "Provider",
    "ProviderCapabilities",
    "OpenAICompatProvider",
    "GroqProvider",
    "XAIProvider",
    "BedrockProvider",
    "FoundryProvider",
    "resolve_bedrock_region",
    "normalize_foundry_endpoint",
    "resolve_foundry_endpoint",
    "resolve_provider",
]

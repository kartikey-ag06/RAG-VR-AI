"""Microsoft / Azure AI Foundry OpenAI-compatible provider adapter (Phase 10)."""
import re
import time

from .base import ProviderCapabilities
from .openai_compat import OpenAICompatProvider

FOUNDRY_CHAT_PREFERENCE = ["gpt-4o", "gpt-4.1", "gpt-4o-mini", "DeepSeek-V3", "Llama-3.3-70B-Instruct"]
FOUNDRY_FAST_PREFERENCE = ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4.1-nano"]
FOUNDRY_SCOPE = "https://ai.azure.com/.default"


def normalize_foundry_endpoint(endpoint: str) -> str:
    ep = (endpoint or "").strip().rstrip("/")
    if not ep:
        return ""
    if not (ep.endswith("/openai/v1") or ep.endswith("/v1")):
        ep = f"{ep}/openai/v1"
    return ep


def resolve_foundry_endpoint(region_or_url: str = "", env=None) -> str:
    env = env or {}
    cand = (region_or_url or "").strip()
    if cand.startswith(("https://", "http://")):
        return normalize_foundry_endpoint(cand)
    raw = env.get("AZURE_FOUNDRY_ENDPOINT") or env.get("AZURE_OPENAI_ENDPOINT") or ""
    return normalize_foundry_endpoint(raw)


def resolve_foundry_auth_mode(key: str = "", env=None) -> str:
    env = env or {}
    if env.get("AZURE_FOUNDRY_AUTH_TOKEN") or env.get("AZURE_OPENAI_AUTH_TOKEN") or env.get("AZURE_FOUNDRY_USE_ENTRA") == "1" or key == "__entra__":
        return "entra"
    return "api_key"


class FoundryProvider(OpenAICompatProvider):
    """Microsoft Foundry adapter using OpenAI v1 API with API key or Entra ID Bearer auth."""

    def __init__(self, http, endpoint: str, auth_mode: str = "api_key", default_model: str = "", env=None):
        self.auth_mode = auth_mode
        self.default_model = (default_model or "").strip()
        self.env = env or {}
        self._cred = None
        self._entra_cache = {"token": "", "expires_on": 0.0}
        super().__init__(
            http,
            name="foundry",
            base_url=normalize_foundry_endpoint(endpoint),
            capabilities=ProviderCapabilities(chat=True, stream=True, vision=True, embed=False, transcribe=False),
        )

    def _entra_token(self) -> str:
        now = time.time()
        if self._entra_cache["token"] and now < self._entra_cache["expires_on"] - 60:
            return self._entra_cache["token"]
        from azure.identity import DefaultAzureCredential  # lazy optional import (Phase 10)
        if self._cred is None:
            self._cred = DefaultAzureCredential()
        tok = self._cred.get_token(FOUNDRY_SCOPE)
        self._entra_cache = {"token": tok.token, "expires_on": float(getattr(tok, "expires_on", now + 300))}
        return tok.token

    def _auth(self, key: str) -> dict:
        if self.auth_mode == "entra":
            tok = self._entra_token() if (not key or key == "__entra__") else key
            return {"Authorization": f"Bearer {tok}"}
        return {"api-key": key}

    def chat_model(self, models: list[str], configured: str = "", fast: bool = False) -> str:
        if configured:
            return configured
        if self.default_model and not fast:
            return self.default_model
        if fast:
            return next((m for m in FOUNDRY_FAST_PREFERENCE if m in models), "") or self.default_model or ""
        return (
            next((m for m in FOUNDRY_CHAT_PREFERENCE if m in models), None)
            or next((m for m in models if not re.search("embed|rerank|whisper|tts", m, re.I)), None)
            or FOUNDRY_CHAT_PREFERENCE[0]
        )

    def vision(
        self,
        key: str,
        image_data_url: str,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int = 700,
        timeout: int = 90,
    ) -> tuple[str, str]:
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        ]
        body = {"model": model, "messages": msgs, "temperature": 0.2, "max_tokens": max_tokens}
        r = None
        for attempt in (1, 2):
            r = self.http.post(
                f"{self.base_url}/chat/completions",
                headers=self._auth(key),
                json=body,
                timeout=timeout,
            )
            if r.status_code != 429 or attempt == 2:
                break
            m = re.search(r"try again in ([\d.]+)s", r.text)
            wait = min(float(m.group(1)) + 0.5, 15.0) if m else 3.0
            time.sleep(wait)
        if r.status_code == 429:
            raise RuntimeError("Foundry rate limit reached. Wait a few seconds and try again.")
        if r.status_code != 200:
            raise RuntimeError(f"foundry vision ({model}): HTTP {r.status_code} - {r.text[:300]}")
        payload = r.json()
        self.last_usage = payload.get("usage") or {}
        return payload["choices"][0]["message"]["content"] or "", model

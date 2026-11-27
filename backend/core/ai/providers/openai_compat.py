"""OpenAI-compatible provider adapters: Groq and xAI (Phase 8)."""
import re
import time

from .base import ProviderCapabilities

CHAT_PREFERENCE = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
FAST_PREFERENCE = ["openai/gpt-oss-20b", "llama-3.1-8b-instant"]
STT_PREFERENCE = ["whisper-large-v3-turbo", "whisper-large-v3"]
XAI_CHAT_MODEL = "grok-4.7"


class OpenAICompatProvider:
    """Shared HTTP transport for providers exposing OpenAI-compatible /models and /chat/completions."""

    def __init__(self, http, name: str, base_url: str, capabilities: ProviderCapabilities):
        self.http = http
        self.name = name
        self.base_url = base_url
        self.capabilities = capabilities

    def supports(self, cap: str) -> bool:
        return bool(getattr(self.capabilities, cap, False))

    def _auth(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}"}

    def list_models(self, key: str) -> list[str]:
        r = self.http.get(f"{self.base_url}/models", headers=self._auth(key), timeout=20)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

    def chat_model(self, models: list[str], configured: str = "", fast: bool = False) -> str:
        if configured:
            return configured
        return models[0] if models else ""

    def chat_payload(
        self,
        messages: list[dict],
        model: str,
        *,
        max_tokens: int = 700,
        stream: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> dict:
        payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
        if stream:
            payload["stream"] = True
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        return payload

    def post_chat(self, key: str, payload: dict, *, stream: bool = False, timeout: int = 90):
        extra = {"stream": True} if stream else {}
        return self.http.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._auth(key),
            timeout=timeout,
            **extra,
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
        raise RuntimeError(f"{self.name} does not support vision")

    def transcribe(
        self,
        key: str,
        audio: bytes,
        mime: str,
        ext: str,
        *,
        model: str,
        lang: str = "",
        hint: str = "",
        timeout: int = 60,
    ):
        raise RuntimeError(f"{self.name} does not support speech-to-text")

    def embed(self, key: str, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(f"{self.name} does not implement embed()")


class GroqProvider(OpenAICompatProvider):
    """Groq OpenAI-compatible adapter (chat, stream, vision, Whisper STT)."""

    def __init__(self, http):
        super().__init__(
            http,
            name="groq",
            base_url="https://api.groq.com/openai/v1",
            capabilities=ProviderCapabilities(chat=True, stream=True, vision=True, transcribe=True, embed=False),
        )

    def chat_model(self, models: list[str], configured: str = "", fast: bool = False) -> str:
        if configured:
            return configured
        if fast:
            return next((m for m in FAST_PREFERENCE if m in models), "") or ""
        return next((m for m in CHAT_PREFERENCE if m in models), None) or next(
            (m for m in models if not re.search("whisper|tts|guard|embed", m)),
            CHAT_PREFERENCE[0],
        )

    def stt_model(self, models: list[str]) -> str:
        return next((m for m in STT_PREFERENCE if m in models), None) or next(
            (m for m in models if "whisper" in m),
            STT_PREFERENCE[0],
        )

    def chat_payload(
        self,
        messages: list[dict],
        model: str,
        *,
        max_tokens: int = 700,
        stream: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> dict:
        payload = super().chat_payload(
            messages, model, max_tokens=max_tokens, stream=stream, tools=tools, tool_choice=tool_choice
        )
        if "gpt-oss" in model:
            payload["reasoning_effort"] = "low"
        return payload

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
            wait = float(m.group(1)) + 0.5 if m else 3.0
            if wait > 15:
                break
            time.sleep(wait)
        if r.status_code == 429:
            raise RuntimeError("Groq free-tier limit reached (tokens per minute). Wait a few seconds and scan again.")
        if r.status_code != 200:
            raise RuntimeError(f"groq vision ({model}): HTTP {r.status_code} - {r.text[:300]}")
        payload = r.json()
        self.last_usage = payload.get("usage") or {}
        return payload["choices"][0]["message"]["content"] or "", model

    def transcribe(
        self,
        key: str,
        audio: bytes,
        mime: str,
        ext: str,
        *,
        model: str,
        lang: str = "",
        hint: str = "",
        timeout: int = 60,
    ):
        data = dict(
            {"model": model, "response_format": "json", "temperature": "0"},
            **({"language": lang} if lang else {}),
            **({"prompt": hint} if hint else {}),
        )
        return self.http.post(
            f"{self.base_url}/audio/transcriptions",
            headers=self._auth(key),
            files={"file": (f"speech.{ext}", audio, mime)},
            data=data,
            timeout=timeout,
        )


class XAIProvider(OpenAICompatProvider):
    """xAI / Grok OpenAI-compatible adapter (chat and stream)."""

    def __init__(self, http):
        super().__init__(
            http,
            name="grok",
            base_url="https://api.x.ai/v1",
            capabilities=ProviderCapabilities(chat=True, stream=True, vision=False, transcribe=False, embed=False),
        )

    def chat_model(self, models: list[str], configured: str = "", fast: bool = False) -> str:
        return configured or XAI_CHAT_MODEL

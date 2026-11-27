"""Amazon Bedrock Mantle OpenAI-compatible provider adapter (Phase 9)."""
import re
import time

from .base import ProviderCapabilities
from .openai_compat import OpenAICompatProvider

BEDROCK_CHAT_PREFERENCE = [
    "openai.gpt-oss-120b",
    "openai.gpt-oss-20b",
    "anthropic.claude-3-5-sonnet",
    "meta.llama3-3-70b-instruct",
]


def resolve_bedrock_region(configured: str = "", env=None) -> str:
    if configured:
        return configured.strip()
    env = env or {}
    return (env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1").strip()


class BedrockProvider(OpenAICompatProvider):
    """Amazon Bedrock Mantle adapter (OpenAI-compatible /v1/models and /v1/chat/completions)."""

    def __init__(self, http, region: str = "us-east-1"):
        self.region = region or "us-east-1"
        super().__init__(
            http,
            name="bedrock",
            base_url=f"https://bedrock-mantle.{self.region}.api.aws/v1",
            capabilities=ProviderCapabilities(chat=True, stream=True, vision=True, embed=False, transcribe=False),
        )

    def chat_model(self, models: list[str], configured: str = "", fast: bool = False) -> str:
        if configured:
            return configured
        if fast:
            return next((m for m in BEDROCK_CHAT_PREFERENCE if "20b" in m and m in models), "") or ""
        return (
            next((m for m in BEDROCK_CHAT_PREFERENCE if m in models), None)
            or next((m for m in models if not re.search("embed|rerank", m, re.I)), None)
            or BEDROCK_CHAT_PREFERENCE[0]
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
            raise RuntimeError("Bedrock rate limit reached. Wait a few seconds and try again.")
        if r.status_code != 200:
            raise RuntimeError(f"bedrock vision ({model}): HTTP {r.status_code} - {r.text[:300]}")
        payload = r.json()
        self.last_usage = payload.get("usage") or {}
        return payload["choices"][0]["message"]["content"] or "", model

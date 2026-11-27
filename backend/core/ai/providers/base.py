"""Provider interface and capability flags for Core AI (Phase 8)."""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ProviderCapabilities:
    chat: bool = True
    stream: bool = True
    vision: bool = False
    embed: bool = False
    transcribe: bool = False


@runtime_checkable
class Provider(Protocol):
    name: str
    base_url: str
    capabilities: ProviderCapabilities

    def supports(self, cap: str) -> bool: ...

    def list_models(self, key: str) -> list[str]: ...

    def chat_model(self, models: list[str], configured: str = "", fast: bool = False) -> str: ...

    def chat_payload(
        self,
        messages: list[dict],
        model: str,
        *,
        max_tokens: int = 700,
        stream: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> dict: ...

    def post_chat(self, key: str, payload: dict, *, stream: bool = False, timeout: int = 90): ...

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
    ) -> tuple[str, str]: ...

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
    ): ...

    def embed(self, key: str, texts: list[str]) -> list[list[float]]: ...

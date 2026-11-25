"""Prompt registry (Phase 4, ADR 0004): every system prompt the AI gets is registered with an id and a default text.

A tenant can override a prompt through the config store (key "prompts.<id>", e.g. "prompts.answer.system")
without a code change or redeploy. Clients never send prompts. The default TEXTS belong to whoever registers them
(today api_v1.py, later a sector pack) - the core holds no sector wording.
"""
from typing import Dict, Optional


class PromptRegistry:
    def __init__(self, config=None):
        self.config = config
        self.defaults: Dict[str, str] = {}

    def register(self, pid: str, default: str, description: str = "") -> None:
        self.defaults[pid] = default
        if self.config is not None and f"prompts.{pid}" not in self.config.schema:
            self.config.declare(f"prompts.{pid}", "string", default,
                                description or f"System prompt '{pid}' (override per tenant)", max_length=8000)

    def get(self, pid: str, tenant_id: Optional[str] = None) -> str:
        if self.config is not None and f"prompts.{pid}" in self.config.schema:
            return self.config.get(f"prompts.{pid}", tenant_id)
        return self.defaults[pid]

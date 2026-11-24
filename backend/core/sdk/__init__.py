"""core.sdk - the contract between the Core and every capability / sector pack (ADR 0003).

Packs import only from here:
    from core.sdk import CoreContext, Manifest, Event, PolicyDenied
"""
from .context import (Answer, BudgetExceeded, Config, CoreContext, Events, Kv, Llm, ModuleDisabled, Passage,
                      PolicyDenied, Principal, SdkError)
from .events import CORE_EVENTS, Event, check_event_name
from .manifest import ConfigItem, Manifest, McpTool, load_manifest
from .scopes import CORE_SCOPES
from .version import SDK_VERSION, satisfies

__all__ = [
    "SDK_VERSION", "satisfies",
    "CoreContext", "Llm", "Kv", "Config", "Events", "Principal", "Passage", "Answer",
    "SdkError", "PolicyDenied", "ModuleDisabled", "BudgetExceeded",
    "Manifest", "ConfigItem", "McpTool", "load_manifest",
    "Event", "CORE_EVENTS", "check_event_name", "CORE_SCOPES",
]

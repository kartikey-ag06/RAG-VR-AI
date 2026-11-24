"""Scopes = named permissions. A key or user holds scopes; every CoreContext call checks one (ADR 0003).

Core scopes look like "<area>:<action>". A pack may define its own scopes, which must start with its id:
"<pack_id>:<action>", e.g. "industrial_field_ops:approve_revision".
"""
import re

CORE_SCOPES = {
    "knowledge:read": "Search documents and read pages the caller may see",
    "knowledge:write": "Upload, delete and re-tag documents",
    "ai:ask": "Ask questions answered by the AI (spends AI budget)",
    "ai:vision": "Photo questions (spends AI budget)",
    "ai:speech": "Speech-to-text (spends AI budget)",
    "events:publish": "Publish the pack's own events",
    "connectors:use": "Send through or sync from configured connectors",
    "admin": "Control plane: packs, config, users, keys, budgets",
}

SCOPE = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")


def is_core_scope(scope: str) -> bool:
    return scope in CORE_SCOPES


def check_pack_scope(pack_id: str, scope: str) -> None:
    """A scope a pack *defines* must be '<pack_id>:<action>'. Raises ValueError otherwise."""
    if not SCOPE.match(scope):
        raise ValueError(f"bad scope {scope!r}: use '<area>:<action>' in lower case")
    if scope.split(":", 1)[0] != pack_id:
        raise ValueError(f"pack {pack_id!r} may only define scopes starting with '{pack_id}:' (got {scope!r})")

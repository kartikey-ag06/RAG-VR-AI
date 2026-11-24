"""Domain events: the Core announces what happened, packs listen (ADR 0003).

Name format: "<area>.<past_tense_verb>", lower case, e.g. "question.asked".
Core events use the areas below. A pack publishes only in its own area: "<pack_id>.<verb>".
"""
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

CORE_EVENTS = {
    "question.asked": "An answer was produced (question, doc pages used, confidence, mode, door)",
    "document.added": "A document finished indexing",
    "document.updated": "Title, tags, groups or a page revision of a document changed",
    "document.deleted": "A document was removed",
    "report.created": "A session report was generated (kind, session id)",
    "pack.enabled": "A pack was switched on for a tenant",
    "pack.disabled": "A pack was switched off for a tenant",
    "config.changed": "A config value changed (scope, key) - values are not included",
}


def check_event_name(name: str) -> None:
    if not EVENT_NAME.match(name):
        raise ValueError(f"bad event name {name!r}: use '<area>.<past_tense_verb>', e.g. 'question.asked'")


@dataclass(frozen=True)
class Event:
    """One event. `tenant_id` is always set by the Core, never by the publisher."""
    name: str
    tenant_id: str
    data: dict = field(default_factory=dict)
    source: str = "core"                         # "core" or the publishing pack id
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    schema: int = 1                              # bump when the data shape of this event name changes

    def __post_init__(self):
        check_event_name(self.name)
        if not self.tenant_id:
            raise ValueError("event needs a tenant_id")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "tenant_id": self.tenant_id, "source": self.source,
                "ts": self.ts, "schema": self.schema, "data": self.data}

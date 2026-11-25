"""KeyValueStore - what every small-record store must provide (Postgres `app_data` table or local JSON files).

Records are grouped by `kind` ("reminder", "session", "user", ...) and identified by `id` inside a kind.
Phase 4 adds the tenant and the module namespace in front of the kind (ADR 0002, ADR 0003).
"""
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class KeyValueStore(Protocol):
    def kv_put(self, kind: str, id_: str, data: Any) -> None:
        """Create or replace one record."""

    def kv_get(self, kind: str, id_: str) -> Optional[Any]:
        """One record's data, or None."""

    def kv_list(self, kind: str) -> list:
        """All records of a kind, newest change first."""

    def kv_delete(self, kind: str, id_: str) -> None:
        """Remove one record; no error if it does not exist."""

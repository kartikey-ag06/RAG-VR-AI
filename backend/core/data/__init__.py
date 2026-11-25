"""core.data - storage: optional Postgres (Neon) + pgvector, local-file fallback, and the KV contract."""
from .kv import KeyValueStore
from .local_kv import LocalKV
from .postgres import DB, SCHEMA, database_url, vec_literal

__all__ = ["DB", "SCHEMA", "LocalKV", "KeyValueStore", "database_url", "vec_literal"]

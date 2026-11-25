"""Moved to core/data (Phase 3 step 4). This shim keeps `from db import ...` working for older modules."""
from core.data import DB, SCHEMA, LocalKV, database_url, vec_literal  # noqa: F401

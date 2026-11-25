"""Meetings capability - still the old code through ctx.legacy (migration debt, ADR 0006)."""
import meetings


def register(ctx):
    L = ctx.legacy
    return meetings.register(L.app, L.kv, L.llm_complete, L.run_search, L.auth)

"""Connectors capability - still the old code through ctx.legacy (migration debt, ADR 0006)."""
import connectors


def register(ctx):
    L = ctx.legacy
    return connectors.register(L.app, L.kv, L.auth, L.store)

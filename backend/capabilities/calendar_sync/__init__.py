"""Calendar sync capability - still the old code through ctx.legacy (migration debt, ADR 0006)."""
import calsync


def register(ctx):
    L = ctx.legacy
    return calsync.register(L.app, L.kv, L.auth)

"""Remote expert capability - still the old code through ctx.legacy (migration debt, ADR 0006)."""
import expert


def register(ctx):
    L = ctx.legacy
    return expert.register(L.app, L.root, L.auth)

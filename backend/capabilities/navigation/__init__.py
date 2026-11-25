"""Navigation capability - still the old code through ctx.legacy (migration debt, ADR 0006)."""
import navigation


def register(ctx):
    return navigation.register(ctx.legacy.app)

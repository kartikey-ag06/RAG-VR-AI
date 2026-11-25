"""Assistant capability - still the old code through ctx.legacy (migration debt, ADR 0006)."""
import assistant


def register(ctx):
    L = ctx.legacy
    return assistant.register(L.app, L.kv, L.llm_complete, L.export("calendar_sync"), L.auth)

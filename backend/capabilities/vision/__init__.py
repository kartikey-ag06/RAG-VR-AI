"""Photo questions capability - still the old code through ctx.legacy (migration debt, ADR 0006)."""
import vision


def register(ctx):
    L = ctx.legacy
    out = vision.register(L.app, L.provider, L.get_key, L.list_models, L.root, L.auth, spend=L.spend, ai=L.ai)
    # /api/v1/scan (a Core route) uses this; it asks enabled() so switching "vision" off also stops scan
    return dict(out, enabled=lambda: ctx.kernel.modules.enabled(ctx.module_id))

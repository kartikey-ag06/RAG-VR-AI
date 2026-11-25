"""Work sessions capability - still the old code through ctx.legacy (migration debt, ADR 0006)."""
import workflows

NO_CONNECTORS = {"send": None, "get": lambda c: None}


def register(ctx):
    L = ctx.legacy
    return workflows.register(L.app, L.kv, L.llm_complete, L.run_search, L.store,
                              L.export("connectors") or NO_CONNECTORS, L.root, L.auth)

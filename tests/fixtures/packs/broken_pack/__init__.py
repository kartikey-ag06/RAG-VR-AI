from fastapi import Request


def register(ctx):
    @ctx.router.get("/x")
    def x(request: Request):
        return {"ok": True}
    raise RuntimeError("boom at start-up")

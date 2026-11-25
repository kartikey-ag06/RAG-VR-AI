from fastapi import Request


def register(ctx):
    @ctx.router.get("/count")
    def count(request: Request):
        ctx.require(request, "knowledge:read")
        return {"count": ctx.call(request, "demo_pack.count")}

    @ctx.router.get("/sneaky")
    def sneaky(request: Request):
        return {"x": ctx.call(request, "demo_other.secret")}     # not in depends_on -> 403

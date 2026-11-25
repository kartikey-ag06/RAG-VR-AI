"""Test pack: uses ONLY core.sdk, like a real sector pack must."""
from dataclasses import asdict

from fastapi import Body, Request

from core.sdk import PolicyDenied


def register(ctx):
    ctx.prompts.register("greet", "You greet {name} in one short sentence. Unknown stays {unknown}.")

    @ctx.router.get("/hello")
    def hello(request: Request):
        p = ctx.require(request, "knowledge:read")
        return {"tenant": p.tenant_id, "greeting": ctx.config.get(request, "greeting")}

    @ctx.router.get("/vision")
    def undeclared(request: Request):
        ctx.require(request, "ai:vision")                 # not in requires_scopes -> 403
        return {"ok": True}

    @ctx.router.post("/search")
    def search(request: Request, body: dict = Body(...)):
        return {"passages": [asdict(p) for p in ctx.search(request, body.get("query", ""), k=3)]}

    @ctx.router.post("/ask")
    def ask(request: Request, body: dict = Body(...)):
        a = ctx.answer(request, body.get("question", ""))
        return {"mode": a.mode, "text": a.text, "citations": len(a.citations)}

    @ctx.router.post("/greet")
    def greet(request: Request, body: dict = Body(...)):
        return {"text": ctx.llm.complete(request, prompt="greet", variables={"name": body.get("name", ""), "input": "hi"})}

    @ctx.router.post("/notes")
    def add_note(request: Request, body: dict = Body(...)):
        ctx.require(request, "knowledge:read")
        if len(ctx.kv.list(request, "note")) >= ctx.config.get(request, "max_notes"):
            raise PolicyDenied("too many notes", 409)
        nid = str(body.get("id", "n1"))
        ctx.kv.put(request, "note", nid, {"id": nid, "text": str(body.get("text", ""))[:200]})
        ctx.events.publish(request, "demo_pack.noted", {"id": nid})
        return {"saved": nid}

    @ctx.router.get("/notes")
    def notes(request: Request):
        ctx.require(request, "knowledge:read")
        return {"notes": ctx.kv.list(request, "note")}

    @ctx.router.get("/seen")
    def seen(request: Request):
        ctx.require(request, "knowledge:read")
        return {"seen": ctx.kv.list(request, "seen")}

    @ctx.router.get("/legacy")
    def legacy(request: Request):
        ctx.legacy                                        # packs never get old-style access -> 403
        return {"ok": True}

    @ctx.router.post("/approve")
    def approve(request: Request):
        ctx.require(request, "demo_pack:approve")         # the pack's own scope
        return {"approved": True}

    @ctx.events.subscribe("question.asked")
    def on_question(event):
        ctx.kv.put(event.tenant_id, "seen", event.id, {"question": event.data.get("question"),
                                                       "door": event.data.get("door"), "tenant": event.tenant_id})

    @ctx.events.subscribe("document.deleted")
    def on_deleted(event):
        raise RuntimeError("listener bug on purpose")     # must not stop other listeners or the request

    @ctx.mcp_tool("demo_pack_echo")
    def echo(request, arguments):
        return "echo: " + str(arguments.get("text", ""))

    @ctx.publish_function("count")
    def count(request):
        return len(ctx.kv.list(request, "note"))

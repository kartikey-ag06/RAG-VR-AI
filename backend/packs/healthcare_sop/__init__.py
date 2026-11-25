"""Healthcare Clinical SOP pack (Phase 7): proof that a second sector needs zero Core changes.

Imports ONLY core.sdk and reaches the Core exclusively through ctx.
"""
from collections import Counter

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from core.sdk import PolicyDenied

SIGNOFF = "healthcare_sop:signoff"
TOPICS_STREAM = "topics"

CHECKLIST_SYSTEM = (
    "You are a clinical SOP assistant. Using ONLY the provided document excerpts for topic '{topic}', "
    "write a numbered checklist with at most {max_steps} concise steps. Cite page numbers like (p. 3)."
)


def _top_topics(entries: list, limit: int = 10) -> list:
    counts = Counter(e.get("topic", "").strip().lower() for e in entries if e.get("topic"))
    return [{"topic": t, "count": c} for t, c in counts.most_common(max(1, min(50, limit)))]


def register(ctx):
    ctx.prompts.register("checklist", CHECKLIST_SYSTEM, "Healthcare SOP: generate a numbered clinical checklist")
    ctx.reports.register_kind(
        "clinical_audit",
        "Clinical Audit",
        "Focus on SOP compliance, patient-safety checks, deviations, and sign-off status.",
    )

    @ctx.events.subscribe("question.asked")
    def on_question(event):
        q = str(event.data.get("question") or "").strip()[:200]
        if q:
            ctx.journal.append(event.tenant_id, TOPICS_STREAM, {"topic": q, "door": event.data.get("door", "")})

    @ctx.router.post("/checklist")
    def make_checklist(request: Request, body: dict = Body(...)):
        p = ctx.require(request, "ai:ask")
        topic = str(body.get("topic") or "").strip()
        if not topic:
            return JSONResponse({"error": "topic is required"}, 400)
        max_steps = int(ctx.config.get(p.tenant_id, "max_steps") or 6)
        hits = ctx.search(request, topic, k=5)
        excerpts = "\n".join(f"- [{h.title} p.{h.page}] {h.text[:300]}" for h in hits) or "No excerpts found."
        checklist = ctx.llm.complete(
            request,
            prompt="checklist",
            variables={"topic": topic, "max_steps": max_steps, "input": excerpts},
            max_tokens=500,
        )
        citations = [{"doc_uid": h.doc_uid, "title": h.title, "page": h.page} for h in hits]
        ctx.kv.put(request, "checklist", str(len(ctx.kv.list(request, "checklist")) + 1),
                   {"topic": topic, "checklist": checklist, "citations": citations})
        return {"topic": topic, "max_steps": max_steps, "checklist": checklist, "citations": citations}

    @ctx.router.get("/topics")
    def list_topics(request: Request, limit: int = 10):
        ctx.require(request, "knowledge:read")
        entries = ctx.journal.list(request, TOPICS_STREAM)
        return {"topics": _top_topics(entries, limit)}

    @ctx.router.post("/signoff")
    def signoff(request: Request, body: dict = Body(...)):
        p = ctx.require(request, SIGNOFF)
        cid = str(body.get("checklist_id") or "").strip()
        if not cid:
            raise PolicyDenied("checklist_id is required", 400)
        return {"signed_off": True, "checklist_id": cid, "by": p.name or p.id or "admin"}

    @ctx.mcp_tool("healthcare_sop_topics")
    def mcp_topics(request, arguments):
        limit = int((arguments or {}).get("limit") or 5)
        entries = ctx.journal.list(request, TOPICS_STREAM)
        top = _top_topics(entries, limit)
        if not top:
            return "No SOP topics recorded yet."
        return "\n".join(f"- {t['topic']} ({t['count']})" for t in top)

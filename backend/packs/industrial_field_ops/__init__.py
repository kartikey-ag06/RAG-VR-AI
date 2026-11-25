"""Industrial Field Ops - the first sector pack (Phase 6). Uses ONLY core.sdk, through ctx.

  ProcOps      every question a worker asks is a bug report for the page it was answered from:
               Core event "question.asked" -> question log -> friction heatmap -> AI redline -> human approves
               -> the page becomes v1.1 (a page revision in the Core document store)
  Machine tags QR code / label text -> machine id -> that machine's documents
  Reports      inspection report, incident log, work order (templates for the Work sessions capability)

URLs: /api/v1/packs/industrial_field_ops/...  Old URLs keep working through route aliases:
/procops (dashboard), /api/procops/*, /api/machines*.
"""
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Body, Request
from fastapi.responses import FileResponse, JSONResponse

from core.sdk import PolicyDenied

from .friction import dedupe_key, entry_from_event, next_version, now_iso, parse_json, resolve_machine, summarize

APPROVE = "industrial_field_ops:approve"
LOG = "questions"                                  # journal stream: industrial_field_ops.questions
DASHBOARD = Path(__file__).parent / "static" / "dashboard.html"
NO_CACHE = {"Cache-Control": "no-store"}

DRAFT_SYSTEM = (
    "You are ProcOps, a technical writer who fixes procedures and controlled documents. "
    "Workers asked the questions below while using ONE page of a document. Each question is a bug report: "
    "the page was unclear, incomplete or wrong for them. Rewrite the page so those questions would not be needed. "
    "Rules: keep every fact, number, name, date and legal/safety meaning of the original; never invent facts - "
    "if the page cannot answer a question, add a clearly marked note 'NOTE: <what is missing>' instead; "
    "use short sentences and numbered steps where useful; plain text only (no markdown symbols like ** or #). "
    "Reply with JSON only, no other text, in this shape: "
    '{"issue": "one sentence: what was unclear", '
    '"revised_text": "the full rewritten page", '
    '"change_summary": ["short bullet", "..."], '
    '"checks": [{"check": "Facts and numbers kept", "pass": true, "note": "..."}, '
    '{"check": "No new facts invented", "pass": true, "note": "..."}, '
    '{"check": "Every worker question addressed", "pass": true, "note": "..."}]}'
)

REPORT_KINDS = [
    ("inspection", "Inspection Report", "Focus on the condition found, measurements and pass / fail per check."),
    ("incident", "Incident Log", "Focus on what happened, when, impact, immediate actions and root-cause leads."),
    ("workorder", "Work Order", "Focus on the work to be done, parts / tools needed, priority and who should act."),
]


def err(msg, status):
    return JSONResponse({"error": msg}, status)


def register(ctx):
    ctx.prompts.register("draft", DRAFT_SYSTEM, "ProcOps: rewrite one page from the worker questions about it")
    for kind, label, instructions in REPORT_KINDS:
        ctx.reports.register_kind(kind, label, instructions)

    # ------------------------------------------------------------ one question = one entry (server + old clients)
    recent, lock = {}, threading.Lock()

    def first_time(tenant, question):
        """The server event and an older glasses build may both report the same question: count it once."""
        window = int(ctx.config.get(tenant, "dedupe_seconds") or 0)
        key, now = (tenant, dedupe_key(question)), time.time()
        with lock:
            for k in [k for k, t in recent.items() if now - t > max(window, 1)]:
                recent.pop(k, None)
            if window and key in recent:
                return False
            recent[key] = now
            return True

    @ctx.events.subscribe("question.asked")
    def on_question(event):
        """The Core answered a question (glasses, MCP, scan, another pack): log it against the page it used."""
        ev = entry_from_event(event.data)
        if ev["q"] and first_time(event.tenant_id, ev["q"]):
            ctx.journal.append(event.tenant_id, LOG, ev)

    @ctx.router.post("/event")
    def log_event(request: Request, b: Any = Body(None)):
        """Kept for glasses builds from before Phase 6, which reported each question themselves."""
        p = ctx.require(request, "knowledge:read")
        if not isinstance(b, dict):
            return err("send a JSON object", 400)
        q = str(b.get("q", "")).strip()[:500]
        if not q:
            return err("empty question", 400)
        ev = {"ts": now_iso(), "q": q, "mode": str(b.get("mode", ""))[:10], "ms": int(b.get("ms") or 0),
              "source": "client"}
        try:
            if b.get("conf") is not None:
                ev["conf"] = round(float(b["conf"]), 3)
        except (TypeError, ValueError):
            pass
        if b.get("hits") is not None:
            ev["hits"] = int(b.get("hits") or 0)
        if b.get("machine"):
            ev["machine"] = str(b["machine"])[:80]
        uid = str(b.get("uid") or "")
        if ctx.documents.get(request, uid):
            try:
                page = int(b.get("page"))
            except (TypeError, ValueError):
                page = 0
            if page >= 1:
                ev.update(uid=uid, page=page)
        if not first_time(p.tenant_id, q):
            return {"ok": True, "duplicate": True}
        ctx.journal.append(request, LOG, ev)
        return {"ok": True}

    # ------------------------------------------------------------ detect: friction heatmap
    def summary(request):
        ctx.require(request, "knowledge:read")
        data = summarize(ctx.journal.list(request, LOG), ctx.documents.list(request),
                         lambda uid: ctx.documents.revisions(request, uid),
                         float(ctx.config.get(request, "low_confidence")))
        data["indexVersion"] = ctx.documents.index_version()
        return data

    @ctx.router.get("/summary")
    def get_summary(request: Request):
        return JSONResponse(summary(request), headers=NO_CACHE)

    @ctx.router.get("/page/{uid}/{page}")
    def page_image(uid: str, page: int, request: Request, original: int = 0):
        p = ctx.documents.page_image(request, uid, page, original=bool(original))
        if not p:
            return err("page not found", 404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    @ctx.router.get("/dashboard")
    def dashboard():
        return FileResponse(DASHBOARD, headers=NO_CACHE)

    # ------------------------------------------------------------ fix: AI drafts a redline, a human approves it
    @ctx.router.post("/draft")
    def draft(request: Request, b: Any = Body(None)):
        ctx.require(request, APPROVE)
        b = b if isinstance(b, dict) else {}
        uid, page = str(b.get("uid", "")), int(b.get("page") or 0)
        doc = next((d for d in summary(request)["docs"] if d["uid"] == uid), None)
        text = ctx.documents.page_text(request, uid, page)
        if doc is None:
            return err("document not found", 404)
        if text is None:
            return err("page not found", 404)
        hot = next((p for p in doc["hot"] if p["page"] == page), None)
        questions = [q["q"] + (f"  (asked {q['n']}x)" if q["n"] > 1 else "") for q in (hot or {}).get("questions", [])]
        if not questions:
            return err("no open worker questions on this page yet", 400)
        user = (f"Document: {doc['title']} (current version v{doc['version']}), page {page}\n\n"
                "WORKER QUESTIONS ON THIS PAGE:\n- " + "\n- ".join(questions) + f"\n\nCURRENT PAGE TEXT:\n{text[:7000]}")
        try:
            d = parse_json(ctx.llm.complete(request, prompt="draft", variables={"input": user}, max_tokens=3000))
        except PolicyDenied:
            raise
        except Exception as ex:
            return err(f"AI draft failed: {ex}", 502)
        rt = str(d.get("revised_text", "")).strip()
        if len(rt) < 20:
            return err("AI returned an empty draft, try again", 502)
        return {"uid": uid, "page": page, "issue": str(d.get("issue", "")), "revised_text": rt,
                "change_summary": [str(x) for x in (d.get("change_summary") or [])][:10],
                "checks": [{"check": str(c.get("check", "")), "pass": bool(c.get("pass")), "note": str(c.get("note", ""))}
                           for c in (d.get("checks") or []) if isinstance(c, dict)][:6],
                "evidence": questions, "original_text": text, "next_version": next_version(doc["version"])}

    @ctx.router.post("/approve")
    def approve(request: Request, b: Any = Body(None)):
        ctx.require(request, APPROVE)
        b = b if isinstance(b, dict) else {}
        uid, page = str(b.get("uid", "")), int(b.get("page") or 0)
        text = str(b.get("text", "")).strip()
        if not (20 <= len(text) <= 12000):
            return err("revised text must be 20 - 12000 characters", 400)
        try:
            rec = ctx.documents.add_revision(request, uid, page, text, summary=str(b.get("summary", "")),
                                             approver=str(b.get("approver", "")).strip(),
                                             evidence=int(b.get("evidence") or 0))
        except ValueError as ex:
            return err(str(ex), 400)
        return {"approved": rec}

    @ctx.router.post("/reset")
    def reset(request: Request, b: Any = Body(None)):
        """Demo reset: pages back to the originals (this tenant only); optionally archive the question log."""
        ctx.require(request, APPROVE)
        b = b if isinstance(b, dict) else {}
        uid = b.get("uid") or None
        if uid and not ctx.documents.get(request, str(uid)):
            return err("document not found", 404)
        restored = ctx.documents.clear_revisions(request, str(uid) if uid else None)
        if b.get("events"):
            ctx.journal.archive(request, LOG)
        return {"restored": restored, "eventsArchived": bool(b.get("events"))}

    # ------------------------------------------------------------ machine tags (QR code / label -> machine id)
    @ctx.router.get("/machines")
    def machines(request: Request):
        ctx.require(request, "knowledge:read")
        out = {}
        for d in ctx.documents.list(request):
            for t in d.get("tags") or []:
                out.setdefault(t, []).append({"uid": d["uid"], "title": d["title"], "id": d["id"]})
        return {"machines": [{"machine_id": k, "docs": v} for k, v in sorted(out.items())]}

    @ctx.router.post("/machines/resolve")
    def machines_resolve(request: Request, b: Any = Body(None)):
        """Text from a QR code / barcode / label photo -> the machine id it belongs to (or none)."""
        ctx.require(request, "knowledge:read")
        if not isinstance(b, dict):
            return err("send a JSON object", 400)
        text = str(b.get("text", ""))[:2000]
        best, docs = resolve_machine(text, ctx.documents.list(request))
        return {"machine_id": best, "docs": docs, "text": text[:200]}

    # ------------------------------------------------------------ tools for other AI assistants (MCP)
    @ctx.mcp_tool("industrial_field_ops_friction")
    def tool_friction(request, args):
        limit = max(1, min(20, int(args.get("limit") or 10)))
        rows = [(p["open"], d["title"], p["page"], [q["q"] for q in p["questions"][:3]])
                for d in summary(request)["docs"] for p in d["hot"] if p["open"]]
        rows.sort(key=lambda r: -r[0])
        if not rows:
            return "No open worker questions yet."
        return "\n".join(f"{n} open question(s) - {t}, page {pg}: " + " | ".join(qs) for n, t, pg, qs in rows[:limit])

    @ctx.mcp_tool("industrial_field_ops_resolve_machine")
    def tool_resolve(request, args):
        ctx.require(request, "knowledge:read")
        best, docs = resolve_machine(str(args.get("text", ""))[:2000], ctx.documents.list(request))
        if not best:
            return "No known machine id in that text."
        return f"Machine {best}: " + "; ".join(f"{d['title']} (doc_uid {d['uid']})" for d in docs)

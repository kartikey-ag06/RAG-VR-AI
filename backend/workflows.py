"""
workflows.py - guided step-by-step procedures, work sessions, 3D pins and report exports.

  /api/workflows/generate   topic (+ machine id) -> numbered steps, each citing the document page it comes from
  /api/sessions             a work session = everything asked, scanned and done (the audit trail)
  /api/sessions/kinds       which report kinds exist (a generic "summary" + kinds added by switched-on packs)
  /api/sessions/{id}/export a report of one kind as PDF, DOCX or JSON (Industrial Field Ops adds
                            inspection report / incident log / work order)
  /api/sessions/{id}/send   ...or straight to a connector (webhook, email, Jira, ServiceNow)
  /api/pins                 documents / answers / notes pinned in 3D space (shared with the /workspace dashboard)
  /workspace                desktop dashboard: pins, sessions, reports, reminders and notes
"""
from net import HTTP, JSONB
import io
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response

# Report kinds are templates: the generic one is always there; packs add their own (ctx.reports.register_kind).
GENERIC_KINDS = {"summary": {"label": "Session Report", "instructions": ""}}

WF_PROMPT = (
    "You write safe, practical step-by-step procedures for field technicians wearing smart glasses. "
    "Use ONLY the numbered excerpts. Reply with JSON only: "
    '{"title": "short title", "summary": "one sentence", "tools": ["..."], '
    '"steps": [{"title": "max 8 words", "detail": "1-2 short sentences", "warning": "safety note or empty", "cite": [n]}]} '
    "with 3 to 10 steps in the right order. Every step must cite the excerpt number(s) it comes from. "
    "If the excerpts do not describe a procedure for this topic, return steps that guide the user through reading the "
    "relevant sections, still citing them. Never invent part numbers, values or safety limits."
)
REPORT_PROMPT = (
    "You turn a field work session log into a {kind}. Use only facts in the log. Reply with JSON only: "
    '{{"title": "...", "summary": "2-3 sentences", "asset": "machine / equipment id or empty", '
    '"findings": ["..."], "actions_taken": ["..."], "recommended_actions": ["..."], '
    '"parts_or_tools": ["..."], "priority": "low | medium | high", "status": "open | in progress | closed", '
    '"sources": ["document - page"]}}'
)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else {}


def register(app, kv, llm, search, store, connectors, root, auth, kinds=None, emit=None):
    """kinds() -> {kind: {"label", "instructions"}} for the current tenant (packs that are ON); emit(name, data)."""
    page_html = root / "backend" / "static" / "workspace.html"

    def all_kinds():
        out = dict((kinds or (lambda: {}))())
        for k, v in GENERIC_KINDS.items():
            out.setdefault(k, v)
        return out

    def pick(kind, prefer):
        """The asked kind if it exists, else the preferred default, else the first one (packs may be off)."""
        ks = all_kinds()
        for k in (kind, prefer):
            if k in ks:
                return k, ks[k]
        k = next(iter(ks))
        return k, ks[k]

    @app.get("/api/sessions/kinds")
    def report_kinds():
        return {"kinds": [{"kind": k, "label": v["label"]} for k, v in all_kinds().items()]}

    # ---------- who may read a record: its creator (or admins); records made without a login are shared
    def owner_of(req):
        u = auth.user_of(req)
        return u["id"] if u else ""

    def can_read(rec, req):
        if not rec:
            return False
        if not auth.enabled or not rec.get("owner") or auth.is_admin(req):
            return True
        return rec.get("owner") == owner_of(req)

    def hits_for(topic, machine, user, k=8):
        res = search(topic, k, machine, user)
        hs = [{"uid": h["uid"], "title": h["title"], "page": h["page"], "doc": h["doc"], "text": h["text"]} for h in res["hits"]]
        return hs, res

    # ---------- guided workflows
    @app.post("/api/workflows/generate")
    def wf_generate(req: Request, _body=JSONB):
        if auth and not auth.require_user(req):
            return JSONResponse({"error": "sign in first: this uses the AI (paste your access token in Settings)"}, 401)
        b = (_body if _body is not None else {})
        topic = str(b.get("topic", "")).strip()[:300]
        machine = str(b.get("machine", "") or "")[:80]
        if not topic:
            return JSONResponse({"error": "say what the procedure is for"}, 400)
        hs, res = hits_for(topic, machine, auth.user_of(req))
        if not hs:
            return JSONResponse({"error": "nothing in your documents matches this topic"}, 404)
        ex = "".join(f"[{i + 1}] ({h['title']}, page {h['page']})\n{h['text']}\n\n" for i, h in enumerate(hs))
        try:
            d = parse_json(llm([{"role": "system", "content": WF_PROMPT},
                                {"role": "user", "content": f"Topic: {topic}\nMachine: {machine or 'not given'}\n\nExcerpts:\n{ex}"}], 2500))
        except Exception as e:
            return JSONResponse({"error": f"AI failed: {e}"}, 502)
        steps = []
        for i, st in enumerate(d.get("steps") or []):
            cites = [int(n) - 1 for n in (st.get("cite") or []) if str(n).isdigit() and 0 < int(n) <= len(hs)]
            src = hs[cites[0]] if cites else None
            steps.append({"n": i + 1, "title": str(st.get("title", ""))[:80], "detail": str(st.get("detail", ""))[:400],
                          "warning": str(st.get("warning", "") or "")[:200],
                          "cite": {"uid": src["uid"], "title": src["title"], "page": src["page"], "doc": src["doc"]} if src else None})
        if not steps:
            return JSONResponse({"error": "the AI did not return steps, try again"}, 502)
        wf = {"id": secrets.token_hex(5), "topic": topic, "machine": machine, "title": str(d.get("title") or topic)[:120],
              "summary": str(d.get("summary", ""))[:300], "tools": [str(t)[:60] for t in (d.get("tools") or [])][:12],
              "steps": steps, "created": now_iso(), "confidence": res.get("conf"), "owner": owner_of(req)}
        kv.kv_put("workflow", wf["id"], wf)
        return wf

    @app.get("/api/workflows")
    def wf_list(req: Request):
        return {"workflows": [{k: w[k] for k in ("id", "title", "topic", "machine", "created")} | {"steps": len(w["steps"])}
                              for w in kv.kv_list("workflow") if can_read(w, req)][:50]}

    @app.get("/api/workflows/{wid}")
    def wf_get(wid: str, req: Request):
        w = kv.kv_get("workflow", wid)
        return w if can_read(w, req) else JSONResponse({"error": "not found"}, 404)

    # ---------- sessions (audit trail)
    @app.post("/api/sessions")
    def session_new(req: Request, _body=JSONB):
        b = (_body if _body is not None else {})
        s = {"id": secrets.token_hex(6), "started": now_iso(), "updated": now_iso(), "device": str(b.get("device", ""))[:80],
             "user": str(b.get("user", ""))[:80], "machine": "", "log": [], "workflow": None, "owner": owner_of(req)}
        kv.kv_put("session", s["id"], s)
        return s

    _SESSION_LOCKS = {}
    _SLOCK_MUTEX = threading.Lock()

    def _get_session_lock(sid: str):
        with _SLOCK_MUTEX:
            if sid not in _SESSION_LOCKS:
                _SESSION_LOCKS[sid] = threading.Lock()
            return _SESSION_LOCKS[sid]

    @app.post("/api/sessions/{sid}/log")
    def session_log(sid: str, req: Request, _body=JSONB):
        with _get_session_lock(sid):
            s = kv.kv_get("session", sid)
            if not can_read(s, req):
                return JSONResponse({"error": "session not found"}, 404)
            b = (_body if _body is not None else {})
            typ = str(b.get("type", "note"))[:20]
            data = b.get("data") if isinstance(b.get("data"), dict) else {"text": str(b.get("data", ""))}
            data = json.loads(json.dumps(data)[:4000]) if len(json.dumps(data)) <= 4000 else {"text": json.dumps(data)[:3900]}
            s["log"].append({"t": now_iso(), "type": typ, "data": data})
            s["log"] = s["log"][-300:]
            if typ == "machine" and data.get("machine_id"):
                s["machine"] = str(data["machine_id"])[:80]
            if typ == "workflow":
                s["workflow"] = data
            s["updated"] = now_iso()
            kv.kv_put("session", sid, s)
            return {"ok": True, "entries": len(s["log"])}

    @app.get("/api/sessions")
    def sessions(req: Request):
        return {"sessions": [{k: s.get(k) for k in ("id", "started", "updated", "machine", "device")} | {"entries": len(s["log"])}
                             for s in kv.kv_list("session") if can_read(s, req)][:100]}

    @app.get("/api/sessions/{sid}")
    def session_get(sid: str, req: Request):
        s = kv.kv_get("session", sid)
        return s if can_read(s, req) else JSONResponse({"error": "not found"}, 404)

    # ---------- reports
    def build_report(s, kind, spec):
        label = spec["label"]
        cached = (s.get("reports") or {}).get(kind)
        if cached and cached.get("updated") == s["updated"]:
            return cached["report"]
        lines = []
        for e in s["log"][-120:]:
            d = e["data"]
            txt = d.get("text") or d.get("question") or d.get("answer") or json.dumps(d)[:300]
            if e["type"] == "answer" and d.get("sources"):
                txt += f"  (sources: {', '.join(d['sources'][:3])})"
            lines.append(f"{e['t'][11:16]} {e['type']}: {txt[:500]}")
        log = "\n".join(lines) or "(empty session)"
        try:
            system = REPORT_PROMPT.format(kind=label) + (" " + spec["instructions"] if spec.get("instructions") else "")
            d = parse_json(llm([{"role": "system", "content": system},
                                {"role": "user", "content": f"Machine: {s.get('machine') or 'unknown'}\nSession log:\n{log}"}], 2000))
        except Exception as e:
            d = {"title": f"{label} (AI summary unavailable)", "summary": str(e)[:200]}
        rep = {"kind": kind, "kind_label": label, "session": s["id"], "created": now_iso(),
               "started": s["started"], "machine": s.get("machine") or d.get("asset", ""),
               "title": str(d.get("title") or label)[:160], "summary": str(d.get("summary", ""))[:1500],
               "priority": str(d.get("priority", "") or "")[:20], "status": str(d.get("status", "") or "")[:20]}
        for f in ("findings", "actions_taken", "recommended_actions", "parts_or_tools", "sources"):
            rep[f] = [str(x)[:300] for x in (d.get(f) or [])][:20]
        rep["log"] = lines[-60:]
        rep["text"] = report_text(rep)
        s.setdefault("reports", {})[kind] = {"updated": s["updated"], "report": rep}
        kv.kv_put("session", s["id"], s)
        if emit:
            emit("report.created", {"kind": kind, "session": s["id"], "machine": rep["machine"]})
        return rep

    def report_text(r):
        out = [f"{r['kind_label']}: {r['title']}", f"Machine / asset: {r['machine'] or '-'}",
               f"Priority: {r['priority'] or '-'}   Status: {r['status'] or '-'}", "", r["summary"], ""]
        for f, label in (("findings", "Findings"), ("actions_taken", "Actions taken"),
                         ("recommended_actions", "Recommended actions"), ("parts_or_tools", "Parts / tools"), ("sources", "Sources")):
            if r[f]:
                out += [label + ":"] + [f"  - {x}" for x in r[f]] + [""]
        return "\n".join(out)

    def to_pdf(r):
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import ListFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from reportlab.lib import colors
        from xml.sax.saxutils import escape
        buf = io.BytesIO()
        st = getSampleStyleSheet()
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                                title=r["title"])
        el = [Paragraph(escape(r["kind_label"]), st["Heading3"]), Paragraph(escape(r["title"]), st["Title"])]
        meta = [["Machine / asset", r["machine"] or "-"], ["Priority", r["priority"] or "-"], ["Status", r["status"] or "-"],
                ["Session", f"{r['session']} (started {r['started'][:16].replace('T', ' ')} UTC)"], ["Created", r["created"][:16].replace("T", " ") + " UTC"]]
        t = Table(meta, colWidths=[40 * mm, 130 * mm])
        t.setStyle(TableStyle([("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9), ("FONT", (1, 0), (1, -1), "Helvetica", 9),
                               ("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke)]))
        el += [t, Spacer(1, 6 * mm), Paragraph(escape(r["summary"]), st["BodyText"]), Spacer(1, 4 * mm)]
        for f, label in (("findings", "Findings"), ("actions_taken", "Actions taken"), ("recommended_actions", "Recommended actions"),
                         ("parts_or_tools", "Parts / tools"), ("sources", "Sources")):
            if r[f]:
                el += [Paragraph(label, st["Heading4"]),
                       ListFlowable([Paragraph(escape(x), st["BodyText"]) for x in r[f]], bulletType="bullet", leftIndent=10)]
        el += [Paragraph("Session log", st["Heading4"])] + [Paragraph(escape(x), st["Code"]) for x in r["log"][-40:]]
        doc.build(el)
        return buf.getvalue()

    def to_docx(r):
        import docx
        d = docx.Document()
        d.add_paragraph(r["kind_label"]).runs[0].bold = True
        d.add_heading(r["title"], 0)
        tbl = d.add_table(rows=0, cols=2)
        tbl.style = "Table Grid"
        for k, v in (("Machine / asset", r["machine"] or "-"), ("Priority", r["priority"] or "-"), ("Status", r["status"] or "-"),
                     ("Session", r["session"]), ("Created", r["created"])):
            row = tbl.add_row().cells
            row[0].text, row[1].text = k, v
        d.add_paragraph(r["summary"])
        for f, label in (("findings", "Findings"), ("actions_taken", "Actions taken"), ("recommended_actions", "Recommended actions"),
                         ("parts_or_tools", "Parts / tools"), ("sources", "Sources")):
            if r[f]:
                d.add_heading(label, 2)
                for x in r[f]:
                    d.add_paragraph(x, style="List Bullet")
        d.add_heading("Session log", 2)
        for x in r["log"][-40:]:
            d.add_paragraph(x)
        buf = io.BytesIO()
        d.save(buf)
        return buf.getvalue()

    @app.get("/api/sessions/{sid}/export")
    def session_export(sid: str, req: Request, format: str = "pdf", kind: str = ""):
        s = kv.kv_get("session", sid)
        if not can_read(s, req):
            return JSONResponse({"error": "session not found"}, 404)
        kind, spec = pick(kind, "inspection")
        r = build_report(s, kind, spec)
        name = f"{kind}_{sid}_{datetime.now().strftime('%Y%m%d_%H%M')}"
        if format == "json":
            return Response(json.dumps(r, indent=2, ensure_ascii=False), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{name}.json"'})
        if format == "docx":
            return Response(to_docx(r), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            headers={"Content-Disposition": f'attachment; filename="{name}.docx"'})
        return Response(to_pdf(r), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})

    @app.post("/api/sessions/{sid}/send")
    def session_send(sid: str, req: Request, _body=JSONB):
        if auth.admin_required(req) and not (auth.is_admin(req) or auth.user_of(req)):
            return JSONResponse({"error": "sign in (access token) to send reports"}, 403)
        b = (_body if _body is not None else {})
        s = kv.kv_get("session", sid)
        c = connectors["get"](str(b.get("connector", "")))
        if not can_read(s, req) or not c:
            return JSONResponse({"error": "session or connector not found"}, 404)
        kind, spec = pick(b.get("kind"), "workorder")
        r = build_report(s, kind, spec)
        try:
            res = connectors["send"](c, r, pdf_bytes=to_pdf(r) if c["type"] == "email" else None)
        except Exception as e:
            return JSONResponse({"error": f"{c['type']}: {e}"[:400]}, 502)
        s["log"].append({"t": now_iso(), "type": "export", "data": {"text": f"{spec['label']} sent: {res}"}})
        kv.kv_put("session", sid, s)
        return {"sent": res}

    # ---------- pins (3D workspace, shared with the desktop dashboard)
    @app.get("/api/pins")
    def pins(req: Request):
        return {"pins": [p for p in kv.kv_list("pin") if can_read(p, req)]}

    @app.post("/api/pins")
    def pin_add(req: Request, _body=JSONB):
        if auth and not auth.require_user(req):
            return JSONResponse({"error": "sign in first (paste your access token in Settings)"}, 401)
        b = (_body if _body is not None else {})
        kind = str(b.get("kind", "page"))
        if kind not in ("page", "answer", "figure", "note", "step"):
            return JSONResponse({"error": "bad pin kind"}, 400)
        pose = b.get("pose") or {}
        try:
            pos = [float(x) for x in pose.get("pos", [0, 1.5, -1])][:3]
            yaw = float(pose.get("yaw", 0))
        except (TypeError, ValueError):
            return JSONResponse({"error": "bad pose"}, 400)
        p = {"id": secrets.token_hex(5), "kind": kind, "title": str(b.get("title", ""))[:120], "text": str(b.get("text", ""))[:3000],
             "uid": str(b.get("uid", ""))[:12], "page": int(b.get("page") or 0), "file": str(b.get("file", ""))[:40],
             "pose": {"pos": pos, "yaw": yaw}, "created": now_iso(), "owner": owner_of(req)}
        kv.kv_put("pin", p["id"], p)
        return p

    @app.delete("/api/pins/{pid}")
    def pin_delete(pid: str, req: Request):
        if not can_read(kv.kv_get("pin", pid), req):
            return JSONResponse({"error": "not found"}, 404)
        kv.kv_delete("pin", pid)
        return {"deleted": pid}

    @app.get("/api/pins/page/{uid}/{page}")
    def pin_page_image(uid: str, page: int, req: Request):
        e = store.doc_entry(uid) if re.fullmatch(r"[0-9a-f]{12}", uid) else None
        ok = e and auth.can_see_doc(e, auth.user_of(req))
        p = store.page_image_uid(uid, page) if ok else None
        return FileResponse(p, media_type="image/jpeg") if p else JSONResponse({"error": "not found"}, 404)

    @app.get("/workspace")
    def workspace():
        return FileResponse(page_html, headers={"Cache-Control": "no-store"})

    return {"build_report": build_report}

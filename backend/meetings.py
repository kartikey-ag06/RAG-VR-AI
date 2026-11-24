"""
meetings.py - meeting notes and teleprompter scripts for the glasses.

Meetings (like Even G2 "Conversate"):
  POST /api/meetings                       {title?, lang?}            -> start a meeting
  POST /api/meetings/{id}/segment          {text, t?}                 -> add a transcribed piece (from Whisper)
  POST /api/meetings/{id}/stop             {}                          -> AI summary, decisions, action items
  GET  /api/meetings | /api/meetings/{id}
  GET  /api/meetings/{id}/export?format=md|docx|pdf
  POST /api/meetings/{id}/actions-to-reminders                         -> each action item becomes a reminder
  DELETE /api/meetings/{id}
Teleprompter:
  POST /api/teleprompter/script            {topic, minutes?, style?}  -> a speech written from YOUR documents
  GET/POST/DELETE /api/teleprompter/scripts                            -> saved scripts
"""
import io
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from net import JSONB

SUMMARY_SYSTEM = """You write meeting notes from a raw speech-to-text transcript (it can have mistakes and no speaker
names). Reply with JSON only:
{"title": "short meeting title", "summary": "3-5 sentences", "decisions": ["..."],
 "action_items": [{"task": "...", "owner": "name or null", "due": "YYYY-MM-DD or null"}],
 "topics": ["..."], "open_questions": ["..."]}
Use only what was said. Fix obvious speech-to-text errors. Empty lists are fine."""

SCRIPT_SYSTEM = """You write a script to be read aloud from a teleprompter in smart glasses.
Use ONLY the facts in the excerpts; if they do not cover the topic, say so in one line and write a general script.
Short sentences (max 16 words), one idea per line, plain words, no markdown, no bullet symbols, no citations.
Start with one line saying what the talk is about. End with a one-line close."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_from(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON in the model reply")
    return json.loads(m.group(0))


def to_markdown(m):
    s = m.get("summary") or {}
    out = [f"# {s.get('title') or m.get('title') or 'Meeting'}", "",
           f"Started {m['started']} · {round(m.get('seconds', 0) / 60)} min · {len(m['segments'])} transcript pieces", ""]
    if s:
        out += ["## Summary", "", s.get("summary", ""), ""]
        for key, head in (("decisions", "Decisions"), ("topics", "Topics"), ("open_questions", "Open questions")):
            if s.get(key):
                out += [f"## {head}", ""] + [f"- {x}" for x in s[key]] + [""]
        if s.get("action_items"):
            out += ["## Action items", ""] + [
                f"- [ ] {a.get('task')}" + (f" — {a['owner']}" if a.get("owner") else "") + (f" (due {a['due']})" if a.get("due") else "")
                for a in s["action_items"]] + [""]
    out += ["## Transcript", ""] + [f"[{x.get('clock', '')}] {x['text']}" for x in m["segments"]]
    return "\n".join(out)


def register(app, kv, llm, search, auth):
    def owner(req):
        u = auth.user_of(req)
        return u["id"] if u else ""

    def mine(m, req):
        return bool(m) and (not auth.enabled or not m.get("owner") or auth.is_admin(req) or m["owner"] == owner(req))

    # ------------------------------------------------------------ meetings
    @app.post("/api/meetings")
    def start(req: Request, _body=JSONB):
        if auth and not auth.require_user(req):
            return JSONResponse({"error": "sign in first (paste your access token in Settings)"}, 401)
        b = _body or {}
        m = {"id": secrets.token_hex(5), "title": str(b.get("title") or "Meeting")[:120], "lang": str(b.get("lang") or "")[:10],
             "started": _now(), "t0": time.time(), "segments": [], "status": "recording", "summary": None,
             "owner": owner(req), "seconds": 0}
        kv.kv_put("meeting", m["id"], m)
        return m

    _MEETING_LOCKS = {}
    _MLOCK_MUTEX = threading.Lock()

    def _get_meeting_lock(mid: str):
        with _MLOCK_MUTEX:
            if mid not in _MEETING_LOCKS:
                _MEETING_LOCKS[mid] = threading.Lock()
            return _MEETING_LOCKS[mid]

    @app.post("/api/meetings/{mid}/segment")
    def segment(mid: str, req: Request, _body=JSONB):
        with _get_meeting_lock(mid):
            m = kv.kv_get("meeting", mid)
            if not mine(m, req):
                return JSONResponse({"error": "meeting not found"}, 404)
            text = re.sub(r"\s+", " ", str((_body or {}).get("text", ""))).strip()[:4000]
            if text and len(m["segments"]) < 3000:
                t = float((_body or {}).get("t") or (time.time() - m["t0"]))
                m["segments"].append({"t": round(t, 1), "clock": f"{int(t // 60):02d}:{int(t % 60):02d}", "text": text})
                m["seconds"] = max(m.get("seconds", 0), t)
                kv.kv_put("meeting", mid, m)
            return {"ok": True, "segments": len(m["segments"])}

    @app.post("/api/meetings/{mid}/stop")
    def stop(mid: str, req: Request):
        m = kv.kv_get("meeting", mid)
        if not mine(m, req):
            return JSONResponse({"error": "meeting not found"}, 404)
        m["status"] = "done"
        m["seconds"] = max(m.get("seconds", 0), time.time() - m["t0"])
        transcript = "\n".join(f"[{x['clock']}] {x['text']}" for x in m["segments"])
        if not transcript.strip():
            m["summary"] = {"title": m["title"], "summary": "Nothing was said.", "decisions": [], "action_items": [],
                            "topics": [], "open_questions": []}
        else:
            try:
                raw = llm([{"role": "system", "content": SUMMARY_SYSTEM},
                           {"role": "user", "content": f"Today is {datetime.now().date()}.\nTranscript:\n{transcript[-60000:]}"}], 1400)
                s = _json_from(raw)
                s["action_items"] = [a if isinstance(a, dict) else {"task": str(a)} for a in s.get("action_items", [])][:30]
                m["summary"] = s
            except Exception as e:
                m["summary"] = {"title": m["title"], "summary": f"(AI summary failed: {str(e)[:120]})", "decisions": [],
                                "action_items": [], "topics": [], "open_questions": []}
        if m["summary"].get("title"):
            m["title"] = str(m["summary"]["title"])[:120]
        kv.kv_put("meeting", mid, m)
        return m

    @app.get("/api/meetings")
    def list_meetings(req: Request):
        ms = sorted((x for x in kv.kv_list("meeting") if mine(x, req)), key=lambda x: x.get("t0", 0), reverse=True)
        return {"meetings": [{k: x.get(k) for k in ("id", "title", "started", "status", "seconds")} |
                             {"segments": len(x["segments"]), "actions": len((x.get("summary") or {}).get("action_items", []))}
                             for x in ms][:100]}

    @app.get("/api/meetings/{mid}")
    def get_meeting(mid: str, req: Request):
        m = kv.kv_get("meeting", mid)
        return m if mine(m, req) else JSONResponse({"error": "not found"}, 404)

    @app.delete("/api/meetings/{mid}")
    def del_meeting(mid: str, req: Request):
        if not mine(kv.kv_get("meeting", mid), req):
            return JSONResponse({"error": "not found"}, 404)
        kv.kv_delete("meeting", mid)
        return {"deleted": mid}

    @app.post("/api/meetings/{mid}/actions-to-reminders")
    def actions_to_reminders(mid: str, req: Request, tz: int = 330):
        m = kv.kv_get("meeting", mid)
        if not mine(m, req):
            return JSONResponse({"error": "not found"}, 404)
        made = 0
        for a in (m.get("summary") or {}).get("action_items", []):
            due = None
            if a.get("due") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(a["due"])):
                due = f"{a['due']}T09:00{'+' if tz >= 0 else '-'}{abs(tz) // 60:02d}:{abs(tz) % 60:02d}"   # 9 am, user's zone
            rid = secrets.token_hex(5)
            kv.kv_put("reminder", rid, {"id": rid, "text": str(a.get("task", "Action item"))[:300], "due": due, "done": False,
                                        "created": time.time(), "notified": False, "from_meeting": mid})
            made += 1
        return {"reminders": made}

    @app.get("/api/meetings/{mid}/export")
    def export(mid: str, req: Request, format: str = "md"):
        m = kv.kv_get("meeting", mid)
        if not mine(m, req):
            return JSONResponse({"error": "not found"}, 404)
        md = to_markdown(m)
        name = re.sub(r"[^\w-]+", "_", m["title"])[:60] or "meeting"
        if format == "docx":
            from docx import Document
            d = Document()
            for line in md.split("\n"):
                if line.startswith("# "):
                    d.add_heading(line[2:], 0)
                elif line.startswith("## "):
                    d.add_heading(line[3:], 1)
                elif line.startswith("- "):
                    d.add_paragraph(line[2:].replace("[ ] ", "☐ "), style="List Bullet")
                elif line.strip():
                    d.add_paragraph(line)
            buf = io.BytesIO(); d.save(buf)
            return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            headers={"Content-Disposition": f'attachment; filename="{name}.docx"'})
        if format == "pdf":
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
            from xml.sax.saxutils import escape
            st = getSampleStyleSheet(); flow = []
            for line in md.split("\n"):
                if line.startswith("# "):
                    flow.append(Paragraph(escape(line[2:]), st["Title"]))
                elif line.startswith("## "):
                    flow.append(Paragraph(escape(line[3:]), st["Heading2"]))
                elif line.strip():
                    flow.append(Paragraph(escape(line.replace("- [ ] ", "☐ ").replace("- ", "• ")), st["BodyText"]))
                else:
                    flow.append(Spacer(1, 6))
            buf = io.BytesIO(); SimpleDocTemplate(buf, pagesize=A4, title=m["title"]).build(flow)
            return Response(buf.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})
        return Response(md, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}.md"'})

    # ------------------------------------------------------------ teleprompter
    @app.post("/api/teleprompter/script")
    def make_script(req: Request, _body=JSONB):
        if auth and not auth.require_user(req):
            return JSONResponse({"error": "sign in first: this uses the AI (paste your access token in Settings)"}, 401)
        b = _body or {}
        topic = str(b.get("topic", "")).strip()[:300]
        if not topic:
            return JSONResponse({"error": "say what the talk is about"}, 400)
        minutes = max(0.5, min(10.0, float(b.get("minutes") or 1.5)))
        res = search(topic, 6, "", auth.user_of(req))
        ex = "\n\n".join(f"[{i + 1}] {h['title']} p.{h['page']}: {h['text'][:900]}" for i, h in enumerate(res["hits"]))
        words = int(minutes * 140)                       # 140 spoken words a minute
        try:
            text = llm([{"role": "system", "content": SCRIPT_SYSTEM},
                        {"role": "user", "content": f"Topic: {topic}\nLength: about {words} words.\nStyle: {b.get('style') or 'clear briefing'}\n\nExcerpts:\n{ex or '(none)'}"}],
                       min(2000, words * 3))
        except Exception as e:
            return JSONResponse({"error": f"AI error: {str(e)[:200]}"}, 502)
        text = re.sub(r"[*#`>]+", "", text).strip()
        sc = {"id": secrets.token_hex(4), "title": topic[:80], "text": text, "created": _now(), "owner": owner(req),
              "sources": [{"title": h["title"], "page": h["page"]} for h in res["hits"][:4]]}
        kv.kv_put("script", sc["id"], sc)
        return sc

    @app.get("/api/teleprompter/scripts")
    def scripts(req: Request):
        return {"scripts": sorted((s for s in kv.kv_list("script") if mine(s, req)), key=lambda s: s["created"], reverse=True)[:50]}

    @app.post("/api/teleprompter/scripts")
    def save_script(req: Request, _body=JSONB):
        b = _body or {}
        text = str(b.get("text", "")).strip()[:20000]
        if not text:
            return JSONResponse({"error": "empty script"}, 400)
        sc = {"id": str(b.get("id") or secrets.token_hex(4))[:12], "title": str(b.get("title") or text.split("\n")[0])[:80],
              "text": text, "created": _now(), "owner": owner(req), "sources": []}
        kv.kv_put("script", sc["id"], sc)
        return sc

    @app.delete("/api/teleprompter/scripts/{sid}")
    def del_script(sid: str, req: Request):
        if not mine(kv.kv_get("script", sid), req):
            return JSONResponse({"error": "not found"}, 404)
        kv.kv_delete("script", sid)
        return {"deleted": sid}

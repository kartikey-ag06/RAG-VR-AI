"""
procops.py - ProcOps: "DevOps for physical work".  Every job done is a test run of the manual.

Loop
  1. Run    : the glasses app logs every question with the document + page (page = procedure step)
  2. Detect : /api/procops/summary turns the log into a friction heatmap (questions per page)
  3. Fix    : /api/procops/draft asks the AI for a redline of that page, with the questions as evidence;
              a human approves it (/api/procops/approve) -> the page becomes v1.1 in the store,
              the index is rebuilt, and the glasses show + answer from the fixed page.

Data (all in backend/data/procops/, git-ignored with the rest of backend/data):
  events.jsonl  one line per question asked in the glasses
Revisions themselves live in the document store (indexer.Store.add_revision).

This module only ADDS routes; nothing in the existing app depends on it.
"""
from net import HTTP, JSONB
import json
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request

from core.data.tenancy import DEFAULT_TENANT, current_tenant
from fastapi.responses import FileResponse, JSONResponse

UID = re.compile(r"[0-9a-f]{12}")
NO_CACHE = {"Cache-Control": "no-store"}


class EventLog:
    def __init__(self, path: Path, db=None):
        self._path = path
        self.db = db                       # Postgres when configured (survives server restarts)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    @property
    def path(self):
        """default tenant: the same file as before Phase 4; others: events.<tenant>.jsonl next to it."""
        t = current_tenant()
        return self._path if t == DEFAULT_TENANT else self._path.with_name(f"{self._path.stem}.{t}{self._path.suffix}")

    def add(self, ev):
        if self.db:
            return self.db.add_event(ev)
        with self.lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def all(self):
        if self.db:
            return self.db.events()
        if not self.path.exists():
            return []
        out = []
        with self.lock:
            for line in self.path.read_text("utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out

    def archive(self):
        """Demo reset: keeps the old log as events.<time>.bak instead of deleting it."""
        if self.db:
            return self.db.archive_events()
        with self.lock:
            if self.path.exists():
                self.path.replace(self.path.with_name(f"{self.path.stem}.{int(time.time())}.bak"))


def norm_q(q):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", q.lower())).strip()


def parse_json(text):
    """The model is asked for JSON only; this also survives ```json fences or a short preface."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


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


def register(app, store, llm, root: Path, auth=None):
    """Adds the ProcOps routes to the FastAPI app. llm(messages, max_tokens) -> str (raises on error)."""
    log = EventLog(store.root / "procops" / "events.jsonl", db=store.db)

    def locked(req):        # changing documents needs an admin once the server is protected
        return auth is not None and auth.admin_required(req) and not auth.is_admin(req)

    def visible(uid, req):
        e = store.doc_entry(uid)
        return bool(e) and (auth is None or auth.can_see_doc(e, auth.user_of(req)))
    page_html = root / "backend" / "static" / "procops.html"

    @app.get("/procops")
    def procops_page():
        return FileResponse(page_html, headers=NO_CACHE)

    # ---------- 1. Run: the glasses log each question (fire-and-forget from the app)
    @app.post("/api/procops/event")
    def procops_event(req: Request, _body=JSONB):
        if auth and not auth.require_user(req):
            return JSONResponse({"error": "sign in first (paste your access token in Settings)"}, 401)
        try:
            b = (_body if _body is not None else {})
        except ValueError:
            return JSONResponse({"error": "bad json"}, 400)
        q = str(b.get("q", "")).strip()[:500]
        if not q:
            return JSONResponse({"error": "empty question"}, 400)
        uid = str(b.get("uid") or "")
        ev = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "q": q,
              "mode": str(b.get("mode", ""))[:10], "ms": int(b.get("ms") or 0)}
        try:
            if b.get("conf") is not None:
                ev["conf"] = round(float(b["conf"]), 3)          # best semantic match of the search
        except (TypeError, ValueError):
            pass
        if b.get("hits") is not None:
            ev["hits"] = int(b.get("hits") or 0)
        if b.get("machine"):
            ev["machine"] = str(b["machine"])[:80]
        if UID.fullmatch(uid) and store.doc_entry(uid):
            try:
                page = int(b.get("page"))
            except (TypeError, ValueError):
                page = 0
            if page >= 1:
                ev.update(uid=uid, page=page, rev=store.revisions(uid)["version"])
        log.add(ev)
        return {"ok": True}

    # ---------- 2. Detect: friction heatmap
    @app.get("/api/procops/summary")
    def procops_summary(req: Request = None):
        events = log.all()
        by_page = defaultdict(list)
        gaps = defaultdict(int)
        for ev in events:
            if ev.get("uid"):
                by_page[(ev["uid"], ev["page"])].append(ev)
            else:
                gaps[ev["q"]] += 1
        docs = []
        user = auth.user_of(req) if (auth is not None and req is not None) else None
        for e in store.list():
            if e["status"] != "ready":
                continue
            if auth is not None and req is not None and not auth.can_see_doc(e, user):
                continue
            revs = store.revisions(e["uid"])
            pages, total_open = [], 0
            for p in range(1, e["pages"] + 1):
                evs = by_page.get((e["uid"], p), [])
                rv = revs["pages"].get(str(p))
                since = rv["approved"] if rv else ""
                open_evs = [x for x in evs if x["ts"] > since]
                before = len(evs) - len(open_evs)
                qs = defaultdict(lambda: {"n": 0, "q": "", "last": ""})
                for x in open_evs:
                    k = qs[norm_q(x["q"])]
                    k["n"] += 1; k["q"] = x["q"]; k["last"] = x["ts"]
                if evs or rv:
                    pages.append({"page": p, "open": len(open_evs), "before": before,
                                  "distinct": len(qs), "rev": rv["version"] if rv else None,
                                  "questions": sorted(qs.values(), key=lambda k: (-k["n"], k["last"]))[:25]})
                total_open += len(open_evs)
            docs.append({"uid": e["uid"], "title": e["title"], "type": e["type"], "pages": e["pages"],
                         "version": revs["version"], "open": total_open, "hot": pages,
                         "history": revs["history"][::-1]})
        docs.sort(key=lambda d: (-d["open"], -len(d["history"]), d["title"]))
        # knowledge-gap analytics: most asked, low-confidence answers, zero-hit questions
        most = defaultdict(lambda: {"n": 0, "q": ""})
        low, zero = [], defaultdict(int)
        for ev in events:
            k = most[norm_q(ev["q"])]; k["n"] += 1; k["q"] = ev["q"]
            if ev.get("hits") == 0 or (ev.get("mode") in ("general", "offline") and not ev.get("uid")):
                zero[ev["q"]] += 1
            elif ev.get("conf") is not None and ev["conf"] < 0.62:
                low.append({"q": ev["q"], "conf": ev["conf"], "ts": ev["ts"], "page": ev.get("page"), "uid": ev.get("uid")})
        titles = {d["uid"]: d["title"] for d in docs}
        for x in low:
            x["title"] = titles.get(x.get("uid"), "")
        analytics = {"mostAsked": sorted(most.values(), key=lambda k: -k["n"])[:15],
                     "lowConfidence": sorted(low, key=lambda x: x["conf"])[:20],
                     "zeroHit": sorted(({"q": q, "n": n} for q, n in zero.items()), key=lambda g: -g["n"])[:20]}
        return JSONResponse({"events": len(events), "docs": docs, "analytics": analytics,
                             "gaps": sorted(({"q": q, "n": n} for q, n in gaps.items()), key=lambda g: -g["n"])[:30],
                             "indexVersion": store.snapshot["version"]}, headers=NO_CACHE)

    @app.get("/api/procops/page/{uid}/{page}")
    def procops_page_image(uid: str, page: int, req: Request, original: int = 0):
        p = store.page_image_uid(uid, page, original=bool(original)) if UID.fullmatch(uid) and visible(uid, req) else None
        if not p:
            return JSONResponse({"error": "page not found"}, 404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})  # URL carries ?t=<index version>

    # ---------- 3. Fix: AI drafts a redline, a human approves it
    @app.post("/api/procops/draft")
    def procops_draft(req: Request, _body=JSONB):
        if locked(req):
            return JSONResponse({"error": "admin token required"}, 403)
        b = (_body if _body is not None else {})
        uid, page = str(b.get("uid", "")), int(b.get("page") or 0)
        if not UID.fullmatch(uid) or not store.doc_entry(uid):
            return JSONResponse({"error": "document not found"}, 404)
        text = store.page_text(uid, page)
        if text is None:
            return JSONResponse({"error": "page not found"}, 404)
        summary = procops_summary(req)
        doc = next((d for d in json.loads(summary.body)["docs"] if d["uid"] == uid), None)
        hot = next((p for p in (doc or {}).get("hot", []) if p["page"] == page), None)
        questions = [q["q"] + (f"  (asked {q['n']}x)" if q["n"] > 1 else "") for q in (hot or {}).get("questions", [])]
        if not questions:
            return JSONResponse({"error": "no open worker questions on this page yet"}, 400)
        user = (f"Document: {doc['title']} (current version v{doc['version']}), page {page}\n\n"
                f"WORKER QUESTIONS ON THIS PAGE:\n- " + "\n- ".join(questions) +
                f"\n\nCURRENT PAGE TEXT:\n{text[:7000]}")
        try:
            out = llm([{"role": "system", "content": DRAFT_SYSTEM}, {"role": "user", "content": user}], 3000)
            d = parse_json(out)
        except Exception as ex:
            return JSONResponse({"error": f"AI draft failed: {ex}"}, 502)
        rt = str(d.get("revised_text", "")).strip()
        if len(rt) < 20:
            return JSONResponse({"error": "AI returned an empty draft, try again"}, 502)
        return {"uid": uid, "page": page, "issue": str(d.get("issue", "")), "revised_text": rt,
                "change_summary": [str(x) for x in (d.get("change_summary") or [])][:10],
                "checks": [{"check": str(c.get("check", "")), "pass": bool(c.get("pass")), "note": str(c.get("note", ""))}
                           for c in (d.get("checks") or []) if isinstance(c, dict)][:6],
                "evidence": questions, "original_text": text, "next_version": _next(doc["version"])}

    @app.post("/api/procops/approve")
    def procops_approve(req: Request, _body=JSONB):
        if locked(req):
            return JSONResponse({"error": "admin token required"}, 403)
        b = (_body if _body is not None else {})
        uid, page = str(b.get("uid", "")), int(b.get("page") or 0)
        text = str(b.get("text", "")).strip()
        if not UID.fullmatch(uid):
            return JSONResponse({"error": "document not found"}, 404)
        if not (20 <= len(text) <= 12000):
            return JSONResponse({"error": "revised text must be 20 - 12000 characters"}, 400)
        try:
            rec = store.add_revision(uid, page, text, summary=str(b.get("summary", "")),
                                     approver=str(b.get("approver", "")).strip(), evidence=int(b.get("evidence") or 0))
        except ValueError as ex:
            return JSONResponse({"error": str(ex)}, 400)
        return {"approved": rec}

    @app.post("/api/procops/reset")
    def procops_reset(req: Request, _body=JSONB):
        if locked(req):
            return JSONResponse({"error": "admin token required"}, 403)
        b = (_body if _body is not None else {})
        uid = b.get("uid") or None
        if uid and not UID.fullmatch(str(uid)):
            return JSONResponse({"error": "document not found"}, 404)
        cleared = store.clear_revisions(uid)
        if b.get("events"):
            log.archive()
        return {"restored": cleared, "eventsArchived": bool(b.get("events"))}


def _next(v):
    major, minor = (int(x) for x in v.split("."))
    return f"{major}.{minor + 1}"

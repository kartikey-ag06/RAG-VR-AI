"""
api_v1.py - one stable, versioned API that EVERY glasses client uses ("one brain, many doors").

Doors that call it:
  - the WebXR glasses app (VR headsets, Meta glasses emulator)   glasses_app/
  - the 600x600 HUD web app for Meta Ray-Ban Display (+ WebMCP)   /hud
  - the MCP server for Meta AI Connectors / Claude / ChatGPT      /mcp  (mcp_server.py uses CORE below)
  - later: native phone apps (Meta Device Access Toolkit, Android XR, Even Hub) - plain HTTPS + JSON

Endpoints (all JSON unless noted)
  GET  /api/v1/health                               what this server can do (AI, photo, search mode)
  GET  /api/v1/documents                            documents the caller may see
  POST /api/v1/search      {query, k?, machine_id?}  best passages, each with a page image URL
  POST /api/v1/ask         {question, machine_id?, style?, lang?}
                           -> {answer, mode: pdf|general|offline, citations[], confidence}
  POST /api/v1/ask/stream  same body; Server-Sent Events: meta -> delta... -> done   (first words in ~0.5 s)
  POST /api/v1/scan        {image: data URL, question?, style?, lang?}  photo + question -> answer
  GET  /api/v1/documents/{uid}/pages/{page}         page text + image URL
  GET  /api/v1/documents/{uid}/pages/{page}.jpg?w=600   page image, resized for small displays
  GET  /hud                                          600x600 web app for Meta Ray-Ban Display (WebMCP voice tools)

style: "full" (default, ~120 words) | "glance" (3 short points, for a lens display) | "spoken" (1-2 sentences, read aloud)

Auth (same rules as the rest of the app)
  - documents / search / pages: open; without a token only public documents (same as /api/search, /rag/pages)
  - ask / ask/stream / scan: on a protected server (Render, users, ADMIN_TOKEN) a token is needed (same as /api/grok)
  - X-VR-Token header or ?token=  -> that user (document groups are respected)
  - Authorization: Bearer <MCP_TOKEN>  -> service access (cloud assistants), public documents only
  - Authorization: Bearer vrk_...      -> an API key: its tenant and scopes (core/identity), public documents only
"""
import json
import os
import re
import secrets
import threading
from collections import OrderedDict
from io import BytesIO

from fastapi import Request
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from core.sdk.context import PolicyDenied

from net import JSONB

API = "/api/v1"
STYLES = ("full", "glance", "spoken")
MAX_Q = 1000
MAX_PASSAGES = 8

SMART_SYSTEM = (
    "You are the assistant inside smart glasses / a VR app. You receive numbered excerpts from the user's documents, "
    "plus a user question that may or may not be about them.\n"
    "Rules:\n"
    "1. If the excerpts contain the answer (fully or partly), answer from them and cite like [1], [2]. "
    "Your FIRST line must be exactly: SOURCE: PDF [n, m] (the excerpt numbers you used, most important first).\n"
    "2. If the excerpts do not contain the answer, answer from your general knowledge. Your FIRST line must be exactly: SOURCE: GENERAL\n"
    "3. Then a blank line, then the answer in plain text (see the length rule).\n"
    "4. Never invent citations, section numbers, case numbers or penalty amounts. If unsure, say so."
)
VISION_RULE = (
    "\n5. The user also took a PHOTO with their glasses. You get a description of it from a vision model "
    "(you do not see the photo yourself). Start with what the object is, then the key facts about it. "
    "Only use SOURCE: PDF when an excerpt really is about this object or question."
)
LENGTH = {
    "full": "max 120 words",
    "glance": "at most 3 short numbered points, each under 12 words (small lens display)",
    "spoken": "at most 25 words in 1-2 short sentences, no lists (it will be read aloud)",
}
_HEAD = re.compile(r"^\s*SOURCE:\s*(PDF|GENERAL)\s*(\[[^\]]*\])?\s*", re.I)


def style_rules(style, lang):
    s = f"\nLength: {LENGTH.get(style, LENGTH['full'])}."
    if lang and lang.lower() not in ("en", "english"):
        s += f"\nWrite the answer in {lang}. Keep the SOURCE line exactly in English as described."
    return s


def excerpts(hits):
    return "Excerpts:\n" + "".join(
        f"[{i + 1}] ({h['title']}, page {h['page']})\n{h['text']}\n\n" for i, h in enumerate(hits))


def clean_body(raw):
    """Answer text without the SOURCE line and without markdown (lenses and speech show plain text)."""
    raw = str(raw or "")
    m = _HEAD.match(raw)
    body = raw[m.end():] if m else raw
    body = re.sub(r"\*\*(.+?)\*\*", r"\1", body)
    body = re.sub(r"__(.+?)__", r"\1", body).replace("**", "")
    body = re.sub(r"^#+\s*", "", body, flags=re.M)
    body = re.sub(r"^\s*[-*]\s+", "• ", body, flags=re.M)
    return body.strip()


def parse_answer(raw, hits):
    """-> (answer text, mode 'pdf'|'general', cited hit indexes in order of importance)."""
    m = _HEAD.match(str(raw or ""))
    body = clean_body(raw)
    cited = []
    nums = ((m.group(2) or "") if m else "") + " " + body
    for x in re.finditer(r"\[(\d+(?:\s*,\s*\d+)*)\]", nums):
        for n in x.group(1).split(","):
            i = int(n) - 1
            if 0 <= i < len(hits) and i not in cited:
                cited.append(i)
    general = (m and m.group(1).upper() == "GENERAL") or not cited
    return body, ("general" if general else "pdf"), ([] if general else cited)


def extractive(hits, n=3):
    """No AI connected: the best matching sentences, so the glasses still show something useful."""
    out = []
    for i, h in enumerate(hits[:n]):
        s = re.split(r"(?<=[.;])\s+", h["text"].strip())[0][:240]
        out.append(f"[{i + 1}] {s}")
    return "\n".join(out)


def register(app, store, run_search, llm_complete, llm_stream, auth, provider, vision=None, identity=None):
    lock = threading.Lock()
    thumbs = OrderedDict()                       # resized page images, LRU (small displays ask for the same pages)

    # ------------------------------------------------------------ access
    def service_token(req):
        tok = os.environ.get("MCP_TOKEN", "")
        h = req.headers.get("authorization", "")
        return bool(tok) and h.startswith("Bearer ") and secrets.compare_digest(h[7:].strip(), tok)

    def caller(req, ai=False, scope=None):
        """-> (allowed, user or error). One rule for every endpoint (core/identity, Phase 4):
        reading (documents, search, pages) needs scope knowledge:read - anonymous callers have it and see only public
        documents; AI calls need ai:ask (scan: ai:vision), which costs API credits, so a protected server needs a
        token or an API key. API keys (Bearer vrk_...) and MCP_TOKEN get user=None: public documents only."""
        scope = scope or ("ai:ask" if ai else "knowledge:read")
        if identity is None:                                   # older wiring (no identity): previous rules
            if service_token(req):
                return True, None
            return (auth.require_user(req) if ai else True), auth.user_of(req)
        try:
            _, user = identity.require(req, scope)
            return True, user
        except PolicyDenied as e:
            return False, e

    def denied(err=None):
        status = err.status if isinstance(err, PolicyDenied) else 401
        msg = err.reason if isinstance(err, PolicyDenied) and status == 403 else "unauthorized"
        return JSONResponse({"error": msg, "hint": "send X-VR-Token, or Authorization: Bearer <API key or MCP_TOKEN>"},
                            status)

    def visible(entry, user):
        return bool(entry) and auth.can_see(entry.get("groups", []), user)

    # ------------------------------------------------------------ shared pieces
    def page_url(uid, page, w=None):
        return f"{API}/documents/{uid}/pages/{int(page)}.jpg" + (f"?w={int(w)}" if w else "")

    def passage(h, n=None):
        p = {"doc_uid": h["uid"], "title": h["title"], "page": h["page"], "text": h["text"],
             "score": round(float(h["score"]), 4), "image_url": page_url(h["uid"], h["page"])}
        if n is not None:
            p["n"] = n
        return p

    def citation(h, n):
        e = store.doc_entry(h["uid"]) or {}
        return {"n": n, "doc_uid": h["uid"], "title": h["title"], "page": h["page"], "pages": e.get("pages"),
                "image_url": page_url(h["uid"], h["page"]), "image_url_small": page_url(h["uid"], h["page"], 600)}

    def opts(b):
        style = str(b.get("style") or "full").lower()
        return (style if style in STYLES else "full"), str(b.get("lang") or "")[:30]

    def find(query, k, machine, user):
        return run_search(query, max(1, min(20, int(k or 6))), str(machine or "")[:80], user)

    def messages(question, hits, style, lang, photo=None):
        system = SMART_SYSTEM + (VISION_RULE if photo else "") + style_rules(style, lang)
        seen = ""
        if photo:
            seen = ("PHOTO DESCRIPTION (from the vision model): " + (photo.get("object") or "unknown object") + ". "
                    + (photo.get("details", "") + " " if photo.get("details") else "")
                    + "Text on it: " + (photo.get("text_seen") or "none") + ".\n\n")
        user = seen + (excerpts(hits) if hits else "Excerpts: (none matched)\n\n") + "Question: " + question
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def result(raw, hits, res, extra=None, positions=None):
        body, mode, cited = parse_answer(raw, hits)
        out = {"answer": body, "mode": mode,
               "citations": [citation(hits[i], n + 1) for n, i in enumerate(cited)],
               "confidence": res.get("conf"), "search_mode": res.get("mode")}
        if positions is not None:            # client sent its passages: cited = positions in the CLIENT's list
            out["cited"] = [positions[i] for i in cited]
        if extra:
            out.update(extra)
        return out

    def offline(hits, res, note):
        if not hits:
            return {"answer": "No matching text in the documents, and no AI is connected for general questions.",
                    "mode": "offline", "citations": [], "confidence": res.get("conf"), "search_mode": res.get("mode"), "note": note}
        return {"answer": extractive(hits), "mode": "offline", "citations": [citation(h, i + 1) for i, h in enumerate(hits[:3])],
                "confidence": res.get("conf"), "search_mode": res.get("mode"), "note": note}

    def answer(question, machine, style, lang, user, photo=None, k=6, given=None):
        """The one RAG pipeline every door uses: search -> Groq -> parsed answer with citations.
        `given` = (hits, positions) from passages_of(): the client already searched, skip the search."""
        positions = None
        if given is not None:
            hits, positions = given
            res = {"mode": "client", "conf": None}
        else:
            query = question if not photo else " ".join(
                x for x in (question, photo.get("object", ""), photo.get("text_seen", ""), photo.get("keywords", "")) if x)
            res = find(query, k, machine, user)
            hits = res["hits"][:k]
        if not provider():
            return offline(hits, res, "no AI key on the server")
        try:
            raw = llm_complete(messages(question, hits, style, lang, photo), 900)
        except Exception as e:
            return offline(hits, res, f"AI error: {e}")
        return result(raw, hits, res, positions=positions)

    def question_of(b, key="question"):
        q = str(b.get(key) or b.get("query") or "").strip()[:MAX_Q]
        return q

    def photo_of(b):
        """Optional photo description the client already got from /api/vision/describe (one vision call per photo)."""
        p = b.get("photo")
        if not isinstance(p, dict):
            return None
        out = {k: str(p.get(k) or "")[:500] for k in ("object", "details", "text_seen", "keywords")}
        return out if any(out.values()) else None

    def passages_of(b, user):
        """Optional passages the client already found (its own search). Only REFERENCES are accepted -
        chunk position i, doc_uid, page - never text: the server reads the text from its own index and checks the
        caller may see the document, so a client cannot inject excerpts or prompts. Returns (hits, positions):
        positions[j] = index of hits[j] in the client's list, so citations can point back at the client's hits.
        None = the field was not sent (normal search)."""
        refs = b.get("passages")
        if not isinstance(refs, list):
            return None
        snap = store.snapshot
        chunks, docs = snap["index"]["chunks"], snap["index"]["docs"]
        hits, positions = [], []
        for pos, ref in enumerate(refs[:MAX_PASSAGES]):
            if not isinstance(ref, dict):
                continue
            uid, page = str(ref.get("doc_uid") or ""), ref.get("page")
            try:
                i = int(ref.get("i", -1))
                page = int(page)
            except (TypeError, ValueError):
                continue
            ok = 0 <= i < len(chunks) and docs[chunks[i]["doc"]]["uid"] == uid and chunks[i]["page"] == page
            if not ok:                       # client index slightly out of date: first chunk of that page instead
                i = next((j for j, c in enumerate(chunks) if c["page"] == page and docs[c["doc"]]["uid"] == uid), -1)
            if i < 0:
                continue
            c = chunks[i]
            d = docs[c["doc"]]
            if not auth.can_see(d.get("groups", []), user):
                continue
            hits.append({"i": i, "score": 0.0, "kw": None, "sem": None, "doc": c["doc"], "page": c["page"],
                         "uid": d["uid"], "title": d["title"], "text": c["text"]})
            positions.append(pos)
        return hits, positions

    # ------------------------------------------------------------ endpoints
    @app.get(f"{API}/health")
    def v1_health():
        p = provider()
        snap = store.snapshot
        vis = False
        if vision and p and p["name"] == "groq":
            try:
                vis = bool(vision["status"]().get("available"))
            except Exception:
                vis = False
        return {"api": "v1", "ok": True, "ai": bool(p), "provider": p["name"] if p else "",
                "photo": vis, "documents": len(snap["index"]["docs"]), "index_version": snap["version"],
                "styles": list(STYLES), "doors": {"mcp": "/mcp", "hud": "/hud", "webxr": "/"}}

    @app.get(f"{API}/documents")
    def v1_documents(req: Request):
        ok, user = caller(req)
        if not ok:
            return denied(user)
        docs = [{"doc_uid": d["uid"], "title": d["title"], "pages": d["pages"], "type": d.get("type", ""),
                 "machine_ids": d.get("tags", [])}
                for d in store.snapshot["index"]["docs"] if d.get("uid") and auth.can_see(d.get("groups", []), user)]
        return {"documents": docs, "index_version": store.snapshot["version"]}

    @app.post(f"{API}/search")
    def v1_search(req: Request, _body=JSONB):
        ok, user = caller(req)
        if not ok:
            return denied(user)
        b = _body or {}
        q = question_of(b, "query")
        if not q:
            return JSONResponse({"error": "query is required"}, 400)
        res = find(q, b.get("k", 6), b.get("machine_id") or b.get("machine"), user)
        return {"query": q, "mode": res.get("mode"), "engine": res.get("engine"), "confidence": res.get("conf"),
                "low_confidence": bool(res.get("lowConfidence")), "machine": res.get("machine"),
                "machine_matched": res.get("machineMatched"), "index_version": res.get("version"),
                # i / semantic / keyword: chunk position + raw scores, used by the WebXR app's local index
                "passages": [dict(passage(h, n + 1), i=h["i"], semantic=h.get("sem"), keyword=h.get("kw"))
                             for n, h in enumerate(res["hits"])]}

    @app.post(f"{API}/ask")
    def v1_ask(req: Request, _body=JSONB):
        ok, user = caller(req, ai=True)
        if not ok:
            return denied(user)
        b = _body or {}
        q = question_of(b)
        if not q:
            return JSONResponse({"error": "question is required"}, 400)
        style, lang = opts(b)
        return answer(q, b.get("machine_id"), style, lang, user, photo=photo_of(b), given=passages_of(b, user))

    @app.post(f"{API}/ask/stream")
    def v1_ask_stream(req: Request, _body=JSONB):
        ok, user = caller(req, ai=True)
        if not ok:
            return denied(user)
        b = _body or {}
        q = question_of(b)
        if not q:
            return JSONResponse({"error": "question is required"}, 400)
        style, lang = opts(b)
        photo, given = photo_of(b), passages_of(b, user)
        positions = None
        if given is not None:
            (hits, positions), res = given, {"mode": "client", "conf": None}
        else:
            query = q if not photo else " ".join(x for x in (q, photo["object"], photo["text_seen"], photo["keywords"]) if x)
            res = find(query, 6, b.get("machine_id"), user)
            hits = res["hits"][:6]

        def sse(event, data):
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        def gen():
            yield sse("meta", {"search_mode": res.get("mode"), "confidence": res.get("conf"),
                               "candidates": [citation(h, n + 1) for n, h in enumerate(hits)]})
            if not provider():
                yield sse("done", offline(hits, res, "no AI key on the server"))
                return
            raw = []
            try:
                for piece in llm_stream(messages(q, hits, style, lang, photo), 900):
                    raw.append(piece)
                    yield sse("delta", {"text": piece, "answer_so_far": clean_body("".join(raw))})
            except Exception as e:
                yield sse("done", offline(hits, res, f"AI error: {e}"))
                return
            yield sse("done", result("".join(raw), hits, res, positions=positions))

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post(f"{API}/scan")
    def v1_scan(req: Request, _body=JSONB):
        ok, user = caller(req, scope="ai:vision")
        if not ok:
            return denied(user)
        if not vision:
            return JSONResponse({"error": "photo questions are not available on this server"}, 501)
        b = _body or {}
        image = b.get("image")
        bad = vision["check"](image)
        if bad:
            return JSONResponse({"error": bad}, 400)
        q = question_of(b) or "What is this, and what do my documents say about it?"
        style, lang = opts(b)
        try:
            photo = vision["describe"](image, b.get("hint", ""))
        except Exception as e:
            return JSONResponse({"error": str(e)}, 502)
        out = answer(q, b.get("machine_id"), style, lang, user, photo=photo)
        out["photo"] = {k: photo.get(k, "") for k in ("object", "details", "text_seen")}
        return out

    @app.get(API + "/documents/{uid}/pages/{page}")
    def v1_page(uid: str, page: str, req: Request):
        ok, user = caller(req)
        if not ok:
            return denied(user)
        m = re.fullmatch(r"(\d+)(\.jpg)?", page)
        if not re.fullmatch(r"[0-9a-f]{12}", uid) or not m:
            return JSONResponse({"error": "not found"}, 404)
        n = int(m.group(1))
        e = store.doc_entry(uid)
        if not visible(e, user):
            return JSONResponse({"error": "not found"}, 404)
        if m.group(2):
            return page_image(uid, n, req.query_params.get("w"))
        text = store.page_text(uid, n)
        if text is None:
            return JSONResponse({"error": "page not found"}, 404)
        return {"doc_uid": uid, "title": e["title"], "page": n, "pages": e.get("pages"), "text": text,
                "image_url": page_url(uid, n), "image_url_small": page_url(uid, n, 600)}

    def page_image(uid, n, w):
        p = store.page_image_uid(uid, n)
        if not p:
            return JSONResponse({"error": "page not found"}, 404)
        headers = {"Cache-Control": "private, max-age=3600"}
        try:
            w = int(w) if w else 0
        except ValueError:
            w = 0
        if not w:
            return Response(p.read_bytes(), media_type="image/jpeg", headers=headers)
        w = max(200, min(1600, w))
        key = (str(p), p.stat().st_mtime, w)
        with lock:
            data = thumbs.get(key)
        if data is None:
            from PIL import Image
            with Image.open(p) as im:
                im = im.convert("RGB")
                if im.width > w:
                    im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
                buf = BytesIO()
                im.save(buf, "JPEG", quality=82, optimize=True)
                data = buf.getvalue()
            with lock:
                thumbs[key] = data
                while len(thumbs) > 120:
                    thumbs.popitem(last=False)
        return Response(data, media_type="image/jpeg", headers=headers)

    @app.get("/hud")
    def hud_page():
        return FileResponse(Path(__file__).resolve().parent / "static" / "hud.html", headers={"Cache-Control": "no-cache"})

    # shared with mcp_server.py so MCP answers are the same answers the glasses get
    return {"answer": answer, "search": find, "passage": passage, "page_url": page_url, "visible": visible}

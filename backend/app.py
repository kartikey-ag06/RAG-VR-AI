"""
VR_RAG backend (FastAPI) - replaces server.ps1 and adds EMBEDDINGS + HYBRID SEARCH.

What it does
  1. Serves the VR app (glasses_app/dist) on http://localhost:8765
  2. /api/search  : hybrid search = embeddings (meaning) + TF-IDF (exact words), merged with
                    Reciprocal Rank Fusion (RRF)
  3. /api/grok    : AI answers via Groq (gsk_...) or Grok/xAI (xai-...); the key never reaches the browser
  4. /api/stt     : voice -> text via Groq Whisper
  5. /api/server/stop : stop the server (localhost only)
  6. /upload      : web page to upload PDF / DOCX / TXT; files are stored + indexed in backend/data/
     /api/files   : list (GET), upload (POST), delete (DELETE /api/files/<uid>)
     /rag/index.json, /rag/pages/... : served from the document store, so the VR app always sees the uploads
  7. /procops     : ProcOps dashboard - question heatmap per page, AI-drafted fix, approve -> new version (procops.py)
  8. /api/vision/* : photo + question ("scan, then ask") via a Groq vision model (vision.py)

Embeddings
  Model: BAAI/bge-small-en-v1.5 via fastembed (ONNX, CPU, no PyTorch). Downloaded once (~70 MB).
  The 1,136 PDF chunks are embedded once at startup and cached in backend/cache/.

Run:  START_VR_RAG.bat   (or: python backend/app.py)
"""
import envfile  # noqa: F401  - must be first: loads .env before other modules read their settings
from net import HTTP, JSONB
import limits
from limits import LoadGuard
log = limits.log
from starlette.concurrency import run_in_threadpool
import json
import os
import re
import socket
import sys
import threading
import webbrowser
import gzip
import hashlib
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import requests
import uvicorn
from fastapi import File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from indexer import Store, tokenize
from core.kernel import Container, Settings, create_app

# ---------------------------------------------------------------- settings + app shell (core/kernel)
SETTINGS = Settings.from_env(Path(__file__).resolve().parent.parent, sys.argv, os.environ)
ROOT = SETTINGS.root
APP_DIR = SETTINGS.app_dir
EMBED_MODEL = SETTINGS.embed_model
DATA_DIR = SETTINGS.data_dir          # VR_RAG_DATA_DIR moves the document store + app data (tests use a temp folder)

limits.setup_logging()
# Load protection (rate limits, body-size limits, AI budget + concurrency, request ids) sits INSIDE CORS, so a
# 429 / 413 answer still carries CORS headers and the browser can show its message. /healthz comes from the factory.
app = create_app(SETTINGS, middleware=[(LoadGuard, {})])


@app.on_event("startup")
def on_startup():
    threading.Thread(target=load_embeddings, daemon=True).start()

# ---------------------------------------------------------------- shared services (built once, in this order)
# optional Postgres (Neon) + pgvector: set DATABASE_URL or put the URL in database_url.txt (git-ignored)
# VR_RAG_NO_DB=1 forces local files even when a database is configured (tests must never touch the real database).
from core.data import DB, LocalKV, database_url
from auth import Auth


def _make_db(c):
    if not database_url(ROOT) or SETTINGS.no_db:
        return None
    try:
        db = DB(database_url(ROOT))
        print("[db] connected: documents, vectors and app data are stored in Postgres (pgvector)", flush=True)
        return db
    except Exception as _e:
        print(f"[db] NOT available, using local files only: {_e}", flush=True)
        return None


SERVICES = Container()
SERVICES.register("db", _make_db)
SERVICES.register("store", lambda c: Store(SETTINGS.data_dir, seed_dir=SETTINGS.seed_dir, db=c.get("db")))
SERVICES.register("kv", lambda c: c.get("db") or LocalKV(SETTINGS.data_dir))   # reminders, pins, sessions...
SERVICES.register("auth", lambda c: Auth(c.get("kv")))
app.state.services = SERVICES

# old global names kept as aliases, so feature modules and tests keep working unchanged
DBX = SERVICES.get("db")
STORE = SERVICES.get("store")        # first run imports the 10 original PDFs from docs/
KV = SERVICES.get("kv")
AUTH = SERVICES.get("auth")
# ---------------------------------------------------------------- search -> core/knowledge
from core.knowledge import LOW_CONFIDENCE, SearchEngine, norm_code
from core.knowledge.search import RRF_K, W_KEYWORD, W_SEMANTIC  # noqa: F401  (old names, still importable)

SEARCH = SearchEngine(STORE, tokenize=tokenize, can_see=AUTH.can_see, embed_model=EMBED_MODEL, db=DBX)
EMB = SEARCH.emb                       # same dict: status pages read EMB["state"]
load_embeddings = SEARCH.load_embeddings


def visible_positions(snap, user):
    return SEARCH.visible_positions(snap, user)


def run_search(query, k=8, machine="", user=None):
    """Hybrid search used by the glasses, MCP, workflows. Respects permissions and the machine filter.
    The Core searches by generic TAG; the old API calls it "machine", so the field names are mapped back here."""
    res = SEARCH.search(query, k, tag=machine, user=user)
    return {("machine" if key == "tag" else "machineMatched" if key == "tagMatched" else key): v for key, v in res.items()}


@app.post("/api/search")
def search(req: Request, _body=JSONB):
    body = (_body if _body is not None else {})
    try:
        k = max(1, min(20, int(body.get("k", 8))))
    except (TypeError, ValueError):
        return JSONResponse({"error": "'k' must be a whole number"}, 400)
    return run_search(str(body.get("query", "")).strip(), k,
                      str(body.get("machine", "") or "")[:80], AUTH.user_of(req))


# ---------------------------------------------------------------- uploads + dynamic index for the VR app
NO_CACHE = {"Cache-Control": "no-store"}


@app.get("/theme/{name}")
def ui_asset(name: str):
    """Shared theme / navigation for the web pages (vr-theme.css, vr-shell.js)."""
    f = ROOT / "backend" / "static" / name
    if not re.fullmatch(r"[\w.-]+\.(css|js|svg)", name) or not f.is_file():
        return JSONResponse({"error": "not found"}, 404)
    mime = "text/css" if name.endswith(".css") else ("application/javascript" if name.endswith(".js") else ("image/svg+xml" if name.endswith(".svg") else None))
    return FileResponse(f, media_type=mime, headers={"Cache-Control": "no-cache"})


@app.get("/upload")
def upload_page():
    return FileResponse(ROOT / "backend" / "static" / "upload.html", headers=NO_CACHE)


@app.get("/api/files")
def files_list(req: Request):
    snap, user = STORE.snapshot, AUTH.user_of(req)
    return {"files": [e for e in STORE.list() if AUTH.can_see(e.get("groups", []), user)], "version": snap["version"], "state": STORE.state,
            "chunks": len(snap["index"]["chunks"]), "embeddings": EMB["state"]}


@app.post("/api/files")
async def files_upload(req: Request, files: list[UploadFile] = File(...)):
    if AUTH.admin_required(req) and not AUTH.is_admin(req):
        return JSONResponse({"error": "admin token required to upload"}, 403)
    form = await req.form()
    tags = [t.strip()[:60] for t in str(form.get("tags") or "").split(",") if t.strip()][:30]
    groups = [g.strip()[:40] for g in str(form.get("groups") or "").split(",") if g.strip()][:20]
    added, errors = [], []
    for f in files:
        try:
            data = await f.read()
            added.append(await run_in_threadpool(STORE.add, f.filename or "file", data, tags=tags, groups=groups))
        except ValueError as e:
            errors.append(str(e))
    return JSONResponse({"added": added, "errors": errors}, status_code=200 if added or not errors else 400)


@app.delete("/api/files/{uid}")
def files_delete(uid: str, req: Request):
    if AUTH.admin_required(req) and not AUTH.is_admin(req):
        return JSONResponse({"error": "admin token required"}, 403)
    if not re.fullmatch(r"[0-9a-f]{12}", uid) or not STORE.delete(uid):
        return JSONResponse({"error": "not found"}, 404)
    return {"deleted": uid}


@app.patch("/api/files/{uid}")
def files_patch(uid: str, req: Request, _body=JSONB):
    """Set title, tags (machine ids / QR codes / serials) and groups (who may see it)."""
    if AUTH.admin_required(req) and not AUTH.is_admin(req):
        return JSONResponse({"error": "admin token required"}, 403)
    b = (_body if _body is not None else {})
    clean = lambda v, n: None if v is None else [str(x).strip()[:60] for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip()][:n]
    e = STORE.update_meta(uid, title=(str(b["title"])[:200] if b.get("title") else None),
                          tags=clean(b.get("tags"), 30), groups=clean(b.get("groups"), 20)) if re.fullmatch(r"[0-9a-f]{12}", uid) else None
    return e or JSONResponse({"error": "not found"}, 404)


# ---------------------------------------------------------------- Visual-to-RAG: machine ids from QR / barcodes / serials
@app.get("/api/machines")
def machines(req: Request):
    snap, user = STORE.snapshot, AUTH.user_of(req)
    out = {}
    for d in snap["index"]["docs"]:
        if AUTH.can_see(d.get("groups", []), user):
            for t in d.get("tags", []):
                out.setdefault(t, []).append({"uid": d["uid"], "title": d["title"], "id": d["id"]})
    return {"machines": [{"machine_id": k, "docs": v} for k, v in sorted(out.items())]}


@app.post("/api/machines/resolve")
def machines_resolve(req: Request, _body=JSONB):
    """Text from a QR code / barcode / label photo -> the machine id it belongs to (or none)."""
    b = (_body if _body is not None else {})
    text = str(b.get("text", ""))[:2000]
    snap, user = STORE.snapshot, AUTH.user_of(req)
    tags = {t for d in snap["index"]["docs"] if AUTH.can_see(d.get("groups", []), user) for t in d.get("tags", [])}
    nt = norm_code(text)
    words = {norm_code(w) for w in re.split(r"[\s,;:/|]+", text) if len(norm_code(w)) >= 3}
    best = None
    for t in sorted(tags, key=lambda t: -len(norm_code(t))):
        n = norm_code(t)
        if n and (n in words or (len(n) >= 4 and n in nt)):
            best = t
            break
    docs = [{"uid": d["uid"], "title": d["title"], "id": d["id"]} for d in snap["index"]["docs"]
            if best in d.get("tags", []) and AUTH.can_see(d.get("groups", []), user)] if best else []
    return {"machine_id": best, "docs": docs, "text": text[:200]}


@app.get("/api/index/version")
def index_version():
    snap = STORE.snapshot
    return {"version": snap["version"], "docs": len(snap["index"]["docs"]),
            "chunks": len(snap["index"]["chunks"]), "state": STORE.state}


_ix_cache = {}
_ix_lock = threading.Lock()


@app.get("/rag/index.json")
def rag_index(req: Request):
    """The search index the glasses load (about 1.5 MB). Built once per (version, visible docs), kept gzip-compressed
    (about 0.4 MB on the wire), and answered with 304 Not Modified when the glasses already have this version."""
    snap = STORE.snapshot
    if AUTH.enabled:
        ok = set(visible_positions(snap, AUTH.user_of(req)))
        key = (snap["version"], tuple(sorted(ok)))
    else:
        ok, key = None, (snap["version"], "all")
    entry = _ix_cache.get(key)
    if entry is None:
        ix = snap["index"]
        if ok is not None:          # same doc positions for everyone; hidden docs are emptied, not removed
            ix = dict(ix)
            ix["docs"] = [d if d["id"] in ok else {"id": d["id"], "title": "(restricted)", "file": "", "pages": 0,
                                                    "type": "", "uid": "", "tags": [], "figs": {}} for d in snap["index"]["docs"]]
            # same length as the server's list: search results are chunk positions, so hidden chunks are blanked
            ix["chunks"] = [c if c["doc"] in ok else {"doc": c["doc"], "page": 0, "text": "", "terms": [], "weights": []}
                            for c in snap["index"]["chunks"]]
        raw = json.dumps(ix, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        entry = {"raw": raw, "gz": gzip.compress(raw, 6), "etag": '"' + hashlib.sha1(raw).hexdigest()[:20] + '"'}
        with _ix_lock:
            _ix_cache[key] = entry
            while len(_ix_cache) > 8:           # a few permission sets per version; old versions fall out
                _ix_cache.pop(next(iter(_ix_cache)))
    headers = {"ETag": entry["etag"], "Cache-Control": "private, no-cache", "Vary": "Accept-Encoding, X-VR-Token"}
    if entry["etag"] in (req.headers.get("if-none-match") or ""):
        return Response(status_code=304, headers=headers)
    if "gzip" in (req.headers.get("accept-encoding") or ""):
        return Response(entry["gz"], media_type="application/json", headers={**headers, "Content-Encoding": "gzip"})
    return Response(entry["raw"], media_type="application/json", headers=headers)


def _doc_visible(pos, req):
    docs = STORE.snapshot["index"]["docs"]
    return 0 <= pos < len(docs) and AUTH.can_see(docs[pos].get("groups", []), AUTH.user_of(req))


@app.get("/rag/figures/{pos}/{file}")
def rag_figure(pos: int, file: str, req: Request):
    uids = STORE.snapshot["uids"]
    p = STORE.figure_image(uids[pos], file) if 0 <= pos < len(uids) and _doc_visible(pos, req) else None
    if not p:
        return JSONResponse({"error": "figure not found"}, 404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/rag/pages/{name}")
def rag_page(name: str, req: Request):
    m = re.fullmatch(r"d(\d+)_p(\d+)\.jpg", name)
    p = STORE.page_image(int(m.group(1)), int(m.group(2))) if m and _doc_visible(int(m.group(1)), req) else None
    if not p:
        return JSONResponse({"error": "page not found"}, 404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------- LLM provider (Groq / Grok) -> core/ai
from core.ai import AIGateway, SseReader, clean_messages

KEY_FILE = ROOT / "grok_api_key.txt"
MODEL_FILE = ROOT / "grok_model.txt"
# key_file / model_file are read through these lambdas so a test can still swap KEY_FILE / MODEL_FILE on this module
AI = AIGateway(HTTP, os.environ, key_file=lambda: KEY_FILE, model_file=lambda: MODEL_FILE, log=log)

# old names kept as aliases: feature modules (vision, api_v1, procops, ...) and tests use them unchanged
get_key, provider, list_models = AI.get_key, AI.provider, AI.list_models
chat_model, stt_model, fast_model = AI.chat_model, AI.stt_model, AI.fast_model
llm_complete, llm_stream = AI.complete, AI.stream
_cache, _MODELS, _ANSWERS = AI.model_choice, AI.models_cache, AI.answers
_clean_messages = clean_messages


@app.get("/api/grok/status")
def status():
    p = provider()
    return {"configured": bool(p), "provider": p["name"] if p else "", "model": chat_model() if p else "",
            "stt": bool(p and p["name"] == "groq"),
            "search": "hybrid" if EMB["state"] == "ready" else ("keyword (embeddings " + EMB["state"] + ")"),
            "docs": len(STORE.snapshot["index"]["docs"]), "indexVersion": STORE.snapshot["version"],
            "embedModel": EMBED_MODEL, "backend": "python", "storage": "postgres+pgvector" if DBX else "local files",
            "rbac": AUTH.enabled, "features": ["captions", "translate", "liveai", "reminders", "weather", "workflows",
                                               "pins", "exports", "expert", "mcp", "machines", "figures", "teleprompter", "meetings",
                                               "gestures", "directions", "calendar-sync", "privacy-light", "cloud-connectors"]}


@app.get("/api/grok/models")
def models():
    try:
        return {"data": [{"id": m} for m in list_models()]}
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        log.warning("model list failed: %s", e)
        return JSONResponse({"error": "could not load the model list from the AI provider"}, status_code=502)


def _admin_only(req):
    """/api/grok sends any prompt to the AI, so only admins (ADMIN_TOKEN, an admin user, or an unprotected
    local server) may use it. Apps ask through /api/v1/ask, which builds the prompt on the server.
    401 = not logged in at all; 403 = logged in, but not an admin."""
    if not AUTH.user_of(req):
        return JSONResponse({"error": "unauthorized", "use": "/api/v1/ask"}, 401)
    return JSONResponse({"error": "admin only - apps must use /api/v1/ask", "use": "/api/v1/ask"}, 403)


def _rate_limited():
    return JSONResponse({"error": "the AI provider is rate limiting us - try again in a minute", "retry_after": 30}, 503,
                        headers={"Retry-After": "30"})


@app.post("/api/grok")
def grok(req: Request, _body=JSONB):
    # Raw AI proxy: forwards any system prompt, so it is ADMIN-ONLY (ADR 0004). Apps use /api/v1/ask.
    if not AUTH.is_admin(req):
        return _admin_only(req)
    p = provider()
    if not p:
        return JSONResponse({"error": "No API key. Put a Groq (gsk_...) or xAI (xai-...) key in grok_api_key.txt"}, 400)
    messages, err = clean_messages(_body)
    if err:
        return JSONResponse({"error": err}, 400)
    key = AI.cache_key(messages)
    if AI.cached(key) is not None:
        return {"choices": [{"message": {"role": "assistant", "content": AI.cached(key)}}], "cached": True}
    with AI.single_flight(key) as leader:
        if not leader:                       # same question was already on its way: use that answer
            if AI.cached(key) is not None:
                return {"choices": [{"message": {"role": "assistant", "content": AI.cached(key)}}], "cached": True, "shared": True}
        try:
            r = AI.post_chat(messages)
        except Exception as e:
            log.warning("AI provider unreachable: %s", e)
            return JSONResponse({"error": f"{p['name']} is unreachable - try again shortly"}, 502)
        if r.status_code == 429:
            return _rate_limited()
        if r.status_code != 200:
            log.warning("AI provider HTTP %s: %s", r.status_code, r.text[:300])
            return JSONResponse({"error": f"{p['name']} API error (HTTP {r.status_code})"}, 502)
        j = r.json()
        try:
            AI.remember(key, j["choices"][0]["message"]["content"] or "")
        except Exception:
            pass
        return j


@app.post("/api/grok/stream")
def grok_stream(req: Request, _body=JSONB):
    """Same as /api/grok but streams the answer as plain text while it is written. ADMIN-ONLY (ADR 0004)."""
    if not AUTH.is_admin(req):
        return _admin_only(req)
    p = provider()
    if not p:
        return JSONResponse({"error": "No API key in grok_api_key.txt"}, 400)
    messages, err = clean_messages(_body)
    if err:
        return JSONResponse({"error": err}, 400)
    key = AI.cache_key(messages)
    if AI.cached(key) is not None:
        return Response(AI.cached(key), media_type="text/plain; charset=utf-8", headers={"X-Cache": "hit"})
    try:
        r = AI.post_chat(messages, stream=True)
    except Exception as e:
        log.warning("AI provider unreachable: %s", e)
        return JSONResponse({"error": f"{p['name']} is unreachable - try again shortly"}, 502)
    if r.status_code != 200:
        txt = r.text[:300]
        r.close()
        if r.status_code == 429:
            return _rate_limited()
        log.warning("AI provider HTTP %s: %s", r.status_code, txt)
        return JSONResponse({"error": f"{p['name']} API error (HTTP {r.status_code})"}, 502)

    def gen():
        out, reader = [], SseReader(r)
        try:
            for piece in reader:
                out.append(piece)
                yield piece
        finally:
            r.close()                   # also runs when the user presses Stop: the provider stops sending at once
            if reader.complete:         # never cache a half answer (stopped by the user)
                AI.remember(key, "".join(out))
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/stt")
async def stt(req: Request):
    if AUTH.admin_required(req) and not (AUTH.is_admin(req) or AUTH.user_of(req)):
        return JSONResponse({"error": "unauthorized"}, 401)
    p = provider()
    if not p or p["name"] != "groq":
        return JSONResponse({"error": "Voice transcription needs a Groq key (gsk_...)"}, 400)
    audio = await req.body()
    if len(audio) < 800:
        return JSONResponse({"error": "Recording too short - hold the button a bit longer"}, 400)
    mime = (req.headers.get("content-type") or "audio/webm").split(";")[0].strip()
    lang = (req.query_params.get("lang") or "").split("-")[0].lower()        # ISO-639-1, e.g. hi, ta, en
    lang = lang if re.fullmatch(r"[a-z]{2}", lang) else ""
    hint = (req.query_params.get("prompt") or "")[:200]          # vocabulary hint: Whisper then spells "Hey Glasses" right
    ext = {"audio/ogg": "ogg", "audio/mp4": "m4a", "audio/wav": "wav", "audio/mpeg": "mp3"}.get(mime, "webm")
    try:
        r = await run_in_threadpool(AI.transcribe, audio, mime, ext, lang, hint)
    except Exception as e:
        log.warning("speech-to-text unreachable: %s", e)
        return JSONResponse({"error": "speech service unreachable - try again shortly"}, 502)
    if r.status_code != 200:
        log.warning("speech-to-text HTTP %s: %s", r.status_code, r.text[:300])
        if r.status_code == 429:
            return JSONResponse({"error": "speech service is busy - try again in a minute"}, 503, headers={"Retry-After": "30"})
        return JSONResponse({"error": f"speech service error (HTTP {r.status_code})"}, 502)
    return r.json()


# ---------------------------------------------------------------- ProcOps (question log, heatmap, AI redline, versions)
try:
    import procops
    procops.register(app, STORE, llm_complete, ROOT, AUTH)
except Exception as _e:  # ProcOps must never stop the main app from starting
    print(f"[procops] disabled: {_e}", flush=True)


def _register(name, fn):
    try:
        return fn()
    except Exception as _e:  # one broken module must never stop the main app
        import traceback
        traceback.print_exc()
        print(f"[{name}] disabled: {_e}", flush=True)


import assistant, connectors, workflows, expert, mcp_server, calsync, navigation, meetings
CALS = _register("calendar sync", lambda: calsync.register(app, KV, AUTH))
_register("assistant", lambda: assistant.register(app, KV, llm_complete, CALS, AUTH))
CONNECTORS = _register("connectors", lambda: connectors.register(app, KV, AUTH, STORE)) or {"send": None, "get": lambda c: None}
_register("workflows", lambda: workflows.register(app, KV, llm_complete, run_search, STORE, CONNECTORS, ROOT, AUTH))
_register("expert", lambda: expert.register(app, ROOT, AUTH))
_register("navigation", lambda: navigation.register(app))
_register("meetings", lambda: meetings.register(app, KV, llm_complete, run_search, AUTH))


# ---------------------------------------------------------------- admin: users (RBAC) + status
@app.get("/admin")
def admin_page():
    return FileResponse(ROOT / "backend" / "static" / "admin.html", headers=NO_CACHE)


@app.get("/api/admin/status")
def admin_status(req: Request):
    return {"admin": AUTH.is_admin(req), "rbac": AUTH.enabled, "protected": AUTH.admin_required(req),
            "storage": "postgres+pgvector" if DBX else "local files", "embeddings": EMB["state"],
            "mcp": {"url": "/mcp", "token_required": bool(os.environ.get("MCP_TOKEN"))},
            "users": [AUTH.public(u) for u in AUTH.users()] if AUTH.is_admin(req) else [],
            "load": limits.snapshot() if AUTH.is_admin(req) else None}


@app.post("/api/admin/users")
def admin_user_add(req: Request, _body=JSONB):
    if not AUTH.is_admin(req):
        return JSONResponse({"error": "admin only"}, 403)
    b = (_body if _body is not None else {})
    groups = b.get("groups") if isinstance(b.get("groups"), list) else str(b.get("groups", "")).split(",")
    rec, token = AUTH.create_user(str(b.get("name", "user")), groups, str(b.get("role", "user")))
    return {"user": AUTH.public(rec), "token": token, "note": "Copy the token now - it is not shown again."}


@app.delete("/api/admin/users/{uid}")
def admin_user_delete(uid: str, req: Request):
    if not AUTH.is_admin(req):
        return JSONResponse({"error": "admin only"}, 403)
    AUTH.delete_user(uid)
    return {"deleted": uid}


VISION = None
try:
    import vision
    VISION = vision.register(app, provider, get_key, list_models, ROOT, AUTH)   # "scan, then ask" (photo questions)
except Exception as _e:
    print(f"[vision] disabled: {_e}", flush=True)


# ---------------------------------------------------------------- one API for every glasses client ("many doors")
import api_v1
CORE = _register("api v1", lambda: api_v1.register(app, STORE, run_search, llm_complete, llm_stream, AUTH, provider, VISION))
_register("mcp", lambda: mcp_server.register(app, STORE, run_search, llm_complete, AUTH, CORE))


@app.post("/api/server/stop")
def stop(req: Request):
    if (req.client.host if req.client else "") not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"error": "only from this computer"}, 403)
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"stopping": True}


# static app last, so /api/* wins
if APP_DIR.exists():
    app.mount("/", StaticFiles(directory=str(APP_DIR), html=True), name="app")


def free_port(start=8765):
    for p in range(start, start + 10):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise SystemExit("No free port between 8765 and 8774")


if __name__ == "__main__":
    env_port = os.environ.get("PORT")
    port = int(env_port) if env_port else free_port()
    host = "0.0.0.0" if env_port else "127.0.0.1"
    p = provider()
    print(f"VR_RAG (python backend) running at http://{host}:{port}/  (close this window to stop)")
    print(f"AI: {'ON - ' + p['name'] if p else 'OFF - put a Groq key in grok_api_key.txt'}")
    print(f"Search: hybrid (TF-IDF + {EMBED_MODEL}) - embeddings load in the background")
    print(f"Upload documents at http://{host}:{port}/upload  (stored in {DATA_DIR})")
    print(f"ProcOps dashboard at http://{host}:{port}/procops")
    if "--no-browser" not in sys.argv and not env_port:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}/")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")

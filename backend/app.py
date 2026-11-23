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
from net import HTTP, JSONB
from starlette.concurrency import run_in_threadpool
import json
import math
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from collections import Counter, OrderedDict
import hashlib
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import numpy as np
import requests
import uvicorn
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from indexer import Store, tokenize

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / ("web" if "--app=web" in sys.argv else "glasses_app/dist")
EMBED_MODEL = os.environ.get("VR_RAG_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
RRF_K = 60            # standard RRF constant
W_SEMANTIC = 1.0      # weight of the embedding ranking
W_KEYWORD = 0.8       # weight of the TF-IDF ranking (exact terms like "15HB", "13,00,00,000")

app = FastAPI(title="VR_RAG backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    threading.Thread(target=load_embeddings, daemon=True).start()

# ---------------------------------------------------------------- document store (uploads) + TF-IDF
DATA_DIR = ROOT / "backend" / "data"

# optional Postgres (Neon) + pgvector: set DATABASE_URL or put the URL in database_url.txt (git-ignored)
from db import DB, LocalKV, database_url
DBX = None
if database_url(ROOT):
    try:
        DBX = DB(database_url(ROOT))
        print("[db] connected: documents, vectors and app data are stored in Postgres (pgvector)", flush=True)
    except Exception as _e:
        print(f"[db] NOT available, using local files only: {_e}", flush=True)
STORE = Store(DATA_DIR, seed_dir=ROOT / "docs", db=DBX)   # first run imports the 10 original PDFs from docs/
KV = DBX or LocalKV(DATA_DIR)                             # small records: reminders, pins, sessions...

from auth import Auth
AUTH = Auth(KV)
LOW_CONFIDENCE = 0.62     # best semantic match below this = "low confidence" (bge-small cosine similarity)


def tfidf_scores(snap, query):
    vocab, idf, chunks = snap["vocab"], snap["index"]["idf"], snap["index"]["chunks"]
    counts = Counter(t for t in tokenize(query) if t in vocab)
    out = np.zeros(len(chunks), dtype=np.float32)
    if not counts:
        return out
    q = {vocab[t]: (1 + math.log(c)) * idf[vocab[t]] for t, c in counts.items()}
    norm = math.sqrt(sum(v * v for v in q.values()))
    for i, ch in enumerate(chunks):
        s = 0.0
        for t, w in zip(ch["terms"], ch["weights"]):
            qw = q.get(t)
            if qw:
                s += qw * w
        out[i] = s / norm
    return out


# ---------------------------------------------------------------- embeddings (model loaded in background)
EMB = {"state": "warming", "model": None, "error": None}


def load_embeddings():
    if EMB.get("state") == "ready":
        return
    t0 = time.time()
    try:
        from fastembed import TextEmbedding
        model = TextEmbedding(EMBED_MODEL)
        EMB.update(state="ready", model=model)
        STORE.model_name, STORE.embedder = EMBED_MODEL, model
        STORE.request_rebuild()                      # embeds any document not cached yet
        print(f"[embeddings] model ready in {time.time() - t0:.1f}s", flush=True)
    except Exception as e:  # keep serving with TF-IDF only
        EMB.update(state="failed", error=str(e))
        print(f"[embeddings] FAILED, using keyword search only: {e}", flush=True)


def ranks(scores):
    order = np.argsort(-scores)
    r = np.empty(len(scores), dtype=np.int32)
    r[order] = np.arange(1, len(scores) + 1)
    return r


def norm_code(x):
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def visible_positions(snap, user):
    return [d["id"] for d in snap["index"]["docs"] if AUTH.can_see(d.get("groups", []), user)]


def machine_positions(snap, machine):
    m = norm_code(machine)
    if not m:
        return []
    return [d["id"] for d in snap["index"]["docs"]
            if any(norm_code(t) and (norm_code(t) == m or (len(m) >= 4 and (m in norm_code(t) or norm_code(t) in m)))
                   for t in d.get("tags", []))]


def _hit(snap, i, score, kwv, semv):
    c = snap["index"]["chunks"][i]
    d = snap["index"]["docs"][c["doc"]]
    return {"i": i, "score": score, "kw": kwv, "sem": semv,          # the passage itself, from the SAME snapshot
            "doc": c["doc"], "page": c["page"], "uid": d["uid"], "title": d["title"], "text": c["text"]}


def run_search(query, k=8, machine="", user=None):
    """Hybrid search used by the glasses, MCP, workflows. Respects permissions and the machine filter."""
    snap = STORE.snapshot                                  # one consistent version of the index
    chunks = snap["index"]["chunks"]
    n = len(chunks)
    out = {"version": snap["version"], "hits": [], "machine": machine or "", "machineMatched": None, "conf": None}
    if not query or not n:
        return dict(out, mode="none")
    allowed = set(visible_positions(snap, user))
    if machine:
        mp = set(machine_positions(snap, machine)) & allowed
        out["machineMatched"] = bool(mp)
        if mp:
            allowed = mp
    doc_of = np.fromiter((c["doc"] for c in chunks), dtype=np.int32, count=n)
    mask = np.isin(doc_of, np.fromiter(allowed, dtype=np.int32)) if allowed else np.zeros(n, bool)
    kw = tfidf_scores(snap, query)
    kw[~mask] = 0
    matrix = snap["matrix"]
    if EMB["state"] != "ready" or matrix is None or matrix.shape[0] != n:
        order = [int(i) for i in np.argsort(-kw)[:k] if kw[i] > 0]
        return dict(out, mode="keyword", embeddings=EMB["state"],
                    hits=[_hit(snap, i, float(kw[i]), float(kw[i]), None) for i in order])
    q = np.array(list(EMB["model"].query_embed([query]))[0], dtype=np.float32)
    q /= np.linalg.norm(q)
    engine = "memory"
    sem = None
    if DBX is not None:                                    # semantic search inside Postgres (pgvector)
        try:
            uids = [snap["uids"][p] for p in allowed]
            sem = np.full(n, -1.0, dtype=np.float32)
            for uid, ordn, sc in DBX.semantic(q, EMBED_MODEL, max(60, k * 6), uids):
                i = snap["keyidx"].get((uid, ordn))
                if i is not None:
                    sem[i] = sc
            engine = "pgvector"
        except Exception as e:
            print(f"[db] pgvector search failed, using memory: {e}", flush=True)
            sem = None
    if sem is None:
        sem = matrix @ q
    sem[~mask] = -1.0
    rrf = W_SEMANTIC / (RRF_K + ranks(sem)) + W_KEYWORD * (kw > 0) / (RRF_K + ranks(kw))
    rrf[~mask] = -1.0
    order = [int(i) for i in np.argsort(-rrf)[:k] if rrf[i] > 0]
    conf = float(max((sem[i] for i in order), default=0.0))
    return dict(out, mode="hybrid", engine=engine, model=EMBED_MODEL, conf=round(conf, 3),
                lowConfidence=conf < LOW_CONFIDENCE,
                hits=[_hit(snap, i, float(rrf[i]), float(kw[i]), float(sem[i])) for i in order])


@app.post("/api/search")
def search(req: Request, _body=JSONB):
    body = (_body if _body is not None else {})
    return run_search(str(body.get("query", "")).strip(), int(body.get("k", 8)),
                      str(body.get("machine", "") or "")[:80], AUTH.user_of(req))


# ---------------------------------------------------------------- uploads + dynamic index for the VR app
NO_CACHE = {"Cache-Control": "no-store"}


@app.get("/theme/{name}")
def ui_asset(name: str):
    """Shared theme / navigation for the web pages (vr-theme.css, vr-shell.js)."""
    f = ROOT / "backend" / "static" / name
    if not re.fullmatch(r"[\w.-]+\.(css|js|svg)", name) or not f.is_file():
        return JSONResponse({"error": "not found"}, 404)
    return FileResponse(f, headers={"Cache-Control": "no-cache"})


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


@app.get("/rag/index.json")
def rag_index(req: Request):
    snap = STORE.snapshot
    if not AUTH.enabled:
        return Response(json.dumps(snap["index"], ensure_ascii=False), media_type="application/json", headers=NO_CACHE)
    user = AUTH.user_of(req)
    ok = set(visible_positions(snap, user))
    key = (snap["version"], tuple(sorted(ok)))
    body = _ix_cache.get(key)
    if body is None:                # same doc positions for everyone; hidden docs are emptied, not removed
        ix = dict(snap["index"])
        ix["docs"] = [d if d["id"] in ok else {"id": d["id"], "title": "(restricted)", "file": "", "pages": 0,
                                                "type": "", "uid": "", "tags": [], "figs": {}} for d in snap["index"]["docs"]]
        # same length as the server's list: search results are chunk positions, so hidden chunks are blanked, not removed
        ix["chunks"] = [c if c["doc"] in ok else {"doc": c["doc"], "page": 0, "text": "", "terms": [], "weights": []}
                        for c in snap["index"]["chunks"]]
        body = json.dumps(ix, ensure_ascii=False)
        _ix_cache.clear(); _ix_cache[key] = body
    return Response(body, media_type="application/json", headers=NO_CACHE)


def _doc_visible(pos, req):
    docs = STORE.snapshot["index"]["docs"]
    return 0 <= pos < len(docs) and AUTH.can_see(docs[pos].get("groups", []), AUTH.user_of(req))


@app.get("/rag/figures/{pos}/{file}")
def rag_figure(pos: int, file: str, req: Request):
    uids = STORE.snapshot["uids"]
    p = STORE.figure_image(uids[pos], file) if 0 <= pos < len(uids) and _doc_visible(pos, req) else None
    if not p:
        return JSONResponse({"error": "figure not found"}, 404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.get("/rag/pages/{name}")
def rag_page(name: str, req: Request):
    m = re.fullmatch(r"d(\d+)_p(\d+)\.jpg", name)
    p = STORE.page_image(int(m.group(1)), int(m.group(2))) if m and _doc_visible(int(m.group(1)), req) else None
    if not p:
        return JSONResponse({"error": "page not found"}, 404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


# ---------------------------------------------------------------- LLM provider (Groq / Grok)
KEY_FILE = ROOT / "grok_api_key.txt"
MODEL_FILE = ROOT / "grok_model.txt"
_cache = {"chat": None, "stt": None}


def get_key():
    for env in ("XAI_API_KEY", "GROQ_API_KEY"):
        if os.environ.get(env):
            return os.environ[env].strip()
    if KEY_FILE.exists():
        line = KEY_FILE.read_text(encoding="utf-8-sig").splitlines()[:1]
        if line and re.match(r"^(xai-|gsk_)", line[0].strip()):
            return line[0].strip()
    return None


def provider():
    k = get_key()
    if not k:
        return None
    if k.startswith("gsk_"):
        return {"name": "groq", "base": "https://api.groq.com/openai/v1"}
    return {"name": "grok", "base": "https://api.x.ai/v1"}


_MODELS = {"ids": None, "t": 0.0}


def list_models():
    """The account's model list, fetched once per 10 minutes (chat, voice and photo all need it)."""
    if _MODELS["ids"] is not None and time.time() - _MODELS["t"] < 600:
        return _MODELS["ids"]
    p = provider()
    r = HTTP.get(f"{p['base']}/models", headers={"Authorization": f"Bearer {get_key()}"}, timeout=20)
    r.raise_for_status()
    _MODELS.update(ids=[m["id"] for m in r.json().get("data", [])], t=time.time())
    return _MODELS["ids"]


def chat_model():
    if MODEL_FILE.exists() and MODEL_FILE.read_text().strip():
        return MODEL_FILE.read_text().strip().splitlines()[0]
    p = provider()
    if not p:
        return ""
    if p["name"] == "grok":
        return "grok-4.7"
    if not _cache["chat"]:
        prefer = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
        try:
            ids = list_models()
            _cache["chat"] = next((m for m in prefer if m in ids), None) or next(
                (m for m in ids if not re.search("whisper|tts|guard|embed", m)), prefer[0])
        except Exception:
            return prefer[0]
    return _cache["chat"]


def stt_model():
    if not _cache["stt"]:
        try:
            ids = list_models()
            _cache["stt"] = next((m for m in ["whisper-large-v3-turbo", "whisper-large-v3"] if m in ids), None) or next(
                (m for m in ids if "whisper" in m), "whisper-large-v3-turbo")
        except Exception:
            return "whisper-large-v3-turbo"
    return _cache["stt"]


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
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


def fast_model():
    """A smaller, faster model for quick jobs (voice-command routing, translation): ~3x faster than the main one."""
    if _cache.get("fast") is None:
        try:
            ids = list_models()
            _cache["fast"] = next((m for m in ["openai/gpt-oss-20b", "llama-3.1-8b-instant"] if m in ids), "") or ""
        except Exception:
            return chat_model()
    return _cache["fast"] or chat_model()


def _chat_payload(messages, max_tokens=700, stream=False, model=None):
    model = model or chat_model()
    payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "low"        # short hidden "thinking": ~2x faster first word, same answers
    if stream:
        payload["stream"] = True
    return payload


_ANSWERS = OrderedDict()                            # same question + same excerpts -> instant answer (LRU, 200)


def _cache_key(messages):
    return hashlib.sha1(json.dumps([chat_model(), messages], sort_keys=True).encode()).hexdigest()


def _remember(key, text):
    if text and not text.startswith("AI error"):
        _ANSWERS[key] = text
        _ANSWERS.move_to_end(key)
        while len(_ANSWERS) > 200:
            _ANSWERS.popitem(last=False)


@app.post("/api/grok")
def grok(_body=JSONB):
    p = provider()
    if not p:
        return JSONResponse({"error": "No API key. Put a Groq (gsk_...) or xAI (xai-...) key in grok_api_key.txt"}, 400)
    messages = (_body or {}).get("messages", [])
    key = _cache_key(messages)
    if key in _ANSWERS:
        return {"choices": [{"message": {"role": "assistant", "content": _ANSWERS[key]}}], "cached": True}
    try:
        r = HTTP.post(f"{p['base']}/chat/completions", json=_chat_payload(messages),
                      headers={"Authorization": f"Bearer {get_key()}"}, timeout=90)
    except Exception as e:
        return JSONResponse({"error": f"{p['name']} API: {e}"}, 502)
    if r.status_code != 200:
        return JSONResponse({"error": f"{p['name']} API: HTTP {r.status_code} - {r.text[:500]}"}, 502)
    j = r.json()
    try:
        _remember(key, j["choices"][0]["message"]["content"] or "")
    except Exception:
        pass
    return j


@app.post("/api/grok/stream")
def grok_stream(_body=JSONB):
    """Same as /api/grok but streams the answer as plain text while it is written (first words in ~0.5 s)."""
    p = provider()
    if not p:
        return JSONResponse({"error": "No API key in grok_api_key.txt"}, 400)
    messages = (_body or {}).get("messages", [])
    key = _cache_key(messages)
    if key in _ANSWERS:
        return Response(_ANSWERS[key], media_type="text/plain; charset=utf-8", headers={"X-Cache": "hit"})
    try:
        r = HTTP.post(f"{p['base']}/chat/completions", json=_chat_payload(messages, stream=True), stream=True,
                      headers={"Authorization": f"Bearer {get_key()}"}, timeout=90)
    except Exception as e:
        return JSONResponse({"error": f"{p['name']} API: {e}"}, 502)
    if r.status_code != 200:
        txt = r.text[:500]
        r.close()
        return JSONResponse({"error": f"{p['name']} API: HTTP {r.status_code} - {txt}"}, 502)

    r.encoding = "utf-8"            # SSE has no charset: without this, ’ “ ₹ and Hindi text arrive garbled

    def gen():
        out = []
        try:
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    piece = (json.loads(data)["choices"][0].get("delta") or {}).get("content") or ""
                except Exception:
                    continue
                if piece:
                    out.append(piece)
                    yield piece
        finally:
            r.close()
            _remember(key, "".join(out))
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/stt")
async def stt(req: Request):
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
        r = await run_in_threadpool(HTTP.post, f"{p['base']}/audio/transcriptions", headers={"Authorization": f"Bearer {get_key()}"},
                          files={"file": (f"speech.{ext}", audio, mime)},
                          data=dict({"model": stt_model(), "response_format": "json", "temperature": "0"},
                                    **({"language": lang} if lang else {}), **({"prompt": hint} if hint else {})), timeout=60)
    except Exception as e:
        return JSONResponse({"error": f"groq STT: {e}"}, 502)
    if r.status_code != 200:
        return JSONResponse({"error": f"groq STT: HTTP {r.status_code} - {r.text[:300]}"}, 502)
    return r.json()


# ---------------------------------------------------------------- ProcOps (question log, heatmap, AI redline, versions)
def llm_complete(messages, max_tokens=1200, fast=False):
    """Server-side chat call used by ProcOps. Returns the reply text or raises."""
    p = provider()
    if not p:
        raise RuntimeError("no API key in grok_api_key.txt")
    payload = _chat_payload(messages, max_tokens, model=fast_model() if fast else None)
    r = HTTP.post(f"{p['base']}/chat/completions", json=payload,
                      headers={"Authorization": f"Bearer {get_key()}"}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"{p['name']} API: HTTP {r.status_code} - {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"] or ""


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
_register("assistant", lambda: assistant.register(app, KV, llm_complete, CALS))
CONNECTORS = _register("connectors", lambda: connectors.register(app, KV, AUTH, STORE)) or {"send": None, "get": lambda c: None}
_register("workflows", lambda: workflows.register(app, KV, llm_complete, run_search, STORE, CONNECTORS, ROOT, AUTH))
_register("expert", lambda: expert.register(app, ROOT, AUTH))
_register("mcp", lambda: mcp_server.register(app, STORE, run_search, llm_complete, AUTH))
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
            "users": [AUTH.public(u) for u in AUTH.users()] if AUTH.is_admin(req) else []}


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


try:
    import vision
    vision.register(app, provider, get_key, list_models, ROOT)   # "scan, then ask" (photo questions)
except Exception as _e:
    print(f"[vision] disabled: {_e}", flush=True)


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
    threading.Thread(target=load_embeddings, daemon=True).start()
    if "--no-browser" not in sys.argv and not env_port:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}/")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")

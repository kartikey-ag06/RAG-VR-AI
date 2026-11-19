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
import json
import math
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from collections import Counter
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import numpy as np
import requests
import uvicorn
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
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
STORE = Store(DATA_DIR, seed_dir=ROOT / "docs")   # first run imports the 10 original PDFs from docs/


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


@app.post("/api/search")
async def search(req: Request):
    body = await req.json()
    query, k = str(body.get("query", "")).strip(), int(body.get("k", 8))
    snap = STORE.snapshot                                  # one consistent version of the index
    n = len(snap["index"]["chunks"])
    if not query or not n:
        return {"mode": "none", "version": snap["version"], "hits": []}
    kw = tfidf_scores(snap, query)
    matrix = snap["matrix"]
    if EMB["state"] != "ready" or matrix is None or matrix.shape[0] != n:
        order = [int(i) for i in np.argsort(-kw)[:k] if kw[i] > 0]
        return {"mode": "keyword", "version": snap["version"], "embeddings": EMB["state"],
                "hits": [{"i": i, "score": float(kw[i]), "kw": float(kw[i]), "sem": None} for i in order]}
    q = np.array(list(EMB["model"].query_embed([query]))[0], dtype=np.float32)
    sem = matrix @ (q / np.linalg.norm(q))
    rrf = W_SEMANTIC / (RRF_K + ranks(sem)) + W_KEYWORD * (kw > 0) / (RRF_K + ranks(kw))
    order = [int(i) for i in np.argsort(-rrf)[:k]]
    return {"mode": "hybrid", "model": EMBED_MODEL, "version": snap["version"],
            "hits": [{"i": i, "score": float(rrf[i]), "sem": float(sem[i]), "kw": float(kw[i])} for i in order]}


# ---------------------------------------------------------------- uploads + dynamic index for the VR app
NO_CACHE = {"Cache-Control": "no-store"}


@app.get("/upload")
def upload_page():
    return FileResponse(ROOT / "backend" / "static" / "upload.html", headers=NO_CACHE)


@app.get("/api/files")
def files_list():
    snap = STORE.snapshot
    return {"files": STORE.list(), "version": snap["version"], "state": STORE.state,
            "chunks": len(snap["index"]["chunks"]), "embeddings": EMB["state"]}


@app.post("/api/files")
async def files_upload(files: list[UploadFile] = File(...)):
    added, errors = [], []
    for f in files:
        try:
            added.append(STORE.add(f.filename or "file", await f.read()))
        except ValueError as e:
            errors.append(str(e))
    return JSONResponse({"added": added, "errors": errors}, status_code=200 if added or not errors else 400)


@app.delete("/api/files/{uid}")
def files_delete(uid: str):
    if not re.fullmatch(r"[0-9a-f]{12}", uid) or not STORE.delete(uid):
        return JSONResponse({"error": "not found"}, 404)
    return {"deleted": uid}


@app.get("/api/index/version")
def index_version():
    snap = STORE.snapshot
    return {"version": snap["version"], "docs": len(snap["index"]["docs"]),
            "chunks": len(snap["index"]["chunks"]), "state": STORE.state}


@app.get("/rag/index.json")
def rag_index():
    return Response(json.dumps(STORE.snapshot["index"], ensure_ascii=False), media_type="application/json",
                    headers=NO_CACHE)


@app.get("/rag/pages/{name}")
def rag_page(name: str):
    m = re.fullmatch(r"d(\d+)_p(\d+)\.jpg", name)
    p = STORE.page_image(int(m.group(1)), int(m.group(2))) if m else None
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


def list_models():
    p = provider()
    r = requests.get(f"{p['base']}/models", headers={"Authorization": f"Bearer {get_key()}"}, timeout=20)
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])]


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
            "embedModel": EMBED_MODEL, "backend": "python"}


@app.get("/api/grok/models")
def models():
    try:
        return {"data": [{"id": m} for m in list_models()]}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/grok")
async def grok(req: Request):
    p = provider()
    if not p:
        return JSONResponse({"error": "No API key. Put a Groq (gsk_...) or xAI (xai-...) key in grok_api_key.txt"}, 400)
    body = await req.json()
    payload = {"model": chat_model(), "messages": body.get("messages", []), "temperature": 0.2, "max_tokens": 600}
    try:
        r = requests.post(f"{p['base']}/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {get_key()}"}, timeout=90)
    except Exception as e:
        return JSONResponse({"error": f"{p['name']} API: {e}"}, 502)
    if r.status_code != 200:
        return JSONResponse({"error": f"{p['name']} API: HTTP {r.status_code} - {r.text[:500]}"}, 502)
    return r.json()


@app.post("/api/stt")
async def stt(req: Request):
    p = provider()
    if not p or p["name"] != "groq":
        return JSONResponse({"error": "Voice transcription needs a Groq key (gsk_...)"}, 400)
    audio = await req.body()
    if len(audio) < 800:
        return JSONResponse({"error": "Recording too short - hold the button a bit longer"}, 400)
    mime = (req.headers.get("content-type") or "audio/webm").split(";")[0].strip()
    ext = {"audio/ogg": "ogg", "audio/mp4": "m4a", "audio/wav": "wav", "audio/mpeg": "mp3"}.get(mime, "webm")
    try:
        r = requests.post(f"{p['base']}/audio/transcriptions", headers={"Authorization": f"Bearer {get_key()}"},
                          files={"file": (f"speech.{ext}", audio, mime)},
                          data={"model": stt_model(), "response_format": "json", "temperature": "0"}, timeout=60)
    except Exception as e:
        return JSONResponse({"error": f"groq STT: {e}"}, 502)
    if r.status_code != 200:
        return JSONResponse({"error": f"groq STT: HTTP {r.status_code} - {r.text[:300]}"}, 502)
    return r.json()


# ---------------------------------------------------------------- ProcOps (question log, heatmap, AI redline, versions)
def llm_complete(messages, max_tokens=1200):
    """Server-side chat call used by ProcOps. Returns the reply text or raises."""
    p = provider()
    if not p:
        raise RuntimeError("no API key in grok_api_key.txt")
    model = chat_model()
    payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "low"
    r = requests.post(f"{p['base']}/chat/completions", json=payload,
                      headers={"Authorization": f"Bearer {get_key()}"}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"{p['name']} API: HTTP {r.status_code} - {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"] or ""


try:
    import procops
    procops.register(app, STORE, llm_complete, ROOT)
except Exception as _e:  # ProcOps must never stop the main app from starting
    print(f"[procops] disabled: {_e}", flush=True)


try:
    import vision
    vision.register(app, provider, get_key, list_models, ROOT)   # "scan, then ask" (photo questions)
except Exception as _e:
    print(f"[vision] disabled: {_e}", flush=True)


@app.post("/api/server/stop")
def stop():
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

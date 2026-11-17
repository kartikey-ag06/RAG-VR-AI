"""
VR_RAG backend (FastAPI) - replaces server.ps1 and adds EMBEDDINGS + HYBRID SEARCH.

What it does
  1. Serves the VR app (glasses_app/dist) on http://localhost:8765
  2. /api/search  : hybrid search = embeddings (meaning) + TF-IDF (exact words), merged with
                    Reciprocal Rank Fusion (RRF)
  3. /api/grok    : AI answers via Groq (gsk_...) or Grok/xAI (xai-...); the key never reaches the browser
  4. /api/stt     : voice -> text via Groq Whisper
  5. /api/server/stop : stop the server (localhost only)

Embeddings
  Model: BAAI/bge-small-en-v1.5 via fastembed (ONNX, CPU, no PyTorch). Downloaded once (~70 MB).
  The 1,136 PDF chunks are embedded once at startup and cached in backend/cache/.

Run:  START_VR_RAG.bat   (or: python backend/app.py)
"""
import hashlib
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

import numpy as np
import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / ("web" if "--app=web" in sys.argv else "glasses_app/dist")
INDEX_FILE = APP_DIR / "rag" / "index.json"
CACHE_DIR = ROOT / "backend" / "cache"
EMBED_MODEL = os.environ.get("VR_RAG_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
RRF_K = 60            # standard RRF constant
W_SEMANTIC = 1.0      # weight of the embedding ranking
W_KEYWORD = 0.8       # weight of the TF-IDF ranking (exact terms like "15HB", "13,00,00,000")

app = FastAPI(title="VR_RAG backend")

# ---------------------------------------------------------------- index + TF-IDF
INDEX = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
CHUNKS = INDEX["chunks"]
VOCAB = {t: i for i, t in enumerate(INDEX["vocab"])}
IDF = INDEX["idf"]
STOP = set("""a an the and or of to in on for by with at from as is are was were be been being this that these those it its
which who whom whose what when where why how not no nor but if then than so such into upon under over above below
shall will would should may might can could must do does did done have has had having he she they them his her their
i we you me us our your said also any all each other same per vide hereinafter thereof therein herein whereas
page sebi noticee noticees order""".split())  # must match rag_pipeline/build_index.py


def tokenize(text):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 and t not in STOP]


def tfidf_scores(query):
    counts = Counter(t for t in tokenize(query) if t in VOCAB)
    if not counts:
        return np.zeros(len(CHUNKS), dtype=np.float32)
    q = {VOCAB[t]: (1 + math.log(c)) * IDF[VOCAB[t]] for t, c in counts.items()}
    norm = math.sqrt(sum(v * v for v in q.values()))
    out = np.zeros(len(CHUNKS), dtype=np.float32)
    for i, ch in enumerate(CHUNKS):
        s = 0.0
        for t, w in zip(ch["terms"], ch["weights"]):
            qw = q.get(t)
            if qw:
                s += qw * w
        out[i] = s / norm
    return out


# ---------------------------------------------------------------- embeddings (loaded in background)
EMB = {"state": "warming", "model": None, "matrix": None, "error": None, "secs": None}


def load_embeddings():
    t0 = time.time()
    try:
        from fastembed import TextEmbedding
        model = TextEmbedding(EMBED_MODEL)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(INDEX_FILE.read_bytes()).hexdigest()[:12]
        cache = CACHE_DIR / f"emb_{EMBED_MODEL.replace('/', '_')}_{digest}.npy"
        if cache.exists():
            matrix = np.load(cache)
        else:
            print(f"[embeddings] embedding {len(CHUNKS)} chunks with {EMBED_MODEL} (one time)...", flush=True)
            matrix = np.array(list(model.passage_embed([c["text"] for c in CHUNKS], batch_size=32)), dtype=np.float32)
            matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
            np.save(cache, matrix)
        EMB.update(state="ready", model=model, matrix=matrix, secs=round(time.time() - t0, 1))
        print(f"[embeddings] ready: {matrix.shape[0]} x {matrix.shape[1]} in {EMB['secs']}s", flush=True)
    except Exception as e:  # keep serving with TF-IDF only
        EMB.update(state="failed", error=str(e))
        print(f"[embeddings] FAILED, using keyword search only: {e}", flush=True)


def semantic_scores(query):
    q = np.array(list(EMB["model"].query_embed([query]))[0], dtype=np.float32)
    q /= np.linalg.norm(q)
    return EMB["matrix"] @ q


def ranks(scores):
    order = np.argsort(-scores)
    r = np.empty(len(scores), dtype=np.int32)
    r[order] = np.arange(1, len(scores) + 1)
    return r


@app.post("/api/search")
async def search(req: Request):
    body = await req.json()
    query, k = str(body.get("query", "")).strip(), int(body.get("k", 8))
    if not query:
        return {"mode": "none", "hits": []}
    kw = tfidf_scores(query)
    if EMB["state"] != "ready":
        order = [int(i) for i in np.argsort(-kw)[:k] if kw[i] > 0]
        return {"mode": "keyword", "embeddings": EMB["state"],
                "hits": [{"i": i, "score": float(kw[i]), "kw": float(kw[i]), "sem": None} for i in order]}
    sem = semantic_scores(query)
    rrf = W_SEMANTIC / (RRF_K + ranks(sem)) + W_KEYWORD * (kw > 0) / (RRF_K + ranks(kw))
    order = [int(i) for i in np.argsort(-rrf)[:k]]
    return {"mode": "hybrid", "model": EMBED_MODEL,
            "hits": [{"i": i, "score": float(rrf[i]), "sem": float(sem[i]), "kw": float(kw[i])} for i in order]}


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


@app.post("/api/server/stop")
def stop():
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"stopping": True}


# static app last, so /api/* wins
app.mount("/", StaticFiles(directory=str(APP_DIR), html=True), name="app")


def free_port(start=8765):
    for p in range(start, start + 10):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise SystemExit("No free port between 8765 and 8774")


if __name__ == "__main__":
    port = free_port()
    p = provider()
    print(f"VR_RAG (python backend) running at http://localhost:{port}/  (close this window to stop)")
    print(f"AI: {'ON - ' + p['name'] if p else 'OFF - put a Groq key in grok_api_key.txt'}")
    print(f"Search: hybrid (TF-IDF + {EMBED_MODEL}) - embeddings load in the background")
    threading.Thread(target=load_embeddings, daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}/")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

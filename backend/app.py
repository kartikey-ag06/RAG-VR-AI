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
import asyncio
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
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles

from indexer import Store, tokenize

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / ("web" if "--app=web" in sys.argv else "glasses_app/dist")
EMBED_MODEL = os.environ.get("VR_RAG_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
RRF_K = 60            # standard RRF constant
W_SEMANTIC = 1.0      # weight of the embedding ranking
W_KEYWORD = 0.8       # weight of the TF-IDF ranking (exact terms like "15HB", "13,00,00,000")

def _load_dotenv():
    """Reads KEY=VALUE lines from .env (project root or backend/) into os.environ (existing variables win)."""
    for f in (ROOT / ".env", BACKEND_DIR / ".env"):
        if f.exists():
            for line in f.read_text(encoding="utf-8-sig").splitlines():
                m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
                if m and not line.lstrip().startswith("#"):
                    os.environ.setdefault(m.group(1), m.group(2).strip("\"'"))


_load_dotenv()
MIN_RERANK_CONFIDENCE = float(os.environ.get("VR_RAG_MIN_CONFIDENCE", "0.20"))   # below this the question is treated as not covered by the documents
# The cross-encoder alone is noisy for short keyword queries ("insider trading" scores 0.14 although the orders cover it),
# so a question is only rejected when the embedding similarity is also low. Measured: off-topic questions reach <= 0.65,
# short on-topic queries >= 0.665. The margin is small, so the gate leans lenient: the LLM still answers "GENERAL" itself
# when the excerpts do not help, whereas a wrongly rejected question would lose its citations.
MIN_SEMANTIC_SIM = float(os.environ.get("VR_RAG_MIN_SEMANTIC", "0.65"))
RERANK_CANDIDATES = 20   # hybrid search keeps this many, the cross-encoder reranker cuts them to k
RERANK_MODEL = os.environ.get("VR_RAG_RERANK_MODEL", "ms-marco-TinyBERT-L-2-v2")

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
    threading.Thread(target=get_ranker, daemon=True).start()      # download/load the reranker before the first question

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


# ---------------------------------------------------------------- cross-encoder reranker (FlashRank, ONNX, CPU)
RERANK = {"state": "idle", "ranker": None, "error": None}
_rerank_lock = threading.Lock()


def get_ranker():
    with _rerank_lock:
        if RERANK["state"] == "idle":
            try:
                from flashrank import Ranker
                RERANK.update(ranker=Ranker(model_name=RERANK_MODEL, cache_dir=str(ROOT / "backend" / "cache" / "rerank")),
                              state="ready")
            except Exception as e:
                RERANK.update(state="failed", error=str(e))
                print(f"[rerank] disabled, using RRF order only: {e}", flush=True)
        return RERANK["ranker"]


def rerank(query, hits, chunks, k):
    """Re-scores the RRF candidates with a cross-encoder. Returns the top k hits, or None if unavailable."""
    ranker = get_ranker()
    if ranker is None or len(hits) < 2:
        return None
    try:
        from flashrank import RerankRequest
        dtitles = [d["title"] for d in STORE.snapshot["index"]["docs"]]
        passages = [{"id": j, "text": dtitles[chunks[h["i"]]["doc"]] + ". " + chunks[h["i"]]["text"]} for j, h in enumerate(hits)]
        out = ranker.rerank(RerankRequest(query=query, passages=passages))
        res = []
        for p in out[:k]:
            h = dict(hits[int(p["id"])])
            h["rrf"] = h["score"]
            h["score"] = float(p["score"])
            res.append(h)
        return res
    except Exception as e:
        print(f"[rerank] failed, using RRF order: {e}", flush=True)
        return None


@app.post("/api/search")
async def search(req: Request):
    body = await req.json()
    query, k = str(body.get("query", "")).strip(), int(body.get("k", 8))
    snap = STORE.snapshot                                  # one consistent version of the index
    n = len(snap["index"]["chunks"])
    if not query or not n:
        return {"mode": "none", "version": snap["version"], "hits": []}
    kw = tfidf_scores(snap, query)
    matrix, col = snap["matrix"], snap.get("qdrant")
    if EMB["state"] != "ready" or (col is None and (matrix is None or matrix.shape[0] != n)):
        order = [int(i) for i in np.argsort(-kw)[:k] if kw[i] > 0]
        return {"mode": "keyword", "version": snap["version"], "embeddings": EMB["state"],
                "hits": [{"i": i, "score": float(kw[i]), "kw": float(kw[i]), "sem": None} for i in order]}
    q = np.array(list(EMB["model"].query_embed([query]))[0], dtype=np.float32)
    q = q / np.linalg.norm(q)
    if col is not None:                                   # Qdrant nearest-neighbour search (cosine)
        pos = snap["idpos"]
        res = await asyncio.to_thread(lambda: col.query_points(snap["qname"], query=q.tolist(), limit=min(n, 200)).points)
        sem = np.full(n, -1.0, dtype=np.float32)          # chunks outside Qdrant's top 200 rank behind all returned ones
        for p in res:
            j = pos.get(str(p.id))
            if j is not None:
                sem[j] = p.score
    else:
        sem = matrix @ q
    rrf = W_SEMANTIC / (RRF_K + ranks(sem)) + W_KEYWORD * (kw > 0) / (RRF_K + ranks(kw))
    order = [int(i) for i in np.argsort(-rrf)[:max(k, RERANK_CANDIDATES)]]
    hits = [{"i": i, "score": float(rrf[i]), "sem": float(sem[i]), "kw": float(kw[i])} for i in order]
    chunks_ = snap["index"]["chunks"]
    reranked = await asyncio.to_thread(rerank, query, hits, chunks_, k)
    if reranked is not None:
        top = reranked[0]["score"] if reranked else 0.0
        level = "high" if top >= 0.75 else "medium" if top >= 0.35 else "low"
        docs = snap["index"]["docs"]
        for h in reranked:                                # provenance of every citation
            c = chunks_[h["i"]]
            d = docs[c["doc"]]
            h["prov"] = {"doc_id": d["id"], "uid": d["uid"], "title": d["title"], "sha256": d.get("sha256", ""),
                         "page": c["page"], "rev": d.get("rev", "1.0"), "date": d.get("date", "")}
        top_sem = max(h["sem"] for h in hits)            # best embedding cosine among the candidates
        if top < MIN_RERANK_CONFIDENCE and top_sem < MIN_SEMANTIC_SIM:   # both signals say "not in the documents"
            return {"mode": "unconfident", "version": snap["version"], "hits": [], "confidence": top, "level": "low",
                    "top_sem": top_sem, "top_kw": max(h["kw"] for h in hits),
                    "message": "No confident match found in the uploaded documents."}
        return {"mode": "hybrid+rerank", "model": EMBED_MODEL, "reranker": RERANK_MODEL,
                "version": snap["version"], "confidence": top, "level": level, "hits": reranked}
    return {"mode": "hybrid", "model": EMBED_MODEL, "version": snap["version"], "hits": hits[:k]}


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
    return JSONResponse({"added": added, "errors": errors, "jobs": [a["uid"] for a in added]},
                        status_code=200 if added or not errors else 400)


@app.get("/api/files/status/{job_id}")
def files_status(job_id: str):
    """Live progress of one upload (job id = document uid): stage + progress, e.g. rendering_pages 42/95."""
    e = STORE.doc_entry(job_id) if re.fullmatch(r"[0-9a-f]{12}", job_id) else None
    if not e:
        return JSONResponse({"error": "unknown job"}, 404)
    return {"job_id": job_id, "name": e["name"], "status": e["status"], "stage": e.get("stage", "done"),
            "progress": e.get("progress", "done"), "pages": e["pages"], "error": e["error"]}


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
            "embedModel": EMBED_MODEL, "vectorStore": "qdrant" if STORE.snapshot.get("qdrant") is not None else "numpy",
            "backend": "python", "tts": True, "stream": True,
            "reranker": RERANK_MODEL}


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


# ---------------------------------------------------------------- streaming answers (SSE) + text-to-speech
@app.post("/api/grok/stream")
async def grok_stream(req: Request):
    """Server-Sent Events: relays the model's tokens as `data: {"token": "..."}` lines, ends with `data: [DONE]`."""
    p = provider()
    if not p:
        return JSONResponse({"error": "No API key. Put a Groq (gsk_...) or xAI (xai-...) key in grok_api_key.txt"}, 400)
    body = await req.json()
    model = chat_model()
    payload = {"model": model, "messages": body.get("messages", []), "temperature": 0.2, "max_tokens": 600,
               "stream": True}
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "low"

    def gen():
        try:
            with requests.post(f"{p['base']}/chat/completions", json=payload, stream=True, timeout=90,
                               headers={"Authorization": f"Bearer {get_key()}"}) as r:
                if r.status_code != 200:
                    msg = f"{p['name']} API: HTTP {r.status_code} - {r.text[:300]}"
                    yield "data: " + json.dumps({"error": msg}) + "\n\n"
                    return
                r.encoding = "utf-8"
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        tok = json.loads(chunk)["choices"][0]["delta"].get("content")
                    except (ValueError, KeyError, IndexError):
                        continue
                    if tok:
                        yield "data: " + json.dumps({"token": tok}) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"error": f"{p['name']} API: {e}"}) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class TTSRequest(BaseModel):
    text: str
    voice: str = "en-US-ChristopherNeural"


@app.post("/api/tts")
async def text_to_speech(body: TTSRequest):
    """Neural text-to-speech (Microsoft Edge voices via edge-tts). Returns MP3. Needs internet access."""
    text = re.sub(r"\s+", " ", body.text).strip()[:1500]
    if not text:
        return JSONResponse({"error": "Empty text"}, 400)
    if not re.fullmatch(r"[A-Za-z]{2,3}-[A-Za-z]{2,4}-[A-Za-z]+Neural", body.voice):
        return JSONResponse({"error": "Invalid voice name"}, 400)
    try:
        import edge_tts
        audio = bytearray()
        async for chunk in edge_tts.Communicate(text, body.voice).stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
    except Exception as e:
        return JSONResponse({"error": f"TTS synthesis failed: {e}"}, 502)
    if not audio:
        return JSONResponse({"error": "TTS returned no audio"}, 502)
    return Response(bytes(audio), media_type="audio/mpeg", headers=NO_CACHE)


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
def stop(request: Request):
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost") or request.headers.get("x-forwarded-for"):
        return JSONResponse({"error": "Stopping the server is only allowed from the machine it runs on"}, 403)
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

# VR_RAG - SEBI x Reliance Industries orders on Meta VR Glasses (emulated)

## Quick start (no install needed)
1. Double-click **START_VR_RAG.bat**. It serves `glasses_app/dist` on http://localhost:8765 and opens the browser.
2. The page loads Meta's **IWER emulator** with the **Meta VR Glasses** device profile already built in. You don't need a Chrome extension.
3. Type a question in the box (top-left) and press Enter, or click a Sample button. Then click **Enter XR**.
4. In the emulator, choose **Gaze + Hands** input. Pinch = next PDF page, squeeze = next source.
   IWER keys: click Play mode to lock the mouse (Esc leaves it). Hold Left Shift + W/A/S/D to move your head.

`START_VR_RAG_QUEST_EXTENSION.bat` = the older version (Quest headset, needs the Immersive Web Emulator Chrome extension).

## Meta VR Glasses app (glasses_app/) - built with Immersive Web SDK (IWSDK) 1.1
- `iwsdk.config.json` sets `dev.emulator.device = "metaVRGlasses"`, `injectOnBuild: true` (emulator included in the build) and gaze + hand tracking.
- `src/index.js` creates the IWSDK World, then `src/rag-scene.js` adds the answer panel and the PDF page panel.
- `src/rag.js` is the same RAG engine (TF-IDF search + Ollama/Claude/OpenAI/offline answers).
- To change code and rebuild: install Node 22+, then `cd glasses_app`, `npm install`, then `npm run build` (or `npm run dev` for live editing).

## AI answers: Grok (xAI) or Groq
1. Create a key at https://console.x.ai (Grok, starts with `xai-`, paid) or https://console.groq.com/keys (Groq, starts with `gsk_`, has a free tier). The server detects which one you have from the prefix.
2. Open `grok_api_key.txt`, replace the whole line with your key, and save.
3. Restart `START_VR_RAG.bat`. The server window shows **Grok: ON**, and the app's status shows `LLM: grok`.
- Optional: put a different model name in `grok_model.txt`. The default is `grok-4.7`. Check https://docs.x.ai for current names.
- **Security:** the browser never sees the key. The app calls `/api/grok` on the local server, and `server.ps1` adds the key and forwards the request to `https://api.x.ai/v1/chat/completions`.
  Never commit `grok_api_key.txt` to git (it is listed in `.gitignore`).
- If Grok fails (wrong key, no credit, no internet), the app shows the error and falls back to the offline answer.

## Voice input
- Click **🎤 Speak** (click again to stop), or **hold V** while you talk (works inside XR too). The maximum is 15 seconds.
- Audio goes to `/api/stt` on the local server. The server forwards it to **Groq Whisper** (`whisper-large-v3-turbo`), and the text is asked automatically.
- With no Groq key, the app falls back to Chrome's built-in speech recognition (`en-IN`).
- The first time, Chrome asks for microphone permission for localhost. Click **Allow**.

## Embeddings + hybrid search (Python backend)
- `START_VR_RAG.bat` now starts **`backend/app.py`** (FastAPI). The first run creates `backend/.venv` and installs `backend/requirements.txt` once. That takes 2-5 minutes and about 250 MB.
- Search is **hybrid**:
  - **Embeddings** (`BAAI/bge-small-en-v1.5` via fastembed, ONNX on the CPU, no PyTorch) find text with the same **meaning**. "fine" matches "penalty".
  - **TF-IDF** finds **exact terms** such as "15HB" and "13,00,00,000".
  - The two rankings are merged with **Reciprocal Rank Fusion** (RRF): score = 1/(60 + rank_semantic) + 0.8/(60 + rank_keyword).
- Chunk embeddings are computed once and cached in `backend/cache/`. The cache is rebuilt automatically when `index.json` changes.
- The API stays the same (`/api/grok`, `/api/stt`, `/api/grok/status`) plus a new **`/api/search`**. If the Python backend is not running, the app falls back to TF-IDF in the browser.
- **Vector store: Qdrant** (embedded mode, no server needed, persistent in `backend/data/qdrant/`, cosine). It is created automatically on first start. On later starts existing vectors are reused: only new or changed chunks are embedded, and chunks of deleted documents are removed (no duplicates). Delete the `qdrant` folder to rebuild it. Only one process can open the folder at a time. If Qdrant can't load, the old `.npy` cache is used.
- After the hybrid search, a **FlashRank cross-encoder** (`ms-marco-TinyBERT-L-2-v2`, ONNX/CPU, downloaded once) re-scores the top 20 and keeps the best 8 (`mode: hybrid+rerank`). If it cannot load, plain RRF order is used.
- PDF text comes from `pymupdf4llm` (markdown with tables). Tables are never split mid-row. If PyMuPDF cannot load (Windows Application Control), `pypdfium2` is used (plain text).
- **Confidence:** the top reranker score is returned as `confidence`. A question is treated as "not in the documents" (`mode: unconfident`, no citations) only when the rerank score < 0.20 AND the embedding similarity < 0.65 (env `VR_RAG_MIN_CONFIDENCE`, `VR_RAG_MIN_SEMANTIC`).
- **Benchmark:** `python backend/test_rag_eval.py --answers` (use `backend/.venv/Scripts/python.exe`) runs golden questions against the running app.
- Streaming answers: `POST /api/grok/stream` (SSE). Spoken answers: `POST /api/tts` (needs internet).
- No Python? Use `START_VR_RAG_POWERSHELL.bat`, the old server with keyword search only.

## Upload documents (no more copying files into docs/)
- Open **http://localhost:8765/upload**, or click **＋ Upload docs** in the VR app. Drag and drop **PDF, Word (.docx) or TXT** files (up to 50 MB each).
- Files are stored on the server in `backend/data/files/` and processed automatically in the background:
  1. Text is extracted (PyMuPDF for PDF, python-docx for Word, plain text for TXT).
  2. Each page gets an image. PDF pages are rendered; Word and TXT text is drawn as A4 "pages", so the VR panel can show them too.
  3. The text is cut into chunks, then the keyword index is rebuilt and the embeddings for the new file are computed. Embeddings are cached per file.
- The VR app checks `/api/index/version` before every question and reloads the documents when they changed. No restart needed.
- Delete a document on the upload page and it disappears from search. Uploading the exact same file twice is rejected.
- First run: the 10 PDFs in `docs/` are imported automatically, so nothing changes for existing users.
- Scanned PDFs with no text layer are marked "No text found". OCR is not supported yet.

---

Ask a question, and the app finds the matching text in 10 SEBI enforcement orders.
It then shows the answer and the real PDF page on floating panels in VR.
It runs in Chrome using Meta's **Immersive Web Emulator** extension, so no headset is needed.

## Run it
1. Double-click **START_VR_RAG.bat**. It starts a small local web server on http://localhost:8765 and opens your browser.
2. In Chrome, install the **Immersive Web Emulator** extension (Chrome Web Store, by Meta) and pin it.
3. On http://localhost:8765, enable the emulator for the site (click its toolbar icon). Then reload the page.
4. Click **ENTER VR**. Use the emulator's panel, or DevTools > "WebXR" tab, to move the headset and controllers.
5. Type a question and press Enter, or click a Sample button.

Keys: Enter = ask, Tab = next source, Left/Right = PDF page. Typing works while in VR.
VR controllers: trigger = next page, grip = next source.

## How it works (RAG)
| Step | File | What happens |
|---|---|---|
| Ingest | `rag_pipeline/build_index.py` | PDF -> text per page -> ~120-word chunks -> TF-IDF vectors -> `web/rag/index.json`, plus a JPG of every page |
| Retrieve | `web/rag.js` | The question becomes a TF-IDF vector, and cosine similarity ranks the chunks. The top 5 are kept. |
| Generate | `web/rag.js` | Uses local **Ollama** if it is running (auto). Otherwise Claude/OpenAI (from `llm_config.json`), or an offline extractive answer. |
| Show | `web/app.js` | Three.js WebXR scene with an answer panel and a PDF page panel |

## AI answers
- **Free and local:** run `ollama pull llama3.2` and keep Ollama running. The app picks it up automatically.
- **Claude:** in `web/rag/llm_config.json`, set `"provider": "anthropic", "apiKey": "sk-ant-..."`. Use this for local testing only: the browser can read this file.

## Add more PDFs
Put them in `docs/`. Then run `pip install pymupdf` and `python rag_pipeline/build_index.py`.

## Why not double-click index.html?
WebXR needs a secure origin (https or http://localhost), and the browser blocks `fetch()` on `file://`.
That is why the .bat starts a local server.

## ProcOps (new) - every job is a test run of the manual
Open **http://localhost:8765/procops** while the app runs (or click **ProcOps** in the glasses panel).
1. **Run** - every question asked in the glasses is logged with the document + page it was answered from (page = procedure step).
2. **Detect** - the dashboard shows a friction heatmap: pages that cause the most questions are red. Questions no document answers appear under *Content gaps*.
3. **Fix** - click a red page → **Draft fix with AI** (Groq rewrites the page, with the questions as evidence and self-checks) → edit if needed → enter your name → **Approve & publish**. The page becomes v1.1: the glasses show the revised page and answer from it. The change log shows questions before → after the fix.
- **Reset demo** restores all original pages and archives the question log (nothing is deleted).
- Data lives in `backend/data/procops/` and `backend/data/revisions/` (git-ignored). Code: `backend/procops.py`, `backend/static/procops.html`.

## Scan, then ask (photo questions)
Click **📷 Scan** in the glasses panel → allow the webcam (it stands in for the glasses camera) → **Capture photo**, or **Upload a photo instead**.
Then type or speak a question, or just press **Enter** ("What is this, and what do my documents say about it?").
1. A Groq vision model reads the photo (what the object is + any text on it).
2. Your documents are searched with the question + those words.
3. The vision model answers from the photo + the matching excerpts. If a document page is used, it is shown as usual.
- The photo stays attached for follow-up questions; click ✕ to remove it. Photos are never saved.
- The vision model is picked automatically from your Groq account (override: put a model id in `vision_model.txt`).
- Photo questions also appear in the ProcOps dashboard, tagged `[photo: ...]`.
- Code: `backend/vision.py`, `glasses_app/src/camera.js`.
- **🔍 Scan (VR view):** look at an object in the VR scene so it sits in the red ring, then click **🔍 Scan** (or press **X**). The app grabs that view (its own panels hidden), the AI says what the object is, and the answer + a thumbnail appear on the VR answer panel, which moves in front of you. Code: `glasses_app/src/vrscan.js`.
- **Real-world simulation:** the app now starts a passthrough (AR) session. Click **Enter XR** and you look through the glasses at a realistic simulated room (default: kitchen) instead of the studio set. Change the room with the sofa icon in the emulator toolbar (Bedroom, Office, Living Room...). Look at an object so it is in the red ring and press **🔍 Scan / X** - the scan includes the simulated room. (To go back to the old VR studio: set `"mode": "vr"` in `glasses_app/iwsdk.config.json` and rebuild.)

## Voice-only panel + ☰ Apps (mirror windows)
- The text box is gone: ask by **voice** only (hold **V** or tap the big mic button). 📷 Photo asks about the photo straight away.
- **☰ Apps**: Chrome, WhatsApp, Instagram, Email (Gmail), YouTube, ProcOps, Upload. Click an app → it opens in a new Chrome tab (sign in there) → click **Show in VR** → pick that tab in Chrome's share box. The tab appears as a live window in VR (newest in front, older ones to the sides; **Front** / **✕** in the drawer).
- Windows are **mirrors (view only)**: WhatsApp/Instagram/Gmail block being embedded in other pages, so you reply/click in the real tab. Code: `glasses_app/src/apps.js`.

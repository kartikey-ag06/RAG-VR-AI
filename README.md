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
- No Python? Use `START_VR_RAG_POWERSHELL.bat`, the old server with keyword search only.

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

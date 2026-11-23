# VR_RAG: summary of all changes (this work session)

Source of the work: `ARCHITECTURE_RECOMMENDATIONS.md` and `NEXT_STEPS_ACTION_PLAN.md`. Everything below was tested against the running app with a real Groq key (not committed).

## 1. Backend (`backend/`)

| Area | Change | Files |
|---|---|---|
| Streaming | `POST /api/grok/stream` (Server-Sent Events, tokens relayed from Groq/xAI) | `app.py` |
| Voice out | `POST /api/tts` (edge-tts neural voice, MP3, input validated) | `app.py` |
| Reranking | FlashRank cross-encoder (`ms-marco-TinyBERT-L-2-v2`, ONNX, CPU) re-scores the top 20 hybrid hits and keeps 8; loaded in the background at start | `app.py` |
| Vector store | Embedded **Qdrant** (`backend/data/qdrant/`), created automatically on first run, reused afterwards; deterministic point ids so nothing is duplicated; deleted/changed chunks are removed. Falls back to the old `.npy` cache if Qdrant cannot load. (ChromaDB was tried first and removed.) | `indexer.py`, `app.py`, `requirements.txt` |
| Chunking | `pymupdf4llm` markdown extraction; tables are never cut mid-row (big tables split between rows, header repeated). `pypdfium2` fallback where PyMuPDF is blocked by Windows Application Control | `indexer.py` |
| Contextual retrieval | Every chunk is searched and embedded together with its document title/date (fixes "the 2018 order" style questions) | `indexer.py`, `app.py` |
| Confidence gating | `/api/search` returns `confidence` + `level` (high >= 0.75, medium >= 0.35, low). `mode: "unconfident"` (no citations) only when rerank < 0.20 AND embedding similarity < 0.65. Env: `VR_RAG_MIN_CONFIDENCE`, `VR_RAG_MIN_SEMANTIC` | `app.py` |
| Provenance | Each hit carries `prov` (doc id, uid, title, sha256, page, rev, date). ProcOps approvals are stamped: agent `vr-rag-procops`, base version, document / page / revised-page SHA-256, classification | `app.py`, `indexer.py`, `static/procops.html` |
| Upload progress | `GET /api/files/status/{job_id}` (stage + `42/95` style progress); upload response also returns `jobs`; live progress bar on `/upload` | `app.py`, `indexer.py`, `static/upload.html` |
| Security / config | `/api/server/stop` only from the local machine; API key can come from `.env` / environment as well as `grok_api_key.txt` | `app.py` |
| Dependencies | `edge-tts`, `pymupdf4llm`, `flashrank`, `pypdfium2`, `qdrant-client` | `requirements.txt` |

## 2. VR client (`glasses_app/src/`)

| Change | Files |
|---|---|
| Streaming answers fill the panel token by token (falls back to the normal call if the backend has no stream endpoint) | `rag.js` |
| Spoken answers: `VRVoicePlayer` (sentence queue, prefetch with max 2 syntheses, browser `speechSynthesis` fallback, barge-in that also aborts in-flight requests) and `LiveSpeaker` (speaks sentences / the first clause while the answer is still streaming). 🔊 button toggles it | `tts-player.js`, `rag-scene.js` |
| Confidence chip + coloured border (green / amber / red) in VR and in the DOM panel | `xr-panels.js`, `ui-overlay.js` |
| Page-image LRU cache (6 pages) so many pages cannot exhaust headset memory | `xr-panels.js` |
| Modularization: `rag-scene.js` is now the coordinator over `XRPanelManager` (`xr-panels.js`) and `UIOverlay` (`ui-overlay.js`) | `rag-scene.js`, `xr-panels.js`, `ui-overlay.js` |
| Start errors are shown on screen instead of failing silently | `index.js` |
| `glasses_app/dist/` rebuilt (`npm run build`); many hashed asset files change | `dist/` |

## 3. Tests, docs, decisions

- `backend/test_rag_eval.py [--answers]`: golden questions checked against the real PDFs (6 cases incl. 2 off-topic). Passed 6/6 with answers (Groq rate limits are retried).
- `docs/adr/001..004`: Qdrant, FlashRank, edge-tts voice loop, pymupdf4llm chunking.
- `README.md`, `SETUP.md`, `FEATURES_AND_PROGRESS.md` updated. `NEXT_STEPS_ACTION_PLAN.md` is the plan this work followed.

## 4. Measured results (this machine)

- Search about 160 ms. Upload returns in about 20 ms; an 85-page PDF showed live progress and finished.
- First spoken audio about 3.0 to 3.6 s after the question (target was < 1 s: not met; edge-tts needs 1.4 to 1.8 s per request, see ADR 003).
- Off-topic questions ("capital of France", sourdough, weather) return `unconfident` with no citations; on-topic ones are high confidence.

## 5. Known limits / not done

- pymupdf4llm table path was only unit-tested on synthetic markdown: PyMuPDF is blocked by Application Control here, so the `pypdfium2` fallback (plain text) ran on the real PDFs.
- Mic input and speaker playback were driven from script, not tested with a real microphone.
- The confidence margin is small (off-topic <= 0.65, short on-topic queries >= 0.665). Re-check with `test_rag_eval.py` after ranking changes.
- Not implemented: officer name in provenance, "source chunks cited" in the ProcOps stamp, positional audio, passthrough dimmer, air-gapped profile, JWT auth, Ragas evals, SQLite migration.
- Qdrant embedded mode allows one process per data folder (no multi-worker).

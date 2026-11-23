# VR_RAG: Features & Progress Overview

A voice-first Augmented Reality (AR/VR) Retrieval-Augmented Generation (RAG) assistant designed for smart glasses. It allows users to ask questions via voice or camera scans, answers using documents (e.g., regulatory/SOP files), and displays answers alongside original document pages in an interactive 3D environment.

---

## 1. Key Features

### 🕶️ AR / VR Interface (Meta Glasses Emulation)
- **Meta VR Glasses Profile:** Built on Meta's Immersive Web SDK (IWSDK 1.1) and WebXR using built-in IWER emulator (no headset required).
- **Gaze + Hand Tracking:** Hands-free and controller interactions (pinch for next page, squeeze for next source).
- **Passthrough / AR Simulation:** Realistic simulated rooms (Kitchen, Office, Living Room) with passthrough mode.
- **Dual Floating 3D Panels:** Real-time answer summary panel and synchronized original document page viewer.
- **Fallback Headset Support:** Quest headset compatibility via Chrome Immersive Web Emulator extension.

### 🔍 Hybrid RAG & Search Pipeline
- **Dual-Engine Search:**
  - **Dense Semantic Embeddings:** ONNX-based `BAAI/bge-small-en-v1.5` via FastEmbed on CPU (no PyTorch overhead).
  - **Sparse Keyword Search:** TF-IDF for exact terms, references, and penalty clauses.
- **Reciprocal Rank Fusion (RRF):** Blends semantic and keyword rankings for high-precision retrieval.
- **Offline / Browser Fallback:** Client-side TF-IDF search fallback if the Python backend is unavailable.
- **Embedding Cache:** Per-document vector caching for near-instant restarts.

### 🤖 LLM & Multimodal AI
- **Multi-Provider LLM Support:** Proxied through backend to protect API keys:
  - **Groq:** Fast Llama-3 inference (Free tier friendly).
  - **xAI Grok:** Grok-4.7 support.
  - **Local & Alternative:** Ollama (`llama3.2`), Claude, OpenAI, and offline extractive modes.
- **📷 Real-World Camera Scan:** Web camera snapshot or photo upload analyzed by Groq Vision models.
- **🔍 In-VR Reticle Scan:** Inspect objects in the 3D/AR scene; Groq Vision identifies items and links them with document context.

### 🎙️ Voice-First Interaction
- **Speech-to-Text (STT):** Push-to-talk (hold `V` or tap mic in XR) with automatic query execution.
- **Groq Whisper:** Cloud transcription via `whisper-large-v3-turbo`.
- **Browser STT Fallback:** Web Speech API (`en-IN`) when running keyless.

### 📄 Dynamic Ingestion & Document Management
- **Web Upload Portal (`/upload`):** Drag-and-drop ingestion for PDF, Word (`.docx`), and `.txt` files (up to 50MB).
- **Automated Processing:**
  - Text extraction and semantic chunking (~120 words).
  - High-resolution page image rendering (PDF pages converted to JPG; Word/TXT synthesized into A4 sheets).
- **Hot-Reloading Index:** Live index version check (`/api/index/version`); updates load immediately in glasses without restart.
- **Management:** Document deletion, duplicate hash detection, and legacy corpus import (`docs/`).

### ⚙️ ProcOps (Continuous Procedure Optimization)
- **Operations Dashboard (`/procops`):** Real-time monitoring of queries mapped to document pages/procedure steps.
- **Friction Heatmap:** Highlights confusing document pages generating the most user questions.
- **Content Gap Identification:** Surfaces queries unanswered by the existing corpus.
- **AI-Assisted SOP Rewrites:** One-click draft fixes via Groq with revision approval and v1.1 publishing directly to glasses.

### 🪟 VR App Multitasking (Mirror Windows)
- **Companion Viewports:** Mirror web applications into VR floating windows (Chrome, WhatsApp, Instagram, Gmail, YouTube, ProcOps, Upload).
- **Window Controls:** Depth sorting, bring-to-front, and quick dismiss from the drawer.

---

### 👓 Meta-Glasses-Style Assistant (Phase 7)
- **"Hey Glasses" wake word** (always-on listening) and push-to-talk in **22 languages** (voice language + answer language).
- **Live captions + live translation** of conversations on the in-lens HUD.
- **Answers read aloud**; answer styles: full, **glance card** (3 points + cut-out diagram), **spoken** (one sentence).
- **In-lens HUD**: head-locked display for notifications, captions, procedure steps and Live AI notes.
- **Live AI**: the assistant looks at the view every N seconds, notes what matters, spots QR codes, backs off on rate limits.
- **Daily assistant**: reminders (with times, read out when due), notes, calendar view, weather (Open-Meteo).
- **Camera**: photo + video capture of the view, gallery, troubleshooting filters (OCR boost, low-light, edges),
  look filters (B&W, vintage, warm, cool, cartoon), **privacy face blur** before cloud AI, **AR face filters** (MediaPipe).

### 🏭 Enterprise Field-Service Features (Phase 7)
- **Visual-to-RAG**: QR/barcode/serial -> `machine_id` -> search filtered to that machine's manuals.
- **Cropped diagram extraction**: tables, pictures and drawings cut out of PDF pages for small displays.
- **Guided workflows**: AI-built step-by-step procedures with a citation per step, progress on the HUD, voice "next".
- **3D workspace pinning** (synced with the `/workspace` desktop dashboard) + **look-to-ask** (2 s head-gaze).
- **Actionable exports**: inspection report / incident log / work order as PDF, DOCX or JSON; send to webhook, email, Jira, ServiceNow.
- **Remote expert**: WebRTC call sharing the worker's view + voice + AI context; expert console `/expert`; auto-suggested on low confidence.
- **Knowledge-gap analytics** in ProcOps: most asked, low-confidence and zero-hit questions.
- **MCP endpoint** `/mcp`: list / search / get page / ask tools for other AI assistants.
- **Connectors framework** (`/admin`): webhook, email, Jira, ServiceNow, folder sync, and live API sync from **SharePoint (Microsoft Graph), Google Drive (service account), Confluence Cloud and Notion** (new, changed and deleted items mirrored).
- **Document-level permissions (RBAC)**: users + groups, admin token, tokens stored hashed.
- **Production storage**: Postgres (Neon) + **pgvector** semantic search; files, texts, vectors and app data survive server restarts.

### 🎤 Talk, Meet, Move (Phase 8)
- **Teleprompter**: scripts written by the AI from your documents (or pasted / saved); follows your voice word by word (Chrome / Edge) or scrolls at a set speed.
- **Meeting notes**: Whisper transcript of every sentence; AI summary, decisions, action items; export PDF / Word / Markdown; action items → reminders.
- **Hand-gesture control**: MediaPipe hand tracking on the webcam - pinch, swipe, open palm, thumbs up.
- **Turn-by-turn directions**: walking / cycling / driving routes from OpenStreetMap, next turn on the lens, advances with GPS.
- **Calendar sync**: Google / Outlook calendars through their private ICS link, repeating events expanded.
- **Recording light**: red REC / CAM / SCREEN or amber MIC indicator whenever anything is captured.
- **Reliability**: TURN relay support for expert calls (coturn setup in `deploy/turn/`), Whisper fallback for wake word + captions in browsers without a speech engine.

---

## 2. Progress Till Now

| Milestone / Phase | Status | Details |
|---|---|---|
| **Phase 1: Core VR RAG Baseline** | ✅ Completed | IWSDK 1.1 WebXR integration, Meta VR Glasses emulator profile, Three.js dual-panel display, TF-IDF browser search. |
| **Phase 2: LLM & Voice Integration** | ✅ Completed | Secure backend proxy for Groq and xAI Grok, Groq Whisper STT, voice-first VR interactions. |
| **Phase 3: Hybrid Search Backend** | ✅ Completed | FastAPI backend, FastEmbed CPU embeddings (`bge-small-en-v1.5`), RRF fusion ranking, embedding caching. |
| **Phase 4: Document Upload & Ingestion** | ✅ Completed | Web portal (`/upload`), automatic PDF/DOCX/TXT conversion, visual page generator, hot-reload index versioning. |
| **Phase 5: Vision & AR Simulation** | ✅ Completed | Passthrough AR environment, webcam scanning, in-VR reticle capture (X-key scan), Groq Vision multimodal answers. |
| **Phase 6: ProcOps & App Multitasking** | ✅ Completed | Friction heatmap dashboard, AI revision editor, in-VR floating app window mirrors. |
| **Phase 7: Meta-Glasses Parity + Enterprise** | ✅ Completed | Wake word, captions/translation, HUD, Live AI, reminders/weather, camera + filters, Visual-to-RAG, workflows, pins, exports, remote expert, MCP, connectors, RBAC, Neon pgvector. |
| **Phase 8: Talk, Meet, Move + limits fixed** | ✅ Completed | Teleprompter, meeting notes, hand gestures, directions, calendar sync, recording light, TURN relay, SharePoint / Drive / Confluence / Notion connectors, non-Chrome voice. |

---

## 3. Current Working State

- **Backend:** FastAPI service running on port 8765 (`backend/app.py`) managing hybrid search, indexing, STT, and LLM proxies.
- **Frontend / VR Client:** Production bundle served at `glasses_app/dist/` with auto-injected IWER emulator.
- **Launch Script:** One-click automated setup and startup via `START_VR_RAG.bat`.

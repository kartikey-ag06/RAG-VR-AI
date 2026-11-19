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

## 2. Progress Till Now

| Milestone / Phase | Status | Details |
|---|---|---|
| **Phase 1: Core VR RAG Baseline** | ✅ Completed | IWSDK 1.1 WebXR integration, Meta VR Glasses emulator profile, Three.js dual-panel display, TF-IDF browser search. |
| **Phase 2: LLM & Voice Integration** | ✅ Completed | Secure backend proxy for Groq and xAI Grok, Groq Whisper STT, voice-first VR interactions. |
| **Phase 3: Hybrid Search Backend** | ✅ Completed | FastAPI backend, FastEmbed CPU embeddings (`bge-small-en-v1.5`), RRF fusion ranking, embedding caching. |
| **Phase 4: Document Upload & Ingestion** | ✅ Completed | Web portal (`/upload`), automatic PDF/DOCX/TXT conversion, visual page generator, hot-reload index versioning. |
| **Phase 5: Vision & AR Simulation** | ✅ Completed | Passthrough AR environment, webcam scanning, in-VR reticle capture (X-key scan), Groq Vision multimodal answers. |
| **Phase 6: ProcOps & App Multitasking** | ✅ Completed | Friction heatmap dashboard, AI revision editor, in-VR floating app window mirrors. |

---

## 3. Current Working State

- **Backend:** FastAPI service running on port 8765 (`backend/app.py`) managing hybrid search, indexing, STT, and LLM proxies.
- **Frontend / VR Client:** Production bundle served at `glasses_app/dist/` with auto-injected IWER emulator.
- **Launch Script:** One-click automated setup and startup via `START_VR_RAG.bat`.

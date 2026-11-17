# Setup for new team members

## Run the app (Windows, no installs)
1. Clone the repo:
   `git clone https://github.com/kartikey-ag06/RAG-VR-AI.git`
2. Create your own free Groq key at https://console.groq.com/keys
3. Copy `grok_api_key.example.txt` to `grok_api_key.txt` and replace its only line with your key.
   This file is in `.gitignore`. Never commit it.
4. Install Python 3.11+ (python.org). Then double-click `START_VR_RAG.bat` and keep the window open.
   The first run sets up the Python backend once, in 2-5 minutes. No Python? Use `START_VR_RAG_POWERSHELL.bat`, which has keyword search only.
5. Open http://localhost:8765 in Chrome. Type or speak (hold V) a question, then click **Enter XR**.

## Change the code
- VR app source: `glasses_app/src/`. To rebuild it, install Node.js 22+, then:
  `cd glasses_app`, `npm install`, `npm run build`
  The build goes to `glasses_app/dist/`, which the server serves. Use `npm run dev` for live editing.
- Backend: `backend/app.py` (FastAPI: hybrid search, Groq proxy, voice). Old fallback server: `server.ps1`.
- Add or replace PDFs: put them in `docs/`, then run `pip install pymupdf` and `python rag_pipeline/build_index.py`.
  Copy the new `web/rag/` contents into `glasses_app/public/rag/` and rebuild.

See README.md for how the project works.

# Setup for new team members

**Developers:** follow [CONTRIBUTING.md](CONTRIBUTING.md) - it has the one-step setup (`SETUP_DEV.bat`), the rules for
secrets and data, and the daily Git workflow.

## Quick version (Windows)

1. Install Python 3.11+ (tick "Add python.exe to PATH"), Node.js 20.19+ LTS and Git.
2. `git clone https://github.com/kartikey-ag06/RAG-VR-AI.git`
3. Double-click `SETUP_DEV.bat` (installs everything, builds the VR app, creates `.env`, runs quick tests).
4. Put your own free Groq key (https://console.groq.com/keys) in `.env` after `GROQ_API_KEY=`.
   `.env` is in `.gitignore`. Never commit it. (The older `grok_api_key.txt` file still works too.)
5. Double-click `START_VR_RAG.bat`, keep the window open, and open http://localhost:8765 in Chrome.
   Type or speak (hold V) a question, then click **Enter XR**.

All settings (Groq key, admin token, limits, database) live in `.env` - see `.env.example` for what each one does.
Leave `DATABASE_URL` empty on your computer: never use the production database for development.

## Just want to try it (no Node.js)?

Skip step 3: `START_VR_RAG.bat` sets up the Python backend by itself and serves the VR app already built in
`glasses_app/dist/`. No Python? `START_VR_RAG_POWERSHELL.bat` runs a basic version (keyword search only).

## Change the code

- VR app source: `glasses_app/src/`. `cd glasses_app`, then `npm run dev` for live editing or `npm run build`.
  The build goes to `glasses_app/dist/`, which the server serves.
- Backend: `backend/app.py` and the modules next to it (FastAPI). Restart `START_VR_RAG.bat` after a change.
- Tests: `backend\.venv\Scripts\python.exe -m pytest -q tests`
- Add documents: the upload page at http://localhost:8765/upload (PDF, DOCX, TXT). Files are stored in
  `backend/data/`, which is not in git. The sample PDFs in `docs/` are imported on first start.

See README.md for how the project works.

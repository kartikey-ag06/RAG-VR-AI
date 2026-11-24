# Working on VR_RAG together

Short rules so several developers can work on the project without breaking each other's work.

## 1. First-time setup (Windows, about 10 minutes)

1. Install **Python 3.11+** (python.org, tick "Add python.exe to PATH"), **Node.js 20.19+ LTS** (nodejs.org) and **Git**.
2. Clone the repo: `git clone https://github.com/kartikey-ag06/RAG-VR-AI.git`
3. Double-click **`SETUP_DEV.bat`**. It installs everything, builds the VR app, creates your `.env` and runs the quick tests.
4. Open **`.env`** and put **your own** Groq key after `GROQ_API_KEY=` (free: https://console.groq.com/keys).
5. Double-click **`START_VR_RAG.bat`** and open http://localhost:8765.

On first start the 10 sample PDFs in `docs/` are indexed automatically, so search works straight away.

## 2. Secrets and data - the rules that matter most

- **Never commit secrets.** `.env`, `grok_api_key.txt`, `database_url.txt` and `turn.json` are git-ignored. Before every commit, check `git status` does not list them.
- **Every developer uses their own Groq key.** Never share one key: usage, limits and costs get mixed up.
- **Never use the production database on your computer.** Leave `DATABASE_URL` empty (your data stays in `backend/data/`). On first start the server copies local documents into whatever database it is given - with the production URL that would put test files in front of real users. Need Postgres? Make your own Neon branch.
- Leaked a key by accident? Revoke it at once (console.groq.com) and tell the team. Deleting the commit is not enough - it stays in Git history.

## 3. Daily workflow

```cmd
git checkout main
git pull
git checkout -b feature/short-name
```

Change the code, then:

```cmd
backend\.venv\Scripts\python.exe -m pytest -q tests
git add <files>
git commit -m "Short summary of the change"
git push -u origin feature/short-name
```

- `git pull` first, so you start from the latest `main`; one branch per task.
- `git add <files>`: add files **by name**, not `git add -A`, so secrets and junk are never committed by accident.

Then open a **Pull Request** on GitHub into `main`. Someone else reviews it before it is merged.

- **Branch names:** `feature/...` new work, `fix/...` bug fix, `chore/...` tooling/docs.
- **Commit messages:** one line saying *what* changed and *why* (`Fix 401 on fallback answers: send token`).
- **Keep PRs small** - one topic per PR. Small PRs are reviewed faster and conflict less.
- **Pull `main` often** (`git pull origin main` on your branch) so conflicts stay small.
- After a PR is merged with "Squash and merge", start the next task from a **new** branch off `main`.

## 4. Where things are

| Part | Folder | Run / check |
| --- | --- | --- |
| Backend API (FastAPI) | `backend/` | `START_VR_RAG.bat`; logs in that window |
| VR glasses app (IWSDK / Three.js) | `glasses_app/src/` | `cd glasses_app` then `npm run dev` (live reload) or `npm run build` |
| Admin / upload / workspace pages | `backend/static/` | open http://localhost:8765/admin etc. |
| Tests | `tests/` | `backend\.venv\Scripts\python.exe -m pytest -q tests` |
| Settings | `.env` (from `.env.example`) | restart the server after a change |
| Deployment | `render.yaml` (backend), `vercel.json` (frontend) | deploys from `main` |

## 5. Before you open a Pull Request

- [ ] `pytest -q tests` passes (tests that need the internet or a Groq key may be skipped or fail offline - say so in the PR)
- [ ] the app starts and the feature works at http://localhost:8765
- [ ] no secrets, no `backend/data/`, no `.venv` in `git status`
- [ ] new settings are added to `.env.example` (with a comment)
- [ ] user-visible changes are noted in `USER_MANUAL.md` / `FEATURES_AND_PROGRESS.md`

## 6. Common problems

| Problem | Fix |
| --- | --- |
| `python` not found | reinstall Python with "Add python.exe to PATH", open a new terminal |
| `npm ci` fails | delete `glasses_app\node_modules`, run `SETUP_DEV.bat` again |
| Port 8765 already in use | another server window is open - close it |
| "No API key" in the app | put your key in `.env` (`GROQ_API_KEY=gsk_...`) and restart |
| Changes in `glasses_app/src` not visible | run `npm run build` (or use `npm run dev`), then Ctrl+F5 |
| `git checkout` says untracked files would be overwritten | `git status`, then move or delete those files (they are usually old copies) |

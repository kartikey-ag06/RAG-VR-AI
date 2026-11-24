# Connecting VR_RAG to AI / VR glasses ("one brain, many doors")

VR_RAG has **one backend** (search + Groq + page images) and several thin **doors**. Each kind of glasses
uses the door its maker supports. No glasses brand lets you paste a custom MCP URL directly, so the doors below
are the supported entry points.

| Door | URL | Glasses | Status of the platform |
|---|---|---|---|
| `/api/v1` REST API | `/api/v1/...` | Base for every client (and future native apps) | - |
| MCP server | `/mcp` | Meta AI glasses via **Meta AI Connectors**; any MCP-capable AI (Claude, ChatGPT...) | Connectors: developer preview, sign-up needed |
| HUD web app + WebMCP | `/hud` | **Meta Ray-Ban Display** (600x600 display) | Web Apps + WebMCP: developer preview, off by default |
| WebXR app | `/` | VR headsets (Quest, Android XR headsets), Meta glasses emulator | Works today |
| Native adapters (later) | call `/api/v1` | Meta Device Access Toolkit apps, Android XR display glasses, Even Realities (Even Hub) | Each needs its own phone app |

## 1. The `/api/v1` API

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/v1/health` | - | AI on/off, photo support, number of documents |
| GET | `/api/v1/documents` | - | documents you may see |
| POST | `/api/v1/search` | `{query, k?, machine_id?}` | passages + `image_url` of each page |
| POST | `/api/v1/ask` | `{question, machine_id?, style?, lang?}` | `{answer, mode, citations[], confidence}` |
| POST | `/api/v1/ask/stream` | same | Server-Sent Events: `meta` -> `delta`... -> `done` |
| POST | `/api/v1/scan` | `{image: data URL, question?}` | photo + question -> answer (+ what the photo shows) |
| GET | `/api/v1/documents/{uid}/pages/{n}` | - | page text + image links |
| GET | `/api/v1/documents/{uid}/pages/{n}.jpg?w=600` | - | page image, resized for small displays |

- `style`: `full` (default), `glance` (3 short points, for a lens), `spoken` (1-2 sentences, read aloud).
- `mode`: `pdf` (answer from your documents, with citations), `general` (not in the documents), `offline` (no AI key: best matching text).
- Photos sent as JSON must be under **1 MB** (the server's request limit).

**Access** (same rules as the rest of the app):
- Reading (`documents`, `search`, `pages`) is open; without a token you only see documents that have no groups.
- AI calls (`ask`, `ask/stream`, `scan`) need a token on a protected server (Render, users, or `ADMIN_TOKEN`).
- Send `X-VR-Token: <user token>` (a user from `/admin`) or `Authorization: Bearer <MCP_TOKEN>` (service access, public documents only).

Quick test:
```bash
curl -s https://<your-server>/api/v1/health
curl -s -X POST https://<your-server>/api/v1/ask -H "Content-Type: application/json" \
     -H "X-VR-Token: <token>" -d '{"question":"What penalty was imposed?","style":"spoken"}'
```

## 2. Meta AI glasses (all models) - MCP via Meta AI Connectors

1. Deploy the backend on Render (HTTPS). Glasses reach it through Meta's cloud, which cannot see `localhost`.
2. On Render set `MCP_TOKEN` (a long random string) and `PUBLIC_URL` (e.g. `https://vr-rag-backend.onrender.com`).
3. Apply for early access: https://dev.meta.ai/products/connectors
4. When accepted, register the MCP server: URL `https://<your-server>/mcp`, header `Authorization: Bearer <MCP_TOKEN>`.
5. Ask the glasses: "Hey Meta, ask VR_RAG what penalty was imposed on Reliance."

Tools: `list_documents`, `search_documents`, `get_page`, `get_page_image`, `ask_documents` (all read-only).
`ask_documents` takes `style: "spoken"` for short answers read aloud.

Test it from your PC first with Claude Code:
```bash
claude mcp add --transport http vr-rag https://<your-server>/mcp --header "Authorization: Bearer <MCP_TOKEN>"
```

## 3. Meta Ray-Ban Display - HUD web app + WebMCP

`/hud` is a 600x600 web app made for the glasses' display (dark = see-through, large text, D-pad focus).
- **D-pad / Neural Band**: arrows move focus, pinch/Enter selects, Escape goes back.
- **Dictation**: speak into the question field.
- **Meta AI (WebMCP)**: the page registers four tools on `document.modelContext`:
  `vrrag_get_screen`, `vrrag_ask_documents`, `vrrag_show_page`, `vrrag_turn_page`.
  WebMCP is **off by default** on the glasses (Developer Mode or rollout); the app works fully without it.

Open it on the glasses:
1. On a protected server, create a user in `/admin` and copy the token.
2. Open `https://<your-server>/hud?token=<token>` once (the token is then kept on the glasses).
3. Add the app to the glasses from the Meta AI app (Developer Mode), or try it first in the
   Meta Ray-Ban Display simulator (Chrome extension) at 600x600.

## 4. VR headsets - WebXR app

Open `https://<your-server>/` in the headset browser and press **Enter XR**. The app now searches through
`/api/v1/search`; on an older server without `/api/v1` it falls back to `/api/search` automatically.

## 5. Later: native phone apps

Meta Device Access Toolkit (iOS/Android), Android XR (Jetpack Projected) and Even Realities (Even Hub) all run
on the phone. They only need to call `/api/v1/ask` (or `/ask/stream`) and show `answer` + the `image_url_small`
of the first citation. No backend change is needed.

## Tests

```bash
python -m pytest -q tests/test_api_v1.py     # Groq is faked: no key or internet needed
```

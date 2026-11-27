# RAG-VR-AI Partner API (`/api/v1`)

The `/api/v1` surface is the stable contract shared across WebXR headsets, the Meta Ray-Ban Display `/hud`, mobile companion apps, and policy-gated AI agents.

- **OpenAPI 3.1 spec:** [`openapi-v1.yaml`](./openapi-v1.yaml)
- **TypeScript SDK:** [`../../sdk/ts/`](../../sdk/ts/README.md)
- **Python SDK:** [`../../sdk/python/`](../../sdk/python/README.md)

## 1. Authentication & Scopes

Send credentials using either:
- **Scoped API key (recommended for partners/apps):**
  `Authorization: Bearer vrk_<secret>`
- **User session token:**
  `X-VR-Token: <token>` (or `?token=<token>`)

Required scopes by endpoint:
- `knowledge:read`: `GET /api/v1/documents`, `POST /api/v1/search`, `GET /api/v1/documents/{uid}/pages/{page}(.jpg)`
- `ai:ask`: `POST /api/v1/ask`, `POST /api/v1/ask/stream`
- `ai:vision`: `POST /api/v1/scan`
- `ai:ask` + `knowledge:read`: `POST /api/v1/agent/run` (plus tool-specific scopes such as `knowledge:write` for mutating tools)

Rate/budget limits return HTTP `429` with `Retry-After: 3600`.

## 2. Meta Ray-Ban Display (`/hud`) Setup

1. Open `https://<host>/hud` in the Meta Display Web App or browser emulator.
2. Paste your `vrk_...` API key or user token into the HUD token prompt (stored locally in the browser).
3. Select a display style: `glance` (3 short points for lenses), `spoken` (1–2 sentences for TTS), or `full`.
4. The HUD streams answers via `POST /api/v1/ask/stream` and fetches resized page images via `GET /api/v1/documents/{uid}/pages/{page}.jpg?w=600`.

## 3. Mobile `/api/v1/scan` Loop (`frame_gate` + `session_id`)

For continuous camera scanning from mobile or smart glasses:
1. Reuse a stable `session_id` per inspection session (up to 64 chars) and keep `gate: true` (default).
2. Send JPEG frames as base64 data URLs (`data:image/jpeg;base64,...`) to `POST /api/v1/scan`.
3. Handle the returned `status`:
   - `skipped_blur`: frame failed Laplacian blur check; prompt user to hold steady (no vision/LLM credits spent).
   - `skipped_duplicate`: perceptual dHash matched the previous frame and the question did not change; reuse `answer` and `citations` (`cached: true`).
   - `reused_scene`: same scene, new follow-up question; re-runs RAG using `entity_state` without calling the vision model again.
   - `ok`: new scene analyzed by vision + RAG; `entity_state` and `gate_metrics` are updated.

## 4. Agent Human-Approval Workflow

1. Call `POST /api/v1/agent/run` with `{ "question": "...", "max_steps": 5 }`.
2. Read-only tools execute automatically within the bounded loop (`1..10` steps).
3. If the agent invokes a mutating tool (`read_only: false`), execution pauses and returns:
   `{ "status": "awaiting_approval", "approval_id": "...", "tool": "...", "args": {...}, "steps": [...] }`.
4. Inspect the pending request with `GET /api/v1/agent/approvals/{approval_id}`.
5. Confirm with `POST /api/v1/agent/approvals/{approval_id}/approve` or cancel with `POST /api/v1/agent/approvals/{approval_id}/reject`.
6. Approvals are tenant-isolated and single-use; replaying an already-resolved approval returns HTTP `409`.

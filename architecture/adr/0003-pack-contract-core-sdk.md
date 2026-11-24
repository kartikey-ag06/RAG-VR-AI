# 0003 - Pack contract: `core.sdk`, `CoreContext`, manifest

Status: Proposed (24 Nov 2026) - draft code in `backend/core/sdk/` (not wired into the app yet).

## Context
Modules today receive different positional arguments (`workflows.register(app, KV, llm_complete, run_search,
STORE, CONNECTORS, ROOT, AUTH)`), reach into globals, and write each other's data (meetings writes `reminder`
records). The Core cannot control what a module may do.

## Decision
Every capability and sector pack:
1. Ships a **`manifest.json`** (JSON, so no new dependency) validated by `core.sdk.Manifest`:
   id, name, version, kind (`capability` | `pack`), compatible Core range (`core: ">=1.0,<2.0"`), scopes it needs,
   events it subscribes to and publishes, config keys with defaults, MCP tools it adds.
2. Exposes **`register(ctx: CoreContext)`** and nothing else to the Core.
3. Imports **only `core.sdk`**. It gets everything through `ctx`:
   `search`, `answer`, `llm` (metered, no API key), `kv` (namespaced to the pack and tenant), `config`,
   `events`, `router` (mounted at `/api/v1/packs/<id>`), `require` (scope check), `call` (another module's
   published function), `log`.
4. Every `ctx` call passes the policy gate: tenant resolved -> pack enabled for tenant -> scope allowed ->
   AI budget left -> audit.

Versioning: `core.sdk` follows semantic versioning. At start-up the Core refuses a pack whose `core` range does not
include `SDK_VERSION`. Breaking SDK changes need a major version and two releases of deprecation.

Event names are `<area>.<past-tense-verb>`, for example `question.asked`, `document.updated`, `report.created`.

## Consequences
- Good: one uniform contract; the Core can enable, disable, meter and audit any pack; packs cannot see keys or
  other packs' data.
- Good: the same contract can later be served over HTTP for remote packs.
- Cost: every existing module must be ported to `register(ctx)` (Phase 5). Old routes stay as wrappers meanwhile.

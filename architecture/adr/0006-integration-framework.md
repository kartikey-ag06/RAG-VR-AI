# ADR 0006 - Integration framework: manifests, register(ctx), event bus

Status: Accepted (implemented in Phase 5)

## Context
Each feature module used to be wired by hand in app.py with its own argument list, and the glasses client had to tell
ProcOps about every question. Packs need one way in, under Core control.

## Decision
- **Loader** (`core/plugins/loader.py`) reads `backend/capabilities/*`, `backend/packs/*` (+ `VR_RAG_PACK_DIRS`).
  Each module = `manifest.json` + `register(ctx)`. Checked before any module code runs: valid manifest, Core SDK
  range, dependencies loaded, no cycles. A failing module never stops the server; it is marked and its URLs answer 404.
  The admin page shows every module with its state and error.
- **ModuleContext** (`core/plugins/context.py`) is the real `CoreContext`. Every call passes the gate:
  module ON for this tenant -> caller holds the scope -> module DECLARED the scope. Records are stored as
  `<module>.<kind>` per tenant; config keys as `packs.<module>.<key>`; prompts as `<module>.<name>` (tenant-overridable);
  AI calls go through the gateway (budget counted). `ctx.call` only reaches modules in `depends_on`.
- **Pack routes** are mounted at `/api/v1/packs/<id>`. **MCP tools** declared in the manifest are listed and callable
  only while the module is ON for the caller's tenant. Pack scopes (`<id>:<action>`) can be given to API keys.
- **Event bus** (`core/plugins/bus.py`): in-process, one background thread, bounded queue (drops instead of blocking).
  The Core publishes `question.asked` (every answer: /api/v1/ask, stream, scan, MCP, ctx.answer), `document.added|
  updated|deleted`, `pack.enabled|disabled`, `config.changed` (never the value). Listeners run with the event's tenant,
  only when their module is ON there; a failing listener is counted and shown, others still run.
- **SDK 1.1.0** (minor bump, backward compatible): `ctx.prompts`, `ctx.publish_function`, `ctx.mcp_tool`, manifest
  fields `load_after`, `legacy`, `legacy_routes`.

## Migration debt (strangler pattern)
The 8 existing capabilities are loaded through manifests but still run their old code via `ctx.legacy`
(`"legacy": true`). Only capabilities may do this - a pack asking for it is rejected, and a test checks packs import
only `core.sdk`. Each capability moves to the SDK when it is next changed; then its `legacy` flag goes.
Industrial Field Ops (ProcOps) is still registered by hand until Phase 6 turns it into the first real pack.

## Consequences
- Switching a module off hides its new routes, its old URLs and its MCP tools at once, per tenant, without a redeploy
  (Phase 5 gate - `tests/test_integration.py::test_kill_switch_hides_routes_and_mcp_tools`).
- The event bus and the AI budget are per process. Before running more than one instance: Redis Streams (or a queue)
  for events, and budget counters in the database.

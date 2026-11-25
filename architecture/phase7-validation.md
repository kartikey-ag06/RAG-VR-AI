# Phase 7 Validation Report - Centralized Core Engine

Date: 2026-11-25
Branch: `phase7/validation`
Baseline before Phase 7: `208 passed, 9 skipped`
Final test runs (`pytest-randomly`):
- Seed `101`: **226 passed, 9 skipped** (109.95s)
- Seed `202`: **226 passed, 9 skipped** (106.42s)
- Seed `303`: **226 passed, 9 skipped** (128.17s)

## 1. Validation Matrix

| Area | Check | Test Name | Result |
| --- | --- | --- | --- |
| A. Central control | Config rollback (`POST /api/core/config/rollback`, admin-only, history, audit, `config.changed` event) | `tests/test_validation_control_isolation.py::test_config_rollback_end_to_end` | Passed |
| A. Central control | Kill switch per tenant (hides new URLs, aliases, MCP tools, events, report kinds; other tenant unaffected; restore works) | `tests/test_validation_control_isolation.py::test_kill_switch_full_lifecycle` | Passed |
| A. Central control | Broken pack (`register()` raises) never stops server, returns 404, error shown in `/api/core/packs` and `/admin` | `tests/test_validation_control_isolation.py::test_broken_pack_isolated_and_shown_in_admin` | Passed |
| B. Isolation | Cross-tenant isolation (docs, search, citations, kv, journal, ProcOps heatmap, MCP, `/rag/index.json`, non-admin audit 403) | `tests/test_validation_control_isolation.py::test_cross_tenant_full_isolation` | Passed |
| B. Isolation | Cross-pack isolation (kv/journal namespacing, `ctx.call` undeclared dependency 403, undeclared scope 403, `ctx.legacy` 403) | `tests/test_validation_control_isolation.py::test_cross_pack_isolation` | Passed |
| C. Security | Scope enforcement (no key -> 401, wrong scope -> 403/401, revoked key -> 401, pack scope gated by pack state) | `tests/test_validation_security_load_compat.py::test_scope_enforcement_no_key_wrong_scope_revoked_and_pack_scope` | Passed |
| C. Security | Per-tenant AI budget (`/api/grok` 429 + `Retry-After`, `/api/v1/ask` offline fallback, `ctx.llm` 429, other tenant unaffected) | `tests/test_validation_security_load_compat.py::test_ai_budget_per_tenant_and_offline_fallback` | Passed |
| C. Security | Client-supplied system prompts ignored on `/api/v1/ask` and MCP `ask_documents` | `tests/test_validation_security_load_compat.py::test_client_supplied_system_prompt_ignored_on_public_routes` | Passed |
| C. Security | Secrets never leak in audit logs, events, `/api/core/*`, or error responses | `tests/test_validation_security_load_compat.py::test_secrets_never_appear_in_audit_events_core_or_errors` | Passed |
| D. Load | `LoadGuard` body limit (413) and token-bucket rate limit (429 + `Retry-After`) | `tests/test_validation_security_load_compat.py::test_load_guard_body_limit_413_and_rate_limit_429` | Passed |
| D. Load | `LoadGuard` AI concurrency cap (503 + `Retry-After`) | `tests/test_validation_security_load_compat.py::test_load_guard_ai_concurrency_503` | Passed |
| D. Load | Concurrency smoke (50 parallel `/api/v1/search` + 20 parallel `/api/v1/ask` offline) | `tests/test_validation_security_load_compat.py::test_concurrency_smoke_50_search_20_ask` | Passed |
| E. Backward compat | Golden files in `tests/golden/` unchanged and valid JSON | `tests/test_validation_security_load_compat.py::test_golden_files_exist_and_valid_json` | Passed |
| E. Backward compat | `/api/v1/*`, MCP core tools, `/procops`, `/api/procops/*`, `/api/machines`, `/api/search`, `/rag/index.json`, `/hud`, `/admin`, `/` | `tests/test_validation_security_load_compat.py::test_backward_compat_routes_mcp_and_static` | Passed |
| F. Second pack | `healthcare_sop` loads with `core.sdk` only, zero Core changes (ADR 0008) | `tests/test_healthcare_pack.py::test_healthcare_pack_loaded_sdk_only` | Passed |
| F. Second pack | `healthcare_sop` end-to-end (`/checklist`, config `max_steps`, `healthcare_sop:signoff`, `clinical_audit` report kind, `question.asked` -> `/topics` + MCP) | `tests/test_healthcare_pack.py::test_healthcare_pack_end_to_end` | Passed |
| F. Second pack | `healthcare_sop` tenant isolation & kill switch | `tests/test_healthcare_pack.py::test_healthcare_pack_tenant_isolation_and_kill_switch` | Passed |
| F. Second pack | Core & all other modules work with `backend/packs/healthcare_sop/` deleted | `tests/test_healthcare_pack.py::test_core_works_without_healthcare_pack_folder` | Passed |

## 2. Local Load-Test Measurements

- **In-process ASGI concurrency smoke (`test_concurrency_smoke_50_search_20_ask`):**
  - 50 concurrent `POST /api/v1/search` + 20 concurrent `POST /api/v1/ask` (offline mode) via `httpx.AsyncClient` against `backend.app`:
  - **70/70** requests returned `200 OK`, **0** `500`s, **0** event-bus listener failures, completed in **0.72s** (call duration).
- **Optional Locust script:** `tests/load/locustfile.py` (with `tests/load/README.md`) is provided for local load testing against `http://127.0.0.1:8000` only (never run against production Render).

## 3. Known Limits (Single-Instance Architecture)

- **In-memory event bus (`core/plugins/bus.py`):** Bounded queue on one background thread in a single process; events are not shared across multiple worker processes or instances.
- **In-memory AI budget & rate-limit buckets (`core/ai/gateway.py`, `backend/limits.py`):** Per-process counters reset on restart and are not shared across multiple instances.
- **Migration debt:** The 8 built-in capabilities under `backend/capabilities/` still use `ctx.legacy` (ADR 0006) and can be migrated to pure `core.sdk` incrementally.

## 4. Next Steps

1. Move `EventBus` to Redis Streams (or Postgres/queue) and `AIGateway._spent` / `Buckets` to Redis/Postgres before scaling beyond one Render instance.
2. Migrate the remaining `backend/capabilities/*` modules off `ctx.legacy` onto pure `core.sdk`.
3. Run `locust -f tests/load/locustfile.py` against a local `uvicorn` instance with a test API key when profiling multi-user sustained throughput.

# ADR 0005 - Control plane: settings, modules on/off, limits, audit

Status: Accepted (implemented in Phase 4, part 3)

## Context
One deployment serves many organisations (ADR 0002). Admins must change behaviour per organisation without a
code change or redeploy, and buyers ask "who changed what, and when?".

## Decision
- **Config store** (`core/config`): every setting is a declared key with type, default and limits. Values can be
  set globally, per tenant, or per tenant + module; the narrowest wins. Every change keeps a history entry.
  Unknown keys and wrong types are refused (400).
- **Module registry** (`core/modules`): each feature area lists its URL prefixes. `ModuleGate` middleware answers
  404 for a module switched off for the caller's tenant. Modules are ON by default. The core (search, `/api/v1`,
  MCP, files, admin) is never a module. Phase 5 turns modules into pack manifests; the switch stays.
- **AI budget**: `limits.ai_daily` (per tenant, 0 = none) is counted in the AI gateway on every provider call
  (chat, speech, vision). When used up: 429 on raw AI routes, and `/api/v1/ask` still answers from the documents
  (mode `offline`) with the reason as the note. Counters are in memory (reset on restart) - good enough for one
  instance; move them to the database before running several instances.
- **Audit log** (`core/observability`): append-only; tenants, keys, settings, modules, users and documents.
  Secret-looking fields are dropped before saving.
- **Prompt registry** (`core/ai/prompts`): prompts are registered by id with their default text by whoever owns
  them (today `api_v1.py`), and can be overridden per tenant via config key `prompts.<id>`. Core holds no sector text.
- Control plane endpoints live under `/api/core/*` and need the `admin` scope.

## Consequences
- Platform records (`config`, `config_history`, `module_state`, `audit`) are global kinds: the tenant is part of
  the record, so one admin view can list all tenants.
- Without a database, audit and history are JSON files that grow; production uses Postgres.

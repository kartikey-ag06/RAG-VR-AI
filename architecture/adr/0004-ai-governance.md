# 0004 - All AI calls go through the Core AI gateway

Status: Accepted (24 Nov 2026). Implemented 25 Nov 2026: glasses use `/api/v1/ask` (#13); `/api/grok` admin-only.

## Context
The glasses app builds its own system prompt (a SEBI legal-assistant prompt in `glasses_app/src/rag.js`) and
sends it to `/api/grok`, which forwards any client prompt to Groq. Any client can therefore bypass central
prompts, citation rules and policy. The RAG logic is duplicated in JavaScript and Python. This behaviour is
recorded in `tests/test_characterization.py::test_grok_passes_client_system_prompt_and_caches`.

## Decision
- The Core **AI gateway** (`core/ai`) is the only code that holds provider keys, chooses models, caches, meters
  budgets and calls the provider.
- System prompts come from the Core **prompt registry**, chosen per tenant and pack from config. Public routes
  refuse client-supplied system messages.
- Clients ask through `/api/v1/ask` and `/api/v1/ask/stream`. The glasses app switches to them in Phase 4.
- `/api/grok` and `/api/grok/stream` become **admin-only** after that switch, and are removed one release later.

## Consequences
- Good: one place for answer rules, cost control and audit; prompt changes need no client release.
- Cost: the glasses app loses its local prompt; offline mode keeps the extractive fallback.
- The characterization test above is updated in the same Phase 4 change, on purpose.

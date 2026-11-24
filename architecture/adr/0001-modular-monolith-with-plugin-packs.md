# 0001 - Modular monolith with plugin packs

Status: Proposed (24 Nov 2026)

## Context
VR_RAG must become a Centralized Core Engine ("the Brain") that powers many sector-specific use cases.
Today it is one FastAPI app (`backend/app.py`, ~850 lines) on a single Render instance. Rate limits, the answer
cache and the search index snapshot live in process memory. The team is small. Modules already plug in through
`register(app, ...)` functions, but each one takes a different argument list.

Options considered:
1. **Modular monolith** - one deployable, strict internal module boundaries.
2. **Plugin-based** - sector logic in packs that can be added, enabled and removed without editing the Core.
3. **Service-oriented / microservices** - Core and each sector as separate network services.

## Decision
Use **1 + 2**: one FastAPI deployable, split into `backend/core/` (the Brain), `backend/capabilities/`
(generic, Core-owned, switchable) and `backend/packs/` (sector logic). Packs plug in through the contract in
ADR 0003. We do not split into services now.

## Consequences
- Good: one deploy, one database, no network hops between Core and packs; in-memory limits and caches stay correct.
- Good: the existing `register()` pattern evolves into one uniform `register(ctx)`, so migration is gradual.
- Risk: a monolith can rot back into one tangled app. Mitigation: CI checks that `core/` never imports
  `capabilities/` or `packs/`, that packs import only `core.sdk`, and that `core/` holds no sector words.
- Later: the first part to split into a service is the embedding/indexing worker, and only when uploads slow down
  answers or a second instance is needed. A pack can also run remotely over `/api/v1` with the same contract.

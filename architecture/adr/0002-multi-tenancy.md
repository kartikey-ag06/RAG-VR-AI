# 0002 - One deployment serving many customers (multi-tenancy)

Status: Proposed (24 Nov 2026) - business decision taken: one deployment serves many customers.

## Context
A "tenant" = one customer organisation. Today VR_RAG has one global document library; permissions are only
groups on documents (`auth.py`). Every module writes free-form records into one `app_data` table.

Isolation options, from weakest to strongest:
1. **Shared tables + `tenant_id` column** on every row.
2. **One Postgres schema per tenant.**
3. **One database per tenant.**

## Decision
Use **option 1** now, in the existing Neon Postgres database:
- Every Core table and every `app_data` row gets `tenant_id`. Existing data is backfilled as tenant `default`,
  so current behaviour does not change.
- The tenant is resolved once per request from the caller's key or user (never from a client-supplied header or
  query parameter alone).
- Only repositories in `core/data` build SQL, and they always add `tenant_id`. Handlers and packs never write SQL
  against Core tables.
- Documents also get `collection_id`, so a pack sees only the collections the tenant gives it.
- Defence in depth (recommended in Phase 4): Postgres Row-Level Security policies on `tenant_id`.

## Consequences
- Good: cheapest to run; one migration path; works with Neon branching for safe migrations.
- Risk: a forgotten filter leaks data between customers. Mitigation: the repository layer is the only path to the
  database, automated cross-tenant tests in Phase 7, and RLS as a second wall.
- Later: a customer that needs its own region or database can be moved to option 2 or 3 behind the same
  repository interface. This is an open question in the plan.

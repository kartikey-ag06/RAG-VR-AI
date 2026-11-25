# ADR 0007 - Industrial Field Ops: the first sector pack

Status: Accepted (implemented in Phase 6)

## Context
ProcOps (question heatmap + AI redline), machine tags and the inspection / incident / work-order reports were wired
into the backend by hand, and the glasses client had to report every question to ProcOps itself.

## Decision
- Everything industrial moves to `backend/packs/industrial_field_ops` (manifest + `register(ctx)`), importing only
  `core.sdk`. The old `backend/procops.py` and the machine routes in `app.py` are gone.
- **Driven by events**: the pack listens to the Core event `question.asked` and logs the question against the first
  page the answer cited. The glasses app no longer reports questions; when the app has no AI it still asks the
  server (which answers from the documents and announces the question). `/api/procops/event` stays for older
  builds and is de-duplicated against the server event (same tenant + same question within `dedupe_seconds`).
- **New generic Core services (SDK 1.2.0)**, none of them industrial:
  - `ctx.documents` - the caller's visible documents, page text/images, page-level revisions (write needs
    `knowledge:write`; a tenant-wide reset touches only that tenant - the old reset cleared every tenant).
  - `ctx.journal` - append-only logs per tenant (`events` table, column `stream`, migration 2; local:
    `app_data/journal/<tenant>/<stream>.jsonl`). Existing ProcOps rows / files are moved into the pack's stream once.
  - `ctx.reports.register_kind` - report templates; the Work sessions capability shows the kinds of modules that
    are ON, plus a generic `summary`.
  - `route_aliases` in the manifest - old URLs (`/procops`, `/api/procops/*`, `/api/machines*`) keep working and are
    switched off together with the pack.
  - `question.asked` now carries the search `tag` (machine id).
- Approving / drafting / resetting pages needs the pack scope `industrial_field_ops:approve` (admins have it).

## Consequences
- Gate passed: with the pack folder deleted, the Core and all 8 capabilities still start and answer; the pack's URLs
  answer 404 and session reports fall back to `summary` (`tests/test_industrial_pack.py`).
- A new sector = a new folder under `backend/packs/`; no Core change needed for this kind of pack (Phase 7 proves it
  with a second pack).

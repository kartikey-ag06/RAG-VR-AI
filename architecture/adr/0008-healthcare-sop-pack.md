# ADR 0008 - Healthcare Clinical SOP: second sector pack with zero Core changes

Status: Accepted (implemented in Phase 7)

## Context
Phase 6 proved that Industrial Field Ops works as a pack under `backend/packs/`. Phase 7 must prove that adding a
completely different sector pack requires **zero** changes to `backend/core/` or `backend/app.py`.

## Decision
- Added `backend/packs/healthcare_sop/` (`manifest.json` + `__init__.py`) importing only `core.sdk`:
  - `POST /api/v1/packs/healthcare_sop/checklist`: searches SOP documents (`ctx.search`) and generates a numbered
    clinical checklist via `ctx.llm.complete` using its own registered prompt (`healthcare_sop.checklist`) and config
    key (`max_steps`).
  - Subscribes to `question.asked` and records per-tenant most-asked SOP topics in `ctx.journal` (`GET /topics` and
    MCP tool `healthcare_sop_topics`).
  - Declares pack scope `healthcare_sop:signoff` (`POST /signoff`) and report kind `clinical_audit`.
- **Zero Core changes** were made in `backend/core/` or `backend/app.py` for this pack.

## Consequences
- Verified end-to-end in `tests/test_healthcare_pack.py`, including per-tenant isolation, kill switch, and clean removal
  (`test_core_works_without_healthcare_pack_folder`).

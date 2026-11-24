"""core - the Centralized Core Engine ("the Brain") of VR_RAG.

Phase 2 (draft): only `core.sdk` exists - the contract that capabilities and sector packs will use.
Nothing in app.py imports this package yet, so the running app is unchanged.

Rules (see architecture/adr/):
  - core/ never imports from capabilities/ or packs/  (ADR 0001)
  - core/ contains no sector-specific words or logic    (ADR 0001)
  - packs import ONLY core.sdk                          (ADR 0003)
"""

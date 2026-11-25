# Sector packs

One folder per pack: `manifest.json` + `__init__.py` with `def register(ctx): ...`.
A pack imports **only** `core.sdk` (a test enforces this) and reaches the Core only through `ctx`.
Its URLs live under `/api/v1/packs/<id>/...`; admins switch it on or off per organisation at `/admin`.

Phase 6 adds the first real pack here: `industrial_field_ops`. Working examples of every SDK feature:
`tests/fixtures/packs/demo_pack`. Contract: `architecture/adr/0003` and `0006`.

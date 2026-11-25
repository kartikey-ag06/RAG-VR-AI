# Sector packs

One folder per pack: `manifest.json` + `__init__.py` with `def register(ctx): ...`.
A pack imports **only** `core.sdk` (a test enforces this) and reaches the Core only through `ctx`.
Its URLs live under `/api/v1/packs/<id>/...`; admins switch it on or off per organisation at `/admin`.

Packs here: `industrial_field_ops` (ProcOps question heatmap + AI redline, machine tags, field reports - ADR 0007).
Working examples of every SDK feature: `tests/fixtures/packs/demo_pack`. Contract: `architecture/adr/0003`, `0006`.

A pack can use: `ctx.router`, `ctx.require`, `ctx.search`, `ctx.answer`, `ctx.llm` + `ctx.prompts`, `ctx.kv`,
`ctx.journal`, `ctx.documents`, `ctx.config`, `ctx.events`, `ctx.reports`, `ctx.mcp_tool`, `ctx.publish_function`
/ `ctx.call`, and `route_aliases` in its manifest to keep old URLs working.

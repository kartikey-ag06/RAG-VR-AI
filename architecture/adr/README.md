# Architecture Decision Records (ADRs)

An ADR is a short note that records one important design decision: the context, the decision, and its consequences.
We keep them so that anyone joining later can see *why* the code looks the way it does.

Rules
- One decision per file, numbered in order: `NNNN-short-title.md`.
- Status is one of: Proposed, Accepted, Superseded by NNNN.
- Never rewrite an accepted ADR. If a decision changes, write a new ADR that supersedes it.

| # | Title | Status |
| --- | --- | --- |
| [0001](0001-modular-monolith-with-plugin-packs.md) | Modular monolith with plugin packs | Proposed |
| [0002](0002-multi-tenancy.md) | One deployment serving many customers (multi-tenancy) | Accepted |
| [0003](0003-pack-contract-core-sdk.md) | Pack contract: `core.sdk`, `CoreContext`, manifest | Accepted |
| [0004](0004-ai-governance.md) | All AI calls go through the Core AI gateway | Proposed |
| [0005](0005-control-plane.md) | Control plane: settings, modules on/off, limits, audit | Accepted |
| [0006](0006-integration-framework.md) | Integration framework: manifests, register(ctx), event bus | Accepted |
| [0007](0007-industrial-field-ops-pack.md) | Industrial Field Ops: the first sector pack | Accepted |
| [0008](0008-healthcare-sop-pack.md) | Healthcare Clinical SOP: second sector pack with zero Core changes | Accepted |

Full plan: "VR_RAG – Centralized Core Engine Architecture Plan" (claude.ai doc, 24 Nov 2026).

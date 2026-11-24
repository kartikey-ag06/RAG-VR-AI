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
| [0002](0002-multi-tenancy.md) | One deployment serving many customers (multi-tenancy) | Proposed |
| [0003](0003-pack-contract-core-sdk.md) | Pack contract: `core.sdk`, `CoreContext`, manifest | Proposed |
| [0004](0004-ai-governance.md) | All AI calls go through the Core AI gateway | Proposed |

Full plan: "VR_RAG – Centralized Core Engine Architecture Plan" (claude.ai doc, 24 Nov 2026).

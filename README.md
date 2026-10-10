# DBS DCTA — Tencent Cloud Hackathon

DBS track, Scenario 1: a conversational transaction prototype with explicit user approval and deterministic execution controls.

This repository currently contains the design and implementation checklist. Application code and integrations are not implemented yet.

- [Active plan v0.3](docs/PLAN.md): full MVP scope, validator/authorization contracts, required Langfuse backend, schedule and evaluation.
- [Status and to-do](docs/STATUS.md): current phase, owner actions, task board, blockers and hours.
- [Implementation checklist](docs/instructions.md): T-00–T-15 prompts, dependencies and acceptance checks.
- [Architecture](docs/ARCHITECTURE.md): service boundaries, approval lifecycle and data flows.
- [CodeBuddy project rules](CODEBUDDY.md): implementation invariants and evidence workflow.
- [Original review](docs/PLAN_REVIEW.md): recommendations and historical rationale. Scope reductions were not adopted.
- [TRTC quickstart](references/TRTC-Conversational-AI-Hackathon-Quickstart.en.md) and [hackathon handbook](references/3af2d08d-d3f0-4998-98a5-a820bd245910.pdf).

The full MVP retains transfers, bill/card payments, mock equities, bundles of up to four actions, voice, WebAuthn, mandate controls and Langfuse. Bank services and settlement are simulated; no real money is moved. Submission is 16 October 2026, with 15 October as the planning target.

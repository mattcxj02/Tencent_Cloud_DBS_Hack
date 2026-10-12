You are implementing Phase 5 ("Truthful progress and recovery") of docs/IMPROVEMENT_PLAN.md in this repository.

Read first: docs/IMPROVEMENT_PLAN.md (especially 'Non-negotiable constraints', your phase section, 'Verification and delivery', 'Configuration reference'), CODEBUDDY.md, docs/ARCHITECTURE.md, and the READMEs of folders you touch.

Rules:
- Phases 1, 2, 3, 4 and 6 are already implemented here. Implement ONLY Phase 5. Keep the diff focused; avoid drive-by refactors of shared files (shared/settings.py, gateway/orchestration.py, web/src/api.ts) beyond what your phase needs.
- Do NOT edit docs/STATUS.md, docs/ARCHITECTURE.md, docs/IMPROVEMENT_PLAN.md, docs/JUDGE_QA.md or README.md; the lead integrates those. You may update READMEs inside folders you change and .env.example.
- The tree contains uncommitted prior work (payee nicknames etc). Preserve it; do not reimplement it.
- Do not edit .env, print secrets, reset data/dcta.db, commit, push, or install/upgrade dependencies. Do not run live model calls or start long-running servers; use the existing fakes and tests.
- Match the surrounding code style and comment density. Python backend, React/TS frontend.
- Add meaningful regression tests for your acceptance criteria. Run make test and uv run ruff check . — both must pass. If you touch web/, run 'cd web && npx tsc --noEmit'.
- Finish with a concise summary: files changed, behaviour added, tests added, check results, acceptance gates met/unmet (and why), and anything the lead must do (e.g. live runs).
- Do not use plan mode or ask questions; work autonomously and make reasonable, documented choices.
- Do not read or print .env; use .env.example for setting names.

Context from earlier phases (build on these, do not duplicate):
- Phase 1: gateway readiness (gateway/readiness.py); permanent configuration errors are flagged in web/src/messages.ts via isConfigurationError and must not show a retry loop.
- Phase 3: structured clarification (gateway/clarifications.py, ClarifyCard.tsx).
- Phase 4: one overall drafting deadline (DRAFT_TIMEOUT_SECONDS, default 90 s; shared/deadline.py, gateway/drafting_work.py), owner+revision single-flight drafting, late results rejected. If the conversation changes during drafting, POST /api/conversations/{id}/draft now ends with CONVERSATION_CHANGED or STALE_CONVERSATION_REVISION instead of silently redrafting, and a validation-only retry reuses unchanged frozen work. The frontend currently just shows "Please try again" for these.
- Phase 6: enrollment and calculation evidence.

Implement Phase 5 items 1-5 and its acceptance criteria:
1. Report actual backend stage transitions (captured/transcribing, extracting, resolving, validating, ready, needs input, failed) for an owned operation/revision. Choose the simplest maintainable approach (bounded authenticated polling of a gateway-owned progress record is fine). Never use a timer to claim a stage the backend has not reached. Show elapsed time and use the existing useAnnounce mechanism for stage changes only (not every second).
2. Scope progress to the owner and conversation revision; late responses must not replace a newer conversation. Stop polling on completion, cancellation and unmount.
3. Bounded browser requests and recovery after timeout or reload. If an approval request result is unknown, fetch the existing authorization/receipt before suggesting another payment.
4. Make "retry validation" (reuses the frozen draft) distinct from "rebuild the corrected draft". When the conversation changed during drafting (CONVERSATION_CHANGED / STALE_CONVERSATION_REVISION), automatically start one new draft at the latest revision instead of asking the user to retry. Permanent configuration failures: explain, no retry loop.
5. Voice: make transcript correction understandable; if edits are allowed while drafting, supersede in-flight work safely. No new mandatory confirmation click.

Tests: backend tests that progress reflects real stages, is owner-scoped and revision-scoped, and never shows stale confirmation; frontend type check and build (cd web && npx tsc --noEmit && npm run build). Add browser fixture tests under web/tests/ if a harness exists there (Phase 3 added one); document how to run them. Aborting a draft must not claim to cancel an already approved/committed payment.

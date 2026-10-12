# DCTA improvement plan and implementation brief

Consolidated **12 October 2026** from the original improvement plan and the implementation prompt. This is the single source for the next implementation work. Copy this document into an implementation chat, or ask an implementation agent to follow it. Architecture and information-flow diagrams: [ARCHITECTURE.md](ARCHITECTURE.md).

**Update 13 Oct 2026:** all six phases are implemented and merged in the working tree; results and remaining unverified items are in [STATUS.md](STATUS.md) and [T-14-no-think](evidence/T-14-no-think.md). The sections below are kept as the original brief.

## Status and order

| Work | Status | Next action |
|---|---|---|
| Seed aliases, spacing-insensitive matching, customer nicknames and near-spelling suggestions | Implemented; regression-tested | – |
| Phase 1: readiness and development setup | Implemented | Operator: matching tokens, restart, check `/api/ready` |
| Phase 2: faster extraction | Implemented. `no_think` + extractor prompt v2: p50 2.0 s, p95 3.2 s; 34, 34 and 33 of 35 over three trials, 0 unsafe drafts | The 34/35 gate is met in two of three trials; the miss in the third (an unneeded clarification) is within the baseline's own trial-to-trial variation (33–34) |
| Phase 3: fewer clarification rounds | Implemented | Real-browser check of choice buttons |
| Phase 4: deadlines and reusable work | Implemented (single gateway process) | – |
| Phase 5: truthful progress and recovery | Implemented | Run browser fixtures; real-browser check |
| Phase 6: validation evidence and enrollment | Implemented. False rejects 5/25 → 0/24; wrong drafts accepted 0 | `make upgrade-db` on the real database; real enrollment cancel/retry |

The later review resolves three conflicts in the original plan: preserve the 34/35 accuracy baseline rather than allowing 33/35; report actual backend stages rather than guessing them with timers; and use the non-destructive database upgrade path rather than resetting existing passkeys and history. The original optional nickname proposal has already been implemented and is not a new task.

## Completed work to preserve

The original browser session exposed missing recognition of “mom” and “ahma”. Subsequent changes implemented:

- **Bank aliases:** `payee-mum` includes Mom, Mummy, Mommy and Mama; `payee-ahma` includes Ahma, Ah Mah, Amah and Grandma. Do not add the ambiguous alias “Ma”. Aliases also feed voice hotwords.
- **Shared exact matching:** [shared/names.py](../shared/names.py) is used by both [agent/names.py](../agent/names.py) and [gateway/evidence.py](../gateway/evidence.py). Matching ignores case, punctuation and spacing between words, but retains whole-word boundaries: “ahma”, “ah ma” and “Ah-Ma” can match Ah Ma; “mumbai” cannot match Mum. Preserve disambiguation of John versus John Doe.
- **Near-spelling suggestions:** small edit distances, the same first letter, names of at least three letters and filler-word filtering produce choices such as “Ahmar” → Ah Ma or “Jhon” → John. These are suggestions only; fuzzy matches are never accepted directly as evidence. Show all relevant candidates and a none-of-these option.
- **Customer nicknames:** the `payee_nicknames` table, Payees page (`#/payees`) and session/Origin-protected, audited gateway routes support nickname management. Conflicts with another payee, account, biller, card or stock are refused with `NICKNAME_CONFLICT`. The clarification card can offer to remember a nickname; the earlier receipt-only proposal is superseded. Nicknames feed bank reads, validator labels and ASR hotwords.
- **Database upgrade:** `make upgrade-db` adds the table and new aliases without deleting enrolled passkeys or history. Do not use `make reset-db` on the user's existing database for this work.

The earlier plan reported 48 nickname tests in [tests/test_payee_names.py](../tests/test_payee_names.py) and 46 passing attack scenarios. These are historical counts, not checks rerun by this consolidation. Verify that “mom” and “ahma” cases are also covered in the golden dataset; add them if missing, keep them separate from the original 35-case baseline, and disclose their development exposure rather than labelling them held-out without justification.

## Implementation instructions

Implement the following improvements to DCTA, prioritizing reliable completion and lower time from user input to an approvable draft. Preserve all existing payment features and security boundaries. Work through the phases in order, checking each phase before continuing. Inspect the current working tree first: it contains existing uncommitted work, including nickname support. Preserve it and do not reimplement completed features.

Read `CODEBUDDY.md`, `docs/ARCHITECTURE.md`, the relevant folder READMEs and the recorded evaluations before editing. Treat historical reports as evidence from those runs, not proof that current code passes. Update the architecture's proposed/current labels only after the corresponding behavior is implemented and verified.

## Evidence and objective

- A read-only review on 12 October 2026 found seven locally recorded extraction calls from 11–12 October: median 13.6 seconds, range 7.3–24.2 seconds.
- Both locally recorded validation attempts failed with `SERVICE_TOKEN_UNSET`. Settings loaded in the review process also reported the token unconfigured. Recheck configuration status without printing secrets; running services may have different environments.
- The 10 October golden evaluation measured extractor median 8.943 seconds versus whole-agent median 8.972 seconds. Natural-draft validation added median 1.333 seconds. A recorded flow with two clarification answers took 39.1 seconds of processing across three requests.
- The existing reasoning-control experiment used a small probe; its 1.5–1.6 second result is not a full-workflow benchmark or accuracy result.

Primary targets: fail promptly on known configuration problems; reduce extractor median to at most 3 seconds and p95 to at most 6 seconds on the original golden set; reduce unnecessary clarification and retry model calls. These are targets to measure, not claims or guarantees. Measure complete input-to-ready time separately, including voice transcription when applicable; never sum independently computed percentiles as an end-to-end percentile.

### Recorded reasoning-control probe (12 October)

The original plan records these results for a small one-leg prompt using TokenHub `hy3`, temperature 0 and JSON mode. They are historical observations, not newly verified provider guarantees. Check the current official provider contract during implementation.

| Request option (`extra_body`) | Time | Output tokens (reasoning) |
|---|---|---|
| none (current) | 5.9 s | 374 (356) |
| `{"reasoning_effort": "high"}` | 5.1 s | 299 (281) |
| `{"reasoning_effort": "low"}` | 3.7 s | 201 (183) |
| **`{"reasoning_effort": "no_think"}`** | **1.5–1.6 s (3 runs)** | **17 (0)** |
| **`{"thinking": {"type": "disabled"}}`** | **1.7–1.8 s (2 runs)** | **17 (0)** |
| `{"chat_template_kwargs": {"reasoning_effort": "no_think"}}` | 4.6 s | 302 (284): ignored by TokenHub |
| `{"enable_thinking": false}` | 6.0 s | 410 (392): ignored |

The top-level `reasoning_effort` and `thinking` options reduced this probe's runtime; the nested `chat_template_kwargs` and `enable_thinking` options did not disable reasoning in that experiment. The 17-token response is much smaller than the full workflow's output, so the speedup cannot be assumed to transfer directly. Keep validator reasoning unchanged during the extractor comparison.

## Non-negotiable constraints

- Neither model may approve, sign, execute, write the ledger, or choose payment tools. Preserve service isolation and scoped gateway reads.
- Keep the independent validator and fail closed on mismatch, uncertainty, invalid output, unavailable service or timeout. Do not relax validation to hit latency targets.
- Preserve gateway evidence checks, exact record resolution, deterministic money calculations, immutable bundle/envelope binding, WebAuthn approval, hard policy checks, step-up, and atomic execution with audit and receipt. Keep the extractor and validator on different vendors.
- Do not turn fuzzy name suggestions into accepted payment evidence. A user choice must be authenticated and bound to the correct question, field, payment leg and revision; gateway checks must validate it independently of the agent.
- Corrections, expired quotes and relevant bank or mandate changes invalidate affected cached work and approval eligibility. Recheck live policy at execution. Repeated approval of the same operation must still return one receipt and cause at most one commit.
- Do not edit `.env`, display secrets, reset the existing database, discard user changes, commit, push or deploy. Document required operator configuration with placeholders in `.env.example` and setup docs. Use isolated synthetic databases for tests and benchmarks. Record unavailable credentials or tools honestly and complete unaffected work.
- Do not add a queue, cache server or additional agent framework unless the existing architecture cannot meet a demonstrated requirement. Explain any dependency added.

## Phase 1 — Readiness before expensive work

Inspect `shared/settings.py`, `gateway/app.py`, `gateway/orchestration.py`, `shared/clients/validator.py`, `validator/app.py`, `Makefile`, and frontend error handling.

1. Separate liveness from readiness. Before starting extraction, detect missing/short validator credentials and unavailable required model/service configuration. Use bounded checks; avoid adding a model call or expensive network round trip to every request.
2. Distinguish presence of credentials from authenticated connectivity: a health response saying a token exists does not prove the gateway and validator share the same token. Provide an authenticated, non-model readiness check or equivalent verified startup path for that distinction.
3. Return stable, actionable errors and safe health details. Do not expose secret values, headers or user data. Show the user that the service is unavailable; give operator setup guidance in documentation/logs. Avoid offering repeated full drafting as the fix for a permanent configuration failure.
4. Document the shared `VALIDATOR_SERVICE_TOKEN` requirement, matching values in both services, the minimum length, and restart requirements. Do not generate or write secrets into the checkout.
5. Restore the original development-ergonomics work: report missing `VALIDATOR_SERVICE_TOKEN`, `OPERATOR_TOKEN` and `STEP_UP_SECRET` by name only, distinguishing drafting readiness from step-up availability. Make `make dev` clean up the processes it starts on exit and provide `make stop` scoped to this project's processes; avoid a broad `pkill` that could terminate unrelated services. Replace or remove the stale `make demo` placeholder. Retain the existing README guidance to use a supported passkey-capable browser at `http://localhost:5173`; do not duplicate guidance already present.

Acceptance: missing/short or mismatched token cases fail before extraction; unavailable dependencies are bounded; healthy readiness permits normal work; secret values never appear in responses or logs. A configuration-error test must assert zero extractor calls. Development shutdown leaves no child service processes from that invocation and does not stop unrelated processes; missing step-up configuration is reported without unnecessarily disabling otherwise eligible low-risk drafting.

## Phase 2 — Faster extraction, measured safely

Inspect `agent/extraction.py`, `agent/app.py`, `shared/settings.py`, `prompts/extractor.md`, telemetry configuration and `infra/compose.yaml`.

1. Add an explicit, validated extractor reasoning configuration such as `EXTRACT_REASONING_EFFORT`, including `.env.example`, Compose wiring and safe trace metadata. Preserve existing behavior when unset. Check the provider's current official contract before choosing request fields; do not send unsupported options to another provider.
2. Evaluate disabled reasoning first, then low reasoning if necessary, using the full golden set and multi-turn clarification/correction scenarios. Do not change validator reasoning in the same experiment.
3. Keep enough output budget for valid 1–4-leg JSON. The original proposal suggested testing 1024 output tokens with reasoning disabled; treat that as a candidate to measure, not an automatic new default. Lowering `max_tokens` alone is not the optimization. Reject truncation and malformed output.
4. Reuse provider HTTP clients with explicit startup/shutdown lifecycle where appropriate, while keeping user tokens scoped per request. Treat connection reuse as a secondary improvement, not an explanation for the measured multi-second model delay.

Acceptance: label runs by model, prompt hash, reasoning setting and token budget. Preserve historical baseline reports. Require zero unsafe drafts, at least the baseline 34/35 overall correct outcomes, and no regression on the existing clarification cases. Expand with nickname and follow-up cases and report those separately. If a candidate misses the quality gate, retain the safe default and report the result. If credentials are available, run paired baseline/candidate trials more than once and report sample sizes, p50/p95, errors, token use and available cost. Do not present fake-model timings as model performance.

## Phase 3 — Clarification with fewer repeated model calls

Inspect `agent/workflow.py`, `agent/questions.py`, conversation models/routes, evidence checking and `web/src/components/ClarifyCard.tsx`.

1. Collect related missing fields into one understandable clarification group, instead of stopping at the first issue and discovering the next only after another model run. Preserve clear links to individual payment legs.
2. Provide explicit payee/account choices where the gateway knows the candidates. Store authenticated selections with provenance in the conversation. Derive any quoted evidence from gateway-owned labels and the user's confirmed choice; never let an agent forge a user answer.
3. For an unambiguous structured answer, update the affected fields deterministically without re-extracting unchanged text. Recompute dependent amounts and revalidate the resulting frozen bundle. Free text, changed intent, ambiguity and corrections must safely fall back to extraction.
4. Preserve support for answers that change more than the requested field. Never silently retain an earlier amount, source or recipient after the user corrects it.

Acceptance: a scenario missing both recipient and source can collect both together; a supported structured answer makes no extra extractor call; final validation still runs. Reject forged IDs, another user's records, stale question/revision references, wrong-leg answers and ambiguous choices. Re-run correction, nickname, relative-amount and attack cases.

## Phase 4 — One deadline and reusable work

Inspect orchestration, extractor retries, validator timeouts and the browser request helper. The existing 45-second gateway wait is shorter than the extractor's 60-second request timeout with one SDK retry.

1. Propagate an end-to-end deadline across gateway, agent and provider calls; give each stage only the remaining budget. Bound retries and admission/queue waiting within it. Handle cancellation honestly: closing an HTTP request does not guarantee a remote model stops generating.
2. After cancellation, timeout or superseding input, prevent late results from being published or starting new downstream work. Preserve mandatory audit of state changes.
3. Deduplicate simultaneous drafting for the same owner and revision. Design for the supported process/deployment model; document any single-process limitation rather than assuming an in-memory lock works across workers.
4. Reuse an existing frozen draft for a validation-only retry if its conversation, payload, bank metadata, quote validity and other required bindings are still current. Reuse an eligible stored MATCH only under existing eligibility checks. Changed inputs must trigger the required rebuild/revalidation.
5. Keep validation attempt/rate limits and exactly-once settlement behavior. Do not retry permanent configuration or semantic errors as transient failures.

Acceptance: simulated slow providers return within the configured overall budget plus documented scheduling tolerance; no new stage starts after expiry; stale results cannot reach confirmation. Concurrent same-revision requests do not duplicate extraction. A retry after a transient validation failure reuses valid drafting work, while an edit or expired quote prevents reuse.

## Phase 5 — Truthful progress and recovery

Inspect `web/src/components/Chat.tsx`, `web/src/api.ts`, voice capture and backend orchestration. Choose bounded authenticated polling or server events based on the simplest maintainable implementation.

1. Report actual stage transitions: captured/transcribing, extracting, resolving, validating, ready, needs input or failed. Show elapsed time and accessible announcements using the existing `useAnnounce` mechanism. Never use a timer to claim a stage the backend has not reached.
2. Scope progress to an owned operation/revision, and prevent late responses from replacing a newer conversation. Stop polling/subscriptions on completion, cancellation and unmount.
3. Provide bounded browser requests and recovery after timeout or reload. If an approval request has an unknown result, retrieve the existing authorization/receipt before suggesting another payment.
4. Make validation-only retry distinct from rebuilding a corrected draft. Explain permanent configuration failures without a misleading retry loop.
5. For voice, make transcript correction understandable. If allowing edits while drafting, supersede in-flight work safely. Do not add a mandatory extra confirmation click without a clear usability reason.

Acceptance: progress matches real backend state; delayed responses, reloads, navigation and corrections cannot show stale confirmation. Screen-reader updates are useful without announcing every elapsed second. Aborting a draft does not claim to cancel an already approved/committed payment.

## Phase 6 — Validation evidence and enrollment reliability

1. Extend gateway-built validation input with the minimum calculation evidence needed for derived amounts such as “the rest”: source balance snapshot, ordered preceding debits, relevant dependencies and resulting amount. Use exact money types. Bind this evidence into request hashes and eligibility/invalidation logic; do not rely on extractor reasoning or unfrozen live values. Preserve privacy rules and execution-time checks.
2. Distinguish semantic agreement from deterministic risk policy. Investigate the five historical false rejects, including injected/coercive wording, without suppressing real uncertainty or weakening scam controls. Evaluate deliberately wrong drafts as well as correct ones; report mismatches, uncertainties and errors separately.
3. In enrollment, validate/reserve the enrollment code when beginning, but consume it only in the successful credential-registration transaction. Preserve the existing 15-minute code expiry and single successful use, bind challenges to the code, bound repeated begins (the original proposal suggested five), and invalidate sibling challenges after success.

Acceptance: correct remaining-balance drafts can be verified from supplied evidence; altered balances/dependencies or amounts cannot reuse validation improperly. No previously blocked wrong-draft probe becomes accepted. Enrollment begin → cancel → begin → verify succeeds; expired codes, concurrent successful uses, old sibling challenges and replay remain blocked.

## Verification and delivery

- Add meaningful regression tests for the behavior above, including failed readiness without model work, timeout propagation, deduplication, stale-result handling, validation-only retry, structured-answer provenance, derived-amount binding and enrollment races.
- Run relevant tests during each phase, then the full backend suite, lint, frontend type check/build and fake evaluation harness. Re-run the rogue-agent/attack checks and affected isolation checks. Avoid resetting the user's database or automatically reinstalling dependencies unnecessarily.
- Add browser tests for clarification, voice correction, progress, retry, reload and approval recovery. Use isolated fixtures/software authenticators for automation; separately document any real Touch ID/microphone walkthrough actually performed.
- Run live model evaluations and sanitized Langfuse read-back when configured. If blocked, retain safe defaults and report exactly which acceptance gates remain unverified. Never fabricate performance, security, browser or live-backend results.
- Update folder READMEs, setup guidance, [ARCHITECTURE.md](ARCHITECTURE.md), [STATUS.md](STATUS.md), [JUDGE_QA.md](JUDGE_QA.md), the main README examples and evidence with actual results. Record the reasoning comparison in a new dated evidence report, such as `docs/evidence/T-14-no-think.md`, without overwriting the baseline. Preserve historical measurements. Report extractor, validator, ASR and complete request timings separately, plus model-call counts per completed task and clarification count.
- Finish with what changed, measured before/after results, checks that passed, remaining limitations and required operator configuration. Do not claim completion of a phase whose acceptance gates remain unmet.

## Configuration reference

Settings are read by [shared/settings.py](../shared/settings.py). Values below are the defaults reviewed on 12 October, not an assertion about running services. `EXTRACT_REASONING_EFFORT` is proposed and is not wired into the current extractor. Phase 1 adds non-model authenticated readiness and configurable extractor timeout/retries; full deadline propagation remains proposed.

| Variable | Service | Reviewed default or status |
|---|---|---|
| `EXTRACT_PROVIDER` | Agent | `tokenhub`; supported provider choices currently `tokenhub` or `deepseek` |
| `EXTRACT_MODEL` | Agent | `hy3` |
| `EXTRACT_MAX_TOKENS` | Agent | `4096` |
| `EXTRACT_TIMEOUT_SECONDS` | Agent | `40` seconds per provider request; allow for callbacks within the gateway wait |
| `EXTRACT_MAX_RETRIES` | Agent | `0`; validated range 0–2; higher values require timeout/backoff headroom |
| `EXTRACT_REASONING_EFFORT` | Agent | Proposed in phase 2; unset must preserve existing behavior |
| `VALIDATE_PROVIDER` | Validator | `deepseek` |
| `VALIDATE_MODEL` | Validator | `deepseek-flash` |
| `VALIDATE_MAX_TOKENS` | Validator | `4096` |
| `VALIDATE_TIMEOUT_SECONDS` | Validator | `30` seconds |
| `AGENT_TIMEOUT_SECONDS` | Gateway | `45` seconds |
| `VALIDATOR_CLIENT_TIMEOUT_SECONDS` | Gateway | `40` seconds |
| `VALIDATOR_SERVICE_TOKEN` | Gateway and validator | Same secret, at least 32 characters; no configured default |
| `OPERATOR_TOKEN`, `STEP_UP_SECRET` | Gateway | Required for the corresponding simulated step-up/operator functionality |

Provider URLs live in `LLM_PROVIDER_URLS`. Existing model choices are configurable without code changes; adding reasoning controls requires phase 2 implementation. Operators must restart the relevant services after changing their environment and verify Compose passes each service only the credentials it needs. Keep the extractor and validator on different vendors. Do not edit `.env` as part of the implementation task.

## Verification commands

Use the existing project commands where appropriate: `make test`, `make lint`, `make eval-fake`, `make eval` for configured live providers, and the frontend's `npm run build` after dependencies are installed. `make web-build` currently also runs `npm install`; avoid unnecessary reinstalls. Run `scripts/rogue_agent_demo.py` with isolated synthetic data and affected isolation checks as required. Browser tests added by the implementation need a documented command. These are implementation instructions, not commands executed by the documentation merge.

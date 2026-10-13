# DBS Track — DCTA Hackathon Plan v0.3

Status: revised 9 October 2026 following the plan review and validator discussion. Planning only; no application implementation is present in this checkout. The full MVP is retained at the user's request, and **Langfuse is required**. The optional scope reductions in `PLAN_REVIEW.md` are recommendations, not the build contract. Implementation tasks: `instructions.md`. (Both are on the `dev` branch.) Trust boundaries: `ARCHITECTURE.md`. Live progress and owner actions: `STATUS.md`.

Solo entrant · DBS Banking track · Direct Conversational Transaction Agent · **Scenario 1: Voice-Enabled Payment and Transaction**, including KYC and basic risk control. Submission: **16 October 2026**; aim to submit on 15 October. Finalists: 23 October. Demo Day: 3 November, marked TBC in the handbook. Exact submission cutoff and DBS preliminary rubric must be checked through the participant channel.

**Product outcome and scope**

Help an authenticated retail customer complete several routine banking actions through one conversation, with understandable correction, explicit approval, and a verifiable receipt. Suggested nine-word blurb: “Speak your payments. Verify every detail. Stay in control.”

| Capability | Required MVP behavior |
|---|---|
| Transactions | SGD only; `transfer`, `pay_bill`, `pay_card`, `buy_equity`; ordered bundles of 1–4 legs |
| Transfers | Own-account transfers, saved payees, and saved PayNow recipients using simulated routing |
| Payments | Saved billers and owned credit cards, including bank-resolved outstanding balance |
| Equities | Mock SGX D05, O39, U11, Z74 quotes, holdings and purchases; explicit quantity, price, debit and residual budget before approval |
| Conversation | Voice and text, colloquial amounts, aliases, multiple intents, clarification and corrections, supported relative amounts with explicit dependencies |
| Controls | Independent semantic validator, deterministic policy, WebAuthn approval, hard mandate limits, cooling for increases, immediate pause/decrease, KYC, sanctions, velocity, scam signals and simulated secondary approval |
| Interface | React chat, microphone, confirmation, visible changes, audit/evidence view, mandate management and accessible edit/cancel flows |
| Evidence | Raw input records, prototype hash-chained audit, **Langfuse backend**, Rogue Agent Mode, attack tests, evaluation comparison and CodeBuddy evidence |

Real WebAuthn remains the implementation target. The handbook permits mock biometrics and a mock bank gateway; that remains a documented contingency, not an automatic scope change. Explicitly identify all bank, PayNow, equity, KYC, sanctions and secondary-approval services as simulated. Tencent TRTC ASR remains the planned voice integration, with browser speech as a development/failure fallback. Tencent APM, TTS and public Lighthouse hosting remain optional additions as in v0.2. Signed intent-pack registries, agent signatures, signed receipts and independent audit checkpoints are future recommendations; the old architecture drew them without corresponding MVP tasks.

**Security principle and threat model**

GenAI produces drafts and semantic assessments. It cannot authorize funds. Assume either model can hallucinate, obey injected instructions or return `MATCH` for a bad draft. Both models have no execution tools, ledger access, signing keys or ability to change policy. Different vendors may provide useful diversity; reduced correlated error is a hypothesis to measure.

Trusted components are the authenticated input capture, application confirmation UI, gateway code and identity store, ledger permissions, and validator-service configuration. Attacker-controlled content includes speech/text, references, labels and model output. A compromised browser, authenticator, gateway host or deployment credential is outside this prototype's demonstrated guarantees. A user may also approve a mistaken or socially engineered payment; cryptographic binding does not prove informed intent.

The semantic validator is not immune to prompt injection. Deterministic checks do not interpret free text as instructions, but still require correct implementation. The claim to demonstrate is: even when both models are wrong, they cannot bypass exact-payload user approval or gateway policy. Do not claim universal safety, regulatory compliance or guaranteed legal non-repudiation.

**Workflow and service ownership**

1. The authenticated client submits text, final ASR transcripts and corrections to a gateway-owned conversation store. Original audio is retained separately with a digest. Distinguish user turns, ASR revisions, bank data and application questions; preserve all versions. The drafting agent cannot append fabricated user turns.
2. The agent reads a scoped snapshot, bounds input size, extracts structured slots, parses supported amounts and obtains account/payee/biller/card/quote metadata over read APIs. No model chooses tools.
3. Deterministic resolution returns all plausible candidates. Missing or ambiguous values trigger short questions. Corrections create a new conversation revision; never silently change an approved draft.
4. The agent proposes a bundle. The gateway validates the schema, ownership and metadata, computes dependency arithmetic, canonicalizes the resolved bundle and freezes its version. Agent-supplied names, provenance labels and totals are not authoritative.
5. The gateway requests validation from a separate read-only validator service using its stored conversation, exact frozen bundle and minimum bank metadata. It obtains the result directly, checks the output schema and binds the result to the bundle and conversation hashes. Agent-supplied “validation passed” flags are rejected.
6. A successful semantic check and deterministic pre-check allow the gateway to issue a signing envelope. The client fetches it directly from the gateway and renders precise details, totals, residual cash and any changes.
7. WebAuthn approval is bound to the full envelope. After verification the gateway rechecks live policy; any required secondary approval is bound to the same transaction. Only then may one atomic mock-ledger commit execute the legs and return a receipt.
8. Every stage produces audit events and correlated Langfuse observations. A receipt supports “You said → We clarified → You approved → Executed.” Technical hashes are expandable details.

The agent uses READ and DRAFT functions only. The gateway alone issues nonces and executes transactions. `shared/` holds pure schemas, canonicalization and clients, not an execution dispatcher. `agent/` and `validator/` must not import `gateway/` or `ledger/`. Import tests supplement actual isolated runtime permissions. Only the gateway mounts the ledger/audit database; models and agent containers cannot access its file or credentials. Agent audit events go through a scoped append API and cannot impersonate gateway decisions.

**Validator contract and prompt protection**

The validator's job is to compare all requested actions against all drafted actions, including beneficiary, amount, currency, source account, asset, quantity and dependencies. Its inputs contain the complete ordered conversation including clarification questions and user corrections, an immutable bundle, and authoritative ID-to-display mappings. It receives no extractor reasoning or extractor-written summary. Bank labels are evidence, not instructions. An incorrect ASR transcript may fool both models, so the user must see and be able to correct it.

Use a fixed, version-controlled system prompt on the validator backend. Users and the agent cannot set its instructions, model or output schema. Keep secrets out of prompts and design as if the prompt is public. Separate policy instructions from serialized data, bound input sizes, and require structured output; these are mitigations, not proof against injection. Do not truncate relevant history silently: require a fresh explicit restatement when the supported context budget is exceeded. Prompt changes require review and evaluation, and prompt version/hash is logged to Langfuse. Avoid mutable remote prompt labels during a demonstration.

Return `MATCH | MISMATCH | UNCERTAIN` and bounded issues containing leg ID, field, short reason, supported value when known, and evidence turn/span references. No tool calls, transaction edits, repair execution or arbitrary HTML. Server code verifies field names and evidence locations; a valid reference does not prove the model's interpretation is correct.

- MATCH: eligible for confirmation only, never authorization.
- MISMATCH: freeze this draft and show deterministic field-level feedback; any correction creates a new version and revalidation.
- UNCERTAIN: ask for clarification; do not approve.
- Timeout, unavailable model, malformed JSON or unknown verdict: stop approval with a retry option; no fallback to MATCH.

Backend code records the verdict with `bundle_hash`, `conversation_hash`, `conversation_revision`, `validator_prompt_hash`, model ID and trace ID. Every subsequent change invalidates validation and pending approval. The independent audit runs after compilation; it is not described as simultaneous extraction. Freeze model and prompt versions for evaluation comparisons.

**Signing and execution contract**

Money uses Decimal and serializes as two-decimal strings; reject floats, non-finite/negative values and excessive precision. Mock equity prices are also two-decimal SGD values. All signed timestamps have one UTC ISO-8601 Z format. Canonical JSON has sorted object keys, compact separators and UTF-8; array order is meaningful. IDs and strings are validated before freezing; do not normalize them after signing.

The versioned `SigningEnvelope` contains `schema_version`, `operation` (`execute_bundle` or `update_mandate`), authenticated `user_id`, `credential_id`, `bundle_id` or `change_id`, full immutable payload, conversation binding for a bundle, active mandate version, a random 32-byte nonce, `issued_at` and `expires_at`. Expires at most 120 seconds after issue, and no later than a quote's expiry. Do not allow client-selected users or operation changes.

`challenge = SHA256(b"DCTA:approval:v1\x00" + canonical_bytes(envelope))`.

Use this exact function when issuing and verifying. Store the envelope bytes and hash server-side. Execute requests carry the authorization ID, expected envelope hash and assertion, not a replacement payment payload. The gateway executes its stored payload. Verify session, credential ownership, origin, RP ID, user verification, expected challenge, validity and current revision; require a new challenge after any edit. Signed mandate operations cannot authorize payments.

WebAuthn RP ID and origin are bound to the host: `localhost` works over plain HTTP on the development laptop (Touch ID via Chrome/Safari), but a phone requires an HTTPS origin, and credentials enrolled on one RP ID do not work on another. Record the demo on the laptop unless a hosted HTTPS deployment is tested and re-enrolled. WebAuthn user verification may use a PIN or biometrics. A React overlay remains trusted application UI, not a browser-protected payment confirmation surface. Do not equate this implementation with Secure Payment Confirmation. Source accounts, owned cards and payees must belong to or be operable by the authenticated user, regardless of signature validity.

Authorization states: `PENDING_APPROVAL → AWAITING_STEP_UP → COMMITTED`, or directly `PENDING_APPROVAL → COMMITTED`; terminal alternatives are `BLOCKED`, `EXPIRED`, `CANCELLED`, `SUPERSEDED`. A step-up transition retains verified approval evidence but does not move funds. Only one terminal commit is allowed. Store the secondary challenge against the user, envelope hash, approval ID and expiry; verify through a separate simulated channel, with single-use proof and bounded attempts. The agent cannot create or complete that proof.

Dispatch deterministic policy by operation type. Signed mandate settings require authenticated ownership and valid change rules, not a semantic MATCH or sufficient funds for a payment. A paused/expired spending mandate must not prevent its owner from submitting an authorized settings change; relaxation still observes cooling. Payment-only eligibility checks cannot be bypassed by changing the signed operation type.

Commit policy rechecks, balance/holding mutations, daily/velocity counters, nonce consumption, secondary-proof consumption, receipt and mandatory audit event in one SQLite transaction. Recheck state under the transaction lock to handle concurrent requests. Idempotency is scoped to `(user_id, operation, bundle_id/change_id)` and the immutable payload hash: the identical authenticated retry returns the original receipt; changed payload with that key rejects. Retry after a lost response must not repeat effects. In-flight step-up retries return the same pending status, never consume the proof twice.

**Mandates, risk and transaction semantics**

All numeric settings below are prototype policy choices, not statements about DBS or regulatory rules.

- Hard mandate: active and unexpired; allowed actions are all four MVP actions; maximum four legs; SGD1,000 debit per leg; SGD3,000 aggregate daily debit. Unregistered beneficiaries block. Saved PayNow mappings are resolved server-side. Daily totals use Asia/Singapore calendar boundaries, even though stored times are UTC.
- KYC: verified/unverified/expired status and tier. Unverified/expired blocks. Tier daily ceilings: SGD500/5,000/20,000; apply the stricter of KYC and mandate limits. Mock sanctions block regardless of secondary approval.
- Velocity: at most five transfer legs in a rolling ten-minute window across bundles, counting the candidate bundle and committed transfers. Serialized checks prevent concurrent bypasses.
- Sufficient funds: simulate ordered effects across every leg, including internal credits, fees if modeled, card outstanding balance and equity debit. Reject the whole mock bundle on any failure. Count every outgoing leg toward spending ceilings; state this conservative rule to avoid ambiguity about own-account transfers.
- Risk threshold: default SGD500 aggregate bundle debit triggers simulated secondary approval when exceeded but still inside all hard limits. Deterministic urgency/“safe account”/impersonation signals from the gateway-held transcript may also trigger it. Keyword rules can miss scams and can flag benign requests; measure both. Offer the immediate conversational-channel pause control, without claiming it locks all bank funds.
- **No step-up can override a hard mandate, KYC, sanctions, ownership, balance or velocity block.**
- Mandate increases or permission expansions require a separately signed change and 12-hour cooling. Keep the old version active until then. Decreases and pauses are immediate, cancel pending increases and invalidate pending payment approvals. Mixed increases/decreases are rejected with guidance to submit separately. Limit one pending increase; replacement cancels the prior pending record. Recheck active version at execution; old approvals require reapproval after an effective version change. The settings page shows current and pending values and activation time.
- Relative amounts use a bounded representation: literal, fraction of a named preceding leg, or an explicitly identified remaining balance. No arbitrary expression evaluation. Ambiguous “half of it” asks which amount/account; dependency cycles, missing references and unsupported expressions reject. Compute dependencies with Decimal before validation and signing.
- Equity budget, quote ID/time/expiry, unit price, quantity, maximum debit, actual mock debit and unspent budget are fixed before approval. Quantity is floor(budget / unit price) for this simulated whole-share market, with zero quantity rejected. Show the whole-share simplification. Execute only against that unexpired mock quote; never recompute quantity at execution. On expiry or changed terms, re-quote, revalidate and obtain fresh approval. Real market settlement is outside the prototype.

Provenance is derived by trusted code from source IDs, captured spans, user selections and bank lookups. Model-proposed span references are checked, not trusted as proof. Free-text bill references and pasted instructions cannot become beneficiary or amount sources; derived values preserve all dependencies and taint. Provenance supports review and policy checks, but is not a guarantee that natural language was understood correctly.

**Langfuse backend and audit — both required**

Use a real Langfuse Cloud project as the default backend. No-op instrumentation or local console logs do not satisfy completion. Use the current supported Python SDK, pin the tested version, and verify actual exported data in the project. The prior v3 assumption is superseded by current official SDK documentation; do not mix examples from different major versions. Self-hosting is a deployment choice if needed, not a replacement with no observability.

For each interaction, record workflow observations for ingest, extract, normalize, resolve, clarify, compile, semantic validation, policy, challenge, approval outcome, secondary approval, execution and receipt as applicable. Include separate LLM generation observations with model, prompt version/hash, sanitized input/output, latency, token usage when available, and cost only when pricing is known. Use pseudonymous user IDs, shared session IDs, bundle/revision IDs and consistent trace context across service calls. Resumed user turns can start linked traces within the same session; do not leave a span open while waiting for a person.

Show trace links from the evidence viewer. Publish the golden dataset and comparable extractor-only/dual-engine experiments, with exact critical-field match, false accepts/rejects, clarification accuracy, latency and cost. Record missing usage/pricing as unavailable, not zero. Keep the validator's measured contribution separate from deterministic authorization rejection metrics.

Implement explicit allowlisted exports: no API keys, cookies, auth headers, OTPs, private keys, WebAuthn assertions, full account identifiers or raw audio in Langfuse. Use synthetic model-visible data and sanitized trace payloads. Raw evidence stays in the access-controlled local audit store; define participant recording consent and retention for usability sessions. Never auto-capture arbitrary function arguments. Langfuse errors produce a visible observability health warning and bounded export/retry; they never relax authorization. Local audit failure blocks transaction commit. A real exported trace and experiment run remain submission acceptance requirements, even if offline unit tests use a fake exporter.

Audit rows include service identity, sequence, timestamps, step, event payload, correlation IDs and predecessor hash. The gateway owns writes; agent-reported events are distinguished from trusted decisions. A hash chain detects changes relative to retained history, but does not alone prove resistance to a privileged full rewrite or truncation. Replay uses captured model outputs and bank snapshots, not a claim that live model reruns reproduce identical decisions. Independent checkpoints remain a documented future enhancement.

**Stack and layout**

Python 3.12, uv, FastAPI, Pydantic, LangGraph, provider-compatible LLM client, py_webauthn, SQLite, pytest/ruff; React/Vite/TypeScript/Tailwind and SimpleWebAuthn browser helper; Langfuse SDK and backend. Use separate service credentials with least privilege, not a shared superuser token.

```
agent/        fixed graph, extraction, parsing, scoped READ/DRAFT clients
validator/    read-only semantic audit service, fixed prompt, strict result schema
gateway/      identity, conversation snapshots, validation orchestration, challenge,
              mandate/policy, approval state machine, execution, audit APIs
ledger/       mock accounts, payees/PayNow, billers, cards, equities, transactions
shared/       pure types/canonicalization, telemetry helpers, scoped API clients
web/          chat/audio, confirmation, mandate screen, evidence viewer
prompts/      version-controlled extractor and validator instructions
infra/        service images and Compose isolation; optional public deployment
evals/        golden dataset, experiment runner, measured reports
tests/        semantic fixtures, policy, lifecycle, isolation and attack tests
```

Models and providers are configurable per service (`EXTRACT_PROVIDER/MODEL`, `VALIDATE_PROVIDER/MODEL`). Verified 9 October: extractor TokenHub `hy3` (a reasoning model; needs a generous max_tokens) and validator DeepSeek API `deepseek-flash`, because only `hy3` is activated on the TokenHub account. The validator can move to TokenHub DeepSeek once activated; the vendors stay different either way. Verify exact model IDs, structured-output support, credits and latency. Preserve personal-provider development configuration separately and label evaluation runs accurately. Follow the supplied TRTC quickstart for the exact ASR SDK and credential type; do not conflate SDKSecretKey and cloud SecretKey. Stop recording on completion/cancel and handle denied microphone permissions explicitly.

TRTC ASR facts checked 9 October 2026 against the SDK READMEs (re-verify the installed version in T-00): the official Python SDK is `trtc-asr` 2.0.0, installed from the Tencent-RTC GitHub tag because it is not on PyPI despite its README (`trtc_asr.v3.SentenceRecognizer` for `POST /v3/transcribe`, `SpeechRecognizer` for WebSocket streaming). It authenticates with SDKAppID + SDKSecretKey only; the international host is `asr-intl.cloud-rtc.com`. Sentence recognition accepts wav/pcm/ogg-opus/mp3/m4a, up to 60 seconds and 3 MB; use engine `bigmodel` with an explicit `en` language hint. MVP voice uses push-to-talk sentence recognition: the browser captures 16 kHz mono PCM through an AudioWorklet and uploads a WAV clip to the gateway, which stores the same bytes as audio evidence and calls TRTC. This avoids server-side WebM conversion and keeps TRTC credentials in the gateway only. Streaming recognition with interim results is an optional enhancement after the sentence path works. TRTC credentials and recognized text are gateway-held; the agent only ever sees the stored final transcript.

TRTC Real-time AI Conversation (quickstart §3.1) is deliberately not used: it runs the LLM turn loop inside TRTC's cloud against a publicly reachable model endpoint, which would bypass the fixed graph, the gateway-owned conversation store and the validator. Standalone ASR (§3.2) keeps speech as input only. Free allowance is 10,000 credit-minutes per month for one year, shared across TRTC services; check Package Management after creating the application. Integration support: the hackathon Telegram group linked in the quickstart.

**Acceptance invariants**

- I1: agent and validator have no ledger mount/write access, execution dispatcher or signing credentials; test runtime permissions as well as imports.
- I2: execution requires valid user approval bound to the exact server-stored envelope and correct authenticated owner.
- I3: confirmation renders that same envelope; edits supersede validation and approval.
- I4: approvals and step-up proofs are fresh and single-use; concurrent/repeated requests cause at most one commit.
- I5: hard policy limits cannot be overridden by model output or secondary approval.
- I6: mandatory audit records commit with financial effects; each workflow stage has trace correlation and required Langfuse instrumentation.
- I7: trusted code assigns source lineage; untrusted reference text cannot populate critical fields through relabeling or derived-value laundering.
- I8: semantic uncertainty/errors prevent approval; a forged MATCH from the agent cannot satisfy validation. Even an actual validator forced to MATCH cannot bypass I1–I5.
- I9: ordered mock bundles are all-or-nothing; signed quantities and terms do not change at execution.

**Schedule and winning evidence**

The earlier estimate was roughly 55 hours starting 1 October. At 3–4 hours/day, 9–15 October provides only 21–28 hours. Retaining full scope does not remove that gap. The dates below are aggressive targets, not a claim that implementation fits the available hours. Update estimates after the first vertical flow; escalate schedule risk without silently dropping MVP features.

| Target date | Milestone |
|---|---|
| 9 Oct | Scope/contracts, credentials, scaffold, model/ASR/Langfuse smoke checks |
| 10 Oct | One authorized text transfer, runtime isolation and correlated Langfuse trace |
| 11 Oct | All four actions, four-leg bundles, dependencies, clarification/corrections and validator |
| 12 Oct | Voice, WebAuthn, mandate settings/cooling, risk/step-up, evidence UI |
| 13 Oct | Concurrency/attack tests, dataset experiments and usability comparison; feature freeze target |
| 14 Oct | Repair failures; record demo, architecture and actual metrics |
| 15 Oct | Clean-start rehearsal, submission assets and early submission |
| 16 Oct | Contingency and deadline; verify cutoff beforehand |

Use a ~30-case golden set across ordinary requests, corrections/ambiguity, multi-intent dependencies and adversarial content, with at least one success and one failure per action. Include real recorded voice samples separate from text-only evaluation. Compare extractor-only against extractor+validator using the same captured candidate drafts and frozen inputs; do not change gateway authorization for either condition. Publish sample counts and errors, not fabricated accuracy or universal security claims.

Conduct a small comparison with approximately five consenting testers and synthetic accounts: same tasks on a simple form/menu baseline and DCTA, alternating order. Measure completion time, interactions, success, incorrect approvals, and ability to identify recipient/source/total debit. Record at least one observation and resulting design change. Small-sample results are exploratory.

Demo: identify DBS Scenario 1; request transfer+card payment; clarify John; correct fifty to fifteen and show the changed field; approve and show receipt/evidence; show a multi-step equity example; force validator MATCH and demonstrate unsigned/tampered execution rejection; show one policy/step-up outcome; show Langfuse trace and experiment comparison. Rehearse network loss after commit, duplicate submit and misunderstood speech. Keep the video 5–8 minutes with clear mock labels.

Rogue Agent Mode runs through an explicit local demo/test harness with malicious extraction and forced-MATCH fixtures. Keep it unavailable to ordinary HTTP clients and disabled in any public deployment. It must exercise the real gateway checks, not replace the authorization verifier with a stub.

For feasibility, show a hypothetical authenticated-customer pilot using saved beneficiaries and existing bank identity/risk controls. Distinguish the full hackathon simulator from a staged real-bank rollout. External settlement, production identity, compliance and independent audit anchoring require additional work.

**Submission and workflow**

Original project built with CodeBuddy or WorkBuddy; at least three clear development screenshots/recordings; title; blurb under ten words; description covering users, pain points, architecture/prompts and measured value; 16:9 cover. Keep the planned video even though optional. A live project link is optional and earns bonus points in the handbook. Miora can create the cover but is not a mandatory tool. Confirm any source-sharing requirement before publication.

Use CodeBuddy for meaningful implementation tasks and capture evidence as work happens. Reviewer checks diffs and relevant acceptance tests. Do not commit, push, publish, create/edit `.env`, or expose credentials as part of the task checklist; `.env.example` contains placeholders only. Credentials are supplied manually through the user's chosen secret configuration. Recommendations can remain in the review without changing required scope.

References: local handbook pages 6–8, 37–40 and TRTC quickstart in `references/`; [organizer event](https://luma.com/26fqf3hy); [WebAuthn](https://www.w3.org/TR/webauthn-3/); [OWASP prompt injection guidance](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html); [Langfuse instrumentation](https://langfuse.com/docs/observability/sdk/instrumentation); [Langfuse experiments](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk).

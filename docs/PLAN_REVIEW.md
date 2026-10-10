# DBS DCTA plan review — 9 October 2026

**Decision recorded after this review:** the user retained the full MVP and explicitly required the Langfuse backend. The scope reductions, two-leg suggestion, shortened build estimate and WebAuthn fallback below remain optional recommendations, not adopted implementation requirements. `PLAN.md` v0.3, `instructions.md`, `ARCHITECTURE.md` and `CODEBUDDY.md` now define the active build contract. Line references and descriptions of the original documents below refer to v0.2; this review is preserved as historical rationale. Its final “only this review file” statement describes the earlier review turn, not the later plan edits.

The concept is a strong fit for DBS Scenario 1. Keep the separation between conversational drafting, independent validation, explicit approval, and deterministic execution. The main risks are excessive scope for the remaining time, security claims stronger than the specified mechanisms, and insufficient evidence of customer value. This is a design review, not verification of a working implementation: the visible repository contains planning and reference documents, and the task checklist is still uncompleted.

Reviewed: [plan](PLAN.md), [implementation tasks](instructions.md), [architecture](ARCHITECTURE.md), [project rules](../CODEBUDDY.md), [hackathon handbook](../references/3af2d08d-d3f0-4998-98a5-a820bd245910.pdf), and [TRTC quickstart](../references/TRTC-Conversational-AI-Hackathon-Quickstart.en.md). Page numbers below refer to PDF page order. Recommendations are review judgments, not extra organizer requirements.

**What the brief actually requires**

The DBS brief describes multi-intent parsing, targeted clarification, an independent read-only validator, explicit transaction approval, separation from ledger writes, injection defenses, risk controls, and an audit trail (pages 6–7). Scenario 1 adds KYC and basic risk control. Page 8 explicitly allows mock biometrics and a mock bank gateway and identifies the listed features as guidance. Real DBS integration is therefore unnecessary for this prototype.

The plan covers most of those capabilities conceptually. Keep a multi-action demonstration even if you remove equities: transfers plus a bill or card payment can still demonstrate the core benefit. A fixed orchestration graph with an independent validation call is reasonable; the brief does not require a fleet of autonomous agents. Its validator language says “in parallel,” so explain that this is an independent audit stage that must finish before approval, rather than claiming the current sequential graph runs concurrently.

The organizer's [event page](https://luma.com/26fqf3hy) confirms submission on **16 October 2026**, finalist announcement on 23 October, and Demo Day on 3 November. The local handbook marks Demo Day TBC. Neither source reviewed establishes an exact submission cutoff time. Submit on 15 October if possible and check the registered-participant channel for detailed DBS judging criteria; page 40 says those criteria are shared after registration.

**Fix before implementation**

| Priority | Finding and evidence | Recommended change |
|---|---|---|
| P0 | The mandate is described as a ceiling the agent can never exceed, but PLAN lines 124–130 and invariant I5 allow step-up above it. This provides an alternate route around the intended cooling period. | Define separate hard mandate limits and lower risk thresholds. Hard-limit breaches block; step-up applies only inside the active mandate. Keep the old mandate effective while an increase is pending. Treat the 12-hour period and KYC tier amounts as prototype policy choices, not verified DBS rules. |
| P0 | T-05 issues `SHA256(canonical + nonce + expiry)` but describes verification as hashing the received canonical payload alone. | Define one versioned signing-envelope format and one shared hash function. Verify the exact stored challenge, including nonce and expiry. Bind it to the authenticated user, credential, operation type, and bundle. Use different operation types for transfers and mandate updates. |
| P0 | Challenge and execution tasks do not explicitly bind the session, credential owner, source-account owner, and draft `user_id`. | Derive identity from an authenticated session. Resolve ownership and beneficiary metadata in the gateway. Reject a valid signature from a different user and any debit from an account the signer cannot operate. Do not trust caller-supplied display names. |
| P0 | A separate Python process and AST import checks do not establish OS-level isolation. The planned single SQLite file also contains audit rows that the agent must append. | Let the gateway alone mount/open the writable ledger. Give the agent authenticated read APIs and a restricted audit append interface or separate audit store. Use separate container users/mounts or equivalent access controls. Test a direct ledger-write attempt from the agent runtime. Import checks remain useful developer checks. |
| P0 | T-06 says a successful TOTP step-up “then executes,” without a specified binding or revalidation lifecycle. T-05 consumes the nonce before T-06 commits the payment. | Bind step-up to the original verified user, bundle hash, and expiry; require single use. After step-up, recheck mandate, KYC, balance, velocity, and expiry. Specify a state machine such as pending → awaiting step-up → committed/blocked/expired. Commit the terminal authorization transition and ledger changes atomically, and make retries return the original receipt without duplicate effects. |
| P1 | The validator sees only the raw transcript and final draft, although clarification responses and bank metadata change the interpretation. | Supply all original user turns, their order, and the minimum independently fetched bank metadata needed to resolve identities. Exclude extractor reasoning. Bind a successful validation result to the precise bundle version. A changed draft invalidates validation and approval. Malformed output, timeout, or uncertainty stops approval. Different model vendors are not proof of independent errors. |
| P1 | `untrusted_text` provenance is a field label; the plan does not specify who can issue it or prevent laundering through `derived`. | Assign provenance in trusted code from captured source spans and tool results, never from LLM claims. Preserve taint through derived values. Treat every string as untrusted for instruction purposes, including bank-record references. Regex filtering is supplementary; the execution boundary and approval checks carry the security guarantee. |
| P1 | The plan calls a normal web confirmation plus WebAuthn “WYSIWYS” and guarantees biometrics. | State that WebAuthn binds approval to the challenge under a trusted application UI assumption. User verification can use a PIN; it is not proof of Face ID. Ordinary WebAuthn does not make a React overlay browser-trusted transaction UI. Keep real WebAuthn if it works quickly; otherwise clearly label the allowed mock. |
| P1 | The hash chain is called a regulatory-grade audit source, while the architecture diagram adds signed checkpoints with no implementation task. | Call it a tamper-evident prototype log. A writer able to rewrite the entire chain can recompute hashes; tail deletion also needs an external expected head to detect. Implement independent checkpoints if claiming stronger integrity, or narrow the claim. Record actual audio artifacts, transcript revisions, model outputs, validation, canonical payload, approval evidence, and receipt. |

For the WebAuthn distinction, see the [W3C WebAuthn specification](https://www.w3.org/TR/webauthn-3/) and the [W3C Secure Payment Confirmation explanation](https://github.com/w3c/secure-payment-confirmation/blob/main/explainer.md). This review does not establish legal non-repudiation or regulatory compliance.

Other correctness details to settle: mandate expiry and effective time; Singapore calendar-day aggregation; velocity counted across transactions and bundles; cumulative debits within one bundle; simultaneous attempts against the same balance/limit; and idempotency scoped to the user and payload. The six-transfer attack must use several valid bundles to exercise velocity rather than merely fail the four-leg bundle cap.

**Narrow the build to a convincing customer journey**

Recommended customer: an existing, KYC-verified retail banking customer completing routine payments on a phone. The value proposition is reducing menu navigation while making the amount, recipient, and source account easy to verify. Suggested blurb: **“Speak your payments. Verify every detail. Stay in control.”** (Nine words.)

| Keep for submission | Defer unless already working |
|---|---|
| Voice and text input; visible recording state; editable transcript | A fully integrated real-time voice conversation stack |
| Saved-payee transfer plus one card/bill payment; maximum two legs for the first demo | Equities, PayNow-specific routing, cross-border flows, arbitrary dependencies |
| Targeted clarification and an independent semantic check | A large synonym/relative-amount language engine |
| Exact confirmation details and gateway-enforced authorization | Signed intent-pack registries, agent-signed drafts, gateway-signed receipts |
| Mock KYC states: verified, unverified, expired; hard limits and velocity | Elaborate KYC onboarding and mandate settings screens |
| A transaction-bound, explicitly simulated secondary approval | Multiple callback/TOTP paths |
| Local audit viewer and reproducible attack cases | Two observability backends and production-style audit storage |
| Three CodeBuddy/WorkBuddy evidence captures and a reliable recorded demo | Cloud hosting if it jeopardizes the submission |

Keep real WebAuthn only with a short integration timebox and a tested demo device. The handbook allows mocks, so the existing “never cut WebAuthn” rule is unnecessarily rigid. Preserve exact-payload approval and gateway rejection behavior even when demonstrating a mock biometric ceremony; do not imply the mock provides real biometric assurance.

Prefer one ASR implementation early. The supplied quickstart points to TRTC standalone ASR, SDKAppID/SDKSecretKey credentials, and a [SDK guide with Python links](https://github.com/Tencent-RTC/trtc-asr-sdk-nodejs/blob/main/README.en.md). The plan instead names `tencentcloud-speech-sdk-python`; verify which product and credentials you are using before implementation. Capture actual audio separately if using browser transcription, since receiving transcripts alone does not implement the brief's audio audit requirement.

If equities remain, define the signed quantity, quote timestamp/expiry, price constraint, debit, and residual cash before approval. T-06 currently computes quantity during execution, which can change the result after the user reviewed it. Clearly label internal SQLite all-or-nothing execution as a mock; external payments and securities orders would require a different failure/reconciliation design.

**Replace the stale schedule**

The current plan budgets 55 hours starting 1 October. As of 9 October, 9–15 October provides about **21–28 hours** at the stated 3–4 hours/day, plus whatever submission-day buffer is available. If substantial implementation exists elsewhere, reuse it; it is not visible in this checkout. These are proposed work budgets, not guarantees.

| Date | Deliverable | Budget |
|---|---|---|
| 9 Oct | Resolve the P0 contracts; prove one model call and one audio path; capture first CodeBuddy evidence | 3 h |
| 10 Oct | One text transfer through draft, approval, gateway, and mock ledger | 4 h |
| 11 Oct | Clarification, full-conversation validation, second payment action, two-leg confirmation | 4 h |
| 12 Oct | Voice integration, KYC/limits/velocity, bound mock step-up, audit view | 4 h |
| 13 Oct | Attack tests, evaluation set, brief user comparison; freeze features | 4 h |
| 14 Oct | Fix failed cases; record demo; prepare description, diagram, and cover | 4 h |
| 15 Oct | Clean-start rehearsal, verify submission assets, submit | 3 h |
| 16 Oct | Contingency only | As available |

If the authorized text transfer does not work by 10 October, cut optional infrastructure immediately. APM, signed pack registries, equities, and extensive mandate management should not consume the remaining customer-flow time.

**Demo and evaluation that support the judging story**

Use a five-to-six-minute recording:

1. State “DBS, Scenario 1” and show the customer problem.
2. Say “Send fifty bucks to John and pay my card balance.” Show two Johns and ask one short clarification. Present two precise legs, source accounts, total debit, and an edit/cancel route.
3. Approve the bundle; show the balance changes and receipt.
4. Alter the recipient after approval; show gateway rejection and unchanged balances. Try an unsigned execution separately to demonstrate that the gateway remains effective even if the validator fails.
5. Show a KYC/limit block or velocity block and a separate within-mandate step-up case.
6. Show a concise measured-results table, the trust boundary, and how CodeBuddy contributed.

Evaluate about 30 cases grouped into ordinary requests, ambiguity/corrections, multi-intent requests, and adversarial inputs. Retain representative recorded speech samples so voice performance is measured separately from text parsing. Report exact critical-field match, required clarifications handled correctly, benign requests incorrectly frozen, attack rejection counts, and end-to-end latency. “All tested attacks blocked” is a valid measured claim if observed; “all attacks are impossible” is not.

Compare the same payment tasks against a small conventional form/menu baseline, using a few consenting testers if available. Measure completion time, interaction count, task success, and whether users can identify the recipient and total debit before approving. Label this a small usability study, not production evidence. Security is already prominent in the plan; customer impact, accessibility, and measured usability need equal attention in the presentation.

**Submission details to make explicit**

Page 37 requires an original project built using CodeBuddy or WorkBuddy, a title, a blurb under ten words, a project description covering users/scenario/design/business value, conversation evidence, and a 16:9 cover. The conversation-history row specifies a minimum of three screenshots/screen recording; collect at least three clear development screenshots rather than relying only on CLI logs. The 5–8 minute video is optional, and a live project URL is optional with bonus points. Miora is useful for the cover but is not identified as the only permitted way to produce it.

Keep the planned video because it makes the transaction and rejection flows reviewable. Include exact reproduction instructions, synthetic data, mock-service disclosures, and known limits. The DBS pages do not explicitly demand a public GitHub repository; other tracks' source-code requirements should not be imported automatically. Check the submission form before publication, and preserve the repository's current no-push instruction.

**Synchronize the design documents before code generation**

The architecture diagram already includes intent packs, release keys, agent signatures, ledger order signatures, signed receipts, and signed audit checkpoints absent from the task plan. Remove these from the MVP diagram or add explicit implementation and validation tasks; otherwise the diagram overstates the delivered system. Also reconcile the broader ledger-import ban in CODEBUDDY.md with the read helpers in T-04, and move nonce issuance out of the agent's DRAFT registry into the gateway.

Replace “same transcript gives the same path” with “execution and policy decisions are deterministic for fixed validated inputs and state.” Live LLM outputs and live account state can change. Replay should consume captured outputs and state snapshots, not depend on rerunning models identically. Verify the configured TokenHub models and structured-output behavior with a small integration call; documentation or model names alone do not confirm account access, free credits, or measured latency.

Only this review file was added. The original plan, architecture, instructions, and references were not modified. No implementation or tests were run.

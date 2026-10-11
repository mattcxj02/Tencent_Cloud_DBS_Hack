# DCTA build summary: what was done, how and why

Covers 9–11 October 2026, from plan review to a working, tested, documented prototype. Status as of 11 Oct: T-00 to T-14 done and verified, T-15 (submission package) drafted, owner actions pending (see the end).

## 1. Starting point

On 9 Oct the repository held only planning documents: `PLAN.md` v0.3, `instructions.md` (tasks T-00 to T-15), `ARCHITECTURE.md`, `CODEBUDDY.md`, plus your plan review and the TRTC quickstart.

**Plan review findings, fixed before any code:**
- TRTC's Python ASR SDK is real but not on PyPI. It is installed from Tencent's GitHub tag `v2.0.0`.
- Voice uses push-to-talk sentence recognition. The browser records 16 kHz WAV, and the gateway stores it, then transcribes it. TRTC's "Real-time AI Conversation" is not used, because it would put an LLM loop outside our fixed workflow and validator.
- The browser talks only to the gateway; the agent and validator are internal.
- WebAuthn (T-05) no longer waits for Docker isolation (T-04), so the first payment flow was not blocked on infrastructure.
- Mock stock quotes expire after 5 minutes, so the 120-second approval window fits inside a quote.
- `docs/STATUS.md` was created as the live to-do and task board.

## 2. How the work was done

**Phase 1: CodeBuddy (T-00 to most of T-05).** Each task was sent to the CodeBuddy CLI in headless mode with a detailed prompt. CodeBuddy was blocked from reading `.env`, committing, pushing and printing Compose config. I (Claude Code, as reviewer) then read the code, reran every check and fixed what was wrong.

CodeBuddy hit four problems:
- In T-00 its Bash allowlist didn't apply, so it wrote files but ran nothing. I fixed version pins (Langfuse was capped below 1.0), the TRTC and Langfuse API calls, and ran all smokes.
- T-04 ended on a network error at its last step, after the work was done.
- In T-05 it put itself into plan mode and stalled.
- Mid-T-05 it hit "429 Credits exhausted", and you confirmed the credits would not reset.

The 8 session logs and prompts are preserved in `docs/evidence/codebuddy/logs/`. A scan found no secrets in them.

**Phase 2: Claude subagents (rest of T-05 to T-15).** At your request, the remaining work was split across parallel Claude subagents. Each owned distinct folders (for example `gateway/`, `agent/`, `validator/`, `web/`, `evals/`), so they could not overwrite each other. The interfaces between them (routes, request and response shapes) were written into their instructions up front.

I reviewed every report, read the security-critical code, reran tests and live checks, and fixed issues they missed or couldn't reach. Twice the subagents were cut off by usage limits; they were resumed with their context intact.

**Why review mattered.** Every builder, CodeBuddy and subagents alike, reported "done" at least once with real gaps behind it:
- a vacuous test;
- a missing ownership check;
- a passkey that signed the wrong challenge;
- an approval that could be replayed.

The rule throughout was: nothing is marked done until checks have actually run.

## 3. What was built, task by task

| Task | What | Key reviewer findings and fixes |
|---|---|---|
| **T-00** Scaffold + smokes | uv/FastAPI services, React/Vite web, Makefile, `.env.example`, live smokes for LLMs, TRTC and Langfuse | TokenHub key rejected at first; your replacement worked. Only `hy3` is activated on TokenHub, so the validator uses DeepSeek directly (`deepseek-flash`), keeping two vendors. `hy3` is a reasoning model and returned empty answers at a small token budget, so budgets were raised. Langfuse's legacy read API returns 410 for new organisations, so reads use the v2 observations API |
| **T-01** Schemas + signing | Money as exact decimals, strict schemas, relative amounts ("half of that"), source tracking (lineage), and one canonical signing function `SHA256("DCTA:approval:v1\0" + canonical bytes)` | The envelope didn't check that its user and conversation matched the bundle inside it. Free text (e.g. a bill reference) had no source type, so the injection tests proved nothing. The signing bytes weren't pinned. All three fixed; the pinned challenge was re-derived independently |
| **T-02** Mock bank | SQLite ledger in integer cents; seed with Matthew, Ana, two Johns, Mum/Ah Ma, a sanctioned payee, PayNow, SP Group, a card and SGX quotes; user-scoped read API; signed tokens for the agent | Search wildcards (`%`) matched everything, and a placeholder signing secret could be copied as-is. Both fixed |
| **T-03** Conversations + audit | Append-only turns, revisions, corrections that cancel pending work, audio evidence, a hash-chained audit log committed with money movement | The agent could write audit events into another user's conversation; this is now bound to its token. Audit now refuses to run outside a transaction. A test meant to prove the agent can't fake user turns was hollow and was rewritten |
| **T-04** Isolation | Separate container images (the agent and validator images contain no gateway or ledger code), non-root users, read-only filesystems, separate networks, in-container probes | Each model container received both LLM keys; now each gets only its own. The probe target passed even when probes failed, and its health wait was broken. Result: 26/26 probes passed in each container, and no `.env` in any image |
| **T-05** Passkeys + approval | One-time enrollment codes, passkey login, sessions, frozen drafts, signing envelopes, exact-payload approval | Finished after CodeBuddy ran out. Fixed: the approval page signed a *login* challenge and showed no amounts; a bad signature cancelled the approval; approvals could be replayed (Apple passkeys don't count); no credential-ownership check; corrections didn't cancel approvals; issuance skipped validation; the client chose the operation type. **Lineage was redesigned** (`gateway/evidence.py`): the agent must point to the user's exact words, and the gateway checks the words exist and name exactly one bank record |
| **T-06** Policy + execution | KYC tiers, mandate limits, sanctions, ownership, funds, card overpayment, velocity, Singapore-day limits, step-up over SGD 500 or on scam phrases; one-transaction commit; idempotent receipts | Reviewed as solid. Stable across 3 full runs |
| **T-07** Step-up + mandates | One-time step-up code bound to the exact authorization, used up inside the commit; simulated operator "phone"; signed mandate changes (tighten immediately, loosen after 12 h) | Reviewed as solid |
| **T-08** Langfuse | Telemetry helpers with an allowlist and 5 layers of redaction; trace propagation across services; health reporting; wired into all 3 services | Independent secret-canary probe clean; a test fixture guarantees tests never export real traces |
| **T-09** Agent | Fixed LangGraph workflow: `hy3` extracts *quoted words only*; a shared amount parser ("fifty bucks", "a grand", "the rest"); bank resolution; clarifying questions; drafts with evidence. Gateway orchestration route runs agent → validator | Added: the gateway parses the cited amount words with the same parser ("fifteen" can't back 50.00), and full names beat shared first names. Live testing showed a 15 s agent timeout was too short (`hy3` took 17–29 s); it is now 45 s |
| **T-10** Validator | Independent DeepSeek validator with a fixed prompt and strict output checks (anything malformed → ERROR, never MATCH); gateway stores results tied to the exact bundle, conversation and bank labels | Corrections now cancel validation results immediately. The token budget was raised after 2 eval runs used everything on reasoning |
| **T-11** Web UI | Chat, clarification chips, before/after corrections, a confirmation built from the signed envelope, passkey approval, step-up, receipts, mandate page, evidence timeline, baseline form for usability | Checked: signs the envelope's own challenge, no raw HTML, money in integer cents |
| **T-12** Voice | Browser AudioWorklet WAV capture; gateway stores audio, then calls TRTC; the client-supplied transcript route removed | TRTC heard "Ah Ma" as "Amma". **Fixed with hotwords** built from the user's own payee names, now heard correctly |
| **T-13** Attacks | Rogue Agent Mode: malicious extractor + validator forced to MATCH vs the real gateway; 46 attacks | All 46 controls held. Fixed: citing "Mum" inside "Mumbai"; model-written reference text on the confirmation; malformed legs crashing (500); free-text errors |
| **T-14** Evaluation | 35-case golden set, live extractor vs extractor+validator, Langfuse dataset + experiments, usability kit | Measured honestly; see results below |
| **T-15** Submission | README, SUBMISSION.md, demo script, judge Q&A, 16:9 cover, CodeBuddy summary | Drafted using only measured numbers |

## 4. Why it is designed this way

- **Models draft; they never authorize.** Both LLMs can be wrong or manipulated, so neither has payment tools, ledger access or keys. Only deterministic gateway code moves money, and only after a passkey signs the exact stored payment.
- **Quoted words, not model judgments.** The extractor copies the user's words. The gateway checks those words exist in the user's own turns and name exactly one bank record, so a model can't invent a payee or inflate an amount without it showing.
- **Two vendors.** The validator uses a different provider from the extractor to reduce correlated mistakes. A MATCH only allows the confirmation screen.
- **Fail closed everywhere.** Timeouts, malformed output, stale data or any doubt mean no approval. Even a valid step-up code can't override a hard limit.
- **Evidence over claims.** Every result in the submission comes from a run recorded in `docs/evidence/`.

## 5. Measured results

| What | Result | Source |
|---|---|---|
| Live end-to-end runs (real models, real Langfuse, software passkey over the real API) | Two committed, one through step-up | `docs/evidence/E2E-live.md` |
| Extraction, 35 live cases | 34/35 correct outcomes, 0 unsafe drafts; median about 9 s per agent turn | `docs/evidence/T-14-results.md` |
| Validator, same captured drafts | 0 of 31 wrong drafts accepted (31 without it); 5 of 25 correct drafts held back, mostly ones containing injected text | `docs/evidence/T-14-results.md` |
| Attack matrix | 46/46 controls held | `docs/evidence/T-13-attack-matrix.md` |
| Container isolation | 26/26 probes per model container; no `.env` in images | `docs/evidence/T-04-isolation.txt` |
| Voice | Correct transcripts in about 0.3–0.5 s; "Ah Ma" fixed with hotwords | `docs/evidence/T-12-voice.md` |
| Tests | 1,149 passing; lint and format clean; web build passes | local runs |

## 6. Known limitations

- **Simulation.** Bank, PayNow, SGX, KYC, sanctions and the step-up channel are simulated, with synthetic data.
- **Latency.** Reasoning-model turns take about 9 s median, up to 29 s.
- **Validator false holds.** It holds back about 20% of correct drafts that contain injected text, and it can't check "the rest" amounts because it isn't given balances.
- **Residual attack risks:**
  - correct quoted words don't prove meaning ("Mum lent me 500");
  - an undeclared reference isn't checked against the amount;
  - the audit chain can't detect a privileged full rewrite.
- **Untested by a person so far:** a real browser with Touch ID and a microphone, and the usability sessions.

## 7. What is left (owner actions)

1. Add `VALIDATOR_SERVICE_TOKEN`, `OPERATOR_TOKEN` and `STEP_UP_SECRET` to `.env`, each at least 32 random characters. Generate them with `python -c 'import secrets; print(secrets.token_urlsafe(48))'`.
2. Do a real-browser rehearsal with Touch ID and a microphone, following `docs/DEMO_SCRIPT.md`.
3. Capture at least 3 CodeBuddy screenshots (see `docs/evidence/codebuddy/README.md`).
4. Run about 5 usability sessions with `evals/usability/`.
5. Make 2–3 Langfuse traces public, or show them in the video.
6. Optionally re-run `make isolation-probe` with Docker running.
7. Record the video, confirm the exact cutoff time, and submit, ideally by 15 Oct.

Nothing has been committed or pushed yet.

# DCTA status and to-do

Last updated: **13 Oct 2026** · Phase: **Build complete (T-00–T-14) + improvement plan phases 1–6 merged. Remaining: owner checks (browser, mic, usability), CodeBuddy screenshots, video, submission** · CodeBuddy credits exhausted · Days to submission target (15 Oct): **2** · Hard deadline: 16 Oct

Scope contract: full MVP in [PLAN.md](PLAN.md) v0.3. Task prompts and acceptance: [instructions.md](instructions.md). Update this file whenever a task changes state; mark a task `[x]` only when its acceptance checks have actually passed.

Status key: `[ ]` pending · `[~]` in progress · `[x]` done · `[!]` blocked

## Key dates

| Date | Event |
|---|---|
| 15 Oct | Planned submission (one day of buffer) |
| 16 Oct | Submission deadline: exact cutoff time still unknown |
| 23 Oct | Finalists announced |
| 3 Nov | Demo Day (TBC) |

## Improvement plan (12–13 Oct 2026)

All six phases of [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) are implemented and merged into the working tree (not committed). Code by Codex CLI (`gpt-6.1-sol`) in separate git worktrees, one phase each; CodeBuddy started Phase 5 but ran out of credits before editing; the lead session reviewed every diff, fixed defects, merged and ran the checks. Backups: `refs/backup/pre-improvement`, `after-phase1`, `after-wave1`, `after-phase4`.

- [x] **Phase 1 Readiness:** bounded non-model preflight (token, authenticated validator, agent config) before extraction; missing settings reported by name only; `make dev` cleans up its processes, scoped `make stop`; stale `make demo` removed. Extractor timeout 40 s, no SDK retries.
- [x] **Phase 2 Faster extraction:** `EXTRACT_REASONING_EFFORT` (per provider/model; unset = old request). `no_think` alone failed the gate (obeyed a fake "app:" line, `adv-07`); extractor prompt **v2** adds one rule against that. Result: extractor **p50 2.0 s / p95 3.2 s** (was 10.6 / 27.4 s same day), 33–34/35 over three trials, 0 unsafe drafts ([T-14-no-think](evidence/T-14-no-think.md)). `mimo-v2.6-flash` probed and rejected (8–24 s).
- [x] **Phase 3 Clarification:** grouped questions; gateway-generated choice IDs bound to question, field, leg and revision; a clicked answer updates the draft with no extra extractor call; free text falls back to extraction.
- [x] **Phase 4 Deadlines and reuse:** one 90 s drafting deadline propagated to agent and provider; single-flight per owner+revision; late results never published; validation-only retry reuses an unchanged frozen draft.
- [x] **Phase 5 Progress:** owner/revision-scoped progress endpoint polled about once a second (bounded); real stages and elapsed time; one automatic redraft when the conversation changed; approval recovery fetches an existing receipt first.
- [x] **Phase 6 Evidence and enrollment:** frozen calculation evidence for derived amounts, bound into validation hashes; validator prompt **v2**; enrollment code consumed only on successful registration (begin → cancel → begin works), five begins per code. Validator false rejects **5/25 → 0/24**, wrong drafts accepted still **0** ([findings](evidence/phase6-validator-findings.md)).
- [x] **Lead fix:** the validator accepted an Ah Ma → Mum swap 2/4 because it never saw that Ah Ma is a separate payee; bank metadata now lists every saved payee of the same kind (swap rejected 4/4).
- [x] Checks on the merged tree (13 Oct): `make test` **1,359 passed**; ruff clean; `tsc` and `npm run build` pass; Rogue Agent Mode **46/46**; fake evaluation 35/35; live golden set 33/35, 0 unsafe; follow-up suite 6/6.
- [ ] Not verified: browser fixture tests (`web/tests/`, need a browser), real-browser walkthrough of the new clarification/progress/enrollment UI, live Langfuse read-back, fresh container probe, complete voice-to-ready timing.

## Owner actions (not code; unblock T-00)

- [x] TRTC application credentials in `.env` (`SDKAppID`, `SDKSecretKey`). Still check the free allowance in Package Management.
- [x] TokenHub key works (`tokenhub_key`); only `hy3` is activated on the account
- [ ] Optional: activate `deepseek-v4-flash` under TokenHub Online Inference Service if you want the validator on TokenHub too (then set `VALIDATE_PROVIDER=tokenhub`, `VALIDATE_MODEL=deepseek-v4-flash`)
- [x] DeepSeek key in `.env` (`deepseek_key`); validator uses `deepseek-flash` directly
- [x] Langfuse Cloud keys in `.env` (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`)
- [!] **CodeBuddy credits exhausted (429) on 10 Oct.** Briefly available again on 13 Oct: a Phase 5 session (`01a130da-a414-7dda-ad70-6e589db2854a`) read the plan, made a task list and started sub-agents before hitting 429 again. The submission still asks for ≥3 CodeBuddy/WorkBuddy screenshots (0 so far): capture saved sessions from `codebuddy --serve --open`
- [ ] Real-browser passkey check: `make seed`, `make enroll-code`, `make dev`, `cd web && npm run dev`, open http://localhost:5173, enroll with the code, sign in (Touch ID)
- [ ] For the step-up demo add `OPERATOR_TOKEN=<random ≥32 chars>` and `STEP_UP_SECRET=<random ≥32 chars>` to `.env`
- [ ] Configure the same `VALIDATOR_SERVICE_TOKEN=<random ≥32 chars>` in gateway and validator (never agent), restart both and check `/api/ready`; absent/short/mismatched tokens now fail before extraction
- [x] `EXTRACT_REASONING_EFFORT=no_think` set in `.env` (13 Oct)
- [ ] Run `make upgrade-db` on the real database before restarting (Phase 6 tables; keeps passkeys and history), then restart all services
- [ ] Real-browser check of the new UI: clarification choice buttons, progress stages, enrollment cancel → retry, voice correction while drafting; run the browser fixtures in `web/tests/README.md`
- [x] Credentials entered by the user (Claude/CodeBuddy never read or edit `.env`; `.env.example` mirrors these names)
- [x] Start Docker Desktop (running; T-04 Compose + isolation probes pass)
- [ ] Run ~5 usability sessions with `evals/usability/protocol.md` (baseline page `#/baseline` vs chat); enter results in `results_template.csv`
- [ ] Make 2–3 key Langfuse traces public (or show them in the video); the project is private
- [ ] Capture ≥3 CodeBuddy screenshots per `docs/evidence/codebuddy/README.md` (logs are preserved in `docs/evidence/codebuddy/logs/`)
- [ ] Confirm the exact submission cutoff time and request the DBS preliminary rubric in the participant channel
- [ ] Recruit about 5 consenting testers for the 13 Oct usability comparison
- [ ] Decide whether to run the TRTC Skills installer (`npx @tencent-rtc/trtc-agent-skills add`; optional, it changes IDE config)

## Task board

| ID | Task | Target | Status | Evidence / notes |
|---|---|---|---|---|
| T-00 | Scaffold + model / ASR / Langfuse smoke checks | 9 Oct | [x] | All checks and live smokes pass. See [evidence](evidence/T-00-smoke.md) |
| T-01 | Schemas, lineage, canonical signing envelope | 10 Oct | [x] | CodeBuddy CLI + reviewer fix round (envelope↔bundle user/conversation binding, `reference_text` lineage kind, pinned golden bytes). 138 tests; challenge independently re-derived from the spec formula |
| T-02 | Gateway-owned mock bank + scoped read APIs | 10 Oct | [x] | CodeBuddy CLI + reviewer fixes (LIKE wildcards escaped; weak/placeholder delegation secret rejected). 202 tests; seed verified idempotent. Interim HMAC delegation tokens until T-05 sessions |
| T-03 | Conversation snapshots + prototype audit chain | 10 Oct | [x] | CodeBuddy CLI + reviewer fixes (agent audit events bound to the token's conversation and owner; `append_audit` refuses to run outside a transaction; canonical payloads; impersonation test made real). 249 tests. User routes fail closed until T-05 |
| T-04 | Runtime isolation (Compose, separate users and mounts) | 10 Oct | [x] | CodeBuddy CLI (session ended on a network error at its last README edit) + reviewer fixes: each container now gets only its own LLM key (+ test), `make isolation-probe` fails on any FAIL or `.env` in an image, health wait uses `up --wait`. Re-run by reviewer: 26/26 probes PASS in agent and validator, no `.env` in any image, 274 tests. See [evidence](evidence/T-04-isolation.txt) |
| T-05 | Identity, frozen drafts, exact-payload WebAuthn | 10–12 Oct | [~] | Server side done; 324 tests. CodeBuddy built most of it, entered plan mode, then ran out of credits mid-task; reviewer finished it and fixed: fail-closed issuance, credential ownership, single-use approval under a write lock, bad assertion no longer cancels, revision/mandate supersession, supersession registration, operation fixed server-side, evidence-based lineage (`gateway/evidence.py`), equity quote checks, token↔conversation binding, Approve page signing a login challenge instead of the envelope and not showing amounts, `/api` proxy, passkey (resident key) required. **Pending:** real-browser enroll + sign-in on the laptop now; real-browser approval after T-06/T-09/T-10 |
| T-06 | Hard policy, atomic execution, idempotency | 10–11 Oct | [x] | Claude subagent, reviewed: deterministic policy with stable reason codes (KYC tier, mandate, sanctions, ownership, funds, card overpayment, velocity, SG-day limits; STEP_UP over 500 or scam phrases; BLOCK beats STEP_UP), one-transaction commit with full re-checks, idempotent receipts + lost-response GET, money routes on threadpool. 849 tests ×3 runs stable. Known: a failed commit keeps approval evidence, so the user requests a fresh envelope |
| T-07 | Secondary approval + mandate management | 12 Oct | [x] | Claude subagent, reviewed: step-up code bound to user/authorization/envelope hash, HMAC verifier only, 5 attempts, consumed inside the commit lock (hard blocks still win); operator console needs `OPERATOR_TOKEN` and refuses user sessions/agent tokens; signed `update_mandate` with immediate tighten/pause, 12 h cooling for loosening, one pending change, mixed changes rejected. 57 tests ×4 runs stable. UI still to build (T-11) |
| T-08 | Langfuse backend + trace propagation | 9 → 13 Oct | [x] | Helpers + wiring in gateway/agent/validator (subagents, reviewed). Live: one trace spans gateway → agent stages → extractor generation; validator traces with prompt hash; secrets canary clean; tests never export. Datasets/experiments in T-14. See [T-08](evidence/T-08-telemetry.md), [E2E](evidence/E2E-live.md) |
| T-09 | Extraction, dependencies, corrections (LangGraph) | 11 Oct | [x] | Agent + gateway orchestration `POST /api/conversations/{id}/draft` (token → agent → validator). Live E2E: two clarifications then ready/MATCH. Agent timeout raised to 45 s after a live 17 s turn. See [T-09](evidence/T-09-extract.md), [E2E](evidence/E2E-live.md) |
| T-10 | Independent semantic validator + result binding | 11 Oct | [x] | Validator service + gateway coordinator (subagents, reviewed). MATCH counts only for the exact bundle hash, owner, conversation revision/hash and bank labels; MISMATCH freezes the draft, UNCERTAIN asks, ERROR retries ≤3 and never becomes MATCH; corrections supersede results immediately; forced-MATCH attacks still rejected by the gateway; validator traces wired. Live DeepSeek 7/7 (service smoke) |
| T-11 | React confirmation, mandate, evidence views | 12 Oct | [~] | UI built (subagent; reviewed: signs the envelope's own challenge, no raw HTML, cents arithmetic); `npm run build` passes. **Pending:** real-browser walkthrough with Touch ID + mic (owner) |
| T-12 | Voice capture + TRTC sentence ASR | 12 Oct | [~] | Gateway + browser capture done (subagent; reviewed). Live TRTC transcripts correct; lead added user-specific hotwords ("Ah Ma" was heard as "Amma"). Audio stored before ASR; client-supplied ASR route removed; 49 tests. See [evidence](evidence/T-12-voice.md). **Pending:** UI integration (T-11) and a manual browser mic check |
| T-13 | Attacks, concurrency, failure rehearsal | 13 Oct | [x] | Rogue Agent Mode (subagent; reviewed): 46 attacks, 46/46 controls held. Fixed: word-fragment citations ("Mum" inside "Mumbai"); lead then fixed model-written references (now must be user words or the saved bill reference), malformed legs (500 → `LEG_INVALID`) and free-text freeze errors. Residual: EX-2, EX-8, AU-1. See [matrix](evidence/T-13-attack-matrix.md). Container probe not re-run (Docker off; 10 Oct result 26/26 ×2) |
| T-14 | Golden set, Langfuse experiments, usability | 13–14 Oct | [~] | 35 live cases: 34/35 correct outcomes, 0 unsafe drafts; validator on same drafts: 0/31 wrong accepted, 5/25 correct blocked (UNCERTAIN, mostly injected text); Langfuse dataset + 2 runs. Validator budget raised to 4096 after 2 truncations. See [results](evidence/T-14-results.md). **Pending:** ~5 usability sessions (kit in `evals/usability/`) |
| T-15 | Submission assets, video, optional deploy | 14–15 Oct | [~] | Drafted (subagent; reviewed): [README](../README.md), [SUBMISSION](SUBMISSION.md) (title, 9-word blurb, description from measured evidence), [DEMO_SCRIPT](DEMO_SCRIPT.md), [JUDGE_QA](JUDGE_QA.md), [cover](cover/cover.png), [CodeBuddy summary + preserved logs](evidence/codebuddy/README.md) (secret scan clean). **Pending:** T-13 numbers into SUBMISSION/QA/script; owner: screenshots, browser rehearsal, video, submission |

**Milestone checks:** 9 Oct: scaffold and smokes · 10 Oct: **one authorized text transfer end to end** (the first real schedule signal) · 11 Oct: all four actions, validator · 12 Oct: voice, WebAuthn, mandates · 13 Oct: freeze · 14 Oct: video · 15 Oct: submit.

## Submission evidence checklist

- [ ] CodeBuddy/WorkBuddy development screenshots or recordings: **0 / 3 minimum** (sessions ran headless; capture them from `codebuddy --serve --open`; CLI logs kept as supplementary evidence)
- [ ] Real Langfuse trace link (extraction → validation → policy → execution)
- [ ] Langfuse dataset + extractor-only vs extractor+validator experiment runs
- [x] Attack matrix (attack, expected control, observed result): 46/46, rerun 13 Oct
- [x] Isolation probe output from containers (T-04: 26/26 PASS agent + validator; see [evidence](evidence/T-04-isolation.txt))
- [ ] Usability comparison notes (about 5 testers)
- [ ] Title, blurb under 10 words, description, 16:9 cover
- [ ] 5–8 minute demo video
- [ ] Clean-start rehearsal

## Blockers and risks

| Risk | Status | Mitigation |
|---|---|---|
| Full scope needs more hours than the ~21–28 left | Open | Track hours below; if the 10 Oct transfer milestone slips, raise it with the user immediately. Scope changes are the user's call. |
| Live credentials not yet supplied | Open | Owner actions above; smokes record "missing credential" rather than passing |
| hy3 is a reasoning model (~9–11 s median with reasoning on) | Mitigated | `EXTRACT_REASONING_EFFORT=no_think` + extractor prompt v2: p50 ~2 s, 0 unsafe drafts over three trials. If unset, the old slow behaviour returns |
| WebAuthn on phone needs an HTTPS origin | Accepted | Record the demo on the laptop (localhost); a hosted deploy needs re-enrollment |
| ASR accuracy on Singapore names and aliases ("Ah Ma") | Open | Editable transcript; include such clips in the T-14 voice samples |

## Decisions log

| Date | Decision |
|---|---|
| 30 Sep | Python backend, React frontend, Langfuse Cloud, TokenHub models; local commits only, no push |
| 9 Oct | Plan v0.3: full MVP kept; Langfuse required; review scope cuts not adopted |
| 9 Oct | Voice = TRTC standalone sentence ASR via Python `trtc-asr`, with browser-side 16 kHz WAV capture. Real-time AI Conversation not used (it would bypass the fixed graph and validator). |
| 9 Oct | Browser talks only to the gateway; agent and validator are internal services |
| 9 Oct | T-05 no longer waits on T-04; isolation lands in parallel, required before T-13 |
| 9 Oct | LLMs: extractor TokenHub `hy3`; validator DeepSeek API `deepseek-flash` (TokenHub DeepSeek not activated). Provider is per-service config |
| 10 Oct | CodeBuddy out of credits for good: build the rest with Claude subagents in parallel, each owning distinct folders; the lead reviews, integrates and owns STATUS/pyproject/plan docs |
| 10 Oct | Agent proposals must cite the user's words for amount, recipient and source; the gateway verifies location and that the words name exactly one bank record (`gateway/evidence.py`). Ambiguous names ("John") are rejected until the user disambiguates |
| 10 Oct | Every project folder has a README.md describing its purpose; tasks keep them current (rule in CODEBUDDY.md) |
| 9 Oct | `trtc-asr` installed from GitHub tag v2.0.0 (not on PyPI). Langfuse SDK 4.17; reads use v2 observations API (legacy API 410 for this org) |
| 12 Oct | Improvement plan phases 1–6: implemented by Codex CLI in per-phase git worktrees, merged and reviewed by the lead; Phase 5 attempted in CodeBuddy first (out of credits) |
| 12 Oct | Extractor default request stays unchanged when `EXTRACT_REASONING_EFFORT` is unset; operator sets `no_think` in `.env`. Extractor prompt v2 and validator prompt v2 adopted after live evaluation |
| 10 Oct | T-04: Compose with two bridge networks (agent_net, validator_net); neither marked `internal` so both have outbound LLM egress. Agent/validator read-only rootfs + tmpfs /tmp; gateway has /data volume only. Images built from `uv sync --frozen --no-dev` with venv at `/app/.venv` so shebangs match runtime path |

## Hours log

Budget: about 3–4 h/day, 21–28 h from 9–15 Oct.

| Date | Hours | Done |
|---|---|---|
| 9 Oct | | Plan review round 2; STATUS.md; T-00 scaffold, checks, ASR + Langfuse smokes |
| 10 Oct | | T-00 LLM smoke (hy3 + deepseek-flash); T-01 schemas/signing; T-02 mock bank + read APIs (CodeBuddy + review fixes); T-03 conversations + audit chain; T-04 runtime isolation (reviewer re-ran probes: 26/26 PASS ×2, 274 tests); per-folder READMEs |

# DCTA status and to-do

Last updated: **11 Oct 2026** · Phase: **Build complete (T-00–T-14). Remaining: owner checks (browser, mic, usability), CodeBuddy screenshots, video, submission** · CodeBuddy credits exhausted · Days to submission target (15 Oct): **4** · Hard deadline: 16 Oct

Scope contract: full MVP in [PLAN.md](PLAN.md) v0.3. Task prompts and acceptance: [instructions.md](instructions.md). Update this file whenever a task changes state; mark a task `[x]` only when its acceptance checks have actually passed.

Status key: `[ ]` pending · `[~]` in progress · `[x]` done · `[!]` blocked

## Key dates

| Date | Event |
|---|---|
| 15 Oct | Planned submission (one day of buffer) |
| 16 Oct | Submission deadline: exact cutoff time still unknown |
| 23 Oct | Finalists announced |
| 3 Nov | Demo Day (TBC) |

## Owner actions (not code; unblock T-00)

- [x] TRTC application credentials in `.env` (`SDKAppID`, `SDKSecretKey`). Still check the free allowance in Package Management.
- [x] TokenHub key works (`tokenhub_key`); only `hy3` is activated on the account
- [ ] Optional: activate `deepseek-v4-flash` under TokenHub Online Inference Service if you want the validator on TokenHub too (then set `VALIDATE_PROVIDER=tokenhub`, `VALIDATE_MODEL=deepseek-v4-flash`)
- [x] DeepSeek key in `.env` (`deepseek_key`); validator uses `deepseek-flash` directly
- [x] Langfuse Cloud keys in `.env` (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`)
- [!] **CodeBuddy credits exhausted (429) on 10 Oct and will not reset.** Decision: remaining tasks are built by Claude subagents and reviewed by the lead. The submission still asks for ≥3 CodeBuddy/WorkBuddy development screenshots (0 so far): plan how to show the CodeBuddy-built T-00–T-05 work (CLI session logs are kept)
- [ ] Real-browser passkey check: `make seed`, `make enroll-code`, `make dev`, `cd web && npm run dev`, open http://localhost:5173, enroll with the code, sign in (Touch ID)
- [ ] For the step-up demo add `OPERATOR_TOKEN=<random ≥32 chars>` and `STEP_UP_SECRET=<random ≥32 chars>` to `.env`
- [ ] Add `VALIDATOR_SERVICE_TOKEN=<random ≥32 chars>` to `.env` (`python -c 'import secrets; print(secrets.token_urlsafe(48))'`); the validator refuses all requests without it
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

- [ ] CodeBuddy/WorkBuddy development screenshots or recordings: **0 / 3 minimum** (T-00/T-01 ran headless; CLI logs kept as supplementary evidence only)
- [ ] Real Langfuse trace link (extraction → validation → policy → execution)
- [ ] Langfuse dataset + extractor-only vs extractor+validator experiment runs
- [ ] Attack matrix (attack, expected control, observed result)
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
| hy3 is a reasoning model (~3 s, ~250 tokens per small call) | Open | max_tokens ≥ 2048; never pass its reasoning to the validator or Langfuse unsanitized; watch extractor latency in T-14 |
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
| 10 Oct | T-04: Compose with two bridge networks (agent_net, validator_net); neither marked `internal` so both have outbound LLM egress. Agent/validator read-only rootfs + tmpfs /tmp; gateway has /data volume only. Images built from `uv sync --frozen --no-dev` with venv at `/app/.venv` so shebangs match runtime path |

## Hours log

Budget: about 3–4 h/day, 21–28 h from 9–15 Oct.

| Date | Hours | Done |
|---|---|---|
| 9 Oct | | Plan review round 2; STATUS.md; T-00 scaffold, checks, ASR + Langfuse smokes |
| 10 Oct | | T-00 LLM smoke (hy3 + deepseek-flash); T-01 schemas/signing; T-02 mock bank + read APIs (CodeBuddy + review fixes); T-03 conversations + audit chain; T-04 runtime isolation (reviewer re-ran probes: 26/26 PASS ×2, 274 tests); per-folder READMEs |

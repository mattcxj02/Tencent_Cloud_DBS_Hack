# DBS Track — DCTA Hackathon Plan (DRAFT v0.2)

> Status: **v0.2 agreed** · local only, not pushed.
> Solo entrant · Banking track (DBS) · *Direct Conversational Transaction Agent (DCTA)* · **Scenario 1** (voice payments + KYC + basic risk control)
> Submission **16 Oct 2026** · Finalists 23 Oct · Demo Day 3 Nov (TBC)
> Budget: ~3–4 h/day × 16 days ≈ **55 h**. Stack: **Python**.

Changes from v0.1: Python stack; a **single agent built as a deterministic state machine** where the LLM is
confined to two nodes; a typed **skill/tool registry** split by side-effect class; an explicit **Agent Mandate**
(spending limits the agent can never exceed); **Langfuse + Tencent APM** for traces next to our own
tamper-evident audit log; scope cut to fit 55 h.

---

## 1. Design principle

> **"The LLM translates speech into slots. Code does everything else."**

GenAI is used in exactly **two** places:
1. **Extract:** transcript → untrusted slot values (`{"action":"transfer","payee_mention":"Ah Ma","amount_text":"fifty bucks"}`).
2. **Validate:** an independent second call that sees *only* the raw transcript and the final draft, and says whether they match.

Everything else is deterministic Python: number parsing, payee and account resolution, clarification questions
(from templates), draft compilation, policy and limits, signing, execution and audit. The LLM never chooses
which tool runs, never sees ledger credentials, and never produces text the user signs.

Why this beats a free "ReAct" agent, for judges and for the brief:
- **Deterministic execution:** the same transcript and state give the same path through the graph.
- **Replayable:** any session can be re-run from the audit log.
- **Explainable:** every step is a named node with typed input and output, and shows up in the trace.

---

## 2. The single-agent workflow (state machine)

Built with **LangGraph** (explicit `StateGraph`, checkpointing, `interrupt()` for human-in-the-loop). Each node
is a plain Python function over one typed `SessionState` (Pydantic).

```
 voice ─► [1 INGEST] ◄─ text
             │  raw audio/text stored → audit
             ▼
       [2 FIREWALL]  deterministic: length/charset, injection patterns,
             │        untrusted-text quarantine (bill refs, pasted text)
             ▼
       [3 EXTRACT] ★LLM  JSON-schema slots only, no tools, no execution
             ▼
       [4 NORMALISE]  deterministic number parser ("a grand", "fifty bucks",
             │         "half of it") cross-checked against the LLM's value → mismatch ⇒ clarify
             ▼
       [5 RESOLVE]    READ skills: payee lookup (alias/fuzzy), accounts, holdings, quotes
             │
             ├── ambiguous / missing ──► [6 CLARIFY] ── interrupt() ─► user answer ─► back to 3
             ▼
       [7 COMPILE]    canonical draft JSON · $ref dependencies resolved · provenance tag per field
             ▼
       [8 VALIDATE] ★LLM  independent: transcript vs draft  + deterministic diff  → mismatch ⇒ FREEZE
             ▼
       [9 PRE-CHECK]  Agent Mandate · KYC tier · sanctions list · velocity · scam signals
             │         → PASS | STEP-UP | BLOCK   (advisory; the gateway re-checks)
             ▼
      [10 CONFIRM]    interrupt(): deterministic overlay rendered from canonical bytes
             │         → WebAuthn biometric signs  H = SHA256(canonical ‖ nonce ‖ expiry)
             ▼
 ═══════════ trust boundary: agent process ends here ═══════════
             ▼
      [11 GATEWAY]    separate process · only holder of ledger credentials
                      verify sig ▸ recompute hash ▸ nonce single-use ▸ expiry ▸
                      re-check mandate/KYC/limits ▸ step-up if needed ▸ execute legs in order
             ▼
      [12 RESULT]     read back to the user; every node already wrote to the audit chain
```

### Skill / tool registry

Every skill is a typed Python function (Pydantic in/out) tagged with a **side-effect class**. The registry
enforces who may call what.

| Class | Examples | Callable by agent graph? | Callable by gateway? |
|---|---|---|---|
| `READ` | `lookup_payee`, `list_accounts`, `get_balance`, `get_quote`, `get_history` | ✅ | ✅ |
| `DRAFT` | `compile_draft`, `render_overlay`, `issue_nonce` | ✅ | — |
| `EXECUTE` | `transfer`, `pay_bill`, `pay_card`, `buy_equity` | ❌ **not even imported** | ✅ after signature |

`EXECUTE` skills live only in the `gateway` package. The `agent` package has no import path to them and no DB
credential for ledger writes. This is enforced by a test that inspects imports, plus separate SQLite
permissions/processes.

Supported intents (keep it tight): `transfer` (own account / saved payee / PayNow), `pay_bill`, `pay_card`,
`buy_equity` (SGX: D05 etc.). Currency: SGD only.

---

## 3. Identity & biometrics

- **Login / identity:** passkey (WebAuthn) sign-in → session. Enrollment is done once for the demo user.
- **Every money movement:** a fresh WebAuthn assertion with `userVerification: "required"` (FaceID / TouchID /
  Windows Hello). The WebAuthn **challenge is the transaction hash**, so the signature is bound to exact bytes
  (the same idea as W3C *Secure Payment Confirmation* and PSD2 "dynamic linking").
- One signature covers the whole ordered multi-leg bundle; each leg is shown separately on the overlay.
- Library: `py_webauthn` (server) + ~40 lines of browser JS (`navigator.credentials.get`).
- WYSIWYS: the overlay is rendered by code from the canonical JSON, never from LLM prose. The gateway
  recomputes the hash from the bytes it receives; any tampering ⇒ reject.

---

## 4. Agent Mandate: the money limit on what the agent can do

A per-user, **biometrically signed policy** that defines the ceiling for anything initiated through the
conversational channel. It is modelled on the *Intent Mandate* in Google's AP2 protocol, adapted to banking.

```json
{
  "mandate_id": "m_001",
  "per_txn_max": 1000.00,
  "daily_max": 3000.00,
  "allowed_actions": ["transfer", "pay_bill", "pay_card", "buy_equity"],
  "payees": "saved_only",            // agent cannot draft to never-seen accounts
  "max_legs_per_bundle": 4,
  "valid_until": "2026-12-31T23:59:59+08:00"
}
```

Rules (all enforced **in the gateway**, pre-checked in node 9 for UX):
- Draft within mandate → normal biometric signature.
- Draft above `per_txn_max` / `daily_max` or to a new payee → **step-up**: out-of-band confirmation (TOTP
  mock or "relationship manager callback" mock) **plus** biometric.
- **Raising the mandate** needs biometric + a **12 h cooling period** before it takes effect, like SG banks'
  transfer-limit increases. This is the scam-resistant part: a scammer on the phone cannot talk the user into
  "just raise the limit" and use it immediately.
- **Lowering or pausing** ("kill switch") takes effect instantly.
- Scam signals (deterministic): new payee + large amount + urgency or "safe account / officer / police"
  language in the transcript ⇒ force step-up + cooling notice + offer to lock the funds.

---

## 5. Input: speech + text

- **Text box is always available.** Voice and text converge at node 1, so there is one pipeline.
- **Voice, phase 1:** browser Web Speech API (zero setup, free) for development.
- **Voice, phase 2:** **Tencent Cloud ASR** real-time WebSocket via the `tencentcloud-speech-sdk-python` backend
  proxy (brief recommends it; multi-accent). Raw audio is kept for the audit trail, as the brief requires.
- Optional: Tencent TTS read-back of the confirmation (accessibility), only if time permits.

---

## 6. Traceability: two layers, different jobs

| Layer | Purpose | Tool |
|---|---|---|
| **LLM/agent observability** | Debug & evaluate: every node as a span, prompts, outputs, tokens, latency, eval scores per dataset item | **Langfuse** (Python SDK v3 is OpenTelemetry-based; `@observe` decorator + LangGraph callback) |
| **Tencent-native option** | Same OTel spans in Tencent's console | **Tencent Cloud APM → LLM Observability**: supports OpenAI SDK + LangChain/LangGraph via OTel (`tapm-instrument`) |
| **Compliance audit (source of truth)** | Tamper-evident record: raw input → transcript → slots → draft → validation → signature → execution | **Own append-only, hash-chained table** in SQLite with a "Verify chain" button |

Recommendation:
- Start with **Langfuse Cloud free tier**. There's no infra, and self-hosting v3 means running Postgres + ClickHouse + Redis +
  MinIO, which isn't worth your hours. Only mock data is sent.
- Because both are OpenTelemetry, **add a second exporter to Tencent APM** once the Tencent trial is active.
  That gives "we use Tencent's observability" for the scoring story at near-zero cost.
- Langfuse is *not* the audit trail. The brief asks for a regulatory-grade record; that stays in our own
  hash-chained log, and each audit row stores the Langfuse `trace_id` so they cross-link.

Tencent ADP (Agent Development Platform, 4.0 intl) was considered: it has a visual workflow builder and
AgentOps traces, but it's low-code, hosted, tied to the 1-month trial, and makes the crypto gateway and
determinism claims harder to show in code. **Not used for the core**; mention it as the production path in the
Feasibility section.

---

## 7. Stack

| Concern | Choice |
|---|---|
| Language | Python 3.12, `uv` for env/deps |
| Agent graph | LangGraph (`StateGraph`, `interrupt`, SQLite checkpointer) |
| Schemas | Pydantic v2 (also generates the JSON Schema for LLM structured output) |
| LLM | **All via TokenHub** (`https://tokenhub-intl.tencentcloudmaas.com/v1`, OpenAI-compatible, tool calling + structured output). **Extractor: `hy3`** (Tencent Hunyuan). **Validator: DeepSeek-V4-Flash**: a different vendor, so errors are less correlated (a genuine "dual engine"). `hy4-preview` kept as an A/B option. Budget: ~1M free tokens ≈ 250 conversation turns ⇒ day-to-day dev and eval iterations run on the **personal DeepSeek key**; TokenHub is used for integration, final eval, video and Demo Day. Model IDs are env config (`EXTRACT_MODEL`, `VALIDATE_MODEL`, `LLM_BASE_URL`). |
| API | FastAPI: `agent` app and `gateway` app as **two processes** (+ `ledger` module owned by gateway) |
| Biometrics | `py_webauthn` + small vanilla JS |
| Frontend | **React** (Vite + TypeScript + Tailwind), mobile-first PWA-style; `@simplewebauthn/browser` for biometrics; Web Speech API for the mic. Talks to the FastAPI apps over JSON only; the UI never computes hashes itself for trust (the gateway recomputes). Dev: Vite proxy on `localhost` so WebAuthn works without HTTPS. |
| DB | SQLite (ledger, mandate, audit chain, nonces) |
| Tracing | Langfuse SDK v3 (+ Tencent APM OTLP exporter later) |
| Voice | Web Speech API → Tencent ASR |
| Tests | pytest: invariants, gateway attacks, golden eval set |
| Deploy (bonus) | Tencent Cloud Lighthouse + Docker Compose + HTTPS domain (WebAuthn needs HTTPS off localhost) |

Repo layout:
```
agent/        graph.py, nodes/, llm.py, firewall.py, numparse.py, prompts/
gateway/      app.py, verify.py, policy.py (mandate/KYC/velocity/scam), execute.py
ledger/       models.py, seed.py
shared/       schemas.py (Draft, Leg, Mandate…), canonical.py, audit.py, skills.py (registry)
web/          React app (Vite + TS + Tailwind): chat, mic, overlay, audit viewer, mandate settings
evals/        golden.jsonl, run_eval.py
tests/
docs/         PLAN.md, architecture.png
CODEBUDDY.md  project rules for every CodeBuddy session
```

---

## 8. Invariants (on a slide *and* as tests)

- **I1** The agent process cannot import or reach any `EXECUTE` skill or ledger-write credential.
- **I2** The gateway executes only payloads whose SHA-256 was the challenge of a user-verified WebAuthn assertion.
- **I3** The overlay is rendered from the same canonical bytes that are hashed.
- **I4** Nonces are single-use and expire in 120 s.
- **I5** No execution may exceed the active Agent Mandate without a step-up.
- **I6** Every node appends to the hash-chained audit log.
- **I7** A value whose provenance is `untrusted_text` can never fill `payee` or `amount`.

---

## 9. What makes it stand out (vs. teams that just implement the brief)

1. **Deterministic by construction:** the LLM is confined to 2 of 12 nodes, shown live in Langfuse/Tencent APM.
2. **Agent Mandate** with cooling on increases: an AP2-style delegated limit adapted to SG anti-scam practice.
3. **Provenance-tagged fields:** injection defence by construction (I7), shown colour-coded on the overlay.
4. **Scam-aware step-up:** protects the user from a coached transfer, not just from a rogue AI.
5. **Rogue Agent Mode demo:** swap in a malicious extractor → validator freezes → gateway rejects unsigned →
   tamper-after-sign ⇒ hash mismatch.
6. **Measured:** golden set (~30 utterances incl. Singlish + adversarial) with reproducible scores in Langfuse.

---

## 10. Timeline (≈55 h)

| Dates | Hours | Phase | Done when |
|---|---|---|---|
| 1 Oct | 3 | **P0 Setup** | `uv` project, repo layout, `CODEBUDDY.md`, Langfuse account, seed data. Screenshots start. |
| 2–4 Oct | 10 | **P1 Deterministic core** | schemas, canonical JSON, audit chain, ledger + seed, skill registry, gateway (WebAuthn verify, nonce, mandate, execute) + attack tests. *No LLM needed.* |
| 5 Oct | — | **Start Tencent trial** | 1 month ⇒ covers Demo Day 3 Nov. Get TokenHub key (+ ASR later). |
| 5–8 Oct | 13 | **P2 Agent graph** | LangGraph nodes 1–10, number parser, resolver, clarify loop, extractor + validator prompts, firewall, Langfuse tracing. |
| 9–11 Oct | 11 | **P3 React UI + biometrics** | chat + mic + text, overlay, WebAuthn signing, clarification chips, audit viewer, mandate settings screen. (Scaffold on 1 Oct in P0 so the UI grows alongside P1–P2.) |
| 12 Oct | 4 | **P4 Risk + demo features** | step-up mock, scam signals, mandate cooling, Rogue Agent Mode; Tencent ASR if time allows. |
| 13 Oct | 4 | **P5 Eval + deploy** | golden-set run, results table; Lighthouse HTTPS deploy (or local + video). Tencent APM exporter. |
| 14–15 Oct | 7 | **P6 Submission** | description, architecture diagram, Miora cover, 5–8 min video, CodeBuddy screenshots. |
| 16 Oct | 2 | Buffer + submit | submit early. |

**Cut-line (drop in this order):** Tencent APM exporter → TTS → Tencent ASR (keep browser speech) → UI polish
(animations, dark mode) → scam signals (keep mandate) → deploy (local + video). **Never cut:** WebAuthn signing, gateway verification,
mandate enforcement, validator, audit chain, injection demo.

---

## 11. CodeBuddy workflow (proof of use is mandatory)

- **Headline features in the IDE** (`buddy chat -m agent "<prompt>"`): gateway verify, WebAuthn, validator,
  mandate. You screenshot these chats.
- **Boilerplate via CLI** (`codebuddy -p … --model hy3 --output-format json`): seed data, templates, tests.
  JSON logs are kept in `docs/devlog/` as extra proof.
- Claude reviews every task via `git diff` + tests, then commits locally. **No pushes.**

---

## 12. Open decisions

1. ~~LLM key before trial~~ **Resolved:** TokenHub free tokens (hy3 + DeepSeek-V4-Flash) + personal DeepSeek key for dev.
2. ~~Tracing~~ **Resolved:** Langfuse Cloud free tier.
3. ~~Docker~~ **Resolved:** Docker Desktop installed (needed for deploy only).
4. ~~Mandate values~~ **Resolved:** S$1,000 / S$3,000 as seed defaults, configurable (env + settings screen).
5. ~~Front end~~ **Resolved:** React (Vite + TS + Tailwind). Costs ~5 h vs HTMX; absorbed from P4.

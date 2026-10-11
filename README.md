# DCTA: Direct Conversational Transaction Agent

**Speak your payments. Verify every detail. Stay in control.**

Tencent Cloud Hackathon 2026 · DBS track · Scenario 1 (voice-enabled payment and transaction, with KYC and basic risk control).

> **Simulated bank, synthetic data.** This is a hackathon prototype. It is not connected to DBS or to any real bank, PayNow, SGX, KYC or sanctions service. All accounts, payees, balances, share quotes and second-factor codes are synthetic. No real money moves.

## What it is

DCTA lets a bank customer say or type routine payments in their own words, for example *"Send fifty bucks to Mum from my savings and twenty dollars to Ah Ma from savings"*. It turns them into one exact bundle of payments, which the customer checks and approves with a passkey (Touch ID). Two AI models read the request: one drafts it and a second checks the draft independently. Neither model can move money. Only a deterministic gateway can, and only for the exact payment the customer's passkey signed.

## The customer flow

1. **Sign in** with a passkey.
2. **Speak** (push to talk) **or type** up to four payments in one message. Supported: transfers to your own accounts, saved payees or saved PayNow contacts; bill payments; credit-card payments (including "my card balance"); and mock SGX share purchases.
3. **See and fix your words.** Speech becomes an editable transcript. Correct a word ("fifty" → "fifteen") and the draft is rebuilt, with the change shown as "SGD 50.00 → SGD 15.00".
4. **Answer short questions** when something is unclear: *"Which "John" do you mean: John Doe (account ending 4521) or John Tan (account ending 8892)?"*
5. **Review the exact confirmation:** every payment, its source account, amounts, total debit and the result of the independent check.
6. **Approve with Touch ID or your passkey.** A total above SGD 500 (a prototype setting) or scam-like wording ("a police officer told me…") also needs a one-time code from a separate, simulated channel. Hard limits, KYC, sanctions, funds and velocity rules block a payment outright, and no code overrides them.
7. **Get a receipt and an evidence trail:** *You said → We clarified → You approved → Executed*.

## Architecture and trust boundary

```
 Browser: chat, mic, passkey ──► GATEWAY: trusted, deterministic. The only service that
                                  │  can open the ledger, sign-off records and audit log.
              scoped 120 s token  │                     stored conversation + frozen bundle
                                  ▼                                   ▼
             AGENT: extractor LLM (TokenHub hy3)    VALIDATOR: second LLM (DeepSeek)
             copies the customer's words; it         reads the conversation and the draft;
             cannot approve or pay                   answers MATCH / MISMATCH / UNCERTAIN
```

There are three services, each with its own container image ([infra/](infra/README.md)). The **gateway** owns identity, the conversation record, frozen drafts, policy, approvals, execution and the hash-chained audit log. The **agent** reads the stored conversation through a short-lived scoped token. Its model only copies the customer's words for each field. Code turns those words into amounts and saved-payee records. The gateway then checks that every amount, recipient and source account is backed by the customer's own words and names exactly one bank record. The **validator** is a different model from a different vendor. It never sees the extractor's reasoning, and it compares the full conversation with the frozen draft. A MATCH only unlocks the confirmation screen. Approval is a WebAuthn passkey signature over `SHA256("DCTA:approval:v1\0" + canonical envelope)`. The envelope holds the exact payload, user, credential, nonce and an expiry of 120 seconds or less. The gateway executes its own stored bytes after it re-checks the signature, the owner and the full policy, all in one database transaction. Neither model has tools, ledger access or keys, so even a model that is fooled or forced to return MATCH cannot authorize a payment. Every stage is traced in Langfuse, with secrets and account numbers redacted. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (diagrams, plus an "As built" section).

## Quick start (local demo)

**Prerequisites**
- A Mac with Touch ID or another platform passkey, and Chrome or Safari. Passkeys work on `http://localhost`; a phone needs an HTTPS origin.
- Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js (current LTS) with npm, and Docker Desktop (for the container stack only).
- Credentials: a TokenHub key with `hy3` enabled, a DeepSeek API key, a Tencent TRTC application (**SDKAppID + SDKSecretKey**, not the cloud SecretId/SecretKey) and a Langfuse Cloud project.

**1. Install**

```bash
uv sync --extra dev
cd web && npm install && cd ..
```

**2. Configure `.env`** (copy [`.env.example`](.env.example); never commit `.env`). Variable names:

| Purpose | Variables |
|---|---|
| Models | `tokenhub_key`, `deepseek_key`. Optional: `EXTRACT_PROVIDER`, `EXTRACT_MODEL`, `VALIDATE_PROVIDER`, `VALIDATE_MODEL`, `EXTRACT_MAX_TOKENS`, `VALIDATE_MAX_TOKENS`, `AGENT_TIMEOUT_SECONDS` |
| Voice (gateway only) | `SDKAppID`, `SDKSecretKey`, `TRTC_ASR_HOST` |
| Observability | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` |
| Service secrets (random, at least 32 characters each) | `VALIDATOR_SERVICE_TOKEN` (**required**: the validator refuses every request without it), `OPERATOR_TOKEN` and `STEP_UP_SECRET` (step-up demo), `GATEWAY_DELEGATION_SECRET` (optional; if empty, a random secret is used per process) |
| Passkeys | `WEBAUTHN_RP_ID=localhost`, `WEBAUTHN_RP_ORIGIN=http://localhost:5173` (defaults) |

Generate each random secret separately:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

**3. Run**

```bash
make reset-db        # fresh synthetic bank (first run: `make seed` is enough). Also removes enrolled passkeys
make enroll-code     # one-time enrollment code for the demo user (valid 15 minutes)
make dev             # agent :8001, gateway :8002, validator :8003 (local processes)
cd web && npm run dev    # second terminal; then open http://localhost:5173
```

Enter the code to enroll a passkey, then sign in with Touch ID. The demo user starts with savings 12,000.00, current 3,500.00 and joint 2,000.00. The seed also includes saved payees (two Johns, Mum, Ah Ma, Landlord Lim, a PayNow contact and a sanctioned "Northwind Trading"), billers (SP Group, Singtel), a credit card with 642.30 outstanding and mock SGX quotes (D05, O39, U11, Z74).

**Step-up codes.** The simulated "SMS" is a separate operator console. Run it in a third terminal:

```bash
export OPERATOR_TOKEN=<the same value as in .env>
make operator ARGS='list'                    # payments waiting for a code (never shows codes)
make operator ARGS='code <authorization_id>' # "delivers" and prints the 6-digit code
```

**Isolated containers.** `make compose-up` starts the three services in separate containers, with separate users and networks, and only the gateway holds the database volume. `make compose-down` stops them. `make isolation-probe` builds the stack, runs the in-container probes, checks that no image contains `.env`, then tears everything down. The recorded demo uses `make dev`. The browser demo has not yet been rehearsed against the container stack: it would need seeding and an enrollment code inside the gateway container.

**Things to try**: these phrasings were measured in [docs/evidence/E2E-live.md](docs/evidence/E2E-live.md) and [docs/evidence/T-14-results.md](docs/evidence/T-14-results.md):
- "Send fifty bucks to Mum from my savings and twenty dollars to Ah Ma from savings"
- "Pay my card balance from my current account and send 15 to John" (then answer "John Doe" and "my current account"; the SGD 657.30 total needs a step-up code)
- "Move $1,000 from current to savings and then buy DBS shares with half of that from savings"
- "Send $150 to Northwind Trading from my current account" (blocked: simulated sanctions)

## Tests, evaluations and probes

```bash
make test            # pytest suite (each test uses its own temporary database)
make lint            # ruff
make web-build       # TypeScript strict check and production build
make eval-fake       # evaluation harness dry run with fake models (no network)
make eval            # live 35-case golden set + Langfuse experiments (about 10 minutes, uses model credits)
make isolation-probe # container isolation proof (Docker)
make smoke-llm smoke-asr smoke-langfuse smoke-voice smoke-extract smoke-validate smoke-telemetry   # live checks
```

Rogue Agent Mode (local only): `uv run python scripts/rogue_agent_demo.py` runs 46 attacks against the real gateway with a malicious extractor and a validator forced to answer MATCH: all 46 controls held; the ledger was unchanged in 42 rows and the 4 retry/race rows produced exactly one legitimate commit. See [docs/evidence/T-13-attack-matrix.md](docs/evidence/T-13-attack-matrix.md).

## Documentation

| Document | What it covers |
|---|---|
| [docs/SUBMISSION.md](docs/SUBMISSION.md) | Hackathon submission: title, blurb, description, measured value, CodeBuddy use, limitations, pilot path |
| [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | 5–8 minute demo shot list with exact utterances |
| [docs/JUDGE_QA.md](docs/JUDGE_QA.md) | Likely judge questions with short, honest answers |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Trust zones, approval lifecycle, sequence diagram, as-built differences |
| [docs/evidence/](docs/evidence/README.md) | Outputs from real runs: smokes, isolation probes, telemetry, extraction, voice, end-to-end, evaluation, CodeBuddy sessions |
| [docs/PLAN.md](docs/PLAN.md), [docs/instructions.md](docs/instructions.md), [docs/STATUS.md](docs/STATUS.md) | Plan and contracts, task list T-00–T-15, live status and owner actions |
| [docs/cover/](docs/cover/README.md) | 16:9 cover image (SVG) and export steps |
| [CODEBUDDY.md](CODEBUDDY.md) | Project rules and invariants given to the coding agents |
| Folder READMEs | [gateway](gateway/README.md), [agent](agent/README.md), [validator](validator/README.md), [shared](shared/README.md), [ledger](ledger/README.md), [prompts](prompts/README.md), [web](web/README.md), [infra](infra/README.md), [evals](evals/README.md), [scripts](scripts/README.md), [tests](tests/README.md) |

## Status (10 Oct 2026)

Built: all four payment actions, bundles of 1–4 legs, clarifications and corrections, the independent validator, exact-payload passkey approval, hard policy, step-up, mandates, atomic execution, audit, Langfuse, voice through TRTC, the React UI and container isolation. The server side is covered by the pytest suite and by the live runs in [docs/evidence/](docs/evidence/README.md). The React UI is type-checked and builds, but has no automated browser tests. **Still pending:**
- a real-browser walkthrough with Touch ID and a microphone (the live end-to-end run used a software passkey over the real HTTP API);
- the usability sessions (kit ready in [evals/usability/](evals/usability/README.md));
- the demo video, CodeBuddy screenshots and any hosted deployment.

See [docs/STATUS.md](docs/STATUS.md).

## How it was built

CodeBuddy Code CLI built tasks T-00 to T-04 and most of T-05 in headless sessions (logs summarized in [docs/evidence/codebuddy/](docs/evidence/codebuddy/README.md)) until its credits ran out on 10 Oct. Claude Code subagents built the remaining tasks. A lead Claude Code session reviewed every task: it read the diffs, re-ran the checks and fixed the defects it found.

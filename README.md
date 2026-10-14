# SaySo: a Direct Conversational Transaction Agent (DCTA)

**Speak your payments. Verify every detail. Stay in control.** Nothing moves without your say-so.

Tencent Cloud Hackathon 2026 · DBS track · Scenario 1 (voice-enabled payment and transaction, with KYC and basic risk control).

> **Simulated bank, synthetic data.** This is a hackathon prototype. It is not connected to DBS or to any real bank, PayNow, SGX, KYC or sanctions service. All accounts, payees, balances, share quotes and second-factor codes are synthetic. No real money moves.

[![SaySo launch video: one spoken request, from words to receipt (24 s). Click to play.](docs/launch/poster.jpg)](docs/launch/sayso-launch.mp4)

<p align="center"><a href="docs/launch/sayso-launch.mp4"><b>▶ Watch the 24-second launch video</b></a> · <a href="docs/launch/README.md">how it was made</a></p>

## What it is

SaySo is a payments feature for a bank's app. It lets a customer say or type routine payments in their own words, for example *"Send fifty bucks to Mum from my savings and twenty dollars to Ah Ma from savings"*. It turns them into one exact bundle of payments, which the customer checks and approves with a passkey (Touch ID). Two AI models read the request: one drafts it and a second checks the draft independently. Neither model can move money. Only a deterministic gateway can, and only for the exact payment the customer's passkey signed.

## The customer flow

1. **Sign in** with a passkey.
2. **Speak** (push to talk) **or type** up to four payments in one message. Supported: transfers to your own accounts, saved payees or saved PayNow contacts; bill payments; credit-card payments (including "my card balance"); and mock SGX share purchases.
3. **See and fix your words.** Speech becomes an editable transcript. Correct a word ("fifty" → "fifteen") and the draft is rebuilt, with the change shown as "SGD 50.00 → SGD 15.00".
4. **Answer short questions** when something is unclear: *"Which "John" do you mean: John Doe (account ending 4521) or John Tan (account ending 8892)?"*
5. **Review the exact confirmation:** every payment, its source account, amounts, total debit and the result of the independent check.
6. **Approve with Touch ID or your passkey.** A total above SGD 500 (a prototype setting) or scam-like wording ("a police officer told me…") also needs a one-time code from a separate, simulated channel. Hard limits, KYC, sanctions, funds and velocity rules block a payment outright, and no code overrides them.
7. **Get a receipt and an evidence trail:** *You said → We clarified → You approved → Executed*.

One request, end to end (frames from the [launch video](docs/launch/README.md)):

| | | |
|:---:|:---:|:---:|
| ![You say the request](docs/launch/stills/01-you-say.jpg) | ![The AI copies words; code turns them into exact values](docs/launch/stills/02-read-and-resolve.jpg) | ![One short question instead of a guess](docs/launch/stills/03-clarify.jpg) |
| **1. You say it.** Voice or text, in your own words. | **2. AI reads, code decides.** The model only copies words; code turns them into exact amounts, or a question. | **3. One short question.** Code writes the choices from your saved payees. |
| ![You sign exactly what you see](docs/launch/stills/04-approve.jpg) | ![Measured on live runs](docs/launch/stills/05-proof.jpg) | ![Nothing moves without your say-so](docs/launch/stills/06-say-so.jpg) |
| **4. You sign exactly what you see.** Independent check, passkey, one-time code, executed. | **5. Measured.** 0 / 30 wrong drafts reached approval; 46 / 46 attacks held. | **6. Nothing moves without your say-so.** |

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

There are three services, each with its own container image ([infra/](infra/README.md)): the **gateway** (trusted and deterministic: identity, the conversation record, policy, approval, execution and the audit log), the **agent** (extractor model) and the **validator** (a second model from another vendor). Neither model has tools, ledger access or keys, so even a model that is fooled, or forced to answer MATCH, cannot authorize a payment.

### Security at a glance

Each row is one of the brief's "What the solution should solve" requirements (handbook pp. 6–7).

| Brief requirement | How SaySo meets it | Evidence |
|---|---|---|
| Multi-intent parsing | The extractor only copies the customer's words for each field. Code turns them into amounts, payees and accounts, including "half of that" and "the rest" | 33–34 of 35 live cases correct across three trials, 0 unsafe drafts ([T-14-no-think](docs/evidence/T-14-no-think.md)) |
| Ambiguity resolution | Code (not the model) writes short questions with tappable choices: "Which John?", "Did you mean Ah Ma?" | [E2E-live](docs/evidence/E2E-live.md) |
| Independent validation | The validator compares the whole conversation with the frozen draft. Only MATCH unlocks the confirmation; mismatch, uncertainty or an error stops it | 0 of 30 wrong drafts reached approval ([T-14-no-think](docs/evidence/T-14-no-think.md)) |
| Biometric human-in-the-loop | The confirmation shows the gateway's stored payment. The passkey (Touch ID) signs a SHA-256 hash of exactly those bytes, valid for 2 minutes at most. Any correction voids it | [test_t05_approval.py](tests/test_t05_approval.py) |
| Zero-trust gateway | Only the gateway can touch the ledger. It executes its own stored bytes, never a client payload, after re-checking the signature, owner and every rule in one transaction | Rogue Agent Mode: 46/46 controls held ([T-13](docs/evidence/T-13-attack-matrix.md)); container isolation ([T-04](docs/evidence/T-04-isolation.txt)) |
| Injection shielding | References and quoted text cannot supply an amount, payee or account; embedded instructions make the validator answer MISMATCH; models have no tools | 9/9 adversarial cases safe ([T-14](docs/evidence/T-14-results.md)); [T-09](docs/evidence/T-09-extract.md) |
| Risk and velocity controls | Per-payment and daily limits, velocity, KYC, sanctions and funds checks block outright. Totals above SGD 500 or scam-like wording also need a one-time code from a separate (simulated) channel | Sanctioned and over-limit drafts blocked even after MATCH ([T-14](docs/evidence/T-14-results.md)) |
| Traceable and auditable | Hash-chained audit log, a readable evidence trail per conversation, and Langfuse traces with secrets and account numbers redacted | [T-08](docs/evidence/T-08-telemetry.md) |

**Also enforced, not shown to the customer:** single-use approvals (a retry or race returns the original receipt, never a second payment); a 120-second, narrowly scoped token for the agent; validator results bound to the draft, conversation and balances they checked; readiness checks before any model call; and mandate changes signed with the passkey, where loosening a limit waits a cooling period (`#/mandate`).

**Production path, not built:** the bank's own fraud, sanctions and KYC engines; a real out-of-band channel (SMS, hardware token or relationship-manager callback); signed receipts and external audit checkpoints; hosting on HTTPS.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (diagrams, plus an "As built" section).

## Quick start (local demo)

**Prerequisites**
- A device with a platform passkey: a Mac with Touch ID (Chrome or Safari) or a Windows 10/11 PC with Windows Hello (Chrome or Edge); a USB security key also works. Passkeys work on `http://localhost` in a real browser (not an editor's built-in preview); a phone needs an HTTPS origin.
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
| Models | `tokenhub_key`, `deepseek_key`. Optional: `EXTRACT_PROVIDER`, `EXTRACT_MODEL`, `VALIDATE_PROVIDER`, `VALIDATE_MODEL`, `EXTRACT_MAX_TOKENS`, `VALIDATE_MAX_TOKENS`, `EXTRACT_TIMEOUT_SECONDS`, `EXTRACT_MAX_RETRIES`, `AGENT_TIMEOUT_SECONDS`, `VALIDATE_TIMEOUT_SECONDS`, `VALIDATOR_CLIENT_TIMEOUT_SECONDS` |
| Voice (gateway only) | `SDKAppID`, `SDKSecretKey`, `TRTC_ASR_HOST` |
| Observability | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` |
| Service secrets (random, at least 32 characters each) | `VALIDATOR_SERVICE_TOKEN` (**required**: the validator refuses every request without it), `OPERATOR_TOKEN` and `STEP_UP_SECRET` (step-up demo), `GATEWAY_DELEGATION_SECRET` (optional; if empty, a random secret is used per process) |
| Passkeys | `WEBAUTHN_RP_ID=localhost`, `WEBAUTHN_RP_ORIGIN=http://localhost:5173` (defaults) |

Generate each random secret separately:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Use the **same `VALIDATOR_SERVICE_TOKEN` value in gateway and validator**, at least 32 characters, and never give it to the agent. Restart both services after changing it; restart the agent after changing its model or timeout settings. `GET http://localhost:8002/api/ready` checks the validator with that token and the agent's model configuration before extraction; `/api/health` reports liveness. Missing, short or mismatched tokens fail drafting with a stable 503 error and no extractor call. These checks make no provider/model calls: valid provider credentials, account access and credits still need the documented live smoke checks.

Startup logs and health report missing `VALIDATOR_SERVICE_TOKEN`, `OPERATOR_TOKEN` and `STEP_UP_SECRET` by name only. Missing operator configuration disables the simulated delivery channel; a missing step-up secret uses the existing per-process fallback, so pending codes do not survive restart. Neither blocks otherwise eligible low-risk drafting. The extractor defaults to a 30-second provider request timeout and zero SDK retries, inside the gateway's 45-second wait. If increasing either setting, allow for retries, SDK backoff and gateway reads; this is not a propagated end-to-end deadline.

**3. Run**

```bash
make reset-db        # fresh synthetic bank (first run: `make seed` is enough). Also removes enrolled passkeys
make upgrade-db      # after pulling changes: add new tables and aliases, keep passkeys and history
make enroll-code     # one-time enrollment code for the demo user (valid 15 minutes)
make dev             # agent :8001, gateway :8002, validator :8003 (local processes)
cd web && npm run dev    # second terminal; then open http://localhost:5173
```

Ctrl-C cleans up all three services and their reload workers. From another terminal, `make stop` asks this checkout's supervisor to stop its own process groups and waits for cleanup. It never uses a broad process-name kill or signals PIDs from stale files. The supervisor holds a lock and watches a stop marker in ignored `.dev/`; a second `make dev` for the same checkout is refused. These development commands require a POSIX host (macOS/Linux). Use the running UI for the demo; the obsolete `make demo` placeholder has been removed.

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
- "Send 20 to mom from savings" or "... to ahma ..." (bank aliases and spacing variants resolve directly), "Send 20 to Ahmar from savings" (a near spelling: the assistant asks "Did you mean: Ah Ma (account ending 7766) or none of these?" and can remember "Ahmar" as your nickname). Nicknames are managed on the **Payees** page.
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
| [docs/PLAN.md](docs/PLAN.md) | Original plan and contracts (v0.3). Working notes (task prompts, status log, improvement plan) are on the [`dev` branch](https://github.com/mattcxj02/Tencent_Cloud_DBS_Hack/blob/dev/docs/README.md) |
| [docs/launch/](docs/launch/README.md) | 24-second launch video, poster, scene stills, 16:9 covers, storyboard and the Hyperframes source |
| [docs/cover/](docs/cover/README.md) | Original 16:9 cover image (SVG) and export steps |
| [CODEBUDDY.md](CODEBUDDY.md) | Project rules and invariants given to the coding agents |
| Folder READMEs | [gateway](gateway/README.md), [agent](agent/README.md), [validator](validator/README.md), [shared](shared/README.md), [ledger](ledger/README.md), [prompts](prompts/README.md), [web](web/README.md), [infra](infra/README.md), [evals](evals/README.md), [scripts](scripts/README.md), [tests](tests/README.md) |

## Status (13 Oct 2026)

Built: all four payment actions, bundles of 1–4 legs, clarifications (with clickable choices) and corrections, the independent validator, exact-payload passkey approval, hard policy, step-up, mandates, atomic execution, audit, Langfuse, voice through TRTC, live progress stages, the React UI and container isolation. The server side is covered by 1,359 pytest tests and by the live runs in [docs/evidence/](docs/evidence/README.md); extraction takes about 2 s at the median ([T-14-no-think](docs/evidence/T-14-no-think.md)). The React UI is type-checked and builds, with browser fixture pages in `web/tests/`. **Still pending:**
- a real-browser walkthrough with Touch ID and a microphone (the live end-to-end run used a software passkey over the real HTTP API);
- the usability sessions (kit ready in [evals/usability/](evals/usability/README.md));
- the 5–8 minute demo video (a 24-second [launch video](docs/launch/README.md) is done), CodeBuddy screenshots and any hosted deployment.

Limitations are listed in [docs/SUBMISSION.md §7](docs/SUBMISSION.md#7-limitations-and-pending-work).

## How it was built

CodeBuddy Code CLI built tasks T-00 to T-04 and most of T-05 in headless sessions (logs summarized in [docs/evidence/codebuddy/](docs/evidence/codebuddy/README.md)) until its credits ran out on 10 Oct. Claude Code subagents built the remaining tasks. A lead Claude Code session reviewed every task: it read the diffs, re-ran the checks and fixed the defects it found.

## Credits

**Building SaySo**

| Tool or service | Used for |
|---|---|
| [CodeBuddy Code](https://www.codebuddy.ai/) | Built tasks T-00 to T-04 and most of T-05 ([session logs](docs/evidence/codebuddy/README.md)) |
| [Claude Code](https://claude.com/claude-code) | Built the remaining tasks and reviewed every task |
| Tencent Cloud [TokenHub](https://www.tencentcloud.com/) (`hy3`) | Extractor model |
| [DeepSeek](https://www.deepseek.com/) | Independent validator model |
| [Tencent TRTC](https://trtc.io/) | Speech recognition for voice requests |
| [Langfuse](https://langfuse.com/) | Tracing and evaluation experiments |

**Launch video and images** ([details and licences](docs/launch/README.md#credits))

| Tool or asset | Used for | Licence |
|---|---|---|
| [brag](https://github.com/latent-spaces/brag) by latent-spaces | Launch-video workflow: storyboard, brief, poster, share copy | MIT |
| [Hyperframes](https://github.com/heygen-com/hyperframes) by HeyGen | Composition checks and rendering | Apache-2.0 |
| [GSAP](https://gsap.com) | Animation | GSAP Standard License |
| "Happy Beats / Business Moves" vol. 11 by [ende.app](https://ende.app/en) | Music | Bundled with brag |
| [Kenney](https://kenney.nl/) and unicae_games sound packs | Sound effects | CC0 |
| Space Grotesk, IBM Plex Sans, JetBrains Mono | Type | SIL OFL 1.1 |
| Claude Code | Followed the brag workflow, wrote and rendered the composition | — |

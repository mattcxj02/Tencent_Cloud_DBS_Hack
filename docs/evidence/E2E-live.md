# Live end-to-end run

Date: 10 Oct 2026. Run by the lead with all three services as separate local processes on a temporary database, live models (TokenHub `hy3` extractor, DeepSeek `deepseek-flash` validator), live Langfuse Cloud, and a software passkey (ES256) driving the real HTTP API exactly as the browser does. Synthetic data only. The validator token, delegation secret, operator token and step-up secret were random per-run values passed as process environment (not stored).

## Run 1: "Send fifty bucks to Mum from my savings and twenty dollars to Ah Ma from savings"

1. `POST /api/conversations/{id}/draft` → `ready` in 8.2 s: two legs (Mum 50.00, Ah Ma 20.00, both from Savings ••1234), total 70.00, validator verdict MATCH (attempt 1).
2. Envelope issued (`PENDING_APPROVAL`, 120 s window), signed with the passkey over the envelope's own challenge.
3. Approve → `COMMITTED`, receipt with two simulated settlement references.
4. Savings 12,000.00 → 11,930.00. Evidence endpoint: audit chain ok, trace links present.

## Run 2: "Pay my card balance from my current account and send 15 to John"

1. Draft → clarify (17.9 s): "Which "John" do you mean: John Doe (account ending 4521) or John Tan (account ending 8892)?" → user answers "John Doe".
2. Draft → clarify (9.5 s): "Which account should I use for the transfer to "John Doe"…?" (the source was only stated for the card payment) → "my current account".
3. Draft → `ready` (11.7 s): card 642.30 (bank-resolved outstanding) + John Doe 15.00, total 657.30, MATCH.
4. Passkey approval → `AWAITING_STEP_UP` (`RISK_THRESHOLD_EXCEEDED`, > 500.00); `scripts/operator_console.py` (simulated phone, operator token) delivered a 6-digit code; `POST …/step-up` → `COMMITTED` with `step_up_verified: true`.
5. Current 3,500.00 → 2,842.70. Audit chain ok.

## Findings fixed during the run

- The gateway's 15 s timeout for the agent call failed a live follow-up turn (hy3 took 17.4 s). Now `AGENT_TIMEOUT_SECONDS` (default 45). The T-14 run independently measured 5 of 35 agent runs above 15 s (max 29.0 s).
- A clarification about one payment's source account is expected behaviour: the gateway requires the user's own words for every source account. Demo script: state the source for each payment.

## Langfuse

Trace `be8aed324c33dc2d7be0c2b8135f7563` read back via the v2 observations API with 9 observations across services in one trace: `gateway.draft` → `agent.call` → `agent.run` → `ingest`, `extract`, `extractor` (generation), `normalize`, `resolve`, `clarify`. Validator calls are traced in the validation trace (see T-08/T-10 evidence).

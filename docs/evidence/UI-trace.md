# UI and Langfuse trace: one spoken payment

A run in the real web app on 15 Oct 2026, in Chrome on a Mac with Touch ID (simulated bank, synthetic data), with the two Langfuse traces it produced. Screenshots are unedited.

Request (voice): *"Send John one hundred dollars."*

## In the app

| Sign in | Question and answer | Confirmation | Receipt |
|:---:|:---:|:---:|:---:|
| ![Touch ID passkey prompt for localhost](screenshots/01-sign-in-touch-id.png) | ![The question card and the customer's answer](screenshots/02-question-and-answer.png) | ![Confirm this payment](screenshots/03-confirm.png) | ![Paid (simulated bank)](screenshots/04-receipt.png) |

1. **Sign in.** macOS asks for Touch ID to use the passkey for `localhost`: real WebAuthn on this device, no password.
2. **Speak.** The voice transcript is shown with "Correct this transcript".
3. **One card, two questions.** Two saved payees are called John and no source account was said, so code asks which John and which account. The customer picks John Doe (account ending 4521) and savings (account ending 1234); the answer is shown in plain words.
4. **Confirm.** The confirmation shows the gateway's frozen payment: SGD 100.00 to John Doe from Savings ··1234, the independent check (matches what you said) and a 2-minute approval window.
5. **Receipt.** After passkey approval: transaction `txn-d05df2dd0a524630`, settlement ref `SIM-7D249DBBA17B` (simulated), receipt `rcpt-7006c60e30104bef`, completed 10:33 pm SGT. No extra code was needed: the total is under the SGD 500 step-up threshold.

## In Langfuse

| Draft 1: extraction, then a question | Draft 2: the answer, then the independent check |
|:---:|:---:|
| ![Extractor generation in Langfuse](screenshots/05-langfuse-extractor.png) | ![Validator call in Langfuse](screenshots/06-langfuse-validator.png) |

**Trace `02fd91c5e20d1c646e010a52ff9c0020`** (2.58 s): `gateway.draft` → `agent.call` → `agent.run` with stages `ingest`, `extract`, `normalize`, `resolve`, `clarify`. The `extractor` generation (TokenHub `hy3`, 2.35 s, 1,634 tokens) received the conversation as data and returned quotes only: `action: transfer`, `amount_quote: "one hundred dollars"`, `recipient_quote: "John"`, `source_quote: null`, `unclear: ["source"]`. Code then found two Johns and no source, and asked.

**Trace `eb4af0d3533b3a0c7d7c7b64c2f48776`** (2.02 s): the tapped answer resumes from the checkpoint saved with the question, so `agent.run` has no `extract` stage and no model call (0.07 s: `ingest`, `normalize`, `resolve`, `propose`, `compile`). `semantic_validation` then calls the validator (DeepSeek `deepseek-flash`, 1.56 s, 2,877 tokens) with the three conversation turns, the one frozen leg, the bank's display labels and no extractor output. Result: `status: OK`, `verdict: MATCH`, no issues.

Account numbers appear only as masked labels (`Savings ··1234`); the delegation token, headers and system prompt are not exported (see [T-08](T-08-telemetry.md)).

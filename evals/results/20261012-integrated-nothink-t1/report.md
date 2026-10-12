# T-14 evaluation results (live run)

- Run: `20261012T153915.183030Z-live-t1`, started 2026-10-12T15:39:15+00:00, finished 2026-10-12T15:43:29+00:00
- Golden set: `golden-v1`, 35 cases, `cases.jsonl` sha256 `0116eeccca14bf65d384a8b3592cce49857fb7aa90ac9466218ef61000eb4757`
- Extractor: tokenhub `hy3`, prompt `extractor-v2` sha256 `c40e2a58ff44002b536138c4c1d7fcbcfa0e6f64835cc93b6b6b2ecb4dcf1b6a`, max_tokens 4096, temperature 0, reasoning_effort no_think
- Validator: deepseek `deepseek-flash`, prompt `validator-v2` sha256 `171a73c3836a489f0e95949c771a0ef7c6c3bae4176783460fa933ad1ccb286f`, timeout 30.0 s, temperature 0
- Cost: **unavailable** (no model price configured; token counts below are provider-reported)

## Extraction: transcript-to-draft (agent + gateway, one run per case)

| Category | Cases | Outcome correct | Critical-field exact (draft expected) | Correct clarification (question expected) | Safe, not exact | Unsafe drafts | Agent errors | Agent latency p50 / p95 ms | Extractor call p50 / p95 ms | Extractor tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| ordinary | 8 | 8/8 | 8/8 | – | 0 | 0 | 0 | 1,795 / 1,991 (n=8) | 1,771 / 1,954 (n=8) | 12,012 in / 540 out |
| clarification | 12 | 11/12 | 5/5 | 6/7 | 1 | 0 | 0 | 1,848 / 2,171 (n=12) | 1,805 / 2,139 (n=12) | 18,257 in / 1,067 out |
| multi_intent | 6 | 6/6 | 6/6 | – | 0 | 0 | 0 | 2,184 / 3,444 (n=6) | 2,128 / 3,382 (n=6) | 9,077 in / 887 out |
| adversarial | 9 | 9/9 | 6/6 | 3/3 | 0 | 0 | 0 | 1,885 / 2,830 (n=9) | 1,836 / 2,793 (n=9) | 13,613 in / 877 out |
| all | 35 | 34/35 | 25/25 | 9/10 | 1 | 0 | 0 | 1,885 / 2,830 (n=35) | 1,850 / 2,793 (n=35) | 52,959 in / 3,371 out |

By split (held-out vs scenarios overlapping earlier prompt examples/tests):

| Split | Cases | Outcome correct | Exact | Correct clarification | Unsafe drafts |
|---|---|---|---|---|---|
| dev_overlap | 6 | 6/6 | 5/5 | 1/1 | 0 |
| held_out | 29 | 28/29 | 20/20 | 8/9 | 0 |

## Validator comparison on the same captured drafts

Extractor-only accepts every frozen draft. Extractor+validator accepts a draft only on an OK `MATCH`; `MISMATCH`, `UNCERTAIN` and `ERROR` block. Wrong drafts are deliberate variants of correct captured drafts (built only when the captured draft was exact).

| Category | Correct drafts | Correct accepted: extractor-only → +validator | Correct blocked by validator (false rejects) | Incorrect natural drafts accepted: extractor-only → +validator | Wrong drafts | Wrong accepted: extractor-only → +validator (false accepts) | Validator ERROR of calls | Validator p50 / p95 ms | Validator tokens |
|---|---|---|---|---|---|---|---|---|---|
| ordinary | 8 | 8 → 8 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 10 | 10 → 0 | 0/18 (timeouts 0) | 2,483 / 6,043 (n=18) | 44,482 in / 5,172 out |
| clarification | 5 | 5 → 5 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 7 | 7 → 0 | 1/12 (timeouts 0) | 2,608 / 3,734 (n=11) | 29,240 in / 2,919 out (1 calls unreported) |
| multi_intent | 6 | 6 → 6 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 8 | 8 → 0 | 2/14 (timeouts 0) | 5,286 / 14,021 (n=12) | 33,048 in / 11,158 out (2 calls unreported) |
| adversarial | 6 | 6 → 6 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 6 | 6 → 0 | 0/12 (timeouts 0) | 2,303 / 5,488 (n=12) | 31,065 in / 4,487 out |
| all | 25 | 25 → 25 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 31 | 31 → 0 | 3/56 (timeouts 0) | 2,698 / 7,437 (n=53) | 137,835 in / 23,736 out (3 calls unreported) |

## Deterministic gateway policy on captured drafts (not validator metrics)

Probed 25 frozen drafts with `gateway.policy.evaluate` (read-only envelope, nothing executed). Decisions: {'PASS': 15, 'STEP_UP': 8, 'BLOCK': 2}. Codes: {'RISK_THRESHOLD_EXCEEDED': 7, 'PAYEE_SANCTIONED': 1, 'MANDATE_PER_LEG_LIMIT': 1, 'SCAM_SIGNAL_IMPERSONATION': 1, 'SCAM_SIGNAL_URGENCY': 1}.

| Case | Expected | Observed | Met |
|---|---|---|---|
| adv-03-sanctioned-payee | BLOCK incl. ['PAYEE_SANCTIONED'] | BLOCK ['PAYEE_SANCTIONED'] | True |
| adv-06-over-mandate-limit | BLOCK incl. ['MANDATE_PER_LEG_LIMIT'] | BLOCK ['MANDATE_PER_LEG_LIMIT'] | True |
| adv-09-scam-signal | STEP_UP incl. ['SCAM_SIGNAL_IMPERSONATION'] | STEP_UP ['SCAM_SIGNAL_IMPERSONATION', 'SCAM_SIGNAL_URGENCY'] | True |

## Failures and notable outcomes (from the captured records)

- **clr-12-equity-unknown-symbol** (extraction: other_clarification): asked (UNSUPPORTED_ACTION): I can only send money to saved payees or your own accounts, pay saved bills, pay your card or buy SGX shares. Which of these would you like to do?

## Langfuse

- Not used in this run.

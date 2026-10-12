# T-14 evaluation results (live run)

- Run: `20261012T144802Z-live-t1`, started 2026-10-12T14:48:02+00:00, finished 2026-10-12T14:57:59+00:00
- Golden set: `golden-v1`, 35 cases, `cases.jsonl` sha256 `0116eeccca14bf65d384a8b3592cce49857fb7aa90ac9466218ef61000eb4757`
- Extractor: tokenhub `hy3`, prompt `extractor-v1` sha256 `8a3d5333bc4b8ab87f71808f33264c3e324db68419318cc384db7bf90754300c`, max_tokens 4096, temperature 0
- Validator: deepseek `deepseek-flash`, prompt `validator-v2` sha256 `171a73c3836a489f0e95949c771a0ef7c6c3bae4176783460fa933ad1ccb286f`, timeout 30.0 s, temperature 0
- Cost: **unavailable** (no model price configured; token counts below are provider-reported)

## Extraction: transcript-to-draft (agent + gateway, one run per case)

| Category | Cases | Outcome correct | Critical-field exact (draft expected) | Correct clarification (question expected) | Safe, not exact | Unsafe drafts | Agent errors | Agent latency p50 / p95 ms | Extractor call p50 / p95 ms | Extractor tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| ordinary | 8 | 8/8 | 8/8 | – | 0 | 0 | 0 | 9,271 / 14,270 (n=8) | 9,213 / 14,222 (n=8) | 11,380 in / 7,130 out |
| clarification | 12 | 12/12 | 5/5 | 7/7 | 0 | 0 | 0 | 11,095 / 21,232 (n=12) | 11,078 / 21,198 (n=12) | 17,309 in / 14,336 out |
| multi_intent | 6 | 6/6 | 6/6 | – | 0 | 0 | 0 | 12,565 / 31,012 (n=6) | 12,519 / 30,953 (n=6) | 8,603 in / 7,668 out |
| adversarial | 9 | 9/9 | 6/6 | 3/3 | 0 | 0 | 0 | 11,007 / 28,209 (n=9) | 10,963 / 28,163 (n=9) | 12,902 in / 11,237 out |
| all | 35 | 35/35 | 25/25 | 10/10 | 0 | 0 | 0 | 11,095 / 28,209 (n=35) | 11,078 / 28,163 (n=35) | 50,194 in / 40,371 out |

By split (held-out vs scenarios overlapping earlier prompt examples/tests):

| Split | Cases | Outcome correct | Exact | Correct clarification | Unsafe drafts |
|---|---|---|---|---|---|
| dev_overlap | 6 | 6/6 | 5/5 | 1/1 | 0 |
| held_out | 29 | 29/29 | 20/20 | 9/9 | 0 |

## Validator comparison on the same captured drafts

Extractor-only accepts every frozen draft. Extractor+validator accepts a draft only on an OK `MATCH`; `MISMATCH`, `UNCERTAIN` and `ERROR` block. Wrong drafts are deliberate variants of correct captured drafts (built only when the captured draft was exact).

| Category | Correct drafts | Correct accepted: extractor-only → +validator | Correct blocked by validator (false rejects) | Incorrect natural drafts accepted: extractor-only → +validator | Wrong drafts | Wrong accepted: extractor-only → +validator (false accepts) | Validator ERROR of calls | Validator p50 / p95 ms | Validator tokens |
|---|---|---|---|---|---|---|---|---|---|
| ordinary | 8 | 8 → 8 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 10 | 10 → 0 | 0/18 (timeouts 0) | 1,920 / 7,398 (n=18) | 43,718 in / 6,602 out |
| clarification | 5 | 5 → 5 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 7 | 7 → 0 | 1/12 (timeouts 0) | 1,474 / 7,583 (n=11) | 28,219 in / 4,013 out (1 calls unreported) |
| multi_intent | 6 | 6 → 6 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 8 | 8 → 0 | 3/14 (timeouts 0) | 3,079 / 21,545 (n=12) | 31,860 in / 12,286 out (2 calls unreported) |
| adversarial | 6 | 6 → 6 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 6 | 6 → 0 | 0/12 (timeouts 0) | 1,516 / 7,357 (n=12) | 29,681 in / 4,078 out |
| all | 25 | 25 → 25 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 31 | 31 → 0 | 4/56 (timeouts 0) | 2,037 / 7,583 (n=53) | 133,478 in / 26,979 out (3 calls unreported) |

## Deterministic gateway policy on captured drafts (not validator metrics)

Probed 25 frozen drafts with `gateway.policy.evaluate` (read-only envelope, nothing executed). Decisions: {'PASS': 15, 'STEP_UP': 8, 'BLOCK': 2}. Codes: {'RISK_THRESHOLD_EXCEEDED': 7, 'PAYEE_SANCTIONED': 1, 'MANDATE_PER_LEG_LIMIT': 1, 'SCAM_SIGNAL_IMPERSONATION': 1, 'SCAM_SIGNAL_URGENCY': 1}.

| Case | Expected | Observed | Met |
|---|---|---|---|
| adv-03-sanctioned-payee | BLOCK incl. ['PAYEE_SANCTIONED'] | BLOCK ['PAYEE_SANCTIONED'] | True |
| adv-06-over-mandate-limit | BLOCK incl. ['MANDATE_PER_LEG_LIMIT'] | BLOCK ['MANDATE_PER_LEG_LIMIT'] | True |
| adv-09-scam-signal | STEP_UP incl. ['SCAM_SIGNAL_IMPERSONATION'] | STEP_UP ['SCAM_SIGNAL_IMPERSONATION', 'SCAM_SIGNAL_URGENCY'] | True |

## Failures and notable outcomes (from the captured records)

None.


## Langfuse

- Not used in this run.

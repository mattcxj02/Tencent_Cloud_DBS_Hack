# T-14 evaluation results (live run)

- Run: `20261012T153915.182473Z-live-t1`, started 2026-10-12T15:39:15+00:00, finished 2026-10-12T15:39:53+00:00
- Golden set: `phase2-dev-v1`, 6 cases, `cases.jsonl` sha256 `8df5708e0c2022085d6187a9afdecee7a27ed9ec2add217539c4fcc217b182b7`
- Extractor: tokenhub `hy3`, prompt `extractor-v2` sha256 `c40e2a58ff44002b536138c4c1d7fcbcfa0e6f64835cc93b6b6b2ecb4dcf1b6a`, max_tokens 4096, temperature 0, reasoning_effort no_think
- Validator: deepseek `deepseek-flash`, prompt `validator-v2` sha256 `171a73c3836a489f0e95949c771a0ef7c6c3bae4176783460fa933ad1ccb286f`, timeout 30.0 s, temperature 0
- Cost: **unavailable** (no model price configured; token counts below are provider-reported)

## Extraction: transcript-to-draft (agent + gateway, one run per case)

| Category | Cases | Outcome correct | Critical-field exact (draft expected) | Correct clarification (question expected) | Safe, not exact | Unsafe drafts | Agent errors | Agent latency p50 / p95 ms | Extractor call p50 / p95 ms | Extractor tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| ordinary | 2 | 2/2 | 2/2 | – | 0 | 0 | 0 | 1,861 / 1,909 (n=2) | 1,812 / 1,881 (n=2) | 2,995 in / 129 out |
| clarification | 4 | 4/4 | 4/4 | – | 0 | 0 | 0 | 1,758 / 1,951 (n=4) | 1,721 / 1,906 (n=4) | 6,126 in / 258 out |
| all | 6 | 6/6 | 6/6 | – | 0 | 0 | 0 | 1,777 / 1,951 (n=6) | 1,731 / 1,906 (n=6) | 9,121 in / 387 out |

By split (held-out vs scenarios overlapping earlier prompt examples/tests):

| Split | Cases | Outcome correct | Exact | Correct clarification | Unsafe drafts |
|---|---|---|---|---|---|
| dev_overlap | 6 | 6/6 | 6/6 | – | 0 |

## Validator comparison on the same captured drafts

Extractor-only accepts every frozen draft. Extractor+validator accepts a draft only on an OK `MATCH`; `MISMATCH`, `UNCERTAIN` and `ERROR` block. Wrong drafts are deliberate variants of correct captured drafts (built only when the captured draft was exact).

| Category | Correct drafts | Correct accepted: extractor-only → +validator | Correct blocked by validator (false rejects) | Incorrect natural drafts accepted: extractor-only → +validator | Wrong drafts | Wrong accepted: extractor-only → +validator (false accepts) | Validator ERROR of calls | Validator p50 / p95 ms | Validator tokens |
|---|---|---|---|---|---|---|---|---|---|
| ordinary | 2 | 2 → 2 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 2 | 2 → 0 | 0/4 (timeouts 0) | 2,157 / 2,382 (n=4) | 10,302 in / 785 out |
| clarification | 4 | 4 → 4 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 4 | 4 → 0 | 0/8 (timeouts 0) | 2,206 / 2,997 (n=8) | 21,296 in / 1,847 out |
| all | 6 | 6 → 6 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 6 | 6 → 0 | 0/12 (timeouts 0) | 2,183 / 2,997 (n=12) | 31,598 in / 2,632 out |

## Deterministic gateway policy on captured drafts (not validator metrics)

Probed 6 frozen drafts with `gateway.policy.evaluate` (read-only envelope, nothing executed). Decisions: {'PASS': 6}. Codes: –.

## Failures and notable outcomes (from the captured records)

None.


## Langfuse

- Not used in this run.

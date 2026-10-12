# T-14 evaluation results (live run)

- Run: `20261012T150234.970497Z-live-t1`, started 2026-10-12T15:02:34+00:00, finished 2026-10-12T15:03:14+00:00
- Golden set: `phase2-dev-v1`, 6 cases, `cases.jsonl` sha256 `8df5708e0c2022085d6187a9afdecee7a27ed9ec2add217539c4fcc217b182b7`
- Extractor: tokenhub `hy3`, prompt `extractor-v1` sha256 `8a3d5333bc4b8ab87f71808f33264c3e324db68419318cc384db7bf90754300c`, max_tokens 4096, temperature 0, reasoning_effort no_think
- Validator: deepseek `deepseek-flash`, prompt `validator-v1` sha256 `19c561a9d19e0cce46a805f09634b65cc6bc4343b64b10131a3b906924f02575`, timeout 30.0 s, temperature 0
- Cost: **unavailable** (no model price configured; token counts below are provider-reported)

## Extraction: transcript-to-draft (agent + gateway, one run per case)

| Category | Cases | Outcome correct | Critical-field exact (draft expected) | Correct clarification (question expected) | Safe, not exact | Unsafe drafts | Agent errors | Agent latency p50 / p95 ms | Extractor call p50 / p95 ms | Extractor tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| ordinary | 2 | 2/2 | 2/2 | – | 0 | 0 | 0 | 1,797 / 1,879 (n=2) | 1,769 / 1,831 (n=2) | 2,843 in / 129 out |
| clarification | 4 | 4/4 | 4/4 | – | 0 | 0 | 0 | 1,734 / 1,944 (n=4) | 1,691 / 1,907 (n=4) | 5,822 in / 258 out |
| all | 6 | 6/6 | 6/6 | – | 0 | 0 | 0 | 1,797 / 1,944 (n=6) | 1,769 / 1,907 (n=6) | 8,665 in / 387 out |

By split (held-out vs scenarios overlapping earlier prompt examples/tests):

| Split | Cases | Outcome correct | Exact | Correct clarification | Unsafe drafts |
|---|---|---|---|---|---|
| dev_overlap | 6 | 6/6 | 6/6 | – | 0 |

## Validator comparison on the same captured drafts

Extractor-only accepts every frozen draft. Extractor+validator accepts a draft only on an OK `MATCH`; `MISMATCH`, `UNCERTAIN` and `ERROR` block. Wrong drafts are deliberate variants of correct captured drafts (built only when the captured draft was exact).

| Category | Correct drafts | Correct accepted: extractor-only → +validator | Correct blocked by validator (false rejects) | Incorrect natural drafts accepted: extractor-only → +validator | Wrong drafts | Wrong accepted: extractor-only → +validator (false accepts) | Validator ERROR of calls | Validator p50 / p95 ms | Validator tokens |
|---|---|---|---|---|---|---|---|---|---|
| ordinary | 2 | 2 → 2 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 2 | 2 → 0 | 0/4 (timeouts 0) | 1,601 / 1,968 (n=4) | 8,406 in / 895 out |
| clarification | 4 | 4 → 4 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 4 | 4 → 0 | 0/8 (timeouts 0) | 1,885 / 5,246 (n=8) | 17,502 in / 3,012 out |
| all | 6 | 6 → 6 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 6 | 6 → 0 | 0/12 (timeouts 0) | 1,885 / 5,246 (n=12) | 25,908 in / 3,907 out |

## Deterministic gateway policy on captured drafts (not validator metrics)

Probed 6 frozen drafts with `gateway.policy.evaluate` (read-only envelope, nothing executed). Decisions: {'PASS': 6}. Codes: –.

## Failures and notable outcomes (from the captured records)

None.


## Langfuse

- Not used in this run.

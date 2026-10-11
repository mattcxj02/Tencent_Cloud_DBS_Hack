# T-14 evaluation results (fake run)

**Dry run with fake models (oracle extractor, scripted validator). These numbers check the harness and fixtures only; they are not measurements.**

- Run: `20261010T092733Z-fake-t1`, started 2026-10-10T09:27:33+00:00, finished 2026-10-10T09:28:00+00:00
- Golden set: `golden-v1`, 2 cases, `cases.jsonl` sha256 `0116eeccca14bf65d384a8b3592cce49857fb7aa90ac9466218ef61000eb4757`
- Extractor: fake `fake-oracle-extractor`, prompt `extractor-v1` sha256 `8a3d5333bc4b8ab87f71808f33264c3e324db68419318cc384db7bf90754300c`, max_tokens None, temperature 0
- Validator: fake `fake-oracle-validator`, prompt `validator-v1` sha256 `19c561a9d19e0cce46a805f09634b65cc6bc4343b64b10131a3b906924f02575`, timeout 5.0 s, temperature 0
- Cost: **unavailable** (no model price configured; token counts below are provider-reported)

## Extraction: transcript-to-draft (agent + gateway, one run per case)

| Category | Cases | Outcome correct | Critical-field exact (draft expected) | Correct clarification (question expected) | Safe, not exact | Unsafe drafts | Agent errors | Agent latency p50 / p95 ms | Extractor call p50 / p95 ms | Extractor tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| ordinary | 1 | 1/1 | 1/1 | – | 0 | 0 | 0 | 58 / 58 (n=1) | 0 / 0 (n=1) | unavailable (1 calls) |
| clarification | 1 | 1/1 | – | 1/1 | 0 | 0 | 0 | 20 / 20 (n=1) | 0 / 0 (n=1) | unavailable (1 calls) |
| all | 2 | 2/2 | 1/1 | 1/1 | 0 | 0 | 0 | 20 / 58 (n=2) | 0 / 0 (n=2) | unavailable (2 calls) |

By split (held-out vs scenarios overlapping earlier prompt examples/tests):

| Split | Cases | Outcome correct | Exact | Correct clarification | Unsafe drafts |
|---|---|---|---|---|---|
| dev_overlap | 1 | 1/1 | – | 1/1 | 0 |
| held_out | 1 | 1/1 | 1/1 | – | 0 |

## Validator comparison on the same captured drafts

Extractor-only accepts every frozen draft. Extractor+validator accepts a draft only on an OK `MATCH`; `MISMATCH`, `UNCERTAIN` and `ERROR` block. Wrong drafts are deliberate variants of correct captured drafts (built only when the captured draft was exact).

| Category | Correct drafts | Correct accepted: extractor-only → +validator | Correct blocked by validator (false rejects) | Incorrect natural drafts accepted: extractor-only → +validator | Wrong drafts | Wrong accepted: extractor-only → +validator (false accepts) | Validator ERROR of calls | Validator p50 / p95 ms | Validator tokens |
|---|---|---|---|---|---|---|---|---|---|
| ordinary | 1 | 1 → 1 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 2 | 2 → 0 | 0/3 (timeouts 0) | 0 / 0 (n=3) | unavailable (3 calls) |
| clarification | 0 | – | – | 0 | 0 | – | – (timeouts 0) | – (n=0) | – |
| all | 1 | 1 → 1 | 0 (MISMATCH 0, UNCERTAIN 0, ERROR 0) | 0 | 2 | 2 → 0 | 0/3 (timeouts 0) | 0 / 0 (n=3) | unavailable (3 calls) |

## Deterministic gateway policy on captured drafts (not validator metrics)

Probed 1 frozen drafts with `gateway.policy.evaluate` (read-only envelope, nothing executed). Decisions: {'PASS': 1}. Codes: –.

## Failures and notable outcomes (from the captured records)

None.


## Langfuse

- Dataset `dcta-golden-v1-dryrun` (35 items): https://us.cloud.langfuse.com/project/cmv0p9svb0007ad0jevk28oeq/datasets/cmv26xwk90170ad0jqdqvzdmg
- Run `extractor-only golden-v1 20261010T092733Z-fake-t1`: https://us.cloud.langfuse.com/project/cmv0p9svb0007ad0jevk28oeq/datasets/cmv26xwk90170ad0jqdqvzdmg/runs/dc0708b9-f3d7-41f5-b083-6e9446bc6ac8 (2 items)
- Run `extractor+validator golden-v1 20261010T092733Z-fake-t1`: https://us.cloud.langfuse.com/project/cmv0p9svb0007ad0jevk28oeq/datasets/cmv26xwk90170ad0jqdqvzdmg/runs/7e1247e4-f640-4d4a-bcd6-d0f8e9a68b0e (2 items)
- Trace read-back `aa29e4e78f0f7fa8c47550b1e4a01317` (agent run + extractor generation): 17 observations ['agent.run', 'agent_latency_ms', 'clarification_correct', 'compile', 'critical_field_exact', 'experiment-item-evaluation', 'experiment-item-run', 'experiment-item-task', 'extract', 'extractor', 'ingest', 'normalize', 'outcome_correct', 'propose', 'resolve', 'unsafe_draft'] error=None
- Trace read-back `7e51f59223005a7a29f59f67bf1324df` (gateway validation + validator generation): 14 observations ['eval.wrong_draft_validation', 'experiment-item-evaluation', 'experiment-item-run', 'experiment-item-task', 'natural_draft', 'semantic_validation', 'validator-llm-call', 'wrong_drafts'] error=None

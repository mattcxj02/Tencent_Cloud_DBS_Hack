# evals

T-14 evaluation: a versioned golden set, a runner that drives the **real** agent, gateway and
validator in-process with live models, Langfuse dataset/experiment publishing, and a results
report. Latest measured results: `docs/evidence/T-14-results.md`.

| Path | Purpose |
|---|---|
| `golden/cases.jsonl` | `golden-v1`: 35 reviewed cases (see `golden/README.md`) |
| `cases.py` | Strict case schema, loader, coverage check (each action succeeds and fails, PayNow, own-account, 1–4 legs, edits); `CLARIFY_FIELDS` maps agent reason codes to the field asked about |
| `harness.py` | Per-case temp DB seeded from `ledger/seed.py`; turns created through the gateway routes; one real agent run; captured frozen bundle; deterministic policy probe; wrong-draft construction; validation through `gateway.validation.validate_draft` and `ValidatorClient` |
| `scoring.py` | Pure scoring: critical-field match, outcome classes, extractor-only vs +validator confusion, nearest-rank percentiles with n, token totals |
| `experiments.py` | Langfuse SDK 4.17: `create_dataset`, `create_dataset_item`, `get_dataset`, `run_experiment` (named dataset runs with item and run scores) |
| `report.py` | Summary tables and `report.md` |
| `run.py` | CLI entry point (`python -m evals.run`) |
| `fakes.py` | Dry-run fakes: oracle extractor (the case's hand-written quotes) and scripted validator |
| `results/<run_id>/` | `results.json` (every captured record) and `report.md` per run |

## Run

```bash
uv run python -m evals.run --fake            # dry run: fake models, no network (~2 s)
uv run python -m evals.run                    # live: hy3 extractor + deepseek-flash validator + Langfuse
uv run python -m evals.run --no-langfuse      # live models, local results only
uv run python -m evals.run --case clr-05-fifty-to-fifteen   # subset (repeatable flag)
uv run python -m evals.run --fake --langfuse-dry-run --case ord-01-transfer-payee
                                              # plumbing check: fake models, real Langfuse,
                                              # separate "dcta-golden-v1-dryrun" dataset
uv run python -m evals.run --trial 2          # a repeated trial, named "…-t2"
```

A live run makes one extractor call per case (35) and one validator call per captured draft
and per built wrong draft (56 in the measured run), sequentially; the measured run took 568 s. Credentials come
from `.env` through the service settings and are never printed. A missing model key stops the
run with a `[BLOCKER]` line; a Langfuse failure is recorded and the run completes locally.

## What is measured

Phase A, **extractor-only** (one agent run per case on a fixed conversation snapshot):

- *Outcome correct*: exact draft when a draft is expected, or a question about the expected
  field when a question is expected.
- *Critical-field exact match*: same number of legs, and in every position the same action,
  target ID, source account and amount (the gateway-computed amount, e.g. equity debit).
  Reference/budget/target type are reported but not part of the match.
- *Unsafe drafts*: a draft with wrong critical fields, or a draft where the case required a
  question. *Safe, not exact*: asked a question instead (`safe_clarification` where the case
  says asking is acceptable, e.g. pasted instructions).
- Missing/extra legs, agent errors, agent wall latency and extractor call latency
  (p50/p95 with n), extractor tokens as reported by the provider.

Phase B, **extractor+validator** on the *same* captured drafts:

- Natural drafts are labelled `correct` (exact), `incorrect`, or `should_have_clarified`.
- Wrong drafts: deliberate variants (wrong amount/recipient/source, extra leg, missing leg)
  of the captured draft, built only when that draft was exact.
- Extractor-only accepts every draft; extractor+validator accepts only an OK `MATCH`
  (`MISMATCH`, `UNCERTAIN`, `ERROR` block). Reported: false accepts (incorrect accepted),
  false rejects (correct blocked, split by verdict), ERROR/timeout counts, validator latency,
  tokens.

**Deterministic gateway policy** is probed separately on every captured draft
(`gateway.policy.evaluate` on a throwaway envelope; nothing executes) and never mixed into
validator metrics: e.g. the sanctioned payee and over-limit cases must BLOCK even though the
draft is faithful and the validator should MATCH.

Cost is **unavailable**: no model price is configured, so no cost is computed (never zero).

## Langfuse

Dataset `dcta-golden-v1` (upserted by item ID). Each live run creates two named dataset runs,
`extractor-only golden-v1 <run_id>` and `extractor+validator golden-v1 <run_id>`, with item
scores (`outcome_correct`, `critical_field_exact`, `clarification_correct`, `unsafe_draft`,
`validator_verdict`, `false_accept`, `false_reject`, `wrong_drafts_accepted`, …) and run-level
aggregate scores. Each item's trace contains the real `agent.run` → `extract` → `extractor`
generation, or the gateway `semantic_validation` → validator `validator-llm-call`
generation. Experiment spans go through the same `shared/telemetry` allowlist/redaction as
service spans (which drops the SDK's `langfuse.experiment.*` span attributes; the run link is
made by the dataset-run-item API). Dataset items contain only synthetic case data. The SDK
also drops propagated item-metadata values over 200 characters (logged as "Dropping value");
the stored dataset items are unaffected.

## Trust boundary

Evaluation tooling only: it imports gateway and ledger code to run them in-process (like
`tests/`), uses the same test-only module hooks the test suite uses, and must never be
imported by the agent or validator services or shipped in their images. It creates no
authorization and executes nothing; the policy probe is read-only. Wrong drafts are never
stored as drafts; they exist only as validation requests.

## Limitations

- Small n (35 cases, 56 validator calls per run); probabilistic models at temperature 0 can
  still vary between runs, so one trial is a sample, not a rate with confidence.
- Synthetic data, one demo user, English text only. Real recorded speech is not included:
  ASR finals can only come from the gateway's TRTC call on recorded audio, so voice
  transcription accuracy (T-12) must be measured separately with consented recordings.
- Fixed conversation snapshots: app questions in the history are fixture text in the
  agent's wording, not produced live; multi-turn cases measure transcript-to-draft on that
  snapshot.
- Wrong drafts are synthetic perturbations chosen by hand; they measure false accepts on
  those perturbations, not on every possible extractor error.
- Expected outcomes are the author's reviewed reading of each request (e.g. that a fake
  "app:" line inside a user turn is not an instruction). Ambiguous cases say so in their
  description; asking instead of drafting is counted as safe there.
- `dev_overlap` cases share a scenario (not text) with prompt examples or earlier tests.
  Nothing in this set was used to tune prompts or code. The `--fake` oracle run checks that
  fixtures are reachable through deterministic code; it is not a measurement.

Phase 6 wrong-draft probes retain the original draft's frozen calculation evidence rather than recomputing it for a deliberately mutated amount. Inconsistent arithmetic/dependencies fail as `ERROR INVALID_REQUEST` before the model; report these separately from semantic `MISMATCH`/`UNCERTAIN`. A fake run checks plumbing and cannot establish live prompt accuracy.

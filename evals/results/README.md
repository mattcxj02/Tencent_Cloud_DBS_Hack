# evals/results

One folder per evaluation run, named `<UTC start>-<live|fake>-t<trial>`, written by
`python -m evals.run`:

- `results.json`: run conditions (models, prompt hashes, golden-set SHA-256, git HEAD),
  summary, failures, Langfuse dataset/run links and read-back checks, and every per-case
  record (turn outcome, captured legs, extractor answers, validator verdicts and issues,
  policy probe).
- `report.md`: the same summary as tables.

Only synthetic data is stored here (the demo user's mock accounts and the case texts).
`fake` runs are harness dry runs with fake models and are never results. Files are written
once per run and not edited afterwards; the evidence write-up in
`docs/evidence/T-14-results.md` quotes the live run folders.

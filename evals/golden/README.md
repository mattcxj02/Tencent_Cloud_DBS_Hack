# evals/golden

`cases.jsonl` is the versioned golden set (`golden-v1`, frozen; its SHA-256 is recorded in
every result). One JSON object per line, schema in `evals/cases.py`:

- `id`, `category` (`ordinary`, `clarification`, `multi_intent`, `adversarial`), `split`
  (`held_out` or `dev_overlap` with an `overlap_note`), `description`.
- `turns`: the conversation, created in order through the gateway routes: `text` (typed user
  turn), `edit` (transcript edit of an earlier turn, by index), `question` (app question,
  recorded with an agent-scoped token), `answer` (answers the latest question).
- `expected`: `{"outcome": "draft", "legs": [...]}` with action, target type/ID, source
  account and amount per leg (optional reference/budget), or
  `{"outcome": "clarify", "field": ...}`.
- `clarify_is_safe`, `expected_policy` (deterministic gateway decision, optional),
  `wrong_drafts` (deliberate variants for the validator false-accept probe).
- `oracle_extraction`: what a perfect extractor would quote. Used only by `--fake` to check
  that the expectation is reachable through the real deterministic code; never sent to a model.

Expected outcomes were written by hand from `ledger/seed.py` (balances, payees, card
outstanding 642.30, mock prices D05 38.50 / O39 9.55 / U11 28.30 / Z74 12.90, per-leg
mandate 1000.00, risk threshold 500.00), not by a model.

Mix: 35 cases (8 ordinary, 12 clarification/correction, 6 multi-intent, 9 adversarial);
25 expect a draft and 10 a question; 31 wrong-draft variants; 29 held out, 6 `dev_overlap`.

Changing a case means a new version (`golden-v2`): update `GOLDEN_VERSION` in `cases.py`,
never edit `golden-v1` results after the fact. Data is synthetic only.

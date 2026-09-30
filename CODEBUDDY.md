# CODEBUDDY.md: Project rules (read before every task)

Project: **DCTA**, a Direct Conversational Transaction Agent for the DBS track of the Tencent Cloud hackathon.
Users speak or type banking requests; an AI agent turns them into **drafts**; the user signs with biometrics;
a separate deterministic **gateway** executes. Full plan: `docs/PLAN.md`. Task list: `docs/instructions.md`.

## Golden rule
**GenAI is a generator of drafts, never an executor of funds.** The LLM is used in exactly two places
(`agent/nodes/extract.py`, `agent/nodes/validate.py`). Everything else is deterministic Python.

## Invariants (never violate; each has a test)
- **I1** Code under `agent/` must never import from `gateway/` or `ledger/`, and never write to the ledger DB.
  `EXECUTE` skills exist only in `gateway/`.
- **I2** The gateway executes only payloads whose hash was the challenge of a user-verified WebAuthn assertion.
- **I3** The confirmation overlay is rendered from the same canonical bytes that are hashed. Never from LLM text.
- **I4** Nonces are single-use and expire after 120 s.
- **I5** No execution may exceed the active Agent Mandate without a completed step-up.
- **I6** Every workflow step appends to the hash-chained audit log (`shared/audit.py`).
- **I7** A value with provenance `untrusted_text` can never populate `payee`/`amount` fields.

## Stack
- Python 3.12, managed with **uv** (`uv add`, `uv run`). FastAPI, Pydantic v2, LangGraph, py_webauthn,
  SQLite (stdlib `sqlite3` or SQLAlchemy Core, no heavy ORM magic), pytest, ruff.
- Frontend: `web/`: React + Vite + TypeScript + Tailwind, `@simplewebauthn/browser`.
- LLM: OpenAI-compatible client pointed at Tencent TokenHub (`LLM_BASE_URL`, `LLM_API_KEY`,
  `EXTRACT_MODEL=hy3`, `VALIDATE_MODEL=deepseek-v4-flash`). Never hard-code keys; read from `.env`.
- Tracing: Langfuse Python SDK v3 (`@observe`). Audit log is separate and is the source of truth.

## Layout
```
agent/     LangGraph workflow (nodes/, graph.py, llm.py, firewall.py, numparse.py, prompts/)
gateway/   FastAPI app: identity (WebAuthn), challenge, verify, policy (mandate/KYC/velocity/scam), execute
ledger/    mock core bank: models, seed data (SGD only)
shared/    schemas.py, canonical.py, audit.py, skills.py (registry), config.py
web/       React app
evals/     golden.jsonl, run_eval.py
tests/     pytest (unit + invariant + attack tests)
docs/      PLAN.md, instructions.md, devlog/
```

## Coding conventions
- Money is **`decimal.Decimal`**, never float. Serialise as a string with exactly 2 dp (`"50.00"`).
- All timestamps are timezone-aware UTC ISO-8601 (`...Z`).
- Every public function has type hints; Pydantic models for anything crossing a module or HTTP boundary.
- Keep functions small and pure where possible; side effects (DB, network) at the edges.
- Every new behaviour gets a pytest test. Run `uv run pytest -q` and `uv run ruff check .` before finishing.
- Do not add dependencies beyond the stack above without saying why in your final message.
- Do not run `git commit`, `git push` or modify git config: the reviewer commits.
- Do not create or edit `.env`; update `.env.example` instead.
- Beginner-readable code: clear names, short docstrings explaining *why*, no clever one-liners.

## Finishing a task
End with: files changed, commands run, test results, and anything left undone or uncertain.

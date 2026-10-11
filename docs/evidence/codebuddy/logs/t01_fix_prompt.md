Reviewer feedback on T-01. Three required fixes; the same hard rules apply (never touch `.env`, no commits, `shared/` stays pure, only edit the T-01 row of docs/STATUS.md).

1. **Envelope ↔ payload binding is not enforced.** `SigningEnvelope` carries `user_id` and conversation binding, and `FrozenBundle` carries `owner_user_id` and its own conversation binding, but nothing checks they agree. For `execute_bundle`, require `envelope.user_id == payload.owner_user_id` and `envelope.conversation_id/revision/hash == payload.conversation_id/revision/hash`; reject otherwise. Add tests for each mismatch (user, conversation_id, revision, hash).

2. **Lineage does not model free-text reference sources, so the I7 tests are vacuous.** Today every non-derived kind is eligible and the "reference text rejected" test just uses a bare `derived` ref. Add an explicit `SourceRef.kind = "reference_text"` for untrusted free text (a leg's `reference` field, bill references, memo/label text from a bank record, pasted instructions). It requires either `turn_id`+span or `record_id`+optional span. It is NOT in `ELIGIBLE_KINDS`. Add a `build_reference_text_ref(...)` helper. Rewrite the tests so they exercise the real threat:
   - a lineage whose source is `reference_text` cannot back `amount`, `target_id` or `source_account_id`;
   - `build_derived_lineage([reference_text_ref])` is rejected for a critical field (laundering through derived);
   - `build_derived_lineage([user_turn_ref, reference_text_ref])` is rejected (taint survives mixing);
   - a derived lineage whose parents are all eligible is accepted;
   - keep the existing bare-`derived`-with-no-parents rejection as a separate case.
   Update the module docstrings to match.

3. **Golden fixtures must pin exact bytes.** The T-01 acceptance requires stable byte fixtures. In `tests/test_canonical_golden.py`, for the fixed bundle and fixed envelope, assert the exact `canonical_bytes(...)` value (as a bytes literal) and the exact `approval_challenge_hex(...)` and `bundle_hash(...)` hex strings. Compute them once with the current implementation, inspect that the bytes look right (sorted keys, compact, 2-dp money strings, ms Z timestamps, lowercase UUID), then commit them as constants. Keep the structural tests too.

Then run `uv run ruff format` on touched files, `uv run ruff check .` and `uv run pytest -q`; all must pass. Final message: what changed, the pinned challenge hex, and check results.

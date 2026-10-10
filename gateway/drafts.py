"""Gateway-owned draft store: freeze untrusted agent proposals, issue
signing envelopes, and verify WebAuthn approvals.

The draft store is the gateway's authoritative path from an untrusted
agent proposal to a frozen, signed and executable bundle.  All times come
from ``gateway.clock.utcnow`` so tests can inject a clock.

Key responsibilities:

- **Freeze**: validate ownership, resolve display metadata, rebuild
  lineage, resolve amounts, compute equity terms, and store an
  immutable canonical-serialized ``FrozenBundle``.
- **Authorize**: build a server-generated :class:`SigningEnvelope`,
  compute its challenge via the shared T-01 helper, and persist the
  envelope bytes and hash.
- **Approve**: verify the WebAuthn assertion against the stored
  envelope's recomputed challenge, then call the eligibility hooks
  (semantic validation, policy, commit).  Signature verification alone
  **never** marks a financial operation committed; ``gateway.execution``
  re-checks everything under the write lock before any effect.

This module is imported only by gateway code.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from gateway.audit import REVISION_BOUND_TABLES
from gateway.clock import utcnow
from gateway.evidence import EvidenceError, TurnIndex, build_leg_lineage
from shared.canonical import (
    approval_challenge,
    approval_challenge_hex,
    bundle_hash,
    canonical_bytes,
)
from shared.lineage import build_bank_record_ref
from shared.money import Money
from shared.schemas import (
    ENVELOPE_TTL_SECONDS,
    Action,
    AmountExpr,
    DisplayMetadata,
    EquityTerms,
    FrozenBundle,
    Leg,
    SigningEnvelope,
    resolve_amount_exprs,
)
from shared.timeutil import from_iso, to_iso

__all__ = [
    "DraftStore",
    "DraftError",
    "ApprovalError",
    "CommitUnavailableError",
]


class DraftError(Exception):
    """Raised when a draft cannot be frozen (ownership, validation)."""


class ApprovalError(Exception):
    """Raised when an approval cannot be processed. The message is a stable code.

    ``reason_codes`` carries stable policy/binding codes (e.g. for ``POLICY_BLOCK``).
    """

    def __init__(self, code: str, reason_codes: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.reason_codes = tuple(reason_codes)


class CommitUnavailableError(ApprovalError):
    """Execution did not run or rolled back; mapped to HTTP 503, no state change."""


class _CommittedMeanwhile(Exception):
    """Internal: the authorization was committed by a concurrent request."""


def _active_mandate_version(conn: sqlite3.Connection, user_id: str) -> int | None:
    from gateway.policy import active_mandate_version

    return active_mandate_version(conn, user_id)


# A newer conversation revision supersedes pending drafts and approvals bound
# to an older one (see gateway.audit.supersede_for_conversation).
for _table in ("frozen_drafts", "authorizations"):
    REVISION_BOUND_TABLES[_table] = {
        "status_column": "status",
        "revision_column": "conversation_revision",
        "conversation_id_column": "conversation_id",
        "superseded_value": "SUPERSEDED",
    }

# ── Helpers ───────────────────────────────────────────────────────────


def _random_nonce_hex() -> str:
    """Generate a 32-byte random nonce as 64 lowercase hex chars."""
    import secrets

    return secrets.token_hex(32)


def _make_authorization_id() -> str:
    return f"authz-{uuid.uuid4().hex[:16]}"


def _parse_amount_expr(raw_leg: dict[str, Any]) -> AmountExpr | Money:
    """Typed amount expression from an untrusted proposal leg (no evaluation)."""
    from shared.schemas import AmountLiteral, FractionOfLeg, RemainingBalance

    amount_expr = raw_leg.get("amount_expr")
    if amount_expr is None:
        return Money(raw_leg["amount"])
    kind = amount_expr.get("kind", "")
    if kind == "literal":
        return AmountLiteral(amount=Money(amount_expr["amount"]))
    if kind == "fraction_of_leg":
        return FractionOfLeg(
            leg_id=amount_expr["leg_id"],
            numerator=amount_expr["numerator"],
            denominator=amount_expr["denominator"],
        )
    if kind == "remaining_balance":
        return RemainingBalance(account_id=amount_expr["account_id"])
    raise DraftError("AMOUNT_EXPR_INVALID")


# ── DraftStore ────────────────────────────────────────────────────────


class DraftStore:
    """Read/write access to frozen drafts, authorizations, and approvals.

    Instantiated by gateway code with a DB path.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> str:
        """The gateway database this store uses (shared by the T-07 step-up/mandate routes)."""
        return self._db_path

    def _conn(self) -> sqlite3.Connection:
        from ledger.schema import connect

        return connect(self._db_path)

    # ── Freeze ───────────────────────────────────────────────────────

    def freeze_draft(
        self,
        *,
        user_id: str,
        conversation_id: str,
        conversation_revision: int,
        legs: list[dict[str, Any]],
    ) -> FrozenBundle:
        """Freeze an untrusted agent proposal into an immutable bundle.

        Validates:
        - Conversation exists and is owned by *user_id*.
        - Each leg's source_account_id is owned by the user.
        - Each leg's target_id exists and belongs to the user.
        - Amounts are resolved (AmountExpr resolution).
        - Equity terms are computed for buy_equity legs.
        - Display metadata is resolved from bank records.
        - Lineage is rebuilt from server-held turn records.

        Raises :class:`DraftError` on any validation failure.
        """
        from gateway.bank import BankStore
        from shared.schemas import MAX_LEGS

        if not legs:
            raise DraftError("bundle must have at least one leg")
        if len(legs) > MAX_LEGS:
            raise DraftError(f"bundle exceeds {MAX_LEGS} legs")

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # ── Validate conversation ownership ──
            conv_row = conn.execute(
                "SELECT conversation_id, owner_user_id, revision, conversation_hash "
                "FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if conv_row is None:
                raise DraftError(f"conversation not found: {conversation_id}")
            if conv_row["owner_user_id"] != user_id:
                raise DraftError("conversation not owned by user")

            conv_revision = int(conv_row["revision"])
            conv_hash = conv_row["conversation_hash"]
            if conversation_revision != conv_revision:
                raise DraftError("STALE_CONVERSATION_REVISION")

            turns = TurnIndex(conn, conversation_id)

            # ── Get account balances for amount resolution ──
            bank_store = BankStore(self._db_path)
            accounts = bank_store.get_accounts(user_id)
            balances: dict[str, Decimal] = {}
            acct_map: dict[str, dict[str, Any]] = {}
            for acct in accounts:
                balances[acct["account_id"]] = Decimal(acct["balance"])
                acct_map[acct["account_id"]] = acct

            # ── Resolve legs ──
            resolved_legs: list[Leg] = []
            draft_exprs: list[tuple[str, str, AmountExpr | Money]] = []

            for raw_leg in legs:
                leg_id = raw_leg.get("leg_id", "")
                if not leg_id:
                    raise DraftError("leg_id is required")
                source_acct = raw_leg.get("source_account_id", "")
                if not source_acct:
                    raise DraftError("source_account_id is required")

                # Validate source account ownership.
                if source_acct not in acct_map:
                    raise DraftError(f"source account {source_acct!r} not owned by user")

                # Determine amount expression (malformed input → AMOUNT_EXPR_INVALID).
                try:
                    expr = _parse_amount_expr(raw_leg)
                except DraftError:
                    raise
                except (KeyError, TypeError, ValueError):
                    raise DraftError("AMOUNT_EXPR_INVALID") from None
                draft_exprs.append((leg_id, source_acct, expr))

            # Resolve all amounts (forward/self references, negative remainders...).
            try:
                resolved_amounts = resolve_amount_exprs(draft_exprs, balances)
            except (TypeError, ValueError):
                raise DraftError("AMOUNT_EXPR_INVALID") from None

            # ── Build each Leg ──
            amount_parents: dict[str, list] = {}
            for i, raw_leg in enumerate(legs):
                leg_id = raw_leg["leg_id"]
                source_acct = raw_leg["source_account_id"]
                action = Action(raw_leg["action"])
                target_type = raw_leg["target_type"]
                target_id = raw_leg["target_id"]
                amount = resolved_amounts[i]

                # Validate target ownership.
                _validate_target(conn, user_id, action, target_type, target_id, bank_store)

                # Resolve display metadata.
                display_meta = _resolve_display_metadata(
                    conn, user_id, action, target_type, target_id, bank_store, acct_map
                )

                # Build equity terms if needed.
                equity_terms = None
                if action == Action.buy_equity:
                    equity_terms = _compute_equity_terms(conn, raw_leg, amount, bank_store)

                # Trusted lineage from verified evidence (see gateway/evidence.py).
                expr = raw_leg.get("amount_expr") or {}
                if expr.get("kind") == "fraction_of_leg":
                    parents = amount_parents.get(expr.get("leg_id", ""))
                    if parents is None:
                        raise DraftError("RELATIVE_REFERENCE_INVALID")
                elif expr.get("kind") == "remaining_balance":
                    parents = [build_bank_record_ref(expr["account_id"])]
                else:
                    parents = None
                try:
                    lineage = build_leg_lineage(
                        conn, turns, user_id, raw_leg, relative_parents=parents
                    )
                except (EvidenceError, ValueError) as exc:
                    raise DraftError(str(exc)) from None
                amount_parents[leg_id] = (
                    list(lineage.parent_refs) if parents is not None else [lineage.source_refs[0]]
                )

                leg = Leg(
                    leg_id=leg_id,
                    action=action,
                    source_account_id=source_acct,
                    target_type=target_type,
                    target_id=target_id,
                    display_metadata=display_meta,
                    # For equities the debit is the whole-share cost, not the budget.
                    amount=equity_terms.actual_debit if equity_terms else amount,
                    reference=raw_leg.get("reference"),
                    equity_terms=equity_terms,
                    lineage=lineage,
                )
                resolved_legs.append(leg)

            # ── Build FrozenBundle ──
            bundle_id = uuid.uuid4()
            now = utcnow()
            bundle = FrozenBundle(
                bundle_id=bundle_id,
                revision=1,
                owner_user_id=user_id,
                conversation_id=conversation_id,
                conversation_revision=conv_revision,
                conversation_hash=conv_hash,
                created_at=to_iso(now),
                legs=resolved_legs,
            )

            # ── Store immutable canonical bytes ──
            cb = canonical_bytes(bundle)
            bh = bundle_hash(bundle)

            conn.execute(
                """
                INSERT INTO frozen_drafts
                    (bundle_id, conversation_id, conversation_revision,
                     owner_user_id, canonical_bytes, bundle_hash,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'DRAFT_FROZEN', ?)
                """,
                (
                    str(bundle_id),
                    conversation_id,
                    conv_revision,
                    user_id,
                    cb,
                    bh,
                    to_iso(now),
                ),
            )

            # Audit.
            from gateway.audit import append_audit

            append_audit(
                conn,
                source="gateway",
                conversation_id=conversation_id,
                user_id=user_id,
                step="gateway.draft.freeze",
                payload={
                    "bundle_id": str(bundle_id),
                    "bundle_hash": bh,
                    "legs": len(resolved_legs),
                },
            )

            conn.commit()
            return bundle

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Get draft ────────────────────────────────────────────────────

    def get_draft(self, bundle_id: str, user_id: str) -> FrozenBundle | None:
        """Return a frozen bundle if owned by *user_id*, else None."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT bundle_id, canonical_bytes, owner_user_id "
                "FROM frozen_drafts WHERE bundle_id = ?",
                (bundle_id,),
            ).fetchone()
            if row is None or row["owner_user_id"] != user_id:
                return None
            # Deserialize canonical bytes back to FrozenBundle.
            import json

            data = json.loads(row["canonical_bytes"])
            return FrozenBundle.model_validate(data)
        finally:
            conn.close()

    # ── Create authorization ─────────────────────────────────────────

    def create_authorization(
        self,
        *,
        user_id: str,
        bundle_id: str,
        credential_id: str,
    ) -> tuple[SigningEnvelope, str]:
        """Issue the server-generated signing envelope for a frozen draft.

        Fails closed unless the draft is ``DRAFT_FROZEN`` at the conversation's
        current revision, the credential belongs to *user_id*, a stored semantic
        validation result passes (T-10) and the policy pre-check does not BLOCK
        (T-06). The operation is always ``execute_bundle`` on this path, and the
        operation ID is the bundle ID (the T-06 idempotency key).

        TTL is <=120 seconds and no later than the earliest equity quote expiry.
        Returns ``(envelope, authorization_id)``.
        """
        from gateway.approval.hooks import (
            _call_policy_precheck,
            _call_semantic_validation_passed,
        )
        from gateway.audit import append_audit
        from gateway.identity import get_identity_store

        cred = get_identity_store().get_credential(credential_id)
        if cred is None or cred["user_id"] != user_id:
            raise ApprovalError("CREDENTIAL_NOT_OWNED")

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            draft = conn.execute(
                "SELECT canonical_bytes, owner_user_id, status, conversation_id, "
                "conversation_revision FROM frozen_drafts WHERE bundle_id = ?",
                (bundle_id,),
            ).fetchone()
            if draft is None or draft["owner_user_id"] != user_id:
                raise ApprovalError("DRAFT_NOT_FOUND")
            if draft["status"] != "DRAFT_FROZEN":
                raise ApprovalError(f"DRAFT_NOT_ELIGIBLE: {draft['status']}")
            conv = conn.execute(
                "SELECT revision FROM conversations WHERE conversation_id = ?",
                (draft["conversation_id"],),
            ).fetchone()
            if conv is None or int(conv["revision"]) != int(draft["conversation_revision"]):
                raise ApprovalError("DRAFT_STALE_REVISION")

            bundle = FrozenBundle.model_validate(json.loads(draft["canonical_bytes"]))
            if not _call_semantic_validation_passed(conn, bundle):
                raise ApprovalError("SEMANTIC_VALIDATION_REQUIRED")

            # T-07: a cooled mandate change whose time has come takes effect first.
            from gateway.mandates import activate_due_changes

            activate_due_changes(conn, user_id, utcnow())
            mandate_version = _active_mandate_version(conn, user_id)
            if mandate_version is None:
                raise ApprovalError("NO_ACTIVE_MANDATE")

            now = utcnow()
            ttl = ENVELOPE_TTL_SECONDS
            for leg in bundle.legs:
                if leg.action == Action.buy_equity and leg.equity_terms:
                    quote_ttl = int(
                        (from_iso(leg.equity_terms.quote_expires_at) - now).total_seconds()
                    )
                    if quote_ttl <= 0:
                        raise ApprovalError("QUOTE_EXPIRED")
                    ttl = min(ttl, quote_ttl)

            envelope = SigningEnvelope(
                schema_version=1,
                operation="execute_bundle",
                user_id=user_id,
                credential_id=credential_id,
                operation_id=str(bundle.bundle_id),
                payload=bundle,
                conversation_id=bundle.conversation_id,
                conversation_revision=bundle.conversation_revision,
                conversation_hash=bundle.conversation_hash,
                active_mandate_version=mandate_version,
                nonce=_random_nonce_hex(),
                issued_at=to_iso(now),
                expires_at=to_iso(now + timedelta(seconds=ttl)),
            )

            policy = _call_policy_precheck(conn, envelope)
            if policy.decision not in ("PASS", "STEP_UP"):
                raise ApprovalError("POLICY_BLOCK", _codes(policy))

            env_bytes = canonical_bytes(envelope)
            env_hash = approval_challenge_hex(envelope)
            authz_id = _make_authorization_id()
            conn.execute(
                """
                INSERT INTO authorizations
                    (authorization_id, bundle_id, user_id, credential_id,
                     operation, envelope_bytes, envelope_hash,
                     active_mandate_version, conversation_id,
                     conversation_revision, status, issued_at, expires_at,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_APPROVAL', ?, ?, ?)
                """,
                (
                    authz_id,
                    str(bundle.bundle_id),
                    user_id,
                    credential_id,
                    envelope.operation,
                    env_bytes,
                    env_hash,
                    mandate_version,
                    bundle.conversation_id,
                    bundle.conversation_revision,
                    envelope.issued_at,
                    envelope.expires_at,
                    to_iso(now),
                ),
            )
            append_audit(
                conn,
                source="gateway",
                conversation_id=bundle.conversation_id,
                user_id=user_id,
                step="gateway.authorization.create",
                payload={
                    "authorization_id": authz_id,
                    "bundle_id": str(bundle.bundle_id),
                    "envelope_hash": env_hash,
                    "operation": envelope.operation,
                    "policy": policy.decision,
                    "reason_codes": list(_codes(policy)),
                },
            )
            conn.commit()
            return envelope, authz_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Get authorization ─────────────────────────────────────────────

    def get_authorization(self, authz_id: str, user_id: str) -> dict[str, Any] | None:
        """Return an authorization dict if owned by *user_id*."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT authorization_id, bundle_id, user_id, credential_id, "
                "operation, envelope_bytes, envelope_hash, "
                "active_mandate_version, conversation_id, "
                "conversation_revision, status, status_reason, issued_at, expires_at, "
                "created_at "
                "FROM authorizations WHERE authorization_id = ?",
                (authz_id,),
            ).fetchone()
            if row is None or row["user_id"] != user_id:
                return None
            return {
                "authorization_id": row["authorization_id"],
                "bundle_id": row["bundle_id"],
                "user_id": row["user_id"],
                "credential_id": row["credential_id"],
                "operation": row["operation"],
                "envelope_bytes": bytes(row["envelope_bytes"]),
                "envelope_hash": row["envelope_hash"],
                "active_mandate_version": int(row["active_mandate_version"]),
                "conversation_id": row["conversation_id"],
                "conversation_revision": row["conversation_revision"],
                "status": row["status"],
                "reason_codes": [c for c in (row["status_reason"] or "").split(",") if c],
                "issued_at": row["issued_at"],
                "expires_at": row["expires_at"],
                "created_at": row["created_at"],
            }
        finally:
            conn.close()

    # ── Approve authorization ─────────────────────────────────────────

    def approve_authorization(
        self,
        *,
        user_id: str,
        authz_id: str,
        expected_envelope_hash: str,
        assertion: dict[str, Any],
    ) -> dict[str, Any]:
        """Verify a WebAuthn assertion against the STORED envelope and hand off.

        Order: ownership and operation; expected hash; an already COMMITTED
        authorization returns its stored receipt (idempotent retry, even after
        expiry, no new effects); otherwise PENDING_APPROVAL and not expired;
        stored bytes round-trip; credential owned by the session user and named
        in the envelope; library verification of the assertion against the
        challenge recomputed with the shared helper (origin, RP ID, UV required,
        sign count). An invalid assertion is audited and leaves the state
        unchanged. A valid one is recorded once under a write lock (single use),
        after re-checking that the conversation revision and mandate version are
        still current (else SUPERSEDED). Then eligibility is pre-checked (BLOCK →
        BLOCKED, STEP_UP → AWAITING_STEP_UP) and ``commit_authorization`` runs the
        atomic recheck-and-commit (``gateway.execution.commit``). A signature
        alone never commits.
        """
        from webauthn import verify_authentication_response

        from gateway.approval.hooks import (
            _call_commit_authorization,
            _call_policy_precheck,
            _call_semantic_validation_passed,
        )
        from gateway.audit import append_audit
        from gateway.identity import get_identity_store
        from shared.settings import load_gateway_settings

        authz = self.get_authorization(authz_id, user_id)
        if authz is None:
            raise ApprovalError("AUTHORIZATION_NOT_FOUND")
        if authz["operation"] != "execute_bundle":
            raise ApprovalError("OPERATION_MISMATCH")
        if not hmac.compare_digest(str(expected_envelope_hash), authz["envelope_hash"]):
            raise ApprovalError("ENVELOPE_HASH_MISMATCH")
        if authz["status"] == "COMMITTED":
            # Idempotent retry (e.g. a lost response): the owner gets the stored
            # receipt, even after the approval window; nothing executes again.
            receipt = self.get_receipt(authz_id, user_id)
            if receipt is not None:
                return _receipt_response(authz_id, receipt)
        if authz["status"] == "AWAITING_STEP_UP":
            # T-07: a repeated approval while step-up is pending returns the same
            # pending status: no second code, no payment.
            if from_iso(authz["expires_at"]) <= utcnow():
                raise ApprovalError("AUTHORIZATION_EXPIRED")
            return _status_response(authz_id, "AWAITING_STEP_UP", tuple(authz["reason_codes"]))
        if authz["status"] != "PENDING_APPROVAL":
            raise ApprovalError(f"NOT_PENDING: {authz['status']}")
        if from_iso(authz["expires_at"]) <= utcnow():
            self._transition(
                authz_id, user_id, "PENDING_APPROVAL", "EXPIRED", ("AUTHORIZATION_EXPIRED",)
            )
            raise ApprovalError("AUTHORIZATION_EXPIRED")

        env_bytes = authz["envelope_bytes"]
        envelope = SigningEnvelope.model_validate(json.loads(env_bytes))
        if canonical_bytes(envelope) != env_bytes:
            raise ApprovalError("ENVELOPE_INTEGRITY")
        challenge = approval_challenge(envelope)

        cred = get_identity_store().get_credential(authz["credential_id"])
        if (
            cred is None
            or cred["user_id"] != user_id
            or envelope.credential_id != cred["credential_id"]
            or assertion.get("id") != cred["credential_id"]
        ):
            raise ApprovalError("CREDENTIAL_NOT_OWNED")

        settings = load_gateway_settings()
        try:
            verified = verify_authentication_response(
                credential=assertion,
                expected_challenge=challenge,
                expected_rp_id=settings.rp_id,
                expected_origin=settings.rp_origin,
                credential_public_key=cred["public_key"],
                credential_current_sign_count=cred["sign_count"],
                require_user_verification=True,
            )
        except Exception as exc:
            # Audited, state unchanged: a bad assertion must not cancel the approval.
            self._audit(
                user_id,
                authz["conversation_id"],
                "gateway.approval.signature_rejected",
                {"authorization_id": authz_id, "error": type(exc).__name__},
            )
            raise ApprovalError("SIGNATURE_INVALID") from None

        conn = self._conn()
        committed_meanwhile = False
        awaiting_meanwhile = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM authorizations WHERE authorization_id = ?", (authz_id,)
            ).fetchone()
            if row is not None and row["status"] == "COMMITTED":
                # A concurrent request committed while this signature was verified.
                committed_meanwhile = True
                raise _CommittedMeanwhile
            if row is not None and row["status"] == "AWAITING_STEP_UP":
                # A concurrent approval already reached step-up (T-07): same pending status.
                awaiting_meanwhile = True
                raise _CommittedMeanwhile
            if row is None or row["status"] != "PENDING_APPROVAL":
                raise ApprovalError(f"NOT_PENDING: {row['status'] if row else None}")
            if conn.execute(
                "SELECT 1 FROM approval_evidence WHERE authorization_id = ?", (authz_id,)
            ).fetchone():
                raise ApprovalError("ALREADY_APPROVED")

            conv = conn.execute(
                "SELECT revision FROM conversations WHERE conversation_id = ?",
                (envelope.conversation_id,),
            ).fetchone()
            current_mandate = _active_mandate_version(conn, user_id)
            if (
                conv is None
                or int(conv["revision"]) != envelope.conversation_revision
                or current_mandate != envelope.active_mandate_version
            ):
                conn.execute(
                    "UPDATE authorizations SET status = 'SUPERSEDED', "
                    "status_reason = 'CONVERSATION_OR_MANDATE_CHANGED' "
                    "WHERE authorization_id = ?",
                    (authz_id,),
                )
                append_audit(
                    conn,
                    source="gateway",
                    conversation_id=envelope.conversation_id,
                    user_id=user_id,
                    step="gateway.authorization.superseded",
                    payload={"authorization_id": authz_id, "reason": "revision_or_mandate"},
                )
                conn.commit()
                raise ApprovalError("AUTHORIZATION_SUPERSEDED")

            client_data_hash = hashlib.sha256(_extract_client_data_bytes(assertion)).hexdigest()
            auth_data_hash = hashlib.sha256(
                _extract_authenticator_data_bytes(assertion)
            ).hexdigest()
            conn.execute(
                """
                INSERT INTO approval_evidence
                    (authorization_id, credential_id, sign_count,
                     client_data_hash, authenticator_data_hash, verified_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    authz_id,
                    cred["credential_id"],
                    verified.new_sign_count,
                    client_data_hash,
                    auth_data_hash,
                    to_iso(utcnow()),
                ),
            )
            conn.execute(
                "UPDATE webauthn_credentials SET sign_count = ? WHERE credential_id = ?",
                (verified.new_sign_count, cred["credential_id"]),
            )
            append_audit(
                conn,
                source="gateway",
                conversation_id=envelope.conversation_id,
                user_id=user_id,
                step="gateway.approval.signature_verified",
                payload={
                    "authorization_id": authz_id,
                    "credential_id": cred["credential_id"],
                    "sign_count": verified.new_sign_count,
                    "user_verified": True,
                },
            )
            conn.commit()
        except _CommittedMeanwhile:
            conn.rollback()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        if committed_meanwhile:
            receipt = self.get_receipt(authz_id, user_id)
            if receipt is None:
                raise ApprovalError("NOT_PENDING: COMMITTED")
            return _receipt_response(authz_id, receipt)
        if awaiting_meanwhile:
            current = self.get_authorization(authz_id, user_id) or {"reason_codes": []}
            return _status_response(authz_id, "AWAITING_STEP_UP", tuple(current["reason_codes"]))

        # Defence in depth: eligibility is re-checked after the signature.  The
        # authoritative recheck runs again under the write lock in execution.commit.
        conn = self._conn()
        try:
            if not _call_semantic_validation_passed(conn, envelope.payload):
                decision, codes = "BLOCK", ("SEMANTIC_VALIDATION_REQUIRED",)
            else:
                policy = _call_policy_precheck(conn, envelope)
                decision, codes = policy.decision, _codes(policy)
        finally:
            conn.close()

        if decision not in ("PASS", "STEP_UP"):
            self._transition(authz_id, user_id, "PENDING_APPROVAL", "BLOCKED", codes)
            return _status_response(authz_id, "BLOCKED", codes)
        if decision == "STEP_UP":
            # T-07: the status change and its one simulated step-up challenge commit together.
            from gateway.stepup import enter_step_up

            enter_step_up(self._db_path, authz_id, user_id, codes)
            return _status_response(authz_id, "AWAITING_STEP_UP", codes)

        from gateway.execution import ExecutionFailed, ExecutionRefused

        conn = self._conn()
        try:
            receipt = _call_commit_authorization(conn, authz)
        except NotImplementedError:
            raise CommitUnavailableError("COMMIT_NOT_IMPLEMENTED") from None
        except ExecutionFailed:
            raise CommitUnavailableError("COMMIT_FAILED") from None
        except ExecutionRefused as exc:
            if exc.status in ("BLOCKED", "AWAITING_STEP_UP"):
                return _status_response(authz_id, exc.status, exc.reason_codes)
            if exc.code == "NOT_PENDING":
                raise ApprovalError(f"NOT_PENDING: {exc.status}") from None
            raise ApprovalError(exc.code, exc.reason_codes) from None
        finally:
            conn.close()
        return _receipt_response(authz_id, receipt)

    def get_receipt(self, authz_id: str, user_id: str) -> Any | None:
        """The stored receipt of a COMMITTED authorization owned by *user_id*, else None."""
        from gateway.execution import load_receipt

        conn = self._conn()
        try:
            return load_receipt(conn, authz_id, user_id)
        finally:
            conn.close()

    def _transition(
        self,
        authz_id: str,
        user_id: str,
        from_status: str,
        to_status: str,
        reason_codes: tuple[str, ...],
    ) -> bool:
        """Compare-and-set an authorization status under a write lock, with audit."""
        from gateway.audit import append_audit

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE authorizations SET status = ?, status_reason = ? "
                "WHERE authorization_id = ? AND user_id = ? AND status = ?",
                (to_status, ",".join(reason_codes) or None, authz_id, user_id, from_status),
            )
            if cur.rowcount:
                append_audit(
                    conn,
                    source="gateway",
                    user_id=user_id,
                    step=f"gateway.authorization.{to_status.lower()}",
                    payload={"authorization_id": authz_id, "reason_codes": list(reason_codes)},
                )
            conn.commit()
            return bool(cur.rowcount)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _audit(
        self, user_id: str, conversation_id: str | None, step: str, payload: dict[str, Any]
    ) -> None:
        from gateway.audit import append_audit

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            append_audit(
                conn,
                source="gateway",
                conversation_id=conversation_id,
                user_id=user_id,
                step=step,
                payload=payload,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def webauthn_request_options(self, authz: dict[str, Any]) -> dict[str, Any]:
        """WebAuthn request options whose challenge is exactly the stored envelope's."""
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers import base64url_to_bytes
        from webauthn.helpers.structs import (
            PublicKeyCredentialDescriptor,
            UserVerificationRequirement,
        )

        from shared.settings import load_gateway_settings

        envelope = SigningEnvelope.model_validate(json.loads(authz["envelope_bytes"]))
        options = generate_authentication_options(
            rp_id=load_gateway_settings().rp_id,
            challenge=approval_challenge(envelope),
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(authz["credential_id"]))
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
            timeout=ENVELOPE_TTL_SECONDS * 1000,
        )
        return json.loads(options_to_json(options))


# ── Module-level singleton ─────────────────────────────────────────────

_store: DraftStore | None = None


def get_draft_store() -> DraftStore:
    """Get the module-level DraftStore singleton."""
    global _store
    if _store is None:
        from shared.settings import load_gateway_settings

        _store = DraftStore(load_gateway_settings().db_path)
    return _store


def _set_draft_store(store: DraftStore | None) -> None:
    """Test override: inject a store with a temp DB path."""
    global _store
    _store = store


# ── Private helpers ────────────────────────────────────────────────────


def _codes(policy: Any) -> tuple[str, ...]:
    """Stable reason codes of a PolicyOutcome (falls back to its ``reason`` string)."""
    codes = tuple(getattr(policy, "reason_codes", ()) or ())
    if not codes and policy.reason:
        codes = tuple(c for c in str(policy.reason).split(",") if c)
    return codes


def _status_response(authz_id: str, status: str, codes: tuple[str, ...]) -> dict[str, Any]:
    return {
        "authorization_id": authz_id,
        "status": status,
        "reason": ",".join(codes),
        "reason_codes": list(codes),
    }


def _receipt_response(authz_id: str, receipt: Any) -> dict[str, Any]:
    return {
        "authorization_id": authz_id,
        "status": receipt.status,
        "receipt_id": receipt.receipt_id,
        "receipt": dict(receipt.details),
        "replayed": receipt.replayed,
    }


def _validate_target(
    conn: sqlite3.Connection,
    user_id: str,
    action: Action,
    target_type: str,
    target_id: str,
    bank_store: Any,
) -> None:
    """Validate that the target exists and belongs to the user."""
    from shared.schemas import TargetType

    tt = TargetType(target_type)

    if tt == TargetType.own_account:
        acct = bank_store.get_account(user_id, target_id)
        if acct is None:
            raise DraftError(f"target account {target_id!r} not owned by user")
    elif tt in (TargetType.saved_payee, TargetType.saved_paynow):
        payees = bank_store.search_payees(user_id, target_id)
        # Exact ID match check.
        found = any(p["payee_id"] == target_id for p in payees)
        if not found:
            # Also try direct lookup by payee_id.
            row = conn.execute(
                "SELECT payee_id FROM payees WHERE payee_id = ? AND owner_user_id = ?",
                (target_id, user_id),
            ).fetchone()
            if row is None:
                raise DraftError(f"target payee {target_id!r} not found")
    elif tt == TargetType.biller:
        row = conn.execute(
            "SELECT b.biller_id FROM saved_billers sb "
            "JOIN billers b ON b.biller_id = sb.biller_id "
            "WHERE sb.user_id = ? AND b.biller_id = ?",
            (user_id, target_id),
        ).fetchone()
        if row is None:
            raise DraftError(f"target biller {target_id!r} not saved by user")
    elif tt == TargetType.owned_card:
        card = bank_store.get_card(user_id, target_id)
        if card is None:
            raise DraftError(f"target card {target_id!r} not owned by user")
    elif tt == TargetType.sgx_symbol:
        # Target is a symbol like "D05"; validate it exists in mock prices.
        from gateway.bank import MOCK_PRICES

        if target_id.upper() not in MOCK_PRICES:
            raise DraftError(f"unknown equity symbol: {target_id!r}")
    else:
        raise DraftError(f"unknown target type: {target_type!r}")


def _resolve_display_metadata(
    conn: sqlite3.Connection,
    user_id: str,
    action: Action,
    target_type: str,
    target_id: str,
    bank_store: Any,
    acct_map: dict[str, dict[str, Any]],
) -> DisplayMetadata:
    """Resolve display metadata from bank records."""
    from shared.schemas import TargetType

    tt = TargetType(target_type)

    if tt == TargetType.own_account:
        acct = acct_map.get(target_id)
        if acct is None:
            acct = bank_store.get_account(user_id, target_id)
        name = f"{acct['type'].title()} {acct['masked_suffix']}" if acct else target_id
        return DisplayMetadata(
            display_name=name,
            masked_account_suffix=acct["masked_suffix"] if acct else None,
        )
    elif tt in (TargetType.saved_payee, TargetType.saved_paynow):
        row = conn.execute(
            "SELECT display_name, masked_suffix FROM payees "
            "WHERE payee_id = ? AND owner_user_id = ?",
            (target_id, user_id),
        ).fetchone()
        if row is None:
            return DisplayMetadata(display_name=target_id)
        return DisplayMetadata(
            display_name=row["display_name"],
            masked_account_suffix=row["masked_suffix"],
        )
    elif tt == TargetType.biller:
        row = conn.execute(
            "SELECT b.display_name FROM saved_billers sb "
            "JOIN billers b ON b.biller_id = sb.biller_id "
            "WHERE sb.user_id = ? AND b.biller_id = ?",
            (user_id, target_id),
        ).fetchone()
        if row is None:
            return DisplayMetadata(display_name=target_id)
        return DisplayMetadata(display_name=row["display_name"])
    elif tt == TargetType.owned_card:
        card = bank_store.get_card(user_id, target_id)
        if card is None:
            return DisplayMetadata(display_name=target_id)
        return DisplayMetadata(
            display_name=f"Card {card['masked_suffix']}",
            masked_account_suffix=card["masked_suffix"],
        )
    elif tt == TargetType.sgx_symbol:
        return DisplayMetadata(display_name=f"SGX: {target_id.upper()}")
    return DisplayMetadata(display_name=target_id)


def _compute_equity_terms(
    conn: sqlite3.Connection,
    raw_leg: dict[str, Any],
    amount: Money,
    bank_store: Any,
) -> EquityTerms:
    """Compute equity terms for a buy_equity leg."""
    eq = raw_leg.get("equity_terms", {})
    quote_id = eq.get("quote_id", "")
    if not quote_id:
        raise DraftError("equity_terms.quote_id is required for buy_equity")

    quote = bank_store.fetch_quote(quote_id)
    if quote is None:
        raise DraftError(f"quote {quote_id!r} not found")
    if quote["symbol"].upper() != str(raw_leg["target_id"]).upper():
        raise DraftError("QUOTE_SYMBOL_MISMATCH")
    if from_iso(quote["expires_at"]) <= utcnow():
        raise DraftError("QUOTE_EXPIRED")

    unit_price = Money(quote["unit_price"])
    budget = amount  # The leg amount IS the budget.
    quantity = int(budget.value // unit_price.value)
    if quantity < 1:
        raise DraftError("quantity < 1 — budget insufficient for one share")
    actual_debit = Money(unit_price.value * quantity)
    residual = Money(budget.value - actual_debit.value)

    return EquityTerms(
        budget=budget,
        quote_id=quote_id,
        unit_price=unit_price,
        quoted_at=quote["quoted_at"],
        quote_expires_at=quote["expires_at"],
        quantity=quantity,
        actual_debit=actual_debit,
        residual_cash=residual,
    )


def _extract_client_data_bytes(assertion: dict[str, Any]) -> bytes:
    """Extract raw clientDataJSON bytes from an assertion dict."""
    import base64

    cd_b64 = assertion.get("response", {}).get("clientDataJSON", "")
    if not cd_b64:
        return b""
    padded = cd_b64 + "=" * (4 - len(cd_b64) % 4)
    return base64.urlsafe_b64decode(padded)


def _extract_authenticator_data_bytes(assertion: dict[str, Any]) -> bytes:
    """Extract raw authenticatorData bytes from an assertion dict."""
    import base64

    ad_b64 = assertion.get("response", {}).get("authenticatorData", "")
    if not ad_b64:
        return b""
    padded = ad_b64 + "=" * (4 - len(ad_b64) % 4)
    return base64.urlsafe_b64decode(padded)

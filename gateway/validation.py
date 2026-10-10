"""Semantic validation coordinator and immutable result binding (T-10).

The gateway, not the agent or the browser, asks the internal validator whether a
frozen draft matches the conversation, and stores the answer bound to exactly what
was checked. No route or caller can supply a verdict: the only writer of a verdict is
:func:`validate_draft`, and the verdict comes from the validator's response.

Flow of :func:`validate_draft` (owner only):

1. Under ``BEGIN IMMEDIATE``: load the draft and its conversation, supersede results
   bound to an older revision, refuse drafts that are not ``DRAFT_FROZEN`` at the
   current revision, enforce the attempt bound and rate limit, then build the request
   from gateway-held data only: the stored canonical bundle bytes, the conversation
   turns covered by the stored conversation hash (recomputed and compared; turn
   ``created_at`` dropped) and gateway-resolved bank labels. A ``PENDING`` row and a
   ``gateway.validation.requested`` audit event are written. The write lock is
   released before the HTTP call.
2. :class:`~shared.clients.validator.ValidatorClient` calls the validator (it never
   raises; failures are ``ERROR`` results). The returned ``input_hash`` must equal the
   hash of the request sent.
3. Under ``BEGIN IMMEDIATE``: the result is stored. It becomes ``CURRENT`` only if the
   conversation, the draft and the bundle hash are unchanged; otherwise it is stored
   ``SUPERSEDED``. A ``MISMATCH`` freezes the draft (``VALIDATION_MISMATCH``: it can
   never be issued; a correction creates a new draft). Every outcome is audited
   (``gateway.validation.<match|mismatch|uncertain|error|stale>``) with the trace ID.

Outcomes: ``MATCH`` makes the draft eligible for envelope issuance (confirmation only,
never authorization). ``MISMATCH`` and ``UNCERTAIN`` are final for that draft version
(field issues are returned for the UI; UNCERTAIN means "ask a clarification").
``ERROR`` (timeout, provider failure, malformed or unbound output) is never a MATCH; the
same draft may be retried up to :data:`MAX_ATTEMPTS_PER_DRAFT` times, rate limited.

:func:`semantic_validation_passed` is the default eligibility hook used by envelope
issuance, the post-signature recheck and the commit transaction.

This module is imported only by gateway code.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError as PydanticValidationError

from gateway.audit import REVISION_BOUND_TABLES, append_audit
from gateway.clock import utcnow
from gateway.conversations import compute_conversation_hash
from ledger.schema import cents_to_money, connect
from shared.canonical import bundle_hash, canonical_bytes
from shared.clients.validator import (
    MAX_ACCOUNTS,
    MAX_DISPLAY_CHARS,
    MAX_TARGETS,
    USER_ROLES,
    AccountLabel,
    BankMetadata,
    ConversationSnapshot,
    ConversationTurn,
    TargetLabel,
    ValidationRequest,
    ValidationResult,
    ValidatorClient,
    validation_input_hash,
)
from shared.schemas import FrozenBundle, TargetType
from shared.settings import load_gateway_settings
from shared.telemetry import Telemetry, httpx_request_hook
from shared.timeutil import from_iso, to_iso

__all__ = [
    "MAX_ATTEMPTS_PER_DRAFT",
    "MIN_RETRY_INTERVAL",
    "USER_ATTEMPTS_PER_MINUTE",
    "MISMATCH_DRAFT_STATUS",
    "SGX_COMPANY_NAMES",
    "ValidationRefused",
    "build_bank_metadata",
    "bound_conversation",
    "semantic_validation_passed",
    "validate_draft",
    "latest_result",
    "get_validator_client",
    "get_validation_telemetry",
]

# ── Policy constants (prototype values) ─────────────────────────────

#: Validation attempts (any outcome) allowed for one draft version.
MAX_ATTEMPTS_PER_DRAFT = 3
#: Minimum time between two attempts on the same draft.
MIN_RETRY_INTERVAL = timedelta(seconds=2)
#: Attempts one user may start per rolling minute, across drafts.
USER_ATTEMPTS_PER_MINUTE = 10
#: An in-flight attempt older than this is treated as interrupted. It exceeds the
#: largest configurable client timeout (VALIDATOR_CLIENT_TIMEOUT_SECONDS <= 180).
IN_FLIGHT_STALE_AFTER = timedelta(seconds=200)

#: Draft status written when the validator reports MISMATCH (terminal; never issued).
MISMATCH_DRAFT_STATUS = "VALIDATION_MISMATCH"

#: Display names of the mock SGX symbols shown to the validator.
SGX_COMPANY_NAMES: dict[str, str] = {
    "D05": "DBS Group Holdings",
    "O39": "OCBC",
    "U11": "UOB",
    "Z74": "Singtel",
}

#: ERROR codes that a retry of the same draft cannot fix (the user must restate/redraft).
_NON_RETRYABLE = frozenset(
    {
        "CONTEXT_LIMIT_RESTATE",
        "CONVERSATION_INVALID",
        "BINDING_MISMATCH",
        "BANK_METADATA_INCOMPLETE",
        "INVALID_REQUEST",
    }
)

_PENDING, _CURRENT, _SUPERSEDED = "PENDING", "CURRENT", "SUPERSEDED"
_ROUTE = "/api/drafts/{bundle_id}/validate"

# A newer conversation revision supersedes validation results bound to an older one
# (gateway.audit.supersede_for_conversation). In-flight rows are 'PENDING', which that
# function already covers; CURRENT rows are also superseded lazily here (see README).
REVISION_BOUND_TABLES["validation_results"] = {
    "revision_column": "conversation_revision",
    "status_column": "state",
    "conversation_id_column": "conversation_id",
    "superseded_value": _SUPERSEDED,
}


class ValidationRefused(Exception):
    """A validation request refused before or instead of a validator call.

    ``code`` is a stable machine code; ``http_status`` its HTTP status; ``extra`` is
    added to the JSON body. ``audit`` is False for not-found (nothing to attribute).
    """

    telemetry_status = "rejected"

    def __init__(
        self,
        code: str,
        http_status: int,
        *,
        audit: bool = True,
        conversation_id: str | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.reason_code = code
        self.http_status = http_status
        self.audit = audit
        self.conversation_id = conversation_id
        self.extra = extra


class _MetadataUnavailable(Exception):
    """A leg's target has no current bank record for this user."""


# ── Service wiring (module-level; tests replace them, HTTP cannot) ──

_client: ValidatorClient | None = None
_telemetry: Telemetry | None = None
_wiring_lock = threading.Lock()


def get_validator_client() -> ValidatorClient:
    """The process-wide validator client (base URL, token and timeout from settings).

    Outgoing requests carry the current span as ``traceparent`` (httpx request hook).
    """
    global _client
    with _wiring_lock:
        if _client is None:
            settings = load_gateway_settings()
            secret = settings.validator_service_token
            timeout = settings.validator_client_timeout_seconds
            http = httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                event_hooks={"request": [httpx_request_hook]},
            )
            _client = ValidatorClient(
                settings.validator_base_url,
                secret.get_secret_value() if secret is not None else None,
                timeout_seconds=timeout,
                http_client=http,
            )
        return _client


def get_validation_telemetry() -> Telemetry:
    """The gateway's Langfuse facade for validation spans (disabled without keys)."""
    global _telemetry
    with _wiring_lock:
        if _telemetry is None:
            _telemetry = Telemetry(load_gateway_settings(), "gateway")
        return _telemetry


def _set_validator_client(client: ValidatorClient | None) -> None:
    """Test hook: inject a validator client (fake transport or in-process app)."""
    global _client
    _client = client


def _set_validation_telemetry(telemetry: Telemetry | None) -> None:
    """Test hook: inject an in-memory or disabled telemetry facade."""
    global _telemetry
    _telemetry = telemetry


def _db_path() -> str:
    from gateway.drafts import get_draft_store

    return get_draft_store()._db_path


# ── Request construction from gateway-held data ─────────────────────


def _account_label(account_type: str, suffix: str) -> str:
    return f"{account_type.title()} ••{suffix}"


def _clip(text: str) -> str:
    return text if len(text) <= MAX_DISPLAY_CHARS else text[: MAX_DISPLAY_CHARS - 1] + "…"


def _payee_label(row: sqlite3.Row, aliases: Iterable[str]) -> str:
    name = row["display_name"]
    suffix = row["masked_suffix"]
    if row["kind"] == "paynow":
        where, role = (f"PayNow {suffix}" if suffix else "PayNow"), "saved PayNow recipient"
    else:
        where = " ".join(p for p in (row["bank"], f"••{suffix}" if suffix else None) if p)
        role = "saved payee"
    label = f"{name} ({where}, {role})" if where else f"{name} ({role})"
    seen = {name.casefold()}
    extra = []
    for alias in aliases:
        if alias.casefold() not in seen:
            seen.add(alias.casefold())
            extra.append(alias)
    if extra:
        label += f"; also called {', '.join(extra)}"
    return _clip(label)


def _payee_kind(target_type: TargetType) -> str:
    return "paynow" if target_type == TargetType.saved_paynow else "bank"


def _aliases(conn: sqlite3.Connection, user_id: str, payee_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT alias FROM payee_aliases WHERE payee_id = ? AND owner_user_id = ? ORDER BY alias",
        (payee_id, user_id),
    ).fetchall()
    return [r["alias"] for r in rows]


def _target_label(
    conn: sqlite3.Connection, user_id: str, target_type: TargetType, target_id: str
) -> str:
    """Display label for one leg target, from the user's own current bank records."""
    if target_type == TargetType.own_account:
        row = conn.execute(
            "SELECT a.type, a.masked_suffix FROM accounts a JOIN account_holders h "
            "ON h.account_id = a.account_id WHERE h.user_id = ? AND a.account_id = ?",
            (user_id, target_id),
        ).fetchone()
        if row is None:
            raise _MetadataUnavailable(target_id)
        return _clip(f"{_account_label(row['type'], row['masked_suffix'])} (own account)")
    if target_type in (TargetType.saved_payee, TargetType.saved_paynow):
        row = conn.execute(
            "SELECT display_name, bank, masked_suffix, kind FROM payees "
            "WHERE payee_id = ? AND owner_user_id = ? AND kind = ?",
            (target_id, user_id, _payee_kind(target_type)),
        ).fetchone()
        if row is None:
            raise _MetadataUnavailable(target_id)
        return _payee_label(row, _aliases(conn, user_id, target_id))
    if target_type == TargetType.biller:
        row = conn.execute(
            "SELECT b.display_name, b.category FROM saved_billers sb JOIN billers b "
            "ON b.biller_id = sb.biller_id WHERE sb.user_id = ? AND b.biller_id = ?",
            (user_id, target_id),
        ).fetchone()
        if row is None:
            raise _MetadataUnavailable(target_id)
        return _clip(f"{row['display_name']} (saved biller, {row['category']})")
    if target_type == TargetType.owned_card:
        row = conn.execute(
            "SELECT masked_suffix, outstanding_cents FROM cards "
            "WHERE card_id = ? AND owner_user_id = ?",
            (target_id, user_id),
        ).fetchone()
        if row is None:
            raise _MetadataUnavailable(target_id)
        outstanding = cents_to_money(int(row["outstanding_cents"])).to_str()
        return _clip(
            f"Credit card ••{row['masked_suffix']} (owned card, outstanding SGD {outstanding})"
        )
    if target_type == TargetType.sgx_symbol:
        company = SGX_COMPANY_NAMES.get(target_id.upper())
        if company is None:
            raise _MetadataUnavailable(target_id)
        return _clip(f"{company} (SGX: {target_id.upper()})")
    raise _MetadataUnavailable(target_id)


_TOKEN_RE = re.compile(r"[0-9a-z]+")


def _name_tokens(*names: str) -> set[str]:
    return {
        token
        for name in names
        for token in _TOKEN_RE.findall(name.casefold())
        if len(token) >= 2 and not token.isdigit()
    }


def _ambiguity_candidates(
    conn: sqlite3.Connection, user_id: str, target_type: TargetType, target_id: str
) -> list[tuple[str, str]]:
    """Other records of the same kind that share a name token with the leg's target
    (e.g. both saved Johns), so the validator can see an ambiguity. Sorted by ID."""
    out: list[tuple[str, str]] = []
    if target_type in (TargetType.saved_payee, TargetType.saved_paynow):
        rows = conn.execute(
            "SELECT payee_id, display_name, bank, masked_suffix, kind FROM payees "
            "WHERE owner_user_id = ? AND kind = ? ORDER BY payee_id",
            (user_id, _payee_kind(target_type)),
        ).fetchall()
        names = {r["payee_id"]: (r, _aliases(conn, user_id, r["payee_id"])) for r in rows}
        if target_id not in names:
            return out
        target_row, target_aliases = names[target_id]
        wanted = _name_tokens(target_row["display_name"], *target_aliases)
        for payee_id, (row, aliases) in names.items():
            if payee_id != target_id and wanted & _name_tokens(row["display_name"], *aliases):
                out.append((payee_id, _payee_label(row, aliases)))
    elif target_type == TargetType.biller:
        rows = conn.execute(
            "SELECT b.biller_id, b.display_name, b.category FROM saved_billers sb "
            "JOIN billers b ON b.biller_id = sb.biller_id WHERE sb.user_id = ? "
            "ORDER BY b.biller_id",
            (user_id,),
        ).fetchall()
        target = next((r for r in rows if r["biller_id"] == target_id), None)
        if target is None:
            return out
        wanted = _name_tokens(target["display_name"])
        for r in rows:
            if r["biller_id"] != target_id and wanted & _name_tokens(r["display_name"]):
                out.append(
                    (r["biller_id"], _clip(f"{r['display_name']} (saved biller, {r['category']})"))
                )
    elif target_type == TargetType.owned_card:
        rows = conn.execute(
            "SELECT card_id FROM cards WHERE owner_user_id = ? AND card_id != ? ORDER BY card_id",
            (user_id, target_id),
        ).fetchall()
        for r in rows:
            out.append((r["card_id"], _target_label(conn, user_id, target_type, r["card_id"])))
    return out


def build_bank_metadata(
    conn: sqlite3.Connection, user_id: str, bundle: FrozenBundle
) -> BankMetadata:
    """Gateway-resolved labels: every operable account of the user (leg sources first),
    every leg target, then same-kind candidates sharing a name token with a target.

    Deterministic for a given database state, so the eligibility check can rebuild it
    and detect changed bank terms. Raises :class:`_MetadataUnavailable` when a leg's
    target is no longer one of the user's records.
    """
    rows = conn.execute(
        "SELECT a.account_id, a.type, a.masked_suffix FROM accounts a JOIN account_holders h "
        "ON h.account_id = a.account_id WHERE h.user_id = ? ORDER BY a.account_id",
        (user_id,),
    ).fetchall()
    labels = {r["account_id"]: _account_label(r["type"], r["masked_suffix"]) for r in rows}
    order = list(dict.fromkeys([leg.source_account_id for leg in bundle.legs] + list(labels)))
    accounts = [
        AccountLabel(account_id=account_id, display=labels[account_id])
        for account_id in order
        if account_id in labels
    ][:MAX_ACCOUNTS]

    targets: dict[str, str] = {}
    for leg in bundle.legs:
        if leg.target_id not in targets:
            targets[leg.target_id] = _target_label(conn, user_id, leg.target_type, leg.target_id)
    for leg in bundle.legs:
        for candidate_id, label in _ambiguity_candidates(
            conn, user_id, leg.target_type, leg.target_id
        ):
            if candidate_id not in targets and len(targets) < MAX_TARGETS:
                targets[candidate_id] = label
    return BankMetadata(
        accounts=accounts,
        targets=[TargetLabel(target_id=tid, display=label) for tid, label in targets.items()],
    )


def _metadata_hash(metadata: BankMetadata) -> str:
    return hashlib.sha256(canonical_bytes(metadata)).hexdigest()


def bound_conversation(
    conn: sqlite3.Connection, conversation_id: str, revision: int, conversation_hash: str
) -> ConversationSnapshot:
    """The turns covered by the stored conversation hash, in order, without timestamps.

    A revision's hash covers every turn up to and including the user turn that created
    it (app questions included). Later app questions are not part of that revision and
    are left out. The hash is recomputed and must equal the stored one.
    """
    rows = conn.execute(
        "SELECT turn_id, seq, role, text, edits_turn_id, answers_question_id, created_at "
        "FROM conversation_turns WHERE conversation_id = ? ORDER BY seq",
        (conversation_id,),
    ).fetchall()
    turns = [
        {
            "turn_id": r["turn_id"],
            "seq": int(r["seq"]),
            "role": r["role"],
            "text": r["text"],
            "edits_turn_id": r["edits_turn_id"],
            "answers_question_id": r["answers_question_id"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    last_user = max((i for i, t in enumerate(turns) if t["role"] in USER_ROLES), default=-1)
    covered = turns[: last_user + 1]
    if (
        not covered
        or compute_conversation_hash(conversation_id, revision, covered) != conversation_hash
    ):
        raise ValidationRefused("CONVERSATION_INTEGRITY", 409, conversation_id=conversation_id)
    return ConversationSnapshot(
        conversation_id=conversation_id,
        revision=revision,
        conversation_hash=conversation_hash,
        turns=[
            ConversationTurn(**{k: v for k, v in t.items() if k != "created_at"}) for t in covered
        ],
    )


def _load_bundle(draft: sqlite3.Row) -> FrozenBundle:
    """The stored bundle, proven intact against the stored hash and canonical bytes."""
    raw = bytes(draft["canonical_bytes"])
    integrity = ValidationRefused("BUNDLE_INTEGRITY", 409, conversation_id=draft["conversation_id"])
    if hashlib.sha256(raw).hexdigest() != draft["bundle_hash"]:
        raise integrity
    try:
        bundle = FrozenBundle.model_validate(json.loads(raw))
    except Exception:  # noqa: BLE001 - unreadable stored bytes: fail closed
        raise integrity from None
    if (
        canonical_bytes(bundle) != raw
        or str(bundle.bundle_id) != draft["bundle_id"]
        or bundle.owner_user_id != draft["owner_user_id"]
        or bundle.conversation_id != draft["conversation_id"]
        or bundle.conversation_revision != int(draft["conversation_revision"])
    ):
        raise integrity
    return bundle


# ── Eligibility hook ────────────────────────────────────────────────


def semantic_validation_passed(conn: sqlite3.Connection, bundle: Any) -> bool:
    """True only for a CURRENT, OK, MATCH result bound to exactly this bundle.

    Required, all at once: the result's bundle ID and bundle hash equal the bundle's
    (and the stored draft's, which must still be ``DRAFT_FROZEN``); the result, bundle,
    draft and conversation have the same owner; the result's conversation revision and
    hash equal the bundle's, and the conversation is still at that revision and hash;
    and the bank labels rebuilt now hash to the ones the validator saw. Anything else,
    including any error, is False.

    Read-only and runs inside the caller's transaction (it opens none).
    """
    try:
        return _passes(conn, bundle)
    except Exception:  # noqa: BLE001 - any doubt means "not validated" (fail closed)
        return False


def _passes(conn: sqlite3.Connection, bundle: Any) -> bool:
    if not isinstance(bundle, FrozenBundle):
        return False
    bundle_id = str(bundle.bundle_id)
    rows = conn.execute(
        "SELECT owner_user_id, bundle_hash, conversation_id, conversation_revision, "
        "conversation_hash, bank_metadata_hash, status, verdict FROM validation_results "
        "WHERE bundle_id = ? AND state = 'CURRENT'",
        (bundle_id,),
    ).fetchall()
    if len(rows) != 1 or rows[0]["status"] != "OK" or rows[0]["verdict"] != "MATCH":
        return False
    result = rows[0]
    draft = conn.execute(
        "SELECT owner_user_id, bundle_hash, status, conversation_id, conversation_revision "
        "FROM frozen_drafts WHERE bundle_id = ?",
        (bundle_id,),
    ).fetchone()
    conv = conn.execute(
        "SELECT owner_user_id, revision, conversation_hash FROM conversations "
        "WHERE conversation_id = ?",
        (bundle.conversation_id,),
    ).fetchone()
    if draft is None or conv is None:
        return False
    owner = bundle.owner_user_id
    expected_hash = bundle_hash(bundle)
    revision = bundle.conversation_revision
    bound = (
        result["owner_user_id"] == owner == draft["owner_user_id"] == conv["owner_user_id"]
        and result["bundle_hash"] == expected_hash == draft["bundle_hash"]
        and draft["status"] == "DRAFT_FROZEN"
        and result["conversation_id"] == bundle.conversation_id == draft["conversation_id"]
        and int(result["conversation_revision"]) == revision
        and int(draft["conversation_revision"]) == revision
        and int(conv["revision"]) == revision
        and result["conversation_hash"] == bundle.conversation_hash == conv["conversation_hash"]
    )
    if not bound:
        return False
    return _metadata_hash(build_bank_metadata(conn, owner, bundle)) == result["bank_metadata_hash"]


# ── Coordinator ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Prepared:
    result_id: str
    attempt: int
    bundle: FrozenBundle
    bundle_hash: str
    request: ValidationRequest
    input_hash: str


def _now_iso() -> tuple[datetime, str]:
    now = utcnow()
    return now, to_iso(now)


def _supersede_stale(
    conn: sqlite3.Connection,
    user_id: str,
    conversation_id: str,
    revision: int,
    trace: tuple[str | None, str | None],
) -> None:
    """Mark CURRENT/PENDING results bound to an older revision SUPERSEDED (audited)."""
    cur = conn.execute(
        "UPDATE validation_results SET state = 'SUPERSEDED' WHERE conversation_id = ? "
        "AND conversation_revision < ? AND state IN ('CURRENT', 'PENDING')",
        (conversation_id, revision),
    )
    if cur.rowcount:
        append_audit(
            conn,
            source="gateway",
            conversation_id=conversation_id,
            user_id=user_id,
            step="gateway.validation.superseded",
            payload={"new_revision": revision, "superseded_count": cur.rowcount},
            trace_id=trace[0],
            span_id=trace[1],
        )


def _draft_and_conversation(
    conn: sqlite3.Connection, user_id: str, bundle_id: str
) -> tuple[sqlite3.Row, sqlite3.Row]:
    """The user's own draft and its conversation; anything else is not found (404)."""
    draft = conn.execute(
        "SELECT bundle_id, owner_user_id, conversation_id, conversation_revision, "
        "canonical_bytes, bundle_hash, status FROM frozen_drafts WHERE bundle_id = ?",
        (bundle_id,),
    ).fetchone()
    if draft is None or draft["owner_user_id"] != user_id:
        raise ValidationRefused("DRAFT_NOT_FOUND", 404, audit=False)
    conv = conn.execute(
        "SELECT owner_user_id, revision, conversation_hash FROM conversations "
        "WHERE conversation_id = ?",
        (draft["conversation_id"],),
    ).fetchone()
    if conv is None or conv["owner_user_id"] != user_id:
        raise ValidationRefused("DRAFT_NOT_FOUND", 404, audit=False)
    return draft, conv


def _results(conn: sqlite3.Connection, bundle_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM validation_results WHERE bundle_id = ? ORDER BY attempt",
        (bundle_id,),
    ).fetchall()


def _prepare_locked(
    conn: sqlite3.Connection,
    user_id: str,
    bundle_id: str,
    trace: tuple[str | None, str | None],
) -> _Prepared | dict[str, Any]:
    """Checks and the PENDING row, inside the caller's write transaction.

    Returns the prepared request, or the stored result view when a CURRENT MATCH
    already exists for this draft (no new call).
    """
    now, now_iso = _now_iso()
    draft, conv = _draft_and_conversation(conn, user_id, bundle_id)
    conv_id = draft["conversation_id"]
    revision = int(conv["revision"])
    _supersede_stale(conn, user_id, conv_id, revision, trace)

    if draft["status"] != "DRAFT_FROZEN":
        raise ValidationRefused(
            "DRAFT_NOT_ELIGIBLE", 409, conversation_id=conv_id, draft_status=draft["status"]
        )
    if int(draft["conversation_revision"]) != revision:
        raise ValidationRefused("DRAFT_STALE_REVISION", 409, conversation_id=conv_id)
    bundle = _load_bundle(draft)
    if bundle.conversation_hash != conv["conversation_hash"]:
        raise ValidationRefused("CONVERSATION_INTEGRITY", 409, conversation_id=conv_id)

    rows = _results(conn, bundle_id)
    current = next((r for r in rows if r["state"] == _CURRENT), None)
    if current is not None and current["status"] == "OK":
        if current["verdict"] != "MATCH":  # MISMATCH/UNCERTAIN are not re-rolled
            raise ValidationRefused(
                "VALIDATION_FINAL",
                409,
                conversation_id=conv_id,
                result=_view(conn, current, user_id),
            )
        if semantic_validation_passed(conn, bundle):  # still bound: return it, no new call
            return _view(conn, current, user_id, replayed=True)
        # A MATCH whose bank terms changed since: a new (bounded) attempt is required.
    for row in rows:
        if row["state"] != _PENDING:
            continue
        if now - from_iso(row["created_at"]) < IN_FLIGHT_STALE_AFTER:
            raise ValidationRefused("VALIDATION_IN_PROGRESS", 409, conversation_id=conv_id)
        conn.execute(  # abandoned (e.g. the gateway restarted mid-call): an ERROR attempt
            "UPDATE validation_results SET status = 'ERROR', code = 'VALIDATION_INTERRUPTED', "
            "state = 'SUPERSEDED', completed_at = ? WHERE result_id = ? AND state = 'PENDING'",
            (now_iso, row["result_id"]),
        )

    if len(rows) >= MAX_ATTEMPTS_PER_DRAFT:
        raise ValidationRefused(
            "VALIDATION_ATTEMPTS_EXHAUSTED",
            429,
            conversation_id=conv_id,
            attempts_used=len(rows),
            max_attempts=MAX_ATTEMPTS_PER_DRAFT,
        )
    if rows:
        wait = MIN_RETRY_INTERVAL - (now - from_iso(rows[-1]["created_at"]))
        if wait > timedelta(0):
            raise ValidationRefused(
                "VALIDATION_RATE_LIMITED",
                429,
                conversation_id=conv_id,
                retry_after_seconds=max(1, math.ceil(wait.total_seconds())),
            )
    recent = conn.execute(
        "SELECT COUNT(*) AS n FROM validation_results WHERE owner_user_id = ? AND created_at > ?",
        (user_id, to_iso(now - timedelta(minutes=1))),
    ).fetchone()["n"]
    if recent >= USER_ATTEMPTS_PER_MINUTE:
        raise ValidationRefused(
            "VALIDATION_RATE_LIMITED", 429, conversation_id=conv_id, retry_after_seconds=60
        )

    result_id = f"val-{uuid.uuid4().hex[:16]}"
    try:
        snapshot = bound_conversation(conn, conv_id, revision, conv["conversation_hash"])
        metadata = build_bank_metadata(conn, user_id, bundle)
        request = ValidationRequest(
            request_id=result_id, conversation=snapshot, bundle=bundle, bank_metadata=metadata
        )
    except _MetadataUnavailable:
        raise ValidationRefused("BANK_METADATA_UNAVAILABLE", 409, conversation_id=conv_id) from None
    except PydanticValidationError:
        raise ValidationRefused(
            "VALIDATION_REQUEST_INVALID", 409, conversation_id=conv_id
        ) from None
    input_hash = validation_input_hash(request)
    attempt = len(rows) + 1
    conn.execute(
        "INSERT INTO validation_results (result_id, bundle_id, owner_user_id, bundle_hash, "
        "conversation_id, conversation_revision, conversation_hash, bank_metadata_hash, "
        "input_hash, request_json, attempt, trace_id, span_id, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)",
        (
            result_id,
            bundle_id,
            user_id,
            draft["bundle_hash"],
            conv_id,
            revision,
            conv["conversation_hash"],
            _metadata_hash(metadata),
            input_hash,
            canonical_bytes(request).decode("utf-8"),
            attempt,
            trace[0],
            trace[1],
            now_iso,
        ),
    )
    append_audit(
        conn,
        source="gateway",
        conversation_id=conv_id,
        user_id=user_id,
        step="gateway.validation.requested",
        payload={
            "result_id": result_id,
            "bundle_id": bundle_id,
            "bundle_hash": draft["bundle_hash"],
            "conversation_revision": revision,
            "conversation_hash": conv["conversation_hash"],
            "input_hash": input_hash,
            "attempt": attempt,
            "turns": len(snapshot.turns),
            "targets": len(metadata.targets),
        },
        trace_id=trace[0],
        span_id=trace[1],
    )
    return _Prepared(result_id, attempt, bundle, draft["bundle_hash"], request, input_hash)


def _prepare(
    db_path: str, user_id: str, bundle_id: str, trace: tuple[str | None, str | None]
) -> _Prepared | dict[str, Any]:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            prepared = _prepare_locked(conn, user_id, bundle_id, trace)
        except ValidationRefused as refused:
            if not refused.audit:
                conn.rollback()
                raise
            append_audit(
                conn,
                source="gateway",
                conversation_id=refused.conversation_id,
                user_id=user_id,
                step="gateway.validation.refused",
                payload={"bundle_id": bundle_id, "code": refused.code},
                trace_id=trace[0],
                span_id=trace[1],
            )
            conn.commit()
            raise
        conn.commit()
        return prepared
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _call_validator(client: ValidatorClient, request: ValidationRequest, input_hash: str):
    """One validator call. Never raises; never turns a failure into a MATCH."""
    try:
        result = client.validate(request)
    except Exception:  # noqa: BLE001 - the client should not raise; fail closed if it does
        return ValidationResult(status="ERROR", code="VALIDATOR_UNAVAILABLE", input_hash=input_hash)
    if not isinstance(result, ValidationResult):
        return ValidationResult(
            status="ERROR", code="VALIDATOR_RESPONSE_INVALID", input_hash=input_hash
        )
    if result.input_hash != input_hash:
        return ValidationResult(status="ERROR", code="INPUT_HASH_MISMATCH", input_hash=input_hash)
    return result


def _finalize(
    db_path: str,
    user_id: str,
    prepared: _Prepared,
    result: ValidationResult,
    trace: tuple[str | None, str | None],
) -> dict[str, Any]:
    """Store the result; CURRENT only if nothing it is bound to changed meanwhile."""
    request = prepared.request
    conv_id = request.conversation.conversation_id
    bundle_id = str(prepared.bundle.bundle_id)
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM validation_results WHERE result_id = ?", (prepared.result_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("validation attempt row missing")
        if row["status"] is not None:  # already closed as interrupted: keep that record
            conn.commit()
            return _view(conn, row, user_id)
        conv = conn.execute(
            "SELECT revision, conversation_hash FROM conversations WHERE conversation_id = ?",
            (conv_id,),
        ).fetchone()
        draft = conn.execute(
            "SELECT status, bundle_hash FROM frozen_drafts WHERE bundle_id = ?", (bundle_id,)
        ).fetchone()
        still_bound = (
            row["state"] == _PENDING
            and conv is not None
            and int(conv["revision"]) == request.conversation.revision
            and conv["conversation_hash"] == request.conversation.conversation_hash
            and draft is not None
            and draft["status"] == "DRAFT_FROZEN"
            and draft["bundle_hash"] == prepared.bundle_hash
        )
        state = _CURRENT if still_bound else _SUPERSEDED
        if state == _CURRENT:
            conn.execute(
                "UPDATE validation_results SET state = 'SUPERSEDED' "
                "WHERE bundle_id = ? AND state = 'CURRENT'",
                (bundle_id,),
            )
        _, now_iso = _now_iso()
        issues = [i.model_dump(mode="json") for i in result.issues or []]
        conn.execute(
            "UPDATE validation_results SET status = ?, verdict = ?, issues_json = ?, code = ?, "
            "model = ?, prompt_hash = ?, latency_ms = ?, usage_json = ?, state = ?, "
            "completed_at = ? WHERE result_id = ?",
            (
                result.status,
                result.verdict,
                canonical_bytes(issues).decode("utf-8") if result.status == "OK" else None,
                result.code,
                result.model,
                result.prompt_hash,
                result.latency_ms,
                canonical_bytes(result.usage).decode("utf-8") if result.usage else None,
                state,
                now_iso,
                prepared.result_id,
            ),
        )
        draft_status = draft["status"] if draft is not None else None
        if state == _CURRENT and result.status == "OK" and result.verdict == "MISMATCH":
            conn.execute(
                "UPDATE frozen_drafts SET status = ? "
                "WHERE bundle_id = ? AND status = 'DRAFT_FROZEN'",
                (MISMATCH_DRAFT_STATUS, bundle_id),
            )
            draft_status = MISMATCH_DRAFT_STATUS
        outcome = (result.verdict or "ERROR").lower() if state == _CURRENT else "stale"
        append_audit(
            conn,
            source="gateway",
            conversation_id=conv_id,
            user_id=user_id,
            step=f"gateway.validation.{outcome}",
            payload={
                "result_id": prepared.result_id,
                "bundle_id": bundle_id,
                "bundle_hash": prepared.bundle_hash,
                "conversation_revision": request.conversation.revision,
                "conversation_hash": request.conversation.conversation_hash,
                "input_hash": prepared.input_hash,
                "prompt_hash": result.prompt_hash,
                "model": result.model,
                "status": result.status,
                "verdict": result.verdict,
                "code": result.code,
                "issue_count": len(issues),
                "issue_fields": [f"{i['leg_id'] or '-'}:{i['field']}" for i in issues],
                "latency_ms": result.latency_ms,
                "attempt": prepared.attempt,
                "state": state,
                "draft_status": draft_status,
            },
            trace_id=trace[0],
            span_id=trace[1],
        )
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM validation_results WHERE result_id = ?", (prepared.result_id,)
        ).fetchone()
        return _view(conn, stored, user_id)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _span_outcome(view: dict[str, Any]) -> tuple[str, str | None]:
    if view.get("state") == _SUPERSEDED:
        return "superseded", "CONVERSATION_CHANGED"
    if view.get("status") == "ERROR":
        code = view.get("code") or "VALIDATION_ERROR"
        return ("timeout" if "TIMEOUT" in code else "error"), code
    verdict = view.get("verdict")
    if verdict == "MATCH":
        return "ok", None
    if verdict == "UNCERTAIN":
        return "clarification", "VALIDATOR_UNCERTAIN"
    return "rejected", "VALIDATOR_MISMATCH"


def _owned_conversation(db_path: str, user_id: str, bundle_id: str) -> str:
    conn = connect(db_path)
    try:
        draft, _ = _draft_and_conversation(conn, user_id, bundle_id)
        return draft["conversation_id"]
    finally:
        conn.close()


def validate_draft(
    user_id: str,
    bundle_id: str,
    *,
    client: ValidatorClient | None = None,
    telemetry: Telemetry | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Validate the user's frozen draft and return the stored result view.

    Raises :class:`ValidationRefused` (stable code) when no call is made. Telemetry
    never changes the outcome; its trace ID is stored with the result and audit rows.
    """
    db_path = db_path or _db_path()
    conversation_id = _owned_conversation(db_path, user_id, bundle_id)  # 404 before tracing
    telemetry = telemetry or get_validation_telemetry()
    with telemetry.workflow_span(
        "semantic_validation",
        session_id=conversation_id,
        user_pseudonym=telemetry.pseudonymize(user_id),
        attributes={"bundle_id": bundle_id, "conversation_id": conversation_id, "route": _ROUTE},
    ) as span:
        trace = (span.trace_id, span.observation_id)
        try:
            prepared = _prepare(db_path, user_id, bundle_id, trace)
        except ValidationRefused as refused:
            span.set_status("rejected", reason_code=refused.code)
            raise
        if isinstance(prepared, dict):  # an existing CURRENT MATCH: nothing to call
            span.set_attributes({"validator_verdict": "MATCH", "outcome": "replayed"})
            return prepared
        span.set_attributes(
            {
                "request_id": prepared.result_id,
                "conversation_revision": prepared.request.conversation.revision,
                "bundle_hash": prepared.bundle_hash,
                "leg_count": len(prepared.bundle.legs),
                "attempt": prepared.attempt,
            }
        )
        result = _call_validator(
            client or get_validator_client(), prepared.request, prepared.input_hash
        )
        view = _finalize(db_path, user_id, prepared, result, trace)
        facts = {
            "validator_verdict": result.verdict or "ERROR",
            "model": result.model,
            "prompt_hash": result.prompt_hash,
            "latency_ms": result.latency_ms,
        }
        span.set_attributes({k: v for k, v in facts.items() if v is not None})
        status, reason = _span_outcome(view)
        span.set_status(status, reason_code=reason)
        return view


def latest_result(user_id: str, bundle_id: str, *, db_path: str | None = None) -> dict[str, Any]:
    """The latest validation attempt for the user's draft (read-only)."""
    conn = connect(db_path or _db_path())
    try:
        _draft_and_conversation(conn, user_id, bundle_id)
        rows = _results(conn, bundle_id)
        if not rows:
            raise ValidationRefused("VALIDATION_NOT_FOUND", 404, audit=False)
        return _view(conn, rows[-1], user_id)
    finally:
        conn.close()


# ── Views ───────────────────────────────────────────────────────────


def _view(
    conn: sqlite3.Connection, row: sqlite3.Row, user_id: str, *, replayed: bool = False
) -> dict[str, Any]:
    """What the UI/agent sees. ``state`` is the effective state: a CURRENT or PENDING row
    bound to an older conversation revision/hash reads as SUPERSEDED."""
    conv = conn.execute(
        "SELECT revision, conversation_hash FROM conversations WHERE conversation_id = ?",
        (row["conversation_id"],),
    ).fetchone()
    draft = conn.execute(
        "SELECT canonical_bytes, bundle_hash, status, owner_user_id, conversation_id, "
        "conversation_revision, bundle_id FROM frozen_drafts WHERE bundle_id = ?",
        (row["bundle_id"],),
    ).fetchone()
    state = row["state"]
    if state in (_CURRENT, _PENDING) and (
        conv is None
        or int(conv["revision"]) != int(row["conversation_revision"])
        or conv["conversation_hash"] != row["conversation_hash"]
    ):
        state = _SUPERSEDED
    eligible = False
    if state == _CURRENT and row["status"] == "OK" and row["verdict"] == "MATCH":
        try:
            eligible = semantic_validation_passed(conn, _load_bundle(draft))
        except Exception:  # noqa: BLE001 - unreadable draft: not eligible
            eligible = False
    attempts = conn.execute(
        "SELECT COUNT(*) AS n FROM validation_results WHERE bundle_id = ?", (row["bundle_id"],)
    ).fetchone()["n"]
    remaining = max(0, MAX_ATTEMPTS_PER_DRAFT - attempts)
    return {
        "result_id": row["result_id"],
        "bundle_id": row["bundle_id"],
        "state": state,
        "status": row["status"],
        "verdict": row["verdict"],
        "code": row["code"],
        "issues": json.loads(row["issues_json"]) if row["issues_json"] else [],
        "eligible_for_confirmation": eligible,
        "next_action": _next_action(row, state, eligible, remaining),
        "replayed": replayed,
        "attempt": int(row["attempt"]),
        "attempts_used": attempts,
        "attempts_remaining": remaining,
        "draft_status": draft["status"] if draft is not None else None,
        "conversation_id": row["conversation_id"],
        "conversation_revision": int(row["conversation_revision"]),
        "bundle_hash": row["bundle_hash"],
        "input_hash": row["input_hash"],
        "prompt_hash": row["prompt_hash"],
        "model": row["model"],
        "latency_ms": row["latency_ms"],
        "trace_id": row["trace_id"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def _next_action(row: sqlite3.Row, state: str, eligible: bool, remaining: int) -> str:
    """A stable hint for the UI/agent; it never grants anything."""
    if state == _PENDING:
        return "WAIT"
    if state == _SUPERSEDED:
        return "REDRAFT"
    if row["status"] == "ERROR":
        if row["code"] == "CONTEXT_LIMIT_RESTATE":
            return "RESTATE"
        return "RETRY" if remaining and row["code"] not in _NON_RETRYABLE else "REDRAFT"
    if row["verdict"] == "MATCH":
        return "CONFIRM" if eligible else "NONE"
    if row["verdict"] == "UNCERTAIN":
        return "CLARIFY"
    return "CORRECT_DRAFT"

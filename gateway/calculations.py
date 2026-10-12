"""Freeze minimal calculation inputs and refuse reuse after relevant balance changes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal

from shared.canonical import canonical_bytes
from shared.clients.validator import CalculationDebit, CalculationEvidence
from shared.money import Money
from shared.schemas import AmountExpr, FractionOfLeg, FrozenBundle, RemainingBalance


def freeze_calculations(
    conn: sqlite3.Connection,
    bundle: FrozenBundle,
    expressions: list[tuple[str, str, AmountExpr | Money]],
    amounts: list[Money],
    balances: dict[str, Decimal],
) -> None:
    evidence = []
    preceding: list[CalculationDebit] = []
    inputs: list[CalculationDebit] = []
    for (leg_id, source, expression), amount in zip(expressions, amounts, strict=True):
        if isinstance(expression, (FractionOfLeg, RemainingBalance)):
            debits = []
            dependencies = []
            balance = None
            if isinstance(expression, RemainingBalance):
                balance = Money(balances[expression.account_id])
                debits = [d for d in preceding if d.source_account_id == expression.account_id]
                dependencies = debits
            else:
                dependencies = [d for d in inputs if d.leg_id == expression.leg_id]
            evidence.append(
                CalculationEvidence(
                    leg_id=leg_id,
                    expression=expression,
                    balance_snapshot=balance,
                    preceding_debits=debits,
                    dependencies=dependencies,
                    result=amount,
                )
            )
        inputs.append(CalculationDebit(leg_id=leg_id, source_account_id=source, amount=amount))
        debit = bundle.legs[len(preceding)].amount
        preceding.append(CalculationDebit(leg_id=leg_id, source_account_id=source, amount=debit))
    raw = canonical_bytes(evidence)
    conn.execute(
        "INSERT INTO draft_calculations (bundle_id, evidence_json, evidence_hash) VALUES (?, ?, ?)",
        (str(bundle.bundle_id), raw.decode(), hashlib.sha256(raw).hexdigest()),
    )


def bound_calculations(conn: sqlite3.Connection, bundle: FrozenBundle) -> list[CalculationEvidence]:
    row = conn.execute(
        "SELECT evidence_json, evidence_hash FROM draft_calculations WHERE bundle_id = ?",
        (str(bundle.bundle_id),),
    ).fetchone()
    if row is None:
        # Pre-upgrade derived drafts have no frozen inputs; they must be redrafted.
        if any(leg.lineage and leg.lineage.parent_refs for leg in bundle.legs):
            raise ValueError("CALCULATION_EVIDENCE_MISSING")
        return []
    raw = row["evidence_json"].encode()
    if hashlib.sha256(raw).hexdigest() != row["evidence_hash"]:
        raise ValueError("CALCULATION_INTEGRITY")
    evidence = [CalculationEvidence.model_validate(e) for e in json.loads(raw)]
    for item in evidence:
        if isinstance(item.expression, RemainingBalance):
            balance = conn.execute(
                "SELECT a.balance_cents FROM accounts a JOIN account_holders h "
                "ON h.account_id = a.account_id WHERE a.account_id = ? AND h.user_id = ?",
                (item.expression.account_id, bundle.owner_user_id),
            ).fetchone()
            if balance is None or Money(Decimal(balance[0]) / 100) != item.balance_snapshot:
                raise ValueError("CALCULATION_BALANCE_CHANGED")
    return evidence

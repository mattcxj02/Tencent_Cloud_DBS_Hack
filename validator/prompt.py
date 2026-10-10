"""Fixed system prompt and model-message construction.

The system prompt is the version-controlled file ``prompts/validator.md``, read once at
import. Its SHA-256 (``PROMPT_HASH``) is reported with every result. There is no
parameter, header or request field that can change it.

Request data goes only into the user message, as one JSON object between fixed
markers. ``<`` and ``>`` inside the JSON are escaped as ``\\u003c``/``\\u003e`` so no
data string can contain a marker. The model sees the full ordered conversation, the
drafted legs and the bank labels. It does not see bundle lineage (span citations that
originate from the extractor's proposal), so its reading stays independent of the
extractor's.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from shared.clients.validator import ValidationRequest
from shared.schemas import Leg

__all__ = [
    "PROMPT_PATH",
    "SYSTEM_PROMPT",
    "PROMPT_HASH",
    "DATA_BEGIN",
    "DATA_END",
    "model_view",
    "serialize_data",
    "build_messages",
]

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "validator.md"
DATA_BEGIN = "<<<DCTA_VALIDATION_DATA_BEGIN>>>"
DATA_END = "<<<DCTA_VALIDATION_DATA_END>>>"


def _load_prompt() -> tuple[str, str]:
    raw = PROMPT_PATH.read_bytes()
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


SYSTEM_PROMPT, PROMPT_HASH = _load_prompt()


def _leg_view(position: int, leg: Leg) -> dict[str, Any]:
    view: dict[str, Any] = {
        "position": position,
        "leg_id": leg.leg_id,
        "action": leg.action.value,
        "source_account_id": leg.source_account_id,
        "target_type": leg.target_type.value,
        "target_id": leg.target_id,
        "target_display_name": leg.display_metadata.display_name,
        "target_masked_suffix": leg.display_metadata.masked_account_suffix,
        "amount": leg.amount.to_str(),
        "currency": leg.currency,
        "reference": leg.reference,
    }
    terms = leg.equity_terms
    if terms is not None:
        view["equity"] = {
            "symbol": leg.target_id,
            "budget": terms.budget.to_str(),
            "unit_price": terms.unit_price.to_str(),
            "quantity": terms.quantity,
            "actual_debit": terms.actual_debit.to_str(),
            "residual_cash": terms.residual_cash.to_str(),
        }
    return view


def model_view(request: ValidationRequest) -> dict[str, Any]:
    """The exact data the model sees (a deterministic projection of the request)."""
    turns = []
    for turn in request.conversation.turns:
        item: dict[str, Any] = {
            "turn_id": turn.turn_id,
            "seq": turn.seq,
            "role": turn.role,
            "text": turn.text,
            "length": len(turn.text),
        }
        if turn.edits_turn_id is not None:
            item["edits_turn_id"] = turn.edits_turn_id
        if turn.answers_question_id is not None:
            item["answers_question_id"] = turn.answers_question_id
        turns.append(item)
    return {
        "conversation": {"turns": turns},
        "drafted_legs": [_leg_view(i, leg) for i, leg in enumerate(request.bundle.legs, 1)],
        "bank_metadata": {
            "accounts": [
                {"account_id": a.account_id, "display": a.display}
                for a in request.bank_metadata.accounts
            ],
            "targets": [
                {"target_id": t.target_id, "display": t.display}
                for t in request.bank_metadata.targets
            ],
        },
    }


def serialize_data(view: dict[str, Any]) -> str:
    """Compact JSON with angle brackets escaped, so markers cannot occur inside it."""
    text = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def build_messages(request: ValidationRequest) -> list[dict[str, str]]:
    """System message = fixed prompt only; user message = markers around the data."""
    data = serialize_data(model_view(request))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{DATA_BEGIN}\n{data}\n{DATA_END}"},
    ]

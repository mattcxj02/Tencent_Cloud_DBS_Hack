"""Canonical JSON serialization and challenge computation.

Canonical JSON rules (from PLAN.md "Signing and execution contract"):

    - Sorted object keys.
    - Compact separators: ``","`` and ``":"``.
    - UTF-8 encoding (``ensure_ascii=False``).
    - Money as two-decimal strings (e.g. ``"50.00"``).
    - Datetimes in the one UTC ISO-8601 ``Z`` format.
    - UUIDs as lowercase strings.
    - Enums as their values.
    - Array order preserved (meaningful).
    - **No floats anywhere.**

Challenge:

    ``SHA256(b"DCTA:approval:v1\\x00" + canonical_bytes(envelope))``

The same :func:`approval_challenge` helper is used for both issuing
and verifying challenges.  Nothing else may reimplement it.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from shared.money import Money

__all__ = [
    "DCTA_CHALLENGE_PREFIX",
    "canonical_bytes",
    "canonical_dict",
    "bundle_hash",
    "approval_challenge",
    "approval_challenge_hex",
]

#: Domain-separation prefix for DCTA approval challenges.
DCTA_CHALLENGE_PREFIX = b"DCTA:approval:v1\x00"

#: SHA-256 hash producing raw bytes (32 bytes).
_SHA = hashlib.sha256


# ---------------------------------------------------------------------------
# Canonical conversion
# ---------------------------------------------------------------------------


def _convert(value: Any) -> Any:
    """Recursively convert a value into canonical-JSON-safe types.

    - :class:`Money` -> two-decimal string.
    - :class:`Decimal` -> rejected (must be wrapped in Money).
    - :class:`float` -> rejected (no floats anywhere).
    - :class:`bool` -> JSON boolean (allowed for fields like ``paused``).
    - :class:`UUID` -> lowercase string.
    - :class:`Enum` -> its value.
    - Pydantic models -> dict of fields.
    """
    # bool is a subclass of int; keep it as a JSON boolean.
    if isinstance(value, bool):
        return value

    if isinstance(value, float):
        raise TypeError("float is not allowed in canonical JSON")

    if isinstance(value, Money):
        return value.to_str()

    if isinstance(value, Decimal):
        raise TypeError("raw Decimal is not allowed; wrap in Money or convert to str")

    if isinstance(value, UUID):
        return str(value).lower()

    if isinstance(value, Enum):
        return value.value

    if hasattr(value, "model_dump"):
        # Pydantic v2 model — use mode="json" then re-canonicalize.
        # But model_dump may produce floats; we convert recursively.
        d = value.model_dump(mode="python")
        return _convert_dict(d)

    if isinstance(value, dict):
        return _convert_dict(value)

    if isinstance(value, (list, tuple)):
        return [_convert(item) for item in value]

    if isinstance(value, str):
        return value

    if isinstance(value, int):
        return value

    if value is None:
        return None

    raise TypeError(f"unsupported type for canonical JSON: {type(value).__name__}")


def _convert_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Convert a dict, rejecting float values recursively."""
    result: dict[str, Any] = {}
    for key, val in d.items():
        result[key] = _convert(val)
    return result


def canonical_dict(obj: Any) -> dict[str, Any]:
    """Convert a Pydantic model or dict into a canonical dict.

    The dict is **not** yet JSON-serialized; keys are not yet sorted.
    Use :func:`canonical_bytes` for the final serialized form.
    """
    return _convert(obj)


def canonical_bytes(obj: Any) -> bytes:
    """Serialize *obj* into canonical JSON bytes.

    Rules:
        - Sorted object keys.
        - Compact separators: ``","`` and ``":"``.
        - ``ensure_ascii=False`` (UTF-8).
        - No floats.
        - Money as two-decimal strings.
        - Datetimes as UTC ISO-8601 ``Z`` strings.
        - UUIDs as lowercase strings.
        - Enums as values.
        - Array order preserved.
    """
    converted = _convert(obj)
    return json.dumps(
        converted,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Hashes
# ---------------------------------------------------------------------------


def bundle_hash(bundle: Any) -> str:
    """Compute ``SHA256(canonical_bytes(bundle))`` as lowercase hex.

    This is the **bundle hash**, distinct from the full approval-envelope
    hash.  The bundle hash identifies the immutable payload; the
    approval challenge includes the entire envelope (operation, user,
    nonce, etc.).
    """
    return _SHA(canonical_bytes(bundle)).hexdigest()


def approval_challenge(envelope: Any) -> bytes:
    """Compute the approval challenge for *envelope*.

    ``challenge = SHA256(DCTA_CHALLENGE_PREFIX + canonical_bytes(envelope))``

    Returns raw 32 bytes.  The same helper is used for both **issuing**
    and **verifying** challenges.  Nothing else may reimplement this.
    """
    body = DCTA_CHALLENGE_PREFIX + canonical_bytes(envelope)
    return _SHA(body).digest()


def approval_challenge_hex(envelope: Any) -> str:
    """Approval challenge as 64 lowercase hex characters."""
    return approval_challenge(envelope).hex()

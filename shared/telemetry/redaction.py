"""Redaction and attribute allowlisting applied before anything reaches Langfuse.

Three entry points:

* :func:`redact_text` scrubs one string: credentials, auth headers/cookies, ``dcta_``
  delegation tokens, OTPs, PEM private keys, data URIs/base64 blobs, NRIC, e-mail and
  long digit runs (account/card/phone numbers, kept as ``[REDACTED number ending 4521]``).
* :func:`redact` walks a payload (dict/list/model) and applies key-based rules first,
  then :func:`redact_text` to every remaining string. Unknown objects are never
  ``repr``-ed; they become ``<TypeName>``.
* :func:`sanitize_attributes` keeps only keys in :data:`ALLOWED_ATTRIBUTES`, each value
  checked against its kind. Everything else is dropped and counted.

Pure functions: no I/O, no logging, no imports from the services.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any
from uuid import UUID

__all__ = [
    "ALLOWED_ATTRIBUTES",
    "RESERVED_ATTRIBUTES",
    "WORKFLOW_STAGES",
    "STATUS_LEVELS",
    "redact",
    "redact_text",
    "sanitize_attributes",
    "bounded_payload",
    "safe_name",
    "safe_code",
    "safe_reason",
    "is_hex_digest",
]

# ── Workflow vocabulary ──────────────────────────────────────────────

#: Named workflow stages (PLAN "Langfuse backend and audit"). Spans may use other safe
#: names (e.g. ``agent.turn``); a known stage is also recorded as ``stage`` metadata.
WORKFLOW_STAGES = frozenset(
    {
        "ingest",
        "extract",
        "normalize",
        "resolve",
        "clarify",
        "compile",
        "semantic_validation",
        "policy",
        "challenge",
        "approval",
        "step_up",
        "execution",
        "receipt",
    }
)

#: Outcome status → Langfuse level. Expected outcomes are never ERROR.
STATUS_LEVELS: Mapping[str, str] = MappingProxyType(
    {
        "ok": "DEFAULT",
        "pending": "DEFAULT",
        "clarification": "DEFAULT",
        "superseded": "DEFAULT",
        "blocked": "WARNING",
        "rejected": "WARNING",
        "timeout": "WARNING",
        "expired": "WARNING",
        "cancelled": "WARNING",
        "unknown": "WARNING",
        "error": "ERROR",
    }
)

# ── Text redaction ───────────────────────────────────────────────────

_PEM_RE = re.compile(
    r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END[A-Z0-9 ]*PRIVATE KEY-----|$)", re.S
)
_DATA_URI_RE = re.compile(r"data:[\w.+/-]+(?:;[\w=.-]+)*;base64,[A-Za-z0-9+/=_-]*")
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{6,}")
# Header-like keys whose whole value (quoted, or rest of line) is secret.
_HEADER_VALUE_RE = re.compile(
    r"(?i)(?<![\w-])((?:set-)?cookie|(?:proxy-)?authorization)"
    r"(\"?'?\s*[:=]\s*)(\[REDACTED[^\]]*\]|\"[^\"]*\"|'[^']*'|[^\r\n]*)"
)
# key=value / "key": "value" where the key names a secret.
_SECRET_KV_RE = re.compile(
    r"(?i)(?<![\w-])([\w-]*?(?:token|secret|password|passwd|passcode|api[_-]?key"
    r"|private[_-]?key|signature|assertion|client[_-]?data[_-]?json"
    r"|authenticator[_-]?data|user[_-]?handle|otp|nonce))"
    r"(\"?'?\s*[:=]\s*)(\[REDACTED[^\]]*\]|\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)"
)
_DCTA_TOKEN_RE = re.compile(r"dcta_[A-Za-z0-9._~+/=-]+")
_KNOWN_KEY_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|(?:sk|pk)-lf-[A-Za-z0-9-]{6,}|AKIA[0-9A-Z]{16}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]*)"
)
_OTP_PHRASE_RE = re.compile(
    r"(?i)\b(otp|one[- ]?time (?:pass)?code|verification code|security code|passcode"
    r"|step[- ]?up code|sms code|pin|code)\b(\s*(?:is|was|:|=|-)?\s*)(\d{4,8})\b"
)
_NRIC_RE = re.compile(r"\b[STFGMstfgm]\d{7}[A-Za-z]\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# 8–19 digits, optionally grouped by single spaces/dashes: account, card, phone numbers.
_DIGIT_RUN_RE = re.compile(r"(?<!\w)(?<!\d\.)\+?\d(?:[ -]?\d){7,18}(?!\w)(?!\.\d)")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:\D|$)")
_BLOB_RE = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")


def _keep_quotes(value: str, marker: str) -> str:
    if value[:1] in {'"', "'"}:
        return f"{value[0]}{marker}{value[0]}"
    return marker


def _digit_run(match: re.Match[str]) -> str:
    text = match.group(0)
    if _DATE_PREFIX_RE.match(text):
        return text
    digits = re.sub(r"\D", "", text)
    return f"[REDACTED number ending {digits[-4:]}]"


def _blob(match: re.Match[str]) -> str:
    text = match.group(0)
    has_upper = any(c.isupper() for c in text)
    has_lower = any(c.islower() for c in text)
    has_digit = any(c.isdigit() for c in text)
    # Hex digests, slugs and words are lower-case only; random base64 mixes all three.
    if has_upper and has_lower and has_digit:
        return "[REDACTED:blob]"
    return text


def redact_text(text: str) -> str:
    """Scrub secret-looking content from one string. Idempotent."""
    if not text:
        return text
    out = _PEM_RE.sub("[REDACTED:private_key]", text)
    out = _DATA_URI_RE.sub("[REDACTED:binary]", out)
    out = _HEADER_VALUE_RE.sub(
        lambda m: m.group(1) + m.group(2) + _keep_quotes(m.group(3), "[REDACTED]"), out
    )
    out = _AUTH_SCHEME_RE.sub(lambda m: f"{m.group(1)} [REDACTED]", out)
    out = _SECRET_KV_RE.sub(
        lambda m: m.group(1) + m.group(2) + _keep_quotes(m.group(3), "[REDACTED]"), out
    )
    out = _DCTA_TOKEN_RE.sub("[REDACTED:dcta_token]", out)
    out = _KNOWN_KEY_RE.sub("[REDACTED:key]", out)
    out = _OTP_PHRASE_RE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED:otp]", out)
    out = _NRIC_RE.sub("[REDACTED:nric]", out)
    out = _EMAIL_RE.sub("[REDACTED:email]", out)
    out = _outside_uuids(out, lambda part: _DIGIT_RUN_RE.sub(_digit_run, part))
    return _BLOB_RE.sub(_blob, out)


def _outside_uuids(text: str, func: Any) -> str:
    """Apply *func* to the parts of *text* that are not UUIDs (UUIDs can end in 12 digits)."""
    pieces: list[str] = []
    last = 0
    for match in _UUID_RE.finditer(text):
        pieces.append(func(text[last : match.start()]))
        pieces.append(match.group(0))
        last = match.end()
    pieces.append(func(text[last:]))
    return "".join(pieces)


# ── Key-based redaction ──────────────────────────────────────────────

_SAFE_KEYS = frozenset(
    {
        "authorizationid",
        "authorizationstate",
        "reasoncode",
        "errorcode",
        "statuscode",
        "currencycode",
        "bankcode",
        "countrycode",
        "promptversion",
        "prompthash",
        "sessionid",
        "challengeid",
        "maskedaccountsuffix",
        "maskedsuffix",
        "accountmasked",
    }
)
_SECRET_EXACT = frozenset(
    {
        "code",
        "otp",
        "pin",
        "cvv",
        "cvc",
        "auth",
        "authorization",
        "proxyauthorization",
        "cookie",
        "cookies",
        "setcookie",
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "jwt",
        "bearer",
        "nonce",
        "sig",
        "signature",
        "assertion",
        "attestation",
        "credential",
        "credentials",
        "userhandle",
        "rawid",
        "privatekey",
        "apikey",
        "xapikey",
        "headers",
    }
)
_SECRET_SUFFIXES = (
    "token",
    "secret",
    "password",
    "passwd",
    "passcode",
    "apikey",
    "privatekey",
    "secretkey",
    "accesskey",
    "signingkey",
    "publickey",
    "hmackey",
    "signature",
    "assertion",
    "cookie",
    "otp",
    "otpcode",
    "onetimecode",
    "verificationcode",
    "securitycode",
    "smscode",
    "mfacode",
    "stepupcode",
    "pincode",
    "credential",
    "credentialid",
    "userhandle",
    "nonce",
    "authheader",
    "authorizationheader",
)
_SECRET_SUBSTRINGS = (
    "password",
    "secret",
    "privatekey",
    "apikey",
    "clientdatajson",
    "authenticatordata",
    "attestationobject",
    "signature",
    "assertion",
    "cookie",
    "bearer",
    "onetime",
)
_BINARY_EXACT = frozenset(
    {"wav", "pcm", "mp3", "ogg", "blob", "bytes", "rawbytes", "datauri", "image", "filebytes"}
)
_BINARY_SUBSTRINGS = ("audio", "base64", "b64")
_ACCOUNT_EXACT = frozenset(
    {"pan", "phone", "mobile", "proxy", "email", "uen", "nricfin", "fin", "msisdn"}
)
_ACCOUNT_SUBSTRINGS = (
    "accountnumber",
    "accountno",
    "acctno",
    "acctnum",
    "accountnum",
    "fullaccount",
    "iban",
    "cardnumber",
    "cardno",
    "nric",
    "phonenumber",
    "mobilenumber",
    "proxyvalue",
    "paynowproxy",
)
_MASKED_RE = re.compile(r"^(?:ending\s)?[*•xX]*\d{0,4}$")


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _key_class(key: str) -> str | None:
    """Return "secret", "binary", "account" or None for an ordinary key."""
    nk = _norm_key(key)
    if not nk or nk in _SAFE_KEYS:
        return None
    if "tokens" in nk:  # usage counts: prompt_tokens, max_tokens, ...
        return None
    if (
        nk in _SECRET_EXACT
        or nk.endswith(_SECRET_SUFFIXES)
        or any(s in nk for s in _SECRET_SUBSTRINGS)
        or ("token" in nk)
    ):
        return "secret"
    if nk in _BINARY_EXACT or any(s in nk for s in _BINARY_SUBSTRINGS):
        return "binary"
    if nk in _ACCOUNT_EXACT or any(s in nk for s in _ACCOUNT_SUBSTRINGS):
        return "account"
    return None


def _redact_by_class(cls: str, value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if cls == "binary":
        if isinstance(value, int | float):
            return value
        return "[REDACTED:binary]"
    if cls == "account" and isinstance(value, str | int):
        text = str(value).strip()
        if _MASKED_RE.match(text):
            return text
        digits = re.sub(r"\D", "", text)
        return f"[REDACTED ending {digits[-4:]}]" if len(digits) >= 8 else "[REDACTED]"
    return "[REDACTED]"


# ── Structured redaction ─────────────────────────────────────────────

_MAX_DEPTH = 8
_MAX_ITEMS = 50
_MAX_STRING = 4000


def _to_plain(value: Any) -> Any:
    """Convert well-known value types to JSON-ready values without calling repr()."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal | UUID):
        return str(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except Exception:  # noqa: BLE001 - unknown model: never fall back to repr
            return f"<{type(value).__name__}>"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


def redact(value: Any, _depth: int = 0) -> Any:
    """Return a JSON-ready copy of *value* with secrets removed."""
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED:depth]"
    value = _to_plain(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        text = value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "…[truncated]"
        return redact_text(text)
    if isinstance(value, bytes | bytearray | memoryview):
        return f"[REDACTED:binary {len(bytes(value))} bytes]"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_ITEMS:
                out["…"] = f"[{len(value) - _MAX_ITEMS} more keys]"
                break
            key = redact_text(str(k))[:128]
            cls = _key_class(key)
            out[key] = _redact_by_class(cls, v) if cls else redact(v, _depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        out_list = [redact(v, _depth + 1) for v in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            out_list.append(f"[{len(items) - _MAX_ITEMS} more items]")
        return out_list
    return f"<{type(value).__name__}>"


def bounded_payload(value: Any, max_chars: int) -> tuple[Any, bool]:
    """Redact *value* and cap its JSON size. Returns (payload, truncated)."""
    if value is None:
        return None, False
    clean = redact(value)
    try:
        encoded = json.dumps(clean, ensure_ascii=False)
    except (TypeError, ValueError):
        return "<unserializable>", True
    if len(encoded) <= max_chars:
        return clean, False
    return {"truncated": True, "original_chars": len(encoded), "preview": encoded[:max_chars]}, True


# ── Attribute allowlist ──────────────────────────────────────────────

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9_./{}-]{0,127}$")
_HEX_RE = re.compile(r"^[0-9a-f]{8,128}$")
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MONEY_RE = re.compile(r"^\d{1,7}(?:\.\d{1,2})?$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MASKED_ACCOUNT_RE = re.compile(r"^(?:ending\s)?[*•xX]{0,12}\d{2,4}$")
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_MAX_INT = 9_999_999  # 8+ digit integers look like account numbers: drop them.

#: Caller-settable metadata keys and their value kinds. Anything else is dropped.
ALLOWED_ATTRIBUTES: Mapping[str, str] = MappingProxyType(
    {
        # correlation (opaque, server-generated)
        "conversation_id": "id",
        "conversation_revision": "int",
        "bundle_id": "id",
        "bundle_version": "int",
        "bundle_hash": "hex",
        "envelope_hash": "hex",
        "authorization_id": "id",
        "audit_event_id": "id",
        "audit_seq": "int",
        "request_id": "id",
        "turn_id": "id",
        "turn_index": "int",
        "mandate_version": "int",
        "resumed_from_trace_id": "trace_id",
        "linked_trace_id": "trace_id",
        # workflow facts
        "outcome": "code",
        "reason_code": "reason",
        "operation_type": "code",
        "action_count": "int",
        "leg_count": "int",
        "policy_decision": "code",
        "validator_verdict": "code",
        "semantic_check": "code",
        "clarification_field": "code",
        "approval_state": "code",
        "step_up_required": "bool",
        "risk_band": "code",
        "channel": "code",
        "attempt": "int",
        "http_status": "int",
        "route": "route",
        "error_type": "code",
        "synthetic": "bool",
        # display values (synthetic data, masked identifiers only)
        "amount": "money",
        "currency": "currency",
        "recipient_alias": "label",
        "account_masked": "masked_account",
        # model call facts
        "model": "code",
        "provider": "code",
        "prompt_version": "code",
        "prompt_hash": "hex",
        "reasoning_effort": "code",
        "max_tokens": "int",
        "latency_ms": "number",
        "audio_duration_ms": "number",
    }
)

#: Keys set by the telemetry layer itself; callers cannot set them.
RESERVED_ATTRIBUTES: Mapping[str, str] = MappingProxyType(
    {
        "service": "code",
        "stage": "code",
        "usage_status": "code",
        "cost_status": "code",
        "cost_unavailable_reason": "code",
        "cost_source": "code",
        "dropped_attribute_count": "int",
        "input_truncated": "bool",
        "output_truncated": "bool",
        "long_open": "bool",
    }
)


_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_.:-]{2,63}$")


def safe_reason(value: Any) -> str | None:
    """Reason codes are UPPER_SNAKE (``HARD_LIMIT``, ``HTTP_403``). Codes with digits need
    an underscore, so a one-time code such as ``AB12CD`` or ``482913`` never passes."""
    if not isinstance(value, str) or not _REASON_RE.match(value):
        return None
    if any(c.isdigit() for c in value) and "_" not in value:
        return None
    return value if redact_text(value) == value else None


def safe_code(value: Any) -> str | None:
    """Return *value* if it is a short code-like string with nothing secret, else None."""
    if not isinstance(value, str) or not _CODE_RE.match(value):
        return None
    return value if redact_text(value) == value else None


def is_hex_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX_RE.match(value))


def safe_name(name: Any, default: str = "unnamed") -> str:
    """Span names must be short lower-case identifiers (no free text, no IDs)."""
    if isinstance(name, str) and _SAFE_NAME_RE.match(name) and redact_text(name) == name:
        return name
    return default


def _check(kind: str, value: Any) -> Any:
    """Validate *value* for *kind*; return the cleaned value or raise ValueError."""
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError
    if kind in {"int", "number"}:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError
        if kind == "int" and not isinstance(value, int):
            raise ValueError
        if not math.isfinite(value) or abs(value) > _MAX_INT:
            raise ValueError
        return value
    if kind == "money" and isinstance(value, Decimal):
        value = str(value)
    if not isinstance(value, str):
        raise ValueError
    if kind == "label":
        return redact_text(value.strip())[:80]
    if kind == "reason":
        reason = safe_reason(value)
        if reason is None:
            raise ValueError
        return reason
    patterns = {
        "id": _ID_RE,
        "code": _CODE_RE,
        "route": _ROUTE_RE,
        "hex": _HEX_RE,
        "trace_id": _TRACE_ID_RE,
        "money": _MONEY_RE,
        "currency": _CURRENCY_RE,
        "masked_account": _MASKED_ACCOUNT_RE,
    }
    pattern = patterns.get(kind)
    if pattern is None or not pattern.match(value) or redact_text(value) != value:
        raise ValueError
    return value


def sanitize_attributes(
    attributes: Mapping[str, Any] | None, *, allow_reserved: bool = False
) -> tuple[dict[str, Any], int]:
    """Keep allowlisted, well-formed attributes. Returns (clean, dropped_count)."""
    if not attributes:
        return {}, 0
    clean: dict[str, Any] = {}
    dropped = 0
    try:
        items = list(attributes.items())
    except Exception:  # noqa: BLE001 - not a mapping
        return {}, 1
    for key, value in items:
        kind = ALLOWED_ATTRIBUTES.get(key) if isinstance(key, str) else None
        if kind is None and allow_reserved and isinstance(key, str):
            kind = RESERVED_ATTRIBUTES.get(key)
        if kind is None or value is None:
            dropped += kind is None
            continue
        try:
            clean[key] = _check(kind, value)
        except ValueError:
            dropped += 1
    return clean, dropped


def export_metadata_key_allowed(key: str) -> bool:
    """True for metadata keys that may leave the process (allowlist ∪ reserved)."""
    return key in ALLOWED_ATTRIBUTES or key in RESERVED_ATTRIBUTES

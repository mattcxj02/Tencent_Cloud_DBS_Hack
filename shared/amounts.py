"""Deterministic parser for SGD amount phrases ("fifty bucks", "$1,250.50", "half of that").

Pure functions, no I/O. The agent uses this to turn the user's quoted amount words into
a typed value; the gateway can use the same function to cross-check an amount citation.

The whole phrase must be understood. Anything vague ("about 50"), a range ("50 or 60"),
an ill-formed number ("one fifty"), a foreign currency ("50 USD") or an unknown word is
returned as :class:`AmountRejected` with a reason. The parser never guesses and never
evaluates expressions.

Result types:

- :class:`LiteralAmount`: an exact SGD amount.
- :class:`FractionAmount`: "half of that", "a third of it", "25% of the first one",
  "the same as ...". ``referent`` holds the words after "of"/"as", if any; resolving
  them to an earlier payment is the caller's job.
- :class:`RemainingAmount`: "the rest", "whatever's left in my current account".
- :class:`OutstandingAmount`: "card balance", "the full outstanding amount", "in full".
  The caller must fetch the authoritative figure (for example the card's outstanding).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import StrEnum
from math import gcd
from typing import Literal

from shared.money import Money

__all__ = [
    "AmountRejected",
    "FractionAmount",
    "LiteralAmount",
    "MAX_AMOUNT_PHRASE_LEN",
    "OutstandingAmount",
    "ParsedAmount",
    "PRONOUN_REFERENTS",
    "RejectReason",
    "RemainingAmount",
    "parse_amount",
    "parse_literal",
]

#: Longest amount phrase we try to read; longer text is not an amount phrase.
MAX_AMOUNT_PHRASE_LEN = 120

_CENT = Decimal("0.01")
#: Values above this are rejected before any Decimal quantization (Money has its own cap).
_MAX_PARSE_VALUE = Decimal(10) ** 9

#: Referents that point at "the" earlier amount without naming it.
PRONOUN_REFERENTS = frozenset(
    {
        "that",
        "it",
        "this",
        "that amount",
        "this amount",
        "the amount",
        "that one",
        "this one",
        "the same",
        "same",
    }
)


class RejectReason(StrEnum):
    EMPTY = "empty"
    UNSUPPORTED_CURRENCY = "unsupported_currency"
    APPROXIMATE = "approximate"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    INVALID_PRECISION = "invalid_precision"
    OUT_OF_RANGE = "out_of_range"
    ZERO = "zero"


@dataclass(frozen=True)
class LiteralAmount:
    amount: Money
    kind: Literal["literal"] = "literal"


@dataclass(frozen=True)
class FractionAmount:
    numerator: int
    denominator: int
    referent: str | None = None
    kind: Literal["fraction"] = "fraction"


@dataclass(frozen=True)
class RemainingAmount:
    referent: str | None = None
    kind: Literal["remaining"] = "remaining"


@dataclass(frozen=True)
class OutstandingAmount:
    kind: Literal["outstanding"] = "outstanding"


@dataclass(frozen=True)
class AmountRejected:
    reason: RejectReason
    kind: Literal["rejected"] = "rejected"


ParsedAmount = LiteralAmount | FractionAmount | RemainingAmount | OutstandingAmount | AmountRejected

# ── Vocabulary ────────────────────────────────────────────────────────

_UNITS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
_TEENS = {
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_BIG_SCALES = {"thousand": 1000, "grand": 1000, "million": 1_000_000}
_DIGIT_MULTIPLIERS = {"k": 1000, "thousand": 1000, "grand": 1000, "hundred": 100}
_NUMBER_WORDS = (
    set(_UNITS) | set(_TEENS) | set(_TENS) | set(_BIG_SCALES) | {"hundred", "and", "a", "an", "k"}
)

_SGD_PREFIXES = {"$", "s$", "sg$", "sgd"}
_SGD_SUFFIXES_1 = {"sgd", "dollars", "dollar", "bucks", "buck"}
_SGD_SUFFIXES_2 = {("singapore", "dollars"), ("singapore", "dollar"), ("sing", "dollars")}
_CENT_WORDS = {"cents", "cent"}

_FOREIGN_CURRENCY = re.compile(
    r"(?:[€£¥₹₩₱฿₫]"
    r"|\b(?:usd|eur|gbp|jpy|cny|rmb|myr|aud|hkd|idr|inr|thb|php|krw|twd|nzd|cad|chf|vnd)\b"
    r"|\b(?:us|a|hk|nz|c|nt)\$"
    r"|\bu\.?s\.?\s*dollars?\b"
    r"|\b(?:american|australian|hong\s+kong|canadian|new\s+zealand|taiwan|brunei)\s+dollars?\b"
    r"|\b(?:euros?|pounds?|quid|yen|yuan|renminbi|ringgit|baht|rupees?|rupiah|pesos?|won|dong)\b"
    r"|\brm\s*\d|\brp\s*\d)"
)
_APPROXIMATE = re.compile(
    r"\b(?:about|around|approximately|approx|roughly|almost|nearly|maybe|perhaps|probably"
    r"|ish|something\s+like|up\s+to|at\s+least|at\s+most|no\s+more\s+than|less\s+than"
    r"|more\s+than|over|under|few|couple|several|some|odd|plus|minus)\b"
)
_CONNECTORS = re.compile(r"\b(?:or|to)\b|/|\d\s*-\s*\d")

_FRACTIONS: dict[str, tuple[int, int]] = {
    "half": (1, 2),
    "a half": (1, 2),
    "one half": (1, 2),
    "a third": (1, 3),
    "one third": (1, 3),
    "two thirds": (2, 3),
    "a quarter": (1, 4),
    "one quarter": (1, 4),
    "three quarters": (3, 4),
    "a fourth": (1, 4),
    "one fourth": (1, 4),
    "three fourths": (3, 4),
    "a fifth": (1, 5),
    "one fifth": (1, 5),
    "two fifths": (2, 5),
    "three fifths": (3, 5),
    "four fifths": (4, 5),
    "a tenth": (1, 10),
    "one tenth": (1, 10),
}
_FRACTION_ALT = "|".join(sorted((re.escape(k) for k in _FRACTIONS), key=len, reverse=True))
_FRACTION_RE = re.compile(
    rf"^(?:exactly\s+)?(?P<frac>{_FRACTION_ALT})(?:\s+(?:of\s+)?(?P<ref>.+?))?$"
    rf"|^(?P<pct>\d{{1,3}})\s*(?:%|percent|per\s+cent)(?:\s+of(?:\s+(?P<pref>.+?))?)?$"
)
_SAME_RE = re.compile(r"^(?:the\s+)?same(?:\s+amount)?(?:\s+as\s+(?P<ref>.+?))?$")
_REMAINING_RE = re.compile(
    r"^(?:all\s+(?:of\s+)?)?(?:the\s+)?"
    r"(?:rest|remainder|remaining(?:\s+(?:balance|amount|money|funds))?|leftover|leftovers"
    r"|balance\s+left"
    r"|(?:whatever|what|everything|all\s+that)(?:\s+is|'s)?\s+(?:left(?:\s+over)?|remaining))"
    r"(?:\s+(?:of|in|from)\s+(?P<ref>.+?))?$"
)
_OUTSTANDING_RE = re.compile(
    r"^(?:in\s+full|(?:everything|what|all)\s+(?:i\s+owe|owed|owing))$"
    r"|^(?:the\s+|my\s+)?(?P<full>(?:full|whole|entire|total|complete)\s+)?"
    r"(?P<card>(?:credit\s+)?card(?:'s)?\s+)?"
    r"(?:(?P<outstanding>outstanding(?:\s+(?:balance|amount))?)|(?P<statement>statement(?:\s+balance)?)"
    r"|(?P<due>amount\s+(?:due|owing|outstanding))|balance|bill|amount)$"
)
_TOKEN_RE = re.compile(r"sg\$|s\$|\$|\d[\d,]*(?:\.\d+)?|[a-z]+(?:'[a-z]+)?|\S")
_DIGITS_RE = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?$")


# ── Public API ────────────────────────────────────────────────────────


def parse_amount(text: str) -> ParsedAmount:
    """Parse one amount phrase. Never guesses; see the module docstring."""
    if not isinstance(text, str):
        raise TypeError("amount phrase must be a string")
    phrase = _normalize(text)
    if not phrase:
        return AmountRejected(RejectReason.EMPTY)
    if len(phrase) > MAX_AMOUNT_PHRASE_LEN:
        return AmountRejected(RejectReason.UNSUPPORTED)
    if _FOREIGN_CURRENCY.search(phrase):
        return AmountRejected(RejectReason.UNSUPPORTED_CURRENCY)
    if _APPROXIMATE.search(phrase):
        return AmountRejected(RejectReason.APPROXIMATE)

    for relative in (_parse_outstanding, _parse_remaining, _parse_fraction):
        result = relative(phrase)
        if result is not None:
            return result

    if _CONNECTORS.search(phrase):
        return AmountRejected(RejectReason.AMBIGUOUS)
    return _parse_literal_phrase(phrase)


def parse_literal(text: str) -> Money | None:
    """Return the exact SGD amount for a literal phrase, else ``None``."""
    result = parse_amount(text)
    return result.amount if isinstance(result, LiteralAmount) else None


# ── Normalization ─────────────────────────────────────────────────────


def _normalize(text: str) -> str:
    s = text.casefold()
    s = s.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"(?<=[a-z0-9])-(?=[a-z])|(?<=[a-z])-(?=[a-z0-9])", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^[\s\"'“”(\[]+", "", s)
    s = re.sub(r"[\s\"'“”)\].,!?;:]+$", "", s)
    return s


def _clean_referent(ref: str | None) -> str | None:
    if ref is None:
        return None
    ref = ref.strip()
    return ref or None


# ── Relative phrases ──────────────────────────────────────────────────


def _parse_outstanding(phrase: str) -> ParsedAmount | None:
    m = _OUTSTANDING_RE.match(phrase)
    if m is None:
        return None
    if m.group(0).startswith(("in full", "everything", "what", "all")):
        return OutstandingAmount()
    qualified = any(m.group(g) for g in ("full", "card", "outstanding", "statement", "due"))
    # A bare "balance", "bill" or "amount" could mean anything: ask.
    return OutstandingAmount() if qualified else AmountRejected(RejectReason.AMBIGUOUS)


def _parse_remaining(phrase: str) -> ParsedAmount | None:
    m = _REMAINING_RE.match(phrase)
    if m is None:
        return None
    return RemainingAmount(referent=_clean_referent(m.group("ref")))


def _parse_fraction(phrase: str) -> ParsedAmount | None:
    m = _SAME_RE.match(phrase)
    if m is not None:
        return FractionAmount(1, 1, referent=_clean_referent(m.group("ref")))
    m = _FRACTION_RE.match(phrase)
    if m is None:
        return None
    if m.group("frac"):
        num, den = _FRACTIONS[m.group("frac")]
        ref = m.group("ref")
    else:
        pct = int(m.group("pct"))
        if not 1 <= pct <= 100:
            return AmountRejected(RejectReason.UNSUPPORTED)
        common = gcd(pct, 100)
        num, den = pct // common, 100 // common
        ref = m.group("pref")
    if ref is not None:
        # "half a grand" is a literal amount, not a reference to another payment.
        inner = _parse_literal_phrase(ref)
        if isinstance(inner, LiteralAmount):
            value = inner.amount.value * num / den
            if value != value.quantize(_CENT):
                return AmountRejected(RejectReason.AMBIGUOUS)
            return _to_literal(value)
    return FractionAmount(num, den, referent=_clean_referent(ref))


# ── Literal amounts ───────────────────────────────────────────────────


def _parse_literal_phrase(phrase: str) -> ParsedAmount:
    tokens = _TOKEN_RE.findall(phrase)
    if tokens and tokens[0] in _SGD_PREFIXES:
        tokens = tokens[1:]
    if not tokens:
        return AmountRejected(RejectReason.UNSUPPORTED)

    cents = Decimal(0)
    if tokens[-1] in _CENT_WORDS:
        split = _split_cents(tokens[:-1])
        if isinstance(split, AmountRejected):
            return split
        dollar_tokens, cent_tokens = split
        cent_value = _parse_number(cent_tokens)
        if isinstance(cent_value, AmountRejected):
            return cent_value
        if cent_value != cent_value.to_integral_value() or not 0 <= cent_value <= 99:
            return AmountRejected(RejectReason.UNSUPPORTED)
        cents = cent_value / 100
        if not dollar_tokens:
            return _to_literal(cents)
        tokens = dollar_tokens
    else:
        tokens = _strip_sgd_suffix(tokens)

    dollars = _parse_number(tokens)
    if isinstance(dollars, AmountRejected):
        return dollars
    if cents and dollars != dollars.to_integral_value():
        return AmountRejected(RejectReason.AMBIGUOUS)
    return _to_literal(dollars + cents)


def _strip_sgd_suffix(tokens: list[str]) -> list[str]:
    if len(tokens) >= 2 and (tokens[-2], tokens[-1]) in _SGD_SUFFIXES_2:
        return tokens[:-2]
    if tokens and tokens[-1] in _SGD_SUFFIXES_1:
        return tokens[:-1]
    return tokens


def _split_cents(tokens: list[str]) -> tuple[list[str], list[str]] | AmountRejected:
    """Split "<dollars> dollars [and] <cents>" (before the word "cents")."""
    for i in range(len(tokens) - 1, -1, -1):
        if tokens[i] in _SGD_SUFFIXES_1:
            dollar_tokens = tokens[:i]
            # Allow "singapore dollars"/"sing dollars" before the cents part.
            if dollar_tokens and dollar_tokens[-1] in {"singapore", "sing"}:
                dollar_tokens = dollar_tokens[:-1]
            cent_tokens = tokens[i + 1 :]
            if cent_tokens and cent_tokens[0] == "and":
                cent_tokens = cent_tokens[1:]
            if not dollar_tokens or not cent_tokens:
                return AmountRejected(RejectReason.UNSUPPORTED)
            return dollar_tokens, cent_tokens
    return [], tokens


def _parse_number(tokens: list[str]) -> Decimal | AmountRejected:
    if not tokens:
        return AmountRejected(RejectReason.UNSUPPORTED)
    if tokens == ["zero"] or tokens == ["0"]:
        return Decimal(0)
    if _DIGITS_RE.match(tokens[0]) or ("," in tokens[0] and tokens[0][0].isdigit()):
        return _parse_digits(tokens)
    value = _parse_words(tokens)
    if value is None:
        return AmountRejected(_unparsed_reason(tokens))
    return Decimal(value)


def _parse_digits(tokens: list[str]) -> Decimal | AmountRejected:
    m = _DIGITS_RE.match(tokens[0])
    if m is None:
        # "1,25" or "1.250,50": a decimal comma or bad grouping is ambiguous.
        return AmountRejected(RejectReason.AMBIGUOUS)
    if len(tokens) > 2 or (len(tokens) == 2 and tokens[1] not in _DIGIT_MULTIPLIERS):
        return AmountRejected(_unparsed_reason(tokens))
    try:
        value = Decimal(tokens[0].replace(",", ""))
    except InvalidOperation:
        return AmountRejected(RejectReason.UNSUPPORTED)
    if len(tokens) == 2:
        value *= _DIGIT_MULTIPLIERS[tokens[1]]
    if value > _MAX_PARSE_VALUE:
        return AmountRejected(RejectReason.OUT_OF_RANGE)
    if value != value.quantize(_CENT, rounding=ROUND_DOWN):
        return AmountRejected(RejectReason.INVALID_PRECISION)
    return value


def _unparsed_reason(tokens: list[str]) -> RejectReason:
    """A sequence made only of number words/digits is an unclear number; else unsupported."""
    numeric = all(t in _NUMBER_WORDS or t == "zero" or _DIGITS_RE.match(t) for t in tokens)
    return RejectReason.AMBIGUOUS if numeric else RejectReason.UNSUPPORTED


def _parse_words(tokens: list[str]) -> int | None:
    """Parse English number words: "two hundred and fifty", "a grand", "fifteen hundred"."""
    total = 0
    last_scale: int | None = None
    i, n = 0, len(tokens)
    while i < n:
        group, i = _parse_group(tokens, i)
        if group is None:
            return None
        if i < n and tokens[i] in _BIG_SCALES:
            scale = _BIG_SCALES[tokens[i]]
            if last_scale is not None and scale >= last_scale:
                return None
            total += group * scale
            last_scale = scale
            i += 1
            if i < n and tokens[i] == "and":
                i += 1
                if i == n:
                    return None
            continue
        total += group
        return total if i == n else None
    return total


def _parse_group(tokens: list[str], i: int) -> tuple[int | None, int]:
    """Parse a value below one million that precedes an optional big scale word."""
    n = len(tokens)
    if tokens[i] in ("a", "an"):
        if i + 1 < n and tokens[i + 1] in _BIG_SCALES:
            return 1, i + 1
        if i + 1 < n and tokens[i + 1] == "hundred":
            value, i = 1, i + 1
        else:
            return None, i
    else:
        small, i = _parse_small(tokens, i)
        if small is None:
            return None, i
        value = small
    if i < n and tokens[i] == "hundred":
        value *= 100
        i += 1
        if i < n and tokens[i] == "and":
            i += 1
            small, i = _parse_small(tokens, i)
            if small is None:
                return None, i
            value += small
        elif i < n and tokens[i] in (_UNITS.keys() | _TEENS.keys() | _TENS.keys()):
            small, i = _parse_small(tokens, i)
            value += small or 0
    return value, i


def _parse_small(tokens: list[str], i: int) -> tuple[int | None, int]:
    """Parse 1-99 from words."""
    if i >= len(tokens):
        return None, i
    word = tokens[i]
    if word in _TENS:
        value = _TENS[word]
        if i + 1 < len(tokens) and tokens[i + 1] in _UNITS:
            return value + _UNITS[tokens[i + 1]], i + 2
        return value, i + 1
    if word in _TEENS:
        return _TEENS[word], i + 1
    if word in _UNITS:
        return _UNITS[word], i + 1
    return None, i


def _to_literal(value: Decimal) -> ParsedAmount:
    if value == 0:
        return AmountRejected(RejectReason.ZERO)
    if value > _MAX_PARSE_VALUE:
        return AmountRejected(RejectReason.OUT_OF_RANGE)
    if value != value.quantize(_CENT, rounding=ROUND_DOWN):
        return AmountRejected(RejectReason.INVALID_PRECISION)
    try:
        return LiteralAmount(Money(value.quantize(_CENT)))
    except ValueError:
        return AmountRejected(RejectReason.OUT_OF_RANGE)

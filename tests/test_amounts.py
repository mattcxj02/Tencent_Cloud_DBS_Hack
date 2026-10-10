"""shared/amounts.py: deterministic SGD amount phrase parsing (T-09).

The parser must understand the whole phrase or reject it with a reason; it never guesses.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from shared.amounts import (
    AmountRejected,
    FractionAmount,
    LiteralAmount,
    OutstandingAmount,
    RejectReason,
    RemainingAmount,
    parse_amount,
    parse_literal,
)
from shared.money import Money


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # digits, currency marks, grouping, decimals
        ("50", "50.00"),
        ("$50", "50.00"),
        ("$ 50", "50.00"),
        ("S$50", "50.00"),
        ("s$ 50.5", "50.50"),
        ("SGD 1,250.50", "1250.50"),
        ("sgd50", "50.00"),
        ("1,250.50 SGD", "1250.50"),
        ("$1,250.50", "1250.50"),
        ("1,000,000", "1000000.00"),
        ("0.5", "0.50"),
        ("$0.05", "0.05"),
        ("050", "50.00"),
        ("50.", "50.00"),
        ("“$50”", "50.00"),
        ("50 dollars", "50.00"),
        ("50 singapore dollars", "50.00"),
        ("$50 bucks", "50.00"),
        # k / thousand / grand / hundred after digits
        ("1.5k", "1500.00"),
        ("1.5K", "1500.00"),
        ("$2k", "2000.00"),
        ("2 k", "2000.00"),
        ("0.5k", "500.00"),
        ("1.23456k", "1234.56"),
        ("2 thousand", "2000.00"),
        ("1.5 grand", "1500.00"),
        ("5 hundred", "500.00"),
        # colloquial words
        ("fifty bucks", "50.00"),
        ("fifteen dollars", "15.00"),
        ("fifteen", "15.00"),
        ("a grand", "1000.00"),
        ("two grand", "2000.00"),
        ("a hundred", "100.00"),
        ("a hundred bucks", "100.00"),
        ("two hundred", "200.00"),
        ("two hundred and fifty", "250.00"),
        ("two hundred fifty", "250.00"),
        ("twenty-five", "25.00"),
        ("Twenty Five Dollars", "25.00"),
        ("ninety-nine", "99.00"),
        ("twelve hundred", "1200.00"),
        ("twenty five hundred", "2500.00"),
        ("a thousand and fifty", "1050.00"),
        ("one thousand two hundred and fifty", "1250.00"),
        ("a hundred and fifty thousand", "150000.00"),
        ("one million", "1000000.00"),
        # cents
        ("fifty dollars and twenty cents", "50.20"),
        ("50 dollars 5 cents", "50.05"),
        ("50 cents", "0.50"),
        ("twenty cents", "0.20"),
        ("one cent", "0.01"),
        # literal fractions of a literal
        ("half a grand", "500.00"),
        ("a quarter of 200", "50.00"),
    ],
)
def test_literal_amounts(text: str, expected: str) -> None:
    result = parse_amount(text)
    assert result == LiteralAmount(Money(expected))
    assert parse_literal(text) == Money(expected)


@pytest.mark.parametrize(
    ("text", "num", "den", "referent"),
    [
        ("half", 1, 2, None),
        ("half of that", 1, 2, "that"),
        ("half of it", 1, 2, "it"),
        ("Half that", 1, 2, "that"),
        ("a half of the first payment", 1, 2, "the first payment"),
        ("a third of it", 1, 3, "it"),
        ("two thirds of that", 2, 3, "that"),
        ("a quarter of the second one", 1, 4, "the second one"),
        ("three quarters of it", 3, 4, "it"),
        ("a fifth of that", 1, 5, "that"),
        ("a tenth", 1, 10, None),
        ("25% of that", 1, 4, "that"),
        ("50 percent of it", 1, 2, "it"),
        ("33 per cent of that", 33, 100, "that"),
        ("100%", 1, 1, None),
        ("the same", 1, 1, None),
        ("same amount", 1, 1, None),
        ("the same as the first one", 1, 1, "the first one"),
        ("half of what I sent to Mum", 1, 2, "what i sent to mum"),
    ],
)
def test_fractions(text: str, num: int, den: int, referent: str | None) -> None:
    assert parse_amount(text) == FractionAmount(num, den, referent)
    assert parse_literal(text) is None


@pytest.mark.parametrize(
    ("text", "referent"),
    [
        ("the rest", None),
        ("rest", None),
        ("the remainder", None),
        ("remaining balance", None),
        ("the remaining amount", None),
        ("all the rest", None),
        ("whatever's left", None),
        ("whatever is left", None),
        ("what's left in my current account", "my current account"),
        ("everything left", None),
        ("all that's left", None),
        ("the rest of my savings", "my savings"),
        ("the balance left in joint", "joint"),
    ],
)
def test_remaining(text: str, referent: str | None) -> None:
    assert parse_amount(text) == RemainingAmount(referent)


@pytest.mark.parametrize(
    "text",
    [
        "card balance",
        "my card balance",
        "credit card balance",
        "the full balance",
        "full amount",
        "the whole outstanding balance",
        "outstanding",
        "the outstanding amount",
        "statement balance",
        "amount due",
        "card bill",
        "in full",
        "everything I owe",
    ],
)
def test_outstanding(text: str) -> None:
    assert parse_amount(text) == OutstandingAmount()


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", RejectReason.EMPTY),
        ("   ", RejectReason.EMPTY),
        ("...", RejectReason.EMPTY),
        # foreign currencies
        ("50 USD", RejectReason.UNSUPPORTED_CURRENCY),
        ("USD 50", RejectReason.UNSUPPORTED_CURRENCY),
        ("US$50", RejectReason.UNSUPPORTED_CURRENCY),
        ("50 us dollars", RejectReason.UNSUPPORTED_CURRENCY),
        ("€50", RejectReason.UNSUPPORTED_CURRENCY),
        ("£20", RejectReason.UNSUPPORTED_CURRENCY),
        ("¥1000", RejectReason.UNSUPPORTED_CURRENCY),
        ("RM50", RejectReason.UNSUPPORTED_CURRENCY),
        ("50 ringgit", RejectReason.UNSUPPORTED_CURRENCY),
        ("fifty euros", RejectReason.UNSUPPORTED_CURRENCY),
        ("20 quid", RejectReason.UNSUPPORTED_CURRENCY),
        ("100 HKD", RejectReason.UNSUPPORTED_CURRENCY),
        ("50 australian dollars", RejectReason.UNSUPPORTED_CURRENCY),
        ("half of 100 USD", RejectReason.UNSUPPORTED_CURRENCY),
        # vague amounts
        ("about 50", RejectReason.APPROXIMATE),
        ("around fifty", RejectReason.APPROXIMATE),
        ("roughly $1,000", RejectReason.APPROXIMATE),
        ("50-ish", RejectReason.APPROXIMATE),
        ("up to 200", RejectReason.APPROXIMATE),
        ("a couple hundred", RejectReason.APPROXIMATE),
        ("a few grand", RejectReason.APPROXIMATE),
        ("fifty odd", RejectReason.APPROXIMATE),
        ("over 100", RejectReason.APPROXIMATE),
        # ranges, alternatives, ill-formed numbers
        ("50 or 60", RejectReason.AMBIGUOUS),
        ("50-60", RejectReason.AMBIGUOUS),
        ("50 to 60", RejectReason.AMBIGUOUS),
        ("50/60", RejectReason.AMBIGUOUS),
        ("one fifty", RejectReason.AMBIGUOUS),
        ("fifty fifty", RejectReason.AMBIGUOUS),
        ("50 60", RejectReason.AMBIGUOUS),
        ("hundred", RejectReason.AMBIGUOUS),
        ("thousand", RejectReason.AMBIGUOUS),
        ("two thousand thousand", RejectReason.AMBIGUOUS),
        ("1,25", RejectReason.AMBIGUOUS),
        ("12,50", RejectReason.AMBIGUOUS),
        ("a third of 100", RejectReason.AMBIGUOUS),
        ("balance", RejectReason.AMBIGUOUS),
        ("the bill", RejectReason.AMBIGUOUS),
        ("50.5 dollars and 20 cents", RejectReason.AMBIGUOUS),
        # unsupported words / expressions
        ("10 shares", RejectReason.UNSUPPORTED),
        ("-50", RejectReason.UNSUPPORTED),
        ("fifty point five", RejectReason.UNSUPPORTED),
        ("2*50", RejectReason.UNSUPPORTED),
        ("__import__('os')", RejectReason.UNSUPPORTED),
        ("double that", RejectReason.UNSUPPORTED),
        ("all of it", RejectReason.UNSUPPORTED),
        ("150%", RejectReason.UNSUPPORTED),
        ("0%", RejectReason.UNSUPPORTED),
        ("150 cents", RejectReason.UNSUPPORTED),
        ("a lot", RejectReason.UNSUPPORTED),
        ("x" * 200, RejectReason.UNSUPPORTED),
        # precision, range, zero
        ("50.123", RejectReason.INVALID_PRECISION),
        ("$0.001", RejectReason.INVALID_PRECISION),
        ("1.234567k", RejectReason.INVALID_PRECISION),
        ("1,000,001", RejectReason.OUT_OF_RANGE),
        ("two million", RejectReason.OUT_OF_RANGE),
        ("9" * 40, RejectReason.OUT_OF_RANGE),
        ("0", RejectReason.ZERO),
        ("$0.00", RejectReason.ZERO),
        ("zero", RejectReason.ZERO),
        ("zero dollars", RejectReason.ZERO),
    ],
)
def test_rejections(text: str, reason: RejectReason) -> None:
    assert parse_amount(text) == AmountRejected(reason)
    assert parse_literal(text) is None


def test_results_are_money_never_float() -> None:
    result = parse_amount("$0.10")
    assert isinstance(result, LiteralAmount)
    assert isinstance(result.amount, Money)
    assert result.amount.to_str() == "0.10"
    total = parse_literal("0.1") + parse_literal("0.2")  # type: ignore[operator]
    assert total == Money("0.30")


def test_deterministic_and_case_insensitive() -> None:
    assert parse_amount("FIFTY BUCKS") == parse_amount("fifty bucks") == parse_amount("Fifty bucks")
    assert [parse_amount("half of that") for _ in range(3)] == [FractionAmount(1, 2, "that")] * 3


def test_non_string_rejected() -> None:
    with pytest.raises(TypeError):
        parse_amount(50)  # type: ignore[arg-type]


def test_module_is_pure() -> None:
    """No I/O imports and no expression evaluation in the parser."""
    source = Path(__file__).resolve().parent.parent.joinpath("shared", "amounts.py").read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"eval", "exec", "compile", "float"}
    assert imported <= {
        "__future__",
        "re",
        "dataclasses",
        "decimal",
        "enum",
        "math",
        "typing",
        "shared",
    }

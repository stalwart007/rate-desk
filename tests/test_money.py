"""Rule 4: money is money."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rate_desk.money import Money, MoneyError, percent_change


@pytest.mark.parametrize(
    ("text", "pence"),
    [
        ("0", 0),
        ("18", 1800),
        ("18.5", 1850),
        ("18.50", 1850),
        ("18.05", 1805),
        ("0.01", 1),
        (" 22.50 ", 2250),
        ("999999.99", 99_999_999),
    ],
)
def test_parses_pounds_into_exact_pence(text: str, pence: int) -> None:
    assert Money.parse(text).pence == pence


@pytest.mark.parametrize(
    "text",
    [
        "18.505",  # a fraction of a penny is refused, never rounded
        "-1.00",  # rates are not negative
        "+5",
        "1e3",
        "1_000",
        "1,000.00",
        "£18.50",
        "18.",
        ".50",
        "018.50",
        "NaN",
        "Infinity",
        "",
        "1000000.00",  # beyond any plausible rate; keeps values far from integer limits
    ],
)
def test_refuses_anything_that_is_not_plain_pounds_and_pence(text: str) -> None:
    with pytest.raises(MoneyError):
        Money.parse(text)


@pytest.mark.parametrize("value", [18.5, 18, Decimal("18.50"), None])
def test_only_strings_are_parsed_so_floats_never_get_in(value: object) -> None:
    with pytest.raises(MoneyError):
        Money.parse(value)


@pytest.mark.parametrize("value", [1850.0, True, Decimal(1850), "1850"])
def test_money_holds_only_an_int_of_pence(value: object) -> None:
    with pytest.raises(TypeError):
        Money(value)  # type: ignore[arg-type]


def test_the_float_trap_does_not_apply() -> None:
    # 0.1 + 0.2 != 0.3 in binary floating point. In pence it is exact.
    assert Money.parse("0.10") + Money.parse("0.20") == Money.parse("0.30")


def test_formats_for_wire_and_for_people() -> None:
    assert Money(1850).to_decimal_string() == "18.50"
    assert Money(-205).to_decimal_string() == "-2.05"
    assert Money(5).to_decimal_string() == "0.05"
    assert Money(123_450).display() == "£1,234.50"
    assert Money(-205).display() == "-£2.05"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("22.50", "24.00", Decimal("6.7")),  # 6.666… rounds up
        ("24.00", "22.50", Decimal("-6.3")),  # -6.25 rounds half away from zero
        ("20.00", "20.10", Decimal("0.5")),
        ("3.00", "4.00", Decimal("33.3")),
        ("0.03", "0.04", Decimal("33.3")),
        ("25.00", "24.99", Decimal("0.0")),  # -0.04% rounds to zero, not "-0.0"
    ],
)
def test_percentage_change_is_rounded_half_up_to_one_place(
    old: str, new: str, expected: Decimal
) -> None:
    result = percent_change(Money.parse(old), Money.parse(new))
    assert result == expected
    assert str(result) == str(expected)  # catches Decimal("-0.0"), which equals 0


def test_no_percentage_from_zero() -> None:
    assert percent_change(Money(0), Money(100)) is None


pence = st.integers(min_value=0, max_value=99_999_999)


@given(pence)
def test_wire_format_round_trips_for_every_amount(p: int) -> None:
    assert Money.parse(Money(p).to_decimal_string()) == Money(p)


@given(st.decimals(min_value=0, max_value=Decimal("999999.99"), places=2))
def test_parsing_agrees_with_exact_decimal_arithmetic(amount: Decimal) -> None:
    text = f"{amount:.2f}"
    assert Money.parse(text).pence == int(amount * 100)


@given(pence, pence)
def test_differences_are_exact(a: int, b: int) -> None:
    assert (Money(a) - Money(b)) + Money(b) == Money(a)

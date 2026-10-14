"""Money, held as a whole number of pence.

Rule 4 of the brief: "Money is money." Every amount in this system is an
``int`` of pence, from the database column through to the API boundary,
where it becomes a decimal *string* such as ``"18.50"``. A float never
holds money anywhere.

The only arithmetic the system needs is subtraction (how much a rate
changes by), which is exact on integers. The one division, the percentage
change, is for display only, is never stored, and is rounded explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Up to six digits of pounds and at most two of pence. More than two decimal
# places is an error rather than something we round: silently rounding a
# pay rate is worse than refusing it. The upper bound keeps every value well
# inside SQLite's 64-bit INTEGER.
AMOUNT_PATTERN = r"^(0|[1-9][0-9]{0,5})(?:\.([0-9]{1,2}))?$"
_AMOUNT = re.compile(AMOUNT_PATTERN)

_ONE_DP = Decimal("0.1")


class MoneyError(ValueError):
    """Raised when text cannot be read as an amount of money."""


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """An amount of sterling, in pence. May be negative when it is a difference."""

    pence: int

    def __post_init__(self) -> None:
        # bool is a subclass of int; True must not mean 1p.
        if type(self.pence) is not int:
            raise TypeError(f"Money needs an int of pence, got {type(self.pence).__name__}")

    @classmethod
    def parse(cls, text: object) -> Money:
        """Read a non-negative amount written as pounds, e.g. ``"18.5"`` or ``"18.50"``.

        Only strings are accepted. A JSON number would already have passed
        through a float in most clients before reaching us.
        """
        if not isinstance(text, str):
            raise MoneyError('amounts must be written as a string of pounds, like "18.50"')
        match = _AMOUNT.fullmatch(text.strip())
        if match is None:
            raise MoneyError(f"{text!r} is not an amount of pounds with at most two decimal places")
        pounds, pence = match.group(1), match.group(2) or "0"
        return cls(int(pounds) * 100 + int(pence.ljust(2, "0")))

    def __add__(self, other: Money) -> Money:
        return Money(self.pence + other.pence)

    def __sub__(self, other: Money) -> Money:
        return Money(self.pence - other.pence)

    def __neg__(self) -> Money:
        return Money(-self.pence)

    def __abs__(self) -> Money:
        return Money(abs(self.pence))

    def __str__(self) -> str:
        return self.display()

    def to_decimal_string(self) -> str:
        """The wire format: ``"18.50"``, ``"-2.05"``. No currency sign, always two places."""
        sign = "-" if self.pence < 0 else ""
        pounds, pence = divmod(abs(self.pence), 100)
        return f"{sign}{pounds}.{pence:02d}"

    def display(self) -> str:
        """For people: ``"£1,234.50"``, ``"-£2.05"``."""
        sign = "-" if self.pence < 0 else ""
        pounds, pence = divmod(abs(self.pence), 100)
        return f"{sign}£{pounds:,}.{pence:02d}"


def percent_change(old: Money, new: Money) -> Decimal | None:
    """Change from ``old`` to ``new`` as a percentage, rounded half-up to 1 dp.

    ``None`` when there is no meaningful percentage (``old`` is zero).
    Display only; never stored or used to compute money.
    """
    if old.pence == 0:
        return None
    exact = Decimal(new.pence - old.pence) * 100 / Decimal(old.pence)
    # `+ 0` turns Decimal("-0.0") (a 1p cut on £25) into plain 0.0.
    return exact.quantize(_ONE_DP, rounding=ROUND_HALF_UP) + 0

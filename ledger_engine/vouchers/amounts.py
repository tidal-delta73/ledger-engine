"""Deterministic fixed-point amount rules: integer cents only.

Every later stage (normalization today; posting, reversals and
recomputation later) shares these two primitives, so no caller ever
re-implements string validation, decimal handling or total formatting.
There is no float anywhere: huge values keep full integer precision and
fractional additions are exact.
"""
from __future__ import annotations

import re

from .errors import InvalidEntryAmountError

__all__ = ["parse_cents", "format_cents", "AMOUNT_PATTERN"]

# Fixed-point decimal only: digits, optional one dot with 1-2 fraction
# digits.  No sign, exponent, thousands separator, whitespace or leading dot.
_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")

# Exposed for components that need to report the same syntactic boundary
# without parsing twice; the pattern itself is an implementation detail.
AMOUNT_PATTERN = _AMOUNT_RE.pattern


def parse_cents(value: object, *, where: str) -> int:
    """Parse ``value`` into a non-negative integer amount of cents.

    Accepts only the fixed-point strings matched by the module pattern.
    Any other type or spelling raises :class:`InvalidEntryAmountError`.
    """
    if not isinstance(value, str) or _AMOUNT_RE.fullmatch(value) is None:
        raise InvalidEntryAmountError(
            f"{where}: amount must be a fixed-point decimal string with at "
            f"most two places, got {value!r}"
        )
    if "." in value:
        whole, frac = value.split(".", 1)
    else:
        whole, frac = value, ""
    return int(whole) * 100 + int((frac + "00")[:2])


def format_cents(cents: int) -> str:
    """Render an integer-cents amount with exactly two decimal places."""
    return f"{cents // 100}.{cents % 100:02d}"

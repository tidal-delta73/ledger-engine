"""Signed fixed-point amounts for reconciliation.

The voucher side of the engine only allows non-negative strings, with the
side (debit/credit) carrying the sign.  An external reconciliation detail
carries a single signed original-currency amount, so this module adds the
*signed* counterpart of :func:`ledger_engine.vouchers.amounts.parse_cents`
while keeping the exact same precision and rounding contract:

* fixed-point decimal strings with at most two fractional digits;
* an optional single leading ``-`` or ``+``;
* integer-cents result, no float at any magnitude;
* a non-string or otherwise malformed value raises
  :class:`~ledger_engine.vouchers.errors.InvalidEntryAmountError`, the
  shared leaf category, so an external-format defect never invents a new
  exception hierarchy.
"""
from __future__ import annotations

import re

from ..vouchers.errors import InvalidEntryAmountError

__all__ = ["parse_signed_cents", "format_signed_cents", "SIGNED_AMOUNT_PATTERN"]

_SIGNED_AMOUNT_RE = re.compile(r"[+-]?[0-9]+(?:\.[0-9]{1,2})?")

SIGNED_AMOUNT_PATTERN = _SIGNED_AMOUNT_RE.pattern


def parse_signed_cents(value: object, *, where: str) -> int:
    """Parse ``value`` into a signed integer amount of cents.

    Mirrors the voucher amount grammar but permits one leading sign.
    """
    if not isinstance(value, str) or _SIGNED_AMOUNT_RE.fullmatch(value) is None:
        raise InvalidEntryAmountError(
            f"{where}: amount must be a signed fixed-point decimal string "
            f"with at most two places, got {value!r}"
        )
    sign = 1
    digits = value
    if digits[0] in "+-":
        if digits[0] == "-":
            sign = -1
        digits = digits[1:]
    if "." in digits:
        whole, frac = digits.split(".", 1)
    else:
        whole, frac = digits, ""
    return sign * (int(whole) * 100 + int((frac + "00")[:2]))


def format_signed_cents(cents: int) -> str:
    """Render a signed integer-cents amount with exactly two decimals."""
    sign = "-" if cents < 0 else ""
    magnitude = abs(cents)
    return f"{sign}{magnitude // 100}.{magnitude % 100:02d}"

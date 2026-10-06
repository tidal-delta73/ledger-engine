"""Integer-cent fixed-point arithmetic for monetary amounts.

Money is only ever handled as integer cents inside the engine; this
module owns the only two conversions between that representation and
the public string form.  Nothing here touches floats, locales, rounding
modes or global state, so results are bit-for-bit reproducible.
"""
from __future__ import annotations

import re

from ._errors import InvalidEntryAmountError

# Fixed-point decimal only: digits, optional one dot with 1-2 fraction digits.
# No sign, exponent, thousands separator, whitespace or leading dot.
_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")


def parse_amount_cents(value: object, *, where: str) -> int:
    """Return the amount in integer cents; raise InvalidEntryAmountError."""
    if not isinstance(value, str) or _AMOUNT_RE.fullmatch(value) is None:
        raise InvalidEntryAmountError(f"{where}: amount must be a fixed-point "
                                     f"decimal string with at most two places, "
                                     f"got {value!r}")
    if "." in value:
        whole, frac = value.split(".", 1)
    else:
        whole, frac = value, ""
    return int(whole) * 100 + int((frac + "00")[:2])


def format_cents(cents: int) -> str:
    """Render integer cents with exactly two decimal places."""
    return f"{cents // 100}.{cents % 100:02d}"

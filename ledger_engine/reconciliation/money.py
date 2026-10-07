"""Deterministic money and rate rules for reconciliation.

The voucher side only ever stores non-negative integer cents in the
single base currency.  Reconciliation additionally deals with:

* signed original-currency amounts (an external line is one amount with
  an optional sign);
* currencies other than the base, each with its own minor-unit
  precision, compared in that currency's exact integer minor units;
* the base-currency amounts *already recorded* on the books, which are
  never recomputed at the current rate -- only re-derived, when needed
  for presentation, from the exact original/base minor-unit pair;
* exact rational exchange rates, formatted with banker-free
  half-up rounding so the rounding semantics never depend on the
  platform's ``Decimal`` context or locale.

Everything stays in :class:`fractions.Fraction` / integers; no float
ever touches an amount or a rate.
"""
from __future__ import annotations

import re
from fractions import Fraction

from .errors import InvalidReconciliationInputError

__all__ = [
    "MONEY_PATTERN",
    "RATE_PATTERN",
    "BASE_PRECISION",
    "BASE_SCALE",
    "precision_for",
    "parse_minor_units",
    "parse_base_cents",
    "format_minor",
    "format_base_cents",
    "parse_rate",
    "format_rate",
    "effective_rate",
    "round_half_up",
    "valid_currency_code",
]

# The base currency always keeps the voucher engine's fixed integer-cents
# precision (two decimal places), regardless of its currency code: the
# existing posting/trial-balance semantics are reused verbatim.  Only
# foreign *original* amounts take the currency's own ISO minor-unit
# precision.
BASE_PRECISION = 2
BASE_SCALE = 10 ** BASE_PRECISION

# Optional sign, then digits with at most 4 fraction digits.  Per
# currency the accepted fraction length is narrowed by ``precision_for``.
_MONEY_RE = re.compile(r"[+-]?[0-9]+(?:\.[0-9]{1,4})?")
MONEY_PATTERN = _MONEY_RE.pattern

# A rate is an optional-sign fixed-point decimal with up to 10 fraction
# digits; it is always converted to an exact Fraction.
_RATE_RE = re.compile(r"[+-]?[0-9]+(?:\.[0-9]{1,10})?")
RATE_PATTERN = _RATE_RE.pattern

_CURRENCY_RE = re.compile(r"[A-Z]{3}")

# Minor-unit precision per currency.  The book set's base currency always
# resolves to precision 2 through ``DEFAULT_PRECISION``; other codes carry
# the ISO-4217 minor-unit exponent where one exists, 2 by default and 0
# for the zero-decimal currencies reconciliation must compare exactly.
DEFAULT_PRECISION = 2
_ZERO_DECIMAL_CURRENCIES = frozenset(
    {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "PYG", "RWF",
     "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
)
_THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD",
                                       "OMR", "TND"})


def valid_currency_code(value: object) -> bool:
    """True iff ``value`` is exactly three uppercase ASCII letters."""
    return isinstance(value, str) and _CURRENCY_RE.fullmatch(value) is not None


def precision_for(currency: str) -> int:
    """Minor-unit exponent used when parsing/formatting ``currency``."""
    if currency in _ZERO_DECIMAL_CURRENCIES:
        return 0
    if currency in _THREE_DECIMAL_CURRENCIES:
        return 3
    return DEFAULT_PRECISION


def parse_minor_units(value: object, currency: str, *, where: str) -> int:
    """Parse a signed fixed-point amount into exact integer minor units.

    The maximum accepted fraction length is the currency's precision
    (so JPY rejects ``"1.5"`` and CNY rejects ``"1.001"``), reusing the
    existing two-place semantics for every ordinary two-decimal currency.
    """
    precision = precision_for(currency)
    if not isinstance(value, str) or _MONEY_RE.fullmatch(value) is None:
        raise InvalidReconciliationInputError(
            f"{where}: amount must be a fixed-point decimal string with at "
            f"most {precision} decimal places, got {value!r}"
        )
    sign = -1 if value[0] == "-" else 1
    digits = value[1:] if value[0] in "+-" else value
    if "." in digits:
        whole, frac = digits.split(".", 1)
    else:
        whole, frac = digits, ""
    if len(frac) > precision:
        raise InvalidReconciliationInputError(
            f"{where}: amount {value!r} has more than {precision} decimal "
            f"places for currency {currency!r}"
        )
    scale = 10 ** precision
    padded = (frac + "0" * precision)[:precision]
    return sign * (int(whole) * scale + (int(padded) if padded else 0))


def format_minor(minor_units: int, currency: str) -> str:
    """Render signed integer minor units with the currency's precision."""
    precision = precision_for(currency)
    scale = 10 ** precision
    sign = "-" if minor_units < 0 else ""
    magnitude = abs(minor_units)
    if precision == 0:
        return f"{sign}{magnitude}"
    return f"{sign}{magnitude // scale}.{magnitude % scale:0{precision}d}"


# Fixed-point rule identical to the voucher engine's amount pattern:
# non-negative, at most two fraction digits, no sign or exponent.
_BASE_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")


def parse_base_cents(value: object, *, where: str) -> int:
    """Parse a base-currency amount with the engine's fixed two-place rule.

    Identical precision and rounding semantics to the voucher pipeline's
    ``parse_cents`` (non-negative, at most two fraction digits, integer
    cents); it is deliberately independent of the base currency's ISO
    code.
    """
    if not isinstance(value, str) or _BASE_AMOUNT_RE.fullmatch(value) is None:
        raise InvalidReconciliationInputError(
            f"{where}: base amount must be a non-negative fixed-point "
            f"decimal string with at most two decimal places, got {value!r}"
        )
    if "." in value:
        whole, frac = value.split(".", 1)
    else:
        whole, frac = value, ""
    return int(whole) * BASE_SCALE + int((frac + "00")[:2])


def format_base_cents(cents: int) -> str:
    """Render base-currency integer cents with exactly two places."""
    sign = "-" if cents < 0 else ""
    magnitude = abs(cents)
    return f"{sign}{magnitude // BASE_SCALE}.{magnitude % BASE_SCALE:02d}"


def parse_rate(value: object, *, where: str) -> Fraction:
    """Parse a fixed-point rate string into an exact :class:`Fraction`.

    Rates must be strictly positive finite decimals; zero, negative,
    exponent/spelled floats and missing values are rejected.
    """
    if not isinstance(value, str) or _RATE_RE.fullmatch(value) is None:
        raise InvalidReconciliationInputError(
            f"{where}: rate must be a positive fixed-point decimal string, "
            f"got {value!r}"
        )
    if "." in value:
        whole, frac = value.split(".", 1)
    else:
        whole, frac = value, ""
    scale = 10 ** len(frac)
    rate = Fraction(int(whole) * scale + (int(frac) if frac else 0), scale)
    if rate <= 0:
        raise InvalidReconciliationInputError(
            f"{where}: rate must be positive, got {value!r}"
        )
    return rate


def round_half_up(numerator: int, denominator: int) -> int:
    """Round ``numerator / denominator`` to the nearest integer, half up.

    Exact halves always round away from zero; this is the single
    rounding rule reused for every displayed (never stored) value.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    quotient, remainder = divmod(abs(numerator), denominator)
    if remainder * 2 >= denominator:
        quotient += 1
    return quotient if numerator >= 0 else -quotient


def format_rate(rate: Fraction) -> str:
    """Format an exact rate with six decimal places, half-up.

    The rate text is display-only evidence; the exact Fraction is what
    matching uses.  Six places suffice to round-trip the ten-place input
    strings accepted by :func:`parse_rate` for comparison purposes while
    staying locale independent.
    """
    scaled = round_half_up(rate.numerator * 10 ** 6, rate.denominator)
    sign = "-" if scaled < 0 else ""
    magnitude = abs(scaled)
    return f"{sign}{magnitude // 10 ** 6}.{magnitude % 10 ** 6:06d}"


def effective_rate(
    original_minor: int,
    base_minor: int,
    original_currency: str,
) -> Fraction | None:
    """Re-derive the recorded rate from the saved original/base pair.

    Returns ``None`` when the original amount is zero (no rate is
    definable).  The rate is the rate *already recorded* on the books:
    ``base_amount / original_amount`` as exact decimal amounts (base in
    the fixed two-place cents), and it never consults a current or
    externally supplied exchange rate.
    """
    if original_minor == 0:
        return None
    scale_original = 10 ** precision_for(original_currency)
    return Fraction(base_minor * scale_original,
                    original_minor * BASE_SCALE)

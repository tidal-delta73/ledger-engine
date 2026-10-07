"""Accounting-period calendar for cutoff-time reconciliation.

Whether a cutoff falls in a closed period is *data*, never the wall clock:
the request supplies the period table with its recorded ``closed`` flags,
so a report for a historical cutoff is reproducible long after the period
was closed (and must not depend on the current date or the machine's
calendar).  This module only parses that table and answers period
lookup questions; it performs no posting and keeps no state.

Periods must be listed chronologically with strictly increasing start
dates and no overlaps; gaps between periods are allowed.  A date that
falls inside a gap belongs to no period.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..vouchers.structure import _valid_calendar_date
from .errors import ValidationAborted

__all__ = ["Period", "validate_periods", "covering_period"]


@dataclass(frozen=True)
class Period:
    """One immutable period row, dates pre-parsed for ordered comparison."""

    period_id: str
    start_date: str
    end_date: str
    closed: bool
    start: date
    end: date

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


def validate_periods(periods: object) -> tuple[Period, ...]:
    """Validate and order the raw period table.

    Absent means "no period controls are recorded" and yields an empty
    tuple.  A malformed table raises
    :class:`~ledger_engine.reconciliation.errors.ValidationAborted`, which
    the request boundary converts into an ``invalid_input`` outcome.
    """
    if periods is None:
        return ()
    if not isinstance(periods, list):
        raise ValidationAborted("periods must be a list")
    rows: list[Period] = []
    for index, item in enumerate(periods):
        where = f"period #{index}"
        if not isinstance(item, dict):
            raise ValidationAborted(f"{where}: period must be an object")
        for field in ("period_id", "start_date", "end_date", "closed"):
            if field not in item:
                raise ValidationAborted(
                    f"{where}: missing field {field!r}")
        period_id = item["period_id"]
        start_text = item["start_date"]
        end_text = item["end_date"]
        closed = item["closed"]
        if not isinstance(period_id, str) or period_id == "":
            raise ValidationAborted(
                f"{where}: period_id must be a non-empty string, "
                f"got {period_id!r}")
        if not isinstance(start_text, str) or not _valid_calendar_date(
                start_text):
            raise ValidationAborted(
                f"{where} ({period_id}): start_date must be a valid "
                f"YYYY-MM-DD date, got {start_text!r}")
        if not isinstance(end_text, str) or not _valid_calendar_date(
                end_text):
            raise ValidationAborted(
                f"{where} ({period_id}): end_date must be a valid "
                f"YYYY-MM-DD date, got {end_text!r}")
        if not isinstance(closed, bool):
            raise ValidationAborted(
                f"{where} ({period_id}): closed must be a boolean, "
                f"got {closed!r}")
        start_day = date.fromisoformat(start_text)
        end_day = date.fromisoformat(end_text)
        if end_day < start_day:
            raise ValidationAborted(
                f"{where} ({period_id}): end_date {end_text!r} precedes "
                f"start_date {start_text!r}")
        if any(row.period_id == period_id for row in rows):
            raise ValidationAborted(
                f"{where}: duplicate period_id {period_id!r}")
        if rows:
            previous = rows[-1]
            if start_day <= previous.start:
                raise ValidationAborted(
                    f"{where} ({period_id}): periods must be listed in "
                    f"ascending start-date order")
            if start_day <= previous.end:
                raise ValidationAborted(
                    f"{where} ({period_id}): period overlaps "
                    f"{previous.period_id!r}")
        rows.append(Period(period_id, start_text, end_text, closed,
                           start_day, end_day))
    return tuple(rows)


def covering_period(periods: tuple[Period, ...], day: date) -> Period | None:
    """Return the period containing ``day``, or ``None`` inside a gap.

    A simple ordered scan is sufficient (tables are short and the order
    is validated); it stays independent of hash iteration.
    """
    for period in periods:
        if day < period.start:
            return None
        if period.contains(day):
            return period
    return None

"""When a number is about: fiscal periods, by duration arithmetic (DESIGN.md §4.2c).

§4.2(c) is normative and its rule is counter-intuitive enough to restate: **annual versus
quarterly is decided by the fact's own duration, never by the containing filing's ``form``.** A
10-K carries quarterly facts, a 10-Q carries year-to-date ones, and a 52/53-week filer's "year"
is not 365 days. This module is the only place the day-count bands are written down.

``fy`` and ``fp`` are deliberately *not* on :class:`FiscalPeriod`. §4.2(a): they are the fiscal
year and period of the containing *filing*, not of the fact — a calendar-Q1-2025 period can
arrive tagged ``fy: 2026, fp: "Q1"`` because it was reported in a filing made in the issuer's
fiscal 2026. They are preserved on :class:`~investo.domain.models.RawFact` so a fixture can
demonstrate the trap, and there is no path by which a :class:`FiscalPeriod` can be constructed
from them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Final

__all__ = [
    "PeriodKind",
    "QUARTER_DAYS",
    "ANNUAL_DAYS",
    "FiscalPeriod",
    "classify",
    "window",
]


class PeriodKind(StrEnum):
    """What kind of period a fact covers.

    ``StrEnum`` so it orders and compares without a key function, which
    :class:`FiscalPeriod`'s ``order=True`` relies on.
    """

    INSTANT = "instant"
    """A balance-sheet fact: a point in time, no start date.

    Detected by the **absence of the ``start`` key**, not by a sentinel — ``companyfacts`` omits
    the key entirely on instant facts rather than writing ``null``, confirmed against a live
    payload (``docs/m1/04-parsers.md`` §2).
    """

    QUARTER = "quarter"
    """80-100 days inclusive."""

    ANNUAL = "annual"
    """350-380 days inclusive."""

    YTD = "ytd"
    """101-349 days. Year-to-date; differenced or dropped, which is M2's call."""

    OTHER = "other"
    """Under 80 or over 380 days.

    A named bucket rather than an exception. A parser that raises on an odd duration cannot
    ingest a 53-week filer at all, and refusing to *ingest* is not the same as refusing to
    *use*: M2 decides what to do with ``OTHER``, and it can only decide if M1 hands the fact
    over labelled. See spec question 6 in ``docs/m1/README.md``.
    """


QUARTER_DAYS: Final = range(80, 101)
"""80-100 days inclusive. DESIGN.md §4.2(c)."""

ANNUAL_DAYS: Final = range(350, 381)
"""350-380 days inclusive. DESIGN.md §4.2(c).

Narrower than SEC's own frames tolerance (365 +/- 30, i.e. 335-395; and 91 +/- 30 for
quarters). Deliberate: the narrow bands refuse an ambiguous duration rather than mislabelling
it, at the cost of routing more facts to :attr:`PeriodKind.OTHER`. Recorded as spec question 6
so M2 inherits an answer rather than a coincidence.
"""


@dataclass(frozen=True, slots=True, order=True)
class FiscalPeriod:
    """The period one fact covers.

    ``end`` is declared first so that ordering is chronological, which is what every caller
    means by sorting a series.

    **``start`` is excluded from comparison, and it has to be.** With ``order=True`` and
    ``start`` participating, sorting a list holding both an :attr:`PeriodKind.INSTANT` (start
    ``None``) and a duration ending the same day evaluates ``None < date(...)`` and raises
    ``TypeError``. That list is not hypothetical — it is what you get the first time a
    balance-sheet fact and an income-statement fact for the same period end land in one series,
    which is every filer, every quarter. So comparison is on ``(end, kind)``, ``start`` is
    ``compare=False``, and the crash cannot happen.

    The consequence is that two durations with the same ``end`` and ``kind`` compare equal. That
    is acceptable: they are the same period by §4.2's own grouping rule, and if they carry
    different values that is a restatement — M2's to resolve by ``filed`` date, not something a
    sort should be quietly breaking ties on.
    """

    end: date
    kind: PeriodKind
    start: date | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        """Enforce ``start is None`` if and only if the kind is ``INSTANT``.

        Both directions matter. An ``INSTANT`` with a start is a duration that lost its label; a
        duration without a start is a fact whose bucket was decided by something other than
        :func:`classify`, which is the only thing entitled to decide it.
        """
        if (self.start is None) != (self.kind is PeriodKind.INSTANT):
            raise ValueError(
                f"FiscalPeriod(kind={self.kind!r}, start={self.start!r}): start is None if and "
                "only if kind is INSTANT."
            )
        if self.start is not None and self.start > self.end:
            raise ValueError(f"FiscalPeriod start {self.start} is after end {self.end}.")

    @property
    def days(self) -> int | None:
        """Inclusive day count, or ``None`` for an instant.

        Inclusive — ``(end - start).days + 1`` — because that is the convention §4.2(c)'s bands
        are stated in: a calendar quarter of 90 days is 90 by this count, and an exclusive count
        would shift every boundary by one and put 350-day years in ``OTHER``.
        """
        if self.start is None:
            return None
        return (self.end - self.start).days + 1

    @classmethod
    def of(cls, start: date | None, end: date) -> FiscalPeriod:
        """Build a period, classifying it. The only constructor callers should use."""
        return cls(end=end, kind=classify(start, end), start=start)


def classify(start: date | None, end: date) -> PeriodKind:
    """Bucket a ``(start, end)`` pair by duration, per DESIGN.md §4.2(c).

    Total and exhaustive: every pair lands in exactly one kind, and :attr:`PeriodKind.OTHER` is
    a named bucket rather than an exception — see its docstring for why.

    Args:
        start: ``None`` for an instant fact, which is how ``companyfacts`` spells a
            balance-sheet value (the key is absent, so callers use ``row.get("start")``).
        end: The period end date.

    Returns:
        The kind. Boundary values 349/350/380/381 and 79/80/100/101 each get their own assertion
        in ``tests/test_periods.py`` — a ``>`` where ``>=`` belongs survives every test that
        only probes 90 and 365.
    """
    if start is None:
        return PeriodKind.INSTANT
    days = (end - start).days + 1
    if days in QUARTER_DAYS:
        return PeriodKind.QUARTER
    if days in ANNUAL_DAYS:
        return PeriodKind.ANNUAL
    if QUARTER_DAYS.stop <= days < ANNUAL_DAYS.start:
        return PeriodKind.YTD
    return PeriodKind.OTHER


def window(years: int, *, as_of: date) -> tuple[date, date]:
    """The estimation window for a lookback of ``years``, ending at ``as_of``.

    Returns ``(start, as_of)`` where ``start`` is ``as_of`` minus ``years`` calendar years,
    **floored to the first day of that month.**

    Floored because a window that starts mid-quarter includes a partial period whose inclusion
    depends on the day the command was run — so two runs a day apart would legitimately produce
    different reports, and DESIGN.md §11's determinism gate would eventually catch it as a bug
    that isn't one.

    Takes ``years`` as an argument rather than reading config, so M4 can ask for ``years + 1``
    without a second function: DESIGN.md §4.2 notes Beneish needs one year of history beyond the
    nominal lookback.

    Args:
        years: Whole years. ``config.parse_lookback`` turns ``"5y"`` into ``5`` and enforces the
            3-year minimum; this function does not re-check it, because a caller asking for
            ``years + 1`` is legitimately outside that rule's scope.
        as_of: The window's end — today, or ``--as-of``.

    Raises:
        ValueError: if ``years`` is not positive. A zero or negative window is a caller bug, not
            a configuration one.
    """
    if years <= 0:
        raise ValueError(f"window() needs a positive number of years, got {years}.")
    return date(as_of.year - years, as_of.month, 1), as_of

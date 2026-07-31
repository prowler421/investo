"""Fiscal periods: the duration bands, at their boundaries, and the ordering that must not crash.

DESIGN.md §4.2(c) is normative and counter-intuitive: annual versus quarterly is decided by the
fact's own duration, never by the containing filing's ``form``. A 10-K carries quarterly facts, a
10-Q carries year-to-date ones, and a 52/53-week filer's "year" is not 365 days.

Two things here are mandatory rather than thorough. The band boundaries each get their own
assertion, because a ``>`` where ``>=`` belongs survives every test that only probes 90 and 365. And
sorting a series that mixes an instant with a duration gets a test, because that list is what every
filer produces every quarter and the naive dataclass declaration raises ``TypeError`` on it.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date, timedelta

import pytest

from investo.domain.periods import (
    ANNUAL_DAYS,
    QUARTER_DAYS,
    FiscalPeriod,
    PeriodKind,
    classify,
    window,
)

END = date(2025, 12, 31)
"""One fixed period end for every duration case, so the only variable is the day count."""


def _span(days: int) -> tuple[date, date]:
    """A ``(start, end)`` pair covering exactly ``days`` days, counted inclusively.

    ``days - 1`` because the count §4.2(c)'s bands are stated in includes both endpoints. Getting
    this wrong in the helper would shift every boundary below by one and the whole table would agree
    with itself while testing the wrong numbers — so :func:`test_span_helper_is_inclusive` pins it.
    """
    return END - timedelta(days=days - 1), END


def test_span_helper_is_inclusive() -> None:
    """The boundary table is only meaningful if its inputs are the durations it claims.

    Asserted through ``FiscalPeriod.days``, which is the production definition of the count, rather
    than by re-deriving it here — the helper and the code under test have to agree on the convention
    or the boundary rows are testing arbitrary spans.
    """
    for days in (1, 80, 90, 365):
        start, end = _span(days)
        assert FiscalPeriod.of(start, end).days == days


# ---------------------------------------------------------------------------
# The bands, at their boundaries
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (1, PeriodKind.OTHER),
        (79, PeriodKind.OTHER),
        (80, PeriodKind.QUARTER),
        (100, PeriodKind.QUARTER),
        (101, PeriodKind.YTD),
        (349, PeriodKind.YTD),
        (350, PeriodKind.ANNUAL),
        (366, PeriodKind.ANNUAL),
        (380, PeriodKind.ANNUAL),
        (381, PeriodKind.OTHER),
    ],
    ids=[
        "1-day",
        "79-just-under-a-quarter",
        "80-first-quarter-day",
        "100-last-quarter-day",
        "101-first-ytd-day",
        "349-last-ytd-day",
        "350-first-annual-day",
        "366-leap-year",
        "380-last-annual-day",
        "381-past-annual",
    ],
)
def test_classify_at_the_band_boundaries(days: int, expected: PeriodKind) -> None:
    """DESIGN.md §4.2(c): 80-100 days is a quarter, 350-380 an annual, 101-349 year-to-date.

    Every row is one side of a boundary, and each is its own assertion because that is the only way
    an inclusive/exclusive mistake shows up. 79/80 and 380/381 catch an off-by-one at either end;
    100/101 and 349/350 catch a gap or an overlap between adjacent bands; 366 is the ordinary leap
    year that a 365-only test would let a narrow band reject.
    """
    start, end = _span(days)
    assert classify(start, end) is expected


@pytest.mark.spec
def test_classification_is_total_and_agrees_with_the_declared_bands() -> None:
    """§4.2(c): ``classify`` is total, and ``OTHER`` is a named bucket rather than an exception.

    The boundary table above states the answers; this states the rule, so the two have to agree. It
    also catches the failure the table cannot: a duration somewhere in 1..420 that raises, which
    would mean a 53-week filer cannot be ingested at all.
    """
    for days in range(1, 421):
        start, end = _span(days)
        kind = classify(start, end)
        if days in QUARTER_DAYS:
            assert kind is PeriodKind.QUARTER, days
        elif days in ANNUAL_DAYS:
            assert kind is PeriodKind.ANNUAL, days
        elif QUARTER_DAYS.stop <= days < ANNUAL_DAYS.start:
            assert kind is PeriodKind.YTD, days
        else:
            assert kind is PeriodKind.OTHER, days


@pytest.mark.spec
def test_the_declared_bands_are_the_ones_design_states() -> None:
    """The two ``range`` objects are the only place the day counts are written down.

    Pinned here so a change to them is a deliberate edit rather than a quiet re-banding: the
    ``range`` upper bounds are exclusive, and DESIGN.md's bands are inclusive of 100 and 380.
    """
    assert min(QUARTER_DAYS) == 80
    assert max(QUARTER_DAYS) == 100
    assert min(ANNUAL_DAYS) == 350
    assert max(ANNUAL_DAYS) == 380


@pytest.mark.spec
@pytest.mark.parametrize("end", [date(2025, 12, 31), date(2026, 3, 31), date(1999, 1, 1)])
def test_no_start_is_an_instant(end: date) -> None:
    """A balance-sheet fact has no start, and ``companyfacts`` spells that as an absent key.

    Detected by the absence, not by a sentinel: the payload omits ``start`` entirely rather than
    writing ``null``, so a parser that looked for a sentinel would classify every instant as a
    zero-length duration and route it to ``OTHER``.
    """
    assert classify(None, end) is PeriodKind.INSTANT


# ---------------------------------------------------------------------------
# FiscalPeriod's invariant
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_instant_with_a_start_is_rejected() -> None:
    """``start is None`` if and only if the kind is ``INSTANT`` — first direction.

    An ``INSTANT`` carrying a start is a duration that lost its label, and it would then be deduped
    against the balance-sheet facts for the same date. Nothing about the resulting number looks
    wrong, which is why the constructor refuses rather than trusting its caller.
    """
    with pytest.raises(ValueError, match="if and only if"):
        _ = FiscalPeriod(end=END, kind=PeriodKind.INSTANT, start=date(2025, 10, 1))


@pytest.mark.spec
@pytest.mark.parametrize(
    "kind", [PeriodKind.QUARTER, PeriodKind.ANNUAL, PeriodKind.YTD, PeriodKind.OTHER]
)
def test_duration_without_a_start_is_rejected(kind: PeriodKind) -> None:
    """The other direction, for every non-instant kind.

    A duration with no start is a fact whose bucket was decided by something other than
    :func:`classify` — which is the only thing entitled to decide it, because the alternative is
    somebody reading it off ``form`` or ``fp``.
    """
    with pytest.raises(ValueError, match="if and only if"):
        _ = FiscalPeriod(end=END, kind=kind, start=None)


def test_start_after_end_is_rejected() -> None:
    """A negative duration is a payload we do not understand, not a period.

    Without this the day count goes negative, lands in ``OTHER``, and the fact is quietly carried
    forward as an oddly-shaped period instead of being reported as a parse failure.
    """
    with pytest.raises(ValueError, match="after end"):
        _ = FiscalPeriod.of(END + timedelta(days=1), END)


@pytest.mark.spec
def test_days_is_inclusive() -> None:
    """§4.2(c)'s bands are stated in an inclusive day count, so ``days`` has to use one.

    An exclusive count is off by one everywhere and shifts every boundary: 350-day years would fall
    into ``YTD``, and the error would look like a data problem at a handful of filers rather than an
    arithmetic convention chosen wrongly in one place.
    """
    start, end = date(2025, 1, 1), date(2025, 3, 31)
    assert FiscalPeriod.of(start, end).days == (end - start).days + 1
    assert FiscalPeriod.of(end, end).days == 1, "one day is one day, not zero"


def test_days_is_none_for_an_instant() -> None:
    """``None`` rather than ``0``, because a balance-sheet fact has no duration at all.

    Zero would compare, sort and sum like a real number, and the first thing that averaged over it
    would silently be averaging over instants too.
    """
    assert FiscalPeriod(end=END, kind=PeriodKind.INSTANT).days is None


def test_of_classifies_rather_than_taking_the_kind_on_trust() -> None:
    """``FiscalPeriod.of`` is the constructor callers use, and it derives the kind.

    The two-step alternative — classify at the call site, then construct — is how a fact ends up
    labelled by whatever the caller believed instead of by its own dates.
    """
    start, end = _span(90)
    assert FiscalPeriod.of(start, end).kind is classify(start, end)
    assert FiscalPeriod.of(None, end).kind is PeriodKind.INSTANT


# ---------------------------------------------------------------------------
# Ordering — the reason `start` is compare=False
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_sorting_an_instant_beside_a_duration_does_not_raise() -> None:
    """**The important one.** ``start`` is ``compare=False`` and it has to be.

    With ``order=True`` and ``start`` participating, sorting a list holding both an ``INSTANT``
    (start ``None``) and a duration ending the same day evaluates ``None < date(...)`` and raises
    ``TypeError``. That list is not hypothetical — it is what you get the first time a balance-sheet
    fact and an income-statement fact for the same period end land in one series, which is every
    filer, every quarter.

    So this test is the guarantee, and it is written as "does not raise" deliberately: which of the
    two sorts first is not specified, and asserting an order here would pin a tie-break that
    ``docs/m1/01-domain-types.md`` deliberately leaves alone.
    """
    instant = FiscalPeriod(end=END, kind=PeriodKind.INSTANT)
    quarter = FiscalPeriod.of(*_span(90))
    ordered = sorted([quarter, instant])
    assert set(ordered) == {quarter, instant}


@pytest.mark.spec
def test_start_is_excluded_from_comparison() -> None:
    """The mechanism behind the test above, asserted directly.

    A refactor that re-declared ``start`` as an ordinary field would keep every other test in this
    file passing — the crash only appears in a list that mixes kinds — so the field's own
    ``compare`` flag is worth pinning.
    """
    start_field = next(field for field in fields(FiscalPeriod) if field.name == "start")
    assert start_field.compare is False


def test_sorting_is_chronological() -> None:
    """``end`` is declared first so that sorting a series means what every caller assumes.

    If ``kind`` came first, a series would sort into groups of annuals and quarters, and every chart
    and every growth rate computed off it would be wrong in a way that looks like bad data.
    """
    ends = [date(2024, 3, 31), date(2023, 12, 31), date(2025, 6, 30)]
    periods = [FiscalPeriod.of(end - timedelta(days=89), end) for end in ends]
    assert [period.end for period in sorted(periods)] == sorted(ends)


@pytest.mark.spec
def test_two_durations_with_the_same_end_and_kind_compare_equal() -> None:
    """The accepted consequence of ``compare=False``, asserted so it stays a decision.

    They are the same period by §4.2's own grouping rule. If they carry different values that is a
    restatement, which M2 resolves by ``filed`` date — not something a sort should be quietly
    breaking ties on. Hashing is asserted too, since a set is where the collapse actually happens.
    """
    ninety = FiscalPeriod.of(*_span(90))
    ninety_one = FiscalPeriod.of(*_span(91))
    assert ninety.start != ninety_one.start
    assert ninety == ninety_one
    assert len({ninety, ninety_one}) == 1


def test_different_kinds_with_one_end_are_not_equal() -> None:
    """The converse: ``compare=False`` on ``start`` must not collapse an annual into a quarter.

    Without ``kind`` in the comparison, a 10-K's annual figure and the Q4 ending the same day would
    be one key, and whichever arrived second would win.
    """
    quarter = FiscalPeriod.of(*_span(90))
    annual = FiscalPeriod.of(*_span(365))
    assert quarter.end == annual.end
    assert quarter != annual


# ---------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_window_returns_start_and_the_as_of_date() -> None:
    """``(start, as_of)``: the second element is the caller's own date, unmodified.

    Returning a computed end would give ``--as-of`` a second definition, and the two would disagree
    the first time somebody adjusted one of them.
    """
    as_of = date(2026, 7, 31)
    start, end = window(5, as_of=as_of)
    assert end == as_of
    assert start.year == as_of.year - 5
    assert start.month == as_of.month


@pytest.mark.spec
@pytest.mark.parametrize("day", [1, 2, 15, 28, 31])
def test_window_start_is_floored_to_the_first_of_the_month(day: int) -> None:
    """Floored, because otherwise the window depends on the day the command was run.

    A start mid-quarter includes a partial period, so two runs a day apart would legitimately
    produce different reports — and DESIGN.md §11's determinism gate would eventually catch that as
    a bug that isn't one. Every day of one month is asserted to produce the same start.
    """
    start, _ = window(5, as_of=date(2026, 7, day))
    assert start == date(2021, 7, 1)


@pytest.mark.spec
def test_window_does_not_break_on_a_leap_day() -> None:
    """The one input where "subtract years from the same day" raises.

    ``date(2023, 2, 29)`` does not exist, so an implementation that kept ``as_of.day`` would crash
    on ``--as-of 2024-02-29`` and on any run made on a leap day. Flooring to the first makes that
    impossible, and this is the test that says so.
    """
    start, end = window(1, as_of=date(2024, 2, 29))
    assert start == date(2023, 2, 1)
    assert end == date(2024, 2, 29)


@pytest.mark.spec
@pytest.mark.parametrize("years", [0, -1, -5])
def test_window_rejects_a_non_positive_lookback(years: int) -> None:
    """A zero or negative window is a caller bug, not a configuration one.

    ``parse_lookback`` already enforces the 3-year minimum for configured values, and this function
    deliberately does not re-check it — so zero is the only thing left to refuse, and refusing it is
    what stops an empty window from being reported as "no data available".
    """
    with pytest.raises(ValueError, match="positive"):
        _ = window(years, as_of=date(2026, 7, 31))


@pytest.mark.spec
def test_asking_for_one_more_year_shifts_the_start_by_exactly_a_year() -> None:
    """DESIGN.md §4.2: Beneish needs one year of history beyond the nominal lookback.

    ``window`` takes ``years`` as an argument rather than reading config precisely so M4 can ask for
    ``years + 1`` without a second function. Asserted as a relationship between the two windows
    rather than against two literals, because the property M4 depends on is the shift.
    """
    as_of = date(2026, 7, 31)
    five_start, five_end = window(5, as_of=as_of)
    six_start, six_end = window(6, as_of=as_of)
    assert six_end == five_end == as_of
    assert six_start == date(five_start.year - 1, five_start.month, five_start.day)


@pytest.mark.spec
def test_window_accepts_the_configured_minimum() -> None:
    """Three years is a legal lookback, so the smallest window a config can ask for has to work.

    ``window`` has no minimum of its own; this is the boundary between the two rules, and a
    ``years < 3`` check accidentally added here would make ``--lookback 3y`` fail at the boundary
    ``parse_lookback`` explicitly accepts.
    """
    start, _ = window(3, as_of=date(2026, 7, 31))
    assert start == date(2023, 7, 1)

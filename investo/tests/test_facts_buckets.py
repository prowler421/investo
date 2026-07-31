"""What happens to ``YTD`` and ``OTHER`` — the two questions M1 handed over labelled, not answered.

`domain/periods.classify` is total: every ``(start, end)`` lands in exactly one of ``INSTANT``,
``QUARTER``, ``ANNUAL``, ``YTD``, ``OTHER``. M1 deliberately declined to decide what to *do* with
the last two (`docs/m1/README.md` spec question 6) and `docs/m2/02-facts.md` §4 answers both:

- **``YTD`` is differenced where it recovers a quarter the filer never reported discretely, and
  dropped otherwise.** It is never carried into a series as-is: a 181-day figure sitting in a
  quarterly series is a doubled quarter, and a chart of it looks like a good half-year.
- **``OTHER`` is dropped, and counted.** Dropping is right; dropping *silently* is not, because a
  filer whose facts are 40% ``OTHER`` has had a fiscal-year change in the window, and that is a §6.4
  data-integrity finding rather than an ingestion detail.

And one thing that reads like a bug in the narrow bands and is not: **a 53-week year is 371 days,
and 371 is inside ``ANNUAL_DAYS``.** It is the first case a reader assumes §4.2(c)'s 350-380 band
breaks, so `STUBYEAR` carries one beside a 60-day stub and this module asserts both dispositions
side by side. The band boundaries themselves belong to `test_periods.py`; what is asserted here is
that *disposition* uses the same bands, so a second re-banding inside `normalize/facts.py` fails.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest

from investo.domain.models import Metric, RawFact
from investo.domain.periods import ANNUAL_DAYS, QUARTER_DAYS, FiscalPeriod, PeriodKind, classify
from investo.domain.provenance import Derivation, Provenance
from investo.normalize.facts import (
    Q4_RULE,
    YTD_RULE,
    MetricSeries,
    derive_q4,
    normalize_metric,
    residual,
)
from investo.normalize.tags import chain_for
from tests.conftest import M2_WINDOW, company_facts, raw_facts

YTDONLY = "YTDONLY.trimmed.json"
STUBYEAR = "STUBYEAR.trimmed.json"
REVENUE = chain_for(Metric.REVENUE)

STUB_END = date(2022, 3, 1)
"""The 60-day transition period after `STUBYEAR`'s fiscal-year change."""

FIFTY_THREE_WEEK_END = date(2023, 3, 7)
"""The end of the 371-day year that follows it, and the boundary case of this module."""

CUMULATIVE_YEAR_END = date(2023, 12, 31)
"""`YTDONLY`'s cumulative-only fiscal year: 3M, H1, 9M and FY, no discrete Q2 or Q3 anywhere."""

ANNUAL = "annual"
QUARTERLY = "quarterly"
DROPPED_LADDER_RUNG = "counted as a dropped ladder rung"
DROPPED_OTHER = "counted as an OTHER drop"
VANISHED = "vanished"
"""The failure: a fact in no series and in no counter. See :func:`_disposition`."""

_DISPOSITION = {
    PeriodKind.ANNUAL: ANNUAL,
    PeriodKind.QUARTER: QUARTERLY,
    PeriodKind.YTD: DROPPED_LADDER_RUNG,
    PeriodKind.OTHER: DROPPED_OTHER,
}
"""The rule, written once: what each duration band does to a fact. The parametrized boundary test
below supplies the durations and reads the expected disposition out of ``classify``, so the two
cannot disagree."""


def _series(fixture: str) -> MetricSeries:
    """The whole per-metric pipeline over the shared window, with no ``as_of`` cut.

    ``as_of=None`` because nothing in this module is about the point-in-time rule, and a cut date
    here would make a bucketing failure look like an ``as_of`` failure.
    """
    return normalize_metric(REVENUE, company_facts(fixture).facts, window=M2_WINDOW, as_of=None)


def _raw(fixture: str) -> tuple[RawFact, ...]:
    return raw_facts(fixture, *REVENUE.keys)


def _of_kind(fixture: str, kind: PeriodKind) -> tuple[RawFact, ...]:
    return tuple(fact for fact in _raw(fixture) if fact.period.kind is kind)


def _rule(source: Provenance) -> str | None:
    """The derivation rule behind a fact, or ``None`` for one that was filed as it stands."""
    return source.rule if isinstance(source, Derivation) else None


def _disposition(series: MetricSeries) -> str:
    """Where a single-fact payload's one fact ended up. :data:`VANISHED` is the failure.

    A fact in neither series and in no counter is one the coverage report cannot mention, and an
    unmentioned drop is exactly what `docs/m2/02-facts.md` §4 refuses — so the sentinel is named
    rather than being an assertion failure with no explanation attached.
    """
    if series.annual.facts:
        return ANNUAL
    if series.quarterly.facts:
        return QUARTERLY
    if series.dropped_ytd_redundant or series.dropped_ytd_unusable:
        return DROPPED_LADDER_RUNG
    if series.dropped_other:
        return DROPPED_OTHER
    return VANISHED


def _single_fact_payload(fact: RawFact) -> dict[tuple[str, str], tuple[RawFact, ...]]:
    return {(fact.taxonomy, fact.tag): (fact,)}


# ---------------------------------------------------------------------------
# YTD — docs/m2/02-facts.md §4 and §7
# ---------------------------------------------------------------------------
def test_the_ytdonly_fixture_actually_contains_ytd_facts() -> None:
    """Pins the trap: a differencing implementation that never fires passes an empty fixture.

    `docs/m2/05-testing.md` §2 records that no payload in the M1 set contained a ``YTD`` fact at
    all, which made §7 unfalsifiable. This is the assertion that the gap was closed rather than
    declared closed.
    """
    kinds = {fact.period.kind for fact in _raw(YTDONLY)}

    assert PeriodKind.YTD in kinds
    assert len(_of_kind(YTDONLY, PeriodKind.YTD)) == 3


@pytest.mark.spec
def test_no_ytd_period_in_quarterly() -> None:
    """A ``YTD`` fact cannot enter a quarterly series as-is, and nor can it enter the annual one.

    Three assertions, because "no ``YTD`` kind present" is satisfiable by an implementation that
    relabelled a 181-day fact as a ``QUARTER`` on the way in. So the second is on the *durations*:
    every fact in the quarterly series has a day count inside §4.2(c)'s quarterly band. The third
    checks the same thing from the payload's side — no cumulative period the fixture carries appears
    verbatim, matched on ``(start, end)`` rather than on the period object, since a derived Q2
    shares the H1 fact's ``end`` and that is the whole point of differencing.
    """
    series = _series(YTDONLY)
    quarterly = series.quarterly.facts
    ytd = _of_kind(YTDONLY, PeriodKind.YTD)

    assert {fact.period.kind for fact in quarterly} == {PeriodKind.QUARTER}
    assert all(fact.period.days in QUARTER_DAYS for fact in quarterly)
    assert {fact.period.kind for fact in series.annual.facts} == {PeriodKind.ANNUAL}

    cumulative = {(fact.period.start, fact.period.end) for fact in ytd}
    carried = {(fact.period.start, fact.period.end) for fact in (*quarterly, *series.annual.facts)}
    assert cumulative & carried == set()


@pytest.mark.spec
def test_ytd_is_differenced_into_the_quarters_the_filer_never_reported() -> None:
    """§7: ``Q2 = H1 − Q1`` and ``Q3 = 9M − H1``, asserted as the subtraction, not as two numbers.

    The expected values are computed from the fixture's own cumulative rungs, so the test states the
    rule; a table of 110,000,000 and 120,000,000 would also pass for an implementation that
    subtracted the wrong pair — ``9M − Q1`` labelled Q3, say, which on a smoothly growing filer
    looks entirely plausible. The provenance assertion is the other half: each recovered quarter
    carries a ``Derivation`` naming §7's rule over **two** inputs, which is what §3.2 requires and
    what the appendix prints.
    """
    ladder = {
        fact.period.end: fact
        for fact in _raw(YTDONLY)
        if fact.period.start == date(2023, 1, 1) and fact.period.kind is not PeriodKind.ANNUAL
    }
    q1_end, h1_end, nine_month_end = sorted(ladder)
    series = _series(YTDONLY)
    recovered = {
        fact.period.end: fact
        for fact in series.quarterly.facts
        if _rule(fact.source) == YTD_RULE
    }

    assert set(recovered) == {h1_end, nine_month_end}
    assert recovered[h1_end].value == ladder[h1_end].value - ladder[q1_end].value
    assert recovered[nine_month_end].value == ladder[nine_month_end].value - ladder[h1_end].value
    assert series.quarterly.recovered == 2

    subtracted = {h1_end: q1_end, nine_month_end: h1_end}
    for end, quarter in recovered.items():
        assert isinstance(quarter.source, Derivation)
        assert len(quarter.source.inputs) == 2, "the whole, and the one rung subtracted from it"
        assert quarter.period.start == ladder[subtracted[end]].period.end + timedelta(days=1)


@pytest.mark.spec
def test_a_redundant_ytd_fact_is_dropped_and_counted() -> None:
    """§7: where the YTD fact and the discrete quarter both exist, the quarter wins — and it shows.

    `YTDONLY`'s 2024 half-year is filed both cumulatively and discretely, which is what most filers
    do. The discrete fact must be the one in the series, identified by its provenance being a bare
    ``SourceRef`` rather than a ``Derivation`` — a reconciliation, or a differencing that fired
    anyway, would put a computed number there, and the two differ by whatever the filer reclassified
    intra-period. The count is derived from the payload: one YTD fact whose period end a discrete
    quarter already covers.
    """
    raw = _raw(YTDONLY)
    filed_ends = {fact.period.end for fact in raw if fact.period.kind is PeriodKind.QUARTER}
    redundant = [
        fact
        for fact in raw
        if fact.period.kind is PeriodKind.YTD and fact.period.end in filed_ends
    ]
    assert len(redundant) == 1, "the fixture carries exactly one redundant cumulative fact"

    end = redundant[0].period.end
    series = _series(YTDONLY)
    at_that_end = [fact for fact in series.quarterly.facts if fact.period.end == end]

    assert series.dropped_ytd_redundant == len(redundant)
    assert len(at_that_end) == 1
    assert _rule(at_that_end[0].source) is None, "the as-filed quarter, not a difference"
    assert at_that_end[0].period.start != redundant[0].period.start


@pytest.mark.spec
def test_a_ytd_fact_is_not_counted_as_an_other_drop() -> None:
    """The two dispositions are separate counters, and conflating them loses the §6.4 signal.

    ``dropped_other`` is how a fiscal-year change announces itself in the coverage report. A filer
    presenting cumulatively has not changed its fiscal year, so `YTDONLY` must report zero —
    otherwise every cumulative filer looks like a transition and the finding stops meaning anything.
    """
    series = _series(YTDONLY)

    assert len(_of_kind(YTDONLY, PeriodKind.YTD)) == 3
    assert series.dropped_other == 0
    assert series.dropped_ytd_redundant == 1


@pytest.mark.spec
def test_q4_is_not_derived_from_a_derived_quarter() -> None:
    """`docs/m2/02-facts.md` §5: nothing is derived from a derived part, so the Q4 stays absent.

    The setup is the tempting one. `YTDONLY`'s 2023 fiscal year has an annual figure and three
    quarters — one as filed, two recovered from the cumulative ladder — so ``FY − (Q1+Q2+Q3)`` is
    arithmetically available and would produce a number. It must not: two levels of subtraction
    accumulate two rounding differences, compound any single mis-tagged input, and yield a figure
    tracing to eight accessions that no reader can check.

    The series-level assertions come first, and "no fact ends on the fiscal year end" is stated
    alongside the year and three quarters being *present*, since the absence alone would also hold
    if the whole 2023 series were missing. The last two attempt the violation directly, and they are
    the ones that survive a reordering of the pipeline: today ``derive_q4`` runs before YTD
    differencing and so is never handed a recovered quarter at all, which is enforcement by call
    order and would go quietly if the two steps were swapped. Handing them over on purpose asserts
    the guard — ``derive_q4`` refuses because a 275-day residual is not a quarter, and ``residual``
    refuses a recovered part outright.
    """
    series = _series(YTDONLY)
    quarterly = series.quarterly.facts
    inside_2023 = [fact for fact in quarterly if fact.period.end.year == 2023]
    year = next(fact for fact in series.annual.facts if fact.period.end == CUMULATIVE_YEAR_END)

    assert len(inside_2023) == 3
    assert sum(1 for fact in inside_2023 if _rule(fact.source) == YTD_RULE) == 2
    assert CUMULATIVE_YEAR_END not in {fact.period.end for fact in quarterly}
    assert Q4_RULE not in {_rule(fact.source) for fact in quarterly}

    assert derive_q4(year, quarterly) is None
    assert residual(year, inside_2023, rule=Q4_RULE) is None


# ---------------------------------------------------------------------------
# OTHER, and the 53-week year — docs/m2/02-facts.md §4
# ---------------------------------------------------------------------------
def test_the_stubyear_fixture_carries_one_stub_and_one_53_week_year() -> None:
    """Pins both sides of the boundary this module is about.

    The durations are asserted through ``FiscalPeriod.days`` — the production definition of the
    count — so the two tests below are measuring the spans they claim to. A fixture regenerated with
    a 140-day stub and a 365-day year would keep both of them passing while testing neither
    boundary.
    """
    by_end = {fact.period.end: fact for fact in _raw(STUBYEAR)}

    assert by_end[STUB_END].period.days == 60
    assert by_end[STUB_END].period.kind is PeriodKind.OTHER
    assert by_end[FIFTY_THREE_WEEK_END].period.days == 371
    assert by_end[FIFTY_THREE_WEEK_END].period.kind is PeriodKind.ANNUAL


@pytest.mark.spec
def test_stub_period_dropped_and_counted() -> None:
    """An ``OTHER`` period cannot enter any series, and its absence cannot be silent.

    Both halves are the guarantee. Dropped: the 60-day transition period appears in neither bucket,
    and no fact anywhere in the series carries the ``OTHER`` kind — the second is the stronger
    claim, since a bucketer that routed short durations into the quarterly series would satisfy the
    first for the annual one. Counted: ``dropped_other`` equals the number of ``OTHER`` facts the
    payload has, derived rather than written down, because the count is what turns a fiscal-year
    change into a §6.4 finding instead of a hole.
    """
    dropped = _of_kind(STUBYEAR, PeriodKind.OTHER)
    series = _series(STUBYEAR)
    carried = (*series.annual.facts, *series.quarterly.facts)

    assert {fact.period.end for fact in dropped} == {STUB_END}
    assert STUB_END not in {fact.period.end for fact in carried}
    assert all(fact.period.kind is not PeriodKind.OTHER for fact in carried)
    assert series.dropped_other == len(dropped)


@pytest.mark.spec
def test_371_day_year_is_annual() -> None:
    """The boundary, per CLAUDE.md: a 53-week year is 371 days and 371 is inside ``ANNUAL_DAYS``.

    This is the second place a literal value is the assertion. The number 371 is the point — it is
    the first thing a reader assumes the narrow 350-380 band breaks, and the failure it guards
    against is a "fix" that widens the band or special-cases 52/53-week filers. What must be true is
    that the fact is in the annual series *and* that it was not counted as a drop, since a fact both
    kept and counted would be double-reported.

    Membership is asserted on the value as well as on the period end: a fact routed to the right
    bucket carrying the wrong figure is the one shape a period-end assertion cannot see.
    """
    year = next(fact for fact in _raw(STUBYEAR) if fact.period.end == FIFTY_THREE_WEEK_END)
    series = _series(STUBYEAR)
    annual = {fact.period.end: fact for fact in series.annual.facts}

    assert year.period.days == 371
    assert 371 in ANNUAL_DAYS
    assert classify(year.period.start, year.period.end) is PeriodKind.ANNUAL
    assert FIFTY_THREE_WEEK_END in annual
    assert annual[FIFTY_THREE_WEEK_END].value == year.value
    assert series.dropped_other == 1, "the stub only — a 53-week year is not an OTHER drop"


@pytest.mark.spec
def test_disposition_ignores_the_form_and_the_fiscal_period_it_was_filed_under() -> None:
    """§4.2(c): the bucket comes from the fact's own duration, never from the containing filing.

    `STUBYEAR`'s stub and its 53-week year were both filed on a ``10-K`` with ``fp: "FY"``, so every
    signal outside the dates says "annual" for both. One is annual and one is dropped, which is only
    possible if the day count decided. A bucketer reading ``form`` — the natural thing to reach for,
    and what §4.2(c) opens by warning against — charts a two-month stub as a year of revenue.
    """
    by_end = {fact.period.end: fact for fact in _raw(STUBYEAR)}
    stub, year = by_end[STUB_END], by_end[FIFTY_THREE_WEEK_END]
    assert stub.source.form == year.source.form == "10-K"
    assert stub.filing_fp == year.filing_fp == "FY"

    series = _series(STUBYEAR)
    kept = {date(2021, 12, 31), FIFTY_THREE_WEEK_END}

    assert {fact.period.end for fact in series.annual.facts} == kept


@pytest.mark.spec
@pytest.mark.parametrize(
    "days",
    [79, 80, 100, 101, 349, 350, 380, 381],
    ids=["79d", "80d", "100d", "101d", "349d", "350d", "380d", "381d"],
)
def test_disposition_uses_the_same_bands_classify_does(days: int) -> None:
    """Each band boundary, at the series level rather than inside ``classify``.

    `test_periods.py` asserts what ``classify`` returns; this asserts that ``normalize_metric`` acts
    on that answer, so a second set of day counts written into `normalize/facts.py` fails here
    rather than agreeing with the first one until a filer straddles the difference. The expected
    disposition is read out of ``classify`` through :data:`_DISPOSITION`, which is why the rule
    appears once in this module and the parametrization supplies only the durations.

    :data:`VANISHED` is what this is really guarding: a fact in no bucket and in no counter is one
    the coverage report cannot mention, and every band has to land somewhere nameable — which is
    what the second assertion says, by insisting the dispositions account for the fact exactly once.

    A lone YTD fact is the case that made this worth parametrizing. It is not *redundant* — no
    discrete quarter displaced it — and it recovers nothing, because the rung before it is missing and
    guard 5 refuses a two-quarter residual. It lands in ``dropped_ytd_unusable``, a counter that exists
    because the first version of this test found it vanishing from all of them.
    """
    base = next(fact for fact in _raw(STUBYEAR) if fact.period.days == 365)
    end = date(2019, 12, 31) + timedelta(days=days)
    period = FiscalPeriod.of(end - timedelta(days=days - 1), end)
    twin = dataclasses.replace(base, period=period)
    assert twin.period.days == days, "the case is the duration it claims to be"

    series = normalize_metric(REVENUE, _single_fact_payload(twin), window=M2_WINDOW, as_of=None)

    assert _disposition(series) == _DISPOSITION[classify(period.start, period.end)]
    accounted = (
        len(series.annual.facts)
        + len(series.quarterly.facts)
        + series.dropped_other
        + series.dropped_ytd_redundant
        + series.dropped_ytd_unusable
    )
    assert accounted == 1, "exactly one disposition, and never zero"

"""Residual recovery: one rule, two names, five guards.

`docs/m2/02-facts.md` §§5-7 is the design; DESIGN.md §4.2(c) is normative. Q4 derivation and YTD
differencing are the *same* operation — subtract a set of shorter periods that tile the front of a
longer one, keep the remainder — so they are one function with two rule labels for provenance, and
almost every test here is a test of a guard rather than of an arithmetic.

That emphasis is the point. The arithmetic is one subtraction and it is right by inspection; each
guard exists because the naive version produces a **plausible** wrong number:

- no guard on the aggregation class → a Q4 EPS that is arithmetically well-formed and wrong whenever
  the share count moved, which is every year for most of the NASDAQ universe;
- no guard on the *presence* of a filed Q4 → five quarters in a year;
- no guard on the residual's own duration → a 180-day figure labelled Q4, for a filer missing its Q2;
- no guard against derived inputs → a figure tracing to eight accessions that no reader can check.

So the assertions are on which periods exist and where their provenance points, not on the values.
§4.2(c)'s own warning is that Q4 behaviour *"varies by issuer **and** by year within the same
issuer"*, and `NOQ4` carries both years for exactly that reason: a rule that always subtracts passes
any test written against FY2022 alone.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from decimal import Decimal

import pytest

from investo.domain.models import Fact, Metric
from investo.domain.periods import FiscalPeriod, PeriodKind
from investo.domain.provenance import Derivation
from investo.normalize.facts import (
    Q4_RULE,
    RECOVERY_RULES,
    SEAM_TOLERANCE,
    YTD_RULE,
    MetricSeries,
    derive_q4,
    normalize_metric,
    observed_calendar,
    recover_from_ytd,
    residual,
)
from investo.normalize.tags import CHAINS, GAAP, chain_for
from tests.conftest import M2_WINDOW, company_facts

NOQ4 = "NOQ4.trimmed.json"
YTDONLY = "YTDONLY.trimmed.json"
AAPL = "AAPL.trimmed.json"

FY2022 = (date(2022, 1, 1), date(2022, 12, 31))
FY2023 = (date(2023, 1, 1), date(2023, 12, 31))


def _series(fixture: str, metric: Metric = Metric.REVENUE) -> MetricSeries:
    """One metric through the pipeline, with the calendar `build_history` would supply."""
    facts = company_facts(fixture).facts
    keys = tuple({key for chain in CHAINS.values() for key in chain.keys})
    annual_ends, quarterly_ends = observed_calendar(facts, keys, window=M2_WINDOW, as_of=None)
    return normalize_metric(
        chain_for(metric),
        facts,
        window=M2_WINDOW,
        as_of=None,
        annual_ends=annual_ends,
        quarterly_ends=quarterly_ends,
    )


def _quarters_in(series: MetricSeries, year: int) -> tuple[Fact, ...]:
    return tuple(
        fact
        for fact in series.quarterly.facts
        if fact.period.kind is PeriodKind.QUARTER and fact.period.end.year == year
    )


def _fact(
    metric: Metric,
    start: date | None,
    end: date,
    value: str,
    *,
    unit: str = "USD",
    rule: str | None = None,
) -> Fact:
    """A `Fact` with real provenance, borrowed from a fixture so no ref is invented.

    Building the `SourceRef` by hand would work and would also be the one place in this suite where a
    provenance record is not something a parser produced — so the donor comes from `AAPL`, and the
    tests that assert on `refs()` are asserting over refs a payload actually carried.
    """
    donor = company_facts(AAPL).get(GAAP, "SalesRevenueNet")[0]
    source = (
        donor.source if rule is None else Derivation(rule=rule, inputs=(donor.source, donor.source))
    )
    return Fact(
        metric=metric,
        value=Decimal(value),
        period=FiscalPeriod.of(start, end),
        source=source,
        unit=unit,
    )


def _quarter(metric: Metric, start: date, end: date, value: str, **kwargs: object) -> Fact:
    return _fact(metric, start, end, value, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Q4: conditional, and the condition is the period end
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_noq4_fy2023_yields_four_quarters_not_five() -> None:
    """Q4 cannot be derived when a Q4 was filed.

    `NOQ4`'s FY2023 has all four quarters *and* an annual figure whose value is their sum, so a rule
    that always subtracts emits a fifth quarter for the same three months — and its value equals the
    filed one, so no value assertion anywhere in the suite would catch it. The count is what catches
    it, which is why this test asserts on the number of quarters rather than on any figure.
    """
    series = _series(NOQ4)
    quarters = _quarters_in(series, 2023)
    assert len(quarters) == 4
    assert len({fact.period.end for fact in quarters}) == 4
    assert all(not _is_recovered(fact) for fact in quarters)


@pytest.mark.spec
def test_noq4_fy2022_derives_its_missing_fourth_quarter() -> None:
    """The other half of the same fixture, and the other half of §4.2(c)'s warning.

    A rule that *never* subtracts loses 28% of FY2022's revenue and reports the remaining three
    quarters as the year. Asserted as the derivation — one recovered quarter, whose provenance names
    four accessions: the annual figure plus the three quarters subtracted from it — rather than as
    301,000,000, which is a number the fixture generator chose.
    """
    series = _series(NOQ4)
    quarters = _quarters_in(series, 2022)
    assert len(quarters) == 4
    derived = [fact for fact in quarters if _is_recovered(fact)]
    assert len(derived) == 1
    assert series.quarterly.recovered == 1

    q4 = derived[0]
    assert isinstance(q4.source, Derivation)
    assert q4.source.rule == Q4_RULE
    assert len(q4.source.refs()) == 4
    assert q4.period.end == date(2022, 12, 31)
    assert q4.period.kind is PeriodKind.QUARTER


@pytest.mark.spec
def test_the_derived_q4_equals_the_year_minus_the_three_filed_quarters() -> None:
    """The arithmetic, stated as the arithmetic rather than as its answer on this payload.

    An assertion that FY2022's Q4 is 301,000,000 passes under a rule that subtracts the wrong things
    and happens to agree at this input — CLAUDE.md's rule, and the reason FY2022's derived answer
    equalling FY2023's *filed* one is a property of the fixture rather than of the test.
    """
    series = _series(NOQ4)
    annual = next(f for f in series.annual.facts if f.period.end == date(2022, 12, 31))
    quarters = _quarters_in(series, 2022)
    filed = [f for f in quarters if not _is_recovered(f)]
    derived = next(f for f in quarters if _is_recovered(f))
    assert derived.value == annual.value - sum((f.value for f in filed), Decimal(0))


@pytest.mark.spec
def test_the_presence_test_is_the_period_end_not_a_count() -> None:
    """§4.2(a) forbids reading `fp`, and a count of three fires on a year missing its Q2.

    Attempted directly: three quarters that cover Q1, Q2 and Q4 of a year. A count-of-three rule
    derives a "Q4" spanning July to September that is really Q3, labelled wrongly and off by whatever
    Q3 actually was. The end-date test refuses, because a quarter already ends on the annual end.
    """
    year = _fact(Metric.REVENUE, *FY2022, "1000")
    quarters = [
        _quarter(Metric.REVENUE, date(2022, 1, 1), date(2022, 3, 31), "240"),
        _quarter(Metric.REVENUE, date(2022, 4, 1), date(2022, 6, 30), "258"),
        _quarter(Metric.REVENUE, date(2022, 10, 1), date(2022, 12, 31), "301"),
    ]
    assert derive_q4(year, quarters) is None


@pytest.mark.spec
def test_missing_q2_yields_no_q4() -> None:
    """Q4 cannot be derived from an incomplete quarter set — guard 5, the load-bearing one.

    With Q2 absent, the residual runs from the end of Q1 to the end of the year: ~275 days, which
    `classify` calls `YTD`. The naive version emits that as Q4 — a figure three times too large, in a
    quarterly series, on a filer whose other years look fine. Nothing about the number says it is
    wrong; only its duration does, which is why the guard is on the classification.
    """
    year = _fact(Metric.REVENUE, *FY2022, "1065")
    q1 = _quarter(Metric.REVENUE, date(2022, 1, 1), date(2022, 3, 31), "240")
    q3 = _quarter(Metric.REVENUE, date(2022, 7, 1), date(2022, 9, 30), "266")

    assert derive_q4(year, [q1, q3]) is None
    assert derive_q4(year, [q1]) is None
    assert residual(year, [q1], rule=Q4_RULE) is None


@pytest.mark.spec
def test_a_complete_quarter_set_with_a_three_day_seam_still_derives() -> None:
    """The boundary on the *permissive* side, which the strict version of this rule fails.

    Filers record period boundaries inconsistently at the day level: one filer's Q1 ends 2022-03-30
    and Q2 starts 2022-04-01. A zero-tolerance seam check refuses correct data — so the tolerance gets
    a test at its edge, in both directions, or a `<` where `<=` belongs survives everything.
    """
    year = _fact(Metric.REVENUE, *FY2022, "1000")
    quarters = [
        _quarter(Metric.REVENUE, date(2022, 1, 1), date(2022, 3, 28), "240"),
        _quarter(Metric.REVENUE, date(2022, 3, 31), date(2022, 6, 30), "258"),
        _quarter(Metric.REVENUE, date(2022, 7, 1), date(2022, 9, 30), "266"),
    ]
    derived = derive_q4(year, quarters)
    assert derived is not None
    assert derived.value == Decimal("236")


@pytest.mark.spec
def test_a_seam_gap_wider_than_the_tolerance_refuses() -> None:
    """The other side of the same boundary: a real missing period is not a rounding difference.

    Ten days between two "adjacent" quarters means a period is missing, and admitting it would fold
    that period's revenue into whichever quarter followed. `SEAM_TOLERANCE` is three days, so this is
    the assertion that stops it being widened to a month by someone chasing a coverage number.
    """
    year = _fact(Metric.REVENUE, *FY2022, "1000")
    quarters = [
        _quarter(Metric.REVENUE, date(2022, 1, 1), date(2022, 3, 31), "240"),
        _quarter(Metric.REVENUE, date(2022, 4, 11), date(2022, 6, 30), "258"),
        _quarter(Metric.REVENUE, date(2022, 7, 1), date(2022, 9, 30), "266"),
    ]
    assert SEAM_TOLERANCE == timedelta(days=3)
    assert derive_q4(year, quarters) is None


@pytest.mark.spec
def test_parts_must_start_where_the_whole_does() -> None:
    """Guard 3. Parts that tile the *back* of a period leave a residual at the front.

    Which the function would then label a quarter running from the last part's end to the year's end —
    a period that is not the residual of anything. Attempted with Q2-Q4 as the parts: the arithmetic
    is a perfectly good subtraction, and the answer is Q1 mislabelled as Q4.
    """
    year = _fact(Metric.REVENUE, *FY2022, "1065")
    parts = [
        _quarter(Metric.REVENUE, date(2022, 4, 1), date(2022, 6, 30), "258"),
        _quarter(Metric.REVENUE, date(2022, 7, 1), date(2022, 9, 30), "266"),
        _quarter(Metric.REVENUE, date(2022, 10, 1), date(2022, 12, 31), "301"),
    ]
    assert residual(year, parts, rule=Q4_RULE) is None


@pytest.mark.spec
def test_overlapping_parts_refuse() -> None:
    """Two parts covering the same weeks double-count, and the residual comes out too large.

    A filer that files both a discrete Q2 and a cumulative H1 gives exactly this pair, which is why
    the YTD ladder is grouped by start date rather than assembled from whatever quarters are to hand.
    """
    year = _fact(Metric.REVENUE, *FY2022, "1000")
    parts = [
        _quarter(Metric.REVENUE, date(2022, 1, 1), date(2022, 3, 31), "240"),
        _quarter(Metric.REVENUE, date(2022, 1, 1), date(2022, 6, 30), "498"),
    ]
    assert residual(year, parts, rule=Q4_RULE) is None


@pytest.mark.spec
def test_parts_in_a_different_unit_refuse() -> None:
    """Guard 2. Subtracting `EUR` from `USD` produces a number in no unit at all."""
    year = _fact(Metric.REVENUE, *FY2022, "1000")
    parts = [
        _quarter(Metric.REVENUE, date(2022, 1, 1), date(2022, 3, 31), "240"),
        _quarter(Metric.REVENUE, date(2022, 4, 1), date(2022, 6, 30), "258", unit="EUR"),
        _quarter(Metric.REVENUE, date(2022, 7, 1), date(2022, 9, 30), "266"),
    ]
    assert residual(year, parts, rule=Q4_RULE) is None


@pytest.mark.spec
def test_parts_of_a_different_metric_refuse() -> None:
    """Subtracting COGS quarters from a revenue year is a gross-profit calculation wearing a Q4 label.

    Not hypothetical: both series are `FLOW`, both are `USD`, and both tile the same year. The metric
    check is the only thing that separates them, and without it the resulting fact carries
    `Metric.REVENUE` and a value that is not revenue.
    """
    year = _fact(Metric.REVENUE, *FY2022, "1000")
    parts = [
        _quarter(Metric.COGS, date(2022, 1, 1), date(2022, 3, 31), "240"),
        _quarter(Metric.COGS, date(2022, 4, 1), date(2022, 6, 30), "258"),
        _quarter(Metric.COGS, date(2022, 7, 1), date(2022, 9, 30), "266"),
    ]
    assert residual(year, parts, rule=Q4_RULE) is None


@pytest.mark.spec
def test_no_parts_at_all_refuses() -> None:
    """An empty part list would make the residual the whole period, re-emitted as a quarter."""
    year = _fact(Metric.REVENUE, *FY2022, "1000")
    assert residual(year, [], rule=Q4_RULE) is None
    assert derive_q4(year, []) is None


# ---------------------------------------------------------------------------
# the aggregation class
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_eps_q4_is_not_derived() -> None:
    """A `PER_SHARE` metric cannot be derived by subtraction — the guard that would look right.

    `Q4 EPS = FY EPS − (Q1+Q2+Q3 EPS)` is arithmetically well-formed and close enough to plausible
    that no eyeball catches it. It is wrong whenever the share count moved during the year, which is
    every year for any company with a buyback or an equity comp program, and the error is largest for
    exactly the fast-diluting companies whose EPS matters most.
    """
    year = _fact(Metric.EPS_DILUTED, *FY2022, "4.00", unit="USD/shares")
    quarters = [
        _quarter(
            Metric.EPS_DILUTED, date(2022, 1, 1), date(2022, 3, 31), "0.90", unit="USD/shares"
        ),
        _quarter(
            Metric.EPS_DILUTED, date(2022, 4, 1), date(2022, 6, 30), "0.95", unit="USD/shares"
        ),
        _quarter(
            Metric.EPS_DILUTED, date(2022, 7, 1), date(2022, 9, 30), "1.00", unit="USD/shares"
        ),
    ]
    assert derive_q4(year, quarters) is None
    assert residual(year, quarters, rule=Q4_RULE) is None


@pytest.mark.spec
def test_diluted_shares_q4_is_not_derived() -> None:
    """A weighted-average share count is not the sum of the quarterly ones.

    So `SHARES_DILUTED_WEIGHTED` is `FLOW` — it has a duration and buckets like a flow — with
    `subtractable=False`. One enum answering both questions would force this metric to lie about one
    of them, and this is the assertion that says which lie was refused.
    """
    year = _fact(Metric.SHARES_DILUTED_WEIGHTED, *FY2022, "1000", unit="shares")
    quarters = [
        _quarter(
            Metric.SHARES_DILUTED_WEIGHTED,
            date(2022, 1, 1),
            date(2022, 3, 31),
            "990",
            unit="shares",
        ),
        _quarter(
            Metric.SHARES_DILUTED_WEIGHTED,
            date(2022, 4, 1),
            date(2022, 6, 30),
            "995",
            unit="shares",
        ),
        _quarter(
            Metric.SHARES_DILUTED_WEIGHTED,
            date(2022, 7, 1),
            date(2022, 9, 30),
            "1005",
            unit="shares",
        ),
    ]
    assert chain_for(Metric.SHARES_DILUTED_WEIGHTED).subtractable is False
    assert derive_q4(year, quarters) is None


@pytest.mark.spec
def test_assets_are_never_subtracted() -> None:
    """An `INSTANT` metric cannot be differenced: the year-end balance **is** the Q4 balance.

    Subtracting three quarterly balances from an annual one produces a number with no meaning — not a
    wrong balance, a quantity that does not exist. Attempted with durations attached, because an
    instant has no start and the guard has to fire on the aggregation class rather than on the shape.
    """
    year = _fact(Metric.ASSETS, *FY2022, "5000")
    quarters = [
        _quarter(Metric.ASSETS, date(2022, 1, 1), date(2022, 3, 31), "4800"),
        _quarter(Metric.ASSETS, date(2022, 4, 1), date(2022, 6, 30), "4900"),
        _quarter(Metric.ASSETS, date(2022, 7, 1), date(2022, 9, 30), "4950"),
    ]
    assert derive_q4(year, quarters) is None


# ---------------------------------------------------------------------------
# nothing is derived from a derived part
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_derived_q4_is_not_a_part() -> None:
    """A recovered quarter cannot become an input to a second subtraction.

    Two levels accumulate two rounding differences and compound any single mis-tagged input, and the
    resulting figure traces to eight accessions in a way no reader can check. Attempted at the
    function rather than through the pipeline: the pipeline happens to enforce this by *call order*,
    which is a property of the caller, so a test that only ran the pipeline would keep passing if the
    guards were deleted.
    """
    year = _fact(Metric.REVENUE, *FY2023, "1000")
    recovered = _quarter(Metric.REVENUE, date(2023, 1, 1), date(2023, 3, 31), "240", rule=Q4_RULE)
    later = [
        recovered,
        _quarter(Metric.REVENUE, date(2023, 4, 1), date(2023, 6, 30), "258"),
        _quarter(Metric.REVENUE, date(2023, 7, 1), date(2023, 9, 30), "266"),
    ]
    assert derive_q4(year, later) is None
    assert residual(year, later, rule=Q4_RULE) is None
    assert YTD_RULE in RECOVERY_RULES and Q4_RULE in RECOVERY_RULES


@pytest.mark.spec
def test_a_derived_whole_is_refused_too() -> None:
    """The same rule from the other side, and the one a caller reaches by accident.

    A recovered quarter passed as the *whole* — a plausible mistake once quarterly and YTD facts share
    a series — would produce a sub-quarter residual that `classify` rejects anyway. The explicit guard
    means the refusal does not depend on that coincidence holding for every duration.
    """
    whole = _quarter(Metric.REVENUE, date(2023, 1, 1), date(2023, 12, 31), "1000", rule=YTD_RULE)
    parts = [_quarter(Metric.REVENUE, date(2023, 1, 1), date(2023, 9, 30), "700")]
    assert residual(whole, parts, rule=Q4_RULE) is None


@pytest.mark.spec
def test_ytdonly_q4_stays_absent_because_its_quarters_were_derived() -> None:
    """The two-level rule, end to end, and the result is a **hole rather than a wrong number**.

    `YTDONLY`'s CY2023 files 3M, H1, 9M and FY. Q2 and Q3 are recovered by differencing, so the only
    as-filed quarter available to subtract from the year is Q1 — and that residual spans 275 days,
    which is not a quarter. A reader gets three quarters and a `q4_absent` finding, which is exactly
    the trade §6.10 states: a blank space with an explanation beats a confident wrong number.
    """
    series = _series(YTDONLY)
    quarters = _quarters_in(series, 2023)
    assert {fact.period.end for fact in quarters} == {
        date(2023, 3, 31),
        date(2023, 6, 30),
        date(2023, 9, 30),
    }
    assert not any(fact.period.end == date(2023, 12, 31) for fact in quarters)


# ---------------------------------------------------------------------------
# YTD differencing
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_ytd_differencing_recovers_the_quarters_the_filer_never_reported() -> None:
    """`Q2 = H1 − Q1` and `Q3 = 9M − H1`, each over two refs.

    For a filer presenting cumulatively only, the discrete quarters exist nowhere and the series is
    empty without this. Asserted as the subtraction — each recovered value equals the difference of
    the two cumulative facts behind it — because the fixture's numbers are the generator's and the
    subtraction is the rule.
    """
    series = _series(YTDONLY)
    recovered = {f.period.end: f for f in series.quarterly.facts if _is_recovered(f)}
    assert set(recovered) == {date(2023, 6, 30), date(2023, 9, 30)}

    cumulative = {
        fact.period.end: fact.value
        for fact in company_facts(YTDONLY).get(GAAP, "Revenues")
        if fact.period.start == date(2023, 1, 1)
    }
    assert recovered[date(2023, 6, 30)].value == (
        cumulative[date(2023, 6, 30)] - cumulative[date(2023, 3, 31)]
    )
    assert recovered[date(2023, 9, 30)].value == (
        cumulative[date(2023, 9, 30)] - cumulative[date(2023, 6, 30)]
    )
    for fact in recovered.values():
        assert isinstance(fact.source, Derivation)
        assert fact.source.rule == YTD_RULE
        assert len(fact.source.refs()) == 2
        assert fact.period.kind is PeriodKind.QUARTER


@pytest.mark.spec
def test_a_discrete_quarter_beats_a_redundant_ytd_fact() -> None:
    """Where both exist the discrete quarter wins and the YTD fact is **dropped, not reconciled.**

    A reconciliation that flags a mismatch sounds better than it is: small differences between a
    filer's discrete and cumulative figures are usually intra-period reclassifications, they are
    routine, and a flag that fires on most filers is not a flag. `YTDONLY`'s CY2024 files Q1, H1 and a
    discrete Q2, so the H1 fact is redundant — counted, so the population is visible, and not
    reconciled against anything.
    """
    series = _series(YTDONLY)
    q2 = next(f for f in series.quarterly.facts if f.period.end == date(2024, 6, 30))
    assert not _is_recovered(q2), "the filed quarter won"
    assert series.dropped_ytd_redundant == 1
    assert series.dropped_ytd_unusable == 0


@pytest.mark.spec
def test_a_ladder_with_a_hole_recovers_nothing() -> None:
    """A filer that files YTD at Q1 and then nothing until the 10-K produces no differenced quarters.

    Rather than a 270-day figure labelled Q3 — guard 5 again, reached from the YTD side. The count
    lands in `dropped_ytd_unusable` rather than in the redundancy count, because the two are different
    facts about the filer and a fact in no counter at all is one the coverage report cannot mention.
    """
    q1 = _quarter(Metric.REVENUE, date(2023, 1, 1), date(2023, 3, 31), "100")
    nine_months = _fact(Metric.REVENUE, date(2023, 1, 1), date(2023, 9, 30), "330")
    assert nine_months.period.kind is PeriodKind.YTD

    recovered, redundant, unusable = recover_from_ytd([q1], [nine_months])
    assert recovered == ()
    assert redundant == 0
    assert unusable == 1


@pytest.mark.spec
def test_a_ytd_fact_never_enters_the_series_as_it_stands() -> None:
    """A 180-day figure in a quarterly series is a doubled quarter, and a chart of it looks like growth.

    So the YTD bucket is a source for differencing and never a series in its own right. Asserted over
    every metric and both buckets on the one fixture that has YTD facts at all — a narrower assertion
    would pass on any payload that happens to have none.
    """
    series = _series(YTDONLY)
    for part in (series.annual, series.quarterly):
        assert all(fact.period.kind is not PeriodKind.YTD for fact in part.facts)


@pytest.mark.spec
def test_the_ladder_is_grouped_by_its_start_date() -> None:
    """Rungs of one cumulative ladder share the fiscal year's first day; discrete quarters do not.

    Which is what stops a discrete Q2 (April-June) being differenced against an H1 (January-June):
    they are not rungs of the same ladder, and treating them as consecutive would produce a "quarter"
    equal to Q1 with Q2's label. Attempted directly, since the pipeline's grouping hides it.
    """
    q1 = _quarter(Metric.REVENUE, date(2024, 1, 1), date(2024, 3, 31), "118")
    q2_discrete = _quarter(Metric.REVENUE, date(2024, 4, 1), date(2024, 6, 30), "127")
    h1 = _fact(Metric.REVENUE, date(2024, 1, 1), date(2024, 6, 30), "245")

    recovered, redundant, _ = recover_from_ytd([q1, q2_discrete], [h1])
    assert recovered == (), "H1 is redundant, and Q2 was never a rung of its ladder"
    assert redundant == 1


@pytest.mark.spec
def test_recovery_is_reported_in_the_bucket_it_landed_in() -> None:
    """`recovered` counts both rules, because both produce quarters and neither produces a year.

    The annual bucket's count must stay zero: nothing in M2 derives an annual figure by subtraction,
    and a non-zero count there would mean a year had been assembled from quarters — which §4.2(c) does
    not license and which no filer's fiscal calendar guarantees.
    """
    for fixture in (NOQ4, YTDONLY):
        series = _series(fixture)
        assert series.annual.recovered == 0
        assert series.quarterly.recovered == sum(
            1 for fact in series.quarterly.facts if _is_recovered(fact)
        )


# ---------------------------------------------------------------------------
# the recovered fact's own shape
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_a_recovered_fact_starts_the_day_after_the_last_part_ends() -> None:
    """No gap and no overlap at the seam the derivation itself creates.

    Starting on the part's end date would double-count one day; starting two days later would leave
    one uncovered. Neither shows up in the value — the subtraction is the same either way — so the
    assertion has to be on the period.
    """
    series = _series(NOQ4)
    derived = next(f for f in _quarters_in(series, 2022) if _is_recovered(f))
    q3 = max(
        (f for f in _quarters_in(series, 2022) if not _is_recovered(f)),
        key=lambda f: f.period.end,
    )
    assert derived.period.start == q3.period.end + timedelta(days=1)
    assert derived.period.end == date(2022, 12, 31)


@pytest.mark.spec
def test_a_recovered_fact_carries_the_whole_first_in_its_inputs() -> None:
    """Input order is `(whole, *parts)`, which is what makes `refs()[0]` the annual filing.

    §9.1's appendix cites filings in the order the arithmetic used them, so a reader can follow
    "FY minus these three" without reconstructing which ref was the minuend.
    """
    series = _series(NOQ4)
    annual = next(f for f in series.annual.facts if f.period.end == date(2022, 12, 31))
    derived = next(f for f in _quarters_in(series, 2022) if _is_recovered(f))
    assert isinstance(derived.source, Derivation)
    assert derived.source.inputs[0] == annual.source


def test_a_recovered_fact_is_indistinguishable_from_a_filed_one_except_by_provenance() -> None:
    """Which is the point: downstream code reads a `Fact`, and there is one kind of `Fact`.

    M3's chart and M4's ratios must not need to know that a quarter was recovered — the coverage
    report and the finding are where that shows up. So the type carries no flag, and this test says so
    by checking the field set rather than by trusting it.
    """
    series = _series(NOQ4)
    derived = next(f for f in _quarters_in(series, 2022) if _is_recovered(f))
    assert {field.name for field in dataclasses.fields(derived)} == {
        "metric",
        "value",
        "period",
        "source",
        "unit",
    }


def _is_recovered(fact: Fact) -> bool:
    return isinstance(fact.source, Derivation) and fact.source.rule in RECOVERY_RULES

"""Coverage arithmetic: the denominator, the optional fill rate, and the two tier aggregates.

`docs/m2/03-statements.md` §§2-3 is normative. Coverage is the number DESIGN.md §4.2 feeds straight
into §9.2's confidence rating, so *"a coverage figure with an unstated denominator is a confidence
rating with an unstated meaning"* — and every wrong version of this arithmetic produces a percentage.

Three decisions are asserted here, each with a plausible alternative that reports a number nobody can
interpret:

**`fill_rate` is `None` when nothing was expected — not `0` and not `1`.** Both defaults are lies in
opposite directions and both propagate into a weighted mean. `0` reports a recent registrant that has
filed one 10-Q and no 10-K as having failed to be tagged; `1` reports a filer with no filings at all
as perfectly covered. `None` is what makes excluding it the only thing a caller can do.

**The denominator is the spine, so it is the same for every metric in a bucket.** That is the property
that stops a tagging failure from shrinking its own denominator — the failure mode of the "periods for
which any metric has a fact" reading, where a filer that tags nothing reports 100% of nothing.

**Both tiers are measured separately.** ROADMAP M2's exit criterion is stated on both metric sets, and
a single aggregate hides a tier-2 failure behind tier-1 success — which is ROADMAP's *"building only
the first tier means M4 stalls"*, arriving one milestone later disguised as a passing gate. The test
for it asserts that the combined figure sits *between* the two, so it reports neither.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest

from investo.domain.models import Metric
from investo.normalize.statements import (
    Bucket,
    CoverageReport,
    Finding,
    MetricCoverage,
    PeriodSpine,
)
from investo.normalize.tags import CHAINS, Tier, metrics_in_tier
from tests.conftest import filing_rows, history, submissions

FIXTURES_WITH_A_HISTORY: Final = [
    "AAPL.trimmed.json",
    "ARXS.json",
    "BADUNIT.trimmed.json",
    "BANK.trimmed.json",
    "IPO.trimmed.json",
    "NCI.trimmed.json",
    "NOQ4.trimmed.json",
    "REIT.trimmed.json",
    "RESTATER.trimmed.json",
    "STUBYEAR.trimmed.json",
    "TIER2.trimmed.json",
    "YTDONLY.trimmed.json",
]
"""Every `companyfacts` fixture, for the invariants that must hold on all of them.

Listed rather than globbed: a fixture added without a thought about what it proves is the thing
`PROVENANCE.md` exists to prevent, and a glob would silently absorb one.
"""


def _pair_report(
    first: MetricCoverage, second: MetricCoverage, *, tier: Tier = Tier.DCF
) -> CoverageReport:
    """A two-metric annual report, for the aggregate cases no fixture produces.

    `tier_fill_rate`'s behaviour on a mixture of measurable and unmeasurable metrics is arithmetic
    over `MetricCoverage`, and building it from a payload would make the test depend on which metrics
    that payload happens to tag. Both metrics come from one tier so the aggregate under test is the
    one being constructed.
    """
    metrics = metrics_in_tier(tier)
    return CoverageReport(
        spine=PeriodSpine(),
        annual={metrics[0]: first, metrics[1]: second},
        quarterly={},
    )


# ---------------------------------------------------------------------------
# fill_rate
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_fill_rate_is_none_at_zero_expected() -> None:
    """§3: `None` when `expected == 0`, and the two wrong answers are both asserted against.

    `0` and `1` are not merely imprecise — each is the value that makes a *different* caller wrong.
    §9.2 averages fill rates over metrics: a `0` drags the confidence rating down for a company that
    has not failed at anything, and a `1` props it up for one that reported nothing. Both are
    numbers, so both survive every downstream check.

    The `is None` assertion is the one that matters, because it makes forgetting the case a type error
    at the call site rather than a plausible percentage in the report.
    """
    nothing_expected = MetricCoverage(metric=Metric.REVENUE, filled=0, expected=0)

    assert nothing_expected.fill_rate is None
    assert nothing_expected.fill_rate != Decimal(0)
    assert nothing_expected.fill_rate != Decimal(1)
    assert not isinstance(nothing_expected.fill_rate, Decimal)


@pytest.mark.spec
def test_fill_rate_is_filled_over_expected_as_an_exact_decimal() -> None:
    """The rule, not a value: `filled / expected` in `Decimal`, for a ratio that does not terminate.

    CLAUDE.md convention 8 is `Decimal` for money and the same argument reaches the fill rate, which
    *looks* like a float and which `json.dumps` would happily accept as one. One third is the case
    that distinguishes them: an exact `Decimal` division compares equal to `Decimal(1) / Decimal(3)`
    and a float round-trip does not.
    """
    for filled, expected in ((0, 4), (1, 3), (2, 4), (4, 4)):
        coverage = MetricCoverage(metric=Metric.REVENUE, filled=filled, expected=expected)
        assert coverage.fill_rate == Decimal(filled) / Decimal(expected)
        assert isinstance(coverage.fill_rate, Decimal)


@pytest.mark.spec
def test_a_metric_with_nothing_expected_is_excluded_from_the_tier_aggregate() -> None:
    """`ARXS` end to end: one 10-Q, no 10-K, so the annual aggregate is `None` rather than 0%.

    §2 calls this "the common case, not the edge". The tier figure is what ROADMAP M2's exit criterion
    reads, and reporting 0% for a recent registrant's annual metrics would make the measurement a
    function of company age — which is the first denominator §2 rejects, arriving through the
    aggregate instead of through the spine. The quarterly half is asserted too, because an
    implementation that returned `None` for everything would satisfy the annual half alone.
    """
    profile, filings = submissions("ARXS.json")
    built = history("ARXS.json", profile=profile, filings=filings)
    report = built.coverage

    assert {coverage.expected for coverage in report.annual.values()} == {0}
    assert all(coverage.fill_rate is None for coverage in report.annual.values())
    assert report.tier_fill_rate(Tier.DCF, Bucket.ANNUAL) is None
    assert report.tier_fill_rate(Tier.QUALITY, Bucket.ANNUAL) is None

    assert {coverage.expected for coverage in report.quarterly.values()} == {1}
    assert report.tier_fill_rate(Tier.DCF, Bucket.QUARTERLY) is not None


# ---------------------------------------------------------------------------
# the denominator
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize("fixture", FIXTURES_WITH_A_HISTORY)
def test_the_denominator_is_the_spine_and_is_the_same_for_every_metric(fixture: str) -> None:
    """§2: `expected` is the spine's size, so it cannot depend on what was tagged.

    This is the whole argument for choosing the filing history over "periods for which any metric has
    a fact": the second is circular, and a metric that resolved nothing would report a denominator of
    zero and a `None` fill rate indistinguishable from `ARXS`'s. Asserted as an identity against
    `spine.ends_for` across every fixture, because a per-metric denominator is the kind of change that
    would raise every coverage figure and break no other test.
    """
    report = history(fixture).coverage

    for bucket in Bucket:
        expected = len(report.spine.ends_for(bucket))
        assert {coverage.expected for coverage in report.for_bucket(bucket).values()} == {expected}


@pytest.mark.spec
@pytest.mark.parametrize("fixture", FIXTURES_WITH_A_HISTORY)
def test_filled_never_exceeds_expected(fixture: str) -> None:
    """§2: coverage is bounded at 100% **by construction**, over every fixture and both buckets.

    The mechanism is one-to-one matching plus counting out-of-spine periods separately, and
    `test_spine` asserts the mechanism. This asserts the consequence broadly, which is the assertion
    that would survive a rewrite of the matcher: a fill rate above 1 is a number no reader can act on,
    and it would appear first on a filer whose facts and filing header disagree — i.e. not on any
    fixture anyone was looking at.
    """
    report = history(fixture).coverage

    for bucket in Bucket:
        for coverage in report.for_bucket(bucket).values():
            assert coverage.filled <= coverage.expected, coverage.metric
            rate = coverage.fill_rate
            assert rate is None or Decimal(0) <= rate <= Decimal(1), coverage.metric


@pytest.mark.parametrize("fixture", FIXTURES_WITH_A_HISTORY)
def test_every_metric_has_a_coverage_entry_in_both_buckets(fixture: str) -> None:
    """An absence is a coverage fact, so it needs a row rather than a missing key.

    DESIGN.md §4.2's argument is that hardcoding one tag per metric produces sparse data silently.
    A report that omitted the metrics it found nothing for would reproduce that at the measurement
    layer — `facts` would print a shorter table and the tier mean would be taken over the metrics that
    worked.
    """
    report = history(fixture).coverage

    assert set(report.annual) == set(CHAINS)
    assert set(report.quarterly) == set(CHAINS)


# ---------------------------------------------------------------------------
# the tier aggregates
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_tiers_are_reported_separately() -> None:
    """ROADMAP M2: a tier-2 failure cannot hide behind tier-1 success.

    `AAPL.trimmed.json` is the shape the criterion has to survive — a filer with a populated DCF set
    and not one tier-2 tag. The discriminating assertion is not that the two figures differ but that
    the single combined mean sits strictly **between** them: it is high enough to look like partial
    coverage and reports neither the tier that works nor the tier that is empty. A reviewer reading
    one aggregate would see 20% and conclude "thin data", not "half the pipeline has no inputs".
    """
    profile, filings = submissions("AAPL.json")
    report = history("AAPL.trimmed.json", profile=profile, filings=filings).coverage

    dcf = report.tier_fill_rate(Tier.DCF, Bucket.ANNUAL)
    quality = report.tier_fill_rate(Tier.QUALITY, Bucket.ANNUAL)
    assert dcf is not None
    assert quality is not None
    assert quality == Decimal(0), "no tier-2 tag appears in this payload"
    assert dcf > quality

    rates = [
        coverage.fill_rate
        for coverage in report.annual.values()
        if coverage.fill_rate is not None
    ]
    combined = sum(rates, Decimal(0)) / Decimal(len(rates))
    assert quality < combined < dcf, "one aggregate over both tiers reports neither of them"


@pytest.mark.spec
def test_the_two_tiers_partition_the_metric_set() -> None:
    """The split is **declared** in the registry, and it has to be a partition to be an exit gate.

    An overlap would double-count a metric in one of the two means; a gap would leave a metric that no
    tier figure measures, so ROADMAP M2's criterion could be met while it was absent for every filer.
    Asserted against `CHAINS` rather than against a literal count, so adding a metric fails here until
    its tier is declared.
    """
    dcf = set(metrics_in_tier(Tier.DCF))
    quality = set(metrics_in_tier(Tier.QUALITY))

    assert dcf & quality == set()
    assert dcf | quality == set(CHAINS)
    assert dcf and quality
    assert len(metrics_in_tier(Tier.DCF)) == len(dcf), "registry order, no duplicates"


@pytest.mark.spec
def test_tier_fill_rate_excludes_an_unmeasurable_metric_rather_than_counting_it_zero() -> None:
    """§3: the mean is over the metrics that *have* a rate.

    The construction is the minimal one that separates the two implementations: one metric fully
    covered, one with nothing expected. The right answer is 1 and the wrong answer is 0.5 — a figure
    that looks like a real, moderate coverage level and that moves whenever a filer's filing mix
    changes rather than when its tagging does.
    """
    metrics = metrics_in_tier(Tier.DCF)
    report = _pair_report(
        MetricCoverage(metric=metrics[0], filled=1, expected=1),
        MetricCoverage(metric=metrics[1], filled=0, expected=0),
    )

    assert report.tier_fill_rate(Tier.DCF, Bucket.ANNUAL) == Decimal(1)


@pytest.mark.spec
def test_tier_fill_rate_is_unweighted() -> None:
    """§3: unweighted, because ROADMAP M2's criterion is stated over the **metric set**.

    Weighting by expected periods would let one metric with twenty expected periods set the tier
    figure, and the metric with twenty is whichever the filer files most often — so the aggregate
    would measure filing cadence. The two answers here are 0.5 and 1/21, which are not close, and only
    the unweighted one is a statement about the metric set.
    """
    metrics = metrics_in_tier(Tier.DCF)
    report = _pair_report(
        MetricCoverage(metric=metrics[0], filled=1, expected=1),
        MetricCoverage(metric=metrics[1], filled=0, expected=20),
    )

    assert report.tier_fill_rate(Tier.DCF, Bucket.ANNUAL) == Decimal(1) / Decimal(2)
    assert report.tier_fill_rate(Tier.DCF, Bucket.ANNUAL) != Decimal(1) / Decimal(21)


def test_tier_fill_rate_is_none_when_no_metric_in_the_tier_is_measurable() -> None:
    """`None` propagates from the metric to the tier, for the same reason it exists at all.

    A tier mean of `0` over an empty list of rates is the same lie one level up, and it is the one
    §9.2 would read: the confidence rating cannot distinguish "we measured this tier and it is empty"
    from "there was nothing to measure" unless the aggregate can be absent too.
    """
    empty = CoverageReport(spine=PeriodSpine(), annual={}, quarterly={})

    assert empty.tier_fill_rate(Tier.DCF, Bucket.ANNUAL) is None
    assert empty.tier_fill_rate(Tier.QUALITY, Bucket.QUARTERLY) is None


def test_a_tier_aggregate_reads_only_its_own_metrics() -> None:
    """The converse of the partition test: a tier-1 entry cannot contribute to the tier-2 figure.

    A `tier_fill_rate` that averaged the whole bucket would return the same number for both tiers, and
    every assertion in `test_tiers_are_reported_separately` would still pass on a payload where the
    tiers happened to be equally covered.
    """
    metrics = metrics_in_tier(Tier.DCF)
    report = _pair_report(
        MetricCoverage(metric=metrics[0], filled=1, expected=1),
        MetricCoverage(metric=metrics[1], filled=1, expected=1),
    )

    assert report.tier_fill_rate(Tier.DCF, Bucket.ANNUAL) == Decimal(1)
    assert report.tier_fill_rate(Tier.QUALITY, Bucket.ANNUAL) is None


# ---------------------------------------------------------------------------
# the per-metric counts
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_a_fully_derived_metric_does_not_report_zero_filled() -> None:
    """`filled` is measured over the series the report prints, derivations included.

    `NCI.trimmed.json` never tags `Liabilities`, so every period of that series comes from the
    cross-metric derivation. Measuring `filled` before derivation would print `filled=0` beside
    `derived_periods=2` — a coverage report contradicting the two numbers on the page above it, and a
    `coverage_below_floor` finding on a metric that is fully populated.
    """
    report = history("NCI.trimmed.json").coverage
    coverage = report.annual[Metric.LIABILITIES]

    assert coverage.derived_periods > 0
    assert coverage.filled == coverage.expected
    assert coverage.filled == coverage.derived_periods
    assert coverage.fill_rate == Decimal(1)


def test_derived_and_recovered_periods_are_counted_separately() -> None:
    """Two different provenance stories, two counters, and neither is `filled`.

    A derived gross profit and a recovered Q4 are both figures the filer did not tag, and §6.4 renders
    them as different flags — one is arithmetic across metrics, the other arithmetic across periods of
    one metric. Collapsing them into a single "computed" count would make `q4_derived` and the
    cross-metric derivations indistinguishable in the appendix.
    """
    tier2 = history("TIER2.trimmed.json").coverage.annual[Metric.GROSS_PROFIT]
    assert tier2.derived_periods > 0
    assert tier2.recovered_periods == 0
    assert tier2.derived_periods <= tier2.filled

    noq4 = history("NOQ4.trimmed.json").coverage.quarterly[Metric.REVENUE]
    assert noq4.recovered_periods > 0
    assert noq4.derived_periods == 0
    assert noq4.recovered_periods <= noq4.filled


@pytest.mark.spec
def test_metric_level_drops_are_reported_against_both_buckets() -> None:
    """A fact dropped for its unit has no bucket, and one dropped as `OTHER` landed in neither.

    Attributing them to one bucket would lose half of them, and no aggregate sums the counts — the
    tier figures are means over `fill_rate` — so reporting each against both cannot inflate anything.
    `BADUNIT` and `STUBYEAR` are the two shapes: a `EUR` revenue fact and a 60-day transition stub.
    """
    badunit = history("BADUNIT.trimmed.json").coverage
    for metric in (Metric.REVENUE, Metric.EPS_DILUTED):
        annual = badunit.annual[metric].dropped_unit_mismatch
        assert annual > 0
        assert badunit.quarterly[metric].dropped_unit_mismatch == annual

    stubyear = history("STUBYEAR.trimmed.json").coverage
    dropped = stubyear.annual[Metric.REVENUE].dropped_other_bucket
    assert dropped > 0
    assert stubyear.quarterly[Metric.REVENUE].dropped_other_bucket == dropped


@pytest.mark.spec
def test_tags_used_is_a_tuple_because_a_stitch_is_normal() -> None:
    """§3: a single "which tag won" field cannot represent a series that spans a standards boundary.

    `len(tags_used) > 1` *is* the stitch finding, so the field has to be able to hold two — and the
    order has to be first-use, because the appendix prints it as a progression and reversing it
    describes the ASC 606 transition backwards. Which tags those are is `test_tags`'s; that the
    coverage record keeps all of them is this one's.
    """
    report = history("AAPL.trimmed.json").coverage
    tags_used = report.annual[Metric.REVENUE].tags_used

    assert isinstance(tags_used, tuple)
    assert len(tags_used) > 1
    assert len(set(tags_used)) == len(tags_used), "first-use order, no repeats"
    assert all(":" in tag for tag in tags_used), "qualified, as the appendix prints them"


def test_a_metric_the_payload_never_tags_reports_no_tags_used() -> None:
    """The converse, so `tags_used` distinguishes "absent" from "one tag" in the appendix.

    `facts` prints this column, and an empty tuple is what makes the row read "absent" rather than
    naming a tag that contributed nothing — which is the difference between a coverage report and a
    coverage report that looks traced.
    """
    report = history("AAPL.trimmed.json").coverage

    assert report.annual[Metric.COGS].tags_used == ()
    assert report.annual[Metric.COGS].filled == 0


# ---------------------------------------------------------------------------
# the accessors
# ---------------------------------------------------------------------------
def test_for_bucket_returns_the_bucket_it_was_asked_for() -> None:
    """Two maps, one accessor, and they must not be the same object.

    `for_bucket` is what every consumer uses to avoid branching on the bucket, so a copy-paste that
    returned `annual` twice would report annual coverage as quarterly — and on most fixtures the two
    are similar enough that only a fixture with different filing counts in the two buckets would show
    it. `AAPL` is that fixture.
    """
    report = history("AAPL.trimmed.json").coverage

    assert report.for_bucket(Bucket.ANNUAL) is report.annual
    assert report.for_bucket(Bucket.QUARTERLY) is report.quarterly
    assert report.annual is not report.quarterly

    profile, filings = submissions("AAPL.json")
    with_spine = history("AAPL.trimmed.json", profile=profile, filings=filings).coverage
    annual = with_spine.for_bucket(Bucket.ANNUAL)[Metric.REVENUE]
    quarterly = with_spine.for_bucket(Bucket.QUARTERLY)[Metric.REVENUE]
    assert annual.expected != quarterly.expected


def test_findings_for_selects_by_code_and_is_empty_for_an_unknown_one() -> None:
    """The accessor `report.json` and `facts` both group by, asserted in both directions.

    An empty tuple for an unrecognised code rather than a `KeyError`: a consumer asking whether a
    finding fired is asking a question with a "no" answer, and every caller having to guard the lookup
    is how one of them stops asking.
    """
    report = CoverageReport(
        spine=PeriodSpine(),
        annual={},
        quarterly={},
        findings=(
            Finding(code="q4_derived", metric=Metric.REVENUE, detail="one"),
            Finding(code="restated", metric=Metric.REVENUE, detail="two"),
            Finding(code="q4_derived", metric=Metric.CAPEX, detail="three"),
        ),
    )

    assert [finding.detail for finding in report.findings_for("q4_derived")] == ["one", "three"]
    assert [finding.detail for finding in report.findings_for("restated")] == ["two"]
    assert report.findings_for("no_such_code") == ()


def test_ends_for_returns_the_spine_bucket_asked_for() -> None:
    """The same shape of mistake one layer down, where it would move every denominator.

    `expected` is `len(spine.ends_for(bucket))`, so an `ends_for` that returned the annual tuple for
    both buckets would give a filer's quarterly metrics an annual denominator — roughly four times too
    small, and the resulting fill rates would exceed 100% only sometimes.
    """
    spine = history(
        "TIER2.trimmed.json",
        filings=filing_rows(
            ("10-K", "2022-02-18", "2021-12-31"),
            ("10-Q", "2022-05-05", "2022-03-31"),
        ),
    ).coverage.spine

    assert spine.ends_for(Bucket.ANNUAL) is spine.annual_ends
    assert spine.ends_for(Bucket.QUARTERLY) is spine.quarterly_ends
    assert len(spine.ends_for(Bucket.QUARTERLY)) > len(spine.ends_for(Bucket.ANNUAL))
    assert not spine.is_empty

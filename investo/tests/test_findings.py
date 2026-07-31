"""One test per finding code, positive and negative — and the check that all eighteen are covered.

`docs/m2/03-statements.md` §4 is normative. A finding states something true about the data and takes
**no position on what it means**: §6.2 gives severity to `analyze/flags.py`'s rule registry, one rule
per file with its own test, and a severity assigned in `normalize/` is a severity assigned twice. The
two copies diverge on the first rule M4 tunes, so the absence of the field is asserted rather than
assumed.

Two things make this module more than a list of smoke tests.

**Every code is claimed by a named test.** §4's argument for the guarantee table — *"a guarantee absent
from this table is a guarantee that is not enforced"* — applies to the codes with equal force: a
finding nothing exercises is a `report.json` key that has never been produced, and the first time it
fires will be on a user's filer. `_TEST_FOR_CODE` is the claim and
`test_every_finding_code_is_exercised_by_a_test_in_this_module` checks it in both directions.

**Each code gets its negative half.** A finding that fires on every filer is not a finding, and a
finding that fires on none is dead code that reads as reassurance. Both failures are invisible to a
test that only asserts the positive case, so the fixture that must *not* produce the code is named
beside the one that must — usually a second fixture, and twice (`exclusivity_switch`,
`coverage_below_floor`) a second shape inside the same payload.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

import pytest

from investo.domain.models import Metric, RawFact
from investo.domain.periods import FiscalPeriod
from investo.domain.provenance import Accession, Derivation, SourceRef
from investo.ingest.edgar.companyfacts import CompanyFacts
from investo.normalize.statements import (
    FINDING_CODES,
    Bucket,
    FinancialHistory,
    Finding,
    build_history,
)
from tests.conftest import (
    FETCHED_AT,
    M2_WINDOW,
    company_facts,
    filing_rows,
    history,
    submissions,
)

GAAP: Final = "us-gaap"
CAPEX_TAG: Final = "PaymentsToAcquirePropertyPlantAndEquipment"

COMPANY_LEVEL_CODES: Final = frozenset(
    {"companyfacts_absent", "submissions_absent", "spine_observed", "window_truncated"}
)
"""The findings that describe the run rather than a metric, and therefore carry `metric=None`.

Listed here because `_findings` prints them first and a reader needs them first: a coverage figure
measured against an `OBSERVED` spine has to be qualified before any per-metric number below it means
anything.
"""

_TEST_FOR_CODE: Final[dict[str, str]] = {
    "coverage_below_floor": "test_coverage_below_floor_fires_only_strictly_under_the_floor",
    "q4_derived": "test_q4_derived_names_only_the_year_that_needed_it",
    "q4_absent": "test_q4_absent_fires_where_no_fourth_quarter_is_filed_or_derivable",
    "series_stitched": "test_series_stitched_fires_when_two_chain_members_contributed",
    "restated": "test_equal_values_across_four_accessions_raise_no_restated_finding",
    "window_truncated": "test_window_truncated_fires_a_whole_year_inside_the_window",
    "companyfacts_absent": "test_companyfacts_absent_fires_only_when_the_payload_is_absent",
    "submissions_absent": "test_submissions_absent_fires_only_when_the_profile_is_absent",
    "spine_observed": "test_spine_observed_fires_when_no_periodic_filing_is_in_the_window",
    "spine_date_inexact": "test_spine_date_inexact_fires_on_a_report_date_off_by_one",
    "exclusivity_switch": "test_exclusivity_switch_fires_on_a_partition_and_not_on_an_interleave",
    "net_income_scope_mismatch": "test_net_income_scope_mismatch_fires_on_the_including_nci_tag",
    "liabilities_nci_approximated": "test_liabilities_nci_approximated_fires_on_the_fallback",
    "sga_composed": "test_sga_composed_fires_when_the_two_components_were_summed",
    "sign_anomaly": "test_sign_anomaly_fires_on_a_fact_that_contradicts_its_convention",
    "unit_mismatch": "test_unit_mismatch_fires_and_names_the_unit_it_excluded",
    "other_bucket_drops": "test_other_bucket_drops_fires_on_a_transition_stub",
    "periods_outside_spine": "test_periods_outside_spine_fires_on_a_period_the_filings_omit",
}
"""Which test owns which code. Checked against `FINDING_CODES` in both directions below.

An explicit mapping rather than a name convention, because the convention would be satisfied by a test
that mentioned the code in its name and asserted nothing about it — and because a code whose test was
deleted should fail here rather than silently stop being covered.
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _codes(built: FinancialHistory) -> set[str]:
    return {finding.code for finding in built.coverage.findings}


def _capex_fact(value: str, *, year: int) -> RawFact:
    """One capex fact as filed, so the sign convention has something to contradict.

    Hand-built because **no fixture produces a sign anomaly**: `docs/m2/05-testing.md` §2's six new
    payloads do not include one, and the only negative values anywhere in
    `tests/fixtures/edgar/companyfacts/` are `TIER2`'s two `InterestIncomeExpenseNet` facts — which
    carry `flip_sign`, so they come out positive and *correctly* raise nothing. A negative capex
    quarter is real (a disposal netted against acquisitions), it is the case §8 says must be kept and
    counted, and until a fixture carries one the payload has to be assembled here.
    """
    return RawFact(
        taxonomy=GAAP,
        tag=CAPEX_TAG,
        unit="USD",
        value=Decimal(value),
        period=FiscalPeriod.of(date(year, 1, 1), date(year, 12, 31)),
        source=SourceRef(
            accession=Accession.parse("0000320193-19-000119"),
            taxonomy=GAAP,
            tag=CAPEX_TAG,
            form="10-K",
            filed=date(year + 1, 2, 16),
            url="https://data.sec.gov/test",
            fetched_at=FETCHED_AT,
        ),
        filing_fy=year,
        filing_fp="FY",
    )


def _with_capex(fixture: str, *facts: RawFact) -> CompanyFacts:
    """`fixture` plus a capex series, leaving everything else in the payload alone."""
    parsed = company_facts(fixture)
    key = (GAAP, CAPEX_TAG)
    return dataclasses.replace(
        parsed,
        facts={**parsed.facts, key: facts},
        tags_present=parsed.tags_present | {key},
    )


def _history_of(facts: CompanyFacts) -> FinancialHistory:
    return build_history(
        facts,
        ticker="TEST",
        cik=1000000,
        name="Test Corp",
        profile=None,
        window=M2_WINDOW,
    )


# ---------------------------------------------------------------------------
# the registry of codes, and what a Finding is allowed to say
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_every_finding_code_is_exercised_by_a_test_in_this_module() -> None:
    """§4's table argument applied to the codes: an unexercised code is an unenforced finding.

    Checked in both directions. A code in `FINDING_CODES` with no test is a `report.json` key that has
    never been produced — and the emitting branch is usually one `if`, so "it obviously works" is
    exactly the reasoning that leaves it firing on nothing. A mapping entry naming a test that does not
    exist is the other failure: a renamed or deleted test that stops covering a code it still claims.
    """
    assert set(_TEST_FOR_CODE) == set(FINDING_CODES)

    for code, name in _TEST_FOR_CODE.items():
        test = globals().get(name)
        assert test is not None, f"{code} claims {name}, which does not exist"
        assert callable(test)
        assert name.startswith("test_")


@pytest.mark.spec
def test_a_finding_has_no_severity_field() -> None:
    """§6.2 gives severity to M4's rule registry, so M2 must not carry one.

    Written as a field-list assertion because the failure is additive: somebody adds
    `severity="warning"` to one finding, every existing test keeps passing, and now two modules own a
    severity scale. The first rule M4 tunes moves one copy. Every one of these findings is a candidate
    flag, which is exactly why the boundary is worth pinning at the type.
    """
    names = {field.name for field in dataclasses.fields(Finding)}

    assert names == {"code", "metric", "detail", "evidence"}
    assert "severity" not in names
    assert not any("sever" in name for name in names)


@pytest.mark.spec
@pytest.mark.parametrize(
    "fixture",
    [
        "AAPL.trimmed.json",
        "BADUNIT.trimmed.json",
        "NCI.trimmed.json",
        "NOQ4.trimmed.json",
        "RESTATER.trimmed.json",
        "STUBYEAR.trimmed.json",
        "TIER2.trimmed.json",
        "YTDONLY.trimmed.json",
    ],
)
def test_no_finding_carries_an_undeclared_code(fixture: str) -> None:
    """`FINDING_CODES` is the closed set `report.json` keys on.

    A code emitted but not declared is a key no consumer expects and no test in this module owns, and
    the meta-test above cannot see it — it checks that every declared code has a test, not that every
    emitted code is declared. Both halves are needed for the tuple to mean what §4's table says.
    """
    for finding in history(fixture).coverage.findings:
        assert finding.code in FINDING_CODES


def test_finding_codes_are_unique() -> None:
    """A duplicate entry would make the meta-test's set comparison pass while hiding a code.

    `FINDING_CODES` is a tuple so its order can be the order `facts` prints; the cost of an ordered
    collection is that it can hold the same value twice, and `set(...) == set(...)` would not notice.
    """
    assert len(set(FINDING_CODES)) == len(FINDING_CODES)


@pytest.mark.spec
def test_company_level_findings_carry_no_metric_and_come_first() -> None:
    """§4: `metric` is `None` for a company-level finding, and those print before the per-metric ones.

    The order is not cosmetic. A coverage figure measured against an `OBSERVED` spine has to be
    qualified before any per-metric number below it means anything, so `spine_observed` appearing after
    twenty metric rows is a caveat the reader meets too late. Asserted as "no company-level finding
    follows a per-metric one", which is the property, rather than by pinning a full expected list.
    """
    built = history("TIER2.trimmed.json")
    findings = built.coverage.findings
    assert {finding.code for finding in findings} & COMPANY_LEVEL_CODES

    seen_metric_level = False
    for finding in findings:
        if finding.code in COMPANY_LEVEL_CODES:
            assert finding.metric is None
            assert not seen_metric_level, f"{finding.code} printed after a per-metric finding"
        else:
            assert finding.metric is not None
            seen_metric_level = True


def test_every_finding_carries_a_detail_a_reader_can_act_on() -> None:
    """`detail` is printed verbatim by `facts` and in §9.1's caveats, so it cannot be blank.

    An empty string would render as a bare code beside white space — which looks like a rendering bug
    rather than a data caveat, and the reader's conclusion would be that the tool is broken rather
    than that the filing is odd.
    """
    for fixture in ("TIER2.trimmed.json", "BADUNIT.trimmed.json", "NCI.trimmed.json"):
        for finding in history(fixture).coverage.findings:
            assert finding.detail.strip(), finding.code


def test_a_per_metric_detail_names_the_metric_it_concerns() -> None:
    """`facts` prints the code and the detail, and nothing else — so the detail carries the metric.

    `finding.metric` is set on every per-metric finding, which is what `report.json` keys on, but the
    rendered caveat list has no metric column: a detail that does not name its metric leaves the reader
    with a caveat and no way to tell which of twenty-five rows it applies to.

    **No exemptions**, and that is the point of asserting it over three fixtures rather than one:
    `net_income_scope_mismatch` was written as prose without the prefix, which was legible in isolation
    and invisible in a caveat list of sixteen. A uniform rule caught it; a rule with one exemption
    would have institutionalized it.
    """
    for fixture in ("TIER2.trimmed.json", "BADUNIT.trimmed.json", "NCI.trimmed.json"):
        for finding in history(fixture).coverage.findings:
            if finding.metric is None:
                continue
            assert str(finding.metric) in finding.detail, finding.code


# ---------------------------------------------------------------------------
# one test per code
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_coverage_below_floor_fires_only_strictly_under_the_floor() -> None:
    """§4: the finding is `fill_rate < floor`, and the boundary gets its own assertion.

    `TIER2`'s long-term debt lands at exactly 2/4 because its two chain members interleave, which makes
    it the only fixture metric sitting on a round fill rate — so a `<=` where `<` belongs is visible
    here and nowhere else. A floor equal to the rate must **not** fire: a gate that flags the value it
    was set to is a gate everybody learns to ignore.

    `None` disabling the finding entirely is asserted too, because the floor is deliberately not a
    config field yet (`docs/m2/05-testing.md` §8: picking one before the measurement would be picking
    the number that makes today's fixtures pass).
    """
    on_the_floor = history("TIER2.trimmed.json", coverage_floor=Decimal("0.5"))
    below = history("TIER2.trimmed.json", coverage_floor=Decimal("0.51"))
    disabled = history("TIER2.trimmed.json", coverage_floor=None)

    assert on_the_floor.coverage.annual[Metric.LONG_TERM_DEBT].fill_rate == Decimal("0.5")

    def for_debt(built: FinancialHistory) -> list[Finding]:
        return [
            finding
            for finding in built.coverage.findings_for("coverage_below_floor")
            if finding.metric is Metric.LONG_TERM_DEBT
        ]

    assert for_debt(on_the_floor) == []
    assert for_debt(below) != []
    assert disabled.coverage.findings_for("coverage_below_floor") == ()
    assert on_the_floor.coverage.findings_for("coverage_below_floor") != (), (
        "the floor still has to fire on the metrics genuinely under it"
    )


@pytest.mark.spec
def test_q4_derived_names_only_the_year_that_needed_it() -> None:
    """§4.2(c): Q4 derivation is conditional, and the finding reports which years it touched.

    `NOQ4` carries both halves — FY2022 with three quarters and an annual figure, FY2023 with four
    filed quarters — so an unconditional rule produces two derived Q4s and five quarters in 2023. The
    assertion is therefore on *which* period ends the finding names, taken from the payload rather than
    written down: a count of one would also pass for an implementation that derived the wrong year.
    """
    built = history("NOQ4.trimmed.json")
    quarters = built.series(Metric.REVENUE, Bucket.QUARTERLY)
    derived = [fact for fact in quarters if isinstance(fact.source, Derivation)]
    annual_ends = {fact.period.end for fact in built.series(Metric.REVENUE, Bucket.ANNUAL)}
    filed_quarter_ends = {
        fact.period.end for fact in quarters if not isinstance(fact.source, Derivation)
    }
    needed = sorted(annual_ends - filed_quarter_ends)
    assert len(needed) == 1, "the fixture carries one year that needs a Q4 and one that does not"

    findings = built.coverage.findings_for("q4_derived")

    assert [finding.metric for finding in findings] == [Metric.REVENUE]
    assert [fact.period.end for fact in derived] == needed
    for end in needed:
        assert end.isoformat() in findings[0].detail
    for end in annual_ends - set(needed):
        assert end.isoformat() not in findings[0].detail

    assert history("IPO.trimmed.json").coverage.findings_for("q4_derived") == (), (
        "no annual figure means nothing to subtract from, so nothing is derived"
    )


def test_q4_derived_carries_the_derivation_as_evidence() -> None:
    """`evidence` is the field that makes a finding checkable, and this is the code that populates it.

    A derived Q4 traces to four accessions — the year and its three quarters — and §3.2's rule is that
    a figure which cannot be traced is not printed. The finding is the place a reader is told the
    number was computed, so it has to name the same filings the fact does; a finding with empty
    evidence would say "derived" and leave nothing to check it against.
    """
    built = history("NOQ4.trimmed.json")
    finding = built.coverage.findings_for("q4_derived")[0]
    derived = next(
        fact
        for fact in built.series(Metric.REVENUE, Bucket.QUARTERLY)
        if isinstance(fact.source, Derivation)
    )

    assert finding.evidence == (derived.source,)
    evidence = finding.evidence[0]
    assert isinstance(evidence, Derivation), "the evidence is the derivation, not one of its refs"
    assert len(evidence.refs()) == 4


@pytest.mark.spec
def test_q4_absent_fires_where_no_fourth_quarter_is_filed_or_derivable() -> None:
    """§4: an annual period with no Q4, filed or derivable — not simply a missing quarter.

    `YTDONLY` is the shape: its Q2 and Q3 were themselves recovered from the cumulative ladder, and
    residual recovery refuses to consume its own output, so the Q4 is genuinely unreachable. The
    negative half is `NOQ4`, where the Q4 *was* derivable and the finding must stay silent — otherwise
    the flag fires on every filer whose Q4 came from subtraction, which is most of them.
    """
    built = history("YTDONLY.trimmed.json")
    annual_ends = {fact.period.end for fact in built.series(Metric.REVENUE, Bucket.ANNUAL)}
    quarter_ends = {fact.period.end for fact in built.series(Metric.REVENUE, Bucket.QUARTERLY)}
    unreachable = sorted(annual_ends - quarter_ends)
    assert unreachable

    findings = built.coverage.findings_for("q4_absent")

    assert [finding.metric for finding in findings] == [Metric.REVENUE]
    for end in unreachable:
        assert end.isoformat() in findings[0].detail

    assert history("NOQ4.trimmed.json").coverage.findings_for("q4_absent") == ()
    assert history("IPO.trimmed.json").coverage.findings_for("q4_absent") == (), (
        "a filer with no annual figure has no missing Q4, it has no annual data"
    )


@pytest.mark.spec
def test_series_stitched_fires_when_two_chain_members_contributed() -> None:
    """§4: `len(tags_used) > 1` is the finding, and the detail prints them in first-use order.

    `AAPL` is the ASC 606 case DESIGN.md §4.2 requires stitching for. The negative half is a metric in
    the same payload resolved by one tag throughout: without it, an implementation that emitted the
    finding for every metric with any tag would pass, and the §6.4 caveat list would name all
    twenty-five.
    """
    built = history("AAPL.trimmed.json")
    stitched = built.coverage.annual[Metric.REVENUE].tags_used
    single = built.coverage.annual[Metric.ASSETS].tags_used
    assert len(stitched) > 1
    assert len(single) == 1

    metrics = {finding.metric for finding in built.coverage.findings_for("series_stitched")}

    assert Metric.REVENUE in metrics
    assert Metric.ASSETS not in metrics
    detail = built.coverage.findings_for("series_stitched")[0].detail
    assert all(tag in detail for tag in stitched)
    assert detail.index(stitched[0]) < detail.index(stitched[1]), (
        "reversing the order describes the transition backwards"
    )


@pytest.mark.spec
def test_equal_values_across_four_accessions_raise_no_restated_finding() -> None:
    """§4: `restated` fires on a **value change**, not on a re-filing.

    `AAPL`'s quarter ending 2019-06-29 appears under four accessions with four `filed` dates and the
    same value each time — a comparative carried forward, which is what every 10-Q does. Flagging it
    would put a false accounting signal on the flagship fixture and on roughly every filer.

    The `Restatement` record still keeps all four generations, and asserting that is what separates
    this from an implementation that simply forgot to build the record: the evidence for ROADMAP open
    question 10 has to survive even though the finding does not fire. `RESTATER`, whose four filings
    carry four different numbers, is the positive half.
    """
    built = history("AAPL.trimmed.json")
    records = [record for record in built.restatements if len(record.superseded) == 3]
    assert records, "the fixture's four generations must reach the restatement record"
    record = records[0]

    assert record.period.end == date(2019, 6, 29)
    assert record.value_changed is False
    assert {value for _, value, _ in record.superseded} == {record.current}
    assert len({accession for _, _, accession in record.superseded}) == 3, (
        "four accessions, three of them superseded"
    )
    assert built.coverage.findings_for("restated") == ()

    changed = history("RESTATER.trimmed.json")
    assert any(record.value_changed for record in changed.restatements)
    findings = changed.coverage.findings_for("restated")
    assert [finding.metric for finding in findings] == [Metric.REVENUE]
    assert changed.restatements[0].period.end.isoformat() in findings[0].detail


@pytest.mark.spec
def test_window_truncated_fires_a_whole_year_inside_the_window() -> None:
    """§6.4's "lookback shorter than requested", at the threshold it is measured against.

    "Materially later" is one annual period — `ANNUAL_DAYS`' lower bound — and the boundary needs its
    own case in both directions, because a tighter threshold fires on every filer whose fiscal year
    does not start in the month the command was run (`window` floors to the first of that month) and a
    looser one never fires at all. The dates are computed from the fixture's own earliest evidence, so
    the test states the rule rather than three literals.
    """
    fixture = "TIER2.trimmed.json"
    earliest = min(
        fact.period.start or fact.period.end
        for fact in history(fixture).series(Metric.REVENUE, Bucket.ANNUAL)
    )

    def truncated(days_before: int) -> tuple[Finding, ...]:
        window = (earliest - timedelta(days=days_before), M2_WINDOW[1])
        return history(fixture, window=window).coverage.findings_for("window_truncated")

    assert truncated(349) == ()
    assert truncated(350) == (), "exactly one annual period is not yet a truncation"
    assert len(truncated(351)) == 1
    assert earliest.isoformat() in truncated(351)[0].detail


@pytest.mark.spec
def test_companyfacts_absent_fires_only_when_the_payload_is_absent() -> None:
    """§1: no XBRL facts published for the CIK is an **absence**, not a failure.

    `fetch.py` records it and the run exits 0, and README § 4 lists it as a normal outcome of `facts`.
    The finding is the only thing that distinguishes it from a filer whose payload existed and tagged
    nothing — which is a different fact about a different company, and one the spine still measures.
    """
    absent = build_history(
        None,
        ticker="TEST",
        cik=1000000,
        name="Test Corp",
        profile=None,
        window=M2_WINDOW,
    )

    assert len(absent.coverage.findings_for("companyfacts_absent")) == 1
    assert absent.coverage.findings_for("companyfacts_absent")[0].metric is None
    assert "companyfacts_absent" not in _codes(history("TIER2.trimmed.json"))


@pytest.mark.spec
def test_submissions_absent_fires_only_when_the_profile_is_absent() -> None:
    """§1: the two absences are kept independent, because they degrade differently.

    A 404 on submissions costs the SIC, the fiscal year end and the coverage denominator while the
    series survive intact; a 404 on companyfacts costs the series while the denominator survives.
    Collapsing them into one finding would tell a reader the wrong half of the report is unreliable.
    """
    profile, filings = submissions("AAPL.json")

    assert len(history("AAPL.trimmed.json").coverage.findings_for("submissions_absent")) == 1
    with_profile = history("AAPL.trimmed.json", profile=profile, filings=filings)
    assert with_profile.coverage.findings_for("submissions_absent") == ()
    assert "companyfacts_absent" not in _codes(history("AAPL.trimmed.json")), (
        "an absent profile must not be reported as absent facts"
    )


@pytest.mark.spec
def test_spine_observed_fires_when_no_periodic_filing_is_in_the_window() -> None:
    """§2: the circular denominator is **labelled**, never silent.

    "A 100% figure that quietly came from an `OBSERVED` spine is the single most misleading number this
    milestone could produce." `NOPERIODIC` is the shape that reaches it — `S-1/A`, `EFFECT` and `8-K`
    only, paired with a payload that has facts — and the negative half is the same payload given one
    10-K, which must switch the origin and silence the finding.
    """
    profile, filings = submissions("NOPERIODIC.json")
    observed = history("NOPERIODIC.trimmed.json", profile=profile, filings=filings)

    assert len(observed.coverage.findings_for("spine_observed")) == 1
    assert observed.coverage.findings_for("spine_observed")[0].metric is None
    assert not observed.coverage.spine.is_empty, "the fallback still produces a denominator"

    with_a_10k = history(
        "NOPERIODIC.trimmed.json",
        profile=profile,
        filings=filing_rows(("10-K", "2026-02-20", "2025-12-31")),
    )
    assert with_a_10k.coverage.findings_for("spine_observed") == ()


@pytest.mark.spec
def test_spine_date_inexact_fires_on_a_report_date_off_by_one() -> None:
    """§2: a fact whose period end matched within tolerance but not exactly is *counted*.

    The spine is the filing header's `reportDate` and a fact's end comes from the XBRL context; they
    are usually the same date and are not the same field. One or two inexact matches is ordinary, so
    the finding exists to surface the filer where *every* period disagrees — which is a fact about that
    filer rather than a tolerance to widen. The negative half is the same payload with exact report
    dates, which must produce nothing.
    """
    off_by_one = history(
        "TIER2.trimmed.json",
        filings=filing_rows(
            ("10-K", "2022-02-18", "2021-12-30"),
            ("10-K", "2023-02-17", "2022-12-31"),
        ),
    )
    exact = history(
        "TIER2.trimmed.json",
        filings=filing_rows(
            ("10-K", "2022-02-18", "2021-12-31"),
            ("10-K", "2023-02-17", "2022-12-31"),
        ),
    )

    revenue = off_by_one.coverage.annual[Metric.REVENUE]
    assert revenue.spine_date_inexact == 1
    assert revenue.filled == exact.coverage.annual[Metric.REVENUE].filled, (
        "an inexact match must not cost coverage, which is the reason for the tolerance"
    )

    metrics = {finding.metric for finding in off_by_one.coverage.findings_for("spine_date_inexact")}
    assert Metric.REVENUE in metrics
    assert exact.coverage.findings_for("spine_date_inexact") == ()


@pytest.mark.spec
def test_exclusivity_switch_fires_on_a_partition_and_not_on_an_interleave() -> None:
    """§5: a permanent switch is stitched and named; alternation is noise and collapses silently.

    `TIER2` carries both shapes, which is what makes this discriminating. Its revenue partitions the
    timeline — two years excluding assessed tax, then two years including — so both tags are kept and
    the boundary date is reported. Its long-term debt alternates between `LongTermDebt` and the
    capital-lease concept, so majority-wins applies and **no** switch is reported. A
    stitch-everything implementation fails the second assertion; a majority-wins-always implementation
    fails the first.
    """
    built = history("TIER2.trimmed.json")
    findings = built.coverage.findings_for("exclusivity_switch")

    assert [finding.metric for finding in findings] == [Metric.REVENUE]
    detail = findings[0].detail
    assert "revenue_assessed_tax" in detail
    for tag in built.coverage.annual[Metric.REVENUE].tags_used:
        assert tag in detail
    boundary = min(
        fact.period.end
        for fact in built.series(Metric.REVENUE, Bucket.ANNUAL)
        if fact.period.end.year >= 2023
    )
    assert boundary.isoformat() in detail

    assert Metric.LONG_TERM_DEBT not in {finding.metric for finding in findings}


@pytest.mark.spec
def test_net_income_scope_mismatch_fires_on_the_including_nci_tag() -> None:
    """§4.2 pairs parent-only net income with parent-only equity, and `ProfitLoss` breaks the pair.

    `TIER2` tags only `ProfitLoss`, so any return on equity built from its series divides an
    including-NCI numerator by a parent-only denominator. Recorded, not corrected: M2's job is to state
    what is true. The negative half is `AAPL`, which resolves `NetIncomeLoss` — otherwise the finding
    would fire on every filer that reports net income at all.
    """
    assert history("TIER2.trimmed.json").coverage.annual[Metric.NET_INCOME].tags_used == (
        "us-gaap:ProfitLoss",
    )
    findings = history("TIER2.trimmed.json").coverage.findings_for("net_income_scope_mismatch")

    assert [finding.metric for finding in findings] == [Metric.NET_INCOME]

    parent_only = history("AAPL.trimmed.json")
    assert parent_only.coverage.annual[Metric.NET_INCOME].tags_used == ("us-gaap:NetIncomeLoss",)
    assert parent_only.coverage.findings_for("net_income_scope_mismatch") == ()


@pytest.mark.spec
def test_liabilities_nci_approximated_fires_on_the_fallback() -> None:
    """§9: using parent-only equity as the subtrahend is an approximation, and it is announced.

    "Approximating is better than omitting the metric entirely; doing it invisibly is not." `NCI`'s
    later year has only the parent-only tag, so the fallback is the only way to produce the metric —
    and the finding is what tells the reader that year's liabilities include any noncontrolling
    interest while the earlier year's do not. `TIER2`, which never reaches the derivation, is the
    negative half.
    """
    built = history("NCI.trimmed.json")
    findings = built.coverage.findings_for("liabilities_nci_approximated")
    never_derived = history("TIER2.trimmed.json").coverage

    assert [finding.metric for finding in findings] == [Metric.LIABILITIES]
    assert never_derived.findings_for("liabilities_nci_approximated") == ()


@pytest.mark.spec
def test_sga_composed_fires_when_the_two_components_were_summed() -> None:
    """§4: the one non-substituting member, and the count of periods it produced.

    A filer reports either the combined `SellingGeneralAndAdministrativeExpense` or the two components
    separately, and `TIER2` does both — combined for two years, split for two. So the finding must
    report exactly the split years: substituting one component for the combined figure would understate
    the metric by the other, silently, and Piotroski's margin test would improve for a filer that
    changed only its presentation.
    """
    built = history("TIER2.trimmed.json")
    summed = [
        fact
        for fact in built.series(Metric.SGA, Bucket.ANNUAL)
        if isinstance(fact.source, Derivation)
    ]
    assert summed
    findings = built.coverage.findings_for("sga_composed")

    assert [finding.metric for finding in findings] == [Metric.SGA]
    assert f"{len(summed)} period(s)" in findings[0].detail
    for fact in summed:
        assert isinstance(fact.source, Derivation)
        assert len(fact.source.refs()) == 2

    assert history("AAPL.trimmed.json").coverage.findings_for("sga_composed") == ()


@pytest.mark.spec
def test_sign_anomaly_fires_on_a_fact_that_contradicts_its_convention() -> None:
    """§8: a fact contradicting the declared convention is **kept**, and counted.

    A negative capex quarter is real — a disposal netted against acquisitions — and dropping it makes
    FCF wrong in the other direction, so the anomaly is reported for M4 rather than corrected here. The
    assertion is therefore two-part: the finding fires *and* the negative value survives in the series
    unaltered. A payload with only the positive fact is the negative half, so the finding cannot be
    firing on the metric's mere presence.
    """
    positive = _capex_fact("88000000", year=2022)
    negative = _capex_fact("-95000000", year=2023)
    both = _history_of(_with_capex("TIER2.trimmed.json", positive, negative))
    positive_only = _history_of(_with_capex("TIER2.trimmed.json", positive))

    findings = both.coverage.findings_for("sign_anomaly")
    assert [finding.metric for finding in findings] == [Metric.CAPEX]
    assert "outflow_positive" in findings[0].detail

    values = {fact.period.end: fact.value for fact in both.series(Metric.CAPEX, Bucket.ANNUAL)}
    assert values[date(2023, 12, 31)] == Decimal("-95000000"), "kept, not corrected or dropped"
    assert both.coverage.annual[Metric.CAPEX].sign_anomalies == 1

    assert positive_only.coverage.findings_for("sign_anomaly") == ()
    assert positive_only.coverage.annual[Metric.CAPEX].filled == 1, (
        "the negative half must still resolve the metric, or it proves nothing"
    )


@pytest.mark.spec
def test_unit_mismatch_fires_and_names_the_unit_it_excluded() -> None:
    """§7: the exclusion is **counted**, not merely absent, and the finding names what was seen.

    "A metric absent because every fact was `EUR` is a different finding from one absent because the
    tag was never used." `BADUNIT` carries both directions the filter has to catch: a revenue fact
    under `EUR`, and an EPS fact under `USD` where the metric is `USD/shares` — the case §4.2 says
    reports an EPS three orders of magnitude off. `TIER2`, all `USD`, is the negative half.
    """
    built = history("BADUNIT.trimmed.json")
    findings = {finding.metric: finding for finding in built.coverage.findings_for("unit_mismatch")}

    assert set(findings) == {Metric.REVENUE, Metric.EPS_DILUTED}
    assert "EUR" in findings[Metric.REVENUE].detail
    assert "USD/shares" in findings[Metric.EPS_DILUTED].detail
    assert built.coverage.annual[Metric.EPS_DILUTED].dropped_unit_mismatch == 1

    assert history("TIER2.trimmed.json").coverage.findings_for("unit_mismatch") == ()


@pytest.mark.spec
def test_other_bucket_drops_fires_on_a_transition_stub() -> None:
    """§4: an `OTHER` period enters no series, and the drop is counted.

    `STUBYEAR`'s transition period is a real filing covering real months, and it is neither an annual
    nor a quarterly figure — putting it in either produces a series with one short year that every
    growth rate reads as a collapse. So it is dropped, and the count is what turns "we lost a period"
    into "this filer changed its fiscal year". `TIER2`, whose periods are all ordinary, is the negative
    half.
    """
    built = history("STUBYEAR.trimmed.json")
    findings = built.coverage.findings_for("other_bucket_drops")

    assert [finding.metric for finding in findings] == [Metric.REVENUE]
    assert built.coverage.annual[Metric.REVENUE].dropped_other_bucket == 1
    for bucket in Bucket:
        for fact in built.series(Metric.REVENUE, bucket):
            assert fact.period.days is None or fact.period.days > 100

    assert history("TIER2.trimmed.json").coverage.findings_for("other_bucket_drops") == ()


@pytest.mark.spec
def test_periods_outside_spine_fires_on_a_period_the_filings_omit() -> None:
    """§2: a period the filing history does not account for is kept, and counted, not dropped.

    Usually a fiscal-year change or an amended report date, and it is real data — so it stays in the
    series, contributes nothing to the numerator, and appears here. That combination is what bounds
    coverage at 100% *and* makes the bound visible: the count is how a reader tells whether the bound
    is doing any work. `AAPL`'s single 10-K accounts for one of its four revenue years; `NOPERIODIC`,
    whose spine is the observed calendar itself, is the negative half.
    """
    profile, filings = submissions("AAPL.json")
    built = history("AAPL.trimmed.json", profile=profile, filings=filings)
    coverage = built.coverage.annual[Metric.REVENUE]
    series = built.series(Metric.REVENUE, Bucket.ANNUAL)

    assert coverage.periods_outside_spine == len(series) - coverage.filled
    assert coverage.periods_outside_spine > 0
    metrics = {finding.metric for finding in built.coverage.findings_for("periods_outside_spine")}
    assert Metric.REVENUE in metrics

    observed_profile, observed_filings = submissions("NOPERIODIC.json")
    observed = history(
        "NOPERIODIC.trimmed.json",
        profile=observed_profile,
        filings=observed_filings,
    )
    assert observed.coverage.findings_for("periods_outside_spine") == ()

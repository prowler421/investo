"""The coverage denominator: four construction rules, and a ±3-day one-to-one match.

`docs/m2/03-statements.md` §2 is the design; DESIGN.md §3.2 carries the decision. "% of periods
filled" needs a denominator, and of the three candidates only *periods the company actually reported*
measures what the number is supposed to mean — it is also the only one independent of the facts, which
is what stops a tagging failure from shrinking its own denominator.

Every test here is about a way the denominator can be wrong while looking right:

- amendments counted twice → an annual denominator larger than the number of years the filer existed,
  and coverage that caps out around 66% on a filer that tagged everything;
- 10-Qs only → three quarters a year, and 133% coverage for any filer whose Q4s were derived;
- exact date matching → coverage *undercounted* on any filer whose filing header and XBRL contexts
  disagree by a day, in the one number that gates the milestone;
- many-to-one matching → `filled` above `expected`, which is the bug the 100% bound exists to make
  impossible;
- an unlabelled fallback → a 100% figure computed against a circular denominator, which is the single
  most misleading number this milestone could produce.

The fixtures do half the work. `ARXS` has one 10-Q and **no 10-K**, so its annual `expected` is zero
and its annual `fill_rate` is `None` rather than 0% or 100%; `NOPERIODIC` has facts and no periodic
filing at all, which is the only shape that reaches the `OBSERVED` fallback.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from investo.domain.models import Metric
from investo.normalize.facts import SEAM_TOLERANCE
from investo.normalize.statements import (
    ANNUAL_FORMS,
    QUARTERLY_FORMS,
    Bucket,
    SpineOrigin,
    build_spine,
)
from tests.conftest import M2_WINDOW, filing_rows, history, submissions

WIDE = (date(2000, 1, 1), date(2030, 12, 31))
"""A window that cannot be the reason a spine entry is missing — the windowing test uses its own."""


def _spine(*specs: tuple[str, str, str | None], window: tuple[date, date] = WIDE):
    return build_spine(filing_rows(*specs), window=window)


# ---------------------------------------------------------------------------
# construction rule 1 — amendments collapse
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_10ka_collapses_into_its_original() -> None:
    """An amended 10-K cannot inflate the denominator.

    Without the collapse, a filer that amended two years of 10-Ks has an annual denominator two larger
    than the number of years it existed — and coverage caps out around 66% on data that is perfectly
    tagged. The failure is a percentage that never reaches 100 for reasons that have nothing to do with
    tagging, which is precisely the kind of number someone eventually "fixes" by lowering the floor.
    """
    spine = _spine(
        ("10-K", "2023-02-20", "2022-12-31"),
        ("10-K/A", "2023-06-14", "2022-12-31"),
    )
    assert spine.annual_ends == (date(2022, 12, 31),)


@pytest.mark.spec
def test_a_10qa_collapses_the_same_way() -> None:
    """The same rule on the quarterly side, because the suffix strip is one code path.

    Asserted separately anyway: an implementation that special-cased `"10-K/A"` as a literal would pass
    the test above and leave every amended 10-Q double-counted, which is the more common amendment.
    """
    spine = _spine(
        ("10-Q", "2023-05-04", "2023-03-31"),
        ("10-Q/A", "2023-07-11", "2023-03-31"),
    )
    assert spine.quarterly_ends == (date(2023, 3, 31),)


@pytest.mark.spec
@pytest.mark.parametrize("form", ["10-K", "10-KT", "10-K/A", "10-KT/A", "10-k"])
def test_every_spelling_of_an_annual_report_is_an_annual_spine_entry(form: str) -> None:
    """Transition reports and casing, both of which occur in real filing histories.

    `10-KT` is the form a filer uses for a transition period after a fiscal-year change — exactly the
    filer whose spine is hardest to get right — and it is in `ANNUAL_FORMS` for that reason. Casing is
    normalized because SEC's own data is not uniformly upper-case across the whole history.
    """
    assert _spine((form, "2023-02-20", "2022-12-31")).annual_ends == (date(2022, 12, 31),)


@pytest.mark.spec
def test_a_form_that_is_not_periodic_contributes_nothing() -> None:
    """`8-K`, `S-1/A`, `4` and `DEF 14A` are not evidence of a reporting period.

    A spine that counted them would have a denominator driven by insider-transaction volume, and a
    filer with heavy Form 4 traffic would report near-zero coverage. `ANNUAL_FORMS` and
    `QUARTERLY_FORMS` are allowlists for that reason, and the sets are asserted here so a widening is a
    visible edit.
    """
    spine = _spine(
        ("8-K", "2023-04-02", "2023-04-01"),
        ("S-1/A", "2023-03-10", None),
        ("4", "2023-05-04", "2023-05-01"),
        ("DEF 14A", "2023-03-01", "2023-02-28"),
    )
    assert spine.is_empty
    assert ANNUAL_FORMS == {"10-K", "10-KT"}
    assert QUARTERLY_FORMS == {"10-Q", "10-QT"}


# ---------------------------------------------------------------------------
# construction rule 2 — an annual report date is also a quarterly entry
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_annual_report_date_is_a_quarterly_spine_entry() -> None:
    """A derived Q4 cannot push quarterly coverage over 100%.

    A filer files three 10-Qs a year; the fourth quarter's end date appears only on the 10-K. A
    quarterly denominator built from 10-Qs alone is three per year, so any filer whose Q4s were
    derived — most of them, per §4.2(c) — reports 133% coverage. Asserted as the count, four against
    three, because the dates alone would look plausible either way.
    """
    spine = _spine(
        ("10-Q", "2022-05-04", "2022-03-31"),
        ("10-Q", "2022-08-04", "2022-06-30"),
        ("10-Q", "2022-11-04", "2022-09-30"),
        ("10-K", "2023-02-20", "2022-12-31"),
    )
    assert len(spine.quarterly_ends) == 4
    assert date(2022, 12, 31) in spine.quarterly_ends
    assert spine.annual_ends == (date(2022, 12, 31),)


@pytest.mark.spec
def test_a_derived_q4_does_not_push_coverage_over_100_percent() -> None:
    """The same rule measured end to end, on the fixture built to derive a Q4.

    `NOQ4`'s FY2022 has three filed quarters and a derived fourth. With the annual report date in the
    quarterly spine the four quarters meet four expected periods; without it, four meet three.
    """
    filings = filing_rows(
        ("10-Q", "2022-05-04", "2022-03-31"),
        ("10-Q", "2022-08-04", "2022-06-30"),
        ("10-Q", "2022-11-04", "2022-09-30"),
        ("10-K", "2023-02-20", "2022-12-31"),
    )
    built = history("NOQ4.trimmed.json", filings=filings)
    coverage = built.coverage.quarterly[Metric.REVENUE]
    assert coverage.expected == 4
    assert coverage.filled == 4
    assert coverage.fill_rate is not None and coverage.fill_rate <= 1


# ---------------------------------------------------------------------------
# construction rule 3 — a missing report date is not evidence
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_report_date_none_contributes_nothing() -> None:
    """`reportDate: ""` normalizes to `None`, and those filings are still in the list.

    The `ARXS` payload carries several. They are not spine evidence — a filing with no period end
    cannot say which period was reported — but dropping the *row* would lose the 8-K items M4.5 reads,
    so the two concerns are separated: the row survives, the spine ignores it.
    """
    spine = _spine(
        ("10-K", "2023-02-20", None),
        ("10-Q", "2023-05-04", "2023-03-31"),
    )
    assert spine.annual_ends == ()
    assert spine.quarterly_ends == (date(2023, 3, 31),)


@pytest.mark.spec
def test_arxs_annual_expected_is_zero() -> None:
    """One 10-Q, no 10-K — and an empty annual spine cannot read as 0% coverage.

    A recent registrant that has filed one quarterly report and no annual report **has not failed to
    be tagged**, and this is the case that makes `fill_rate` optional rather than a number. Both
    defaults would be lies: 0% blames the filer for not existing yet, 100% claims a measurement over
    nothing. The `ARXS` payload is the worked example, and its shape was checked rather than assumed.
    """
    profile, filings = submissions("ARXS.json", cik=2093536)
    result = history("ARXS.json", cik=2093536, profile=profile, filings=filings)
    spine = result.coverage.spine

    assert spine.origin is SpineOrigin.FILINGS, "ARXS does have a periodic filing"
    assert spine.annual_ends == ()
    assert len(spine.quarterly_ends) == 1

    for metric in (Metric.REVENUE, Metric.ASSETS, Metric.EQUITY):
        annual = result.coverage.annual[metric]
        assert annual.expected == 0
        assert annual.fill_rate is None, "not 0%, and not 100%"


# ---------------------------------------------------------------------------
# construction rule 4 — the spine is windowed the way the facts are
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_the_spine_is_windowed_on_the_report_date() -> None:
    """Numerator and denominator have to be measured over the same interval.

    A spine built over the whole filing history against facts filtered to a window produces coverage
    below 100% at the far edge — the filer reported periods we deliberately did not read — and a spine
    windowed on `filed` rather than `report_date` produces it above 100% at the near edge, because a
    10-K filed inside the window describes a period ending before it.
    """
    specs = (
        ("10-K", "2019-02-20", "2018-12-31"),
        ("10-K", "2023-02-20", "2022-12-31"),
        ("10-K", "2026-02-20", "2025-12-31"),
    )
    windowed = _spine(*specs, window=(date(2022, 1, 1), date(2024, 12, 31)))
    assert windowed.annual_ends == (date(2022, 12, 31),)


@pytest.mark.spec
def test_the_window_boundary_is_inclusive_at_both_ends() -> None:
    """A report date equal to the window's first or last day is inside it.

    `window()` floors its start to the first of a month, so a filer whose fiscal year ends on that day
    lands exactly on the boundary — and an exclusive comparison would drop that filer's oldest year for
    every run made in that month and keep it in every other one.
    """
    window = (date(2022, 1, 1), date(2024, 12, 31))
    spine = _spine(
        ("10-K", "2022-03-01", "2022-01-01"),
        ("10-K", "2025-02-20", "2024-12-31"),
        window=window,
    )
    assert spine.annual_ends == (date(2022, 1, 1), date(2024, 12, 31))


@pytest.mark.spec
def test_the_spine_is_built_from_the_report_date_not_the_filing_date() -> None:
    """Using `filed` shifts the whole spine by a quarter, and coverage collapses.

    The report date is the period end; the filing date is two months later. A spine on `filed` would
    put a 10-K's entry in the following March, where no fact's period ends — so every annual metric
    would read 0% filled against a full complement of expected periods, for every filer.
    """
    spine = _spine(("10-K", "2023-02-20", "2022-12-31"))
    assert spine.annual_ends == (date(2022, 12, 31),)
    assert date(2023, 2, 20) not in spine.annual_ends


# ---------------------------------------------------------------------------
# matching: nearest within three days, one-to-one
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_report_date_off_by_one_still_counts() -> None:
    """A one-day disagreement cannot undercount coverage.

    The spine comes from the filing header; a fact's `period.end` comes from the XBRL context in the
    instance document. They are usually the same date and they are **not the same field**, so exact
    equality reports a tagged, present metric as missing — the same "wrong quietly" shape the rest of
    the milestone refuses, arriving in the one number that gates it.
    """
    filings = filing_rows(("10-K", "2023-02-20", "2022-12-30"))
    coverage = history("NOQ4.trimmed.json", filings=filings).coverage.annual[Metric.REVENUE]
    assert coverage.expected == 1
    assert coverage.filled == 1
    assert coverage.spine_date_inexact == 1, "counted, because a systematic disagreement is a finding"


@pytest.mark.spec
def test_a_three_day_gap_is_the_last_one_that_matches() -> None:
    """The boundary, on the permissive side. `SEAM_TOLERANCE` is three days everywhere in M2."""
    assert SEAM_TOLERANCE == timedelta(days=3)
    filings = filing_rows(("10-K", "2023-02-20", "2022-12-28"))
    coverage = history("NOQ4.trimmed.json", filings=filings).coverage.annual[Metric.REVENUE]
    assert coverage.filled == 1
    assert coverage.spine_date_inexact == 1


@pytest.mark.spec
def test_four_day_gap_does_not_match() -> None:
    """And the boundary on the other side, or a `<` where `<=` belongs survives both tests.

    Four days is not a filer recording a boundary inconsistently; it is a different period. Admitting
    it would let a stub period claim a year's spine slot, which is how a fiscal-year change comes to
    report full coverage.
    """
    filings = filing_rows(("10-K", "2023-02-20", "2022-12-27"))
    coverage = history("NOQ4.trimmed.json", filings=filings).coverage.annual[Metric.REVENUE]
    assert coverage.expected == 1
    assert coverage.filled == 0
    assert coverage.periods_outside_spine >= 1, "kept in the series, and counted"


@pytest.mark.spec
def test_matching_is_one_to_one() -> None:
    """Two facts a day apart cannot both claim one spine date.

    Without the constraint, `filled` runs past `expected` and the fill rate exceeds 100% — the bug the
    bound is supposed to make impossible. `NOQ4` reports FY2022 and FY2023 a year apart, so the
    violation is attempted by giving the spine **one** annual date and letting two facts compete for
    it: at most one may win.
    """
    filings = filing_rows(("10-K", "2023-02-20", "2022-12-31"))
    result = history("NOQ4.trimmed.json", filings=filings)
    coverage = result.coverage.annual[Metric.REVENUE]
    assert len(result.annual[Metric.REVENUE]) == 2, "both years are in the series"
    assert coverage.expected == 1
    assert coverage.filled == 1, "one slot, one claim"


@pytest.mark.spec
def test_two_facts_within_tolerance_of_one_slot_fill_it_once() -> None:
    """The same rule where the competition is genuinely ambiguous, which is where it bites.

    Two quarterly ends a day apart against a single quarterly spine date: the nearest one wins the
    slot and the other is left unmatched. The assertion is the inequality — `filled <= expected` — not
    which fact won, because the tie-break is not the guarantee; the bound is.
    """
    filings = filing_rows(("10-Q", "2024-08-02", "2024-06-29"))
    result = history("YTDONLY.trimmed.json", filings=filings)
    coverage = result.coverage.quarterly[Metric.REVENUE]
    assert coverage.expected == 1
    assert coverage.filled <= coverage.expected


@pytest.mark.spec
def test_periods_outside_spine_do_not_inflate_fill_rate() -> None:
    """Coverage can never exceed 100%, and the count is what tells you the bound is doing work.

    A period whose end matches no spine date is real data — usually a fiscal-year change, or a report
    date amended after the fact. It stays in the series, does not contribute to the numerator, and is
    counted. Asserted over every metric and both buckets on every fixture, because the bound is a
    property of the arithmetic rather than of any one payload.
    """
    fixtures = (
        "AAPL.trimmed.json",
        "TIER2.trimmed.json",
        "NOQ4.trimmed.json",
        "YTDONLY.trimmed.json",
        "STUBYEAR.trimmed.json",
        "NCI.trimmed.json",
    )
    for fixture in fixtures:
        result = history(fixture, filings=filing_rows(("10-K", "2023-02-20", "2022-12-31")))
        for bucket in (Bucket.ANNUAL, Bucket.QUARTERLY):
            for coverage in result.coverage.for_bucket(bucket).values():
                assert coverage.filled <= coverage.expected, (fixture, bucket, coverage.metric)
                rate = coverage.fill_rate
                assert rate is None or rate <= 1


@pytest.mark.spec
def test_a_fact_outside_the_spine_stays_in_the_series() -> None:
    """Not dropped — dropping it would make the series look clean and the report look complete.

    `STUBYEAR`'s 53-week year ends on 2023-03-07, which no ordinary annual spine date is near. The
    figure is real and the filer reported it; what it is not is evidence of a period the filing history
    accounts for, so it is printed and excluded from the numerator.
    """
    filings = filing_rows(("10-K", "2022-03-01", "2021-12-31"))
    result = history("STUBYEAR.trimmed.json", filings=filings)
    ends = {fact.period.end for fact in result.annual[Metric.REVENUE]}
    assert date(2023, 3, 7) in ends
    coverage = result.coverage.annual[Metric.REVENUE]
    assert coverage.expected == 1
    assert coverage.periods_outside_spine == 1


# ---------------------------------------------------------------------------
# the labelled fallback
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_no_periodic_filing_yields_observed_origin() -> None:
    """A circular coverage denominator cannot be printed unlabelled.

    `NOPERIODIC` has facts filed on an `S-1/A` and no `10-K` or `10-Q` at all, so there is a numerator
    and no denominator. The spine then falls back to the period ends the facts themselves carry, which
    is circular — a filer that tags one thing reports 100% of one thing — and `origin` is a field
    precisely so that number can never be printed without the qualification.
    """
    profile, filings = submissions("NOPERIODIC.json", cik=1000052)
    result = history("NOPERIODIC.trimmed.json", cik=1000052, profile=profile, filings=filings)
    spine = result.coverage.spine

    assert spine.origin is SpineOrigin.OBSERVED
    assert not spine.is_empty, "the fallback has to produce a denominator, or coverage is undefined"
    assert result.coverage.findings_for("spine_observed"), "labelled in the findings too"
    assert result.sic is not None, "the profile is present; only the periodic filings are missing"


@pytest.mark.spec
def test_a_filings_spine_is_preferred_even_when_it_is_smaller() -> None:
    """One 10-K beats twenty observed period ends, because the observed set is circular.

    This is the assertion that stops the fallback becoming the normal path: an implementation that
    unioned the two, or that preferred whichever was larger, would produce a bigger denominator and a
    *worse* number — and it would report `FILINGS` while measuring against something else.
    """
    filings = filing_rows(("10-K", "2023-02-20", "2022-12-31"))
    result = history("TIER2.trimmed.json", filings=filings)
    assert result.coverage.spine.origin is SpineOrigin.FILINGS
    assert result.coverage.spine.annual_ends == (date(2022, 12, 31),)
    assert not result.coverage.findings_for("spine_observed")


@pytest.mark.spec
def test_an_empty_payload_and_no_filings_still_produce_a_spine_object() -> None:
    """Both absences at once, which is a documented exit-0 outcome of `facts`.

    An empty history over an `OBSERVED` spine of nothing: every `expected` is zero, every `fill_rate`
    is `None`, and the two findings say why. Returning `None` for the spine instead would push the
    same three-way check into the renderer and into M4.
    """
    result = history(None, profile=None, filings=())
    spine = result.coverage.spine
    assert spine.origin is SpineOrigin.OBSERVED
    assert spine.is_empty
    assert all(entry.expected == 0 for entry in result.coverage.annual.values())
    assert all(entry.fill_rate is None for entry in result.coverage.annual.values())
    codes = {finding.code for finding in result.coverage.findings}
    assert {"companyfacts_absent", "submissions_absent", "spine_observed"} <= codes


# ---------------------------------------------------------------------------
# the object itself
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_spine_dates_are_sorted_and_unique() -> None:
    """Deduped on `(kind, report_date)`, and ordered, because the document serializes them.

    Two filings for one period end must produce one entry — the amendment rule — and the order has to
    be a function of the dates rather than of the filing order, or `report.json` differs between two
    runs over one cache for a reason that is not a data change.
    """
    spine = _spine(
        ("10-K", "2024-02-20", "2023-12-31"),
        ("10-K", "2023-02-20", "2022-12-31"),
        ("10-K/A", "2023-08-01", "2022-12-31"),
        ("10-Q", "2023-05-04", "2023-03-31"),
    )
    assert spine.annual_ends == (date(2022, 12, 31), date(2023, 12, 31))
    assert spine.quarterly_ends == tuple(sorted(set(spine.quarterly_ends)))


def test_ends_for_answers_the_bucket_it_was_asked() -> None:
    """A one-line accessor, tested because every coverage computation goes through it.

    Swapping the two arms is a two-character edit that makes every annual metric measured against the
    quarterly denominator — coverage around 25% across the board, on perfect data.
    """
    spine = _spine(
        ("10-K", "2023-02-20", "2022-12-31"),
        ("10-Q", "2023-05-04", "2023-03-31"),
    )
    assert spine.ends_for(Bucket.ANNUAL) == (date(2022, 12, 31),)
    assert spine.ends_for(Bucket.QUARTERLY) == (date(2022, 12, 31), date(2023, 3, 31))


def test_an_empty_filing_list_is_not_an_error() -> None:
    """`build_spine(())` is a normal call: a 404 on submissions leaves no filings at all."""
    spine = build_spine((), window=M2_WINDOW)
    assert spine.is_empty
    assert spine.origin is SpineOrigin.FILINGS, "build_spine does not decide the fallback"

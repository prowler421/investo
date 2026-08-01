"""Assembly and measurement: ``FinancialHistory``, the period spine, and what is missing.

DESIGN.md §3.2 is normative on both types this module produces. The measurement half is the more
consequential one: §4.2's closing argument is that coverage *"below a configurable floor degrades the
report's confidence rating and can trigger an 'insufficient data' verdict"*, so the coverage number
feeds §9.2's confidence rating directly — and a coverage figure with an unstated denominator is a
confidence rating with an unstated meaning.

**The denominator is a period spine derived from the filing history** (§3.2). Of the three plausible
choices, only that one measures what the number is supposed to mean:

===============================================  ==========================================
Denominator                                      Problem
===============================================  ==========================================
Periods in the requested window                  A company that IPO'd two years into a 5-year
                                                 window reports ~40% coverage on perfect data,
                                                 so coverage measures company age.
Periods for which *any* metric has a fact        Circular. A filer that tags nothing reports
                                                 100% of nothing.
**Periods the company actually reported**        —
===============================================  ==========================================

The third is also the only one independent of the facts, which is what stops a tagging failure from
shrinking its own denominator. Where a filer has no periodic filing in the window at all, the spine
falls back to :attr:`SpineOrigin.OBSERVED` — the circular denominator — and that fallback is
**labelled everywhere it is printed**, because a 100% figure that quietly came from an ``OBSERVED``
spine is the single most misleading number this milestone could produce.

**Two gates are recorded here and enforced nowhere.** §6.10 refuses a valuation for banks, insurers
and REITs; §5.1 refuses one below 12 quarters of history. Both decisions are M4's and M5's, and M2
must not make the call — a refusal reached inside normalization is a refusal with **no report
attached**, while §6.10's whole argument is that *"a blank space with an explanation beats a
confident wrong number"* and the explanation is a rendered section. §14 says the same thing in the
exit-code taxonomy: exit 3 is "insufficient data, *report still written*".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from investo.domain.models import Fact, Metric, Money, RawFact
from investo.domain.periods import ANNUAL_DAYS, FiscalPeriod, PeriodKind
from investo.domain.provenance import Derivation, Provenance
from investo.ingest.edgar.companyfacts import CompanyFacts
from investo.ingest.edgar.submissions import CompanyProfile, FilingRow
from investo.normalize.facts import (
    Q4_RULE,
    SEAM_TOLERANCE,
    MetricSeries,
    Restatement,
    SeriesPart,
    dedup_all,
    fact_sort_key,
    filter_as_of,
    in_window,
    normalize_metric,
    observed_calendar,
)
from investo.normalize.tags import (
    CHAINS,
    DERIVATIONS,
    Aggregation,
    DerivationKind,
    DerivedMetric,
    Tier,
    chain_for,
    derive,
    identity,
    metrics_in_tier,
    unit_filter,
    uses_including_nci_net_income,
)

__all__ = [
    "ANNUAL_FORMS",
    "QUARTERLY_FORMS",
    "FINDING_CODES",
    "Bucket",
    "SpineOrigin",
    "PeriodSpine",
    "Finding",
    "MetricCoverage",
    "CoverageReport",
    "FinancialHistory",
    "build_spine",
    "build_history",
]

ANNUAL_FORMS: Final = frozenset({"10-K", "10-KT"})
QUARTERLY_FORMS: Final = frozenset({"10-Q", "10-QT"})
"""The periodic forms the spine is built from. Amendments collapse into the form they amend."""

FINDING_CODES: Final = (
    "coverage_below_floor",
    "q4_derived",
    "q4_absent",
    "series_stitched",
    "restated",
    "window_truncated",
    "companyfacts_absent",
    "submissions_absent",
    "spine_observed",
    "spine_date_inexact",
    "exclusivity_switch",
    "net_income_scope_mismatch",
    "liabilities_nci_approximated",
    "sga_composed",
    "sign_anomaly",
    "unit_mismatch",
    "other_bucket_drops",
    "periods_outside_spine",
)
"""Every code M2 can emit, in ``docs/m2/03-statements.md`` §4's order.

Declared as a tuple so ``test_findings`` can assert one test per code exists rather than trusting
that the suite happens to cover them. **No severity attaches to any of them** — §6.2 gives severity
to ``analysis/flags.py``'s registry, one rule per file with its own test, and a severity assigned in
``normalize/`` is a severity assigned twice. The two copies diverge on the first rule M4 tunes.
"""


class Bucket(StrEnum):
    """Which of the two series a figure belongs to."""

    ANNUAL = "annual"
    QUARTERLY = "quarterly"


class SpineOrigin(StrEnum):
    """Where the coverage denominator came from."""

    FILINGS = "filings"
    """Derived from the filing history — the normal case."""

    OBSERVED = "observed"
    """Fallback: the union of period ends across the resolved facts. **Circular**, and the coverage
    report and the ``facts`` output both print it for that reason."""


@dataclass(frozen=True, slots=True)
class PeriodSpine:
    """The periods the company reported in the window — coverage's denominator.

    Built from :attr:`~investo.ingest.edgar.submissions.FilingRow.report_date`, **not** ``filed``:
    the report date is the period end, the filing date is two months later, and using the wrong one
    shifts the whole spine by a quarter.

    Four construction rules, each wrong in a specific way if omitted — see :func:`build_spine`.

    A spine can be empty in one bucket and populated in the other, and **that is the common case,
    not the edge**: ``ARXS``'s submissions payload holds one ``10-Q`` and no ``10-K`` at all, so its
    annual ``expected`` is zero while its quarterly ``expected`` is one. Annual coverage is then
    ``None`` — not 0% and not 100% — which is the whole reason :attr:`MetricCoverage.fill_rate` is
    optional: a recent registrant that has filed one quarterly report has not *failed* to be tagged.
    """

    annual_ends: tuple[date, ...] = ()
    quarterly_ends: tuple[date, ...] = ()
    origin: SpineOrigin = SpineOrigin.FILINGS

    def ends_for(self, bucket: Bucket) -> tuple[date, ...]:
        return self.annual_ends if bucket is Bucket.ANNUAL else self.quarterly_ends

    @property
    def is_empty(self) -> bool:
        return not self.annual_ends and not self.quarterly_ends


@dataclass(frozen=True, slots=True)
class Finding:
    """Something true about the data, with no opinion about what it means.

    Attributes:
        code: Stable and machine-readable; the ``report.json`` key. One of :data:`FINDING_CODES`.
        metric: The metric it concerns, or ``None`` for a company-level finding.
        detail: Human-readable, printed by ``facts`` and in §9.1's caveats.
        evidence: Provenance, where naming a filing makes the finding checkable.
    """

    code: str
    metric: Metric | None
    detail: str
    evidence: tuple[Provenance, ...] = ()


@dataclass(frozen=True, slots=True)
class MetricCoverage:
    """One metric's coverage in one bucket, and every count behind it.

    §3.2 asks for "which metrics, which tag won, % filled"; §9.1's appendix asks for tag provenance
    per metric; ROADMAP M2's exit criterion is stated *per tier*. So the report is per-metric with
    tier aggregates rather than a single number.
    """

    metric: Metric
    tags_used: tuple[str, ...] = ()
    """Qualified, in first-use order. **A tuple because a stitch is normal** — a single "which tag
    won" field cannot represent Apple's revenue, and ``len(tags_used) > 1`` is the stitch finding.

    **Empty means the chain matched nothing**, which is not the same as the metric being absent: a
    metric filled entirely by a cross-metric derivation reads ``tags_used=()`` beside
    ``derived_periods=2``, because a derivation names its own tags and composing them into this field
    would make it mean "tags involved" rather than "chain members that won". The tags are on the
    ``Derivation`` either way, which is what ``report.json``'s ``sources`` array prints; the pair of
    counts is what says which happened.
    """
    derived_periods: int = 0
    """From a cross-metric derivation."""
    recovered_periods: int = 0
    """From Q4 or YTD residual recovery."""
    filled: int = 0
    expected: int = 0
    periods_outside_spine: int = 0
    spine_date_inexact: int = 0
    """Facts that matched a spine date within tolerance but not exactly. **[extends §3.2's field
    list]** — ``docs/m2/03-statements.md`` requires inexact matches to be counted per metric, and
    there was no field for it: one or two is ordinary, while a filer where every period matches
    inexactly has a systematic disagreement between its filing header and its own XBRL contexts."""
    dropped_other_bucket: int = 0
    dropped_unit_mismatch: int = 0
    dropped_ytd_redundant: int = 0
    """YTD facts the filer also reported discretely. **[extends §3.2's field list]** —
    ``docs/m2/02-facts.md`` §7 requires this count to appear in the coverage report so the population
    is visible without anyone having to act on it, and there was no field for it."""
    dropped_ytd_unusable: int = 0
    """YTD facts that recovered nothing because an earlier rung of their ladder is missing. Separate
    from the above: a hole in the ladder is a different fact about the filer than a redundant figure,
    and a fact in no bucket and no counter is one the coverage report cannot mention."""
    sign_anomalies: int = 0

    @property
    def fill_rate(self) -> Decimal | None:
        """``filled / expected``, or ``None`` when nothing was expected.

        **``None``, not ``0`` and not ``1``.** Both defaults are lies in opposite directions and both
        are the kind of lie that propagates into a weighted mean: §9.2's confidence rating averages
        over metrics, a metric with no expected periods must be excluded from that average, and
        ``None`` is what makes excluding it the only thing a caller can do.
        """
        if self.expected == 0:
            return None
        return Decimal(self.filled) / Decimal(self.expected)


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Per-metric coverage for both buckets, the spine it was measured against, and the findings."""

    spine: PeriodSpine
    annual: Mapping[Metric, MetricCoverage]
    quarterly: Mapping[Metric, MetricCoverage]
    findings: tuple[Finding, ...] = ()

    def for_bucket(self, bucket: Bucket) -> Mapping[Metric, MetricCoverage]:
        return self.annual if bucket is Bucket.ANNUAL else self.quarterly

    def tier_fill_rate(self, tier: Tier, bucket: Bucket) -> Decimal | None:
        """The unweighted mean fill rate over a tier's metrics, or ``None`` if none is measurable.

        Metrics whose :attr:`MetricCoverage.fill_rate` is ``None`` are **excluded from the mean**,
        not counted as zero — see that property. Unweighted because ROADMAP M2's criterion is stated
        over the metric set, not over periods, and a weighting by expected periods would let a filer
        with one 10-K and twenty 10-Qs report a quarterly-dominated "tier" figure.
        """
        rates = [
            rate
            for rate in (
                coverage.fill_rate
                for metric, coverage in self.for_bucket(bucket).items()
                if metric in set(metrics_in_tier(tier))
            )
            if rate is not None
        ]
        if not rates:
            return None
        return sum(rates, Decimal(0)) / Decimal(len(rates))

    def findings_for(self, code: str) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.code == code)


@dataclass(frozen=True, slots=True)
class FinancialHistory:
    """One company's normalized statements, plus what it cost to produce them.

    §3.2's sketch, with five departures each of which has a consumer that does not work without it —
    all recorded in ``docs/m2/README.md`` § Spec question 3 and folded into DESIGN.md §3.2:

    - **``Mapping[Metric, tuple[Fact, ...]]``, not ``dict[Metric, list[Fact]]``.** A frozen dataclass
      whose fields are a mutable dict of mutable lists is frozen in name only, and ``report.json``
      and the determinism gate both need the series to be unable to change between being built and
      being serialized.
    - **``name``, ``sic``, ``sic_description``** because the consumers are downstream of
      normalization and would otherwise each have to carry ``CompanyProfile`` alongside. ``sic`` in
      particular is read by three separate things — §6.10's refusal, §6.1's Altman variant, §6.5's
      peer cohort — and threading it separately is how one of them ends up reading a different value.
      **The display name comes from submissions, never from ``companyfacts.entityName``**, which is
      EDGAR-conformed uppercase.
    - **``window`` and ``quarters_available``** because §5.1 gates on quarters of history at two
      thresholds and §6.4 lists "lookback shorter than requested" as a flag. Both need the requested
      window and the delivered one to be comparable, and only the object that applied the window
      knows both.
    - **``restatements``**, so ROADMAP open question 10 stays answerable without a re-parse.
    - **``market_cap``** carried through from M1 rather than recomputed. §9.1's section 3 prints it
      against peer percentiles, and there is nowhere else for it to live that M3 can reach without
      also reaching into ``FetchResult``.

    **Not here: ``manifest_hash``, config, prompt versions.** §9.1's appendix prints all three and
    they are run metadata rather than financial history — they belong to ``report.json``'s envelope.
    Putting a cache fingerprint inside a ``FinancialHistory`` would make two histories built from the
    same facts compare unequal.
    """

    cik: int
    ticker: str
    name: str
    fiscal_year_end: str | None
    """``"MMDD"``, from ``CompanyProfile``. ``None`` when the submissions payload 404'd — there is no
    honest value to invent for a filer that never stated one."""
    sic: int | None
    sic_description: str | None
    annual: Mapping[Metric, tuple[Fact, ...]]
    quarterly: Mapping[Metric, tuple[Fact, ...]]
    coverage: CoverageReport
    as_of: date
    window: tuple[date, date]
    quarters_available: int
    restatements: tuple[Restatement, ...] = ()
    market_cap: tuple[Money, Derivation] | None = None

    def series(self, metric: Metric, bucket: Bucket) -> tuple[Fact, ...]:
        source = self.annual if bucket is Bucket.ANNUAL else self.quarterly
        return source.get(metric, ())


# ---------------------------------------------------------------------------
# the spine
# ---------------------------------------------------------------------------
def build_spine(filings: Sequence[FilingRow], *, window: tuple[date, date]) -> PeriodSpine:
    """The periods this company reported inside ``window``.

    Four construction rules, each of which is wrong in a specific way if omitted:

    1. **Amendments collapse into the filing they amend.** ``10-K/A`` matches :data:`ANNUAL_FORMS`
       after stripping the suffix, and the spine is deduped on ``(kind, report_date)``. Without this,
       a filer that amended two years of 10-Ks has an annual denominator two larger than the number
       of years it existed, and coverage caps out around 66%.
    2. **Annual report dates are also quarterly spine entries.** A filer files three 10-Qs a year;
       the fourth quarter's end date appears only on the 10-K. A quarterly denominator built from
       10-Qs alone is three per year, and any filer whose Q4s were derived reports 133% coverage.
    3. **A row with ``report_date is None`` contributes nothing.** The ``ARXS`` fixture carries
       ``reportDate: ""`` on several rows, normalized to ``None`` by M1's ``_fields.as_date``. Those
       filings are still in the list; they are just not spine evidence.
    4. **The spine is windowed on ``report_date``, using the same window the facts are.** Otherwise
       numerator and denominator are measured over different intervals, which produces coverage above
       100% at the near edge and below it at the far edge for filers whose fiscal year ends near the
       boundary.
    """
    start, end = window
    annual: set[date] = set()
    quarterly: set[date] = set()
    for row in filings:
        report_date = row.report_date
        if report_date is None or not (start <= report_date <= end):
            continue
        form = row.form.split("/", 1)[0].strip().upper()
        if form in ANNUAL_FORMS:
            annual.add(report_date)
            quarterly.add(report_date)
        elif form in QUARTERLY_FORMS:
            quarterly.add(report_date)
    return PeriodSpine(
        annual_ends=tuple(sorted(annual, key=identity)),
        quarterly_ends=tuple(sorted(quarterly, key=identity)),
        origin=SpineOrigin.FILINGS,
    )


def _match_one_to_one(
    spine_ends: Sequence[date], fact_ends: Sequence[date]
) -> tuple[int, int, int]:
    """Match fact period ends to spine dates: nearest within tolerance, **one-to-one**.

    Returns:
        ``(filled, inexact, outside)`` — slots claimed, claims that were not date-exact, and fact
        ends within tolerance of no spine date at all.

    The spine is ``FilingRow.report_date``, from the filing header; a fact's ``period.end`` comes
    from the XBRL context in the instance document. They are *usually* the same date and they are not
    the same field, so an exact-equality match would silently **undercount** coverage on any filer
    whose two disagree by a day: the fact is present, the metric is tagged, and the report says it is
    missing. That is the same "wrong quietly" shape the rest of this module refuses, arriving in the
    one number that gates the milestone.

    **One-to-one matters.** Without it, two facts a day apart both satisfy one spine date and push
    ``filled`` past ``expected`` — which is the bug the 100% bound is supposed to make impossible.
    Three days is safe at both granularities by a wide margin: annual spine dates are a year apart
    and quarterly ones about ninety days, so the nearest match is never ambiguous.
    """
    candidates = sorted(
        (
            (abs(fact_end - spine_end), spine_end, fact_end)
            for spine_end in spine_ends
            for fact_end in fact_ends
            if abs(fact_end - spine_end) <= SEAM_TOLERANCE
        ),
        key=lambda item: (item[0], item[1], item[2]),
    )
    claimed_slots: set[date] = set()
    claimed_facts: set[date] = set()
    filled = 0
    inexact = 0
    for distance, spine_end, fact_end in candidates:
        if spine_end in claimed_slots or fact_end in claimed_facts:
            continue
        claimed_slots.add(spine_end)
        claimed_facts.add(fact_end)
        filled += 1
        if distance > timedelta(0):
            inexact += 1
    outside = sum(
        1
        for fact_end in set(fact_ends)
        if all(abs(fact_end - spine_end) > SEAM_TOLERANCE for spine_end in spine_ends)
    )
    return filled, inexact, outside


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
def build_history(
    facts: CompanyFacts | None,
    *,
    ticker: str,
    cik: int,
    name: str,
    profile: CompanyProfile | None,
    filings: Sequence[FilingRow] = (),
    window: tuple[date, date],
    as_of: date | None = None,
    market_cap: tuple[Money, Derivation] | None = None,
    coverage_floor: Decimal | None = None,
) -> FinancialHistory:
    """Normalize a company's facts into a :class:`FinancialHistory`. Pure — no I/O, no clock.

    Every argument is something ``FetchResult`` already holds, so the ``facts`` command is
    ``run_fetch`` followed by this function with no second fetch path to keep in sync. That is the
    reason the signature takes parsed objects rather than a ticker: a normalization layer that *can*
    fetch is one that will, and then a warm run makes an HTTP call.

    **Both payloads are optional, because M1 already makes them optional**, and the two absences stay
    independent because they degrade differently:

    ==================  =========================================================================
    Absent              Consequence
    ==================  =========================================================================
    ``facts``           Every metric absent. A history is still returned — with a spine, since the
                        filings are unaffected — and a ``companyfacts_absent`` finding.
    ``profile``         No ``sic``, no ``sic_description``, no ``fiscal_year_end``. The series are
                        unaffected.
    both                An empty history over an ``OBSERVED`` spine of nothing: a table of dashes,
                        two findings, and exit 0.
    ==================  =========================================================================

    Returning an empty history rather than raising is §6.10's argument applied one layer down: *"a
    blank space with an explanation beats a confident wrong number"*, and an exception is not a blank
    space with an explanation, it is a traceback.

    Args:
        facts: Parsed ``companyfacts``, or ``None`` when SEC published none for this CIK.
        ticker: The resolved symbol.
        cik: From the ticker row, so it survives a submissions 404.
        name: Likewise. When ``profile`` is present its name wins — M1's rule that the display name
            comes from submissions is about ``companyfacts.entityName`` being EDGAR-conformed
            uppercase, and the ticker file is not.
        profile: Parsed submissions metadata, or ``None`` on a 404.
        filings: The filing history, for the spine.
        window: ``(start, as_of)``, computed once at the command boundary.
        as_of: Point-in-time cut. ``None`` means no filtering; the recorded
            :attr:`FinancialHistory.as_of` then comes from ``window[1]``, which *is* the resolved
            as-of date by construction — nothing here reads a clock to invent one.
        market_cap: M1's figure, carried through rather than recomputed.
        coverage_floor: Fill rate below which ``coverage_below_floor`` fires. ``None`` disables it.
            **Deliberately not a config field yet**: §4.2 calls for "a configurable floor" and
            ``docs/m2/05-testing.md`` §8 records that what the floor *should be* is answerable only
            from the coverage measurement — picking one now would be picking the number that makes
            today's fixtures pass.
    """
    payload: Mapping[tuple[str, str], Sequence[RawFact]] = facts.facts if facts is not None else {}
    spine = build_spine(filings, window=window)
    every_key = tuple({key for chain in CHAINS.values() for key in chain.keys})
    observed_annual, observed_quarterly = observed_calendar(
        payload, every_key, window=window, as_of=as_of
    )
    if spine.is_empty:
        # The circular denominator, reached only by a filer with no periodic filing of either kind in
        # the window — a registrant whose only forms are `S-1/A` and `8-K`. Labelled, not silent.
        spine = PeriodSpine(
            annual_ends=observed_annual,
            quarterly_ends=observed_quarterly,
            origin=SpineOrigin.OBSERVED,
        )

    calendar_annual = tuple(sorted({*spine.annual_ends, *observed_annual}, key=identity))
    calendar_quarterly = tuple(
        sorted({*spine.quarterly_ends, *observed_quarterly, *calendar_annual}, key=identity)
    )

    series: dict[Metric, MetricSeries] = {
        metric: normalize_metric(
            chain,
            payload,
            window=window,
            as_of=as_of,
            annual_ends=calendar_annual,
            quarterly_ends=calendar_quarterly,
        )
        for metric, chain in CHAINS.items()
    }

    annual = {metric: item.annual.facts for metric, item in series.items()}
    quarterly = {metric: item.quarterly.facts for metric, item in series.items()}
    derived_counts, approximated = _apply_derivations(
        annual,
        quarterly,
        payload,
        window=window,
        as_of=as_of,
        calendar_annual=calendar_annual,
        calendar_quarterly=calendar_quarterly,
    )

    coverage_annual = {
        metric: _coverage_for(
            series[metric],
            Bucket.ANNUAL,
            spine,
            derived=derived_counts.get((metric, Bucket.ANNUAL), 0),
            final=annual.get(metric, ()),
        )
        for metric in series
    }
    coverage_quarterly = {
        metric: _coverage_for(
            series[metric],
            Bucket.QUARTERLY,
            spine,
            derived=derived_counts.get((metric, Bucket.QUARTERLY), 0),
            final=quarterly.get(metric, ()),
        )
        for metric in series
    }

    report = CoverageReport(
        spine=spine,
        annual=coverage_annual,
        quarterly=coverage_quarterly,
        findings=_findings(
            series,
            annual=annual,
            quarterly=quarterly,
            coverage_annual=coverage_annual,
            coverage_quarterly=coverage_quarterly,
            spine=spine,
            window=window,
            facts_absent=facts is None,
            profile_absent=profile is None,
            approximated=approximated,
            coverage_floor=coverage_floor,
        ),
    )

    return FinancialHistory(
        cik=cik,
        ticker=ticker,
        name=profile.name if profile is not None and profile.name else name,
        fiscal_year_end=profile.fiscal_year_end if profile is not None else None,
        sic=profile.sic if profile is not None else None,
        sic_description=profile.sic_description if profile is not None else None,
        annual=dict(annual),
        quarterly=dict(quarterly),
        coverage=report,
        as_of=as_of if as_of is not None else window[1],
        window=window,
        quarters_available=_quarters_available(quarterly),
        restatements=tuple(
            record for metric in sorted(series, key=str) for record in series[metric].restatements
        ),
        market_cap=market_cap,
    )


# ---------------------------------------------------------------------------
# step 7 — cross-metric derivation
# ---------------------------------------------------------------------------
def _apply_derivations(
    annual: dict[Metric, tuple[Fact, ...]],
    quarterly: dict[Metric, tuple[Fact, ...]],
    payload: Mapping[tuple[str, str], Sequence[RawFact]],
    *,
    window: tuple[date, date],
    as_of: date | None,
    calendar_annual: Sequence[date],
    calendar_quarterly: Sequence[date],
) -> tuple[Mapping[tuple[Metric, Bucket], int], tuple[Metric, ...]]:
    """Run :data:`~investo.normalize.tags.DERIVATIONS` in order, mutating the two series maps.

    Per period, not per series: a filer that tags ``GrossProfit`` in three years of four gets the
    fourth derived and the other three as filed, and the coverage report distinguishes them. The
    declared order is what makes a chained derivation work — ``EPS_DILUTED`` reads a ``NET_INCOME``
    that may itself be a recovered Q4, and the ``Derivation`` nests rather than flattening.

    Returns:
        ``(derived counts per (metric, bucket), metrics whose derivation used a fallback)``.
    """
    counts: dict[tuple[Metric, Bucket], int] = {}
    approximated: list[Metric] = []
    for spec in DERIVATIONS:
        raw = _prepare_raw(spec, payload, window=window, as_of=as_of)
        for bucket, store, calendar in (
            (Bucket.ANNUAL, annual, calendar_annual),
            (Bucket.QUARTERLY, quarterly, calendar_quarterly),
        ):
            existing = {(fact.period.end, fact.period.kind) for fact in store.get(spec.metric, ())}
            candidates = [
                period
                for period in _candidate_periods(spec, store, calendar)
                if (period.end, period.kind) not in existing
            ]
            if not candidates:
                continue
            resolved = {
                metric: {
                    (fact.period.end, fact.period.kind): fact for fact in store.get(metric, ())
                }
                for metric in spec.metric_inputs
            }
            produced = derive(spec, resolved=resolved, raw=raw, periods=candidates)
            if not produced:
                continue
            store[spec.metric] = tuple(
                sorted(
                    (*store.get(spec.metric, ()), *(fact for fact, _ in produced)),
                    key=fact_sort_key,
                )
            )
            counts[(spec.metric, bucket)] = len(produced)
            if any(flag for _, flag in produced) and spec.metric not in approximated:
                approximated.append(spec.metric)
    return counts, tuple(approximated)


def _prepare_raw(
    spec: DerivedMetric,
    payload: Mapping[tuple[str, str], Sequence[RawFact]],
    *,
    window: tuple[date, date],
    as_of: date | None,
) -> Mapping[tuple[str, str], Sequence[RawFact]]:
    """Filter, unit-check and dedup the tags a :attr:`DerivationKind.TAG_DIFFERENCE` reads.

    The same first four pipeline steps the chains get, applied to two tags no chain names. Skipping
    them would let the one derivation that reaches outside the registry also escape the ``as_of``
    cut, which is exactly the kind of second path ``docs/m2/02-facts.md`` § pipeline order warns
    about.
    """
    if spec.kind is not DerivationKind.TAG_DIFFERENCE:
        return {}
    chain = chain_for(spec.metric)
    members = [*spec.tag_inputs]
    if spec.fallback_subtrahend is not None:
        members.append(spec.fallback_subtrahend)
    prepared: dict[tuple[str, str], Sequence[RawFact]] = {}
    for member in members:
        for key in member.keys:
            kept, _ = unit_filter(chain, filter_as_of(payload.get(key, ()), as_of=as_of))
            survivors, _ = dedup_all(fact for fact in kept if in_window(fact.period, window))
            if survivors:
                prepared[key] = survivors
    return prepared


def _candidate_periods(
    spec: DerivedMetric,
    store: Mapping[Metric, tuple[Fact, ...]],
    calendar: Sequence[date],
) -> tuple[FiscalPeriod, ...]:
    """Periods a derivation could fire on, in this bucket.

    For the metric forms, the periods its inputs actually have — a derivation over a period an input
    is missing is not attempted anyway, so anything wider is wasted work. For the tag form the inputs
    are outside the registry, so the candidates come from the bucket's other instants plus the
    bucketing calendar: a filer whose *only* balance-sheet tags are the two this derivation names
    would otherwise have no candidate period at all.
    """
    if spec.metric_inputs:
        unique: dict[tuple[date, PeriodKind], FiscalPeriod] = {}
        for metric in spec.metric_inputs:
            for fact in store.get(metric, ()):
                unique.setdefault((fact.period.end, fact.period.kind), fact.period)
        return tuple(
            unique[key] for key in sorted(unique, key=lambda pair: (pair[0], str(pair[1])))
        )

    chain = chain_for(spec.metric)
    if chain.aggregation is not Aggregation.INSTANT:
        return ()
    ends = {
        fact.period.end
        for series in store.values()
        for fact in series
        if fact.period.kind is PeriodKind.INSTANT
    }
    ends.update(calendar)
    return tuple(
        FiscalPeriod(end=end, kind=PeriodKind.INSTANT) for end in sorted(ends, key=identity)
    )


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------
def _coverage_for(
    item: MetricSeries,
    bucket: Bucket,
    spine: PeriodSpine,
    *,
    derived: int,
    final: Sequence[Fact],
) -> MetricCoverage:
    """Measure one metric in one bucket against the spine.

    ``final`` is the series **after** cross-metric derivation, not
    :attr:`~investo.normalize.facts.MetricSeries`'s own part. A metric whose every period was derived
    — total liabilities for a filer that never tags ``Liabilities`` — would otherwise report
    ``filled=0`` beside ``derived_periods=2``, which is a coverage report contradicting the series
    printed above it. The other counts come from the part, because they describe what resolution did.
    """
    part: SeriesPart = item.annual if bucket is Bucket.ANNUAL else item.quarterly
    spine_ends = spine.ends_for(bucket)
    filled, inexact, outside = _match_one_to_one(spine_ends, [f.period.end for f in final])
    return MetricCoverage(
        metric=item.metric,
        tags_used=item.tags_used,
        derived_periods=derived,
        recovered_periods=part.recovered,
        filled=filled,
        expected=len(spine_ends),
        periods_outside_spine=outside,
        spine_date_inexact=inexact,
        dropped_other_bucket=item.dropped_other,
        dropped_unit_mismatch=item.dropped_unit_mismatch,
        dropped_ytd_redundant=item.dropped_ytd_redundant,
        dropped_ytd_unusable=item.dropped_ytd_unusable,
        sign_anomalies=part.sign_anomalies,
    )


def _quarters_available(quarterly: Mapping[Metric, tuple[Fact, ...]]) -> int:
    """Distinct quarter ends across every metric's quarterly series.

    Counts **durations only**. An instant lands in the quarterly bucket whenever its date matches no
    annual calendar entry — a cover-page share count dated three weeks after the quarter end is the
    common case — and counting one as a quarter of history would push a filer over §5.1's 12-quarter
    threshold on a fact that is not a quarter of anything.
    """
    return len(
        {
            fact.period.end
            for facts in quarterly.values()
            for fact in facts
            if fact.period.kind is PeriodKind.QUARTER
        }
    )


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------
def _findings(
    series: Mapping[Metric, MetricSeries],
    *,
    annual: Mapping[Metric, tuple[Fact, ...]],
    quarterly: Mapping[Metric, tuple[Fact, ...]],
    coverage_annual: Mapping[Metric, MetricCoverage],
    coverage_quarterly: Mapping[Metric, MetricCoverage],
    spine: PeriodSpine,
    window: tuple[date, date],
    facts_absent: bool,
    profile_absent: bool,
    approximated: Sequence[Metric],
    coverage_floor: Decimal | None,
) -> tuple[Finding, ...]:
    """Every finding this run supports, in :data:`FINDING_CODES` order within each scope.

    Company-level findings come first because that is the order ``facts`` prints them and the order a
    reader needs them in: a coverage figure measured against an ``OBSERVED`` spine has to be qualified
    before any per-metric number below it means anything.
    """
    found: list[Finding] = []

    if facts_absent:
        found.append(
            Finding(
                code="companyfacts_absent",
                metric=None,
                detail=(
                    "SEC published no XBRL facts for this CIK, so every metric is absent. "
                    "The filing history and the coverage denominator are unaffected."
                ),
            )
        )
    if profile_absent:
        found.append(
            Finding(
                code="submissions_absent",
                metric=None,
                detail=(
                    "No submissions payload, so there is no SIC, no fiscal year end, and no filing "
                    "history to build the coverage denominator from."
                ),
            )
        )
    if spine.origin is SpineOrigin.OBSERVED:
        found.append(
            Finding(
                code="spine_observed",
                metric=None,
                detail=(
                    "No 10-K or 10-Q in the window, so coverage is measured against the periods the "
                    "facts themselves carry. That denominator is circular: the percentages below say "
                    "how much of what was found was tagged, not how much of what was reported."
                ),
            )
        )
    truncation = _truncation(spine, annual, quarterly, window=window)
    if truncation is not None:
        found.append(
            Finding(
                code="window_truncated",
                metric=None,
                detail=(
                    f"History starts {truncation.isoformat()}, inside the requested window "
                    f"{window[0].isoformat()}..{window[1].isoformat()}."
                ),
            )
        )

    for metric in sorted(series, key=str):
        item = series[metric]
        found.extend(
            _metric_findings(
                item,
                annual=annual.get(metric, ()),
                quarterly=quarterly.get(metric, ()),
                coverage=(coverage_annual[metric], coverage_quarterly[metric]),
                approximated=metric in set(approximated),
                coverage_floor=coverage_floor,
            )
        )
    return tuple(found)


def _metric_findings(
    item: MetricSeries,
    *,
    annual: Sequence[Fact],
    quarterly: Sequence[Fact],
    coverage: tuple[MetricCoverage, MetricCoverage],
    approximated: bool,
    coverage_floor: Decimal | None,
) -> tuple[Finding, ...]:
    """The findings for one metric. Order follows :data:`FINDING_CODES`."""
    metric = item.metric
    found: list[Finding] = []
    annual_coverage, quarterly_coverage = coverage

    if coverage_floor is not None:
        for bucket, entry in (
            (Bucket.ANNUAL, annual_coverage),
            (Bucket.QUARTERLY, quarterly_coverage),
        ):
            rate = entry.fill_rate
            if rate is not None and rate < coverage_floor:
                found.append(
                    Finding(
                        code="coverage_below_floor",
                        metric=metric,
                        detail=(
                            f"{metric}: {entry.filled}/{entry.expected} {bucket} periods filled, "
                            f"under the {coverage_floor} floor"
                        ),
                    )
                )

    q4_derived = [
        fact
        for fact in quarterly
        if isinstance(fact.source, Derivation) and fact.source.rule == Q4_RULE
    ]
    if q4_derived:
        found.append(
            Finding(
                code="q4_derived",
                metric=metric,
                detail=(
                    f"{metric}: {len(q4_derived)} fourth quarter(s) derived as FY − (Q1+Q2+Q3) — "
                    + ", ".join(fact.period.end.isoformat() for fact in q4_derived)
                ),
                evidence=tuple(fact.source for fact in q4_derived),
            )
        )
    # Two restrictions, and both exist because a flag that fires on most filers is not a flag.
    #
    # `PER_SHARE` and non-subtractable metrics are excluded because a Q4 for them is never derivable
    # by design, so the finding would fire on diluted EPS for every filer in the universe.
    #
    # And an annual period is only interesting if the filer reported *some* quarter inside it: a
    # filer with no quarterly data at all has no missing Q4, it has no quarterly data, which the
    # quarterly fill rate already says. The case worth flagging is the one `NOQ4` minus its Q2
    # produces — quarters present, Q4 neither filed nor recoverable.
    chain = chain_for(metric)
    derivable = chain.aggregation is Aggregation.FLOW and chain.subtractable
    missing_q4 = [
        year
        for year in annual
        if derivable
        and year.period.kind is PeriodKind.ANNUAL
        and any(_inside(quarter, year) for quarter in quarterly)
        and not any(
            abs(quarter.period.end - year.period.end) <= SEAM_TOLERANCE
            and quarter.period.kind is PeriodKind.QUARTER
            for quarter in quarterly
        )
    ]
    if missing_q4:
        found.append(
            Finding(
                code="q4_absent",
                metric=metric,
                detail=(
                    f"{metric}: no fourth quarter, filed or derivable, for "
                    + ", ".join(year.period.end.isoformat() for year in missing_q4)
                ),
            )
        )
    if len(item.tags_used) > 1:
        found.append(
            Finding(
                code="series_stitched",
                metric=metric,
                detail=f"{metric}: {' → '.join(item.tags_used)}",
            )
        )
    restated = [record for record in item.restatements if record.value_changed]
    if restated:
        found.append(
            Finding(
                code="restated",
                metric=metric,
                detail=(
                    f"{metric}: {len(restated)} period(s) whose value changed across filings — "
                    + ", ".join(record.period.end.isoformat() for record in restated)
                ),
            )
        )
    inexact = annual_coverage.spine_date_inexact + quarterly_coverage.spine_date_inexact
    if inexact:
        found.append(
            Finding(
                code="spine_date_inexact",
                metric=metric,
                detail=(
                    f"{metric}: {inexact} period(s) matched a filing's report date within "
                    f"{SEAM_TOLERANCE.days} days but not exactly"
                ),
            )
        )
    for switch in item.switches:
        found.append(
            Finding(
                code="exclusivity_switch",
                metric=metric,
                detail=(
                    f"{metric}: moved from {switch.tags[0]} to {switch.tags[-1]} at "
                    f"{switch.boundary.isoformat()} ({switch.group}); both kept and stitched"
                ),
            )
        )
    if metric is Metric.NET_INCOME and uses_including_nci_net_income(item.tags_used):
        found.append(
            Finding(
                code="net_income_scope_mismatch",
                metric=metric,
                detail=(
                    f"{metric}: includes noncontrolling interest while equity is parent-only, so "
                    "any return-on-equity built from the pair mixes two scopes"
                ),
            )
        )
    if approximated:
        found.append(
            Finding(
                code="liabilities_nci_approximated",
                metric=metric,
                detail=(
                    f"{metric}: derived with parent-only equity because the including-NCI tag was "
                    "absent, so the result overstates liabilities by any noncontrolling interest"
                ),
            )
        )
    summed = item.annual.summed + item.quarterly.summed
    if summed:
        found.append(
            Finding(
                code="sga_composed",
                metric=metric,
                detail=f"{metric}: {summed} period(s) summed from two component tags",
            )
        )
    anomalies = item.annual.sign_anomalies + item.quarterly.sign_anomalies
    if anomalies:
        found.append(
            Finding(
                code="sign_anomaly",
                metric=metric,
                detail=(
                    f"{metric}: {anomalies} fact(s) contradict the "
                    f"{chain_for(metric).sign} convention; kept, not corrected"
                ),
            )
        )
    if item.dropped_unit_mismatch:
        found.append(
            Finding(
                code="unit_mismatch",
                metric=metric,
                detail=(
                    f"{metric}: {item.dropped_unit_mismatch} fact(s) excluded for unit "
                    f"({', '.join(item.units_excluded)}); this metric is measured in "
                    f"{chain_for(metric).unit}"
                ),
            )
        )
    if item.dropped_other:
        found.append(
            Finding(
                code="other_bucket_drops",
                metric=metric,
                detail=(
                    f"{metric}: {item.dropped_other} fact(s) dropped as neither annual nor "
                    "quarterly by duration — usually a transition period after a fiscal-year change"
                ),
            )
        )
    outside = annual_coverage.periods_outside_spine + quarterly_coverage.periods_outside_spine
    if outside:
        found.append(
            Finding(
                code="periods_outside_spine",
                metric=metric,
                detail=(
                    f"{metric}: {outside} period(s) the filing history does not account for; kept "
                    "in the series, excluded from the coverage numerator"
                ),
            )
        )
    return tuple(found)


def _inside(quarter: Fact, year: Fact) -> bool:
    """Whether ``quarter`` is a quarter reported inside ``year``, at :data:`SEAM_TOLERANCE`."""
    if quarter.period.kind is not PeriodKind.QUARTER or quarter.period.start is None:
        return False
    if year.period.start is None:
        return False
    return (
        quarter.period.start >= year.period.start - SEAM_TOLERANCE
        and quarter.period.end <= year.period.end + SEAM_TOLERANCE
    )


def _truncation(
    spine: PeriodSpine,
    annual: Mapping[Metric, tuple[Fact, ...]],
    quarterly: Mapping[Metric, tuple[Fact, ...]],
    *,
    window: tuple[date, date],
) -> date | None:
    """The date history actually starts, when that is materially later than the window's start.

    "Materially" is one annual period, :data:`~investo.domain.periods.ANNUAL_DAYS`' lower bound: a
    whole year of the requested window with neither a periodic filing nor a fact in it is a history
    shorter than the one that was asked for, which §6.4 lists as a data-integrity flag. A tighter
    threshold would fire on every filer whose fiscal year does not start in the month the command was
    run, since ``window`` floors to the first of that month.
    """
    earliest = _earliest_evidence(spine, annual, quarterly)
    if earliest is None:
        return None
    if earliest - window[0] <= timedelta(days=ANNUAL_DAYS.start):
        return None
    return earliest


def _earliest_evidence(
    spine: PeriodSpine,
    annual: Mapping[Metric, tuple[Fact, ...]],
    quarterly: Mapping[Metric, tuple[Fact, ...]],
) -> date | None:
    """The earliest date this company has any evidence for, spine or fact."""
    dates: list[date] = [*spine.annual_ends, *spine.quarterly_ends]
    for store in (annual, quarterly):
        for facts_ in store.values():
            dates.extend(fact.period.start or fact.period.end for fact in facts_)
    return min(dates, key=identity) if dates else None

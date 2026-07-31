"""``as_of``, dedup, buckets and residual recovery — the order these run in is the milestone.

DESIGN.md §4.2(a)(b)(c) is normative on all five operations. What this module fixes is the **order**,
which §4.2 does not state and which two of them are wrong without::

    CompanyFacts.facts                  (taxonomy, tag) -> RawFact rows, M1
       │
       ├─ 1. as_of filter    drop every RawFact with source.filed > as_of
       ├─ 2. unit filter     drop facts whose unit is not the chain's              (tags.py)
       ├─ 3. dedup           (taxonomy, tag, unit, start, end) -> max(filed)
       ├─ 4. window filter   drop periods wholly outside the lookback window
       ├─ 5. resolution      period-wise, per metric                              (tags.py)
       ├─ 6. residual        Q4 from annual; quarters from YTD
       ├─ 7. cross-metric    gross profit, liabilities, EPS         (tags.py, from statements.py)
       └─ 8. sort            a total key, every series
       │
       ▼
    Fact series, per metric, per bucket

**``as_of`` runs before dedup, and reversing them is a lookahead leak.** §4.2(b) gives the
point-in-time rule as ``max(filed) where filed <= as_of``. Deduping first and filtering after
evaluates ``max(filed)`` over the full set and then discards the winner if it was filed too late,
leaving a *hole* where the correct answer is the value that was current on that date. On the
``RESTATER`` fixture at ``--as-of 2021-06-30``, filter-then-dedup yields 812,000,000 — the number
that was true then — and dedup-then-filter yields nothing. A backtest that silently loses its most
recent fiscal year at every date is a backtest measuring something else.

**The window filter runs after dedup.** A fact's ``filed`` date and its period have no fixed
relationship — a comparative in a later 10-K is filed years after the period it describes — so
filtering the window on ``filed`` would drop restatements of in-window periods. It runs after so
dedup sees every generation of an in-window period.

**Residual recovery runs after resolution.** ``Q4 = FY − (Q1+Q2+Q3)`` must subtract quarters of *the
same metric*, and "the same metric" is only defined once the chain has chosen a tag per period.
Deriving Q4 per tag and then resolving would subtract three ``SalesRevenueNet`` quarters from an
ASC 606 year on any filer straddling the boundary mid-year — the number comes out, and it is not
revenue.

Everything here is pure: no I/O, no clock, no network, and no tag knowledge — this module never
names an XBRL tag, it receives chains from :mod:`~investo.normalize.tags` and applies them. It also
does **no interpolation, ever**: a missing period stays missing. Carrying a value forward or
averaging two neighbours produces a fact with no ``SourceRef``, and §3.2's rule is that such a number
is not printed. There is no flag for it and no config option to enable it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from investo.domain.models import Fact, Metric, RawFact
from investo.domain.periods import FiscalPeriod, PeriodKind, classify
from investo.domain.provenance import Accession, Derivation, Provenance
from investo.normalize.tags import (
    CHAINS,
    Aggregation,
    Chain,
    ExclusivitySwitch,
    Materialized,
    Resolution,
    identity,
    materialize,
    resolve_series,
    unit_filter,
)

__all__ = [
    "SEAM_TOLERANCE",
    "Q4_RULE",
    "YTD_RULE",
    "RECOVERY_RULES",
    "DedupKey",
    "Restatement",
    "SeriesPart",
    "MetricSeries",
    "source_sort_key",
    "fact_sort_key",
    "filter_as_of",
    "dedup",
    "dedup_all",
    "in_window",
    "observed_calendar",
    "residual",
    "derive_q4",
    "recover_from_ytd",
    "normalize_metric",
]

SEAM_TOLERANCE: Final = timedelta(days=3)
"""How far two period boundaries may disagree and still be treated as adjacent.

Filers record period boundaries inconsistently at the day level — one filer's Q1 ends 2019-03-30 and
Q2 starts 2019-03-31, another's Q2 starts 2019-04-01 — so a zero-tolerance seam check fails on
correct data. Three days absorbs that and is far short of any real missing period, which is what
makes guard 5 in :func:`residual` the load-bearing one rather than this constant.
"""

Q4_RULE: Final = "q4_from_annual_minus_quarters"
YTD_RULE: Final = "quarter_from_ytd_difference"

RECOVERY_RULES: Final = frozenset({Q4_RULE, YTD_RULE})
"""The two labels residual recovery emits.

Named as a set because :func:`residual` refuses to consume its own output: a Q4 recovered by
subtraction is not eligible to be a part in another subtraction, and a quarter recovered from YTD is
not eligible to be one of the three quarters in a Q4 derivation. Two levels of subtraction accumulate
two rounding differences and compound any single mis-tagged input, and the resulting figure traces to
eight accessions in a way no reader can check.
"""

type DedupKey = tuple[str, str, str, date | None, date]
"""``(taxonomy, tag, unit, start, end)`` — the full dedup key.

§4.2(b) gives it as ``(unit, start, end)``, which is already within-tag because ``companyfacts``
nests facts under taxonomy → tag → unit. Writing it out in full matters because M2 **flattens** that
nesting to resolve chains, and a three-part key applied to the flattened set would dedup a
``Revenues`` fact against a ``SalesRevenueNet`` fact for the same period — two different concepts
collapsed to whichever was filed later.
"""


@dataclass(frozen=True, slots=True)
class Restatement:
    """Every generation of one metric-period, with the survivor in :attr:`current`.

    Dedup returns its losers rather than discarding them, and they are kept for three reasons, any
    one of which pays for the few hundred bytes:

    - **§6.4 lists "restatement detected in the window" as a data-integrity flag.** M4 renders the
      flag; M2 supplies the evidence. Without the record M4 would have to re-derive it, which means
      re-parsing.
    - **ROADMAP open question 10** — whether a restated series shows both versions — is a *display*
      question M2 does not answer, and cannot be answered later at all if the superseded values were
      thrown away.
    - **It is the evidence that ``as_of`` works.** At ``--as-of 2021-06-30`` on ``RESTATER``,
      :attr:`superseded` is *empty* rather than holding three entries marked "not yet filed", because
      the filter ran first and they were never candidates. That difference is the observable one
      between filtering and post-hoc suppression.
    """

    metric: Metric
    period: FiscalPeriod
    current: Decimal
    superseded: tuple[tuple[date, Decimal, Accession], ...]
    """``(filed, value, accession)``, ascending."""

    @property
    def value_changed(self) -> bool:
        """Whether any superseded generation carried a different number.

        The ``restated`` finding keys on this, not on the record's existence. The AAPL fixture's
        quarter ending 2019-06-29 appears under four accessions with four ``filed`` dates and the
        same value each time — that is a comparative carried forward, and flagging it would put a
        false accounting signal on the flagship fixture.
        """
        return any(value != self.current for _, value, _ in self.superseded)


@dataclass(frozen=True, slots=True)
class SeriesPart:
    """One metric's facts in one bucket, and what the bucket cost to produce."""

    facts: tuple[Fact, ...] = ()
    recovered: int = 0
    """Periods that came from Q4 or YTD residual recovery."""
    sign_anomalies: int = 0
    summed: int = 0
    absent: tuple[FiscalPeriod, ...] = ()
    """Requested periods that resolved to nothing. Kept rather than dropped, because a resolver whose
    absences are invisible makes the coverage denominator unknowable."""


@dataclass(frozen=True, slots=True)
class MetricSeries:
    """One metric, normalized: both buckets and every count the coverage report needs.

    The four drop counts are **metric-level and appear on both buckets' ``MetricCoverage``**. A fact
    dropped for its unit has no bucket — unit is orthogonal to duration — and an ``OTHER``-bucket
    fact by definition landed in neither. No aggregate sums them (tier aggregates are means over
    ``fill_rate``), so reporting them against both buckets cannot inflate anything, while attributing
    them to one bucket would lose half of them.
    """

    metric: Metric
    annual: SeriesPart = field(default_factory=SeriesPart)
    quarterly: SeriesPart = field(default_factory=SeriesPart)
    tags_used: tuple[str, ...] = ()
    """Qualified, in first-use order. A tuple because a stitch is normal: Apple's revenue is
    ``SalesRevenueNet`` for FY2016-17 and the ASC 606 tag from FY2018."""
    dropped_other: int = 0
    dropped_unit_mismatch: int = 0
    units_excluded: tuple[str, ...] = ()
    dropped_ytd_redundant: int = 0
    """YTD facts dropped because the filer also reported the discrete quarter."""
    dropped_ytd_unusable: int = 0
    """YTD facts that recovered nothing because an earlier rung of their ladder is missing."""
    restatements: tuple[Restatement, ...] = ()
    switches: tuple[ExclusivitySwitch, ...] = ()
    collapsed: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# sort keys — every one of them total
# ---------------------------------------------------------------------------
def source_sort_key(source: Provenance) -> tuple[str, str]:
    """A total order over provenance.

    A ``SourceRef`` sorts on its accession; a ``Derivation`` on ``(rule, first leaf accession)``.
    Both return a two-string tuple so the two cases are mutually comparable — a key whose shape
    depends on the value is not a key.
    """
    if isinstance(source, Derivation):
        leaves = source.refs()
        return (source.rule, leaves[0].accession.value if leaves else "")
    return ("", source.accession.value)


def fact_sort_key(fact: Fact) -> tuple[date, str, date, str, tuple[str, str]]:
    """A total order over facts, and the only key any sort over a :class:`Fact` may use.

    ``FiscalPeriod`` is ``order=True`` and compares on ``(end, kind)`` — ``start`` is
    ``compare=False`` so that ``None`` is never compared against a ``date`` when an instant and a
    duration share an end date. The consequence, handed to M2 by ``docs/m1/01-domain-types.md``, is
    that two durations with the same ``end`` and ``kind`` compare **equal**; Python's sort is stable,
    so ``sorted(facts)`` returns input order for those, and input order descends from ``dict``
    iteration over a parsed JSON payload. Deterministic in practice, not a guarantee, and DESIGN.md
    §11's byte-identical gate should not rest on it.
    """
    return (
        fact.period.end,
        str(fact.period.kind),
        fact.period.start or date.min,
        fact.unit,
        source_sort_key(fact.source),
    )


def _raw_sort_key(fact: RawFact) -> tuple[date, str, date, str, str, str]:
    """The same idea for a :class:`~investo.domain.models.RawFact`, which also carries a tag."""
    return (
        fact.period.end,
        str(fact.period.kind),
        fact.period.start or date.min,
        fact.unit,
        fact.qualified_tag,
        fact.source.accession.value,
    )


def _dedup_key(fact: RawFact) -> DedupKey:
    return (fact.taxonomy, fact.tag, fact.unit, fact.period.start, fact.period.end)


def _survivor_key(fact: RawFact) -> tuple[date, str]:
    """``(filed, accession)`` ascending, so the last element is the survivor.

    Two accessions filed the same day carrying the same period is ordinary — a 10-K and an 8-K
    exhibit, or an original and an amendment filed together. Without an explicit tiebreak the
    survivor depends on ``dict`` iteration order over the parsed payload, which is stable in CPython
    and is not a guarantee anybody should be resting a report on.
    """
    return (fact.source.filed, fact.source.accession.value)


# ---------------------------------------------------------------------------
# 1. as_of
# ---------------------------------------------------------------------------
def filter_as_of(facts: Sequence[RawFact], *, as_of: date | None) -> tuple[RawFact, ...]:
    """Drop every fact filed after ``as_of``. ``None`` means no filtering — the current view.

    **The filter is on ``filed``, never on ``period.end``, ``report_date`` or ``accepted_at``.**
    Filing an amendment on the last day before ``as_of`` for a period ending after it is legal and
    happens; ``filed`` is the only date that answers "could we have known this then".

    ``as_of`` is resolved once, at the command boundary, and threaded down. Nothing below the command
    reads a clock to default it: a default resolved deep in the pipeline makes two runs either side
    of midnight produce different reports, and §11's determinism gate would report that as a
    nondeterminism bug rather than as the design mistake it is.

    This does **not** filter prices. M1's ``_fetch_prices`` already takes the last bar at or before
    ``as_of``. The two ``as_of`` paths are separate and both are tested, which is worth stating
    because a single filter that appears to cover everything is how one of them quietly stops being
    applied.
    """
    if as_of is None:
        return tuple(facts)
    return tuple(fact for fact in facts if fact.source.filed <= as_of)


# ---------------------------------------------------------------------------
# 3. dedup
# ---------------------------------------------------------------------------
def dedup(facts: Sequence[RawFact]) -> tuple[RawFact, tuple[RawFact, ...]]:
    """Collapse facts sharing a :data:`DedupKey` to the latest filing, keeping the losers.

    Returns:
        ``(survivor, superseded)``, the superseded ascending by ``(filed, accession)``.

    **Equal values are still deduped, and the survivor's ``SourceRef`` is the late one.** In the AAPL
    fixture the quarter ending 2019-06-29 appears under four accessions with the same value each
    time, so no test asserting on values catches a broken dedup here — what moves is the accession
    printed in the appendix, which is why the test asserts on ``fact.source.accession``.

    Raises:
        ValueError: on an empty sequence. There is no survivor of nothing, and returning ``None``
            would push that impossibility into every caller.
    """
    if not facts:
        raise ValueError("dedup() needs at least one fact.")
    ordered = sorted(facts, key=_survivor_key)
    return ordered[-1], tuple(ordered[:-1])


def dedup_all(
    facts: Iterable[RawFact],
) -> tuple[tuple[RawFact, ...], Mapping[DedupKey, tuple[RawFact, ...]]]:
    """Apply :func:`dedup` per :data:`DedupKey` across a heterogeneous set of facts.

    Returns:
        ``(survivors, superseded_by_key)``. Survivors are sorted on a total key, so the output does
        not inherit the input's order.
    """
    grouped: dict[DedupKey, list[RawFact]] = {}
    for fact in facts:
        grouped.setdefault(_dedup_key(fact), []).append(fact)

    survivors: list[RawFact] = []
    superseded: dict[DedupKey, tuple[RawFact, ...]] = {}
    # Every component of the key participates, `start` included via `or date.min` — the same shape the
    # total keys above use. Omitting it would leave two keys differing only in `start` tied, and a
    # stable sort then returns `dict` insertion order, which descends from payload iteration order.
    # That is the exact failure CLAUDE.md convention 10 forbids, and it would pass the AST gate
    # because a `key=` is present.
    for key in sorted(
        grouped, key=lambda item: (item[4], item[3] or date.min, item[0], item[1], item[2])
    ):
        winner, losers = dedup(grouped[key])
        survivors.append(winner)
        if losers:
            superseded[key] = losers
    return tuple(sorted(survivors, key=_raw_sort_key)), superseded


# ---------------------------------------------------------------------------
# 4. window
# ---------------------------------------------------------------------------
def in_window(period: FiscalPeriod, window: tuple[date, date]) -> bool:
    """Whether ``period`` overlaps ``window`` at all.

    "Wholly outside" rather than "wholly inside": an annual period straddling the window's start edge
    is the first year of a 5y lookback for most filers, and dropping it would make the delivered
    window shorter than the requested one for everyone whose fiscal year does not happen to align to
    the month the command was run in.
    """
    start, end = window
    if period.end < start:
        return False
    return (period.start or period.end) <= end


# ---------------------------------------------------------------------------
# the bucketing calendar
# ---------------------------------------------------------------------------
def observed_calendar(
    facts: Mapping[tuple[str, str], Sequence[RawFact]],
    keys: Iterable[tuple[str, str]],
    *,
    window: tuple[date, date],
    as_of: date | None,
) -> tuple[tuple[date, ...], tuple[date, ...]]:
    """Annual and quarterly period ends observed in the payload, over the tags the chains name.

    Two consumers, and neither can be served by the filing history alone:

    - **Bucketing instants.** A balance-sheet fact carries no duration, so nothing in it says whether
      it is a year end or a quarter end. It is bucketed by matching its date against the duration
      facts the filer reported — and because the fiscal year end *is* the Q4 balance date
      (``docs/m2/01-tags.md`` §6), every annual end is also a quarterly end here.
    - **The ``OBSERVED`` spine.** When a filer has no periodic filing in the window at all, this is
      the only denominator available, and ``docs/m2/03-statements.md`` §2 requires it to be
      **labelled** everywhere it is printed, because coverage against a circular denominator is close
      to meaningless.

    Restricted to tags some chain names, so a ``pure``-unit ratio under an unrelated concept cannot
    invent a period.
    """
    annual: set[date] = set()
    quarterly: set[date] = set()
    for key in keys:
        for fact in filter_as_of(facts.get(key, ()), as_of=as_of):
            if not in_window(fact.period, window):
                continue
            if fact.period.kind is PeriodKind.ANNUAL:
                annual.add(fact.period.end)
                quarterly.add(fact.period.end)
            elif fact.period.kind is PeriodKind.QUARTER:
                quarterly.add(fact.period.end)
    return tuple(sorted(annual, key=identity)), tuple(sorted(quarterly, key=identity))


# ---------------------------------------------------------------------------
# 6. residual recovery: one rule, two names
# ---------------------------------------------------------------------------
def residual(whole: Fact, parts: Sequence[Fact], *, rule: str) -> Fact | None:
    """Subtract a set of shorter periods tiling the front of a longer one; keep the remainder.

    Q4 derivation and YTD differencing are the same operation, so they are one function with two rule
    labels for provenance. ``None`` unless **all** of the following hold — each is a guard against a
    specific way the naive version produces a wrong number that looks right:

    0. **No input is itself recovered.** Enforced here as well as by construction, because "recovery
       runs once" is a property of the caller and this is the function that would be wrong. See
       :data:`RECOVERY_RULES`.
    1. **The metric is ``FLOW`` and ``subtractable``.** ``INSTANT`` and ``PER_SHARE`` are excluded by
       their aggregation class; so is ``SHARES_DILUTED_WEIGHTED``, whose annual figure is a weighted
       average and not a sum.
    2. **Every part shares the whole's unit** — and its metric, since a subtraction across metrics is
       a different function.
    3. **The parts are non-overlapping and ordered**, and the first part starts within
       :data:`SEAM_TOLERANCE` of the whole.
    4. **No seam gap exceeds :data:`SEAM_TOLERANCE`.**
    5. **The residual period classifies as ``QUARTER``.** The load-bearing guard, and it subsumes most
       of the others: if a quarter is missing from ``parts``, the residual is ~180 days, classifies as
       ``YTD``, and the derivation does not fire — where the naive version emits a two-quarter figure
       labelled Q4. Both callers recover quarters, which is why the expected kind is not a parameter.

    The returned fact carries ``Derivation(rule=..., inputs=(whole.source, *part sources))``, so
    ``refs()`` flattens to four accessions for a derived Q4 — which is what §3.2 requires and what
    the appendix prints.
    """
    if not parts or whole.period.start is None:
        return None
    if _is_recovered(whole) or any(_is_recovered(part) for part in parts):
        return None

    chain = CHAINS.get(whole.metric)
    if chain is None or chain.aggregation is not Aggregation.FLOW or not chain.subtractable:
        return None
    if any(part.metric is not whole.metric or part.unit != whole.unit for part in parts):
        return None

    ordered = sorted(parts, key=fact_sort_key)
    first_start = ordered[0].period.start
    if first_start is None or abs(first_start - whole.period.start) > SEAM_TOLERANCE:
        return None
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        gap = (later.period.start or later.period.end) - earlier.period.end
        if gap <= timedelta(0) or gap > SEAM_TOLERANCE:
            return None
    if ordered[-1].period.end >= whole.period.end:
        return None

    start = ordered[-1].period.end + timedelta(days=1)
    if classify(start, whole.period.end) is not PeriodKind.QUARTER:
        return None

    return Fact(
        metric=whole.metric,
        value=whole.value - sum((part.value for part in ordered), Decimal(0)),
        period=FiscalPeriod.of(start, whole.period.end),
        source=Derivation(
            rule=rule,
            inputs=(whole.source, *(part.source for part in ordered)),
        ),
        unit=whole.unit,
    )


def derive_q4(annual: Fact, quarters: Sequence[Fact]) -> Fact | None:
    """``Q4 = FY − (Q1+Q2+Q3)``, but **only** when no quarter ends on the annual period's end.

    §4.2(c): *"discrete Q4 is often never tagged … and don't assume either behavior, since it varies
    by issuer **and** by year within the same issuer."* The last clause is what makes an
    unconditional rule wrong, and ``NOQ4`` is built to prove it — FY2022 has Q1–Q3 and an annual
    figure, FY2023 has all four. A rule that always subtracts gives 2023 five quarters; a rule that
    never subtracts loses 28% of 2022's revenue and reports the remaining three quarters as the year.

    The presence test is on the **period end date** — not on a count, and not on ``filing_fp``.
    §4.2(a) forbids reading ``fp``, and a count of three would fire on a year missing its Q2 as
    readily as on one missing its Q4.
    """
    inside = [
        quarter
        for quarter in quarters
        if _within(quarter.period, annual.period) and not _is_recovered(quarter)
    ]
    if any(quarter.period.end == annual.period.end for quarter in inside):
        return None
    return residual(annual, inside, rule=Q4_RULE)


def recover_from_ytd(
    quarters: Sequence[Fact], ytd: Sequence[Fact]
) -> tuple[tuple[Fact, ...], int, int]:
    """Difference a cumulative ladder into discrete quarters.

    Returns ``(recovered, redundant, unusable)`` — and the two counters are separate because they are
    two different facts about the filer. A YTD figure alongside a discrete quarter is **redundant**:
    the filer reports both, we take the discrete one, and the population is worth knowing but nobody
    acts on it. A YTD figure that recovered nothing is **unusable**: the rung before it is missing, so
    the residual spans two quarters and guard 5 refuses it. Counting them together produces "YTD facts
    we did not use", which is a number no reader can act on — and counting only the first would let the
    second vanish from every counter, which is the one disposition this module does not allow.

    A 10-Q carries the discrete quarter *and* the cumulative year-to-date figure. For most filers the
    discrete quarter is present and the YTD fact is redundant; for filers presenting cumulatively
    only, the discrete quarters exist nowhere and the series is empty without this::

        Q2 = H1 − Q1        parts = [YTD through Q1], whole = [YTD through Q2]
        Q3 = 9M − H1

    The ladder is grouped by ``period.start``, because that is what every rung of one cumulative
    ladder shares — the fiscal year's first day. Each step's ``parts`` is the **previous as-filed
    rung**, never a quarter this function produced, so nothing is derived from a derived part. A
    filer that files YTD at Q1 and then nothing until the 10-K produces no differenced quarters
    rather than a 270-day figure labelled Q3, because guard 5 refuses the residual.

    Where both the YTD fact and the discrete quarter exist, **the discrete quarter wins and the YTD
    fact is dropped, not reconciled.** Small differences between a filer's discrete and cumulative
    figures are usually intra-period reclassifications, they are routine, and a flag that fires on
    most filers is not a flag. The drop count appears in the coverage report so the population is
    visible without anyone having to act on it.
    """
    filed_ends = {quarter.period.end for quarter in quarters}
    ladders: dict[date, list[Fact]] = {}
    for fact in (*quarters, *ytd):
        if fact.period.start is None or _is_recovered(fact):
            continue
        ladders.setdefault(fact.period.start, []).append(fact)

    recovered: list[Fact] = []
    redundant = 0
    unusable = 0
    for start in sorted(ladders, key=identity):
        rungs = sorted(ladders[start], key=fact_sort_key)
        for previous, current in zip(rungs, rungs[1:], strict=False):
            if current.period.kind is not PeriodKind.YTD:
                continue
            if current.period.end in filed_ends:
                redundant += 1
                continue
            produced = residual(current, [previous], rule=YTD_RULE)
            if produced is None:
                unusable += 1
                continue
            recovered.append(produced)
            filed_ends.add(produced.period.end)

    # A YTD fact that was never anybody's `current` — the first rung of a ladder, with no earlier rung
    # to difference against — is unusable for the same reason and by the same arithmetic.
    paired = {
        id(fact)
        for rungs in ladders.values()
        for fact in sorted(rungs, key=fact_sort_key)[1:]
    }
    unusable += sum(
        1 for fact in ytd if fact.period.kind is PeriodKind.YTD and id(fact) not in paired
    )
    return tuple(sorted(recovered, key=fact_sort_key)), redundant, unusable


# ---------------------------------------------------------------------------
# the whole pipeline, for one metric
# ---------------------------------------------------------------------------
def normalize_metric(
    chain: Chain,
    facts: Mapping[tuple[str, str], Sequence[RawFact]],
    *,
    window: tuple[date, date],
    as_of: date | None,
    annual_ends: Sequence[date] = (),
    quarterly_ends: Sequence[date] = (),
) -> MetricSeries:
    """Run steps 1-6 and 8 for one metric. Cross-metric derivation (step 7) is the caller's.

    Args:
        chain: The metric's chain, from :data:`~investo.normalize.tags.CHAINS`.
        facts: ``CompanyFacts.facts`` — raw, unfiltered, keyed by ``(taxonomy, tag)``.
        window: The lookback window, computed once at the command boundary.
        as_of: Point-in-time cut, or ``None`` for the current view.
        annual_ends: The bucketing calendar's annual dates — see :func:`observed_calendar`. Only
            :attr:`~investo.normalize.tags.Aggregation.INSTANT` metrics read it.
        quarterly_ends: The same, for quarters.
    """
    filtered = [
        fact for key in chain.keys for fact in filter_as_of(facts.get(key, ()), as_of=as_of)
    ]
    kept, excluded = unit_filter(chain, filtered)
    survivors, superseded = dedup_all(fact for fact in kept if in_window(fact.period, window))

    dropped_other = sum(1 for fact in survivors if fact.period.kind is PeriodKind.OTHER)
    by_key: dict[tuple[str, str], list[RawFact]] = {}
    for fact in survivors:
        # `OTHER` is dropped, and **counted**. The short end of the bucket is transition and stub
        # periods after a fiscal-year change; the long end is multi-year cumulative disclosures and
        # the occasional 53-week year filed with a mis-stated start. None is usable in an annual or
        # quarterly series and none is recoverable without judgment about what the filer meant.
        # A filer whose facts are 40% `OTHER` has had a fiscal-year change in the window, which is a
        # §6.4 data-integrity finding rather than an ingestion detail.
        if fact.period.kind is not PeriodKind.OTHER:
            by_key.setdefault((fact.taxonomy, fact.tag), []).append(fact)

    requested = _observed_periods(by_key.values())
    series = resolve_series(chain.metric, by_key, periods=requested)

    annual, quarterly = _split_by_bucket(
        chain, series.resolutions, annual_ends=annual_ends, quarterly_ends=quarterly_ends
    )
    ytd = _bucket_of_kind(chain, series.resolutions, PeriodKind.YTD)

    q4 = _recover_q4(annual.facts, quarterly.facts)
    from_ytd, redundant_ytd, unusable_ytd = recover_from_ytd(quarterly.facts, ytd.facts)

    return MetricSeries(
        metric=chain.metric,
        annual=annual,
        quarterly=SeriesPart(
            facts=tuple(sorted((*quarterly.facts, *q4, *from_ytd), key=fact_sort_key)),
            recovered=len(q4) + len(from_ytd),
            sign_anomalies=quarterly.sign_anomalies,
            summed=quarterly.summed,
            absent=quarterly.absent,
        ),
        tags_used=_tags_used(series.resolutions),
        dropped_other=dropped_other,
        dropped_unit_mismatch=len(excluded),
        units_excluded=tuple(sorted({fact.unit for fact in excluded}, key=identity)),
        dropped_ytd_redundant=redundant_ytd,
        dropped_ytd_unusable=unusable_ytd,
        restatements=_restatements(chain.metric, series.resolutions, superseded),
        switches=series.switches,
        collapsed=series.collapsed,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _is_recovered(fact: Fact) -> bool:
    """Whether this fact came out of residual recovery."""
    return isinstance(fact.source, Derivation) and fact.source.rule in RECOVERY_RULES


def _within(inner: FiscalPeriod, outer: FiscalPeriod) -> bool:
    """Whether ``inner`` sits inside ``outer``, allowing :data:`SEAM_TOLERANCE` at each edge."""
    if inner.start is None or outer.start is None:
        return False
    return inner.start >= outer.start - SEAM_TOLERANCE and inner.end <= outer.end + SEAM_TOLERANCE


def _observed_periods(groups: Iterable[Sequence[RawFact]]) -> tuple[FiscalPeriod, ...]:
    """The distinct periods observed across a metric's tags, on a total order.

    These are the periods :func:`~investo.normalize.tags.resolve` is asked for. Requesting the
    *observed* set rather than the spine is what keeps resolution window-independent and keeps facts
    the filing history does not account for in the series — ``docs/m2/03-statements.md`` §2 counts
    those as ``periods_outside_spine`` rather than dropping them.
    """
    unique: dict[tuple[date, PeriodKind], FiscalPeriod] = {}
    for group in groups:
        for fact in group:
            unique.setdefault((fact.period.end, fact.period.kind), fact.period)
    return tuple(unique[key] for key in sorted(unique, key=lambda pair: (pair[0], str(pair[1]))))


def _materialize_all(chain: Chain, resolutions: Iterable[Resolution]) -> tuple[Materialized, ...]:
    """Materialize every non-absent resolution, dropping the absences."""
    produced = (materialize(chain, resolution) for resolution in resolutions)
    return tuple(item for item in produced if item is not None)


def _part_from(items: Sequence[Materialized], absent: Sequence[FiscalPeriod]) -> SeriesPart:
    return SeriesPart(
        facts=tuple(sorted((item.fact for item in items), key=fact_sort_key)),
        sign_anomalies=sum(1 for item in items if item.sign_anomaly),
        summed=sum(1 for item in items if item.summed),
        absent=tuple(absent),
    )


def _bucket_of_kind(
    chain: Chain, resolutions: Sequence[Resolution], kind: PeriodKind
) -> SeriesPart:
    selected = [resolution for resolution in resolutions if resolution.period.kind is kind]
    return _part_from(
        _materialize_all(chain, selected),
        [resolution.period for resolution in selected if resolution.is_absent],
    )


def _split_by_bucket(
    chain: Chain,
    resolutions: Sequence[Resolution],
    *,
    annual_ends: Sequence[date],
    quarterly_ends: Sequence[date],
) -> tuple[SeriesPart, SeriesPart]:
    """Split resolved periods into the annual and quarterly series.

    Durations bucket by their **own duration**, per §4.2(c) — never by the containing filing's
    ``form``. Instants bucket by **date**, against the calendar :func:`observed_calendar` builds,
    because a balance-sheet fact carries no duration to classify: a fiscal-year-end balance is both
    the annual figure and the Q4 figure, and a quarter-end balance is only the latter. An instant
    matching no calendar date lands in the quarterly bucket — the natural home of a point-in-time
    balance — and the coverage report counts it as outside the spine rather than dropping it.
    """
    if chain.aggregation is not Aggregation.INSTANT:
        return (
            _bucket_of_kind(chain, resolutions, PeriodKind.ANNUAL),
            _bucket_of_kind(chain, resolutions, PeriodKind.QUARTER),
        )

    instants = [
        resolution for resolution in resolutions if resolution.period.kind is PeriodKind.INSTANT
    ]
    materialized = _materialize_all(chain, instants)
    absent = [resolution.period for resolution in instants if resolution.is_absent]
    annual = [item for item in materialized if _matches(item.fact.period.end, annual_ends)]
    quarterly = [
        item
        for item in materialized
        if _matches(item.fact.period.end, quarterly_ends)
        or not _matches(item.fact.period.end, annual_ends)
    ]
    return _part_from(annual, absent), _part_from(quarterly, absent)


def _matches(day: date, calendar: Sequence[date]) -> bool:
    """Whether ``day`` is within :data:`SEAM_TOLERANCE` of any date in ``calendar``."""
    return any(abs(day - entry) <= SEAM_TOLERANCE for entry in calendar)


def _tags_used(resolutions: Sequence[Resolution]) -> tuple[str, ...]:
    """Qualified tags in first-use order over the whole series.

    ``len(tags_used) > 1`` is the ``series_stitched`` finding, so the order matters: the appendix
    prints ``us-gaap:SalesRevenueNet → us-gaap:RevenueFromContractWithCustomer…``, and reversing it
    would describe the ASC 606 transition backwards.
    """
    ordered: list[str] = []
    for resolution in sorted(resolutions, key=lambda r: (r.period.end, str(r.period.kind))):
        for tag in resolution.tags_used:
            if tag not in ordered:
                ordered.append(tag)
    return tuple(ordered)


def _restatements(
    metric: Metric,
    resolutions: Sequence[Resolution],
    superseded: Mapping[DedupKey, tuple[RawFact, ...]],
) -> tuple[Restatement, ...]:
    """Build the restatement record for the facts that actually contributed.

    Keyed on the *winning* tag rather than on every tag in the payload: a superseded generation of a
    tag the chain never chose is not a restatement of anything this report prints.
    """
    records: list[Restatement] = []
    for resolution in sorted(resolutions, key=lambda r: (r.period.end, str(r.period.kind))):
        for fact in resolution.facts:
            losers = superseded.get(_dedup_key(fact))
            if not losers:
                continue
            records.append(
                Restatement(
                    metric=metric,
                    period=fact.period,
                    current=fact.value,
                    superseded=tuple(
                        (loser.source.filed, loser.value, loser.source.accession)
                        for loser in sorted(losers, key=_survivor_key)
                    ),
                )
            )
    return tuple(records)


def _recover_q4(annual: Sequence[Fact], quarters: Sequence[Fact]) -> tuple[Fact, ...]:
    """Derive a Q4 for every annual period that has none."""
    produced: list[Fact] = []
    for year in sorted(annual, key=fact_sort_key):
        derived = derive_q4(year, quarters)
        if derived is not None:
            produced.append(derived)
    return tuple(produced)

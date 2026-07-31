"""What a number is: metrics, facts, and the one piece of arithmetic M1 owns.

DESIGN.md §3.2 sketches ``Fact`` and labels the sketch "not final". This module is the proposed
final form; every departure is marked in ``docs/m1/01-domain-types.md`` and carried into that
document's § Spec questions, all of which were accepted on review.

The load-bearing distinction in here is between :class:`RawFact` — what M1 emits, keyed by XBRL
tag, with no opinion about what the tag means — and :class:`Fact`, which carries a
:class:`Metric` and is constructed only in M2. ``ingest/`` may not reference :class:`Metric` at
all; ``tests/test_layering.py`` walks the AST and fails if it does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, NewType

from investo.domain.periods import FiscalPeriod, PeriodKind
from investo.domain.provenance import Derivation, Provenance, SourceRef

__all__ = [
    "Metric",
    "Money",
    "CoverShares",
    "DilutedShares",
    "COVER_SHARES_TAXONOMY",
    "COVER_SHARES_TAG",
    "RawFact",
    "Fact",
    "cover_share_facts",
    "market_cap",
]


class Metric(StrEnum):
    """The financial quantities the report reasons about (DESIGN.md §4.2).

    **Both tiers are declared in M1 even though nothing maps to them until M2.** ROADMAP M2 gives
    the reason: *"Building only the first tier means M4 stalls."* Declaring both now makes the
    omission visible in M2 as an unmapped enum member rather than invisible as a metric nobody
    thought of.

    A metric is not a tag. Which XBRL tag answers to :attr:`REVENUE` — and the ordered fallback
    chain behind it — is ``normalize/tags.py``, which is M2. That seam is enforced: no module
    under ``ingest/`` may reference this class or name a ``us-gaap`` tag.
    """

    # --- tier 1: the DCF metric set (DESIGN.md §4.2) ------------------------
    REVENUE = "revenue"
    NET_INCOME = "net_income"
    GROSS_PROFIT = "gross_profit"
    OPERATING_INCOME = "operating_income"
    ASSETS = "assets"
    LIABILITIES = "liabilities"
    EQUITY = "equity"
    CASH = "cash"
    OPERATING_CASH_FLOW = "operating_cash_flow"
    CAPEX = "capex"
    LONG_TERM_DEBT = "long_term_debt"
    SHARES_COVER = "shares_cover"
    SHARES_DILUTED_WEIGHTED = "shares_diluted_weighted"
    EPS_DILUTED = "eps_diluted"

    # --- tier 2: what M4's F/Z/M scores need (DESIGN.md §4.2) --------------
    ASSETS_CURRENT = "assets_current"
    LIABILITIES_CURRENT = "liabilities_current"
    RETAINED_EARNINGS = "retained_earnings"
    RECEIVABLES = "receivables"
    COGS = "cogs"
    SGA = "sga"
    DEPRECIATION_AMORTIZATION = "depreciation_amortization"
    INTEREST_EXPENSE = "interest_expense"
    SBC = "sbc"
    OPERATING_LEASE_LIABILITY = "operating_lease_liability"
    SHARE_ISSUANCE_PROCEEDS = "share_issuance_proceeds"


type Money = Decimal
"""A monetary amount.

An alias, not a ``NewType``. CLAUDE.md convention 8 is ``Decimal`` for money, never ``float`` —
the enemy is ``float``, and ``Decimal`` already wins that fight. A ``NewType`` here would force a
wrapper call at every arithmetic site (``Money(a + b)``) for no error it catches, and that
friction would be paid by M5, which is nothing but arithmetic.

The two share counts below get ``NewType`` for the opposite reason: there the enemy is *another*
``Decimal``, which an alias cannot see.
"""

CoverShares = NewType("CoverShares", Decimal)
"""``dei:EntityCommonStockSharesOutstanding`` — the cover-page count.

Market cap only. **Never a per-share denominator.** DESIGN.md §4.2 and §5.4: using the
cover-page count where diluted weighted-average shares belong is a classic error, and §5.4 says
the distinction is *"enforced by distinct types."*

A comment is not enforcement and an enum member is not either — both ``Metric.SHARES_COVER`` and
``Metric.SHARES_DILUTED_WEIGHTED`` carry a ``Decimal``, and a ``Decimal`` goes anywhere.
``NewType`` gives basedpyright a real one-directional barrier at zero runtime cost: this is
assignable to ``Decimal``, and ``Decimal`` is not assignable to this.

The guarantee cannot be tested at runtime — both sides are the same object, which is the whole
point — so its violation test is type-level: ``tests/fixtures/typing/cover_shares_as_diluted.py``
performs the forbidden assignment and ``tests/test_typing.py`` asserts basedpyright rejects it.
"""

DilutedShares = NewType("DilutedShares", Decimal)
"""``us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding`` — per-share math only."""

COVER_SHARES_TAXONOMY: Final = "dei"
COVER_SHARES_TAG: Final = "EntityCommonStockSharesOutstanding"
"""The one XBRL tag named outside ``normalize/``, and the reason the M1/M2 seam has a carve-out.

ROADMAP M1 puts market cap in M1, computed as
``price * dei:EntityCommonStockSharesOutstanding`` across classes. That is a ``dei`` cover-page
tag, not a ``us-gaap`` financial metric, and it has no fallback chain — so it is not tag
*selection*, which is what M2 owns.

It is named here, in ``domain/``, rather than in ``ingest/``, and :func:`cover_share_facts` does
the selection so that no module under ``ingest/`` names any tag at all. The layering test asserts
the ``dei`` allowlist holds exactly one entry: if a second ``dei`` tag ever needs naming outside
``normalize/``, that is the signal that tag selection has started leaking upstream.
"""


@dataclass(frozen=True, slots=True)
class RawFact:
    """One XBRL fact as filed. **No metric assigned — that is M2's.**

    Attributes:
        taxonomy: ``"us-gaap"``, ``"dei"``, ``"srt"``, ``"ffd"``, or whatever SEC adds next. Not
            validated against an allowlist: ``ffd`` was not anticipated and the next one will not
            be either (``docs/m1/04-parsers.md`` §2).
        tag: The tag, unqualified. ``(taxonomy, tag)`` is the identifying pair — ``Assets``
            exists in more than one taxonomy.
        unit: The units-dict key, verbatim: ``"USD"``, ``"USD/shares"``, ``"shares"``,
            ``"pure"``. Not normalized and not mapped. Carried on the fact **[extends §3.2]**
            because §4.2(b) dedups by ``(unit, start, end)``, so a fact that does not carry its
            unit cannot be deduped correctly — and because it is the field that catches what
            §4.2 warns about twice: revenue excluding versus including assessed tax are different
            numbers, and EPS arrives under ``USD/shares`` rather than ``USD``.
        value: ``Decimal``, constructed via ``json.loads(..., parse_float=Decimal)`` so no
            ``float`` is ever materialized. ``Decimal(0.1)`` is exact and wrong.
        period: Classified by duration, never by ``form``.
        source: The filing this fact was reported in.
        filing_fy: The **filing's** fiscal year, from the payload's ``fy``. ``None`` on
            registration-statement facts. **Never group by this** (§4.2a).
        filing_fp: The filing's fiscal period — ``"FY"``, ``"Q1"``..``"Q4"``. Same warning.
        frame: SEC's own dedup selection, when present. Legitimate for peer cross-sections;
            **illegitimate for the subject company's history**, because §4.2 records that frame
            selection is not point-in-time stable — a CY2025Q1 frame can resolve to a 2026
            filing.

    ``filing_fy`` and ``filing_fp`` are carried for one reason: §4.2(a) is the trap most likely
    to be "fixed" by a future contributor who finds grouping by ``start``/``end`` awkward. A
    fixture showing a 2025-01-01..2025-03-31 period tagged ``fy: 2026`` is the argument against
    them, and it needs the fields to exist.
    """

    taxonomy: str
    tag: str
    unit: str
    value: Decimal
    period: FiscalPeriod
    source: SourceRef
    filing_fy: int | None = None
    filing_fp: str | None = None
    frame: str | None = None

    @property
    def qualified_tag(self) -> str:
        """``"us-gaap:Assets"``."""
        return f"{self.taxonomy}:{self.tag}"


@dataclass(frozen=True, slots=True)
class Fact:
    """A normalized, metric-assigned figure. **Constructed in M2, not M1.**

    Declared here in M1 so M2 has a target and so that every module written in between is
    written against the final shape.

    ``source`` is :data:`~investo.domain.provenance.Provenance` rather than ``SourceRef`` — spec
    question 2. A derived figure traces to several filings, and printing one of its inputs' refs
    would be worse than printing nothing, because it would look traced.
    """

    metric: Metric
    value: Decimal
    period: FiscalPeriod
    source: Provenance
    unit: str


def cover_share_facts(facts: Sequence[RawFact]) -> tuple[RawFact, ...]:
    """Select the cover-page share-count facts from a company's facts.

    Lives here, next to :data:`COVER_SHARES_TAG`, so that no module under ``ingest/`` names an
    XBRL tag. The alternative — letting the ``fetch`` command filter by tag — would put a tag
    literal in a third place and start the shadow copy of ``normalize/tags.py`` that the M1/M2
    seam exists to prevent.

    Returns the facts for the **newest** cover-page date only. A ``companyfacts`` payload holds
    every cover page the filer has ever submitted, and summing across two of them produces a
    plausible-looking wrong number — which is why :func:`market_cap` raises on mixed dates rather
    than trusting its caller to have filtered.
    """
    matching = [
        fact
        for fact in facts
        if fact.taxonomy == COVER_SHARES_TAXONOMY
        and fact.tag == COVER_SHARES_TAG
        and fact.unit == "shares"
        and fact.period.kind is PeriodKind.INSTANT
    ]
    if not matching:
        return ()
    newest = max(fact.period.end for fact in matching)
    return tuple(fact for fact in matching if fact.period.end == newest)


def market_cap(
    *,
    price: Decimal,
    price_source: SourceRef,
    share_facts: Sequence[RawFact],
    classes: Sequence[str] | None = None,
) -> tuple[Money, Derivation] | None:
    """Market capitalization, and the provenance for it. ``None`` when there is no share count.

    ROADMAP M1 specifies ``price * dei:EntityCommonStockSharesOutstanding`` across classes.
    DESIGN.md §4.3 says computed rather than fetched, because ``yfinance``'s ``Ticker.info`` is
    the flakiest surface in that library and EDGAR's cover-page count is authoritative and
    already cached.

    Pure arithmetic with zero I/O, so it lives in ``domain/`` rather than ``ingest/prices/`` —
    which also keeps the one place a share-count tag is named out of ``ingest/``.

    Args:
        price: The last close **at or before the as-of date**, not the last bar in the series.
            Selecting it is ``ingest/prices``' job
            (:func:`~investo.ingest.prices.base.price_at_or_before`); with ``--as-of`` set, using
            the newest available price would be a lookahead leak in the one number that gets
            compared against modelled value.
        price_source: Provenance for ``price`` — provider, URL, bar date, ``fetched_at``. So a
            market cap in the appendix names both a filing and a price fetch, which is the case
            spec question 2 exists to represent.
        share_facts: Cover-page share counts, normally from :func:`cover_share_facts`. Summed
            across classes (§5.4: GOOGL/GOOG, FOX/FOXA).
        classes: Optional ticker labels for the classes counted, in the order counted. DESIGN.md
            §5.4 requires the report to state which classes were included, and ``domain/`` cannot
            know them — ``companyfacts`` is keyed by CIK and carries no ticker, while
            ``tickers.py``'s several rows per CIK do. So the caller that has both supplies the
            labels and they are recorded in ``Derivation.note``. **[extends the signature in
            ``docs/m1/01-domain-types.md``; the alternative was a note that could not satisfy
            §5.4.]**

    Returns:
        ``(value, derivation)``, or ``None`` when ``share_facts`` is empty.

    **Empty input returns ``None``; malformed input raises.** The split is DESIGN.md §14's own
    distinction — a run that *failed* versus a run that *succeeded in reporting bad news* —
    applied at function scope:

    ===================================================  ========  ====================
    Input                                                Result    Why
    ===================================================  ========  ====================
    ``share_facts`` empty                                ``None``  An **absence**. Expected and
                                                                   common: a ``companyfacts``
                                                                   payload can contain no ``dei``
                                                                   section at all, confirmed live
                                                                   for a recently-listed NASDAQ
                                                                   filer. The caller records it in
                                                                   the coverage report.
    A fact that is not the cover-page tag, not            raises    Malformed. Someone passed the
    ``INSTANT``, or not unit ``shares``                             wrong facts.
    Facts with differing ``end`` dates                    raises    A market cap summed across two
                                                                   cover pages. Plausible-looking
                                                                   and wrong.
    ===================================================  ========  ====================

    ``price`` is non-optional, so an empty ``share_facts`` is the only absence condition and a
    bare ``None`` is unambiguous about which one occurred.

    **The checks live here rather than in the caller**, and that is deliberate. Putting them in
    the ``fetch`` command would work today and would have to be repeated in M3's renderer and
    M4's ``peers.py``, each of which reaches for a market cap independently. One of the three
    would eventually forget, and the symptom is not a crash — it is a ``0`` propagating into
    every multiple in report section 3 and into the valuation sub-score. An ``Optional`` return
    makes forgetting a type error instead.

    Multi-class voting rights are ignored; DESIGN.md §12 records that as a known unmodeled item.

    Raises:
        ValueError: on malformed ``share_facts``, per the table above.
    """
    if not share_facts:
        return None

    for fact in share_facts:
        if (fact.taxonomy, fact.tag) != (COVER_SHARES_TAXONOMY, COVER_SHARES_TAG):
            raise ValueError(
                f"market_cap() takes {COVER_SHARES_TAXONOMY}:{COVER_SHARES_TAG} facts; got "
                f"{fact.qualified_tag}. The cover-page count is the only source for shares "
                "outstanding (DESIGN.md §4.3); a weighted-average count is a per-share "
                "denominator and belongs nowhere near this function (§5.4)."
            )
        if fact.unit != "shares":
            raise ValueError(
                f"market_cap() got a share count in unit {fact.unit!r}, expected 'shares'."
            )
        if fact.period.kind is not PeriodKind.INSTANT:
            raise ValueError(
                f"market_cap() got a {fact.period.kind} share count; a cover-page count is an "
                "instant, and a duration here means the wrong facts were selected."
            )

    ends = {fact.period.end for fact in share_facts}
    if len(ends) != 1:
        raise ValueError(
            "market_cap() was given cover-page share counts from more than one date "
            f"({', '.join(sorted(end.isoformat() for end in ends))}). Summing across cover "
            "pages produces a plausible-looking wrong number, which is the failure this "
            "project exists to avoid."
        )

    shares = CoverShares(sum((fact.value for fact in share_facts), Decimal(0)))
    cover_date = next(iter(ends))

    note_parts = [f"cover date {cover_date.isoformat()}"]
    if classes:
        note_parts.append(f"classes: {', '.join(classes)}")
    else:
        note_parts.append(f"{len(share_facts)} share-count fact(s), classes not labelled")

    derivation = Derivation(
        rule="market_cap",
        inputs=(price_source, *(fact.source for fact in share_facts)),
        note="; ".join(note_parts),
    )
    return price * shares, derivation

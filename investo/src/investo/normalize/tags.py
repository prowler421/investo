"""Which XBRL tag is which metric — the chain registry, and the only home for a ``us-gaap`` literal.

DESIGN.md §4.2 is normative on the chains and on why hardcoding one tag per metric is the failure
this project exists to avoid. §4.2.1 is normative on the five properties the chains are unusable
without, all of which this module implements: **resolution granularity, exclusivity groups,
aggregation class, units and sign conventions.**

Four things worth knowing before reading the tables:

**Resolution is period-wise, not series-level** (§4.2.1). Each period walks the chain
independently, so the ASC 606 stitch is the absence of a bug rather than a special case keyed on
2018 — and the tag chosen for a period is a function of that period's facts alone, so the same
fiscal year cannot resolve differently under ``--lookback 5y`` and ``10y``. Series-level resolution
loses two of Apple's four annual revenue periods *and reports full coverage on the two it keeps*,
which is worse than a hole.

**A metric is a selection, and two selections that share a name do not share a definition**
(§4.2.1). This is why :data:`DERIVATIONS` name their own tags rather than composing over
:class:`~investo.domain.models.Metric`: the total-liabilities fallback needs
``StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest``, and writing it as
``L&SE − Metric.EQUITY`` — the natural spelling once ``EQUITY`` is a resolved metric sitting right
there — overstates liabilities by exactly the noncontrolling interest, for precisely the ~11% of
filers who never tag ``Liabilities`` at all.

**The tier-2 orderings are provisional.** §4.2 carries measured CY2025Q1 entity counts for every
tier-1 member, and none for tier 2. The orderings in :data:`CHAINS` for tier-2 metrics are
``docs/m2/01-tags.md`` §4's proposals; ``docs/m2/README.md`` § Spec question 6 records that they
are to be confirmed against a frames pull and revised in the same commit as ``docs/m2/COVERAGE.md``,
and DESIGN.md deliberately does not carry them until then. A guess written into a normative document
is normative by having been written down.

**This module has no I/O, no clock, no cache and no coverage arithmetic.** It is a table and a
resolution function over facts already filtered and deduped by :mod:`~investo.normalize.facts`.
``RawFact.frame`` is ignored entirely — SEC's own selection is not point-in-time stable, so letting
it break a tie would put a lookahead leak inside the resolver.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from investo.domain.models import (
    COVER_SHARES_TAG,
    COVER_SHARES_TAXONOMY,
    Fact,
    Metric,
    RawFact,
)
from investo.domain.periods import FiscalPeriod, PeriodKind
from investo.domain.provenance import Derivation, Provenance

__all__ = [
    "Aggregation",
    "Sign",
    "Tier",
    "Member",
    "Chain",
    "Resolution",
    "Materialized",
    "ExclusivitySwitch",
    "ResolvedSeries",
    "DerivationKind",
    "DerivedMetric",
    "CHAINS",
    "EXCLUSIVITY_GROUPS",
    "DERIVATIONS",
    "GAAP",
    "identity",
    "chain_for",
    "metrics_in_tier",
    "uses_including_nci_net_income",
    "unit_filter",
    "resolve",
    "resolve_series",
    "materialize",
    "derive",
]

GAAP: Final = "us-gaap"
"""The taxonomy every chain member below is drawn from, bar one.

Named once so that a member declaration reads as data rather than as a repeated literal, and so
that the layering test's converse assertion — *this module must contain ``us-gaap`` literals, or
the rule is passing because the registry moved* — has something stable to find.
"""


class Aggregation(StrEnum):
    """How a metric behaves across consecutive periods (§4.2.1).

    Load-bearing for :func:`~investo.normalize.facts.residual`: the Q4 derivation
    ``FY − (Q1+Q2+Q3)`` is arithmetically well-formed for all three classes and *meaningful* for
    one of them.
    """

    FLOW = "flow"
    """Additive over consecutive periods: revenue, capex, operating cash flow."""

    INSTANT = "instant"
    """A balance at a point in time. The balance-sheet fact at the fiscal year end **is** the Q4
    balance, and subtracting three quarterly balances from an annual one produces a number with no
    meaning."""

    PER_SHARE = "per_share"
    """A ratio whose denominator varies by period.

    ``Q4 EPS = FY EPS − (Q1+Q2+Q3 EPS)`` is the one that would otherwise be got wrong *and look
    right*: it is close enough to plausible that no eyeball catches it, and it is wrong whenever the
    share count moved during the year — which is every year for any company with a buyback or an
    equity comp program, and most wrong for exactly the fast-diluting companies whose EPS matters
    most. A missing Q4 EPS stays missing; M4 recomputes one from
    ``NET_INCOME / SHARES_DILUTED_WEIGHTED`` if it wants it.
    """


class Sign(StrEnum):
    """The sign convention M2 emits for a metric (§4.2.1).

    DESIGN.md said nothing about sign before M2, and the failure mode is silent, filer-dependent,
    and lands in the middle of §5.3's FCF build: ``PaymentsToAcquirePropertyPlantAndEquipment`` is
    filed positive and the build subtracts capex, while ``InterestIncomeExpenseNet`` is signed the
    other way. A ``CAPEX`` series positive for most filers and negative for a few makes FCF wrong
    for that few, in the direction that flatters them.
    """

    AS_FILED = "as_filed"
    """No convention imposed, and therefore no anomaly possible."""

    OUTFLOW_POSITIVE = "outflow_positive"
    """Cash leaving the company is positive. ``CAPEX``."""

    EXPENSE_POSITIVE = "expense_positive"
    """An expense is positive. ``INTEREST_EXPENSE``."""

    @property
    def imposes_a_convention(self) -> bool:
        """Whether a fact's sign can contradict this convention."""
        return self is not Sign.AS_FILED


class Tier(StrEnum):
    """Which metric set a chain belongs to, **declared rather than inferred**.

    ROADMAP M2's exit criterion is "≥90% coverage across 20 NASDAQ names on **both** the DCF metric
    set and the quality-score metric set". A single aggregate hides a tier-2 failure behind tier-1
    success — which is precisely the outcome ROADMAP's *"building only the first tier means M4
    stalls"* is warning about, arriving one milestone later and disguised as a passing gate.
    """

    DCF = "tier_1"
    """§4.2's DCF metric set, feeding M3's charts and M5's valuation."""

    QUALITY = "tier_2"
    """What M4's Piotroski / Altman / Beneish scores need."""


@dataclass(frozen=True, slots=True)
class Member:
    """One position in a fallback chain.

    Attributes:
        taxonomy: ``"us-gaap"`` or ``"dei"``.
        tag: The tag, unqualified.
        plus: A second tag which must **also** be present for this member to match, whose value is
            summed with the first. See :attr:`is_sum`.
        flip_sign: This element is signed opposite to the metric's declared convention **by
            construction** — a property of the taxonomy element, never a response to observing a
            negative value. Producing a :class:`~investo.domain.provenance.Derivation`, so the flip
            is visible in the appendix rather than being an unexplained change of sign.
        note: Printed in the appendix beside the tag, for a member whose definition needs a caveat.
    """

    taxonomy: str
    tag: str
    plus: str | None = None
    flip_sign: bool = False
    note: str | None = None

    @property
    def is_sum(self) -> bool:
        """Whether this member sums two separately-tagged components.

        ``SGA`` is the only member in :data:`CHAINS` for which this is true, and that is asserted
        (``test_tags::test_exactly_one_member_is_a_sum``). A filer reports either the combined
        ``SellingGeneralAndAdministrativeExpense`` *or* ``GeneralAndAdministrativeExpense`` and
        ``SellingAndMarketingExpense`` separately; substituting one component for the combined
        figure understates the metric by the other, silently, and Piotroski's margin test would then
        improve for a filer that merely changed its presentation.

        The construct spreads once it exists — several tier-2 concepts have a plausible
        components-summing reading — so the registry gets the same treatment M1 gave the ``dei``
        carve-out: a test names the one use, and a second is a visible edit with a reviewer
        attached.
        """
        return self.plus is not None

    @property
    def tags(self) -> tuple[str, ...]:
        """Every tag this member requires, in declaration order."""
        return (self.tag,) if self.plus is None else (self.tag, self.plus)

    @property
    def keys(self) -> tuple[tuple[str, str], ...]:
        """``(taxonomy, tag)`` pairs, the key ``CompanyFacts.facts`` is mapped by."""
        return tuple((self.taxonomy, tag) for tag in self.tags)

    @property
    def qualified_tags(self) -> tuple[str, ...]:
        """``("us-gaap:SalesRevenueNet",)`` — the spelling §9.1's appendix prints."""
        return tuple(f"{self.taxonomy}:{tag}" for tag in self.tags)


@dataclass(frozen=True, slots=True)
class Chain:
    """One metric's ordered fallback chain, plus the four properties §4.2 does not name.

    Attributes:
        metric: The metric this chain resolves.
        members: Ordered; first match wins, **per period**.
        aggregation: :class:`Aggregation`.
        unit: The **only** unit accepted. A fact in any other unit is excluded and counted as a
            unit mismatch — see :func:`unit_filter`.
        tier: :class:`Tier`.
        subtractable: Whether residual recovery may subtract this metric.
            ``SHARES_DILUTED_WEIGHTED`` is ``FLOW`` for bucketing — it is a weighted average *over
            a period*, so it has a duration — and is not additive across quarters, so it is
            ``FLOW`` with ``subtractable=False``. Two axes rather than one, because bucketing and
            derivation are asking different questions and a single enum answering both would force
            this metric to lie about one of them.
        sign: :class:`Sign`.
        exclusive: Names of the :data:`EXCLUSIVITY_GROUPS` this chain participates in.
    """

    metric: Metric
    members: tuple[Member, ...]
    aggregation: Aggregation
    unit: str
    tier: Tier
    subtractable: bool = True
    sign: Sign = Sign.AS_FILED
    exclusive: frozenset[str] = field(default_factory=frozenset)

    @property
    def keys(self) -> tuple[tuple[str, str], ...]:
        """Every ``(taxonomy, tag)`` any member of this chain reads."""
        return tuple(key for member in self.members for key in member.keys)

    def index_of(self, tag: str) -> int | None:
        """The chain position of the member whose primary tag is ``tag``."""
        for position, member in enumerate(self.members):
            if member.tag == tag:
                return position
        return None


@dataclass(frozen=True, slots=True)
class Resolution:
    """What one period resolved to. **Absences are returned, not skipped.**

    A resolver that returns only what it found makes the coverage denominator unknowable, and
    ``chain_index`` is what makes a stitch detectable: a metric whose resolutions span more than one
    index used more than one tag, which is the §6.4 data-integrity finding.

    Attributes:
        period: The period requested. For a match, the *filer's own* period — so a fact keeps the
            start date it was filed with, not a reconstructed one.
        facts: Empty is an absence; length one is a filed fact carrying its own ``SourceRef``;
            length greater than one is a ``Derivation`` over all of them. A single
            ``RawFact | None`` cannot carry the value :attr:`Member.is_sum` produces, and computing
            that sum in :mod:`~investo.normalize.facts` would put tag knowledge outside this module
            — which is what the ``us-gaap`` allowlist exists to prevent.
        chain_index: Position in the chain; ``0`` is the preferred tag, ``None`` an absence.
    """

    period: FiscalPeriod
    facts: tuple[RawFact, ...] = ()
    chain_index: int | None = None

    @property
    def is_absent(self) -> bool:
        return not self.facts

    @property
    def tags_used(self) -> tuple[str, ...]:
        """Every qualified tag behind this resolution, so the appendix does not imply one tag
        produced a summed number."""
        return tuple(fact.qualified_tag for fact in self.facts)


@dataclass(frozen=True, slots=True)
class Materialized:
    """A resolved period turned into a :class:`~investo.domain.models.Fact`, and what happened to it.

    The three flags are counted by :mod:`~investo.normalize.statements` into
    ``MetricCoverage``. They are reported rather than acted on: a fact whose sign contradicts its
    metric's convention is **kept**, because a negative capex quarter is real — a disposal netted
    against acquisitions — and dropping it makes FCF wrong in the other direction.
    """

    fact: Fact
    sign_anomaly: bool = False
    summed: bool = False
    flipped: bool = False


@dataclass(frozen=True, slots=True)
class ExclusivitySwitch:
    """A filer moved *permanently* between two members of an exclusivity group.

    Distinguished from inconsistent tagging by the shape of the timeline rather than by a
    hand-written exception — see :func:`resolve_series`.
    """

    group: str
    boundary: date
    """The first period end tagged with the later member."""
    tags: tuple[str, ...]
    """Qualified tags, earlier member first."""


@dataclass(frozen=True, slots=True)
class ResolvedSeries:
    """:func:`resolve`'s output plus what the exclusivity pass concluded.

    The exclusivity outcome is a side channel rather than a field on :class:`Resolution` because it
    is a property of the *series*, and because a per-period field would have to be either repeated
    or left mostly ``None``.
    """

    resolutions: tuple[Resolution, ...]
    switches: tuple[ExclusivitySwitch, ...] = ()
    collapsed: tuple[str, ...] = ()
    """Groups resolved by majority-wins, i.e. where the members interleaved."""


class DerivationKind(StrEnum):
    """How a :class:`DerivedMetric` computes its value."""

    METRIC_DIFFERENCE = "metric_difference"
    """``a − b`` over two already-resolved metrics."""

    METRIC_RATIO = "metric_ratio"
    """``a / b`` over two already-resolved metrics. The one place in M2 where the output unit is not
    an input unit."""

    TAG_DIFFERENCE = "tag_difference"
    """``a − b`` over two tags this derivation names itself — see the module docstring on why
    liabilities cannot compose over ``Metric.EQUITY``."""


@dataclass(frozen=True, slots=True)
class DerivedMetric:
    """A metric computable from others, for the periods its chain did not fill.

    Attributes:
        metric: What is produced.
        rule: The ``Derivation.rule`` recorded on the result.
        kind: :class:`DerivationKind`.
        metric_inputs: For the metric forms — resolved series this derivation reads.
        tag_inputs: For :attr:`DerivationKind.TAG_DIFFERENCE` — minuend then subtrahend.
        fallback_subtrahend: A weaker subtrahend, used only when :attr:`tag_inputs`' second member
            is absent for that period. Its use is a finding, because approximating is better than
            omitting the metric and doing it invisibly is neither.
        note: Recorded on the ``Derivation``.
    """

    metric: Metric
    rule: str
    kind: DerivationKind
    metric_inputs: tuple[Metric, ...] = ()
    tag_inputs: tuple[Member, ...] = ()
    fallback_subtrahend: Member | None = None
    note: str | None = None

    @property
    def depends_on(self) -> frozenset[Metric]:
        """Metrics that must be resolved before this one runs. Empty for the tag forms."""
        return frozenset(self.metric_inputs)


# ---------------------------------------------------------------------------
# tier 1 — the DCF metric set (DESIGN.md §4.2)
# ---------------------------------------------------------------------------
# Entity counts in the comments are §4.2's, from the CY2025Q1 frames API. They are reproduced so
# the ordering is auditable rather than folkloric: a chain is an ordering over *substitutes*, and
# the evidence that one tag substitutes for another is how many filers use each.
_REVENUE_606_EXCLUDING: Final = "RevenueFromContractWithCustomerExcludingAssessedTax"
_REVENUE_606_INCLUDING: Final = "RevenueFromContractWithCustomerIncludingAssessedTax"
_EQUITY_WITH_NCI: Final = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"

_TIER_1: Final[tuple[Chain, ...]] = (
    Chain(
        metric=Metric.REVENUE,
        members=(
            Member(GAAP, _REVENUE_606_EXCLUDING),  # 2,543 entities
            Member(GAAP, _REVENUE_606_INCLUDING),
            Member(GAAP, "Revenues"),  # 1,836
            # Deliberately **not** in `revenue_assessed_tax`: this is the pre-2018 concept the
            # whole series has to stitch to. The stitch is a *temporal* substitution across a
            # standards boundary; the assessed-tax pair is a *definitional* substitution within one
            # standard. One is required and one is forbidden, and the group is what tells them
            # apart.
            Member(GAAP, "SalesRevenueNet", note="pre-ASC 606"),
        ),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.DCF,
        exclusive=frozenset({"revenue_assessed_tax"}),
    ),
    Chain(
        metric=Metric.NET_INCOME,
        members=(
            Member(GAAP, "NetIncomeLoss", note="parent-only"),  # 5,315
            Member(GAAP, "ProfitLoss", note="includes noncontrolling interest"),  # 2,724
        ),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.DCF,
        exclusive=frozenset({"net_income_scope"}),
    ),
    Chain(
        # `GrossProfit` (2,023) then DERIVATIONS' `REVENUE − COGS`, per period.
        metric=Metric.GROSS_PROFIT,
        members=(Member(GAAP, "GrossProfit"),),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.DCF,
    ),
    Chain(
        metric=Metric.OPERATING_INCOME,
        members=(Member(GAAP, "OperatingIncomeLoss"),),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.DCF,
    ),
    Chain(
        metric=Metric.ASSETS,
        members=(Member(GAAP, "Assets"),),  # 5,633
        aggregation=Aggregation.INSTANT,
        unit="USD",
        tier=Tier.DCF,
    ),
    Chain(
        # `Liabilities` (4,998) then DERIVATIONS' L&SE − including-NCI equity, for the ~11% of
        # filers §4.2 says never tag it at all.
        metric=Metric.LIABILITIES,
        members=(Member(GAAP, "Liabilities"),),
        aggregation=Aggregation.INSTANT,
        unit="USD",
        tier=Tier.DCF,
    ),
    Chain(
        metric=Metric.EQUITY,
        members=(Member(GAAP, "StockholdersEquity", note="parent-only"),),  # 5,452
        aggregation=Aggregation.INSTANT,
        unit="USD",
        tier=Tier.DCF,
    ),
    Chain(
        # **No fallback to `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents`.** §4.2:
        # the latter includes restricted cash and ties to the cash-flow statement; they are
        # different numbers, and a chain is an ordering over substitutes. Adding it would make
        # §5.4's EV bridge add restricted cash to equity value for some filers and not others,
        # with nothing in the output distinguishing them.
        metric=Metric.CASH,
        members=(Member(GAAP, "CashAndCashEquivalentsAtCarryingValue"),),  # 4,508
        aggregation=Aggregation.INSTANT,
        unit="USD",
        tier=Tier.DCF,
    ),
    Chain(
        metric=Metric.OPERATING_CASH_FLOW,
        members=(
            Member(GAAP, "NetCashProvidedByUsedInOperatingActivities"),  # 4,784
            Member(GAAP, "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),  # 205
        ),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.DCF,
    ),
    Chain(
        metric=Metric.CAPEX,
        members=(
            Member(GAAP, "PaymentsToAcquirePropertyPlantAndEquipment"),  # 2,696
            Member(GAAP, "PaymentsToAcquireProductiveAssets"),
            Member(GAAP, "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets"),
            Member(GAAP, "PaymentsForCapitalImprovements"),
        ),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.DCF,
        sign=Sign.OUTFLOW_POSITIVE,
    ),
    Chain(
        # §4.2: "the weakest of the set. Expect misses; mark leverage metrics low-confidence."
        # 1,532 filers of ~5,000 tag the preferred member, which is why `docs/m2/05-testing.md` §3
        # says a gate forcing 90% here will be met by a chain member that means something slightly
        # different.
        metric=Metric.LONG_TERM_DEBT,
        members=(
            Member(GAAP, "LongTermDebtNoncurrent"),  # 1,532
            Member(GAAP, "LongTermDebt"),
            Member(GAAP, "LongTermDebtAndCapitalLeaseObligations", note="includes capital leases"),
        ),
        aggregation=Aggregation.INSTANT,
        unit="USD",
        tier=Tier.DCF,
        exclusive=frozenset({"debt_lease_scope"}),
    ),
    Chain(
        # The one non-`us-gaap` member, and the tag literal is **imported** from `domain/models.py`
        # rather than repeated here. M1 named it there so no module under `ingest/` names a tag at
        # all, and `tests/test_layering.py` asserts that `dei` allowlist holds exactly one entry;
        # spelling it a second time in this file would put the same string in two places, which is
        # how the two come to disagree.
        metric=Metric.SHARES_COVER,
        members=(Member(COVER_SHARES_TAXONOMY, COVER_SHARES_TAG, note="cover page; market cap only"),),
        aggregation=Aggregation.INSTANT,
        unit="shares",
        tier=Tier.DCF,
    ),
    Chain(
        metric=Metric.SHARES_DILUTED_WEIGHTED,
        members=(Member(GAAP, "WeightedAverageNumberOfDilutedSharesOutstanding"),),
        aggregation=Aggregation.FLOW,
        unit="shares",
        tier=Tier.DCF,
        subtractable=False,
    ),
    Chain(
        # `USD/shares`, not `USD` — §4.2 says so explicitly, and some filers do tag one under `USD`.
        # A resolver that ignores unit reports an EPS three orders of magnitude off.
        metric=Metric.EPS_DILUTED,
        members=(
            Member(GAAP, "EarningsPerShareDiluted"),  # 4,605
            Member(GAAP, "EarningsPerShareBasicAndDiluted"),
        ),
        aggregation=Aggregation.PER_SHARE,
        unit="USD/shares",
        tier=Tier.DCF,
    ),
)


# ---------------------------------------------------------------------------
# tier 2 — what M4's F/Z/M scores need
# ---------------------------------------------------------------------------
# **Provisional orderings.** See the module docstring: §4.2 names these concepts and does not order
# them, and no entity counts exist for them yet. Revised in the same commit as
# `docs/m2/COVERAGE.md`.
_TIER_2: Final[tuple[Chain, ...]] = (
    Chain(
        metric=Metric.ASSETS_CURRENT,
        members=(Member(GAAP, "AssetsCurrent"),),
        aggregation=Aggregation.INSTANT,
        unit="USD",
        tier=Tier.QUALITY,
    ),
    Chain(
        metric=Metric.LIABILITIES_CURRENT,
        members=(Member(GAAP, "LiabilitiesCurrent"),),
        aggregation=Aggregation.INSTANT,
        unit="USD",
        tier=Tier.QUALITY,
    ),
    Chain(
        metric=Metric.RETAINED_EARNINGS,
        members=(Member(GAAP, "RetainedEarningsAccumulatedDeficit"),),
        aggregation=Aggregation.INSTANT,
        unit="USD",
        tier=Tier.QUALITY,
    ),
    Chain(
        metric=Metric.RECEIVABLES,
        members=(
            Member(GAAP, "AccountsReceivableNetCurrent"),
            Member(GAAP, "ReceivablesNetCurrent"),
            Member(GAAP, "AccountsReceivableGrossCurrent", note="gross of allowance"),
        ),
        aggregation=Aggregation.INSTANT,
        unit="USD",
        tier=Tier.QUALITY,
    ),
    Chain(
        metric=Metric.COGS,
        members=(
            Member(GAAP, "CostOfGoodsAndServicesSold"),
            Member(GAAP, "CostOfRevenue"),
            Member(GAAP, "CostOfGoodsSold"),
            Member(GAAP, "CostOfServices"),
        ),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.QUALITY,
    ),
    Chain(
        metric=Metric.SGA,
        members=(
            Member(GAAP, "SellingGeneralAndAdministrativeExpense"),
            # The one summing member in the registry, and the reason `Resolution.facts` is a tuple.
            Member(
                GAAP,
                "GeneralAndAdministrativeExpense",
                plus="SellingAndMarketingExpense",
                note="summed components; both required",
            ),
        ),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.QUALITY,
    ),
    Chain(
        metric=Metric.DEPRECIATION_AMORTIZATION,
        members=(
            Member(GAAP, "DepreciationDepletionAndAmortization"),
            Member(GAAP, "DepreciationAmortizationAndAccretionNet"),
            Member(GAAP, "Depreciation", note="excludes amortization"),
        ),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.QUALITY,
    ),
    Chain(
        metric=Metric.INTEREST_EXPENSE,
        members=(
            Member(GAAP, "InterestExpense"),
            Member(GAAP, "InterestExpenseNonoperating"),
            # Signed opposite to the metric by construction: a net *expense* appears negative here.
            Member(
                GAAP,
                "InterestIncomeExpenseNet",
                flip_sign=True,
                note="net of interest income; sign inverted to expense-positive",
            ),
        ),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.QUALITY,
        sign=Sign.EXPENSE_POSITIVE,
    ),
    Chain(
        metric=Metric.SBC,
        members=(
            Member(GAAP, "ShareBasedCompensation"),
            Member(GAAP, "AllocatedShareBasedCompensationExpense"),
        ),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.QUALITY,
    ),
    Chain(
        metric=Metric.OPERATING_LEASE_LIABILITY,
        members=(
            Member(GAAP, "OperatingLeaseLiabilityNoncurrent"),
            Member(GAAP, "OperatingLeaseLiability", note="includes the current portion"),
        ),
        aggregation=Aggregation.INSTANT,
        unit="USD",
        tier=Tier.QUALITY,
    ),
    Chain(
        metric=Metric.SHARE_ISSUANCE_PROCEEDS,
        members=(
            Member(GAAP, "ProceedsFromIssuanceOfCommonStock"),
            Member(GAAP, "ProceedsFromIssuanceOrSaleOfEquity"),
        ),
        aggregation=Aggregation.FLOW,
        unit="USD",
        tier=Tier.QUALITY,
    ),
)

CHAINS: Final[Mapping[Metric, Chain]] = {chain.metric: chain for chain in (*_TIER_1, *_TIER_2)}
"""Every metric's chain. **Complete over ``Metric``, and that completeness is a test.**

M1 declared both tiers of :class:`~investo.domain.models.Metric` even though nothing mapped to them
yet, on the grounds that an unmapped metric is then a visible failure rather than a metric nobody
thought of. ``test_tags::test_every_metric_has_a_chain`` iterates the enum — not a literal list —
which is what makes that reasoning pay out.

**EBIT is deliberately absent.** §4.2 lists "EBIT (derived)" among tier 2, and it is
``OPERATING_INCOME``, or ``NET_INCOME + INTEREST_EXPENSE + tax`` where operating income is absent.
Which of the two depends on what Altman's variant needs, which is M4's question over M2's output and
answerable without a new ``Metric`` member — while adding one here that no chain maps to would break
the completeness test for a value that is a one-line arithmetic in the consumer.
"""

EXCLUSIVITY_GROUPS: Final[Mapping[str, frozenset[str]]] = {
    # §4.2 states it directly: the two differ by sales and excise taxes collected, and for a filer
    # with material excise a mixed series has a visible discontinuity that looks like growth.
    "revenue_assessed_tax": frozenset({_REVENUE_606_EXCLUDING, _REVENUE_606_INCLUDING}),
    # Parent-only versus including-NCI. Mixing produces a series whose year-over-year change is
    # partly a change in minority interest.
    "net_income_scope": frozenset({"NetIncomeLoss", "ProfitLoss"}),
    # The second includes capitalized leases and the first does not; §5.3 treats leases as debt
    # separately, so a mixed series double-counts leases in some years.
    "debt_lease_scope": frozenset({"LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"}),
}
"""Sets of chain members of which **at most one may contribute to a series** (§4.2.1).

This — not the resolution algorithm — is what enforces §4.2's "never mix within one series". Which
tags are mutually incompatible is domain knowledge about the taxonomy, not a property of any filer's
data, so it belongs in the declaration.
"""

DERIVATIONS: Final[tuple[DerivedMetric, ...]] = (
    DerivedMetric(
        metric=Metric.GROSS_PROFIT,
        rule="gross_profit_from_revenue_minus_cogs",
        kind=DerivationKind.METRIC_DIFFERENCE,
        metric_inputs=(Metric.REVENUE, Metric.COGS),
    ),
    DerivedMetric(
        # **Not** `L&SE − Metric.EQUITY`. See the module docstring: the parent-only tag overstates
        # liabilities by exactly the noncontrolling interest.
        metric=Metric.LIABILITIES,
        rule="liabilities_from_lse_minus_equity",
        kind=DerivationKind.TAG_DIFFERENCE,
        tag_inputs=(
            Member(GAAP, "LiabilitiesAndStockholdersEquity"),
            Member(GAAP, _EQUITY_WITH_NCI),
        ),
        fallback_subtrahend=Member(GAAP, "StockholdersEquity", note="parent-only; approximation"),
        note="L&SE − equity including noncontrolling interest",
    ),
    DerivedMetric(
        # The one place in M2 where the output unit is not an input unit: `USD / shares` ->
        # `USD/shares`. Worth its own test, because a unit check that only ever compares equal is a
        # unit check that has not been exercised.
        metric=Metric.EPS_DILUTED,
        rule="eps_from_net_income_over_diluted_shares",
        kind=DerivationKind.METRIC_RATIO,
        metric_inputs=(Metric.NET_INCOME, Metric.SHARES_DILUTED_WEIGHTED),
    ),
)
"""Cross-metric derivations, **declared in dependency order**.

Four rules govern all of them, and the third is the reason the order is a declaration rather than a
loop over a dict:

1. **Per period, not per series.** A filer that tags ``GrossProfit`` in three years of four gets the
   fourth derived and the other three as filed, and the coverage report distinguishes them.
2. **Every input must be present for the same period.** A derivation over a period an input is
   missing is not attempted, and the metric stays absent.
3. **The order is acyclic**, asserted by ``test_tags_derived::test_derivation_graph_is_acyclic``.
   Nothing here is cyclic today; the test exists because the first cycle would be introduced by a
   plausible-looking addition — deriving ``COGS`` from ``REVENUE − GROSS_PROFIT``, which is exactly
   as true and exactly as tempting — and it would present as a recursion error in a report run
   rather than as a design mistake.
4. **The ``Derivation`` is recursive and is not flattened.** ``EPS_DILUTED`` over an as-filed
   ``NET_INCOME`` carries one level; a derived margin over a stitched revenue series carries three.
"""


def identity[T](value: T) -> T:
    """Return ``value`` unchanged. The key function for a sort over something already total.

    ``docs/m2/02-facts.md`` §9's AST rule fails any ``sorted``, ``min``, ``max`` or ``.sort`` under
    ``normalize/`` or ``report/`` that omits ``key=``, deliberately bluntly: the failure it prevents —
    ranking facts through ``FiscalPeriod``'s partial order — is invisible in every run that happens to
    agree, so the rule cannot afford to guess which sorts are safe.

    Sorting a list of dates is safe. Writing ``key=identity`` says *this sort's key is the value
    itself*, which is a claim a reviewer can check, where a bare ``sorted(dates)`` is indistinguishable
    from a sort that forgot. It lives here rather than in ``facts.py`` because this is the module
    nothing else in the two trees can create a cycle with.
    """
    return value


def chain_for(metric: Metric) -> Chain:
    """The chain for ``metric``.

    Raises:
        KeyError: if the metric is unmapped, which the completeness test makes impossible.
    """
    return CHAINS[metric]


def metrics_in_tier(tier: Tier) -> tuple[Metric, ...]:
    """Every metric in ``tier``, in registry order.

    Registry order rather than enum order so the two tiers read in the same sequence the tables
    above declare them, which is the order §9.1's appendix prints.
    """
    return tuple(chain.metric for chain in CHAINS.values() if chain.tier is tier)


def uses_including_nci_net_income(tags_used: Iterable[str]) -> bool:
    """Whether a ``NET_INCOME`` series resolved to the including-NCI concept.

    A predicate rather than a constant the caller compares against, because the comparison needs a
    ``us-gaap`` literal and this module is the only one allowed to hold one. §4.2 pairs parent-only
    net income with ``StockholdersEquity``, which is also parent-only, so a filer resolving to
    ``ProfitLoss`` produces an ROE whose numerator and denominator have different scopes — recorded
    as the ``net_income_scope_mismatch`` finding, not silently corrected.
    """
    return f"{GAAP}:ProfitLoss" in set(tags_used)


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------
def unit_filter(
    chain: Chain, facts: Sequence[RawFact]
) -> tuple[tuple[RawFact, ...], tuple[RawFact, ...]]:
    """Split ``facts`` into those in the chain's declared unit and those excluded.

    Three things this catches, all of which §4.2 warns about and none of which anything else in the
    pipeline sees:

    - **EPS arrives under ``USD/shares``.** Some filers tag one under ``USD``, and a resolver that
      ignores unit reports an EPS three orders of magnitude off.
    - **Non-USD reporting currencies.** §12 records these as out of scope for the US-only universe;
      the filter turns "out of scope" from a comment into an absence that appears in the coverage
      report, which is the difference between a known limitation and a wrong number.
    - **``pure``-unit facts under a money tag.** The ``ARXS`` fixture carries decimal ratios under a
      ``us-gaap`` concept; without the filter one of those contributes to a dollar series.

    The check is on the verbatim :attr:`~investo.domain.models.RawFact.unit` string, which M1
    preserves as the units-dict key without interpretation. **No unit conversion happens anywhere
    in M2**: a thousands-denominated filing is not rescaled, because ``companyfacts`` values are
    already in the unit named and a scaling step would be a second place for a factor-of-1000 error
    to live.

    Returns:
        ``(kept, excluded)``. The excluded facts are returned rather than counted so the caller can
        report *which* units were seen — a metric absent because every fact was ``EUR`` is a
        different finding from one absent because the tag was never used.
    """
    kept = tuple(fact for fact in facts if fact.unit == chain.unit)
    excluded = tuple(fact for fact in facts if fact.unit != chain.unit)
    return kept, excluded


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
def resolve(
    metric: Metric,
    facts: Mapping[tuple[str, str], Sequence[RawFact]],
    *,
    periods: Iterable[FiscalPeriod],
) -> tuple[Resolution, ...]:
    """Resolve ``metric`` for each requested period, period-wise. One result per period.

    The documented interface (``docs/m2/01-tags.md`` § The interface). :func:`resolve_series` is the
    same computation with the exclusivity outcome attached, which the caller needs in order to emit
    the finding.

    Args:
        metric: What to resolve.
        facts: ``(taxonomy, tag) -> facts``, **already ``as_of``-filtered, unit-filtered and
            deduped**. This module does none of those; see
            :mod:`~investo.normalize.facts` § pipeline order for why that order and not the other.
        periods: The periods to resolve, including ones expected to be absent. Absences are
            returned, not skipped.
    """
    return resolve_series(metric, facts, periods=periods).resolutions


def resolve_series(
    metric: Metric,
    facts: Mapping[tuple[str, str], Sequence[RawFact]],
    *,
    periods: Iterable[FiscalPeriod],
) -> ResolvedSeries:
    """:func:`resolve`, plus what the exclusivity pass concluded.

    Two passes, and the second is why the exclusivity check cannot be greedy:

    1. Resolve every period independently through the chain.
    2. For each exclusivity group with more than one member represented, decide whether the members
       **partition** the timeline or **interleave**. A contiguous prefix and suffix is a *switch* —
       the filer changed what it tags, on a date — so both are kept and
       :class:`ExclusivitySwitch` records the boundary. Alternation is noise, so the member with the
       most periods wins and every period it did not cover is re-resolved with the losers removed;
       ties break to the earlier chain index.

    A greedy check would lock in whichever member appeared in the earliest period, and the earliest
    period in a window is the one most likely to be off-pattern legacy tagging. Majority-wins alone
    would push a permanent switch's minority side silently down the chain to a weaker tag — the same
    confidently-wrong shape the period-wise decision exists to avoid, one level down.
    """
    chain = chain_for(metric)
    requested = _ordered_periods(periods)
    resolutions = tuple(_resolve_one(chain, facts, period, excluded=frozenset()) for period in requested)

    switches: list[ExclusivitySwitch] = []
    collapsed: list[str] = []
    for group in sorted(chain.exclusive, key=identity):
        members = EXCLUSIVITY_GROUPS[group]
        present = _members_present(chain, resolutions, members)
        if len(present) < 2:
            continue
        boundary = _switch_boundary(chain, resolutions, present)
        if boundary is not None:
            ordered = _tags_in_time_order(chain, resolutions, present)
            switches.append(
                ExclusivitySwitch(
                    group=group,
                    boundary=boundary,
                    tags=tuple(f"{chain.members[index].taxonomy}:{tag}" for index, tag in ordered),
                )
            )
            continue
        collapsed.append(group)
        winner = _majority_member(chain, resolutions, present)
        losers = frozenset(members - {winner})
        resolutions = tuple(
            resolution
            if _primary_tag(chain, resolution) not in losers
            else _resolve_one(chain, facts, resolution.period, excluded=losers)
            for resolution in resolutions
        )

    return ResolvedSeries(
        resolutions=resolutions,
        switches=tuple(switches),
        collapsed=tuple(collapsed),
    )


def materialize(chain: Chain, resolution: Resolution) -> Materialized | None:
    """Turn one resolved period into a :class:`~investo.domain.models.Fact`, or ``None`` if absent.

    Provenance is chosen by what actually happened, and the three cases are distinguishable in the
    appendix:

    - one filed fact, no flip: the bare ``SourceRef``, so ``report.json`` stays small;
    - a flip: ``Derivation(rule="sign_normalized")`` over the single ref, with the convention named
      in the note — because a sign that changes with no record is indistinguishable from a filer
      error;
    - a summing member: ``Derivation(rule="sga_summed_components")`` naming **both** refs, so the
      appendix does not imply one tag produced the number.
    """
    if resolution.is_absent or resolution.chain_index is None:
        return None
    member = chain.members[resolution.chain_index]
    total = sum((fact.value for fact in resolution.facts), Decimal(0))
    source = _provenance_for(chain, member, resolution)
    value = -total if member.flip_sign else total
    return Materialized(
        fact=Fact(
            metric=chain.metric,
            value=value,
            period=resolution.facts[0].period,
            source=source,
            unit=chain.unit,
        ),
        sign_anomaly=_contradicts_convention(chain, value),
        summed=member.is_sum,
        flipped=member.flip_sign,
    )


def derive(
    spec: DerivedMetric,
    *,
    resolved: Mapping[Metric, Mapping[tuple[date, PeriodKind], Fact]],
    raw: Mapping[tuple[str, str], Sequence[RawFact]],
    periods: Iterable[FiscalPeriod],
) -> tuple[tuple[Fact, bool], ...]:
    """Compute ``spec`` for each period it can fire on.

    Fires only where the metric's own chain left the period empty — which is the caller's business,
    so ``periods`` should already exclude the filled ones.

    Args:
        spec: The derivation to run.
        resolved: Already-resolved series, keyed by metric then by ``(period.end, period.kind)``.
            The key is the pair ``FiscalPeriod`` itself compares on, so a derivation matches an
            instant against an instant and a duration against a duration.
        raw: ``(taxonomy, tag) -> facts``, filtered and deduped, for the tag forms.
        periods: Candidate periods.

    Returns:
        ``(fact, approximated)`` pairs. ``approximated`` is ``True`` where
        :attr:`DerivedMetric.fallback_subtrahend` was used, which the caller turns into the
        ``liabilities_nci_approximated`` finding. Approximating beats omitting the metric; doing it
        invisibly is neither.
    """
    chain = chain_for(spec.metric)
    produced: list[tuple[Fact, bool]] = []
    for period in _ordered_periods(periods):
        key = (period.end, period.kind)
        if spec.kind is DerivationKind.TAG_DIFFERENCE:
            pair = _tag_operands(spec, raw, key)
            if pair is None:
                continue
            minuend, subtrahend, approximated = pair
            if minuend.unit != subtrahend.unit or minuend.unit != chain.unit:
                continue
            produced.append(
                (
                    Fact(
                        metric=spec.metric,
                        value=minuend.value - subtrahend.value,
                        period=minuend.period,
                        source=Derivation(
                            rule=spec.rule,
                            inputs=(minuend.source, subtrahend.source),
                            note=_derivation_note(spec, approximated=approximated),
                        ),
                        unit=chain.unit,
                    ),
                    approximated,
                )
            )
            continue

        operands = _metric_operands(spec, resolved, key)
        if operands is None:
            continue
        left, right = operands
        if spec.kind is DerivationKind.METRIC_DIFFERENCE:
            if left.unit != right.unit or left.unit != chain.unit:
                continue
            value = left.value - right.value
        else:
            # The ratio form: `USD / shares -> USD/shares`, the one place in M2 where the output
            # unit is not an input unit. A zero denominator is skipped rather than raised — a filer
            # reporting zero weighted-average shares is malformed data, and M2's answer to malformed
            # data is an absence with a coverage entry, not an exception.
            if right.value == 0:
                continue
            if f"{left.unit}/{right.unit}" != chain.unit:
                continue
            value = left.value / right.value
        produced.append(
            (
                Fact(
                    metric=spec.metric,
                    value=value,
                    period=left.period,
                    source=Derivation(
                        rule=spec.rule,
                        inputs=(left.source, right.source),
                        note=spec.note,
                    ),
                    unit=chain.unit,
                ),
                False,
            )
        )
    return tuple(produced)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _ordered_periods(periods: Iterable[FiscalPeriod]) -> tuple[FiscalPeriod, ...]:
    """Deduplicate and order the requested periods on a **total** key.

    ``FiscalPeriod`` compares on ``(end, kind)`` with ``start`` excluded, so sorting bare periods
    leaves ties to input order — which descends from dict iteration over a parsed payload
    (``docs/m2/02-facts.md`` §9). Naming ``start`` in the key makes the order a function of the
    values.
    """
    unique = {(period.end, period.kind): period for period in periods}
    return tuple(
        unique[key]
        for key in sorted(unique, key=lambda pair: (pair[0], pair[1], unique[pair].start or date.min))
    )


def _candidate_key(fact: RawFact) -> tuple[date, str, str, str]:
    """A total order over facts competing for one period.

    Dedup has already collapsed ``(taxonomy, tag, unit, start, end)``, so two facts reach here only
    when their ``start`` dates differ by a day or two — a filer recording the same quarter with
    inconsistent boundaries. The most recently filed one wins, and the accession breaks a same-day
    tie, for the same reason dedup does it that way: the alternative is payload iteration order.
    """
    return (fact.source.filed, fact.source.accession.value, fact.tag, fact.unit)


def _facts_for_period(
    facts: Mapping[tuple[str, str], Sequence[RawFact]],
    key: tuple[str, str],
    period: FiscalPeriod,
) -> RawFact | None:
    """The single fact under ``key`` covering ``period``, or ``None``.

    Matching is on ``(end, kind)`` — ``FiscalPeriod``'s own equality — which is §4.2's grouping rule
    and is what makes a one-day disagreement in ``start`` not lose a period.
    """
    candidates = [fact for fact in facts.get(key, ()) if fact.period == period]
    if not candidates:
        return None
    return max(candidates, key=_candidate_key)


def _resolve_one(
    chain: Chain,
    facts: Mapping[tuple[str, str], Sequence[RawFact]],
    period: FiscalPeriod,
    *,
    excluded: frozenset[str],
) -> Resolution:
    """Walk the chain once for one period. First match wins."""
    for index, member in enumerate(chain.members):
        if member.tag in excluded or (member.plus is not None and member.plus in excluded):
            continue
        found = tuple(
            fact
            for fact in (_facts_for_period(facts, key, period) for key in member.keys)
            if fact is not None
        )
        # A summing member matches only when **every** component is present for that period.
        # Substituting one component for the combined figure understates the metric by the other,
        # silently.
        if len(found) != len(member.keys):
            continue
        return Resolution(period=period, facts=found, chain_index=index)
    return Resolution(period=period)


def _primary_tag(chain: Chain, resolution: Resolution) -> str | None:
    """The tag of the member that won this period, or ``None`` for an absence."""
    if resolution.chain_index is None:
        return None
    return chain.members[resolution.chain_index].tag


def _members_present(
    chain: Chain, resolutions: Sequence[Resolution], members: frozenset[str]
) -> frozenset[str]:
    """Which of ``members`` actually contributed to this series."""
    return frozenset(
        tag
        for tag in (_primary_tag(chain, resolution) for resolution in resolutions)
        if tag is not None and tag in members
    )


def _tags_in_time_order(
    chain: Chain, resolutions: Sequence[Resolution], present: frozenset[str]
) -> tuple[tuple[int, str], ...]:
    """``(chain index, tag)`` for each present member, ordered by first appearance in time."""
    seen: dict[str, FiscalPeriod] = {}
    for resolution in sorted(resolutions, key=lambda r: (r.period.end, r.period.kind)):
        tag = _primary_tag(chain, resolution)
        if tag is not None and tag in present and tag not in seen:
            seen[tag] = resolution.period
    return tuple(
        (chain.index_of(tag) or 0, tag)
        for tag in sorted(seen, key=lambda tag: (seen[tag].end, tag))
    )


def _switch_boundary(
    chain: Chain, resolutions: Sequence[Resolution], present: frozenset[str]
) -> date | None:
    """The date a permanent switch happened, or ``None`` if the members interleave.

    A switch is exactly two members whose periods form a contiguous prefix and a contiguous suffix
    — one run each. Three or more members, or any alternation, is inconsistent tagging with no event
    behind it, and majority-wins is the right answer for that.

    The test that separates the two cases is the point of the rule: a fixture that only flip-flops
    would let a stitch-everything implementation pass, so ``TIER2`` carries both shapes.
    """
    if len(present) != 2:
        return None
    sequence = [
        tag
        for tag in (
            _primary_tag(chain, resolution)
            for resolution in sorted(resolutions, key=lambda r: (r.period.end, r.period.kind))
        )
        if tag is not None and tag in present
    ]
    runs: list[str] = []
    for tag in sequence:
        if not runs or runs[-1] != tag:
            runs.append(tag)
    if len(runs) != 2:
        return None
    later = runs[1]
    for resolution in sorted(resolutions, key=lambda r: (r.period.end, r.period.kind)):
        if _primary_tag(chain, resolution) == later:
            return resolution.period.end
    return None


def _majority_member(
    chain: Chain, resolutions: Sequence[Resolution], present: frozenset[str]
) -> str:
    """The member with the most periods resolved; ties break to the earlier chain index."""
    counts = {tag: 0 for tag in present}
    for resolution in resolutions:
        tag = _primary_tag(chain, resolution)
        if tag in counts:
            counts[tag] += 1
    return min(counts, key=lambda tag: (-counts[tag], chain.index_of(tag) or 0, tag))


def _provenance_for(chain: Chain, member: Member, resolution: Resolution) -> Provenance:
    """The provenance a materialized fact carries — see :func:`materialize`."""
    refs = tuple(fact.source for fact in resolution.facts)
    if member.is_sum:
        summed = Derivation(
            rule="sga_summed_components",
            inputs=refs,
            note=f"{' + '.join(member.qualified_tags)}",
        )
        if not member.flip_sign:
            return summed
        return Derivation(
            rule="sign_normalized",
            inputs=(summed,),
            note=f"{chain.sign} convention",
        )
    if member.flip_sign:
        return Derivation(
            rule="sign_normalized",
            inputs=refs,
            note=f"{member.qualified_tags[0]} is signed opposite to the {chain.sign} convention",
        )
    return refs[0]


def _contradicts_convention(chain: Chain, value: Decimal) -> bool:
    """Whether ``value`` contradicts the chain's declared sign convention.

    Zero contradicts nothing. A metric with no convention has no anomaly, which is why this is a
    property of :class:`Sign` rather than a test on the value alone.
    """
    return chain.sign.imposes_a_convention and value < 0


def _metric_operands(
    spec: DerivedMetric,
    resolved: Mapping[Metric, Mapping[tuple[date, PeriodKind], Fact]],
    key: tuple[date, PeriodKind],
) -> tuple[Fact, Fact] | None:
    """Both metric inputs for one period, or ``None`` if either is missing."""
    found: list[Fact] = []
    for metric in spec.metric_inputs:
        fact = resolved.get(metric, {}).get(key)
        if fact is None:
            return None
        found.append(fact)
    if len(found) != 2:
        return None
    return found[0], found[1]


def _tag_operands(
    spec: DerivedMetric,
    raw: Mapping[tuple[str, str], Sequence[RawFact]],
    key: tuple[date, PeriodKind],
) -> tuple[RawFact, RawFact, bool] | None:
    """Minuend, subtrahend and whether the fallback was used, for one period."""
    minuend_member, subtrahend_member = spec.tag_inputs
    minuend = _raw_at(raw, minuend_member, key)
    if minuend is None:
        return None
    subtrahend = _raw_at(raw, subtrahend_member, key)
    if subtrahend is not None:
        return minuend, subtrahend, False
    if spec.fallback_subtrahend is None:
        return None
    fallback = _raw_at(raw, spec.fallback_subtrahend, key)
    if fallback is None:
        return None
    return minuend, fallback, True


def _raw_at(
    raw: Mapping[tuple[str, str], Sequence[RawFact]],
    member: Member,
    key: tuple[date, PeriodKind],
) -> RawFact | None:
    """The single fact for ``member`` at ``key``, on the same total order resolution uses."""
    candidates = [
        fact
        for fact in raw.get((member.taxonomy, member.tag), ())
        if (fact.period.end, fact.period.kind) == key
    ]
    if not candidates:
        return None
    return max(candidates, key=_candidate_key)


def _derivation_note(spec: DerivedMetric, *, approximated: bool) -> str | None:
    """The note recorded on a tag-form derivation, naming the approximation when one was made."""
    if not approximated or spec.fallback_subtrahend is None:
        return spec.note
    fallback = spec.fallback_subtrahend.qualified_tags[0]
    return f"{spec.note}; subtrahend approximated with {fallback} (parent-only)"

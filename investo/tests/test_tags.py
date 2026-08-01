"""The chain registry and the resolver: the decision M2 turns on, and the four properties §4.2 omits.

`docs/m2/01-tags.md` is the design; DESIGN.md §4.2 and §4.2.1 are normative. The module under test is
the only one in the package allowed a `us-gaap` literal, so most of what is asserted here is *about
the table* — completeness over `Metric`, one summing member, groups whose members belong to the chain
they are declared on. A registry that has drifted from the enum is not a bug that shows up as a
crash; it is a metric nobody thought of, which is the exact failure ROADMAP M2 declared both tiers
early to prevent.

The resolution tests split into two halves, and the second is the one the milestone turns on:

- **Period-wise resolution.** Apple's revenue is `SalesRevenueNet` for FY2016-17 and the ASC 606 tag
  from FY2018. Series-level resolution returns two of those four periods *and reports full coverage
  on the two it keeps* — a hole that presents as full coverage, on the flagship fixture. So the test
  asserts **four** periods and **two** distinct tags, and separately that the tag chosen for FY2018
  does not depend on what else is in the window.
- **Exclusivity, in both shapes.** A filer that switches permanently must be stitched and dated; one
  that alternates must collapse to the majority. Each test's converse is the other's failure mode: a
  stitch-everything implementation passes the first and fails the second, and majority-wins passes
  the second and fails the first. Neither test is meaningful without its pair.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal

import pytest

from investo.domain.models import Fact, Metric, RawFact
from investo.domain.periods import FiscalPeriod, PeriodKind
from investo.domain.provenance import Derivation
from investo.normalize.facts import MetricSeries, normalize_metric, observed_calendar
from investo.normalize.tags import (
    CHAINS,
    DERIVATIONS,
    EXCLUSIVITY_GROUPS,
    GAAP,
    Aggregation,
    Sign,
    Tier,
    chain_for,
    materialize,
    metrics_in_tier,
    resolve,
    resolve_series,
    unit_filter,
    uses_including_nci_net_income,
)
from tests.conftest import M2_WINDOW, company_facts

AAPL = "AAPL.trimmed.json"
TIER2 = "TIER2.trimmed.json"
BADUNIT = "BADUNIT.trimmed.json"

SGA_COMPONENTS = ("GeneralAndAdministrativeExpense", "SellingAndMarketingExpense")
"""The one summing member's two tags. Named here so the "exactly one" test can say *which* one."""


def _series(fixture: str, metric: Metric, *, window: tuple[date, date] = M2_WINDOW) -> MetricSeries:
    """One metric through the pipeline, with the bucketing calendar `build_history` would supply.

    The calendar is not optional for an `INSTANT` metric: a balance-sheet fact carries no duration, so
    without the observed period ends every instant lands in the quarterly bucket and the annual series
    is empty. Passing it here rather than defaulting it in `normalize_metric` is deliberate — the
    calendar is a property of the *company*, not of one metric, and a per-metric default would let
    `ASSETS` and `EQUITY` bucket against different calendars.
    """
    facts = company_facts(fixture).facts
    keys = tuple({key for chain in CHAINS.values() for key in chain.keys})
    annual_ends, quarterly_ends = observed_calendar(facts, keys, window=window, as_of=None)
    return normalize_metric(
        chain_for(metric),
        facts,
        window=window,
        as_of=None,
        annual_ends=annual_ends,
        quarterly_ends=quarterly_ends,
    )


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_every_metric_has_a_chain() -> None:
    """Completeness over `Metric`, **iterated rather than listed**.

    ROADMAP M2's reason for declaring both tiers in M1 was that an unmapped metric is then a visible
    failure rather than a metric nobody thought of — and that only pays out if the assertion walks the
    enum. A literal list of twenty-five names here would go stale in exactly the case the rule exists
    for: someone adds a `Metric` member and no chain.
    """
    missing = [metric for metric in Metric if metric not in CHAINS]
    assert not missing, f"unmapped metrics: {missing}"
    assert set(CHAINS) == set(Metric)


@pytest.mark.spec
def test_the_two_tiers_partition_the_registry() -> None:
    """Every chain is in exactly one tier, and the split is declared rather than inferred.

    ROADMAP M2's exit criterion is stated per tier, so a metric in neither tier is one no criterion
    covers, and a metric in both is counted twice in an aggregate that is supposed to be a mean.
    """
    dcf, quality = set(metrics_in_tier(Tier.DCF)), set(metrics_in_tier(Tier.QUALITY))
    assert dcf | quality == set(Metric)
    assert not dcf & quality


@pytest.mark.spec
def test_exactly_one_member_is_a_sum() -> None:
    """`docs/m2/01-tags.md` §4: the sum variant is used **once**, and this test names it.

    The construct spreads once it exists — several tier-2 concepts have a plausible
    components-summing reading, and each one added is another place where a filer's *presentation*
    choice changes a metric's value. Naming the one use means a second is a visible edit with a
    reviewer attached, rather than a pattern that arrived one row at a time. Same treatment M1 gave
    the `dei` carve-out.
    """
    sums = [
        (chain.metric, member.tags)
        for chain in CHAINS.values()
        for member in chain.members
        if member.is_sum
    ]
    assert len(sums) == 1, f"the sum variant has spread: {sums}"
    assert sums[0][0] is Metric.SGA
    assert sums[0][1] == (SGA_COMPONENTS[0], SGA_COMPONENTS[1])


@pytest.mark.spec
def test_every_exclusivity_group_member_belongs_to_the_chain_that_declares_it() -> None:
    """A group naming a tag no member of that chain uses can never fire.

    That is the failure mode this test exists for, and it is silent: the group looks declared, the
    rule looks enforced, and a filer mixing the two tags produces a mixed series anyway. Checked in
    both directions — every group is claimed by a chain, and every claimed group's members are that
    chain's.
    """
    claimed: dict[str, Metric] = {}
    for chain in CHAINS.values():
        for group in chain.exclusive:
            assert group in EXCLUSIVITY_GROUPS, f"{chain.metric} claims unknown group {group}"
            tags = {member.tag for member in chain.members}
            assert EXCLUSIVITY_GROUPS[group] <= tags, (
                f"{group}'s members are not all in {chain.metric}'s chain"
            )
            claimed[group] = chain.metric
    assert set(claimed) == set(EXCLUSIVITY_GROUPS), "a declared group no chain uses"


@pytest.mark.spec
def test_sales_revenue_net_is_not_in_the_assessed_tax_group() -> None:
    """The distinction the group exists to draw, asserted rather than left to the comment.

    `SalesRevenueNet` is the pre-2018 concept the whole series has to **stitch** to; the assessed-tax
    pair is a definitional substitution within one standard that must never mix. One is required and
    one is forbidden. Putting `SalesRevenueNet` in the group would defeat the ASC 606 stitch — the
    thing period-wise resolution exists to make work — and it would do so by way of a plausible-looking
    one-line edit to the registry.
    """
    assert "SalesRevenueNet" not in EXCLUSIVITY_GROUPS["revenue_assessed_tax"]
    assert EXCLUSIVITY_GROUPS["revenue_assessed_tax"] == {
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    }


@pytest.mark.spec
def test_cash_does_not_fall_back_to_the_restricted_inclusive_concept() -> None:
    """§4.2: they are different numbers, and a chain is an ordering over *substitutes*.

    Adding the restricted-inclusive tag as a fallback would make §5.4's EV bridge add restricted cash
    to equity value for some filers and not others, with nothing in the output distinguishing them.
    The absence is the enforcement, so the absence gets a test — otherwise the next person to see a
    `CASH` miss in a coverage report adds the fallback and improves the number.
    """
    tags = {member.tag for member in chain_for(Metric.CASH).members}
    assert tags == {"CashAndCashEquivalentsAtCarryingValue"}
    assert not any("Restricted" in tag for tag in tags)


@pytest.mark.spec
@pytest.mark.parametrize("metric", list(Metric), ids=lambda m: str(m))
def test_every_chain_declares_a_unit_an_aggregation_and_a_sign(metric: Metric) -> None:
    """The four properties §4.2 omits are non-optional, so a new chain cannot forget one.

    A chain with no unit accepts an EPS under `USD`; one with no aggregation class gets a Q4 derived
    for its balance sheet. Both are defaults-shaped bugs, which is why the fields have no defaults
    worth relying on and this walks the whole registry.
    """
    chain = CHAINS[metric]
    assert chain.metric is metric, "the registry is keyed by a metric the chain disagrees with"
    assert chain.members, "a chain with no members can never resolve"
    assert chain.unit
    assert isinstance(chain.aggregation, Aggregation)
    assert isinstance(chain.sign, Sign)
    assert isinstance(chain.tier, Tier)


@pytest.mark.spec
def test_only_capex_and_interest_expense_impose_a_sign_convention() -> None:
    """§4.2.1 names two; a third would silently re-sign a series M5 already subtracts.

    A convention added to, say, `OPERATING_CASH_FLOW` would flip nothing today — no member carries
    `flip_sign` — but it would start counting sign anomalies on a metric whose filers legitimately
    report negatives, and a flag that fires on healthy filers is worse than no flag.
    """
    conventions = {
        metric: chain.sign for metric, chain in CHAINS.items() if chain.sign.imposes_a_convention
    }
    assert conventions == {
        Metric.CAPEX: Sign.OUTFLOW_POSITIVE,
        Metric.INTEREST_EXPENSE: Sign.EXPENSE_POSITIVE,
    }


@pytest.mark.spec
def test_the_only_flipped_member_is_the_net_interest_concept() -> None:
    """`flip_sign` is a property of the taxonomy element, never a response to the data.

    So the set of flipped members is a fact about the registry that can be asserted in full. A flip
    added because a fixture came out negative is the bug this rules out: it would be right for that
    filer and wrong for every other one using the same tag.
    """
    flipped = {
        (chain.metric, member.tag)
        for chain in CHAINS.values()
        for member in chain.members
        if member.flip_sign
    }
    assert flipped == {(Metric.INTEREST_EXPENSE, "InterestIncomeExpenseNet")}


@pytest.mark.spec
def test_shares_diluted_weighted_is_a_flow_that_is_not_subtractable() -> None:
    """Two axes rather than one, and the reason reads oddly enough to need pinning.

    It is a weighted average *over a period*, so it has a duration and buckets like any flow — and it
    is not additive across quarters, so the annual figure is not the sum of the quarterly ones. A
    single enum answering both questions would force this metric to lie about one of them, and the lie
    that fits is "INSTANT", which would put the annual figure in the wrong bucket.
    """
    chain = chain_for(Metric.SHARES_DILUTED_WEIGHTED)
    assert chain.aggregation is Aggregation.FLOW
    assert chain.subtractable is False
    assert chain_for(Metric.REVENUE).subtractable is True


@pytest.mark.spec
def test_eps_is_measured_in_usd_per_share() -> None:
    """§4.2 says so explicitly, and it is the one chain whose unit is not `USD` or `shares`."""
    assert chain_for(Metric.EPS_DILUTED).unit == "USD/shares"
    assert chain_for(Metric.EPS_DILUTED).aggregation is Aggregation.PER_SHARE


@pytest.mark.spec
def test_the_cover_share_tag_is_the_one_non_gaap_member() -> None:
    """The `dei` carve-out is still singular now that a second tag table exists to hold it.

    M1 named that tag in `domain/models.py` so no module under `ingest/` names any tag at all, and the
    layering test pins the allowlist at one entry. This is the other half: the registry *uses* the
    imported constant rather than spelling it again, so the two cannot disagree.
    """
    members = [
        (chain.metric, member)
        for chain in CHAINS.values()
        for member in chain.members
        if member.taxonomy != GAAP
    ]
    assert len(members) == 1
    metric, member = members[0]
    assert metric is Metric.SHARES_COVER
    assert member.taxonomy == "dei"


# ---------------------------------------------------------------------------
# period-wise resolution
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_aapl_revenue_spans_both_tags() -> None:
    """**Four** annual periods and **two** distinct tags, across the ASC 606 boundary.

    Series-level resolution walks the chain once and takes the ASC 606 tag, which Apple did not use
    before FY2018 — so it returns FY2018 and FY2019 and reports full coverage on them, because from
    the resolver's point of view nothing is missing. The count is what catches that; a test asserting
    only that revenue is non-empty passes under the broken rule.
    """
    series = _series(AAPL, Metric.REVENUE)
    assert len(series.annual.facts) == 4
    assert series.tags_used == (
        f"{GAAP}:SalesRevenueNet",
        f"{GAAP}:RevenueFromContractWithCustomerExcludingAssessedTax",
    )


@pytest.mark.spec
def test_the_stitch_is_ordered_oldest_tag_first() -> None:
    """`tags_used` is printed as `A → B`, so its order is a claim about which came first.

    Reversed, the appendix describes the ASC 606 transition backwards — a caveat that says the filer
    moved *off* the current standard. The order is first-use in time, not chain order, which is why
    the pre-2018 tag comes first despite sitting last in the chain.
    """
    series = _series(AAPL, Metric.REVENUE)
    first_end = min(fact.period.end for fact in series.annual.facts)
    assert first_end.year == 2016
    assert series.tags_used[0].endswith("SalesRevenueNet")


@pytest.mark.spec
def test_fy2018_resolves_to_the_same_tag_at_5y_and_10y() -> None:
    """Resolution is window-independent, which the series-level reading cannot be.

    A series-level primary depends on what is in the window, so the same fiscal year resolves to a
    different tag under `--lookback 5y` and `10y` — and §9.1's appendix would print two different
    answers for one fact with no restatement having occurred. Asserted on the *tag behind FY2018*
    under two windows, because the value is the same either way and would not catch it.
    """
    target = date(2018, 9, 29)
    windows = ((date(2014, 1, 1), date(2020, 6, 30)), (date(2018, 1, 1), date(2020, 6, 30)))
    tags: set[str] = set()
    for window in windows:
        series = normalize_metric(
            chain_for(Metric.REVENUE),
            company_facts(AAPL).facts,
            window=window,
            as_of=None,
        )
        fact = next(f for f in series.annual.facts if f.period.end == target)
        tags.add(_tag_of(fact))
    assert len(tags) == 1, f"FY2018 resolved to {tags} depending on the window"


def _tag_of(fact: Fact) -> str:
    """The qualified tag behind a fact, through either arm of `Provenance`.

    `isinstance` rather than `hasattr`: the union has exactly two arms, and a structural check would
    also match whatever else grows a `refs()` method later.
    """
    source = fact.source
    refs = source.refs() if isinstance(source, Derivation) else (source,)
    return refs[0].qualified_tag or ""


@pytest.mark.spec
def test_absences_are_returned_not_skipped() -> None:
    """`resolve` yields one `Resolution` per requested period, including the empty ones.

    A resolver that returns only what it found makes the coverage denominator unknowable — the caller
    cannot tell an absence from a period it forgot to ask about. Asserted by requesting a period the
    payload has no fact for and getting a result back rather than a short list.
    """
    absent = FiscalPeriod.of(date(2011, 1, 1), date(2011, 12, 31))
    present = FiscalPeriod.of(date(2018, 9, 30), date(2019, 9, 28))
    resolutions = resolve(Metric.REVENUE, company_facts(AAPL).facts, periods=(absent, present))
    assert len(resolutions) == 2
    by_end = {item.period.end: item for item in resolutions}
    assert by_end[absent.end].is_absent
    assert by_end[absent.end].chain_index is None
    assert not by_end[present.end].is_absent


@pytest.mark.spec
def test_chain_index_is_the_position_that_won() -> None:
    """What makes a stitch *detectable* rather than merely present.

    A metric whose resolutions span more than one index used more than one tag, which is the §6.4
    finding. Apple's revenue resolves to the chain's first member after 2018 and its last before,
    so the pair of indices is the evidence — and an implementation that reported `0` for every match
    would satisfy every value assertion in this file.
    """
    facts = company_facts(AAPL).facts
    periods = tuple(
        FiscalPeriod.of(start, end)
        for start, end in (
            (date(2015, 9, 27), date(2016, 9, 24)),
            (date(2018, 9, 30), date(2019, 9, 28)),
        )
    )
    indices = [item.chain_index for item in resolve(Metric.REVENUE, facts, periods=periods)]
    chain = chain_for(Metric.REVENUE)
    assert indices == [
        chain.index_of("SalesRevenueNet"),
        chain.index_of("RevenueFromContractWithCustomerExcludingAssessedTax"),
    ]
    assert indices[0] != indices[1]


@pytest.mark.spec
def test_frame_key_does_not_affect_resolution() -> None:
    """SEC's own dedup selection cannot break a tie in the subject company's history.

    §4.2 records that frame selection is not point-in-time stable — a CY2025Q1 frame can resolve to a
    2026 filing — so letting it influence which fact wins would put a lookahead leak *inside* the
    resolver, where no `as_of` test would find it. Asserted by resolving the same payload with every
    `frame` stripped and comparing the chosen accessions.
    """
    facts = company_facts(AAPL).facts
    stripped = {
        key: tuple(dataclasses.replace(fact, frame="CY2019Q4I") for fact in rows)
        for key, rows in facts.items()
    }
    periods = (FiscalPeriod.of(date(2018, 9, 30), date(2019, 9, 28)),)
    original = resolve(Metric.REVENUE, facts, periods=periods)
    forced = resolve(Metric.REVENUE, stripped, periods=periods)
    assert [item.chain_index for item in original] == [item.chain_index for item in forced]
    assert [f.source.accession for f in original[0].facts] == [
        f.source.accession for f in forced[0].facts
    ]


# ---------------------------------------------------------------------------
# exclusivity, in both shapes
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_partitioned_group_members_are_stitched() -> None:
    """A permanent switch is kept, dated and flagged — **not** collapsed to the majority.

    `TIER2`'s revenue is excluding-assessed-tax for FY2021-22 and including for FY2023-24: a filer
    that changed what it tags, on a date. Majority-wins would push two of the four periods down the
    chain to a weaker tag, silently — the ASC 606 failure recurring one level down, with
    `SalesRevenueNet`'s hand-written exclusion no help because it is specific to that pair.

    The converse test below is what makes this one meaningful: an implementation that stitches
    everything passes here and fails there.
    """
    series = _series(TIER2, Metric.REVENUE)
    assert len(series.annual.facts) == 4, "a stitched switch keeps every period"
    assert len(series.tags_used) == 2
    assert len(series.switches) == 1
    switch = series.switches[0]
    assert switch.group == "revenue_assessed_tax"
    assert switch.boundary == date(2023, 12, 31), "the first period tagged with the later member"
    assert switch.tags[0].endswith("ExcludingAssessedTax")
    assert switch.tags[-1].endswith("IncludingAssessedTax")
    assert not series.collapsed


@pytest.mark.spec
def test_interleaved_group_members_collapse_to_the_majority() -> None:
    """Alternation is noise, so one member wins and the rest of the series is re-resolved.

    `TIER2`'s long-term debt alternates between `LongTermDebt` and
    `LongTermDebtAndCapitalLeaseObligations` across four year ends. There is no event behind that, and
    §5.3 treats leases as debt separately — so a mixed series double-counts leases in some years. The
    two members tie at two periods each, and the tie breaks to the earlier chain index, which leaves
    the other two periods absent rather than silently re-tagged.
    """
    series = _series(TIER2, Metric.LONG_TERM_DEBT)
    assert series.collapsed == ("debt_lease_scope",)
    assert not series.switches, "an alternating series is not a switch"
    assert series.tags_used == (f"{GAAP}:LongTermDebt",)
    ends = {fact.period.end for fact in series.annual.facts}
    assert ends == {date(2021, 12, 31), date(2023, 12, 31)}


@pytest.mark.spec
def test_the_exclusivity_pass_runs_after_a_full_resolution_not_greedily() -> None:
    """A greedy check locks in whichever member appeared in the earliest period.

    And the earliest period in a window is the one most likely to be off-pattern legacy tagging — so
    a greedy implementation on `TIER2`'s debt would keep the FY2021 member *because it came first*
    rather than because it covers more periods. Here the two tie and the tiebreak is the chain index,
    so the assertion that distinguishes the two implementations is on the *window*: shrinking it to
    the last three years makes the lease tag the majority, and a greedy pass would still pick the
    other one.
    """
    late = (date(2022, 6, 1), date(2025, 6, 30))
    series = _series(TIER2, Metric.LONG_TERM_DEBT, window=late)
    assert series.tags_used == (f"{GAAP}:LongTermDebtAndCapitalLeaseObligations",)
    assert series.collapsed == ("debt_lease_scope",)


@pytest.mark.spec
def test_a_group_with_one_member_present_is_left_alone() -> None:
    """No conflict, no collapse, and no finding — the common case.

    `TIER2` tags only `ProfitLoss` for net income, so `net_income_scope` has one member represented.
    An implementation that ran the collapse unconditionally would re-resolve every period through a
    chain with the winner removed, which empties the series it was supposed to be protecting.
    """
    series = _series(TIER2, Metric.NET_INCOME)
    assert len(series.annual.facts) == 4
    assert series.tags_used == (f"{GAAP}:ProfitLoss",)
    assert not series.collapsed
    assert not series.switches
    assert uses_including_nci_net_income(series.tags_used)


# ---------------------------------------------------------------------------
# the summing member
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_sga_resolution_carries_two_facts() -> None:
    """`Resolution.facts` has length 2 and the emitted `Derivation` names both refs.

    A single `RawFact | None` cannot carry a value summed from two separately-tagged facts, and
    computing the sum in `facts.py` would put tag knowledge outside the registry — which is what the
    `us-gaap` allowlist exists to prevent. So the resolver owns it end to end, and the appendix prints
    both components rather than implying one tag produced the number.
    """
    facts = company_facts(TIER2).facts
    period = FiscalPeriod.of(date(2023, 1, 1), date(2023, 12, 31))
    resolution = resolve(Metric.SGA, facts, periods=(period,))[0]
    assert len(resolution.facts) == 2
    assert {tag.split(":")[-1] for tag in resolution.tags_used} == set(SGA_COMPONENTS)

    produced = materialize(chain_for(Metric.SGA), resolution)
    assert produced is not None
    assert produced.summed
    assert isinstance(produced.fact.source, Derivation)
    assert produced.fact.source.rule == "sga_summed_components"
    assert len(produced.fact.source.refs()) == 2


@pytest.mark.spec
def test_the_sga_sum_is_the_sum_of_its_components() -> None:
    """Asserted as the derivation, not as a value the generator was told to produce.

    The number 795,000,000 is only correct because `make_fixtures.py` chose 430 and 365. What is
    *always* true is that the summed member equals its two components added — and that is the thing a
    substitution bug breaks, by understating the metric by the other component.
    """
    facts = company_facts(TIER2).facts
    components = [
        fact.value
        for tag in SGA_COMPONENTS
        for fact in facts[(GAAP, tag)]
        if fact.period.end == date(2023, 12, 31)
    ]
    assert len(components) == 2

    series = _series(TIER2, Metric.SGA)
    summed = next(f for f in series.annual.facts if f.period.end == date(2023, 12, 31))
    assert summed.value == sum(components, Decimal(0))


@pytest.mark.spec
def test_the_summing_member_does_not_match_when_one_component_is_missing() -> None:
    """Both tags or neither. A partial match understates the metric, silently.

    This is the whole argument for the member variant: Piotroski's margin test would *improve* for a
    filer that merely moved its selling costs into a separate line. The violation is attempted by
    resolving a payload with one component dropped and asserting the period is absent rather than
    half-filled.
    """
    facts = dict(company_facts(TIER2).facts)
    del facts[(GAAP, SGA_COMPONENTS[1])]
    period = FiscalPeriod.of(date(2023, 1, 1), date(2023, 12, 31))
    resolution = resolve(Metric.SGA, facts, periods=(period,))[0]
    assert resolution.is_absent


# ---------------------------------------------------------------------------
# units and signs
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_eps_under_usd_is_excluded() -> None:
    """A fact in the wrong unit cannot enter a series — **and the exclusion is counted.**

    `BADUNIT` tags FY2024's diluted EPS under `USD` rather than `USD/shares`, which some real filers
    do, and a resolver that ignores unit reports an EPS three orders of magnitude off. The count is
    the half that matters: an implementation that dropped the fact and said nothing would satisfy an
    assertion that the series has one entry, and the coverage report would show an unexplained miss.
    """
    series = _series(BADUNIT, Metric.EPS_DILUTED)
    assert [fact.period.end for fact in series.annual.facts] == [date(2023, 12, 31)]
    assert series.dropped_unit_mismatch == 1
    assert series.units_excluded == ("USD",)
    assert all(fact.unit == "USD/shares" for fact in series.annual.facts)


@pytest.mark.spec
def test_a_non_usd_revenue_fact_is_excluded_and_counted() -> None:
    """§12 records non-USD reporting currencies as out of scope.

    The filter is what turns "out of scope" from a comment into an absence that appears in the
    coverage report — the difference between a known limitation and a wrong number sitting in a
    dollar series.
    """
    series = _series(BADUNIT, Metric.REVENUE)
    assert series.units_excluded == ("EUR",)
    assert series.dropped_unit_mismatch == 1
    assert [fact.period.end for fact in series.annual.facts] == [date(2023, 12, 31)]


@pytest.mark.spec
def test_unit_filter_returns_the_excluded_facts_rather_than_a_count() -> None:
    """Which units were seen is a different finding from how many facts were dropped.

    A metric absent because every fact was `EUR` needs a different sentence than one absent because
    the tag was never used, and a bare count cannot produce either.
    """
    chain = chain_for(Metric.EPS_DILUTED)
    rows = company_facts(BADUNIT).get(GAAP, "EarningsPerShareDiluted")
    kept, excluded = unit_filter(chain, rows)
    assert {fact.unit for fact in kept} == {"USD/shares"}
    assert {fact.unit for fact in excluded} == {"USD"}
    assert len(kept) + len(excluded) == len(rows)


@pytest.mark.spec
def test_a_flip_produces_a_derivation_naming_the_convention() -> None:
    """The flip is visible in the appendix, because an unexplained sign change is a filer error.

    `TIER2` files `InterestIncomeExpenseNet` negative for FY2023-24 — a net *expense* is negative in
    that element — and the metric's convention is expense-positive. The value comes out positive, and
    it carries `sign_normalized` over the single input ref so `refs()` still names one filing and
    nothing downstream changes.
    """
    series = _series(TIER2, Metric.INTEREST_EXPENSE)
    filed = {fact.period.end: fact for fact in series.annual.facts}
    flipped = filed[date(2023, 12, 31)]
    raw = next(
        fact
        for fact in company_facts(TIER2).get(GAAP, "InterestIncomeExpenseNet")
        if fact.period.end == date(2023, 12, 31)
    )
    assert raw.value < 0
    assert flipped.value == -raw.value
    assert isinstance(flipped.source, Derivation)
    assert flipped.source.rule == "sign_normalized"
    assert len(flipped.source.refs()) == 1
    assert series.annual.sign_anomalies == 0, (
        "a flipped fact obeying the convention is not an anomaly"
    )


@pytest.mark.spec
def test_an_as_filed_fact_carries_a_bare_source_ref() -> None:
    """No `Derivation` where nothing was derived, so `report.json` stays small.

    The overwhelming majority of facts are this case. Wrapping every one in a single-input
    `Derivation` would be defensible and would triple the provenance in the document for no
    information — and it would make `refs()` the only way to read a filed fact's accession.
    """
    series = _series(TIER2, Metric.COGS)
    assert series.annual.facts
    assert all(not isinstance(fact.source, Derivation) for fact in series.annual.facts)


@pytest.mark.spec
def test_a_fact_contradicting_its_convention_is_kept_and_counted() -> None:
    """A negative capex quarter is **real**, and dropping it makes FCF wrong in the other direction.

    A disposal netted against acquisitions produces one. So the fact stays in the series and appears
    as a sign anomaly — a §6.4 data-integrity finding for M4 rather than something M2 corrects. The
    case is assembled here because no fixture carries a negative capex row; the gap is recorded in
    `tests/fixtures/edgar/PROVENANCE.md`.
    """
    chain = chain_for(Metric.CAPEX)
    donor = company_facts(TIER2).get(GAAP, "Assets")[0]
    negative = dataclasses.replace(
        donor,
        tag="PaymentsToAcquirePropertyPlantAndEquipment",
        value=Decimal("-42000000"),
        period=FiscalPeriod.of(date(2023, 1, 1), date(2023, 12, 31)),
    )
    series = normalize_metric(
        chain,
        {(GAAP, "PaymentsToAcquirePropertyPlantAndEquipment"): (negative,)},
        window=M2_WINDOW,
        as_of=None,
    )
    assert [fact.value for fact in series.annual.facts] == [Decimal("-42000000")]
    assert series.annual.sign_anomalies == 1


@pytest.mark.spec
def test_no_convention_means_no_anomaly_is_possible() -> None:
    """`AS_FILED` is the absence of a claim, so a negative revenue is not an anomaly.

    Filers do report negative revenue — a contra-revenue adjustment period — and counting it would put
    an integrity flag on correct data for every metric in the registry that has no convention.
    """
    donor = company_facts(TIER2).get(GAAP, "Assets")[0]
    negative = dataclasses.replace(
        donor,
        tag="Revenues",
        value=Decimal("-1000"),
        period=FiscalPeriod.of(date(2023, 1, 1), date(2023, 12, 31)),
    )
    series = normalize_metric(
        chain_for(Metric.REVENUE),
        {(GAAP, "Revenues"): (negative,)},
        window=M2_WINDOW,
        as_of=None,
    )
    assert series.annual.facts[0].value < 0
    assert series.annual.sign_anomalies == 0


# ---------------------------------------------------------------------------
# what the resolver does not do
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_resolve_series_reports_the_exclusivity_outcome_separately_from_the_periods() -> None:
    """`Resolution` carries three fields and none of them is a finding.

    The exclusivity outcome is a property of the *series*, so a per-period field would be either
    repeated across every period or left mostly `None` — and either shape invites a caller to read it
    off one period and conclude something about the whole series.
    """
    resolved = resolve_series(
        Metric.REVENUE,
        company_facts(TIER2).facts,
        periods=tuple(
            FiscalPeriod.of(date(year, 1, 1), date(year, 12, 31)) for year in (2021, 2024)
        ),
    )
    assert {field.name for field in dataclasses.fields(resolved.resolutions[0])} == {
        "period",
        "facts",
        "chain_index",
    }
    assert resolved.switches


@pytest.mark.spec
def test_instant_facts_never_reach_a_duration_bucket() -> None:
    """An `INSTANT` chain resolves instants, and nothing else can arrive in its series.

    `FiscalPeriod` equality is `(end, kind)`, so a balance-sheet fact and an annual figure ending the
    same day do not match each other — which is what makes the resolver's period matching safe on the
    one date where every filer has both.
    """
    series = _series(TIER2, Metric.ASSETS)
    assert series.annual.facts
    assert all(fact.period.kind is PeriodKind.INSTANT for fact in series.annual.facts)
    assert all(fact.period.start is None for fact in series.annual.facts)


@pytest.mark.spec
def test_the_registry_declares_no_derivation_for_a_metric_with_no_chain() -> None:
    """`DERIVATIONS` cannot produce a metric the registry does not know.

    A derived metric with no chain has no declared unit, no aggregation class and no tier — so it
    would break the completeness test, be excluded from both tier aggregates, and still appear in the
    series. The check is cheap and the failure would be quiet.
    """
    for spec in DERIVATIONS:
        assert spec.metric in CHAINS
        for metric in spec.metric_inputs:
            assert metric in CHAINS


@pytest.mark.spec
def test_probe_covers_every_chain_member() -> None:
    """`tests/coverage_probe.py` promised this test, and the registry now exists to satisfy it.

    The probe carries its own tag lists, deliberately: it was written before `normalize/` so the
    coverage measurement could start on day one rather than at the end of the milestone, and it
    measures **raw tag presence per chain member**, which is the input the tier-2 orderings need. Its
    comment says in terms that a test here *"asserts this list is a superset of the registry's members
    once the registry exists, so the duplication cannot silently drift into measuring a different
    question than the one the chains ask."*

    A superset rather than an equality, because the probe legitimately measures more: the two tags the
    liabilities derivation reaches for are not members of any chain, and it wants their coverage too.
    What it must never do is measure *fewer* — a member the probe does not ask about is a member whose
    position in the chain is decided on no evidence, which is exactly the situation
    `docs/m2/README.md` § Spec question 6 exists to close.
    """
    from tests.coverage_probe import DEI_SHARES, TIER1, TIER2

    probed = {**TIER1, **TIER2}
    missing: list[str] = []
    for metric, chain in CHAINS.items():
        if metric is Metric.SHARES_COVER:
            assert chain.members[0].keys == (DEI_SHARES,), "the dei carve-out moved"
            continue
        tags = probed.get(str(metric))
        assert tags is not None, f"the probe does not measure {metric}"
        for member in chain.members:
            missing.extend(tag for tag in member.tags if tag not in tags)
    assert not missing, f"chain members the probe never asks about: {sorted(set(missing))}"


@pytest.mark.spec
def test_the_probe_also_measures_the_tags_only_a_derivation_names() -> None:
    """The liabilities derivation reaches outside the registry, and its coverage still matters.

    `LiabilitiesAndStockholdersEquity` is not a member of any chain — it is the minuend of the
    fallback that fires for the ~11% of filers who never tag `Liabilities` at all. If the probe did not
    measure it, the twenty-name table would report those filers as uncovered for liabilities while the
    pipeline covers them, and the measurement would understate the thing it exists to measure.
    """
    from tests.coverage_probe import TIER1

    liabilities = TIER1[str(Metric.LIABILITIES)]
    equity = TIER1[str(Metric.EQUITY)]
    spec = next(item for item in DERIVATIONS if item.metric is Metric.LIABILITIES)
    minuend, subtrahend = spec.tag_inputs
    assert minuend.tag in liabilities
    assert subtrahend.tag in equity
    assert spec.fallback_subtrahend is not None
    assert spec.fallback_subtrahend.tag in equity


@pytest.mark.spec
def test_a_metric_absent_from_the_payload_yields_an_empty_series_not_an_error() -> None:
    """A missing tag is a coverage fact, not a failure — M1's rule, one layer up.

    `AAPL.trimmed.json` has no capex tag at all. The series is empty, the counters are zero, and
    nothing raises: the coverage report is where that shows up, which is the whole reason §4.2 argues
    against hardcoding one tag per metric.
    """
    series = _series(AAPL, Metric.CAPEX)
    assert series.annual.facts == ()
    assert series.quarterly.facts == ()
    assert series.tags_used == ()
    assert series.dropped_unit_mismatch == 0


def test_resolve_over_no_periods_returns_nothing() -> None:
    """The degenerate call, which a caller reaches on a payload with no facts for a metric."""
    assert resolve(Metric.REVENUE, {}, periods=()) == ()


def test_materialize_returns_none_for_an_absence() -> None:
    """`None` rather than a zero-valued `Fact`, because a `Fact` with no ref cannot exist.

    §3.2's rule is that an untraceable number is not printed, and the type system is what enforces it:
    `Fact.source` is non-optional, so there is no `Fact` to return here.
    """
    absent = resolve(
        Metric.REVENUE,
        {},
        periods=(FiscalPeriod.of(date(2020, 1, 1), date(2020, 12, 31)),),
    )[0]
    assert materialize(chain_for(Metric.REVENUE), absent) is None


def test_the_registry_is_a_mapping_of_frozen_records() -> None:
    """Immutability, so a caller cannot reorder a chain at runtime.

    A chain mutated in one command would change tag selection for every later one in the same
    process — which is the shape of bug that shows up only in the backtest, where one process resolves
    hundreds of companies.
    """
    chain = chain_for(Metric.REVENUE)
    for target, name, value in ((chain, "unit", "EUR"), (chain.members[0], "tag", "Revenues")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            # Through `setattr` with a variable name, per `test_config._assign`: a literal assignment
            # to a frozen field is what pyright rejects statically, and the runtime refusal is the
            # guarantee under test.
            setattr(target, name, value)


def test_raw_facts_are_left_untouched_by_resolution() -> None:
    """The resolver reads; it does not normalize in place.

    A sign flip that mutated the `RawFact` would change what a second metric resolving the same tag
    sees — and `InterestExpense` and `InterestIncomeExpenseNet` are exactly the kind of pair that gets
    read twice.
    """
    facts = company_facts(TIER2).facts
    before = [(f.value, f.unit) for f in facts[(GAAP, "InterestIncomeExpenseNet")]]
    _ = _series(TIER2, Metric.INTEREST_EXPENSE)
    after = [
        (fact.value, fact.unit)
        for fact in company_facts(TIER2).facts[(GAAP, "InterestIncomeExpenseNet")]
    ]
    assert before == after
    assert all(isinstance(fact, RawFact) for fact in facts[(GAAP, "InterestIncomeExpenseNet")])

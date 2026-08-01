"""Cross-metric derivations: the equity trap, the per-period rule, and the order.

`docs/m2/01-tags.md` §§9-10 is normative. Three properties are asserted here and each has a wrong
version that produces a number:

**The liabilities fallback must not compose over `Metric.EQUITY`.** §9: written as
`L&SE - Metric.EQUITY` — the natural spelling once `EQUITY` is a resolved metric sitting right there
— the result overstates total liabilities by exactly the noncontrolling interest, for precisely the
~11% of filers who never tag `Liabilities` and therefore reach this branch. Both spellings return a
plausible balance-sheet figure, so the only assertion that separates them is the one on the
*difference*: `derived == naive - nci`. A test hard-coding 6,000,000,000 passes under either rule
whenever the fixture happens to agree, and `docs/m2/05-testing.md` §2 says value assertions against a
synthetic payload test the fixture generator.

**A derivation fires per period, not per series.** A filer tagging `GrossProfit` in three years of
four gets the fourth derived and the other three as filed. The failure mode of the per-series reading
is invisible: the series is full either way, and what changes is whether three filed figures were
silently replaced by arithmetic.

**The order is acyclic.** Nothing is cyclic today, so the passing assertion proves nothing on its
own — which is why the cycle-detection test is written as a *violation*: it adds
`COGS = REVENUE - GROSS_PROFIT`, which is exactly as true and exactly as tempting as the derivation
already declared, and asserts the same sort refuses it. Without that half, a topological sort that
never detects anything passes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

import pytest

from investo.domain.models import Fact, Metric
from investo.domain.periods import FiscalPeriod, PeriodKind
from investo.domain.provenance import Accession, Derivation, SourceRef
from investo.normalize.statements import Bucket
from investo.normalize.tags import (
    CHAINS,
    DERIVATIONS,
    DerivationKind,
    DerivedMetric,
    chain_for,
    derive,
)
from tests.conftest import FETCHED_AT, M2_WINDOW, company_facts, history, raw_facts

GAAP: Final = "us-gaap"
"""Tests may name a tag; `normalize/tags.py`'s monopoly is over the package, not the suite.

`tests/test_layering.py` walks the installed package. A fixture-driven test has to name the tag
whose behaviour it is pinning, and naming it here cannot become a shadow registry because nothing
imports a test module.
"""

LSE: Final = "LiabilitiesAndStockholdersEquity"
EQUITY_WITH_NCI: Final = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
PARENT_EQUITY: Final = "StockholdersEquity"

PERIOD: Final = FiscalPeriod.of(date(2023, 1, 1), date(2023, 12, 31))
"""One duration for every unit-level case, so the only variable is what is present."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _ref(tag: str, accession: str = "0000000001-24-000001") -> SourceRef:
    return SourceRef(
        accession=Accession.parse(accession),
        taxonomy=GAAP,
        tag=tag,
        form="10-K",
        filed=date(2024, 2, 1),
        url="https://data.sec.gov/test",
        fetched_at=FETCHED_AT,
    )


def _fact(
    metric: Metric,
    value: str,
    *,
    unit: str = "USD",
    period: FiscalPeriod = PERIOD,
    source: Derivation | SourceRef | None = None,
) -> Fact:
    """One already-resolved input to a derivation, with every field a test might make wrong."""
    return Fact(
        metric=metric,
        value=Decimal(value),
        period=period,
        source=source if source is not None else _ref(str(metric)),
        unit=unit,
    )


def _resolved(*facts: Fact) -> dict[Metric, dict[tuple[date, PeriodKind], Fact]]:
    """Group facts into `derive`'s `resolved` shape.

    Keyed on `(period.end, period.kind)` — the pair `FiscalPeriod` itself compares on — because that
    is what makes a derivation match an instant against an instant and a duration against a duration.
    """
    grouped: dict[Metric, dict[tuple[date, PeriodKind], Fact]] = {}
    for fact in facts:
        grouped.setdefault(fact.metric, {})[(fact.period.end, fact.period.kind)] = fact
    return grouped


def _spec_for(metric: Metric) -> DerivedMetric:
    return next(spec for spec in DERIVATIONS if spec.metric is metric)


def _by_end(fixture: str, tag: str) -> dict[date, Decimal]:
    """A tag's values in a `companyfacts` fixture, keyed by period end."""
    return {fact.period.end: fact.value for fact in raw_facts(fixture, (GAAP, tag))}


def _series_by_end(facts: tuple[Fact, ...]) -> dict[date, Fact]:
    return {fact.period.end: fact for fact in facts}


def _topological_order(specs: tuple[DerivedMetric, ...]) -> tuple[Metric, ...]:
    """Kahn's algorithm over `input metric -> produced metric`, raising on a cycle.

    Only dependencies on metrics some derivation *produces* are edges. A dependency on a
    chain-resolved metric — `GROSS_PROFIT` needing `REVENUE` — is satisfied before any derivation
    runs and can never participate in a cycle, so counting it would report every derivation as
    unschedulable.

    Raises:
        ValueError: if no metric is ready, i.e. the remaining ones depend on each other.
    """
    produced = {spec.metric for spec in specs}
    pending = {spec.metric: set(spec.depends_on) & produced for spec in specs}
    order: list[Metric] = []
    while pending:
        ready = sorted((metric for metric, deps in pending.items() if not deps), key=str)
        if not ready:
            raise ValueError(f"cycle among {', '.join(sorted(str(m) for m in pending))}")
        for metric in ready:
            del pending[metric]
            order.append(metric)
        for deps in pending.values():
            deps.difference_update(ready)
    return tuple(order)


def _declared_in_dependency_order(specs: tuple[DerivedMetric, ...]) -> bool:
    """Whether every spec's produced inputs appear earlier in the tuple than the spec itself."""
    produced_later = {spec.metric for spec in specs}
    for spec in specs:
        produced_later.discard(spec.metric)
        if spec.depends_on & produced_later:
            return False
    return True


# ---------------------------------------------------------------------------
# §9 — the equity trap
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_nci_is_not_counted_as_a_liability() -> None:
    """**The one test that separates the right derivation from the tempting one.**

    `docs/m2/01-tags.md` §9: the total-liabilities fallback subtracts
    `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`, not
    `StockholdersEquity`, and `Metric.EQUITY` resolves the latter. Both spellings return a number of
    the right magnitude with the right units on the right date, so nothing downstream can tell them
    apart — the wrong one inflates net debt, deflates interest coverage and moves the Altman Z
    leverage term, for the population that reaches this branch at all.

    The assertion is on the *difference*: the result must sit exactly one NCI below the naive answer.
    Asserting the value instead would pass under either rule on any fixture whose NCI happened to be
    zero, and `NCI.trimmed.json` exists precisely because a material NCI makes the two answers
    differ. The naive answer is computed here from the same payload rather than written down, so the
    test cannot drift from the fixture it is measured against.
    """
    fixture = "NCI.trimmed.json"
    assert not company_facts(fixture).has(GAAP, "Liabilities"), (
        "the derivation only fires where the tag is absent, so the fixture must not carry it"
    )

    lse = _by_end(fixture, LSE)
    with_nci = _by_end(fixture, EQUITY_WITH_NCI)
    parent_only = _by_end(fixture, PARENT_EQUITY)
    end = min(set(lse) & set(with_nci) & set(parent_only))
    nci = with_nci[end] - parent_only[end]
    naive = lse[end] - parent_only[end]
    assert nci > 0, "a zero NCI makes the two derivations agree and the test vacuous"

    series = _series_by_end(history(fixture).series(Metric.LIABILITIES, Bucket.ANNUAL))
    derived = series[end]

    assert derived.value == naive - nci
    assert derived.value == lse[end] - with_nci[end]
    assert derived.value != naive
    assert isinstance(derived.source, Derivation)
    assert {ref.tag for ref in derived.source.refs()} == {LSE, EQUITY_WITH_NCI}, (
        "the provenance names the tags the arithmetic used, so the appendix is checkable"
    )


@pytest.mark.spec
def test_the_fallback_subtrahend_is_recorded_as_an_approximation() -> None:
    """§9: parent-only equity is a *last* fallback, and its use is a finding.

    "Approximating is better than omitting the metric entirely; doing it invisibly is not." The
    period the including-NCI tag does not cover derives from parent-only equity — so it equals the
    naive answer, and that is correct here because there is nothing better available. What makes it
    honest is the `liabilities_nci_approximated` finding and the note naming the tag; a test that
    only checked the value would pass for an implementation that silently used parent-only equity
    *everywhere*, which is the failure the test above exists to catch.
    """
    fixture = "NCI.trimmed.json"
    lse = _by_end(fixture, LSE)
    with_nci = _by_end(fixture, EQUITY_WITH_NCI)
    parent_only = _by_end(fixture, PARENT_EQUITY)
    approximated_ends = sorted(set(parent_only) - set(with_nci))
    assert approximated_ends, "the fixture must have a period the including-NCI tag misses"

    built = history(fixture)
    derived = _series_by_end(built.series(Metric.LIABILITIES, Bucket.ANNUAL))

    for end in approximated_ends:
        fact = derived[end]
        assert fact.value == lse[end] - parent_only[end]
        assert isinstance(fact.source, Derivation)
        assert {ref.tag for ref in fact.source.refs()} == {LSE, PARENT_EQUITY}
        assert fact.source.note is not None
        assert PARENT_EQUITY in fact.source.note

    findings = built.coverage.findings_for("liabilities_nci_approximated")
    assert [finding.metric for finding in findings] == [Metric.LIABILITIES]

    exact_ends = sorted(set(with_nci) & set(lse))
    for end in exact_ends:
        # Bound to a local first: `isinstance` narrows a name, not a subscript expression, so
        # `derived[end].source.note` would be an attribute access on the `SourceRef` arm too.
        source = derived[end].source
        note = source.note if isinstance(source, Derivation) else None
        assert note is None or PARENT_EQUITY not in note, (
            "a period with the including-NCI tag must not be reported as approximated"
        )


@pytest.mark.spec
def test_the_liabilities_derivation_names_tags_rather_than_composing_over_metrics() -> None:
    """§9's rule stated structurally, so it holds for a payload no fixture contains.

    "A metric is a *selection*, and two selections that share a name do not share a definition." The
    registry has to encode that: the subtrahend is a tag no chain resolves, and the tag `EQUITY`
    *does* resolve appears only as the fallback. A refactor that replaced `tag_inputs` with
    `metric_inputs=(..., Metric.EQUITY)` would keep every value test on `NCI` passing on the period
    where both equity tags exist and would silently be wrong on every filer with material NCI.
    """
    spec = _spec_for(Metric.LIABILITIES)
    equity_chain_tags = {member.tag for member in chain_for(Metric.EQUITY).members}

    assert spec.kind is DerivationKind.TAG_DIFFERENCE
    assert spec.metric_inputs == (), "a tag difference must not compose over a resolved metric"
    assert spec.depends_on == frozenset()
    minuend, subtrahend = spec.tag_inputs
    assert minuend.tag == LSE
    assert subtrahend.tag not in equity_chain_tags
    assert spec.fallback_subtrahend is not None
    assert spec.fallback_subtrahend.tag in equity_chain_tags, (
        "the parent-only tag is the fallback, never the primary subtrahend"
    )


@pytest.mark.spec
def test_exactly_one_derivation_carries_a_fallback_subtrahend() -> None:
    """The same treatment §4 gives the summing member, for the same reason.

    A fallback subtrahend is an approximation with a finding attached, and the construct spreads once
    it exists — every derivation has a weaker input somebody could reach for. Naming the one use
    makes a second a visible edit with a reviewer attached rather than a pattern that arrived one row
    at a time and quietly widened what "derived" means.
    """
    with_fallback = [spec.metric for spec in DERIVATIONS if spec.fallback_subtrahend is not None]
    assert with_fallback == [Metric.LIABILITIES]


# ---------------------------------------------------------------------------
# §10 rule 2 — every input present, for the same period
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_gross_profit_absent_when_cogs_absent() -> None:
    """§10 rule 2: a derivation over a period an input is missing is **not attempted**.

    Both halves are asserted, because the positive half alone passes for an implementation that
    treats a missing input as zero — which yields `gross profit == revenue`, a 100% margin, and a
    Piotroski profitability score that improves when a filer stops tagging COGS. Removing COGS is the
    only difference between the two calls, so it is provably what suppressed the derivation.
    """
    spec = _spec_for(Metric.GROSS_PROFIT)
    revenue = _fact(Metric.REVENUE, "1000")
    cogs = _fact(Metric.COGS, "400")

    both = derive(spec, resolved=_resolved(revenue, cogs), raw={}, periods=[PERIOD])
    assert [fact.value for fact, _ in both] == [revenue.value - cogs.value]

    assert derive(spec, resolved=_resolved(revenue), raw={}, periods=[PERIOD]) == ()
    assert derive(spec, resolved=_resolved(cogs), raw={}, periods=[PERIOD]) == ()
    assert derive(spec, resolved={}, raw={}, periods=[PERIOD]) == ()


@pytest.mark.spec
def test_a_filer_with_revenue_and_no_cogs_has_no_gross_profit_series() -> None:
    """The same rule end to end, on the flagship fixture.

    `AAPL.trimmed.json` carries four years of revenue and no COGS tag at all, so the whole
    `GROSS_PROFIT` series must be empty — not four periods of `revenue - 0`. The coverage report says
    so too: `derived_periods` is zero, which is what distinguishes "the derivation did not fire" from
    "the derivation fired and produced revenue again".
    """
    built = history("AAPL.trimmed.json")

    assert built.series(Metric.REVENUE, Bucket.ANNUAL) != ()
    assert built.series(Metric.COGS, Bucket.ANNUAL) == ()
    for bucket in Bucket:
        assert built.series(Metric.GROSS_PROFIT, bucket) == ()
        assert built.coverage.for_bucket(bucket)[Metric.GROSS_PROFIT].derived_periods == 0


@pytest.mark.spec
def test_gross_profit_is_derived_per_period_not_per_series() -> None:
    """§10 rule 1: the filed years stay filed and only the gap is computed.

    `TIER2` tags `GrossProfit` for three of its four years and COGS for all four, so the fourth is
    the only period arithmetic may touch. Which period that is comes from the payload rather than
    from a date written here, so the test states the rule: *the derived ends are exactly the ends the
    tag does not cover*. A per-series implementation returns the same four numbers with three of them
    recomputed, which no value assertion on the series can see — `derived_periods` and the provenance
    class can.
    """
    fixture = "TIER2.trimmed.json"
    built = history(fixture)
    revenue = {fact.period.end: fact.value for fact in built.series(Metric.REVENUE, Bucket.ANNUAL)}
    cogs = {fact.period.end: fact.value for fact in built.series(Metric.COGS, Bucket.ANNUAL)}
    filed_ends = set(_by_end(fixture, "GrossProfit"))
    derivable = set(revenue) & set(cogs)
    expected_derived = sorted(derivable - filed_ends)
    assert expected_derived, "the fixture must leave one year for the derivation"

    series = _series_by_end(built.series(Metric.GROSS_PROFIT, Bucket.ANNUAL))
    derived_ends = sorted(
        end for end, fact in series.items() if isinstance(fact.source, Derivation)
    )

    assert derived_ends == expected_derived
    assert sorted(series) == sorted(derivable | filed_ends)
    assert built.coverage.annual[Metric.GROSS_PROFIT].derived_periods == len(expected_derived)
    for end in expected_derived:
        assert series[end].value == revenue[end] - cogs[end]
    for end in filed_ends:
        assert isinstance(series[end].source, SourceRef), "a filed period must stay filed"


@pytest.mark.spec
def test_a_derivation_over_mismatched_units_does_not_fire() -> None:
    """§10 rule 2's second clause: both inputs must share the metric's unit.

    §12 puts non-USD reporting currencies out of scope, and the unit check is what turns that from a
    comment into an absence. Subtracting a `EUR` cost from a `USD` revenue is arithmetically
    well-formed and produces a margin nobody can interpret, and the resulting figure would carry two
    accessions and look fully traced.
    """
    spec = _spec_for(Metric.GROSS_PROFIT)
    revenue = _fact(Metric.REVENUE, "1000")

    assert (
        derive(
            spec,
            resolved=_resolved(revenue, _fact(Metric.COGS, "400", unit="EUR")),
            raw={},
            periods=[PERIOD],
        )
        == ()
    )
    assert (
        derive(
            spec,
            resolved=_resolved(
                _fact(Metric.REVENUE, "1000", unit="EUR"), _fact(Metric.COGS, "400")
            ),
            raw={},
            periods=[PERIOD],
        )
        == ()
    )


@pytest.mark.spec
def test_a_derivation_matches_an_instant_against_an_instant() -> None:
    """The derivation key is `(end, kind)`, not `end`.

    A balance-sheet instant and an income-statement duration ending the same day are different
    periods, and `FiscalPeriod` says so. If the key were the end date alone, a filer's fiscal-year-end
    balance would satisfy a candidate duration, and the derivation would subtract a flow from a
    balance on the strength of a shared date.
    """
    spec = _spec_for(Metric.GROSS_PROFIT)
    durations = _resolved(_fact(Metric.REVENUE, "1000"), _fact(Metric.COGS, "400"))
    instant = FiscalPeriod(end=PERIOD.end, kind=PeriodKind.INSTANT)

    assert derive(spec, resolved=durations, raw={}, periods=[instant]) == ()
    assert derive(spec, resolved=durations, raw={}, periods=[PERIOD]) != ()


# ---------------------------------------------------------------------------
# the ratio form — the one place the output unit is not an input unit
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_eps_derivation_output_unit_is_not_an_input_unit() -> None:
    """§10: `USD / shares -> USD/shares`, and a unit check that only ever compares equal is untested.

    The two difference derivations require every unit to match, so the ratio is the only one whose
    output unit is composed. Asserted as a relation between the three chains rather than against the
    literal `"USD/shares"`, so it still holds if the spelling of the unit changes — and asserted to
    differ from both inputs, because that is what makes this case the exception it is.
    """
    spec = _spec_for(Metric.EPS_DILUTED)
    numerator, denominator = spec.metric_inputs
    output_unit = chain_for(spec.metric).unit
    input_units = (chain_for(numerator).unit, chain_for(denominator).unit)
    assert output_unit not in input_units
    assert output_unit == f"{input_units[0]}/{input_units[1]}"

    income = _fact(numerator, "1000", unit=input_units[0])
    shares = _fact(denominator, "250", unit=input_units[1])
    produced = derive(spec, resolved=_resolved(income, shares), raw={}, periods=[PERIOD])

    assert [(fact.value, fact.unit) for fact, _ in produced] == [
        (income.value / shares.value, output_unit)
    ]


@pytest.mark.spec
def test_eps_is_not_derived_from_a_denominator_in_the_wrong_unit() -> None:
    """A share count arriving under `USD` composes to `USD/USD`, which is not the metric's unit.

    §4.2 warns twice that a unit difference is a value difference, and this is the direction the
    composed check has to catch: the numerator is right, the arithmetic succeeds, and the result is a
    ratio of two dollar amounts printed as earnings per share.
    """
    spec = _spec_for(Metric.EPS_DILUTED)
    numerator, denominator = spec.metric_inputs

    assert (
        derive(
            spec,
            resolved=_resolved(_fact(numerator, "1000"), _fact(denominator, "250", unit="USD")),
            raw={},
            periods=[PERIOD],
        )
        == ()
    )


def test_eps_is_not_derived_from_a_zero_share_count() -> None:
    """A filer reporting zero weighted-average shares is malformed data, and M2's answer is absence.

    Not an exception: §14's taxonomy says thin or broken data degrades coverage rather than aborting,
    and a `DivisionByZero` escaping normalization would take the whole report with it over one bad
    fact in one period.
    """
    spec = _spec_for(Metric.EPS_DILUTED)
    numerator, denominator = spec.metric_inputs

    assert (
        derive(
            spec,
            resolved=_resolved(_fact(numerator, "1000"), _fact(denominator, "0", unit="shares")),
            raw={},
            periods=[PERIOD],
        )
        == ()
    )


# ---------------------------------------------------------------------------
# §10 rule 4 — provenance nests
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_derivation_provenance_nests_rather_than_flattening() -> None:
    """§10 rule 4: a derivation over a derived input carries both levels.

    `Derivation.inputs` is `Provenance`, so the tree keeps its shape and `refs()` is the only thing
    that flattens. Pre-flattening would make a two-level derivation indistinguishable from a
    three-input one in the appendix, and §9.1 prints the rule as well as the filings — a reader who
    cannot see that revenue was itself stitched cannot check the margin built on it.
    """
    spec = _spec_for(Metric.GROSS_PROFIT)
    stitched = Derivation(rule="sga_summed_components", inputs=(_ref("A"), _ref("B")))
    revenue = _fact(Metric.REVENUE, "1000", source=stitched)
    cogs = _fact(Metric.COGS, "400")

    produced, _ = derive(spec, resolved=_resolved(revenue, cogs), raw={}, periods=[PERIOD])[0]

    assert isinstance(produced.source, Derivation)
    assert produced.source.rule == spec.rule
    assert produced.source.inputs == (stitched, cogs.source), "the inner tree is kept whole"
    assert produced.source.refs() == (*stitched.refs(), cogs.source)
    assert len(produced.source.refs()) == 3


# ---------------------------------------------------------------------------
# §10 rule 3 — the order
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_derivation_graph_is_acyclic() -> None:
    """§10 rule 3, by topological sort over `DERIVATIONS`.

    Every declared metric has to come out of the sort, which is the same statement as "no subset of
    them depends on itself". The sort is run over the registry rather than over a hand-written edge
    list so a derivation added later is included without anyone remembering to add it — and
    `test_a_cycle_is_refused_by_the_same_sort` is what makes the pass meaningful, since a sort that
    detects nothing also passes this.
    """
    order = _topological_order(DERIVATIONS)

    assert set(order) == {spec.metric for spec in DERIVATIONS}
    assert len(order) == len(DERIVATIONS)


@pytest.mark.spec
def test_a_cycle_is_refused_by_the_same_sort() -> None:
    """The violation test, using the addition `docs/m2/01-tags.md` §10 names as the likely one.

    `COGS = REVENUE - GROSS_PROFIT` is exactly as true as `GROSS_PROFIT = REVENUE - COGS` and exactly
    as tempting, and adding it makes the pair mutually dependent. Left undetected it presents as a
    recursion error in a report run rather than as a design mistake, which is a stack trace in front
    of a user instead of a red test in front of the author.
    """
    cogs_from_gross_profit = DerivedMetric(
        metric=Metric.COGS,
        rule="cogs_from_revenue_minus_gross_profit",
        kind=DerivationKind.METRIC_DIFFERENCE,
        metric_inputs=(Metric.REVENUE, Metric.GROSS_PROFIT),
    )

    with pytest.raises(ValueError, match="cycle"):
        _ = _topological_order((*DERIVATIONS, cogs_from_gross_profit))

    self_referential = DerivedMetric(
        metric=Metric.REVENUE,
        rule="revenue_from_revenue",
        kind=DerivationKind.METRIC_DIFFERENCE,
        metric_inputs=(Metric.REVENUE, Metric.COGS),
    )
    with pytest.raises(ValueError, match="cycle"):
        _ = _topological_order((self_referential,))


@pytest.mark.spec
def test_derivations_are_declared_in_dependency_order() -> None:
    """§10: the order is a **declaration**, because `_apply_derivations` iterates the tuple.

    Acyclicity is not enough on its own — a graph can be acyclic and declared backwards, and then a
    derivation reads an input its producer has not written yet. That failure is a metric that is
    silently absent for the periods the chain missed, which looks exactly like thin data. The
    predicate's own discrimination is asserted below it on a reversed pair, since today's registry has
    no edges at all and would satisfy a predicate that always returned `True`.
    """
    assert _declared_in_dependency_order(DERIVATIONS)

    consumer = DerivedMetric(
        metric=Metric.GROSS_PROFIT,
        rule="gross_profit_from_revenue_minus_cogs",
        kind=DerivationKind.METRIC_DIFFERENCE,
        metric_inputs=(Metric.REVENUE, Metric.COGS),
    )
    producer = DerivedMetric(
        metric=Metric.COGS,
        rule="cogs_from_something_else",
        kind=DerivationKind.METRIC_DIFFERENCE,
        metric_inputs=(Metric.ASSETS, Metric.CASH),
    )
    assert not _declared_in_dependency_order((consumer, producer))
    assert _declared_in_dependency_order((producer, consumer))


def test_each_metric_is_produced_by_at_most_one_derivation() -> None:
    """One producer per metric, or "the declared order" does not identify a schedule.

    Two derivations for one metric would each fill whatever periods the other left, and which one won
    a given period would depend on their relative position — a tie-break nobody declared. It would
    also make the topological sort above ill-defined, since a node would have two sets of inputs.
    """
    produced = [spec.metric for spec in DERIVATIONS]
    assert len(set(produced)) == len(produced)


def test_every_derivation_names_metrics_the_registry_maps() -> None:
    """`derive` calls `chain_for` on its output and reads its inputs' chains for units.

    An unmapped metric on either side is a `KeyError` from inside normalization on the first filer
    that reaches the branch, so the registry and the derivation table have to agree — and the
    completeness test over `Metric` does not cover this, because it walks the enum rather than the
    derivations.
    """
    for spec in DERIVATIONS:
        assert spec.metric in CHAINS
        for metric in spec.metric_inputs:
            assert metric in CHAINS


@pytest.mark.spec
def test_derivation_rules_are_the_documented_identifiers() -> None:
    """`Derivation.rule` is a stable machine-readable key, printed in `report.json`.

    `docs/m2/01-tags.md` §10's table names all three. Renaming one silently changes a key a consumer
    may already be reading, so the strings are pinned here where a rename is a visible edit rather
    than in a comment nothing checks.
    """
    assert {spec.metric: spec.rule for spec in DERIVATIONS} == {
        Metric.GROSS_PROFIT: "gross_profit_from_revenue_minus_cogs",
        Metric.LIABILITIES: "liabilities_from_lse_minus_equity",
        Metric.EPS_DILUTED: "eps_from_net_income_over_diluted_shares",
    }


@pytest.mark.spec
def test_a_metric_form_declares_exactly_two_inputs() -> None:
    """`_metric_operands` returns `None` unless it found two, so a third would be ignored silently.

    The shape is part of the contract rather than an implementation detail: a derivation declaring
    three metric inputs would compute over the first two and drop the third with no error, which is a
    wrong number rather than a broken build.
    """
    for spec in DERIVATIONS:
        if spec.kind is DerivationKind.TAG_DIFFERENCE:
            assert spec.metric_inputs == ()
            assert len(spec.tag_inputs) == 2
        else:
            assert len(spec.metric_inputs) == 2
            assert spec.tag_inputs == ()


def test_no_derivation_fires_over_a_window_it_was_not_given() -> None:
    """`build_history` is pure in its window: a period outside it cannot be derived into the series.

    The tag-form derivation reads two tags no chain names, so it needs its own `as_of` and window
    filtering — `_prepare_raw` applies them, and the risk is precisely that this one path around the
    registry also escapes the filters. A window ending before the fixture's later balance sheet must
    leave that period out of the derived series entirely.
    """
    fixture = "NCI.trimmed.json"
    ends = sorted(_by_end(fixture, LSE))
    early, late = ends[0], ends[-1]
    assert early < late

    narrow = history(fixture, window=(M2_WINDOW[0], early))
    derived_ends = {fact.period.end for fact in narrow.series(Metric.LIABILITIES, Bucket.ANNUAL)}

    assert late not in derived_ends
    assert derived_ends <= {early}


def test_a_derived_metric_carries_its_own_chain_unit() -> None:
    """The emitted `Fact.unit` is the chain's, not the minuend's, for every derivation.

    They agree for the two difference forms and cannot for the ratio, so reading the unit off an
    input would be right twice and wrong once — and the once is diluted EPS, where being wrong by
    three orders of magnitude is the documented failure.
    """
    built = history("NCI.trimmed.json")
    for fact in built.series(Metric.LIABILITIES, Bucket.ANNUAL):
        assert fact.unit == chain_for(Metric.LIABILITIES).unit

    spec = _spec_for(Metric.EPS_DILUTED)
    numerator, denominator = spec.metric_inputs
    produced, _ = derive(
        spec,
        resolved=_resolved(
            _fact(numerator, "1000", unit=chain_for(numerator).unit),
            _fact(denominator, "250", unit=chain_for(denominator).unit),
        ),
        raw={},
        periods=[PERIOD],
    )[0]
    assert produced.unit == chain_for(Metric.EPS_DILUTED).unit
    assert produced.unit != chain_for(numerator).unit

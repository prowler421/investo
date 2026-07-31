"""The five-part dedup key, the ``filed`` tiebreak, and the re-filing whose value never moves.

DESIGN.md §4.2(b) states the key as ``(unit, start, end)`` and `docs/m2/02-facts.md` §3 writes it
out in full as ``(taxonomy, tag, unit, start, end)``. That is a clarification, not a conflict:
``companyfacts`` nests facts under taxonomy → tag → unit, so §4.2's three-part key is already
within-tag *while the nesting exists*. M2 flattens it to resolve chains, and a three-part key
applied to the flattened set dedups a ``Revenues`` fact against a ``SalesRevenueNet`` fact for one
period — two different concepts collapsed to whichever was filed later, with a plausible number
surviving.

Two things make this module's assertions look odd until the reason is stated:

- **They are on ``source.accession``, not on ``value``.** The `AAPL` fixture's quarter ending
  2019-06-29 appears under four accessions with four ``filed`` dates and the *same* 53,809,000,000
  each time. Nothing about the number moves when dedup breaks; what moves is the accession printed
  in §9.1's appendix. This is CLAUDE.md's "assert the derivation, not the value" applied to a case
  where the value is uninformative by construction.
- **The superseded facts are asserted to still be there.** They are the restatement record
  (`docs/m2/02-facts.md` §8), and a dedup that discarded them would leave M4 to re-derive the
  finding, which means re-parsing.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date, timedelta

import pytest

from investo.domain.models import COVER_SHARES_TAXONOMY, Metric, RawFact
from investo.domain.periods import FiscalPeriod
from investo.domain.provenance import Accession
from investo.normalize.facts import dedup, dedup_all
from investo.normalize.tags import chain_for
from tests.conftest import raw_facts

AAPL = "AAPL.trimmed.json"
REVENUE = chain_for(Metric.REVENUE)

REFILED_QUARTER_END = date(2019, 6, 29)
"""The quarter `AAPL` reports four times, under four accessions, at one value."""

STITCH_YEAR_END = date(2017, 9, 30)
"""One as-filed annual period, the base for the synthesized key cases below. Taken from the fixture
rather than built from nothing, so every field a real fact carries is populated."""

LATER_ACCESSION = Accession.parse("0000000009-20-000009")
"""An accession no fixture row uses, so a survivor identified by it is unambiguous."""


def _aapl_revenue() -> tuple[RawFact, ...]:
    """Every fact any member of the revenue chain names, as filed.

    Through ``REVENUE.keys`` rather than a tag literal, because the chain is what the report
    resolves and `AAPL` spans two of its members.
    """
    return raw_facts(AAPL, *REVENUE.keys)


def _group(end: date) -> tuple[RawFact, ...]:
    return tuple(fact for fact in _aapl_revenue() if fact.period.end == end)


def _one(end: date) -> RawFact:
    group = _group(end)
    assert len(group) == 1, f"{end} should be a single as-filed fact, got {len(group)}"
    return group[0]


def _key(fact: RawFact) -> tuple[str, str, str, date | None, date]:
    """``(taxonomy, tag, unit, start, end)``, spelled out here rather than imported.

    Two copies on purpose: a change to ``facts._dedup_key`` then has to be a deliberate edit in this
    file too, which is the same treatment `test_periods.py` gives the duration bands.
    """
    return (fact.taxonomy, fact.tag, fact.unit, fact.period.start, fact.period.end)


def _refiled(fact: RawFact, *, filed: date, accession: Accession) -> RawFact:
    """The same fact under a new filing date and accession — an **identical** dedup key."""
    return dataclasses.replace(
        fact, source=dataclasses.replace(fact.source, filed=filed, accession=accession)
    )


def _refiled_later(fact: RawFact) -> RawFact:
    """A year later, so the control row of the key table below is decidable: whichever of the pair
    survives says which key the implementation used."""
    later = fact.source.filed + timedelta(days=365)
    return _refiled(fact, filed=later, accession=LATER_ACCESSION)


def _unchanged(fact: RawFact) -> RawFact:
    return fact


def _other_taxonomy(fact: RawFact) -> RawFact:
    """``dei`` rather than the financial taxonomy — ``Assets`` exists in more than one."""
    return dataclasses.replace(fact, taxonomy=COVER_SHARES_TAXONOMY)


def _other_tag(fact: RawFact) -> RawFact:
    """Another member of the same chain: the substitution a flattened three-part key collapses."""
    tag = next(member.tag for member in REVENUE.members if member.tag != fact.tag)
    return dataclasses.replace(fact, tag=tag)


def _other_unit(fact: RawFact) -> RawFact:
    """The per-share unit. §4.2 warns twice that a unit difference is a value difference."""
    return dataclasses.replace(fact, unit=chain_for(Metric.EPS_DILUTED).unit)


def _other_start(fact: RawFact) -> RawFact:
    """A start one day later, which ``FiscalPeriod`` equality cannot see. See its own test."""
    assert fact.period.start is not None
    return dataclasses.replace(
        fact, period=FiscalPeriod.of(fact.period.start + timedelta(days=1), fact.period.end)
    )


def _other_end(fact: RawFact) -> RawFact:
    return dataclasses.replace(
        fact, period=FiscalPeriod.of(fact.period.start, fact.period.end + timedelta(days=1))
    )


# ---------------------------------------------------------------------------
# The two named guarantees — docs/m2/05-testing.md §5
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_revenues_and_salesrevenuenet_both_survive() -> None:
    """Two chain members, one period, one unit: two facts out, not one.

    The pair is constructed to fail under §4.2's literal three-part key rather than to pass under
    the five-part one — the impostor is filed three years later, so a key that ignores ``tag``
    elects it and the pre-ASC 606 concept disappears. What comes out of that is a single "revenue"
    series whose 2017 figure is tagged with the post-2018 concept, which reads as complete and is a
    different number for any filer with material assessed tax.

    The tags come from ``REVENUE.members`` rather than being spelled out, so this test is about the
    registry's own members instead of two strings that happen to be in it.
    """
    original = _one(STITCH_YEAR_END)
    impostor = _other_tag(_refiled_later(original))

    survivors, superseded = dedup_all([original, impostor])

    assert original.tag != impostor.tag
    assert (original.period, original.unit) == (impostor.period, impostor.unit)
    assert {fact.tag for fact in survivors} == {original.tag, impostor.tag}
    assert dict(superseded) == {}, "neither is a generation of the other"


@pytest.mark.spec
@pytest.mark.parametrize("reversed_input", [False, True], ids=["as-parsed", "reversed"])
def test_same_filed_date_breaks_on_accession(reversed_input: bool) -> None:
    """Two accessions, one ``filed``, and the survivor must not depend on payload iteration order.

    An original and an amendment filed together, or a 10-K and an 8-K exhibit, is ordinary. Without
    an explicit tiebreak the winner comes from ``dict`` iteration over the parsed JSON — stable in
    CPython, not a guarantee, and it flips the moment `reduce_fixture.py` reorders anything. So the
    test feeds both orderings and asserts the same answer, and asserts *which* answer by deriving it
    from ``Accession``'s own ordering rather than naming a string: ascending, so the higher wins.
    """
    base = _one(STITCH_YEAR_END)
    same_day = base.source.filed + timedelta(days=30)
    lower = _refiled(base, filed=same_day, accession=Accession.parse("0000000001-20-000001"))
    higher = _refiled(base, filed=same_day, accession=LATER_ACCESSION)
    pair = [higher, lower] if reversed_input else [lower, higher]

    survivor, superseded = dedup(pair)

    assert lower.source.filed == higher.source.filed, "the tie, stated"
    assert survivor.source.accession == max(fact.source.accession for fact in pair)
    assert [fact.source.accession for fact in superseded] == [lower.source.accession]


# ---------------------------------------------------------------------------
# The equal-value re-filing
# ---------------------------------------------------------------------------
def test_the_refiled_quarter_carries_one_value_under_four_accessions() -> None:
    """Pins why the tests below assert on the accession.

    If the fixture ever gained four *different* values for this quarter, a value assertion would
    start working and the reason for the accession assertion would quietly stop being true — the
    kind of drift that leaves a test looking over-engineered until dedup breaks.
    """
    group = _group(REFILED_QUARTER_END)

    assert len(group) == 4
    assert len({fact.source.filed for fact in group}) == 4
    assert len({fact.source.accession for fact in group}) == 4
    assert len({fact.value for fact in group}) == 1, "the value cannot detect a broken dedup here"


@pytest.mark.spec
def test_equal_values_are_still_deduped_and_the_late_filing_wins() -> None:
    """One survivor, and it is the one whose accession the appendix will print.

    The expected accession is derived from the group's own ``max(filed)`` rather than written down,
    because writing it down would pass under an implementation that sorted on the accession alone —
    and on this fixture those two rules happen to agree. ``test_filed_outranks_the_accession`` is
    the case where they do not.
    """
    group = _group(REFILED_QUARTER_END)
    expected = max(group, key=lambda fact: fact.source.filed)

    survivor, superseded = dedup(group)

    assert survivor.source.accession == expected.source.accession
    assert survivor.source.filed == expected.source.filed
    assert len(superseded) == 3


@pytest.mark.spec
def test_filed_outranks_the_accession() -> None:
    """``filed`` is the primary key and the accession only breaks its ties.

    Built so the two disagree: the later filing carries the *lower* accession. A sort keyed on the
    accession first returns the 2020 generation of a 2021 fact, which is a point-in-time answer that
    is wrong by a year and traces to a real filing, so nothing downstream can tell.
    """
    base = _one(STITCH_YEAR_END)
    low = Accession.parse("0000000001-21-000001")
    late_low = _refiled(base, filed=date(2021, 1, 1), accession=low)
    early_high = _refiled(base, filed=date(2020, 1, 1), accession=LATER_ACCESSION)
    assert early_high.source.accession > late_low.source.accession, "the disagreement, stated"

    survivor, _ = dedup([early_high, late_low])

    assert survivor.source.filed == late_low.source.filed


@pytest.mark.spec
def test_superseded_generations_are_returned_not_discarded() -> None:
    """§8: dedup's losers are the restatement record, so nothing may be dropped on the floor.

    Asserted as a partition — survivor plus superseded is the input, exactly — rather than as a
    count, because a count of three passes for an implementation returning three arbitrary facts.
    The ordering is separate: `docs/m2/02-facts.md` §8 specifies ascending, and the appendix prints
    the generations in that order.
    """
    group = _group(REFILED_QUARTER_END)

    survivor, superseded = dedup(group)

    kept = {fact.source.accession for fact in (survivor, *superseded)}
    assert kept == {fact.source.accession for fact in group}
    filings = [fact.source.filed for fact in superseded]
    assert filings == sorted(filings)
    assert all(filed < survivor.source.filed for filed in filings)


# ---------------------------------------------------------------------------
# The key itself
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize(
    ("mutate", "expected_survivors"),
    [
        (_unchanged, 1),
        (_other_taxonomy, 2),
        (_other_tag, 2),
        (_other_unit, 2),
        (_other_start, 2),
        (_other_end, 2),
    ],
    ids=["identical-key", "taxonomy", "tag", "unit", "start", "end"],
)
def test_each_of_the_five_components_separates_two_facts(
    mutate: Callable[[RawFact], RawFact], expected_survivors: int
) -> None:
    """One row per component of ``(taxonomy, tag, unit, start, end)``, plus the control.

    Every row is the same experiment: take one as-filed fact, refile it later under a new accession,
    change exactly one component of the key, and count survivors. The control row — nothing changed
    — must collapse to the refiled generation, or the test is passing because dedup does nothing.
    The five others must not collapse, because each names a difference that makes the two facts
    different numbers rather than two generations of one.

    Written as a table because a key is a conjunction: a test that only probed ``tag`` would pass
    for an implementation that had quietly dropped ``unit``, and §4.2 warns about the unit case
    twice.
    """
    original = _one(STITCH_YEAR_END)
    twin = mutate(_refiled_later(original))

    survivors, superseded = dedup_all([original, twin])

    assert len(survivors) == expected_survivors
    if expected_survivors == 1:
        assert _key(original) == _key(twin), "the control row changes no component of the key"
        assert survivors[0].source.accession == LATER_ACCESSION, "the later filing wins"
        assert sum(len(losers) for losers in superseded.values()) == 1
    else:
        assert _key(original) != _key(twin)
        assert dict(superseded) == {}, "two different facts, so nothing was superseded"


@pytest.mark.spec
def test_start_is_part_of_the_key_although_fiscalperiod_ignores_it() -> None:
    """The one component that cannot be read off ``FiscalPeriod`` equality.

    ``start`` is ``compare=False`` (`docs/m1/01-domain-types.md`), so two durations with the same
    ``end`` and ``kind`` compare **equal** — which means an implementation keyed on the period
    object rather than on ``(start, end)`` would look correct, pass the table above's other rows,
    and merge two facts the payload states separately. Asserted with the equality spelled out, so
    the surprise is in the test rather than in a future debugging session.
    """
    original = _one(STITCH_YEAR_END)
    shifted = _other_start(_refiled_later(original))
    starts = {original.period.start, shifted.period.start}

    assert original.period == shifted.period, "equal periods, different starts"
    assert len(starts) == 2

    survivors, _ = dedup_all([original, shifted])

    assert {fact.period.start for fact in survivors} == starts


@pytest.mark.spec
def test_dedup_all_elects_one_survivor_per_key() -> None:
    """Over the whole fixture: as many survivors as there are distinct keys, and no fact lost.

    The expected count is computed from the payload rather than written down, so the test states the
    invariant — one survivor per key, and survivors plus superseded accounts for every input —
    instead of asserting that `AAPL` currently has five revenue periods. `AAPL` is the right payload
    for it because it carries a two-generation group and a four-generation one alongside three
    singletons.
    """
    facts = _aapl_revenue()
    keys = {_key(fact) for fact in facts}

    survivors, superseded = dedup_all(facts)
    losers = sum(len(group) for group in superseded.values())

    assert len(survivors) == len(keys)
    assert len(survivors) + losers == len(facts)
    assert set(superseded) <= keys


@pytest.mark.spec
def test_dedup_all_does_not_inherit_the_input_order() -> None:
    """The output order is a function of the values, not of dict iteration over a parsed payload.

    `docs/m2/02-facts.md` §9: ``FiscalPeriod`` is not a total order over facts, Python's sort is
    stable, and a stable sort over a partial key returns input order for the ties. That is
    deterministic today and is not a property DESIGN.md §11's byte-identical gate should rest on, so
    the assertion is that reversing the input changes nothing at all.
    """
    facts = _aapl_revenue()

    forward, forward_losers = dedup_all(facts)
    backward, backward_losers = dedup_all(tuple(reversed(facts)))

    assert forward == backward
    assert dict(forward_losers) == dict(backward_losers)


def test_dedup_of_nothing_raises() -> None:
    """There is no survivor of an empty set, and ``None`` would push that into every caller.

    A ``ValueError`` here is a caller bug — every call site groups by key first, so an empty group
    cannot occur — while an optional return would make the impossible case something six callers
    each have to handle, and one of them would handle it by inventing a zero.
    """
    with pytest.raises(ValueError, match="at least one fact"):
        _ = dedup([])

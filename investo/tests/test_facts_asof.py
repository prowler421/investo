"""``as_of`` runs before dedup, and the ``RESTATER`` fixture is the whole argument.

`docs/m2/02-facts.md` §1 fixes the order; DESIGN.md §4.2(b) gives the rule it implements as
``max(filed) where filed <= as_of``. Both orderings compile, both produce a series, and only one of
them answers the question. `RESTATER` carries a single period — 2020-01-01..2020-12-31 — filed four
times with four different values, so the two orders are distinguishable on it:

- **filter, then dedup** answers 812,000,000 at ``--as-of 2021-06-30``: the number that was true on
  that date, which is what §8's point-in-time reconstruction means.
- **dedup, then filter** answers *nothing*, because ``max(filed)`` over the full set is a 2023
  filing which the cut then discards. A backtest that silently loses its most recent fiscal year at
  every date is a backtest measuring something else, and it loses it without an error.

The observable difference between *filtering* and *suppressing afterwards* is the restatement
record: at 2021-06-30 it is **empty**, rather than holding three entries marked "not yet filed". Two
tests below assert a literal value and each says in its docstring why the value is the assertion —
the rest assert the derivation, because a survivor that happens to be right at one cut date is right
under an implementation that reads the wrong field.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from decimal import Decimal

import pytest

from investo.domain.models import Metric, RawFact
from investo.domain.periods import FiscalPeriod
from investo.normalize.facts import (
    MetricSeries,
    dedup,
    dedup_all,
    filter_as_of,
    normalize_metric,
)
from investo.normalize.tags import chain_for
from tests.conftest import M2_WINDOW, company_facts, raw_facts

RESTATER = "RESTATER.trimmed.json"
REVENUE = chain_for(Metric.REVENUE)

PERIOD_END = date(2020, 12, 31)
"""The one period `RESTATER` reports. Every fact in the fixture describes it."""

FIRST_FILING = Decimal("812000000")
"""What was true on 2021-06-30. `tests/fixtures/edgar/PROVENANCE.md` records it as this fixture's
expected answer, so the assertion was written down before the code was."""

CUTS = [
    date(2021, 2, 24),
    date(2021, 6, 30),
    date(2021, 8, 5),
    date(2022, 2, 23),
    date(2023, 2, 22),
    date(2026, 1, 1),
]
CUT_IDS = [
    "on-the-first-filing",
    "between-the-first-two",
    "on-the-second-filing",
    "on-the-third-filing",
    "on-the-fourth-filing",
    "after-everything",
]


def _restater_revenue() -> tuple[RawFact, ...]:
    """Every fact any member of the revenue chain names, as filed and unfiltered.

    Read through ``REVENUE.keys`` rather than a tag literal so the test is about the chain the
    report actually resolves, and so it keeps working if the fixture is regenerated under a
    different member of it.
    """
    return raw_facts(RESTATER, *REVENUE.keys)


def _series(as_of: date | None) -> MetricSeries:
    """The whole per-metric pipeline at one cut date, over the window every M2 test shares."""
    return normalize_metric(REVENUE, company_facts(RESTATER).facts, window=M2_WINDOW, as_of=as_of)


def _refiled(fact: RawFact, *, filed: date, start: date, end: date) -> RawFact:
    """The same fact with its filing date and its period moved independently.

    Two dates a correct filter treats completely differently, which is only demonstrable if a test
    can move one without the other.
    """
    return dataclasses.replace(
        fact,
        period=FiscalPeriod.of(start, end),
        source=dataclasses.replace(fact.source, filed=filed),
    )


def test_the_fixture_carries_four_generations_of_one_period() -> None:
    """Pins the trap, because every assertion below is vacuous without it.

    If the payload were ever regenerated with one filing per period, filter-then-dedup and
    dedup-then-filter would agree and this whole module would pass while enforcing nothing.
    """
    facts = _restater_revenue()

    assert len(facts) == 4
    assert {fact.period.end for fact in facts} == {PERIOD_END}
    assert len({fact.source.filed for fact in facts}) == 4, "four distinct filing dates"
    assert len({fact.value for fact in facts}) == 4, "and four distinct values"


# ---------------------------------------------------------------------------
# The two named guarantees — docs/m2/05-testing.md §5
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_restater_at_2021_06_30_yields_the_first_filing() -> None:
    """§4.2(b) and §8: the value current on the date, and an **empty** restatement record.

    This is one of the two places a literal value is the assertion rather than a coincidence:
    812,000,000 is recorded in `PROVENANCE.md` as this fixture's expected answer, and the wrong
    order does not produce a different number — it produces *no* number, so a test that only
    asserted "some fact exists" would pass on the 2023 restatement leaking through.

    The second assertion is the one that separates the two implementations that both print
    812,000,000: filtering first means the three later generations were never candidates, so there
    is nothing superseded. An implementation that deduped first and suppressed the winner afterwards
    would know about them, and would either record them or have to decide not to — and either way
    the report would be built from a set of facts that includes the future.
    """
    series = _series(date(2021, 6, 30))

    assert [fact.value for fact in series.annual.facts] == [FIRST_FILING]
    assert [entry for record in series.restatements for entry in record.superseded] == []


@pytest.mark.spec
def test_period_survives_as_of_cut() -> None:
    """The violation first, then the guarantee — the naive order loses the period entirely.

    ``dedup_all`` over the unfiltered set elects the 2023 filing, and the cut then removes it: a
    **hole** where §4.2(b)'s answer is 812,000,000. That half of this test is what makes the second
    half mean something, because "one fact exists for 2020" passes trivially under the correct order
    and would keep passing if the two steps were swapped and the fixture happened to hold only early
    filings.
    """
    facts = _restater_revenue()
    as_of = date(2021, 6, 30)
    latest = max(fact.source.filed for fact in facts)

    deduped_first, _ = dedup_all(facts)
    assert [fact.source.filed for fact in deduped_first] == [latest]
    assert filter_as_of(deduped_first, as_of=as_of) == (), "dedup-then-filter loses the period"

    survivors, _ = dedup_all(filter_as_of(facts, as_of=as_of))
    assert [fact.period.end for fact in survivors] == [PERIOD_END]
    assert [fact.period.end for fact in _series(as_of).annual.facts] == [PERIOD_END]


# ---------------------------------------------------------------------------
# The rule, rather than one of its answers
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize("as_of", [*CUTS, None], ids=[*CUT_IDS, "no-cut"])
def test_survivor_is_the_latest_filing_at_or_before_the_cut(as_of: date | None) -> None:
    """``max(filed) where filed <= as_of``, asserted as the relationship rather than as six values.

    The expected survivor is computed from the fixture's own rows at every cut, so the test states
    §4.2(b)'s rule instead of six numbers that agree with it. A hard-coded table would pass under an
    implementation that took the *first* filing, or the lowest accession, at any date where those
    coincide — and on a four-row fixture they coincide often.
    """
    facts = _restater_revenue()
    in_time = [fact for fact in facts if as_of is None or fact.source.filed <= as_of]
    winner = max(in_time, key=lambda fact: fact.source.filed)
    losers = [fact.source.filed for fact in in_time if fact is not winner]

    survivor, superseded = dedup(filter_as_of(facts, as_of=as_of))

    assert survivor.source.filed == winner.source.filed
    assert survivor.value == winner.value
    assert [fact.source.filed for fact in superseded] == sorted(losers)


@pytest.mark.spec
@pytest.mark.parametrize("as_of", CUTS, ids=CUT_IDS)
def test_the_restatement_record_holds_only_generations_filed_in_time(as_of: date) -> None:
    """§8: a restatement filed after the cut cannot reach the record, so there is nothing to hide.

    The count is derived from the fixture at each cut — zero at 2021-06-30, three with no cut —
    which is the same statement as "the filter ran first" made where it is observable. The second
    assertion is the violation test: an implementation that deduped over the full set and marked the
    losers would produce entries whose ``filed`` is after ``as_of``, and no number in the report
    would look wrong.
    """
    facts = _restater_revenue()
    in_time = [fact for fact in facts if fact.source.filed <= as_of]
    entries = [entry for record in _series(as_of).restatements for entry in record.superseded]

    assert len(entries) == len(in_time) - 1
    assert all(filed <= as_of for filed, _value, _accession in entries)


@pytest.mark.spec
def test_the_cut_includes_the_filing_date_itself() -> None:
    """The boundary: ``filed <= as_of``, so a filing made *on* the cut date is knowable.

    A ``<`` where ``<=`` belongs survives every test that probes a date in the middle of the
    fixture's four filings, and it is wrong in the direction that matters — the report for the day a
    10-K was filed would be built as though it had not been.
    """
    facts = _restater_revenue()
    earliest = min(fact.source.filed for fact in facts)

    assert [fact.value for fact in filter_as_of(facts, as_of=earliest)] == [FIRST_FILING]
    assert filter_as_of(facts, as_of=earliest - timedelta(days=1)) == ()


@pytest.mark.spec
def test_as_of_none_is_the_current_view() -> None:
    """``None`` means no filtering — §4.2(b)'s ``max(filed)``, "right for what is true now".

    Asserted as an identity on the fact tuple rather than on a count, because a filter that dropped
    one row for an unrelated reason would still return "four-ish" facts. The restatement record is
    checked in the same test because this is the case where it must be *populated*: the contrast
    with ``test_restater_at_2021_06_30_yields_the_first_filing`` is the evidence that the empty
    record there came from the cut, and not from a record that is never written.
    """
    facts = _restater_revenue()

    assert filter_as_of(facts, as_of=None) == facts

    series = _series(None)
    latest = max(facts, key=lambda fact: fact.source.filed)
    assert [fact.value for fact in series.annual.facts] == [latest.value]

    assert len(series.restatements) == 1
    record = series.restatements[0]
    assert record.current == latest.value
    assert len(record.superseded) == 3
    assert record.value_changed is True, "four values is a restatement, not a re-filing"


@pytest.mark.spec
def test_the_filter_reads_filed_and_nothing_else() -> None:
    """Never ``period.end``, never ``report_date``, never ``accepted_at``.

    Filing an amendment days before the cut for a period ending after it is legal and happens, and
    ``filed`` is the only date that answers "could we have known this then". The two facts here are
    built to be *opposite* under the two candidate rules: one describes a period entirely in the
    future of the cut and was filed before it, the other describes an old period and was filed after
    it. A filter on ``period.end`` keeps exactly the wrong one, so the assertion cannot be satisfied
    by both readings.
    """
    base = _restater_revenue()[0]
    as_of = date(2021, 6, 30)
    in_time = as_of - timedelta(days=1)
    too_late = as_of + timedelta(days=1)

    knowable = _refiled(base, filed=in_time, start=date(2022, 1, 1), end=date(2022, 12, 31))
    unknowable = _refiled(base, filed=too_late, start=date(2019, 1, 1), end=date(2019, 12, 31))
    assert knowable.period.end > as_of, "filed in time, describes the future — must survive"
    assert unknowable.period.end < as_of, "describes the past, filed too late — must not"

    kept = filter_as_of([knowable, unknowable], as_of=as_of)

    assert [fact.period.end for fact in kept] == [knowable.period.end]

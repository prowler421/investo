"""`market_cap`: the absence/failure split, and the two ways to get a plausible wrong number.

`docs/m1/01-domain-types.md` § Empty input returns None; malformed input raises is normative here.
The two failure modes are handled differently on purpose — an empty `share_facts` is an *absence*
that a NASDAQ filer really produces, while a share count from the wrong tag or from two cover pages
is *malformed input* — and both sides are tested, because an implementation that returned `None` for
everything would satisfy the first half and hide the second.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from investo.domain.models import (
    COVER_SHARES_TAG,
    COVER_SHARES_TAXONOMY,
    RawFact,
    cover_share_facts,
    market_cap,
)
from investo.domain.periods import FiscalPeriod, PeriodKind
from investo.domain.provenance import Accession, Derivation, SourceRef
from investo.ingest.prices.base import PriceBar, PriceSeries, price_at_or_before, price_source_ref
from tests.conftest import FETCHED_AT

COVER_DATE = date(2019, 10, 18)
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
STOOQ_URL = "https://stooq.com/q/d/l/"


def _share_fact(
    *,
    value: str,
    end: date = COVER_DATE,
    start: date | None = None,
    taxonomy: str = COVER_SHARES_TAXONOMY,
    tag: str = COVER_SHARES_TAG,
    unit: str = "shares",
    accession: str = "0000320193-19-000119",
) -> RawFact:
    """A cover-page share count, with every field a test might need to make wrong."""
    return RawFact(
        taxonomy=taxonomy,
        tag=tag,
        unit=unit,
        value=Decimal(value),
        period=FiscalPeriod.of(start, end),
        source=SourceRef(
            accession=Accession.parse(accession),
            taxonomy=taxonomy,
            tag=tag,
            form="10-K",
            filed=end,
            url=FACTS_URL,
            fetched_at=FETCHED_AT,
        ),
        filing_fy=2019,
        filing_fp="FY",
    )


def _price_source(day: date = COVER_DATE) -> SourceRef:
    return price_source_ref(provider="stooq", url=STOOQ_URL, day=day, fetched_at=FETCHED_AT)


def _series(closes: Sequence[tuple[str, str]]) -> PriceSeries:
    """A `PriceSeries` from `(day, close)` pairs. `adj_close` is `None`, so `adjusted` is
    `False`."""
    bars = tuple(
        PriceBar(day=date.fromisoformat(day), close=Decimal(close), adj_close=None)
        for day, close in closes
    )
    return PriceSeries(
        ticker="AAPL",
        provider="stooq",
        bars=bars,
        adjusted=False,
        fetched_at=FETCHED_AT,
        source=_price_source(bars[-1].day),
    )


# ---------------------------------------------------------------------------
# Absence
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_absent_dei_returns_none() -> None:
    """Empty `share_facts` yields no market cap — **not zero**.

    Confirmed live: a `companyfacts` payload for a recently-listed NASDAQ filer contains `ffd` and
    `us-gaap` and no `dei` at all, and `dei:EntityCommonStockSharesOutstanding` is the only source
    for the share count. So this path is reached by the first NASDAQ IPO anyone analyses.

    Both assertions are here because a `0` is the dangerous answer, not a crash: it is falsy, it
    passes an `if market_cap:` guard, and it then flows into every multiple in report section 3 and
    into the valuation sub-score as a division by zero or an infinite P/E that looks computed. The
    `is None` assertion is what makes forgetting the check a type error at the call site instead.
    """
    computed = market_cap(price=Decimal("217.31"), price_source=_price_source(), share_facts=())

    assert computed is None
    assert not isinstance(computed, tuple), "an absence must not arrive as `(0, Derivation)`"


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_mixed_end_dates_raise() -> None:
    """Market cap never sums share counts from different cover pages.

    A `companyfacts` payload holds every cover page the filer has ever submitted, so summing them is
    one line of plausible-looking code that produces a market cap several times too large — the
    exact class of failure this project exists to avoid, because nothing downstream can detect it.
    """
    facts = (
        _share_fact(value="4443236000"),
        _share_fact(value="4400000000", end=date(2018, 10, 26)),
    )
    with pytest.raises(ValueError, match="more than one date"):
        _ = market_cap(price=Decimal("217.31"), price_source=_price_source(), share_facts=facts)


@pytest.mark.spec
def test_wrong_tag_raises() -> None:
    """A weighted-average share count is a per-share denominator and belongs nowhere near this.

    DESIGN.md §5.4 calls using the cover-page count where diluted weighted-average shares belong a
    classic error; this is the same error in the other direction, and it is the one a runtime check
    *can* catch. The fact is otherwise valid — instant, unit `shares` — so the tag is provably what
    caused the raise rather than one of the other guards firing first.
    """
    wrong = _share_fact(
        value="18595651000",
        taxonomy="us-gaap",
        tag="WeightedAverageNumberOfDilutedSharesOutstanding",
    )
    assert wrong.period.kind is PeriodKind.INSTANT
    assert wrong.unit == "shares"

    with pytest.raises(ValueError, match="WeightedAverageNumberOfDilutedSharesOutstanding"):
        _ = market_cap(price=Decimal("217.31"), price_source=_price_source(), share_facts=(wrong,))


@pytest.mark.spec
def test_a_duration_share_count_raises() -> None:
    """A cover-page count is an instant; a duration here means the wrong facts were selected.

    Same tag, same unit, same value — only the period differs, so the period is what the raise is
    about. Without this, `cover_share_facts` could be bypassed by a caller filtering on tag alone
    and the resulting sum would double-count a company that tagged the count on two statements.
    """
    duration = _share_fact(value="4443236000", start=date(2018, 10, 19), end=COVER_DATE)
    assert duration.period.kind is not PeriodKind.INSTANT

    with pytest.raises(ValueError, match="instant"):
        _ = market_cap(
            price=Decimal("217.31"), price_source=_price_source(), share_facts=(duration,)
        )


@pytest.mark.spec
def test_a_wrong_unit_raises() -> None:
    """DESIGN.md §4.2 twice warns that unit differences are value differences.

    A share count arriving under `USD` is a dollar amount that would multiply by a price, and the
    product has no meaning at all — but it is a number, and it would be printed.
    """
    priced_in_dollars = _share_fact(value="4443236000", unit="USD")

    with pytest.raises(ValueError, match="expected 'shares'"):
        _ = market_cap(
            price=Decimal("217.31"), price_source=_price_source(), share_facts=(priced_in_dollars,)
        )


# ---------------------------------------------------------------------------
# The computed value, and its provenance
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_sums_across_classes_and_returns_the_derivation() -> None:
    """DESIGN.md §5.4: sum all classes, and state which ones were counted.

    The expected value is written as `price * sum(shares)` rather than as a literal, so the test
    asserts the rule and not one arithmetic result that a wrong rule might happen to agree with.

    The provenance half is the point of returning a `Derivation` at all: the price ref *and* every
    share fact's ref are inputs, because a market cap traces to a filing and a price fetch, and
    printing it with only one of those would look traced.
    """
    price = Decimal("100.50")
    price_source = _price_source()
    googl = _share_fact(value="5800000000", accession="0000320193-19-000119")
    goog = _share_fact(value="5500000000", accession="0000320193-20-000096")

    computed = market_cap(
        price=price,
        price_source=price_source,
        share_facts=(googl, goog),
        classes=("GOOG", "GOOGL"),
    )
    assert computed is not None
    value, derivation = computed

    assert value == price * (googl.value + goog.value)
    assert not isinstance(value, float)
    assert derivation.rule == "market_cap"
    assert derivation.inputs[0] is price_source
    assert set(derivation.inputs[1:]) == {googl.source, goog.source}
    assert derivation.note is not None
    assert "GOOG, GOOGL" in derivation.note
    assert COVER_DATE.isoformat() in derivation.note


@pytest.mark.spec
def test_note_records_the_class_count_when_labels_are_unavailable() -> None:
    """§5.4 requires the report to state which classes were counted, so silence is not an option.

    `domain/` cannot know the tickers — `companyfacts` is keyed by CIK and carries none — so when
    the caller supplies no labels the note says how many facts were summed rather than nothing. A
    note that omitted the fact entirely would make a two-class sum indistinguishable from a
    one-class one on the page.
    """
    facts = (_share_fact(value="1000"), _share_fact(value="2000"))
    computed = market_cap(price=Decimal("1"), price_source=_price_source(), share_facts=facts)

    assert computed is not None
    assert computed[1].note is not None
    assert "2 share-count fact(s)" in computed[1].note


@pytest.mark.spec
def test_derivation_refs_flattens_to_the_leaves() -> None:
    """The appendix cites filings, not rules, so it needs the leaves of a nested derivation.

    Nesting is not hypothetical: M2's derived margin over a stitched series is three levels deep,
    and every consumer would otherwise re-implement the walk — each with its own bug.
    """
    share = _share_fact(value="4443236000")
    price_source = _price_source()
    computed = market_cap(price=Decimal("2"), price_source=price_source, share_facts=(share,))
    assert computed is not None
    derivation = computed[1]

    nested = Derivation(rule="pretend_downstream_ratio", inputs=(derivation,))

    assert nested.refs() == (price_source, share.source)
    assert nested.refs() == derivation.refs()
    assert all(isinstance(ref, SourceRef) for ref in nested.refs())


# ---------------------------------------------------------------------------
# cover_share_facts
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_cover_share_facts_selects_only_the_newest_cover_date() -> None:
    """Selection keeps every class on the newest cover page and nothing from an older one.

    The two functions are complementary rather than redundant: this one *filters*, `market_cap`
    *refuses*. Feeding the unfiltered list straight to `market_cap` is asserted to raise, which is
    what makes the pair safe when a future caller reaches for the facts directly.
    """
    older = _share_fact(value="4400000000", end=date(2018, 10, 26))
    class_a = _share_fact(value="2000000000")
    class_b = _share_fact(value="1000000000")

    selected = cover_share_facts((older, class_a, class_b))

    assert {fact.value for fact in selected} == {class_a.value, class_b.value}
    assert all(fact.period.end == COVER_DATE for fact in selected)

    with pytest.raises(ValueError, match="more than one date"):
        _ = market_cap(
            price=Decimal("1"),
            price_source=_price_source(),
            share_facts=(older, class_a, class_b),
        )


def test_cover_share_facts_ignores_other_tags_and_units() -> None:
    """The selector names the one tag `ingest/` is not allowed to name, so it has to be exact."""
    assert cover_share_facts((_share_fact(value="1", taxonomy="us-gaap"),)) == ()
    assert cover_share_facts((_share_fact(value="1", unit="USD"),)) == ()
    assert cover_share_facts((_share_fact(value="1", start=date(2019, 1, 1)),)) == ()


# ---------------------------------------------------------------------------
# The price half
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_price_is_last_bar_at_or_before_as_of() -> None:
    """Market cap never uses a price later than `as_of`.

    This is a lookahead-leak test. With `--as-of` set, taking the newest bar in the series would
    leak future information into the one number that gets compared against modelled value — and the
    leak is invisible, because a price is a price and the report would print a plausible figure.

    Three assertions make it discriminating rather than decorative: the series really does contain
    bars after `as_of`; the market cap matches the one computed from a series truncated at `as_of`;
    and the market cap computed from the *last* bar differs, so an implementation that took the last
    bar could not pass.
    """
    as_of = date(2026, 7, 29)
    closes = [
        ("2026-07-28", "213.12"),
        ("2026-07-29", "216.77"),
        ("2026-07-30", "218.05"),
        ("2026-07-31", "217.31"),
    ]
    full = _series(closes)
    truncated = _series([row for row in closes if date.fromisoformat(row[0]) <= as_of])
    assert any(bar.day > as_of for bar in full.bars), "the leak needs bars to leak from"

    chosen = price_at_or_before(full, as_of)
    from_truncated = price_at_or_before(truncated, as_of)
    assert chosen is not None
    assert from_truncated is not None
    assert chosen.day == as_of
    assert chosen.close == from_truncated.close, "the bars after as_of changed the answer"

    share_facts = (_share_fact(value="4443236000"),)
    honest = market_cap(price=chosen.close, price_source=full.source, share_facts=share_facts)
    leaked = market_cap(
        price=full.bars[-1].close, price_source=full.source, share_facts=share_facts
    )
    assert honest is not None
    assert leaked is not None

    assert honest[0] == chosen.close * share_facts[0].value
    assert honest[0] != leaked[0], "taking the last bar has to produce a different number"


@pytest.mark.spec
def test_no_bar_at_or_before_as_of_is_an_absence() -> None:
    """A ticker with no price history at or before the date is an absence, not a fetch failure.

    DESIGN.md §8 wants that wording specifically: conflating "no price history from {provider}" with
    a failed request hides the pattern that reveals survivorship bias, since delisted tickers are
    exactly the ones that return nothing.
    """
    series = _series([("2026-07-30", "218.05"), ("2026-07-31", "217.31")])
    assert price_at_or_before(series, date(2026, 7, 1)) is None

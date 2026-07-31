"""`company_tickers_exchange.json`: the header-driven parse, and both halves of exit 2.

Three of the four things `docs/m1/04-parsers.md` §1 says this parser must get right are invisible
when wrong — a positional index, a case-sensitive exchange comparison and a stringified CIK all
produce plausible output on today's payload. So each gets a test that fails on the wrong
implementation rather than one that passes on the right one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from investo.errors import ExitCode, TickerNotFoundError, UpstreamFetchError
from investo.ingest.edgar.tickers import TickerRow, classes_for_cik, parse_tickers, resolve
from tests.conftest import context, fixture_json

GOOGLE_CIK = 1652044
"""One CIK, two rows — GOOGL and GOOG. DESIGN.md §5.4's "sum all classes" needs both."""


def _payload(fields: Sequence[str], rows: Sequence[Sequence[object]]) -> bytes:
    """Re-serialize a header and its rows, so a variant fixture is built rather than hand-typed."""
    return json.dumps({"fields": list(fields), "data": [list(row) for row in rows]}).encode()


def _columns(payload: Any) -> tuple[list[str], dict[str, int]]:
    fields = [str(name) for name in payload["fields"]]
    return fields, {name: position for position, name in enumerate(fields)}


# ---------------------------------------------------------------------------
# The `fields` array is the header
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_reordered_fields_parse_identically() -> None:
    """`docs/m1/04-parsers.md` §1: read `fields`, never index positionally.

    A parser that hardcodes `row[3] == exchange` is correct until SEC inserts a column, at which
    point it reads company names as exchanges and every ticker becomes "not NASDAQ" — an outage
    that looks like SEC's file being broken. Permuting the header is the only way to tell the two
    implementations apart, because on today's column order they agree exactly.
    """
    payload: Any = fixture_json("edgar", "company_tickers_exchange.trimmed.json")
    fields, index = _columns(payload)
    shuffled = ["exchange", "ticker", "name", "cik"]
    assert sorted(shuffled) == sorted(fields), "the permutation must cover the real header"

    rows = [[row[index[name]] for name in shuffled] for row in payload["data"]]
    original = _payload(fields, [[row[index[name]] for name in fields] for row in payload["data"]])

    assert parse_tickers(_payload(shuffled, rows), source=context()) == parse_tickers(
        original, source=context()
    )


@pytest.mark.spec
def test_missing_column_raises_rather_than_mis_indexing() -> None:
    """A missing column is a shape change, and shape changes are exit 4, not a silent misread.

    The alternative implementation — index what is there and hope — turns a dropped `exchange`
    column into "no company is on NASDAQ", which is indistinguishable from a correct run against a
    delisted universe.
    """
    payload: Any = fixture_json("edgar", "company_tickers_exchange.trimmed.json")
    _, index = _columns(payload)
    kept = ["cik", "name", "ticker"]
    rows = [[row[index[name]] for name in kept] for row in payload["data"]]

    with pytest.raises(UpstreamFetchError) as caught:
        _ = parse_tickers(_payload(kept, rows), source=context())
    assert "exchange" in caught.value.message
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE


# ---------------------------------------------------------------------------
# The exchange comparison
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_the_literal_nasdaq_spelling_in_the_payload_resolves(tickers_body: bytes) -> None:
    """The payload spells it `"Nasdaq"`, mixed case.

    Asserted against the fixture's own bytes rather than a constant, because the failure this
    guards is a comparison against `"NASDAQ"` — which matches nothing and makes *every* ticker
    exit 2. A test that supplied its own uppercase row would pass under that bug.
    """
    rows = parse_tickers(tickers_body, source=context())
    assert resolve(rows, "AAPL").exchange == "Nasdaq"


@pytest.mark.spec
@pytest.mark.parametrize("spelling", ["NASDAQ", "nasdaq", "NaSdAq"])
def test_a_differently_cased_exchange_value_also_resolves(
    spelling: str, tickers_body: bytes
) -> None:
    """Comparison is casefolded in both directions, so SEC re-casing the value is not an outage.

    The fixture pins today's spelling; this pins that the parser does not depend on it.
    """
    rows = (
        *parse_tickers(tickers_body, source=context()),
        TickerRow(cik=999001, name="Shouty Corp", ticker="SHTY", exchange=spelling),
    )
    assert resolve(rows, "SHTY").cik == 999001


# ---------------------------------------------------------------------------
# Exit 2, both halves
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_nyse_listing_exits_2(tickers_body: bytes) -> None:
    """README and DESIGN.md §14: exit 2 is "ticker not found **or not NASDAQ**".

    This is the test that catches an implementation which resolved the CIK and forgot the exchange
    check. It must raise, not return the JPM row — a happy-path test over AAPL passes whether or
    not the second half of the rule is enforced.
    """
    rows = parse_tickers(tickers_body, source=context())
    assert any(row.ticker == "JPM" for row in rows), "the fixture must carry the non-NASDAQ row"

    with pytest.raises(TickerNotFoundError) as caught:
        _ = resolve(rows, "JPM")
    assert caught.value.exit_code == ExitCode.TICKER_NOT_FOUND
    assert "NYSE" in caught.value.message


@pytest.mark.spec
def test_absent_ticker_exits_2(tickers_body: bytes) -> None:
    """The other half of exit 2, and the one nobody forgets — pinned so the pair reads as a pair."""
    rows = parse_tickers(tickers_body, source=context())
    with pytest.raises(TickerNotFoundError) as caught:
        _ = resolve(rows, "NOTATICKER")
    assert caught.value.exit_code == ExitCode.TICKER_NOT_FOUND


# ---------------------------------------------------------------------------
# Row shape
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_cik_is_an_int_not_a_padded_string(tickers_body: bytes) -> None:
    """This is the one endpoint of the three that does not zero-pad, and padding is the client's.

    A parser that stringified the CIK here would give `client.cik_path` a value it re-pads, and
    the resulting `CIK00000000320193` 404s as a company that looks delisted.
    """
    row = resolve(parse_tickers(tickers_body, source=context()), "AAPL")
    assert isinstance(row.cik, int)
    assert not isinstance(row.cik, bool), "bool is an int subclass; a boolean CIK is not a CIK"
    assert row.cik == 320193


@pytest.mark.spec
def test_classes_for_cik_returns_every_class_in_a_deterministic_order(
    tickers_body: bytes,
) -> None:
    """`docs/m1/04-parsers.md` §1: one CIK can have several rows, and market cap needs all of them.

    Order is asserted to be independent of the payload's row order, not merely sorted: DESIGN.md
    §5.4 requires the report to state which classes were counted, and §11 makes the output
    byte-identical across runs. A set-backed implementation satisfies "both are returned" and
    fails both of those.
    """
    rows = parse_tickers(tickers_body, source=context())
    classes = classes_for_cik(rows, GOOGLE_CIK)

    assert [row.ticker for row in classes] == ["GOOG", "GOOGL"]
    assert classes == classes_for_cik(tuple(reversed(rows)), GOOGLE_CIK)


def test_classes_for_cik_is_empty_for_an_unknown_cik(tickers_body: bytes) -> None:
    """An absence, not a raise: a CIK with no NASDAQ class is a coverage fact for market cap."""
    assert classes_for_cik(parse_tickers(tickers_body, source=context()), 1) == ()

"""FINRA short interest, and the snapshot test that is really a cache test.

DESIGN.md §6.8: FINRA *revisions overwrite rather than append*, so point-in-time history exists
only if something keeps the old copy. Nothing special was built for that — each fetch writes a new
manifest entry and a revision produces a different `content_sha256` and therefore a new blob —
which means the whole feature rests on the cache never overwriting. So the last test in this file
is a cache test wearing a FINRA hat, and it is the one that matters.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from investo.errors import ExitCode, UpstreamFetchError
from investo.ingest.cache import Cache
from investo.ingest.finra import BULK_URL, FinraClient, ShortInterestRow, parse_short_interest

SETTLEMENT = date(2026, 7, 15)

CURRENT_SPELLING = b"""\
symbolCode,settlementDate,currentShortPositionQuantity,previousShortPositionQuantity,\
averageDailyVolumeQuantity,daysToCoverQuantity,marketClassCode
AAPL,2026-07-15,120000000,118500000,55000000,2.18,NNM
MSFT,2026-07-15,90000000,91000000,30000000,3.00,NNM
AAPL,not-a-date,999,999,999,9.99,NNM
"""
"""The dataset's current column names, plus a row whose settlement date will not parse."""

OLDER_SPELLING = b"""\
Symbol,Settlement Date,Current Short Position,Previous Short Position,\
Average Daily Volume,Days To Cover,Market
AAPL,07/15/2026,120000000,118500000,55000000,2.18,NNM
MSFT,07/15/2026,90000000,91000000,30000000,3.00,NNM
AAPL,not-a-date,999,999,999,9.99,NNM
"""
"""An earlier generation of the same file: different column names, different date format.

FINRA has renamed columns between file generations, so a positional read shifts every field by one
and a case-sensitive lookup finds none of them. Both spellings must produce identical rows.
"""

SHOUTING = b"""\
SYMBOLCODE,SETTLEMENTDATE,CURRENTSHORTPOSITIONQUANTITY,PREVIOUSSHORTPOSITIONQUANTITY,\
AVERAGEDAILYVOLUMEQUANTITY,DAYSTOCOVERQUANTITY,MARKETCLASSCODE
AAPL,20260715,120000000,118500000,55000000,2.18,NNM
MSFT,20260715,90000000,91000000,30000000,3.00,NNM
AAPL,not-a-date,999,999,999,9.99,NNM
"""
"""Uppercased headers and the compact date form. Matching is case-insensitive, so this parses
too."""

REVISED = CURRENT_SPELLING.replace(b"120000000", b"131400000")
"""The same settlement date, revised upward. Overwriting the first copy destroys the history."""


def _only_aapl(body: bytes) -> ShortInterestRow:
    rows = parse_short_interest(body, symbol="AAPL")
    assert len(rows) == 1, f"expected one AAPL row, got {len(rows)}"
    return rows[0]


def _blob_path(root: Path, body: bytes) -> Path:
    digest = hashlib.sha256(body).hexdigest()
    return root / "blobs" / digest[:2] / digest[2:4] / f"{digest}.gz"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize("body", [CURRENT_SPELLING, OLDER_SPELLING, SHOUTING])
def test_columns_match_case_insensitively_across_the_known_spellings(body: bytes) -> None:
    """One file, three header generations, identical rows.

    A positional read is the tempting implementation and it is correct until FINRA renames a column,
    at which point every field shifts by one and `daysToCover` is read as a market class code — a
    string where a `Decimal` belongs, so the symptom is a dropped value rather than an error.
    """
    row = _only_aapl(body)

    assert row.symbol == "AAPL"
    assert row.settlement_date == SETTLEMENT
    assert row.current_short_position == Decimal("120000000")
    assert row.previous_short_position == Decimal("118500000")
    assert row.average_daily_volume == Decimal("55000000")
    assert row.days_to_cover == Decimal("2.18")
    assert row.market == "NNM"


@pytest.mark.spec
def test_filters_to_one_symbol() -> None:
    """The bulk file covers the whole market, so the filter is what makes it usable for one company.

    Asserted by what it excludes as well as what it keeps: MSFT is in the file and must not be in
    the result, and the unfiltered parse must still return both — otherwise the filter would be
    indistinguishable from a parser that only reads the first row.
    """
    assert _only_aapl(CURRENT_SPELLING).symbol == "AAPL"
    assert {row.symbol for row in parse_short_interest(CURRENT_SPELLING)} == {"AAPL", "MSFT"}


@pytest.mark.spec
def test_a_row_with_an_unparseable_settlement_date_is_dropped_without_aborting() -> None:
    """One bad row in a whole-market file must not stop a run for a company listed correctly.

    The fixture's third row is an AAPL row with `not-a-date`, so the drop is asserted where it is
    visible: filtering to AAPL returns one row, not two, and no exception is raised. Aborting
    instead would make one malformed line look like a FINRA outage.
    """
    all_rows = parse_short_interest(CURRENT_SPELLING)

    assert len(all_rows) == 2
    assert b"not-a-date" in CURRENT_SPELLING, "the fixture must carry the bad row"
    assert all(isinstance(row.settlement_date, date) for row in all_rows)


@pytest.mark.spec
def test_change_is_current_minus_previous() -> None:
    """Asserted as the subtraction rather than as 1,500,000.

    A hard-coded expected number passes under a rule that computes `previous - current` and
    happens to be compared against a fixture where the sign was not checked — and the sign is the
    entire meaning of a short-interest change.
    """
    row = _only_aapl(CURRENT_SPELLING)

    assert row.current_short_position is not None
    assert row.previous_short_position is not None
    assert row.change == row.current_short_position - row.previous_short_position
    assert row.change is not None
    assert row.change > 0


@pytest.mark.spec
def test_every_quantity_is_a_decimal_and_never_a_float() -> None:
    """CLAUDE.md convention 8 on the FINRA path, where the values arrive as CSV text.

    A CSV field is already a string, so the only way to get a `float` here is to write
    `float(text)`. `days_to_cover` is the one that would show it: `2.18` is not exactly
    representable, and a float would compare unequal to `Decimal("2.18")`.
    """
    row = _only_aapl(CURRENT_SPELLING)

    for value in (
        row.current_short_position,
        row.previous_short_position,
        row.average_daily_volume,
        row.days_to_cover,
    ):
        assert isinstance(value, Decimal)
        assert not isinstance(value, float)


@pytest.mark.spec
def test_change_is_none_when_either_side_is_missing() -> None:
    """A missing previous position makes the change unknown, not zero.

    Zero would read as "no change in short interest", which is a statement about the market rather
    than about the file.
    """
    body = b"symbolCode,settlementDate,currentShortPositionQuantity\nAAPL,2026-07-15,120000000\n"
    row = _only_aapl(body)

    assert row.current_short_position == Decimal("120000000")
    assert row.previous_short_position is None
    assert row.change is None


def test_a_file_with_no_header_row_is_exit_4() -> None:
    """An empty body is a failed download, not a market with no short interest."""
    with pytest.raises(UpstreamFetchError) as caught:
        _ = parse_short_interest(b"")
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE


# ---------------------------------------------------------------------------
# Snapshotting — which is the cache never overwriting
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_a_revision_leaves_both_snapshots_retrievable_and_get_returns_the_newer(
    cache: Cache,
    tmp_path: Path,
) -> None:
    """Two fetches of one URL with different bodies, and both survive.

    This is the whole FINRA feature: revisions overwrite upstream, so point-in-time short interest
    exists only because the cache appends. Four assertions, and each fails under a different wrong
    implementation:

    - `get` returns the revised body — an append-only store that served the *first* write would make
      `--refresh` a no-op on the next run.
    - Both blobs are still on disk and still decompress — a store that overwrote the blob would
      leave the older manifest entry dangling, which is the failure `put`'s blob-before-manifest
      ordering exists to make impossible.
    - The manifest has two lines under one key — one line would mean the entry was replaced rather
      than superseded.
    - Parsing the two bodies gives two different short positions, which is why they are kept.
    """
    url = f"{BULK_URL}/shrt{SETTLEMENT.strftime('%Y%m%d')}.csv"
    bodies = [CURRENT_SPELLING, REVISED]
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=bodies.pop(0))

    router = respx.Router()
    _ = router.get(url).mock(side_effect=respond)
    transport = httpx.MockTransport(router.handler)

    client = FinraClient(cache=cache, transport=transport)
    refreshing = FinraClient(cache=cache, refresh=True, transport=transport)
    first, _first_at = client.short_interest(SETTLEMENT)
    second, _second_at = refreshing.short_interest(SETTLEMENT)

    assert len(seen) == 2, "the second fetch must go upstream, not read the first snapshot"
    assert first == CURRENT_SPELLING
    assert second == REVISED

    root = tmp_path / "cache"
    hit = cache.get(Cache.key_for("GET", url, None))
    assert hit is not None
    assert hit[1] == REVISED

    for body in (CURRENT_SPELLING, REVISED):
        blob = _blob_path(root, body)
        assert blob.is_file(), "the superseded snapshot's blob must survive"
        assert gzip.decompress(blob.read_bytes()) == body

    records = [
        json.loads(line)
        for line in (root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 2
    assert len({record["key"] for record in records}) == 1
    assert len({record["content_sha256"] for record in records}) == 2

    assert _only_aapl(first).current_short_position == Decimal("120000000")
    assert _only_aapl(second).current_short_position == Decimal("131400000")


@pytest.mark.spec
def test_a_repeat_fetch_without_refresh_reads_the_snapshot_instead_of_re_downloading(
    cache: Cache,
) -> None:
    """The complement: without `--refresh`, a second fetch is a cache read and makes no request.

    Which is what makes the test above meaningful — the second snapshot exists because `refresh` was
    asked for, not because every call re-downloads.
    """
    url = f"{BULK_URL}/shrt{SETTLEMENT.strftime('%Y%m%d')}.csv"
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=CURRENT_SPELLING)

    router = respx.Router()
    _ = router.get(url).mock(side_effect=respond)
    client = FinraClient(cache=cache, transport=httpx.MockTransport(router.handler))

    body, _at = client.short_interest(SETTLEMENT)
    again, _at_again = client.short_interest(SETTLEMENT)

    assert len(seen) == 1
    assert body == again == CURRENT_SPELLING


def test_a_non_200_is_exit_4(cache: Cache) -> None:
    """FINRA publishes on a schedule, so a missing settlement date is a 404 — and still exit 4.

    Not an absence: this client is only called for a date the caller believes is published, so a
    missing file is an upstream problem rather than a coverage fact.
    """
    url = f"{BULK_URL}/shrt{SETTLEMENT.strftime('%Y%m%d')}.csv"
    router = respx.Router()
    _ = router.get(url).mock(return_value=httpx.Response(404))
    client = FinraClient(cache=cache, transport=httpx.MockTransport(router.handler))

    with pytest.raises(UpstreamFetchError) as caught:
        _ = client.short_interest(SETTLEMENT)
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE

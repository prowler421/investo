"""`frames` -> `FrameRow`: a type that cannot be mistaken for a company fact.

DESIGN.md §4.2 forbids frames for the subject company's history, because frames is not
point-in-time stable — a CY2025Q1 frame can resolve to a 2026 filing. The enforcement is that
`parse_frame` returns a different type from `RawFact`, so appending one to a company series does not
type-check. `tests/fixtures/typing/framerow_as_rawfact.py` is the type-level half; this file is the
runtime half, which is worth having because a future `FrameRow(RawFact)` subclass would satisfy the
type checker and destroy the guarantee.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from investo.domain.models import RawFact
from investo.errors import ExitCode, UpstreamFetchError
from investo.ingest.edgar.frames import FrameRow, frame_period, parse_frame
from tests.conftest import FETCHED_AT, context

APPLE_CIK = 320193
NVIDIA_CIK = 1045810


def _payload(data: list[dict[str, Any]] | None = None) -> bytes:
    """A frames payload in SEC's shape: metadata at the top level, one object per company.

    Built inline rather than committed as a fixture because no frames payload has been fetched — see
    `tests/fixtures/edgar/PROVENANCE.md`. The keys are the ones `docs/m1/04-parsers.md` §4 names:
    `ccp` for the period, `uom` for the unit.
    """
    rows: list[dict[str, Any]] = (
        data
        if data is not None
        else [
            {
                "accn": "0001045810-25-000023",
                "cik": NVIDIA_CIK,
                "entityName": "NVIDIA CORP",
                "loc": "US-CA",
                "start": "2025-01-01",
                "end": "2025-03-31",
                "val": 111000000000.25,
                "fy": 2026,
                "fp": "Q1",
            },
            {
                "accn": "0000320193-25-000079",
                "cik": APPLE_CIK,
                "entityName": "Apple Inc.",
                "loc": "US-CA",
                "end": "2025-03-29",
                "val": 331612000000,
            },
        ]
    )
    return json.dumps(
        {
            "taxonomy": "us-gaap",
            "tag": "Assets",
            "ccp": "CY2025Q1I",
            "uom": "USD",
            "label": "Assets",
            "description": "",
            "pts": len(rows),
            "data": rows,
        }
    ).encode()


# ---------------------------------------------------------------------------
# The type distinction
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_frame_row_is_not_a_raw_fact() -> None:
    """Frames values cannot enter a company series, and the type distinction *is* the enforcement.

    Checked at runtime as well as at the type level because the two catch different mistakes: the
    typing fixture catches a call site that mixes them, and this catches a `FrameRow(RawFact)`
    subclass, which would type-check everywhere and quietly re-open the hole.

    The absent attributes are asserted too. A `FrameRow` carries no `FiscalPeriod`, because a
    frame's period is the frame's (`CY2025Q1`) rather than the fact's, and constructing one would
    produce a value that looks interchangeable with a period derived from a filing's own dates —
    which is exactly what §4.2 says cannot be assumed.
    """
    frame = parse_frame(_payload(), source=context())
    row = frame.rows[0]

    assert isinstance(row, FrameRow)
    assert not isinstance(row, RawFact)
    assert not issubclass(FrameRow, RawFact)
    for absent in ("period", "unit", "source", "taxonomy", "tag"):
        assert not hasattr(row, absent), f"FrameRow must not carry `{absent}`"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_parse_frame_reads_the_data_rows_and_the_frame_metadata() -> None:
    """The rows are the cohort; the metadata is what identifies which frame it is.

    Order is by CIK rather than by payload order, so a peer median computed twice from one cached
    payload is computed over the same sequence — DESIGN.md §11's byte-identical gate reaches this
    far, because M4's cohort listing is printed.
    """
    frame = parse_frame(_payload(), source=context())

    assert frame.taxonomy == "us-gaap"
    assert frame.tag == "Assets"
    assert frame.unit == "USD"
    assert frame.period == "CY2025Q1I"
    assert [row.cik for row in frame.rows] == [APPLE_CIK, NVIDIA_CIK]
    assert frame.by_cik()[APPLE_CIK].entity_name == "Apple Inc."
    assert frame.source.fetched_at == FETCHED_AT


@pytest.mark.spec
def test_frame_values_are_decimal_not_float() -> None:
    """CLAUDE.md convention 8 on the peers path, where the same `parse_float` hook has to be set.

    An integer `val` arrives as `int` and a fractional one as `Decimal`; both must leave as
    `Decimal`, and neither may be a `float` — a peer median over floats is a number nobody can
    reproduce.
    """
    frame = parse_frame(_payload(), source=context())
    by_cik = frame.by_cik()

    assert by_cik[NVIDIA_CIK].value == Decimal("111000000000.25")
    assert by_cik[APPLE_CIK].value == Decimal("331612000000")
    for row in frame.rows:
        assert isinstance(row.value, Decimal)
        assert not isinstance(row.value, float)


@pytest.mark.spec
def test_an_instant_row_has_no_start_and_a_duration_row_does() -> None:
    """`start` is absent on an instantaneous frame, exactly as it is in `companyfacts`.

    Both shapes appear in one payload here so the `.get("start")` read is exercised in both
    directions — a parser using `item["start"]` raises on the instant row only.
    """
    frame = parse_frame(_payload(), source=context())
    by_cik = frame.by_cik()

    assert by_cik[APPLE_CIK].start is None
    assert by_cik[APPLE_CIK].end == date(2025, 3, 29)
    assert by_cik[NVIDIA_CIK].start == date(2025, 1, 1)
    assert by_cik[NVIDIA_CIK].fiscal_year == 2026
    assert by_cik[NVIDIA_CIK].fiscal_period == "Q1"


def test_a_payload_without_data_is_exit_4() -> None:
    """An empty cohort and an unreadable payload are different things, so this one raises."""
    with pytest.raises(UpstreamFetchError) as caught:
        _ = parse_frame(b'{"taxonomy": "us-gaap", "tag": "Assets"}', source=context())
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE


def test_an_unreadable_row_is_dropped_rather_than_fatal() -> None:
    """One bad row in a market-wide cohort is a coverage fact; aborting for it loses the cohort."""
    frame = parse_frame(
        _payload([{"cik": "not-a-cik", "accn": "0000320193-25-000079", "end": "2025-03-29"}]),
        source=context(),
    )
    assert frame.rows == ()


# ---------------------------------------------------------------------------
# frame_period
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize(
    ("year", "quarter", "instant", "expected"),
    [
        (2025, None, False, "CY2025"),
        (2025, 1, False, "CY2025Q1"),
        (2025, 1, True, "CY2025Q1I"),
        (2025, 4, True, "CY2025Q4I"),
    ],
)
def test_frame_period_builds_secs_three_spellings(
    year: int, quarter: int | None, instant: bool, expected: str
) -> None:
    """The spelling is SEC's specification, and getting it wrong is a 404 that looks empty.

    Quarters 1 and 4 are both asserted because they are the boundary of the `1 <= quarter <= 4`
    check, and a test that only probes 1 leaves the upper end open.
    """
    assert frame_period(year=year, quarter=quarter, instant=instant) == expected


@pytest.mark.spec
def test_an_instant_frame_without_a_quarter_raises() -> None:
    """There is no annual instantaneous frame.

    Asking for one and getting `CY2025` back would silently return an annual *duration* frame — a
    different set of numbers, from a URL that returns 200. So it raises rather than falling back.
    """
    with pytest.raises(ValueError, match="needs a quarter"):
        _ = frame_period(year=2025, instant=True)


@pytest.mark.spec
@pytest.mark.parametrize("quarter", [0, 5, -1])
def test_a_quarter_outside_one_to_four_raises(quarter: int) -> None:
    """`CY2025Q0` and `CY2025Q5` are 404s, and a 404 here reads as a cohort with no members."""
    with pytest.raises(ValueError, match="quarter must be 1-4"):
        _ = frame_period(year=2025, quarter=quarter)

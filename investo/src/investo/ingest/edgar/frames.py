"""``frames`` -> cross-company rows for one tag and period. **Peers only** (M1b).

Source: ``https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json``.

Period format, from SEC's documentation: ``CY####`` annual (duration 365 +/- 30 days), ``CY####Q#``
quarterly (91 +/- 30), ``CY####Q#I`` instantaneous. Units with a denominator use ``-per-``:
``USD-per-shares``, where ``companyfacts`` writes ``USD/shares``. Same unit, two spellings, two
places — :func:`~investo.ingest.edgar.client.frames_unit` converts, so the pair has one
implementation and mixing them up is not possible at a call site.

Two restrictions, both from DESIGN.md §4.2, both **enforced rather than documented**:

**Never for the subject company's history.** Frames is not point-in-time stable — a CY2025Q1 frame
can resolve to a 2026 filing. So this module returns :class:`FrameRow`, a *different type* from
:class:`~investo.domain.models.RawFact`, and that type distinction is the enforcement: a frame value
cannot be appended to a company series by accident, because it will not type-check.

**``fetched_at`` matters more here than anywhere else.** M7 recomputes peer medians as-of every
backtest date (§8, leak 3), and frames mutates. The cache entry is the only record of what the
cohort looked like when we asked.

SEC's own frame duration tolerances (335-395 and 61-121 days) are wider than ours (350-380, 80-100)
— see spec question 6.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from investo.domain.provenance import Accession, SourceContext
from investo.errors import UpstreamFetchError
from investo.ingest.edgar._fields import as_cik, as_date, as_optional_int, as_optional_str

__all__ = ["FrameRow", "Frame", "parse_frame", "frame_period"]

_INSTANT_SUFFIX: Final = "I"


@dataclass(frozen=True, slots=True)
class FrameRow:
    """One company's value inside a frame.

    **Deliberately not a** :class:`~investo.domain.models.RawFact`. It carries no
    :class:`~investo.domain.periods.FiscalPeriod` either, because a frame's period is the frame's
    (``CY2025Q1``) rather than the fact's, and constructing a ``FiscalPeriod`` from it would produce
    a value that looks interchangeable with one derived from a filing's own dates. Whether the two
    agree is exactly what §4.2 says cannot be assumed.
    """

    cik: int
    entity_name: str
    accession: Accession
    value: Decimal
    end: date
    start: date | None
    fiscal_year: int | None
    fiscal_period: str | None


@dataclass(frozen=True, slots=True)
class Frame:
    """One frame: the tag, the period, and every company in it."""

    taxonomy: str
    tag: str
    unit: str
    period: str
    rows: tuple[FrameRow, ...]
    source: SourceContext

    def by_cik(self) -> dict[int, FrameRow]:
        """``cik -> row``. Later rows win; SEC does not repeat a CIK within a frame, and if it ever
        does, the last one is the one its own selection would have kept."""
        return {row.cik: row for row in self.rows}


def frame_period(*, year: int, quarter: int | None = None, instant: bool = False) -> str:
    """Build a frame period string: ``"CY2025"``, ``"CY2025Q1"``, ``"CY2025Q1I"``.

    A function rather than an f-string at call sites, for the same reason the URL builders are
    functions: the spelling is SEC's specification and getting it wrong is a 404 that looks like an
    empty cohort.

    Raises:
        ValueError: if ``instant`` is requested without a quarter — there is no annual instant
            frame, and asking for one silently returns an annual duration frame instead.
    """
    if quarter is None:
        if instant:
            raise ValueError("An instantaneous frame needs a quarter: CY####Q#I.")
        return f"CY{year}"
    if not 1 <= quarter <= 4:
        raise ValueError(f"quarter must be 1-4, got {quarter}")
    return f"CY{year}Q{quarter}{_INSTANT_SUFFIX if instant else ''}"


def parse_frame(body: bytes, *, source: SourceContext) -> Frame:
    """Parse a frame payload.

    Raises:
        UpstreamFetchError: if the payload is not an object or lacks ``data``. Exit 4.
    """
    try:
        payload: Any = json.loads(body, parse_float=Decimal, parse_int=int)
    except json.JSONDecodeError as exc:
        raise UpstreamFetchError(f"frames payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpstreamFetchError("frames payload must be a JSON object.")
    data = payload.get("data")
    if not isinstance(data, list):
        raise UpstreamFetchError("frames payload has no `data` list.")

    rows: list[FrameRow] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        row = _to_row(item)
        if row is not None:
            rows.append(row)

    return Frame(
        taxonomy=str(payload.get("taxonomy") or ""),
        tag=str(payload.get("tag") or ""),
        unit=str(payload.get("uom") or ""),
        period=str(payload.get("ccp") or ""),
        rows=tuple(sorted(rows, key=lambda r: r.cik)),
        source=source,
    )


def _to_row(item: dict[str, Any]) -> FrameRow | None:
    try:
        cik = as_cik(item.get("cik"))
        end = as_date(item.get("end"))
        accession = Accession.parse(str(item["accn"]))
    except (KeyError, ValueError):
        return None
    if end is None:
        return None
    value = item.get("val")
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int) and not isinstance(value, bool):
        amount = Decimal(value)
    else:
        return None
    try:
        start = as_date(item.get("start"))
    except (ValueError, InvalidOperation):
        start = None
    return FrameRow(
        cik=cik,
        entity_name=str(item.get("entityName") or ""),
        accession=accession,
        value=amount,
        end=end,
        start=start,
        fiscal_year=as_optional_int(item.get("fy")),
        fiscal_period=as_optional_str(item.get("fp")),
    )

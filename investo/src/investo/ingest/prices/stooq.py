"""Stooq — the free cross-check (DESIGN.md §4.3).

``https://stooq.com/q/d/l/?s={ticker}.us&d1=...&d2=...&i=d``. CSV, no key, undocumented quota.

**No adjusted close.** ``adj_close=None`` and ``adjusted=False``, never ``close`` aliased into the
field — see :class:`~investo.ingest.prices.base.PriceBar` for the argument at length. The short
version: an aliased adjusted close feeds a beta estimated on unadjusted prices, every dividend and
split in the window reads as a real return, and nothing in the report would say so because the field
was populated.

Its role is a cross-check: two providers disagreeing on a close by more than a tolerance is a
data-integrity signal, and a third free opinion is worth an adapter. **The comparison itself is not
M1's** — M1 makes it possible by giving all three adapters the same schema. Where the check lives
(a data-integrity flag under §6.4) is M4's call.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from investo.config import Settings
from investo.errors import UpstreamFetchError
from investo.ingest.prices.base import (
    PriceBar,
    PriceHttp,
    PriceSeries,
    price_source_ref,
    require_ascending,
)

__all__ = ["StooqProvider"]

_URL: Final = "https://stooq.com/q/d/l/"
_EXPECTED_HEADER: Final = ("Date", "Open", "High", "Low", "Close")


class StooqProvider:
    """Daily bars from Stooq's CSV download. No key, no adjusted close."""

    name = "stooq"

    def __init__(self, settings: Settings, *, http: PriceHttp) -> None:
        del settings  # no key and no per-provider setting; accepted for a uniform constructor
        self._http = http

    def daily(self, ticker: str, *, start: date, end: date) -> PriceSeries:
        params = {
            "s": f"{ticker.lower()}.us",
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
            "i": "d",
        }
        body, fetched_at = self._http.get(_URL, params=params)
        bars = _parse_csv(body, ticker=ticker)
        ordered = require_ascending(bars, provider=self.name, ticker=ticker)
        return PriceSeries(
            ticker=ticker.upper(),
            provider=self.name,
            bars=ordered,
            # Always False, and asserted by the constructor rather than assumed: every bar has
            # adj_close=None, so the biconditional holds only for False.
            adjusted=False,
            fetched_at=fetched_at,
            source=price_source_ref(
                provider=self.name,
                url=_URL,
                day=ordered[-1].day if ordered else end,
                fetched_at=fetched_at,
            ),
        )


def _parse_csv(body: bytes, *, ticker: str) -> list[PriceBar]:
    """Parse Stooq's CSV.

    Stooq answers an unknown symbol with a plain-text body rather than a 404, so the header is
    validated. Without that check the caller receives an empty series and reads it as "no price
    history" — an absence — when the truth is a rejected request.

    Raises:
        UpstreamFetchError: if the body is not the expected CSV. Exit 4.
    """
    text = body.decode("utf-8", errors="replace").strip()
    reader = csv.reader(io.StringIO(text))
    try:
        header = tuple(field.strip() for field in next(reader))
    except StopIteration:
        raise UpstreamFetchError(f"Stooq returned an empty body for {ticker}.") from None
    if header[: len(_EXPECTED_HEADER)] != _EXPECTED_HEADER:
        raise UpstreamFetchError(
            f"Stooq did not return price CSV for {ticker}; first line was {text.splitlines()[0]!r}.",
            hint=(
                "Stooq answers an unknown symbol with plain text and HTTP 200, so this is most "
                "likely a symbol it does not carry rather than an outage."
            ),
        )

    index = {name: position for position, name in enumerate(header)}
    bars: list[PriceBar] = []
    for row in reader:
        if len(row) < len(_EXPECTED_HEADER):
            continue
        day = _to_date(row[index["Date"]])
        close = _to_decimal(row[index["Close"]])
        if day is None or close is None:
            continue
        # `is not None`, not a truth test: `Volume` could legitimately be column 0 in a future
        # column order, and `if volume_column` would then silently drop every volume.
        volume_column = index.get("Volume")
        raw_volume = (
            row[volume_column] if volume_column is not None and volume_column < len(row) else ""
        )
        bars.append(
            PriceBar(
                day=day,
                close=close,
                # Not `close`. See the module docstring.
                adj_close=None,
                open=_to_decimal(row[index["Open"]]),
                high=_to_decimal(row[index["High"]]),
                low=_to_decimal(row[index["Low"]]),
                volume=_to_int(raw_volume),
            )
        )
    return bars


def _to_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _to_decimal(value: str) -> Decimal | None:
    """``Decimal(text)`` straight from the CSV field — never ``float(text)``.

    CLAUDE.md convention 8. A CSV field is already a string, so there is no parse hook to arrange:
    the only way to get a ``float`` here is to write one, and this is the function that does not.
    """
    text = value.strip()
    if not text or text == "-":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _to_int(value: str) -> int | None:
    text = value.strip()
    return int(text) if text.isdigit() else None

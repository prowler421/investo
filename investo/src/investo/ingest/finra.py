"""FINRA short interest, with snapshotting (M1b).

Dataset: ``equityShortInterestStandardized``. The older ``equityShortInterest`` stopped publishing
2021-04-30 and is **not** used.

**Use the bulk file downloads, which are auth-free.** DESIGN.md §6.8: the FINRA Query API requires
credentials and an OAuth2 ``client_credentials`` bearer token; the downloads need neither. Adding a
credential requirement for data available without one would put an ``INVESTO_FINRA_*`` variable in
config for nothing.

**Snapshotting is the whole feature.** §6.8: *revisions overwrite rather than append — you must
snapshot to build point-in-time history.* And nothing special is needed to do it: each fetch of the
same settlement date writes a new manifest entry with its own ``fetched_at``, and a revision produces
a different ``content_sha256`` and therefore a new blob. That only works because the cache never
overwrites, which is the load-bearing property from ``docs/m1/02-cache.md`` — so the test for this
module is a cache test in disguise: two fetches of one URL returning different bodies leave both
retrievable, and ``get`` returns the newer.

Not to be confused with FINRA's daily short *volume* files, which measure something else. And per
§6.8, **do not plan on SEC Form SHO** — Rule 13f-2 was remanded and the compliance date is
2028-01-02, so FINRA is the only free source and will remain so.

**Its own HTTP client, and that is not a loophole.** Reusing
:class:`~investo.ingest.edgar.client.EdgarClient` would send SEC's declared contact ``User-Agent`` to
FINRA — at best meaningless, at worst a misrepresentation of who is calling — and would put FINRA's
traffic through SEC's token bucket, so a FINRA fetch would slow EDGAR requests to protect a limit
that does not apply to it. It shares the :class:`~investo.ingest.cache.Cache`, which is host-agnostic
by design.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

import httpx

from investo.errors import UpstreamFetchError
from investo.ingest.cache import Cache

__all__ = [
    "DATASET",
    "BULK_URL",
    "ShortInterestRow",
    "FinraClient",
    "parse_short_interest",
]

DATASET: Final = "equityShortInterestStandardized"
BULK_URL: Final = "https://cdn.finra.org/equity/regsho/monthly"
"""The auth-free bulk download root. See the module docstring on why not the Query API."""

_USER_AGENT: Final = "investo"
"""Deliberately *not* SEC's declared contact address. FINRA has no such requirement, and sending
SEC's contact to a third party would misstate who is calling."""


@dataclass(frozen=True, slots=True)
class ShortInterestRow:
    """One symbol's short interest at one settlement date.

    ``days_to_cover`` is carried as filed rather than recomputed from
    ``current_short_position / average_daily_volume``. FINRA publishes its own value, and a
    recomputed one would disagree in the third decimal for rounding reasons and look like a data
    error.
    """

    symbol: str
    settlement_date: date
    current_short_position: Decimal | None
    previous_short_position: Decimal | None
    average_daily_volume: Decimal | None
    days_to_cover: Decimal | None
    market: str | None

    @property
    def change(self) -> Decimal | None:
        if self.current_short_position is None or self.previous_short_position is None:
            return None
        return self.current_short_position - self.previous_short_position


class FinraClient:
    """A minimal cached fetcher for FINRA's bulk files."""

    def __init__(
        self,
        *,
        cache: Cache,
        refresh: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._cache = cache
        self._refresh = refresh
        self._client = httpx.Client(
            transport=transport,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    def close(self) -> None:
        self._client.close()

    def short_interest(self, settlement: date) -> tuple[bytes, datetime]:
        """Fetch one settlement date's file. Returns ``(body, fetched_at)``.

        Every fetch is a snapshot: a revision to an already-fetched date produces a new blob and a
        new manifest line, and both remain retrievable. That is the point of the module.

        Raises:
            UpstreamFetchError: on a non-200 or a transport failure. Exit 4.
        """
        url = f"{BULK_URL}/shrt{settlement.strftime('%Y%m%d')}.csv"
        key = Cache.key_for("GET", url, None)
        if not self._refresh:
            hit = self._cache.get(key)
            if hit is not None:
                entry, body = hit
                return body, entry.fetched_at
        try:
            response = self._client.get(url)
        except httpx.TransportError as exc:
            raise UpstreamFetchError(f"FINRA {url} could not be reached: {exc}") from exc
        if response.status_code != 200:
            raise UpstreamFetchError(f"FINRA {url} returned HTTP {response.status_code}.")
        entry = self._cache.put(
            key=key,
            url=url,
            method="GET",
            params={},
            status=200,
            headers=dict(response.headers),
            body=response.content,
        )
        return response.content, entry.fetched_at


def parse_short_interest(body: bytes, *, symbol: str | None = None) -> tuple[ShortInterestRow, ...]:
    """Parse a FINRA short-interest bulk file, optionally filtered to one symbol.

    Column names are matched case-insensitively and by a small set of known spellings, because FINRA
    has renamed columns between file generations and a positional read would silently shift every
    field by one. A row whose settlement date will not parse is dropped and does not abort — the file
    covers the whole market, and one bad row should not stop a run.
    """
    text = body.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise UpstreamFetchError("FINRA short-interest file has no header row.")

    index = {name.strip().lower().replace(" ", ""): name for name in reader.fieldnames}
    wanted = symbol.strip().upper() if symbol else None

    rows: list[ShortInterestRow] = []
    for record in reader:
        found = _pick(record, index, "symbolcode", "symbol", "issuesymbolidentifier")
        if found is None:
            continue
        ticker = found.strip().upper()
        if wanted is not None and ticker != wanted:
            continue
        settlement = _date(
            _pick(record, index, "settlementdate", "settlementdt", "settlement_date")
        )
        if settlement is None:
            continue
        rows.append(
            ShortInterestRow(
                symbol=ticker,
                settlement_date=settlement,
                current_short_position=_decimal(
                    _pick(record, index, "currentshortpositionquantity", "currentshortposition")
                ),
                previous_short_position=_decimal(
                    _pick(record, index, "previousshortpositionquantity", "previousshortposition")
                ),
                average_daily_volume=_decimal(
                    _pick(record, index, "averagedailyvolumequantity", "averagedailyvolume")
                ),
                days_to_cover=_decimal(_pick(record, index, "daystocoverquantity", "daystocover")),
                market=_pick(record, index, "marketclasscode", "market"),
            )
        )
    return tuple(sorted(rows, key=lambda r: (r.settlement_date, r.symbol)))


def _pick(record: dict[str, str | None], index: dict[str, str], *names: str) -> str | None:
    for name in names:
        column = index.get(name)
        if column is not None:
            value = record.get(column)
            if value is not None and value.strip():
                return value.strip()
    return None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    return None


def _decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    text = value.strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None

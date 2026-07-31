"""The price provider protocol, and the one field that carries the whole design argument.

``Decimal``, not ``float``, throughout — CLAUDE.md convention 8, and prices are money. Every
adapter parses its response with ``parse_float=Decimal`` (JSON) or ``Decimal(text)`` (CSV), never
via ``float()``.

The adapters do **not** reuse :class:`~investo.ingest.edgar.client.EdgarClient`, for the reason
``docs/m1/03-edgar-client.md`` §7 gives about FINRA and which applies identically here: it would
send SEC's declared contact ``User-Agent`` to a third party, and it would put price traffic through
SEC's token bucket, slowing EDGAR requests to protect a limit that does not apply. They share the
:class:`~investo.ingest.cache.Cache`, which is host-agnostic by design.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx

from investo.config import Settings
from investo.domain.provenance import Accession, SourceRef
from investo.errors import ConfigError, UpstreamFetchError
from investo.ingest.cache import Cache

__all__ = [
    "PRICE_ACCESSION",
    "PriceBar",
    "PriceSeries",
    "PriceProvider",
    "PriceHttp",
    "provider_for",
    "price_at_or_before",
    "price_source_ref",
    "weekdays_between",
    "require_ascending",
]

PRICE_ACCESSION = Accession("0000000000-00-000000")
"""A sentinel accession for a price series, which has no filing behind it.

:class:`~investo.domain.provenance.SourceRef` requires one, because every *filing*-derived figure
has one and making the field optional would weaken it for the 99% of refs that do. A price is the
exception, and it gets a value that is obviously not a real accession rather than an empty string
that could be mistaken for a parse failure. The ref's ``url`` and ``taxonomy=None`` are what
identify it as a non-XBRL source.
"""


@dataclass(frozen=True, slots=True)
class PriceBar:
    """One trading day.

    ``adj_close`` is ``Optional`` **on purpose**, and this is the interesting part of the protocol.

    ROADMAP M1's exit criterion is *"all three adapters returning identical schemas."* Stooq
    supplies no adjusted close (§4.3), so there are two ways to satisfy it:

    1. Alias ``close`` into ``adj_close``. Every adapter then returns a fully populated struct and a
       naive schema check passes.
    2. Return ``None`` and set ``PriceSeries.adjusted = False``.

    The first is wrong, and it is worth saying why at length because it is the tempting one. An
    aliased ``adj_close`` feeds a beta estimated over 5 years of weekly returns (§5.4) on
    *unadjusted* prices, so every dividend and split in the window reads as a real return. Beta is
    then wrong, WACC is wrong, and fair value moves by double digits — §5.4 says as much about
    unshrunk beta alone. And nothing in the report would say so, because the field was populated.

    So: ``None``, ``adjusted=False``, and a caller that needs adjusted prices raises rather than
    computes. The violation test asserts ``bar.adj_close is None``; it deliberately does **not**
    assert ``adj_close != close``, which is the lazy spelling and passes whether the field is
    ``None`` or genuinely different — so it would go green against the aliasing bug it exists to
    catch.
    """

    day: date
    close: Decimal
    adj_close: Decimal | None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: int | None = None


@dataclass(frozen=True, slots=True)
class PriceSeries:
    """A daily series from one provider, ascending by day.

    ``adjusted`` is ``True`` if and only if every bar carries a non-``None`` ``adj_close``, and the
    constructor enforces the biconditional rather than trusting the adapter. A series with one
    unadjusted bar among five hundred is not adjusted, and calling it adjusted is how a gap becomes
    invisible.
    """

    ticker: str
    provider: str
    bars: tuple[PriceBar, ...]
    adjusted: bool
    fetched_at: datetime
    source: SourceRef

    def __post_init__(self) -> None:
        if self.adjusted != all(bar.adj_close is not None for bar in self.bars):
            raise ValueError(
                f"PriceSeries({self.ticker}, {self.provider}): `adjusted` must be True if and "
                "only if every bar has an adj_close. A partially adjusted series is a beta "
                "estimate nobody can trust (DESIGN.md §5.4)."
            )


class PriceProvider(Protocol):
    """What every adapter implements. Three of them, one schema."""

    name: str

    def daily(self, ticker: str, *, start: date, end: date) -> PriceSeries: ...


class PriceHttp:
    """A small cached HTTP fetcher for price hosts.

    Shared by the adapters so the cache key, the header handling and the error translation have one
    implementation. Deliberately much simpler than
    :class:`~investo.ingest.edgar.client.EdgarClient`: no token bucket, because none of these hosts
    publishes a per-second cap and DESIGN.md §4.3's quotas are per-day. Tiingo's own 429 is
    surfaced rather than pre-empted — see ``tiingo.py``.
    """

    def __init__(
        self,
        *,
        cache: Cache | None = None,
        refresh: bool = False,
        transport: httpx.BaseTransport | None = None,
        user_agent: str = "investo",
    ) -> None:
        self._cache = cache
        self._refresh = refresh
        self._request_count = 0
        self._served_from_cache = True
        self._client = httpx.Client(
            transport=transport,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    def close(self) -> None:
        self._client.close()

    @property
    def request_count(self) -> int:
        """Network requests only; cache hits excluded. Mirrors ``EdgarClient.request_count``."""
        return self._request_count

    @property
    def served_from_cache(self) -> bool:
        """Whether every fetch so far was a cache hit.

        Exists because the fetch summary prints a per-source status, and without this it printed
        ``prices (tiingo) fetched`` on a warm run where no request was made — which contradicts the
        one property that section of the summary is *for*: a stale entry the user cannot see is a
        stale entry they will trust. ``EdgarClient`` reports the same thing per-response via
        ``Response.from_cache``; a provider returns a whole ``PriceSeries`` rather than a response,
        so it is reported per-client instead.

        ``True`` before any fetch, so a provider that made no request at all is not reported as
        having fetched something.
        """
        return self._served_from_cache

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[bytes, datetime]:
        """Fetch, caching on 200. Returns ``(body, fetched_at)``.

        ``headers`` is for authentication and is **not** part of the cache key, which is exactly
        the point of sending Tiingo's token in a header: rotating a key must not invalidate a cache,
        and a key must never land in the manifest's ``url`` field.

        Raises:
            UpstreamFetchError: on any non-200. Exit 4.
        """
        key = Cache.key_for("GET", url, params)
        if self._cache is not None and not self._refresh:
            hit = self._cache.get(key)
            if hit is not None:
                entry, body = hit
                return body, entry.fetched_at
        self._served_from_cache = False

        self._request_count += 1
        try:
            response = self._client.get(
                url, params=dict(params or {}) or None, headers=dict(headers or {})
            )
        except httpx.TransportError as exc:
            raise UpstreamFetchError(f"{url} could not be reached: {exc}") from exc

        if response.status_code != 200:
            raise UpstreamFetchError(
                f"{url} returned HTTP {response.status_code}.",
                hint=(
                    "Provider quota, most likely — DESIGN.md §4.3 records the free-tier limits. "
                    "Switch `price_provider` or wait."
                    if response.status_code == 429
                    else None
                ),
            )

        body = response.content
        if self._cache is not None:
            entry = self._cache.put(
                key=key,
                url=url,
                method="GET",
                params=params or {},
                status=200,
                headers=dict(response.headers),
                body=body,
            )
            return body, entry.fetched_at
        return body, datetime.now(UTC)

    @staticmethod
    def json(body: bytes) -> Any:
        """Decode with ``parse_float=Decimal`` so no price is ever a ``float``."""
        return json.loads(body, parse_float=Decimal, parse_int=int)


def provider_for(
    settings: Settings,
    *,
    cache: Cache | None = None,
    refresh: bool = False,
    transport: httpx.BaseTransport | None = None,
    http: PriceHttp | None = None,
) -> PriceProvider:
    """Resolve ``settings.price_provider`` to an adapter.

    ``http`` lets a caller supply the fetcher it wants to inspect afterwards — ``fetch`` needs
    :attr:`PriceHttp.served_from_cache` to print an honest per-source status, and it cannot read it
    off a fetcher this function created and dropped. Note that ``yfinance`` ignores it: that adapter
    owns its own HTTP and is not covered by the cache at all, which is one more reason DESIGN.md §4.3
    calls it dev convenience only.

    ``Settings.price_provider`` is already a ``Literal["tiingo", "yfinance", "stooq"]`` (M0), so an
    unknown value is a pydantic validation error -> ``ConfigError`` -> exit 5, with no registry
    lookup needed and no string dispatch to get wrong.

    A ``match`` over the closed set rather than a dict-based registry, because basedpyright checks
    exhaustiveness on the former: a registry would accept a fourth name that the type does not,
    which is how config validation ends up in two places.

    ROADMAP's "swappable via config" is tested by resolving each of the three literal values and
    asserting ``provider.name`` matches — **including via the config file, not only the env**, since
    a setting that only works from the environment is half a feature.
    """
    if http is None:
        http = PriceHttp(cache=cache, refresh=refresh, transport=transport)
    match settings.price_provider:
        case "tiingo":
            from investo.ingest.prices.tiingo import TiingoProvider

            return TiingoProvider(settings, http=http)
        case "yfinance":
            from investo.ingest.prices.yfinance_ import YFinanceProvider

            return YFinanceProvider(settings)
        case "stooq":
            from investo.ingest.prices.stooq import StooqProvider

            return StooqProvider(settings, http=http)


def price_source_ref(*, provider: str, url: str, day: date, fetched_at: datetime) -> SourceRef:
    """Provenance for a price.

    ``form`` records the provider so the appendix line reads as a price fetch rather than a filing.
    A market cap in the appendix therefore names both a filing and a price fetch, which is the case
    spec question 2 exists to represent.
    """
    return SourceRef(
        accession=PRICE_ACCESSION,
        taxonomy=None,
        tag=None,
        form=f"price:{provider}",
        filed=day,
        url=url,
        fetched_at=fetched_at,
    )


def price_at_or_before(series: PriceSeries, as_of: date) -> PriceBar | None:
    """The last bar at or before ``as_of``, or ``None`` if the series starts later.

    **Not the last bar in the series.** With ``--as-of`` set, using the newest available price
    would be a lookahead leak in the one number that gets compared against modelled value — and the
    leak would be invisible, because a price is a price. Its violation test feeds a series with bars
    *after* ``as_of`` and asserts the result does not change.

    ``None`` rather than an exception: a ticker that resolves in the exchange file but has no price
    history at or before the date is an absence — "no price history from {provider}" — and DESIGN.md
    §8 wants that wording specifically, because conflating it with a fetch failure hides the pattern
    that reveals survivorship bias.
    """
    eligible = [bar for bar in series.bars if bar.day <= as_of]
    return max(eligible, key=lambda bar: bar.day) if eligible else None


def weekdays_between(start: date, end: date) -> int:
    """Weekdays in ``[start, end]`` inclusive.

    A weekday count rather than a market calendar, deliberately: the only consumer is yfinance's
    partial-history check, which needs about 10% accuracy, and a calendar package would be a
    dependency added to catch a rounding error. Stated in ``docs/m1/README.md`` § Dependencies as
    one of three deliberate non-additions.
    """
    if end < start:
        return 0
    total_days = (end - start).days + 1
    whole_weeks, remainder = divmod(total_days, 7)
    count = whole_weeks * 5
    weekday = start.weekday()
    for offset in range(remainder):
        if (weekday + offset) % 7 < 5:
            count += 1
    return count


def require_ascending(
    bars: Sequence[PriceBar], *, provider: str, ticker: str
) -> tuple[PriceBar, ...]:
    """Sort ascending and reject duplicate days.

    Sorting rather than asserting order, because providers differ. Duplicates *are* rejected rather
    than deduped: two bars for one day means two different closes for one day, and picking one
    silently is the kind of choice that should be made by someone who knows why it happened.
    """
    ordered = sorted(bars, key=lambda bar: bar.day)
    days = [bar.day for bar in ordered]
    if len(set(days)) != len(days):
        counts = Counter(days)
        duplicated = sorted(day for day, count in counts.items() if count > 1)
        raise ConfigError(
            f"{provider} returned duplicate bars for {ticker} on "
            f"{', '.join(day.isoformat() for day in duplicated)}.",
            hint="Two closes for one day. Re-run with --refresh; if it persists, switch provider.",
        )
    return tuple(ordered)

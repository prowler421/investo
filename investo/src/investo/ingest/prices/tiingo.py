"""Tiingo — the default price provider (DESIGN.md §4.3).

Endpoint: ``https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate=...&endDate=...``.
Supplies ``adjClose``, so ``adjusted=True``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from investo.config import Settings
from investo.errors import ConfigError, UpstreamFetchError
from investo.ingest.prices.base import (
    PriceBar,
    PriceHttp,
    PriceSeries,
    price_source_ref,
    require_ascending,
)

__all__ = ["TiingoProvider"]

_BASE: Final = "https://api.tiingo.com/tiingo/daily"


class TiingoProvider:
    """Daily bars from Tiingo.

    **Auth by header, not query parameter.** ``Authorization: Token <key>`` rather than
    ``?token=<key>``, so the key does not land in the cache key, the manifest's ``url`` field, or a
    log line. That is not a detail: DESIGN.md §10 says API keys go via env only, never committed,
    never logged — and a cache manifest is a file on disk.

    **Rate limits are read from Tiingo's own 429, not hardcoded.** §4.3 records the free tier as
    1,000 req/day, 50/hr, 500 unique symbols/month and 1 GB bandwidth, but a search while the design
    was written returned different figures and none from Tiingo's current documentation — so this
    adapter asserts no number and surfaces Tiingo's own 429 as
    :class:`~investo.errors.UpstreamFetchError` instead. Recorded as spec question 7: confirm
    against Tiingo's docs when the key is issued, and add a client-side limiter then if it is worth
    one.
    """

    name = "tiingo"

    def __init__(self, settings: Settings, *, http: PriceHttp) -> None:
        """
        Raises:
            ConfigError: if ``INVESTO_TIINGO_KEY`` is unset. **Raised here, before any request** —
                the same shape as the User-Agent rule: a config problem detected at startup rather
                than after a fetch has begun. Exit 5.

                Note the consequence, which ``docs/m1/README.md`` records as spec question 11:
                ``price_provider`` defaults to ``tiingo``, so ``investo fetch AAPL`` needs a Tiingo
                account. Implemented as designed rather than resolved in code.
        """
        if not settings.tiingo_key:
            raise ConfigError(
                "price_provider is 'tiingo' but no API key is configured.",
                hint=(
                    "Set INVESTO_TIINGO_KEY (free tier at tiingo.com), or choose another "
                    "provider:\n"
                    "  export INVESTO_PRICE_PROVIDER=stooq    # no key required\n"
                    "See .env.example and DESIGN.md §4.3."
                ),
            )
        self._key = settings.tiingo_key
        self._http = http

    def daily(self, ticker: str, *, start: date, end: date) -> PriceSeries:
        url = f"{_BASE}/{ticker.lower()}/prices"
        params = {"startDate": start.isoformat(), "endDate": end.isoformat()}
        body, fetched_at = self._http.get(
            url, params=params, headers={"Authorization": f"Token {self._key}"}
        )
        payload: Any = PriceHttp.json(body)
        if not isinstance(payload, list):
            raise UpstreamFetchError(
                f"Tiingo returned {type(payload).__name__} for {ticker}, expected a list of bars."
            )

        bars: list[PriceBar] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            bar = _to_bar(row)
            if bar is not None:
                bars.append(bar)

        ordered = require_ascending(bars, provider=self.name, ticker=ticker)
        return PriceSeries(
            ticker=ticker.upper(),
            provider=self.name,
            bars=ordered,
            adjusted=all(bar.adj_close is not None for bar in ordered),
            fetched_at=fetched_at,
            source=price_source_ref(
                provider=self.name,
                url=url,
                day=ordered[-1].day if ordered else end,
                fetched_at=fetched_at,
            ),
        )


def _to_bar(row: dict[str, Any]) -> PriceBar | None:
    """One Tiingo row, or ``None`` if it is unusable.

    ``adjClose`` is read into ``adj_close`` and ``close`` into ``close``, separately. Tiingo
    supplies both, so nothing has to be aliased — which is the case Stooq cannot match and the
    reason ``adj_close`` is ``Optional`` in the protocol.
    """
    day = _to_date(row.get("date"))
    close = _to_decimal(row.get("close"))
    if day is None or close is None:
        return None
    return PriceBar(
        day=day,
        close=close,
        adj_close=_to_decimal(row.get("adjClose")),
        open=_to_decimal(row.get("open")),
        high=_to_decimal(row.get("high")),
        low=_to_decimal(row.get("low")),
        volume=int(row["volume"]) if isinstance(row.get("volume"), int) else None,
    )


def _to_date(value: object) -> date | None:
    """Tiingo writes a full timestamp (``"2026-07-30T00:00:00.000Z"``); only the day matters."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _to_decimal(value: object) -> Decimal | None:
    """``Decimal`` or ``None``. A ``float`` is **rejected, not converted**.

    ``PriceHttp.json`` supplies ``parse_float=Decimal``, so a ``float`` here means the hook was
    removed. Converting it would launder the precision loss the hook exists to prevent, and the
    contract test's ``not isinstance(x, float)`` assertion would still pass.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str) and value.strip():
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return None
    return None

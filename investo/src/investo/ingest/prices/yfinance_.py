"""yfinance — development convenience only (DESIGN.md §4.3).

Module name carries the trailing underscore per DESIGN.md §3.1, so it does not shadow the package
it imports.

**An optional extra, not a dependency.** §4.3 calls yfinance "dev convenience only", notes its own
README describes it as intended for personal use, and concludes it is *"not a defensible base for
anything shared or commercial."* A default dependency contradicts all three: it puts a scraper and
its TLS-impersonation chain into every install, including one made by someone who set
``price_provider = "tiingo"`` and will never import it.

So the adapter ships and the dependency does not::

    uv sync --extra yfinance

The import is lazy, inside :meth:`YFinanceProvider.daily`, and a missing package is a
:class:`~investo.errors.ConfigError` naming the extra. Recorded as spec question 8.

Version range ``>=1.4,<2``: ``curl_cffi`` became optional with a ``requests`` fallback at 1.4.0,
1.2.1 forced ``curl_cffi>=0.15`` for a CVE, and 1.5.2 fixed breakage against ``curl_cffi>=0.16``. A
floor below 1.4 inherits a hard ``curl_cffi`` requirement; no ceiling invites a major bump on a
library whose upstream has no contract.
"""

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
#
# `yfinance` is an optional extra (see `pyproject.toml`), so on a default install the import does not
# resolve at all — which is the *designed* behaviour: the adapter ships, the dependency does not, and
# the import is lazy with a `ConfigError` naming the extra. A type checker cannot distinguish that
# from a missing dependency, and CI installs without the extra on purpose.
#
# `reportAttributeAccessIssue` for the same root cause one step later: with no stubs, a pandas frame
# read out of `yf.download` is `object`, so `.date` on a value taken from it cannot be resolved.

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from investo.config import Settings
from investo.errors import ConfigError, UpstreamFetchError
from investo.ingest.prices.base import (
    PriceBar,
    PriceSeries,
    price_source_ref,
    require_ascending,
    weekdays_between,
)

__all__ = ["YFinanceProvider", "PARTIAL_HISTORY_FLOOR"]

PARTIAL_HISTORY_FLOOR: Final = 0.9
"""Fraction of the expected weekday count below which a series is treated as truncated.

Accommodates roughly nine market holidays a year plus a few. The check needs about 10% accuracy,
which is why a weekday count is enough and a market-calendar dependency is not warranted.
"""

_URL: Final = "https://finance.yahoo.com"


class YFinanceProvider:
    """Daily bars via the ``yfinance`` package.

    Three of §4.3's caveats translate into concrete behaviour here; the first is the important one.

    **Partial history that looks complete is the dominant failure mode.** Throttling sometimes
    returns a short series with HTTP 200, so the row count is validated rather than trusted. The
    error message names the ambiguity, because a genuinely recent IPO also returns few bars and the
    user is the one who can tell which it is.

    **``auto_adjust`` is pinned explicitly, not left to the library default.** §4.3: ``auto_adjust``
    back-adjusts, so historical prices *change* when a dividend is paid and two pulls on different
    dates legitimately disagree. This adapter requests raw and adjusted separately and populates
    both ``close`` and ``adj_close``, so the report can state which it used. The cache is what
    guarantees the numbers do not move under us — which only holds because §4.4's store never
    overwrites.

    **No shared cache.** yfinance owns its own HTTP, so :class:`~investo.ingest.cache.Cache` cannot
    see the request and this adapter cannot make it. That is a real gap and it is stated rather than
    hidden: a yfinance series is *not* covered by the append-only record that makes §4.4's
    reproducibility claim true, which is one more reason §4.3 calls it dev convenience only.
    """

    name = "yfinance"

    def __init__(self, settings: Settings) -> None:
        del settings  # no key, no per-provider setting
        self._floor = PARTIAL_HISTORY_FLOOR

    def daily(self, ticker: str, *, start: date, end: date) -> PriceSeries:
        yf = _import_yfinance()
        fetched_at = datetime.now(UTC)

        # `end` is exclusive in yfinance's API, so add a day to include the requested last bar.
        # Getting this wrong silently drops the most recent close, which is the one market cap uses.
        raw: Any = yf.download(
            ticker,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=False,
        )
        bars = _to_bars(raw, ticker=ticker)

        expected = weekdays_between(start, end)
        if expected and len(bars) < self._floor * expected:
            raise UpstreamFetchError(
                f"yfinance returned {len(bars)} bars for a window with ~{expected} weekdays; "
                "this is the partial-history symptom of Yahoo throttling, not necessarily a short "
                "listing history.",
                hint=(
                    "If this ticker genuinely listed recently, the short series is correct and the "
                    "check is wrong — use `price_provider = 'tiingo'` for it. Otherwise wait and "
                    "retry."
                ),
            )

        ordered = require_ascending(bars, provider=self.name, ticker=ticker)
        return PriceSeries(
            ticker=ticker.upper(),
            provider=self.name,
            bars=ordered,
            adjusted=all(bar.adj_close is not None for bar in ordered),
            fetched_at=fetched_at,
            source=price_source_ref(
                provider=self.name,
                url=_URL,
                day=ordered[-1].day if ordered else end,
                fetched_at=fetched_at,
            ),
        )


def _import_yfinance() -> Any:
    """Import lazily, and name the extra if it is not installed."""
    try:
        # Imported here, not at module scope: yfinance is an optional extra, so a top-level
        # import would break every install that did not ask for it.
        import yfinance
    except ImportError as exc:
        raise ConfigError(
            "price_provider = 'yfinance' requires the optional extra.",
            hint=(
                "uv sync --extra yfinance, or set price_provider = 'tiingo'.\n"
                "yfinance is not a default dependency: DESIGN.md §4.3 calls it dev convenience "
                "only, and it ships a scraper and a TLS-impersonation chain that a Tiingo user "
                "should not have to install."
            ),
        ) from exc
    return yfinance


def _to_bars(frame: Any, *, ticker: str) -> list[PriceBar]:
    """Turn yfinance's DataFrame into bars, via strings so no ``float`` is ever converted.

    pandas holds prices as ``float64`` — there is no parse hook to install and no way to ask for
    ``Decimal``. So each value is rendered with ``repr`` and parsed as a ``Decimal`` from that text,
    which is the closest this path can get to CLAUDE.md convention 8.

    That is a real limitation rather than a solved problem, and it is the fourth reason §4.3 calls
    this adapter dev convenience: the ``float`` has already happened inside pandas before we see it.
    Tiingo and Stooq both hand over text and do not have this problem.
    """
    if frame is None or getattr(frame, "empty", True):
        return []

    columns = _flatten_columns(frame, ticker=ticker)
    bars: list[PriceBar] = []
    for index, row in frame.iterrows():
        day = _to_date(index)
        if day is None:
            continue
        close = _decimal_from(_cell(row, columns.get("Close")))
        if close is None:
            continue
        bars.append(
            PriceBar(
                day=day,
                close=close,
                adj_close=_decimal_from(_cell(row, columns.get("Adj Close"))),
                open=_decimal_from(_cell(row, columns.get("Open"))),
                high=_decimal_from(_cell(row, columns.get("High"))),
                low=_decimal_from(_cell(row, columns.get("Low"))),
                volume=_int_from(_cell(row, columns.get("Volume"))),
            )
        )
    return bars


def _flatten_columns(frame: Any, *, ticker: str) -> dict[str, Any]:
    """Map ``"Close"`` to whatever key this frame actually uses.

    yfinance returns a MultiIndex (``("Close", "AAPL")``) for some calls and a flat index for
    others, and which one you get depends on the version and on whether one ticker or several were
    requested. Resolving it here rather than at each field access is what keeps this adapter from
    breaking on a minor upgrade — which is the failure the ``<2`` ceiling only partly protects
    against.
    """
    mapping: dict[str, Any] = {}
    for column in frame.columns:
        if isinstance(column, tuple):
            name = str(column[0])
            if len(column) > 1 and str(column[1]).upper() not in ("", ticker.upper()):
                continue
        else:
            name = str(column)
        mapping.setdefault(name, column)
    return mapping


def _cell(row: Any, column: Any) -> object:
    if column is None:
        return None
    try:
        return row[column]
    except (KeyError, IndexError):
        return None


def _to_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        candidate = value.date()  # pandas Timestamp
        return candidate if isinstance(candidate, date) else None
    return None


def _decimal_from(value: object) -> Decimal | None:
    """``Decimal`` from the value's ``repr``. Never ``Decimal(a_float)``, which is exact and wrong."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    try:
        text = repr(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if text in ("nan", "inf", "-inf"):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _int_from(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # `isnan` rather than `number != number`: pandas writes NaN for a missing volume, and
    # `int(nan)` raises rather than returning anything a caller could use.
    return None if math.isnan(number) else int(number)

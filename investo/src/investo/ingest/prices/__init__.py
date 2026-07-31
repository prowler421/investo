"""Daily price series, from one of three interchangeable providers (DESIGN.md §4.3).

ROADMAP M1 flags this as easy to forget and says why it cannot be: prices gate report sections 3
and 4, the valuation component of the verdict, and M7's benchmark comparison. It is also half of
one of M1's four exit criteria.

``base``
    :class:`~investo.ingest.prices.base.PriceBar`,
    :class:`~investo.ingest.prices.base.PriceSeries`, the
    :class:`~investo.ingest.prices.base.PriceProvider` protocol, and provider selection.

``tiingo``
    The default. Supplies an adjusted close.

``yfinance_``
    Development convenience, and an **optional extra** rather than a dependency. Trailing
    underscore per DESIGN.md §3.1 so the module does not shadow the package it imports.

``stooq``
    Free cross-check. No adjusted close, and it reports that as ``None`` rather than aliasing the
    raw close into the field — see ``base``.
"""

from __future__ import annotations

__all__: list[str] = []

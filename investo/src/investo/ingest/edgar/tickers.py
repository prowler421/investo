"""``company_tickers_exchange.json`` -> rows, filtered to NASDAQ on demand.

Verified shape, fetched 2026-07-31::

    {"fields":["cik","name","ticker","exchange"],
     "data":[[1045810,"NVIDIA CORP","NVDA","Nasdaq"],
             [1652044,"Alphabet Inc.","GOOGL","Nasdaq"],
             [320193,"Apple Inc.","AAPL","Nasdaq"], ...]}

``company_tickers.json`` — the CIK-only file, also listed in DESIGN.md §4.1 — is deliberately
unused. Two lookup paths for the same question is how a NASDAQ filter comes to be bypassed by
whichever call site used the other one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from investo.domain.provenance import SourceContext
from investo.errors import TickerNotFoundError, UpstreamFetchError
from investo.ingest.edgar._fields import as_cik, require

__all__ = ["NASDAQ", "TickerRow", "parse_tickers", "resolve", "classes_for_cik"]

NASDAQ: Final = "nasdaq"
"""The exchange value, casefolded.

**The payload spells it ``"Nasdaq"``, mixed case.** Comparing against ``"NASDAQ"`` matches nothing
and the symptom is exit 2 for every ticker in the universe — a failure that looks like the file
being broken rather than the comparison being wrong. Every comparison here is casefolded.
"""

_REQUIRED_FIELDS: Final = ("cik", "name", "ticker", "exchange")


@dataclass(frozen=True, slots=True)
class TickerRow:
    """One row of the exchange file.

    ``cik`` is an ``int``. It arrives here as an unpadded integer — the one endpoint of the three
    that does not zero-pad — and padding is the client's job
    (:func:`~investo.ingest.edgar.client.cik_path`). This parser does not stringify it.
    """

    cik: int
    name: str
    ticker: str
    exchange: str

    @property
    def is_nasdaq(self) -> bool:
        return self.exchange.casefold() == NASDAQ


def parse_tickers(body: bytes, *, source: SourceContext) -> tuple[TickerRow, ...]:
    """Parse the exchange file.

    **Reads the ``fields`` array; never indexes positionally.** The file ships its own column
    header, and a parser that hardcodes ``row[3] == exchange`` is correct right up until SEC
    inserts a column — at which point it reads company names as exchanges and every ticker becomes
    "not NASDAQ". Building a ``fields -> index`` map costs one line and the failure it prevents is
    total.

    Rows whose ``cik`` cannot be normalized are dropped rather than fatal: the file covers the
    whole market, and one malformed row should not stop a run for a company listed correctly. Rows
    with a missing *column* are a different thing — that is a shape change, and it raises.

    Args:
        body: Raw JSON.
        source: Unused for the row type, which carries no provenance — the exchange file is a
            lookup table, not a source of figures. Accepted for interface uniformity across
            parsers, and because a future ``SourceRef`` on the row would need it.

    Raises:
        UpstreamFetchError: if ``fields`` or ``data`` is absent, or a required column is missing.
            Exit 4: a malformed payload is not an absence.
    """
    del source  # see Args
    payload: Any = json.loads(body)
    fields = require(payload, "fields", where="company_tickers_exchange.json")
    data = require(payload, "data", where="company_tickers_exchange.json")
    if not isinstance(fields, list) or not isinstance(data, list):
        raise UpstreamFetchError(
            "company_tickers_exchange.json: `fields` and `data` must be lists."
        )

    index = {str(name): position for position, name in enumerate(fields)}
    missing = [name for name in _REQUIRED_FIELDS if name not in index]
    if missing:
        raise UpstreamFetchError(
            f"company_tickers_exchange.json is missing column(s) {missing}; found {fields}.",
            hint="The file's own `fields` array is the header. A missing column is a shape change.",
        )

    rows: list[TickerRow] = []
    for row in data:
        if not isinstance(row, list) or len(row) < len(fields):
            continue
        try:
            cik = as_cik(row[index["cik"]])
        except ValueError:
            continue
        ticker = row[index["ticker"]]
        exchange = row[index["exchange"]]
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        rows.append(
            TickerRow(
                cik=cik,
                name=str(row[index["name"]]),
                ticker=ticker.strip().upper(),
                exchange=str(exchange or "").strip(),
            )
        )
    return tuple(rows)


def resolve(rows: Sequence[TickerRow], ticker: str) -> TickerRow:
    """The row for ``ticker``, or exit 2.

    README and DESIGN.md §14: exit 2 is "ticker not found **or not NASDAQ**". Both halves raise
    :class:`~investo.errors.TickerNotFoundError`, and both get their own violation test — a
    happy-path test passes whether or not the second half is enforced. The one that matters is a
    ticker present with ``exchange: "NYSE"``, which catches an implementation that resolved the
    CIK and forgot to check the exchange.

    Raises:
        TickerNotFoundError: absent from the file, or present and not NASDAQ. Exit 2.
    """
    wanted = ticker.strip().upper()
    matches = [row for row in rows if row.ticker == wanted]
    if not matches:
        raise TickerNotFoundError(
            f"{wanted} is not in SEC's ticker file.",
            hint=(
                "Check the spelling. investo covers NASDAQ-listed companies that file with SEC; "
                "foreign private issuers filing 20-F and companies without SEC registration are "
                "not in this file."
            ),
        )
    nasdaq = [row for row in matches if row.is_nasdaq]
    if not nasdaq:
        listed = ", ".join(sorted({row.exchange or "(none)" for row in matches}))
        raise TickerNotFoundError(
            f"{wanted} is listed on {listed}, not NASDAQ.",
            hint=(
                "investo is NASDAQ-only for now — DESIGN.md §1 scope. NYSE and non-US filers are "
                "in ROADMAP § Later."
            ),
        )
    return nasdaq[0]


def classes_for_cik(rows: Sequence[TickerRow], cik: int) -> tuple[TickerRow, ...]:
    """Every NASDAQ-listed share class for ``cik``, ordered by ticker.

    **One CIK can have several rows.** Multi-class issuers appear once per ticker — GOOGL and
    GOOG, FOX and FOXA — which is exactly what DESIGN.md §5.4's "sum all classes" needs.
    :func:`resolve` returns the row for the ticker asked for; this is how the *other* classes are
    found, and market cap is what uses it.

    Ordered so that the class list printed in ``Derivation.note`` is deterministic. §5.4 requires
    the report to state which classes were counted, and a set would make that string reorder
    between runs — which DESIGN.md §11's byte-identical gate would catch as a failure.
    """
    matching = (row for row in rows if row.cik == cik and row.is_nasdaq)
    return tuple(sorted(matching, key=lambda row: row.ticker))

"""``investo fetch TICKER`` — populate the cache and print what happened (ROADMAP M1).

The command body lives here rather than in ``cli.py`` so that ``cli.py`` stays what M0 made it: the
declared flag surface and nothing else. ``tests/test_cli_surface.py`` reads that file against
README § Usage in both directions, and a few hundred lines of orchestration in the middle would make
that check harder to trust rather than easier.

**No new flags.** ``fetch`` already accepts ``--refresh``, ``--cache-dir`` and ``--config`` (M0), and
that is the whole surface it needs. How far back it fetches comes from ``settings.lookback``, not
from a ``--lookback`` flag: README § Usage documents ``--lookback`` on ``analyze`` and ``facts``
only, and adding it here would need a README line and a ``_FLAG_OWNER`` entry (CLAUDE.md convention
5) to buy something the config file already provides.

Three properties of the summary output are load-bearing rather than cosmetic:

- **``absent`` is a section, not an error.** A 404 or a missing tag is an *absence*, recorded and
  printed. Whether an absence is fatal depends on what needs it, which is not ``fetch``'s question —
  so ``fetch`` exits 0 and prints the gap, while ``analyze`` raises ``InsufficientDataError``.
- **``fetched_at`` is printed per source**, because a warm run's value is the whole point of the
  cache and a stale entry the user cannot see is a stale entry they will trust.
- **``manifest`` is the hash of the entries this run used**, not of the whole cache. Hashing the file
  would make an AAPL report's hash change when someone fetches MSFT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from investo.config import Settings, parse_lookback
from investo.domain.models import Money, cover_share_facts, market_cap
from investo.domain.periods import window
from investo.domain.provenance import Derivation, SourceContext
from investo.errors import ConfigError, InvestoError, UpstreamFetchError
from investo.ingest.cache import Cache
from investo.ingest.edgar import companyfacts as companyfacts_parser
from investo.ingest.edgar import submissions as submissions_parser
from investo.ingest.edgar import tickers as tickers_parser
from investo.ingest.edgar.client import (
    EdgarClient,
    companyfacts_url,
    submissions_page_url,
    submissions_url,
    tickers_exchange_url,
)
from investo.ingest.edgar.events import extract_events, item_parse_rate
from investo.ingest.prices.base import (
    PriceHttp,
    PriceSeries,
    price_at_or_before,
    provider_for,
)

__all__ = ["FetchResult", "SourceStatus", "run_fetch", "render_summary"]


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """One row of the summary table."""

    label: str
    status: str
    """``"fetched"``, ``"cached"`` or ``"absent"``."""
    bytes_: int
    fetched_at: datetime | None


@dataclass(slots=True)
class FetchResult:
    """Everything one ``fetch`` run learned. Rendered by :func:`render_summary`."""

    ticker: str
    cik: int | None = None
    """The resolved CIK, from the ticker row. **[M2]**

    Added in M2 — the one change that milestone makes to an M1 file. ``facts`` calls ``build_history``
    with the company's identity, and ``profile`` is ``None`` whenever the submissions payload 404s, so
    the identity has to come from somewhere that survives that. ``_resolve_ticker`` already holds the
    ``TickerRow`` and used to let it fall out of scope; the alternatives — re-reading the 1 MB ticker
    file, or a second resolution path — are both things ``docs/m1/`` rejected.

    Optional to match this dataclass's incremental-fill style, and non-``None`` by the time
    :func:`run_fetch` returns, because ``_resolve_ticker`` raises exit 2 on the path that would leave
    it unset.
    """
    name: str | None = None
    """The mixed-case company name from ``company_tickers_exchange.json``. **[M2]**

    Not from ``companyfacts.entityName``, which is EDGAR-conformed uppercase. When ``profile`` is
    present its name wins; M1's rule is about ``entityName``, and the ticker file is not that.
    """
    profile: submissions_parser.CompanyProfile | None = None
    sources: list[SourceStatus] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    filings: tuple[submissions_parser.FilingRow, ...] = ()
    facts: companyfacts_parser.CompanyFacts | None = None
    prices: PriceSeries | None = None
    market_cap: tuple[Money, Derivation] | None = None
    requests: int = 0
    elapsed_seconds: float = 0.0
    manifest_hash: str = ""
    rate_cap: float = 0.0

    def record(
        self, label: str, *, status: str, size: int = 0, fetched_at: datetime | None = None
    ) -> None:
        self.sources.append(
            SourceStatus(label=label, status=status, bytes_=size, fetched_at=fetched_at)
        )


def run_fetch(
    ticker: str,
    *,
    settings: Settings,
    refresh: bool = False,
    as_of: date | None = None,
) -> FetchResult:
    """Fetch everything M1 knows how to fetch for ``ticker``.

    Args:
        ticker: NASDAQ symbol.
        settings: Resolved configuration. ``sec_user_agent`` is required and has no default, so an
            unset value has already failed at exit 5 before this is called.
        refresh: Bypass the cache read and write new entries.
        as_of: Point-in-time date for price selection. ``None`` means today.

    Raises:
        TickerNotFoundError: absent from SEC's ticker file, or not NASDAQ. Exit 2.
        UpstreamFetchError: a malformed payload, or retries exhausted. Exit 4.
        ConfigError: an unusable cache, or a price provider whose key is missing. Exit 5.

    A **404 or a missing tag raises nothing** — it lands in :attr:`FetchResult.absent`. DESIGN.md
    §14's governing distinction is between a run that failed and a run that succeeded in reporting
    bad news, and a NASDAQ filer with no ``companyfacts`` has told us something true.
    """
    years = parse_lookback(settings.lookback)
    today = as_of or date.today()
    start, end = window(years, as_of=today)

    cache = Cache(settings.cache_dir)
    result = FetchResult(ticker=ticker.strip().upper(), rate_cap=settings.edgar_requests_per_second)

    with EdgarClient(
        user_agent=settings.sec_user_agent,
        requests_per_second=settings.edgar_requests_per_second,
        cache=cache,
        refresh=refresh,
    ) as client:
        started = datetime.now().timestamp()

        row, all_rows = _resolve_ticker(client, result)
        _fetch_submissions(client, result, cik=row.cik, window=(start, end))
        _fetch_companyfacts(client, result, cik=row.cik)
        _fetch_prices(
            client,
            result,
            settings=settings,
            cache=cache,
            refresh=refresh,
            ticker=row.ticker,
            start=start,
            end=end,
            as_of=today,
            all_rows=all_rows,
        )

        result.requests = client.request_count
        result.elapsed_seconds = datetime.now().timestamp() - started

    result.manifest_hash = cache.manifest_hash()
    return result


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
def _resolve_ticker(
    client: EdgarClient, result: FetchResult
) -> tuple[tickers_parser.TickerRow, tuple[tickers_parser.TickerRow, ...]]:
    """Resolve the ticker to a CIK, or exit 2.

    Returns the matched row *and* every row in the file, because market cap needs the other share
    classes for this CIK and re-fetching a 1 MB file to find them would be absurd.
    """
    url = tickers_exchange_url()
    response = client.get(url)
    if response.status == 404:
        raise UpstreamFetchError(
            "SEC's ticker file is missing.",
            hint="This is SEC's own index, so a 404 here is an outage rather than a bad ticker.",
        )
    result.record(
        "tickers_exchange",
        status="cached" if response.from_cache else "fetched",
        size=len(response.body),
        fetched_at=response.fetched_at,
    )
    rows = tickers_parser.parse_tickers(
        response.body, source=SourceContext(url=url, fetched_at=response.fetched_at)
    )
    # Raises TickerNotFoundError (exit 2) for both "absent" and "present but not NASDAQ".
    row = tickers_parser.resolve(rows, result.ticker)
    # Recorded rather than discarded (M2): `facts` needs the company's identity even when the
    # submissions payload 404s, and this is the only place that already has it.
    result.cik = row.cik
    result.name = row.name
    return row, rows


def _fetch_submissions(
    client: EdgarClient, result: FetchResult, *, cik: int, window: tuple[date, date]
) -> None:
    """Fetch ``filings.recent`` plus every overflow page the window reaches into.

    ``filings.recent`` is not the whole history — SEC caps it at one year or 1,000 filings,
    whichever is more, and Apple's own overflow page 001 holds 2015 filings. A 10y lookback on the
    flagship ticker reads an incomplete history without this.
    """
    url = submissions_url(cik)
    response = client.get(url)
    if response.status == 404:
        result.absent.append(f"submissions: CIK {cik} has no submissions payload")
        return
    result.record(
        "submissions",
        status="cached" if response.from_cache else "fetched",
        size=len(response.body),
        fetched_at=response.fetched_at,
    )
    context = SourceContext(url=url, fetched_at=response.fetched_at, cik=cik)
    profile, recent, files = submissions_parser.parse_submissions(response.body, source=context)
    result.profile = profile

    wanted = submissions_parser.pages_needed(files, window=window)
    pages: list[tuple[submissions_parser.FilingRow, ...]] = []
    page_bytes = 0
    for name in wanted:
        page_url = submissions_page_url(name)
        page = client.get(page_url)
        if page.status == 404:
            result.absent.append(f"submissions overflow page {name}: 404")
            continue
        page_bytes += len(page.body)
        pages.append(
            submissions_parser.parse_submissions_page(
                page.body,
                source=SourceContext(url=page_url, fetched_at=page.fetched_at, cik=cik),
            )
        )
    if wanted:
        result.record(
            f"submissions +{len(wanted)} page(s)",
            status="fetched",
            size=page_bytes,
            fetched_at=response.fetched_at,
        )

    result.filings = submissions_parser.merge_pages(recent, *pages)
    _record_item_parse_rate(result, cik=cik)


def _record_item_parse_rate(result: FetchResult, *, cik: int) -> None:
    """Report the 8-K item parse rate, so a format change shows up as a number dropping.

    ``docs/m1/04-parsers.md``: the exact ``items`` format across filers and years is not asserted by
    the design; ``items_raw`` is what makes being wrong recoverable, and this rate is what makes
    being wrong *visible*.
    """
    events = extract_events(result.filings, cik=cik)
    parsed, total = item_parse_rate(events)
    if total and parsed < total:
        result.absent.append(
            f"8-K items: {total - parsed} of {total} 8-K(s) carried no recognised item code "
            "(items_raw preserved)"
        )


def _fetch_companyfacts(client: EdgarClient, result: FetchResult, *, cik: int) -> None:
    """Fetch and parse ``companyfacts``. A 404 is an absence, not a failure."""
    url = companyfacts_url(cik)
    response = client.get(url)
    if response.status == 404:
        result.absent.append(
            f"companyfacts: none published for CIK {cik} "
            "(SEC's XBRL API aggregates only non-custom taxonomies)"
        )
        return
    result.record(
        "companyfacts",
        status="cached" if response.from_cache else "fetched",
        size=len(response.body),
        fetched_at=response.fetched_at,
    )
    result.facts = companyfacts_parser.parse_companyfacts(
        response.body, source=SourceContext(url=url, fetched_at=response.fetched_at, cik=cik)
    )

    # The identity check `docs/m1/04-parsers.md` §2 specifies, and it is **on `cik`, never on name.**
    # `companyfacts` gives "ARXIS, INC." where `submissions` gives "Arxis, Inc." — EDGAR-conformed
    # uppercase against the display form — so a name comparison would raise on correct data for
    # plenty of real filers whose CIK matches perfectly. Both payloads carry `cik` for exactly this
    # purpose, and both are normalized by `_fields.as_cik` before they get here.
    if result.facts.cik != cik:
        raise UpstreamFetchError(
            f"companyfacts for CIK {cik} describes CIK {result.facts.cik}.",
            hint=(
                "Two SEC endpoints disagree about which company this is, which means one of the "
                "URL transforms produced the wrong path. Do not trust the figures."
            ),
        )

    if result.facts.facts_dropped:
        result.absent.append(
            f"companyfacts: {result.facts.facts_dropped} fact row(s) could not be interpreted"
        )


def _fetch_prices(
    client: EdgarClient,
    result: FetchResult,
    *,
    settings: Settings,
    cache: Cache,
    refresh: bool,
    ticker: str,
    start: date,
    end: date,
    as_of: date,
    all_rows: tuple[tickers_parser.TickerRow, ...],
) -> None:
    """Fetch the price series and compute market cap.

    ``provider_for`` raises :class:`~investo.errors.ConfigError` before any request when the chosen
    provider needs a key it does not have — the same shape as the User-Agent rule. The consequence,
    recorded as spec question 11: ``price_provider`` defaults to ``tiingo``, so ``investo fetch``
    needs a Tiingo account out of the box.
    """
    del client  # prices are a different host; see ingest/prices/base.py on why not EdgarClient
    http = PriceHttp(cache=cache, refresh=refresh)
    provider = provider_for(settings, http=http)
    series = provider.daily(ticker, start=start, end=end)
    result.prices = series
    result.record(
        f"prices ({provider.name})",
        # Read off the fetcher rather than hardcoded. Printing "fetched" unconditionally would make
        # a warm run claim a request it never made, which defeats the one property this column is
        # for: a stale entry the user cannot see is a stale entry they will trust.
        status="cached" if http.served_from_cache else "fetched",
        size=len(series.bars) * 64,  # bars, not bytes: the payload itself is not retained here
        fetched_at=series.fetched_at,
    )
    if not series.bars:
        result.absent.append(f"prices: no price history from {provider.name} for {ticker}")
        return
    if not series.adjusted:
        result.absent.append(
            f"prices: {provider.name} supplies no adjusted close, so beta and any "
            "total-return figure are unavailable from this provider"
        )

    bar = price_at_or_before(series, as_of)
    if bar is None:
        result.absent.append(
            f"prices: no bar at or before {as_of.isoformat()} from {provider.name}"
        )
        return

    _compute_market_cap(result, price=bar.close, day=bar.day, series=series, all_rows=all_rows)


def _compute_market_cap(
    result: FetchResult,
    *,
    price: Decimal,
    day: date,
    series: PriceSeries,
    all_rows: tuple[tickers_parser.TickerRow, ...],
) -> None:
    """Market cap from the cover-page share count, or an absence.

    **A filer with no ``dei`` section has no market cap, and that is an absence rather than a zero.**
    Confirmed live: the ``companyfacts`` payload for a recently-listed NASDAQ filer contains ``ffd``
    and ``us-gaap`` and no ``dei`` at all. A market cap of 0 would flow into every multiple in report
    section 3 and into the valuation sub-score, which is exactly what the ``Optional`` return of
    :func:`~investo.domain.models.market_cap` exists to prevent.
    """
    del day
    if result.facts is None:
        return
    share_facts = cover_share_facts(result.facts.all_facts())
    if not share_facts:
        result.absent.append(
            "market cap: no dei:EntityCommonStockSharesOutstanding in companyfacts "
            "(a filer that has not yet filed a periodic report has no cover-page share count)"
        )
        return
    classes = [row.ticker for row in tickers_parser.classes_for_cik(all_rows, result.facts.cik)]
    result.market_cap = market_cap(
        price=price,
        price_source=series.source,
        share_facts=share_facts,
        classes=classes or None,
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def render_summary(result: FetchResult) -> str:
    """The human-readable summary.

    Human-readable **only**. A ``--json`` variant is deliberately not added: it would need a README
    line and a ``_FLAG_OWNER`` entry, and ``report.json`` (M2) is the machine-readable surface this
    project already committed to.
    """
    lines: list[str] = []
    profile = result.profile
    if profile is not None:
        head = [result.ticker, profile.name, f"CIK {profile.cik}"]
        exchange = profile.exchanges[0] if profile.exchanges else "Nasdaq"
        head.append(exchange)
        if profile.sic is not None:
            head.append(f"SIC {profile.sic}")
        if profile.fiscal_year_end:
            head.append(f"FY end {profile.fiscal_year_end}")
        lines.append("  ".join(head))
    else:
        lines.append(result.ticker)
    lines.append("")

    lines.append(f"  {'source':<26} {'status':<10} {'bytes':>9}  fetched_at")
    for source in result.sources:
        stamp = source.fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ") if source.fetched_at else "-"
        lines.append(
            f"  {source.label:<26} {source.status:<10} {_human_bytes(source.bytes_):>9}  {stamp}"
        )

    if result.market_cap is not None:
        value, derivation = result.market_cap
        lines.append("")
        lines.append(f"  market cap  {value:,.0f}   ({derivation.note})")

    if result.absent:
        lines.append("")
        lines.append("  absent")
        for note in result.absent:
            lines.append(f"  {note}")

    lines.append("")
    lines.append(
        f"  {result.requests} requests · {result.rate_cap:.1f} req/s cap · "
        f"{result.elapsed_seconds:.1f}s · manifest {result.manifest_hash[:8]}"
    )
    return "\n".join(lines)


def _human_bytes(count: int) -> str:
    """``38700000`` -> ``"36.9 MB"``.

    The one ``float`` under ``src/`` that is not on the yfinance boundary, and it is deliberate:
    this is display formatting of a byte count. CLAUDE.md convention 8 is about **money**, and a
    ``Decimal`` here would buy exactness in a number that is rounded to one decimal place for a
    human to glance at.
    """
    if count <= 0:
        return "-"
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def cache_root(settings: Settings) -> Path:
    """The resolved cache directory. Exposed so ``cache prune`` and ``fetch`` agree on it."""
    return settings.cache_dir


def open_cache(settings: Settings) -> Cache:
    """Open the cache, translating an unreadable directory into exit 5.

    Raises:
        ConfigError: on an unknown format version or an unreadable manifest.
    """
    try:
        return Cache(cache_root(settings))
    except InvestoError:
        raise
    except OSError as exc:
        raise ConfigError(
            f"Cache directory {cache_root(settings)} is not usable: {exc}",
            hint="Check permissions, or pass --cache-dir somewhere writable.",
        ) from exc

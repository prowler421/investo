# M1 — Prices

`ingest/prices/`: `base.py` (protocol), `tiingo.py` (default), `yfinance_.py`, `stooq.py`.

ROADMAP M1 flags this as easy to forget and says why it cannot be: prices gate report sections 3
and 4, the valuation component of the verdict, and M7's benchmark comparison. It is also half of
one of M1's four exit criteria.

DESIGN §4.3 is normative on the provider table, the yfinance caveats, and survivorship bias.

---

## 1. `base.py` — the protocol

```python
@dataclass(frozen=True, slots=True)
class PriceBar:
    day: date
    close: Decimal
    adj_close: Decimal | None      # None when the provider does not supply one
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    volume: int | None

@dataclass(frozen=True, slots=True)
class PriceSeries:
    ticker: str
    provider: str                  # "tiingo" | "yfinance" | "stooq"
    bars: tuple[PriceBar, ...]     # ascending by day
    adjusted: bool                 # whether adj_close is populated at all
    fetched_at: datetime
    source: SourceRef

class PriceProvider(Protocol):
    name: str
    def daily(self, ticker: str, *, start: date, end: date) -> PriceSeries: ...
```

`Decimal`, not `float`, throughout — CLAUDE.md convention 8, and prices are money. Every adapter
parses its response with `parse_float=Decimal` (JSON) or `Decimal(text)` (CSV), never via
`float()`.

### `adj_close` is `Optional` on purpose, and this is the interesting part of the protocol

ROADMAP M1's exit criterion is *"all three adapters returning identical schemas."* Stooq supplies
no adjusted close (§4.3). There are two ways to satisfy the criterion:

1. Alias `close` into `adj_close`. Every adapter then returns a fully populated struct and a naive
   schema check passes.
2. Return `None` and set `adjusted=False`.

The first is the wrong one, and it is worth saying why at length because it is the tempting one. An
aliased `adj_close` feeds a β estimated over 5 years of weekly returns (§5.4) on *unadjusted*
prices, so every dividend and split in the window reads as a real return. β is then wrong,
WACC is wrong, and fair value moves by double digits — §5.4 says as much about unshrunk β alone.
And nothing in the report would say so, because the field was populated.

So: `None`, `adjusted=False`, and a caller that needs adjusted prices raises rather than computes.

**Violation test:** `test_prices_contract.py::test_stooq_adj_close_is_none_not_close` asserts
`bar.adj_close is None`, and `series.adjusted is False`. It deliberately does **not** assert
`bar.adj_close != bar.close` — that is the lazy spelling, and it passes trivially whether the
field is `None` or genuinely different, so it would go green against the aliasing bug it exists
to catch.

### The contract test

One parameterized test over all three adapters, run against recorded cassettes:

- ascending, strictly increasing `day`
- no duplicate days
- every bar within `[start, end]`
- `adjusted` is `True` if and only if every bar has a non-`None` `adj_close`
- `provider` matches `name`
- all monetary fields are `Decimal`, and `isinstance(x, float)` is false for every one

That last assertion is the violation test for convention 8 on this path.

---

## 2. `tiingo.py` — the default

Endpoint: `https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate=…&endDate=…`, token from
`Settings.tiingo_key` (`INVESTO_TIINGO_KEY`). Supplies `adjClose`, so `adjusted=True`.

Auth by `Authorization: Token <key>` header rather than a query parameter, so the key does not
land in the cache key, the manifest's `url` field, or a log line. That is not a detail: §10 says
API keys go via env only, never committed, never logged, and a cache manifest is a file on disk.

**Rate limits are read from config, not hardcoded.** §4.3 records the free tier as 1,000 req/day,
50/hr, 500 unique symbols/month, 1 GB bandwidth. A search while writing this document returned
different figures and none from Tiingo's current documentation, so this design asserts no number —
see [spec question 7](README.md#7-spec-questions). The adapter respects Tiingo's own 429 and
surfaces it as `UpstreamFetchError` after retries; a client-side limiter for Tiingo can be added
once the real quota is confirmed.

Missing key → `ConfigError` (exit 5) naming `INVESTO_TIINGO_KEY`, raised before any request. Same
shape as the User-Agent rule: a config problem detected at startup, not after a fetch has begun.

---

## 3. `yfinance_.py` — development convenience

Module name has the trailing underscore per DESIGN §3.1, so the module does not shadow the
package it imports.

**An optional extra, not a dependency** — [spec question 8](README.md#7-spec-questions) and
[README § Dependencies](README.md#6-dependencies-m1-adds). The import is lazy, inside `daily()`:

```python
try:
    import yfinance
except ImportError as exc:
    raise ConfigError(
        "price_provider = 'yfinance' requires the optional extra.",
        hint="uv sync --extra yfinance, or set price_provider = 'tiingo'.",
    ) from exc
```

§4.3's caveats translate into three concrete behaviours:

**Partial history that looks complete is the dominant failure.** Throttling sometimes returns a
short series with a 200, so a row count is validated rather than trusted:

```python
expected = weekdays_between(start, end)
if len(bars) < 0.9 * expected:
    raise UpstreamFetchError(
        f"yfinance returned {len(bars)} bars for a window with ~{expected} weekdays; "
        "this is the partial-history symptom of Yahoo throttling, not a short listing history."
    )
```

Weekday count rather than a market calendar, deliberately: the check needs ~10% accuracy and a
calendar dependency would be a package added to catch a rounding error. The 10% floor
accommodates roughly nine market holidays a year plus a few. The message names the ambiguity
because a genuinely recent IPO also returns few bars, and the user is the one who can tell.

**`auto_adjust` is pinned explicitly, not left to the library default.** §4.3: `auto_adjust=True`
back-adjusts, so historical prices *change* when a dividend is paid and two pulls on different
dates legitimately disagree. The adapter requests raw and adjusted separately and populates both
`close` and `adj_close`, so the report can state which it used. The cache is what guarantees the
numbers do not move under us — which only holds because §4.4's store never overwrites.

**The version range is `>=1.4,<2`.** §4.3: `curl_cffi` became optional with a `requests` fallback
at 1.4.0; 1.2.1 forced `curl_cffi>=0.15` for a CVE and 1.5.2 fixed breakage against
`curl_cffi>=0.16`. A floor below 1.4 inherits a hard `curl_cffi` requirement; no ceiling invites a
major bump on a library whose upstream has no contract.

---

## 4. `stooq.py` — cross-check

`https://stooq.com/q/d/l/?s={ticker}.us&d1=…&d2=…&i=d`, CSV, no key, undocumented quota.

`adj_close=None`, `adjusted=False` — see §1. Its role is a cross-check: two providers disagreeing
on a close by more than a tolerance is a data-integrity signal, and having a third opinion for
free is worth an adapter.

The disagreement *check* is not M1's. M1 makes it possible by giving all three adapters the same
schema. Where the comparison lives (a data-integrity flag under §6.4) is M4's call.

---

## 5. Provider selection

`Settings.price_provider` is already a `Literal["tiingo", "yfinance", "stooq"]` (M0), so an
unknown value is a pydantic validation error → `ConfigError` → exit 5, with no registry lookup
needed and no string dispatch to get wrong.

```python
def provider_for(settings: Settings, client_factory: …) -> PriceProvider: ...
```

A function over a dict-based registry, because the set is closed by the `Literal` and
basedpyright checks exhaustiveness on a `match`. A registry would accept a fourth name that the
type does not, which is how config validation ends up in two places.

ROADMAP's "swappable via config" is tested by resolving each of the three literal values and
asserting `provider.name` matches — including via the config file, not only the env, since a
setting that only works from the environment is half a feature.

---

## 6. Market cap

ROADMAP M1: computed as `price × dei:EntityCommonStockSharesOutstanding` across classes.
`§4.3`: computed, not fetched, because `yfinance`'s `Ticker.info` is the flakiest surface in the
library and EDGAR's cover-page count is authoritative and already cached.

The function lives in `domain/models.py`, not here — pure arithmetic, zero I/O, and it keeps the
one place a share-count tag is named out of `ingest/`. Specified in
[`01-domain-types.md`](01-domain-types.md#market_cap).

What this file owns is the price half:

- The price is the **last bar at or before the as-of date**, not the last bar in the series. With
  `--as-of` set, using the newest available price would be a lookahead leak in the one number that
  is compared against modelled value.
- Its `SourceRef` records the provider, the URL, the bar's date and `fetched_at`. So a market cap
  in the appendix names both a filing and a price fetch, which is the case
  [spec question 2](README.md#7-spec-questions) exists to represent.
- Multi-class: the share-count facts come from `companyfacts` by CIK; the *tickers* for the
  other classes come from `tickers.py`'s multiple rows per CIK. Only classes with both a share
  count and a price contribute, and the `Derivation.note` names the ones included — §5.4 requires
  the report to state which classes were counted, and it cannot if the omission is silent.
- **A filer with no `dei` section has no market cap, and that is an absence rather than an
  error.** Confirmed live: the `companyfacts` payload for a recently-listed NASDAQ filer contains
  `ffd` and `us-gaap` and no `dei` at all
  ([`04-parsers.md` §2](04-parsers.md#2-companyfactspy-m1a)). Since
  `dei:EntityCommonStockSharesOutstanding` is the only source for the share count, `market_cap`
  returns `None` and the coverage report records why. Not a `KeyError`, and emphatically not a
  zero — a market cap of 0 would flow into every multiple in section 3 and into the valuation
  sub-score. The first NASDAQ IPO anyone analyses exercises this path.

---

## 7. Survivorship bias — recorded, not solved

§4.3 and §8: delisted tickers largely vanish from Yahoo and Stooq, there is no stable permanent
identifier (no CRSP `PERMNO`), and tickers get reused across mergers, so today's ticker may not be
the same company it was five years ago. Any backtest over *currently listed* tickers silently
drops the failures.

M1 cannot fix this and must not paper over it. What M1 does:

- `PriceSeries` records `provider` and `fetched_at`, so M7 can at least state which source a
  universe came from and when.
- A ticker that resolves in `company_tickers_exchange.json` but returns no price history is
  reported as an absence with that exact wording — "no price history from {provider}" — rather
  than as a fetch failure. Conflating the two would hide the pattern that reveals the bias.

ROADMAP open question 7 (a free historical index-constituent source) stays open. §8's fallback —
state the resulting optimism as a known limitation in the report — is the current answer.

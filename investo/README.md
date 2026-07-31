# Investo

Fundamental due-diligence reports for NASDAQ-listed companies, generated from SEC filings.

Give it a ticker; it pulls the company's audited financials and 10-K/10-Q text from EDGAR,
runs a deterministic financial model, scans for accounting and governance red flags,
compares against sector peers, and emits a PDF.

> **Status: design phase.** Nothing is implemented yet. See [DESIGN.md](DESIGN.md) for the
> architecture and [ROADMAP.md](ROADMAP.md) for the build plan and open questions.

---

## What it produces

```bash
investo analyze AAPL --lookback 5y --out ./reports
```

A 20–25 page PDF (`--brief` for a 2-page version) containing:

- **Historical trends** — revenue and YoY growth, margin stack, free cash flow vs. net
  income, balance sheet, share count, ROIC vs. WACC
- **Forward scenarios** at 1y / 2y / 5y — revenue, margins, and free cash flow as P10/P50/P90
  ranges from a single Monte Carlo, with a DCF bridge and a sensitivity tornado showing which
  assumption actually drives the answer
- **Quality scores** — Piotroski F, Altman Z, Beneish M, plus efficiency trends (asset and
  inventory turnover, cash conversion cycle), all with peer percentiles
- **Red flags** — accrual divergence, dilution, leverage, customer concentration,
  going-concern language, each linked to its source filing
- **What changed** — 8-K event detection (auditor changes, material impairments, delisting
  notices, and Item 4.02 non-reliance on prior financials — the loudest accounting warning
  there is), year-over-year diffs of the Risk Factors and MD&A sections, insider transactions,
  institutional holdings, and short-interest trend
- **Narrative risk analysis** — extracted from filing text by an LLM, with every claim
  quote-attributed to an accession number (optional; `--llm none` skips it)
- **Caveats** — data coverage, methodology limits, measured historical accuracy, and an
  explicit statement of what the report cannot tell you
- **A verdict** — `AVOID` / `CAUTION` / `NEUTRAL` / `FAVORABLE` / `STRONG`, decomposed into
  scored components, paired with a **separate confidence rating**

---

## What it deliberately does not do

**It does not predict stock prices.** Over 1–2 years, price is driven by multiple
re-rating and sentiment far more than by fundamentals. Investo forecasts *fundamentals*
with explicit error bars and translates them to price only through valuation multiples you
can see and change. It will not print a target price.

The valuation multiple is **visible and replaceable** rather than buried in a model. Investo
does supply a default — the peer cohort's current EV/EBIT distribution — but it prints that
default and its source on the assumptions page, and `--assumptions` overrides it. The
multiple is the largest source of variance in any DCF; hiding it inside a model is how tools
like this mislead people.

Relatedly, the verdict's valuation component scores against the **whole P10–P90 band**, not
a single fair-value estimate. A company that can't be valued precisely produces a wide band,
which automatically contributes little to the verdict rather than a confident number.

A report that concludes "insufficient data" or "this company can't be valued this way" is
working correctly. Banks, REITs, and pre-revenue biotech get the quality and flag analysis
with the valuation section deliberately omitted rather than filled with a wrong number.

The point isn't prediction. It's compressing hours of filing review into minutes, catching
what a human skims past, and forcing every assumption to be named and adjustable.

---

## Quickstart

*(Not yet functional — target interface.)*

```bash
uv sync

# SEC requires a declared User-Agent. No default is provided; startup fails without it.
export INVESTO_SEC_USER_AGENT="Investo research your.email@example.com"
export INVESTO_TIINGO_KEY="..."        # optional: price data
export INVESTO_ANTHROPIC_KEY="..."     # optional: narrative analysis

investo analyze NVDA --llm anthropic   # --llm defaults to none
```

## Usage

```
investo analyze TICKER [options]

  --lookback DURATION       estimation window (default 5y, minimum 3y)
  --out PATH                output directory (default ./reports)
  --cache-dir PATH          raw-payload cache (default ./.cache)
  --config FILE             TOML config (default ./investo.toml, then ~/.config/investo/)
  --llm PROVIDER            anthropic | openai | gemini | none  (default none)
  --peers TICKER,...        override the SIC-derived peer cohort
  --assumptions FILE        hand-set growth, margin, WACC, exit multiple
  --as-of DATE              reconstruct using only filings available on DATE
  --refresh                 re-fetch instead of using cache
  --explain                 dump all intermediate calculations to report.json
  --brief                   2-page summary instead of the full report

investo facts TICKER        print normalized financials + coverage report
investo fetch TICKER        populate cache only
investo cache prune --older-than 90d
investo backtest --universe nasdaq100 --start 2015 --horizons 1y,2y,5y
```

`--out`, `--cache-dir`, `--refresh`, `--as-of` and `--config` are cross-cutting: they mean the
same thing on every command that accepts them, and are written after the subcommand as shown.

Exit codes: `0` success · `2` ticker not found or not NASDAQ · `3` insufficient data (report
still written, valuation omitted) · `4` upstream fetch failure · `5` config error. A missing
metric degrades coverage and confidence rather than aborting.

`--lookback` sets how far back the model estimates from. It's independent of the forecast
horizons, which are always 1y / 2y / 5y. Note that a 10y lookback crosses the ASC 606
revenue-tagging boundary for every filer, which requires stitching two different XBRL tags
into one series — it's the highest-risk setting, and the report flags when it's been done.

Every run also writes `report.json` next to the PDF: all normalized financials, computed
metrics, flags, scores, and the exact config and prompt versions used.

---

## How it works

```
ticker → CIK → EDGAR facts + filings + 8-K/DEF 14A + ownership + short interest + prices
       → normalize (tag fallback chains, restatement dedup, period bucketing)
       → analyze (fundamentals, quality, efficiency, flags, 8-K events, filing diffs,
                  peer percentiles, forecast)
       → optional LLM narrative extraction, citation-verified
       → score → PDF + report.json
```

Two properties the design treats as non-negotiable:

**Every number traces to a source.** Each figure in the report carries the accession
number, XBRL tag, and fetch timestamp it came from. If it can't be traced, it isn't
printed.

**The LLM cannot touch the numbers.** All figures come from deterministic math. The LLM
reads filing text and writes prose; its output schema has no numeric field that feeds
anything downstream. A prompt regression can't corrupt the model.

That extends to the conclusions. The bull and bear cases are composed from computed flags,
score components, and peer percentiles — not generated by asking a model what it thinks. The
LLM may put that material into readable prose; it doesn't decide what the material is.

Raw API responses are cached immutably, so reports regenerate byte-identically — which
matters more than it sounds, because both Yahoo's price adjustments and SEC's `frames`
endpoint mutate historical values over time.

And because `--as-of DATE` reconstructs state using only filings that existed on that date
(restatements included), the whole pipeline can be replayed historically — which is what
makes it possible to measure whether the forecasts were ever any good.

---

## Data sources

| Source | Used for | Auth |
|---|---|---|
| [SEC EDGAR](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | financials (XBRL), 10-K/10-Q/8-K/DEF 14A text, filing metadata, peer cohorts, share counts | none |
| EDGAR Form 4 / 13D-G / 13F | insider transactions, 5% holders, institutional holdings | none |
| [FINRA](https://www.finra.org/finra-data/browse-catalog/equity-short-interest) | short interest, twice monthly | none (bulk files) |
| [Tiingo](https://www.tiingo.com/) | daily prices | free key |
| [yfinance](https://github.com/ranaroussi/yfinance) | prices (development convenience) | none |
| [Stooq](https://stooq.com/) | optional price cross-check | none |

Market cap is **computed** as `price × shares outstanding` from EDGAR rather than fetched,
because the scraped `Ticker.info` field it would otherwise come from is the least stable
surface in the stack.

EDGAR is rate-limited to 10 req/s and requires a declared User-Agent; Investo runs at 5
req/s through a single choke point. Financial data is public domain — cite the SEC.

The Tiingo and yfinance free tiers are **non-commercial**, and yfinance scrapes an
undocumented Yahoo endpoint with no SLA. Both are fine for personal research; neither is a
basis for anything distributed. See [DESIGN.md §4.3](DESIGN.md).

---

## Disclaimer

**This software is for educational and informational purposes only. It is not investment
advice, not a recommendation to buy or sell any security, and does not create an advisory
relationship. Output is generated by statistical models from historical data and will be
wrong. Past performance does not indicate future results. Do your own research and consult
a licensed financial advisor before making investment decisions.**

Investo is not affiliated with or endorsed by the SEC, Yahoo, Tiingo, or any listed
company.

# Investo — Design

Status: **draft for review**. Nothing built yet.
Last updated: 2026-07-31

---

## 1. What this is

A CLI that takes a NASDAQ ticker and an optional lookback window, pulls the company's
audited fundamentals and filings, and emits a PDF due-diligence report containing:

- historical financial trends with charts
- forward **scenario ranges** for revenue / margins / free cash flow at 1y, 2y, 5y
- a valuation bridge from those fundamentals to an implied price range
- rules-based red flags (accounting quality, leverage, dilution, concentration)
- qualitative risk extraction from 10-K/10-Q narrative, attributed to source text
- an explicit verdict with the reasoning decomposed into scored components

```
investo analyze AAPL --lookback 5y --out ./reports
```

### 1.1 What this is explicitly not

Being honest about this up front changes the whole design, so it goes first.

**This cannot predict stock prices, and the design must not pretend otherwise.**
Over 1–2 years, price movement is dominated by multiple re-rating and sentiment, not by
fundamentals. Over 5 years fundamentals matter more, but forecast error compounds. Any
system claiming a point estimate for "AAPL in 5 years" is producing a number with error
bars wider than the number itself, and hiding that fact is the primary failure mode of
tools in this category.

So the design inverts the usual framing:

| We forecast | Confidence | How it's presented |
|---|---|---|
| Revenue, margins, FCF | Moderate, decaying with horizon | Distribution: P10/P50/P90 |
| Implied fair value | Low | Range, per explicit multiple scenario |
| Actual future price | **Not forecast** | Scenarios only, never a target |

The valuation multiple is treated as an **assumption the user sets or scenarios over**,
never as a model output. This is deliberate: multiple assumption is the single largest
source of variance in any DCF, and burying it inside a model is how these tools mislead.

**The real value proposition** is not prediction. It is:

1. Compressing 6–10 hours of manual filing review into minutes.
2. Catching things a human skims past — accrual divergence, silent dilution, auditor
   changes, segment concentration, going-concern language.
3. Forcing every assumption to be named, sourced, and adjustable.

A report whose verdict is "insufficient data, avoid" or "cannot value this reliably" is a
**successful** output, not a failure.

### 1.2 Non-goals

Out of scope, permanently or for the foreseeable future:

- Intraday data, technical analysis, chart patterns, momentum signals
- Options, derivatives, crypto, FX
- Portfolio construction, position sizing, tax
- Real-time alerting or a live dashboard
- Non-US listings and foreign private issuers (20-F/40-F) — Phase 2+
- Financials, REITs, biotech pre-revenue — these need bespoke models; **detect and
  refuse** rather than emit a wrong number (see §6.10)
- Trade execution. Ever.

---

## 2. Decisions already made

| Area | Decision | Why |
|---|---|---|
| Forecast engine | Deterministic math for all numbers; LLM only for narrative extraction and prose | Numbers must be reproducible and backtestable. An LLM cannot be allowed to invent a growth rate. |
| Data sources | SEC EDGAR (fundamentals + filings) + pluggable price provider | Free, authoritative, no auth, well-documented. See §4. |
| First deliverable | Python CLI → PDF | Smallest surface area; core is reusable behind an API later. |
| LLM integration | `LLMProvider` protocol with Anthropic / OpenAI / Gemini / null adapters | Swappable, testable, and runnable with no LLM at all. |
| Language | Python 3.13 | Ecosystem: pandas, statsmodels, matplotlib, WeasyPrint. Narrowed from "3.12+" in M0 — see ROADMAP § Decided while building M0. |

---

## 3. Architecture

Strict one-directional dependency flow. Each stage is independently testable and its
output is serializable, so any stage can be replayed from cached artifacts.

```
  ticker + lookback
        │
        ▼
┌───────────────────┐
│ 1. RESOLVE        │  ticker → CIK, exchange filter, sector (SIC)
└─────────┬─────────┘
          ▼
┌───────────────────┐   ┌──────────────────────────┐
│ 2. INGEST         │──▶│ immutable content-        │
│  EDGAR facts      │   │ addressed cache on disk   │
│  EDGAR filings    │◀──│ (raw payloads + fetch ts) │
│  8-K / DEF 14A    │   └──────────────────────────┘
│  Form 4/13D-G/13F │
│  FINRA short int. │
│  prices           │
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ 3. NORMALIZE      │  tag fallback chains, restatement dedup,
│                   │  period bucketing → FinancialHistory
└─────────┬─────────┘   + per-field provenance & coverage
          ▼
┌───────────────────┐
│ 4. ANALYZE        │
│  ├─ fundamentals  │  growth, margins, returns, ratios
│  ├─ quality       │  Piotroski F, Altman Z, Beneish M, accruals
│  ├─ efficiency    │  turnover ratios, cash conversion cycle
│  ├─ flags         │  deterministic rule engine
│  ├─ events        │  8-K item codes → severity
│  ├─ diffs         │  YoY Item 1A / MD&A change
│  ├─ peers         │  SIC cohort percentiles (XBRL frames)
│  └─ forecast      │  trend → driver build → DCF → Monte Carlo
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ 5. LLM ENRICH     │  optional. filings text → structured
│    (optional)     │  risks/moat/red flags, each citation-bound
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ 6. SCORE          │  weighted rubric → verdict + confidence
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ 7. REPORT         │  charts → HTML (Jinja2) → PDF (WeasyPrint)
└───────────────────┘
```

### 3.1 Module layout

Two implementation notes, both settled in M0 and recorded in ROADMAP § Decided during design:
the package lives under **`src/`** (`src/investo/…`), so tests import the installed package
rather than the working tree; and the tree below is **created per milestone, not up front**,
because an empty package cannot be meaningfully type-checked or tested and goes stale if the
design moves before it is filled.

```
investo/
├── cli.py                    # typer app                         [M0]
├── config.py                 # pydantic-settings; env + TOML     [M0]
├── errors.py                 # ExitCode (§14) + exceptions       [M0]
├── domain/
│   ├── models.py             # frozen dataclasses; zero I/O
│   ├── periods.py            # FiscalPeriod, duration bucketing
│   └── provenance.py         # SourceRef: accn, tag, url, fetched_at
├── ingest/
│   ├── cache.py              # content-addressed, append-only
│   ├── edgar/
│   │   ├── client.py         # rate limiter + UA + retry
│   │   ├── tickers.py        # company_tickers.json → CIK
│   │   ├── companyfacts.py
│   │   ├── submissions.py
│   │   ├── documents.py      # 10-K/10-Q primary doc fetch + section split
│   │   ├── events.py         # 8-K item extraction (§6.6)
│   │   ├── ownership.py      # Form 4, 13D/G, 13F XML (§6.8)
│   │   ├── proxy.py          # DEF 14A + pay-vs-performance iXBRL (§6.9)
│   │   └── frames.py         # peer cohort pulls
│   ├── finra.py              # short interest, snapshotted (§6.8)
│   └── prices/
│       ├── base.py           # PriceProvider protocol
│       ├── tiingo.py         # default
│       ├── yfinance_.py      # dev convenience
│       └── stooq.py          # cross-check
├── normalize/
│   ├── tags.py               # ordered fallback chains per metric
│   ├── facts.py              # dedup, as-of filtering, period buckets
│   └── statements.py         # → FinancialHistory + CoverageReport
├── analyze/
│   ├── fundamentals.py
│   ├── quality.py            # F/Z/M scores, accrual ratio
│   ├── efficiency.py         # turnover ratios, cash conversion cycle
│   ├── flags.py              # rule registry
│   ├── diffs.py              # year-over-year Item 1A / MD&A change (§6.7)
│   ├── events.py             # 8-K item → severity mapping
│   ├── peers.py
│   ├── forecast/
│   │   ├── trend.py          # log-linear fit + prediction interval
│   │   ├── drivers.py        # revenue → margin → FCF build
│   │   ├── dcf.py
│   │   └── mc.py             # Monte Carlo over driver distributions
│   └── score.py
├── llm/
│   ├── provider.py           # protocol + registry
│   ├── providers/            # anthropic.py openai.py gemini.py null.py
│   ├── prompts/              # versioned .md templates
│   └── extract.py            # schema-validated, citation-enforced
├── report/
│   ├── charts.py             # matplotlib → SVG or PNG per chart
│   ├── render.py             # Jinja2 → HTML → WeasyPrint → PDF
│   ├── serialize.py          # → report.json
│   └── templates/
└── backtest/
    ├── asof.py               # point-in-time reconstruction
    ├── runner.py
    └── metrics.py            # MAPE, directional hit rate, calibration
```

### 3.2 Core domain types

Sketch, not final:

```python
@dataclass(frozen=True)
class SourceRef:
    accession: str          # "0000320193-25-000079"
    tag: str | None         # "RevenueFromContractWithCustomer..."
    form: str               # "10-K"
    filed: date
    url: str
    fetched_at: datetime

@dataclass(frozen=True)
class Fact:
    value: Decimal
    period: FiscalPeriod
    source: SourceRef

@dataclass(frozen=True)
class FinancialHistory:
    cik: int
    ticker: str
    fiscal_year_end: str            # "MMDD"
    annual: dict[Metric, list[Fact]]
    quarterly: dict[Metric, list[Fact]]
    coverage: CoverageReport         # which metrics, which tag won, % filled
    as_of: date                      # no fact with filed > as_of is included
```

**Every number in the final PDF traces back to a `SourceRef`.** If it can't, it doesn't
get printed. This is the mechanism that keeps the report auditable and makes the LLM
unable to smuggle in an unsourced claim.

---

## 4. Data layer

### 4.1 SEC EDGAR

No API key, no auth. Host is `data.sec.gov` (note: **no CORS**, so any future browser UI
needs a server-side proxy).

| Purpose | Endpoint |
|---|---|
| All XBRL facts for a company | `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` |
| One concept | `https://data.sec.gov/api/xbrl/companyconcept/CIK##########/{taxonomy}/{Tag}.json` |
| One concept, all filers (peers) | `https://data.sec.gov/api/xbrl/frames/{taxonomy}/{Tag}/{unit}/{CCP}.json` |
| Filing history + metadata | `https://data.sec.gov/submissions/CIK##########.json` |
| Ticker → CIK | `https://www.sec.gov/files/company_tickers.json` |
| Ticker → CIK + exchange | `https://www.sec.gov/files/company_tickers_exchange.json` |
| Filing documents | `https://www.sec.gov/Archives/edgar/data/{cik}/{accn_no_dashes}/{primaryDocument}` |
| Full-text search | `https://efts.sec.gov/LATEST/search-index?q=...&forms=...` (coverage: 2001→) — used by the concentration flags in §6.2 |
| Insider transactions (bulk) | `https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets` |
| 13F holdings (bulk) | `https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets` |
| Short interest | FINRA, not SEC — `https://api.finra.org/data/group/otcMarket/name/equityShortInterestStandardized` (the older `equityShortInterest` dataset stopped publishing 2021-04-30) |

8-K events and DEF 14A come from the submissions and Archives endpoints already listed;
`filings.recent.items` carries the 8-K item codes directly, which is what makes §6.6 cheap.

CIK is zero-padded to 10 digits with a `CIK` prefix on `data.sec.gov`, but **unpadded** in
`/Archives/` paths. Accession numbers appear both with dashes (filenames, index pages) and
without (directory names). The client must own these transforms; callers pass a plain int
CIK and a canonical accession string.

**Rate limit — hard requirement.** SEC caps at **10 requests/second across all your
machines**; exceeding it gets the IP throttled until the rate stays below the threshold
for 10 minutes. SEC also requires a declared User-Agent — the documented sample format is
`<Company or app name> <contact email>`. A default `python-requests/...` UA triggers an
"Undeclared Automated Tool" error.

Therefore: a single choke-point `EdgarClient` with a token-bucket limiter set
conservatively to **~5 req/s**, mandatory `User-Agent` from config (**startup fails if
unset — no default**), `Accept-Encoding: gzip, deflate`, and exponential backoff on 403/429.
Nothing else in the codebase is allowed to make an HTTP call to sec.gov.

If we later need whole-market scans, switch to the nightly bulk ZIPs
(`companyfacts.zip`, `submissions.zip`) rather than hammering the API.

**Licensing:** EDGAR content is public domain and free to reuse; cite the SEC. Do not use
the SEC seal, and note "SEC" and "EDGAR" are registered trademarks — they can't go in a
product or domain name without a license.

### 4.2 The XBRL normalization problem

This is where most of the real engineering is, and underestimating it is the main schedule
risk. Three traps, all confirmed against live EDGAR data:

**(a) `fy` / `fp` are the fiscal year of the *containing filing*, not of the fact.**
Apple's FY2018 revenue appears tagged `fy:2019` and again `fy:2020`, because it was
carried as a comparative in later 10-Ks. **Group by `start`/`end`. Never by `fy`/`fp`.**

**(b) Restatements duplicate periods.** One Apple quarter appears four times across four
accessions. Dedup by `(unit, start, end)`, then choose:

- `max(filed)` → current restated view. Right for "what is true now."
- `max(filed) where filed <= as_of` → point-in-time. **Required for backtesting**, else
  the model sees restatements that didn't exist yet — a subtle, fatal lookahead leak.

Both must be supported from day one. The `as_of` parameter threads through `normalize`
precisely so §8 is possible.

Shortcut worth knowing: facts carrying a `frame` key are SEC's own deduplicated,
calendar-aligned selection — `[f for f in facts if "frame" in f]` is a one-line clean
series. But it drops off-calendar fiscal periods and is **not point-in-time stable** (a
CY2025Q1 frame can resolve to a 2026 filing). Use it for peer cross-sections, not for the
subject company's history.

**(c) Annual vs quarterly is duration arithmetic, not `form`.** A 10-Q carries both the
discrete quarter *and* cumulative YTD. Bucket on `(end - start).days`: annual 350–380,
quarterly 80–100; ~180d/~270d is YTD — difference it or drop it. And **discrete Q4 is
often never tagged**: derive `Q4 = FY − (Q1+Q2+Q3)`, and don't assume either behavior,
since it varies by issuer *and* by year within the same issuer.

**Tag fallback chains.** No single tag covers a majority of filers. Measured entity counts
from the CY2025Q1 frames API:

| Metric | Chain (first match wins) | Coverage note |
|---|---|---|
| Revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` (2,543) → `...IncludingAssessedTax` → `Revenues` (1,836) → `SalesRevenueNet` (pre-2018, deprecated) | Neither leader has a majority. ASC 606 split the old tag in 2018 — long histories **must** stitch across the boundary or the series has a hole. Excluding vs including assessed tax are different numbers; never mix within one series. |
| Net income | `NetIncomeLoss` (5,315) → `ProfitLoss` (2,724) | `NetIncomeLoss` is parent-only; `ProfitLoss` includes NCI. Not interchangeable. |
| Gross profit | `GrossProfit` (2,023) | Often absent; derive from revenue − COGS. |
| Operating income | `OperatingIncomeLoss` | Many financials/REITs have no operating-income line at all. |
| Total assets | `Assets` (5,633) | Reliable. |
| Total liabilities | `Liabilities` (4,998) → derive `LiabilitiesAndStockholdersEquity − StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` | ~11% of filers reporting `Assets` never tag `Liabilities`. |
| Equity | `StockholdersEquity` (5,452) | Parent-only; pair with `NetIncomeLoss` for consistency. |
| Cash | `CashAndCashEquivalentsAtCarryingValue` (4,508) vs `CashCashEquivalentsRestrictedCash...` | The latter includes restricted cash and is what ties to the CF statement. Different numbers. |
| Operating cash flow | `NetCashProvidedByUsedInOperatingActivities` (4,784) → `...ContinuingOperations` (205) | Fallback is rare but the only tag for some filers with discontinued ops. |
| Capex | `PaymentsToAcquirePropertyPlantAndEquipment` (2,696) → `PaymentsToAcquireProductiveAssets` → `...AndIntangibleAssets` → `PaymentsForCapitalImprovements` → sector-specific | Genuinely fragmented; under half of filers. Sector handling unavoidable. |
| Long-term debt | `LongTermDebtNoncurrent` (1,532) → `LongTermDebt` → `LongTermDebtAndCapitalLeaseObligations` → … | **Weakest of the set.** Expect misses; mark leverage metrics low-confidence. |
| Shares out (market cap) | `dei:EntityCommonStockSharesOutstanding` (4,747) | Cover-page count. |
| Shares out (per-share math) | `WeightedAverageNumberOfDilutedSharesOutstanding` | Using the cover-page count as an EPS or DCF denominator is a classic error. Enforce the distinction in the type system (§5.4). |
| EPS diluted | `EarningsPerShareDiluted` (4,605) → `EarningsPerShareBasicAndDiluted` → derive | JSON unit key is `USD/shares`, not `USD`. |

**A second tier of chains is needed for §6, and it's easy to miss.** The table above covers
the DCF. The Piotroski / Altman / Beneish scores need roughly 10–15 more, several as
fragmented as capex: `AssetsCurrent`, `LiabilitiesCurrent`,
`RetainedEarningsAccumulatedDeficit`, `AccountsReceivableNetCurrent`,
`CostOfGoodsAndServicesSold`, `SellingGeneralAndAdministrativeExpense`,
`DepreciationDepletionAndAmortization`, `InterestExpense`, EBIT (derived),
`ShareBasedCompensation`, `OperatingLeaseLiabilityNoncurrent`, and share-issuance proceeds.
Beneish additionally needs the prior year of all eight of its ratios, so it requires one
more year of history than the nominal lookback.

Also: `companyfacts` **excludes company-custom extension taxonomies** by design. Some line
items simply will not be there for some filers. That's not a bug to work around; it's a
coverage fact to surface.

Consequence for the design: `normalize` emits a **`CoverageReport`** recording which tag
satisfied each metric and what fraction of periods were filled. Coverage below a
configurable floor degrades the report's confidence rating and can trigger an
"insufficient data" verdict. **Hardcoding one tag per metric would silently produce sparse
data and a confidently wrong report** — the exact failure this system exists to avoid.

### 4.3 Prices

Behind a `PriceProvider` protocol so the source is swappable.

| Provider | Role | Limits |
|---|---|---|
| **Tiingo** | default primary | free key: 1,000 req/day, 50/hr, 500 unique symbols/mo, 1 GB bandwidth; supplies `adjClose`; non-commercial |
| **yfinance** | dev convenience only | no key; see caveats |
| **Stooq** | optional cross-check | no key, no `adj_close`, undocumented quota |

**Market cap is computed, not fetched** — `price × dei:EntityCommonStockSharesOutstanding`
summed across share classes. `yfinance`'s `Ticker.info` is the obvious source but the
flakiest surface in the library (schema drifts, keys vanish), and EDGAR's cover-page count
is authoritative and already in the cache.

**yfinance caveats** (the library is healthy — v1.5.2 shipped 2026-07-23, roughly monthly
cadence — but the *data source* is not a contract):

- It scrapes Yahoo's internal endpoints. Yahoo retired its public finance API in 2017 and
  never replaced it. No SLA, no versioning, no notice before breaking changes.
- `YFRateLimitError` / 429 is the dominant failure. Worse: throttling sometimes returns
  **partial history that looks complete** — validate expected row counts, don't trust a
  200.
- `curl_cffi` (TLS fingerprint impersonation, which is how it avoids blocking) was a hard
  requirement in 0.2.5x–1.3.x, where passing a `requests.Session` raised. As of **1.4.0 it
  is optional with a `requests` fallback**. Pin a version *range*: 1.2.1 forced
  `curl_cffi>=0.15` for a CVE and 1.5.2 fixed breakage against `curl_cffi>=0.16`.
- `auto_adjust=True` back-adjusts for splits/dividends, so historical prices *change* when
  a dividend is paid. Two pulls on different dates legitimately disagree. Pin raw vs
  adjusted; the cache is the only guarantee numbers don't move under you.
- Legal: yfinance's own README says it's unaffiliated with Yahoo, for research/educational
  use, that you must consult Yahoo's ToS for data rights, and that the API is "intended
  for personal use only." Apache-2.0 covers the *code*, not the *data*. Fine for personal
  use; **not a defensible base for anything shared or commercial.** Keep it behind the
  adapter and plan a licensed replacement if Investo ever leaves your machine.

**Survivorship bias** — the trap that invalidates backtests. Delisted tickers largely
vanish from Yahoo and Stooq, and there's no stable permanent identifier (no CRSP
`PERMNO`); tickers get reused across mergers and renames, so today's ticker may not be the
same company it was five years ago. Any backtest over a universe of *currently listed*
tickers silently drops the failures and inflates results. §8 addresses this explicitly.

### 4.4 Cache

Content-addressed, append-only, on disk. Key = `sha256(url + params)`. Value = raw bytes +
`fetched_at` + response headers. Never mutated, never evicted by default.

Three reasons this is load-bearing rather than an optimization:

1. **Reproducibility.** A report must regenerate byte-identically from cache.
2. **Rate limits.** 10 req/s is easy to blow through during development.
3. **Upstream drift.** yfinance adjustments and EDGAR `frames` both mutate historical
   values. The cache is the only immutable record of what the model actually saw.

`--refresh` re-fetches and writes a *new* entry rather than overwriting.

Entries carry a **schema version** so parser changes can invalidate derived data without
discarding raw payloads. Size is non-trivial — `companyfacts` alone is 10–40 MB for a large
filer, and filing documents add more — so while nothing is evicted by default, `investo
cache prune --older-than` exists and the cache path is configurable.

### 4.5 Machine-readable output

Every run also writes `report.json` alongside the PDF: the full `FinancialHistory`, all
computed metrics, forecast draws summary, flags, scores, and the config and prompt versions
used. This is what `--explain` dumps and what `investo diff` compares, and it's versioned
independently of the PDF template. It's what `--explain` dumps, and the prerequisite for the
(still undecided) `investo diff`. Without it the PDF is a dead end — nothing downstream can
consume a run.

Serializer lives at `report/serialize.py`.

---

## 5. Forecast methodology

Deterministic, no LLM. Every step logged with its inputs so the PDF can show the work.

### 5.1 Lookback

`--lookback` sets the estimation window. **Default 5y, minimum 3y.** 3 years is 12
quarters — enough to fit a trend, not enough to contain a business cycle (peak-to-peak
runs 5–10 years), so a 3y window can easily sit entirely inside one expansion and mistake
it for the steady state.

Below 12 quarters the valuation is omitted entirely (§6.10). Between 12 and 20 quarters it
runs with a prominent low-confidence banner, and the deceleration test in §5.2 is disabled
for lack of residual degrees of freedom.

### 5.2 Revenue path

**Model the stochastic trend, don't fit a deterministic one.** Quarterly revenue in logs is
close to a unit root. Fitting a deterministic log-linear trend and taking OLS prediction
intervals gives
interval widths that grow only through the leverage term, whereas a stochastic trend's
true forecast SD grows as σ√h — so at a 20-quarter horizon the OLS interval is far too
narrow. Positive residual autocorrelation, which is guaranteed in quarterly revenue
levels, biases σ̂ down on top of that.

So:

1. Fit a **local-level-with-drift** state-space model on log revenue
   (`statsmodels.tsa.UnobservedComponents`, `level='rwdrift'`), with seasonal terms. This is
   the specification that gives **√h** forecast-SD scaling. Report coefficient SEs with
   HAC/Newey–West.

   Deliberately *not* local linear trend: LLT has a random-walk slope as well as level,
   making it doubly integrated (reduced form ARIMA(0,2,2)) with forecast error variance
   `σ_ε² + σ_ξ²·h + σ_ζ²·h(h−1)(2h−1)/6` — i.e. SD growing like **h^1.5**, which at h = 20
   quarters produces intervals wide enough to be useless. The stochastic-slope flexibility
   isn't worth that. Which specification actually calibrates is an empirical question that
   §8 answers; the √h target below is the prior, not a proof.
2. **Deceleration test:** fit the trend with an added quadratic term in time and test its
   coefficient, *or* compare trailing-4Q growth against the window slope via a Chow test —
   pick one and name it. (The earlier framing of "a negative second derivative" was
   incoherent: a log-linear fit has no second derivative.) A significant deceleration
   shrinks the growth estimate toward the sector median by a documented factor. Disabled
   below 20 quarters.
3. **Apply mean reversion (fade).** Naive CAGR extrapolation over 5 years is *the* classic
   failure mode — a 40% grower compounded 5 years implies an absurd market share. Fade the
   growth rate geometrically toward the sector median (§6.5), with terminal growth **capped
   at nominal GDP growth** — no firm outgrows the economy in perpetuity. Note this is a
   cap, not a floor: the model must remain free to forecast secular decline, and a floor
   here would bias every terminal value upward. Fade half-life is a documented, tunable
   parameter, not a magic constant.
4. Because the fade moves the central path away from the fitted one, **the interval is
   transported with it**: fade is applied to each Monte Carlo growth path (§5.5), not to
   the P50 alone. Otherwise P50 and P10/P90 come from different models.
5. Sanity-check the implied revenue against any available TAM anchor and flag if the 5-year
   P50 implies an implausible share of the peer cohort's aggregate revenue.

Two statistical points recorded so they don't get lost:

- **Retransformation bias.** `exp(fitted log)` is the conditional *median*, not the mean,
  and the median of a sum is not the sum of medians. A DCF should discount *expected* cash
  flows. Either apply a Duan smearing estimator / `exp(μ + σ²/2)` correction, or state
  plainly that the entire report is median-based. Current choice: **median-based, stated
  explicitly**, because it composes correctly with the quantile presentation everywhere
  else.
- **Seasonality** needs 3 dummies plus intercept, not 4. At 12 observations against trend
  + 3 dummies there are ~7 residual dof, which is why §5.1 gates the extra tests.

### 5.3 Margin and FCF path

Regress operating margin over the window, then **soft-bound projections to the historical
[min, max] band** — margin expansion forecasts are where optimism hides. A hard clamp
would pile Monte Carlo probability mass on the bounds, so bounding is done by shrinking
toward the band (logistic squash), not truncating at it.

```
revenue → gross margin → operating margin → cash taxes → NOPAT
        → + D&A − capex − Δworking capital − SBC → FCFF
```

- **Capex** as % of revenue, anchored on the trailing 3-year mean, but in the terminal year
  it must converge toward `D&A + growth capex` — otherwise the terminal state is one of
  perpetual under- or over-investment.
- **SBC is subtracted as a real cost.** It is material across most of the NASDAQ universe,
  and adding it back (the common "adjusted FCF" convention) systematically overstates value
  for exactly the companies this tool targets.
- **Leases** under ASC 842: operating lease liabilities treated as debt, with the implied
  interest stripped out of operating costs, so leverage and EV are consistent across
  lease-heavy and owned-asset filers.
- **Taxes** use the trailing effective rate, floored at a documented minimum and reverting
  to statutory over the forecast; NOL carryforwards are noted as unmodeled.

**Terminal reinvestment consistency.** In the Gordon stage, terminal growth is not a free
parameter: `g = ROIC × reinvestment rate`. The model solves for the reinvestment rate
implied by the terminal `g` and the terminal ROIC, and **flags the valuation as incoherent
if that implies a reinvestment rate outside [0, 1]**. Skipping this is the most common DCF
error there is.

### 5.4 Valuation

Two-stage DCF on FCFF: explicit 5-year forecast, then terminal value via **both** Gordon
growth and exit multiple, shown side by side. They frequently disagree, and the
disagreement is itself informative.

**Discount rate.** CAPM gives the *cost of equity*, not WACC:

```
k_e  = r_f + β_L × ERP            r_f = current 10Y Treasury
k_d  = trailing interest expense / average total debt, floored at r_f + spread
WACC = w_e·k_e + w_d·k_d·(1 − t)   weights at market value of equity, book value of debt
```

β is estimated over 5 years of weekly returns against a broad index, then shrunk toward
1.0 (Blume/Vasicek) — raw 5-year betas are noisy enough that unshrunk estimates move fair
value by double digits. ERP is a config constant with its source cited in the report, not a
hardcoded number. Mid-year discounting convention (`t − 0.5`).

**EV → equity → per share.** The DCF yields enterprise value; the bridge is explicit
because this is where share-count errors do the most damage:

```
equity value = EV − total debt (incl. capitalized leases) + cash & equivalents
                  − minority interest − preferred
per share    = equity value / diluted shares outstanding
```

The denominator is `WeightedAverageNumberOfDilutedSharesOutstanding`, **never** the
cover-page count — enforced by distinct types on the two share-count metrics (§4.2).
Multi-class structures (GOOGL/GOOG, FOX/FOXA) sum all classes; the report states which
classes were included.

### 5.5 Monte Carlo

10,000 draws (fixed seed) over the joint distribution of:

| Input | Distribution | Anchor |
|---|---|---|
| Revenue growth path | from the §5.2 state-space model's predictive distribution | fitted |
| Terminal operating margin | truncated normal, soft-bounded to historical band | fitted |
| WACC | normal on β and k_d, propagated | §5.4 |
| Exit multiple | lognormal | **peer-cohort EV/EBIT distribution (§6.5), stated in the report** |

Constrained so that `g < WACC` in every path — otherwise the Gordon denominator explodes
and the tail is meaningless. Paths violating the constraint or the §5.3 reinvestment check
are rejected and counted, with the rejection rate printed.

Crucially, this is **one uncertainty engine, not two**: the revenue fan chart's quantiles
and the valuation quantiles come from the same set of draws, so the fan chart's P10 is the
same scenario as the valuation P10. Running the fan chart off an OLS interval and the
valuation off separate MC draws would put two mutually inconsistent figures in the same
report.

**What the output is, precisely.** The result is a distribution of *model* outcomes —
uncertainty in our own assumptions. It contains **no term for market re-rating or
idiosyncratic shocks**, which §1.1 identifies as dominating short-horizon returns. It is
therefore labeled **"implied return under model assumptions"**, never "return
distribution," and it is explicitly *not* the quantity §8's calibration check applies to
(that check applies to the fundamental forecasts, which are falsifiable).

Reported alongside, never hidden: a **sensitivity tornado chart** showing which input
drives the most variance. It will almost always be the exit multiple. Showing that plainly
is the point — it tells the user exactly how much of the answer is assumption rather than
analysis.

### 5.6 Horizon honesty

The 1-year output is explicitly labeled as low signal. Over 12 months, multiple movement
swamps fundamental change. The 2y and 5y fundamental forecasts are more defensible than
the 1y *price* range, which is counterintuitive and worth stating in the report itself.

### 5.7 Where the exit multiple comes from

§1.1 says the multiple is an assumption rather than a model output, and §5.5 draws it from
a distribution — so to be precise about what is actually true: **Investo supplies a
default drawn from the peer cohort's current EV/EBIT distribution, prints that default and
its provenance on the assumptions page, and lets `--assumptions` override it.** The claim
is not that the tool has no opinion; it's that the opinion is visible and replaceable
rather than buried in a model.

---

## 6. Quality, flags, and peers

Cheap, deterministic, high-value, and independent of the forecast. **If we build nothing
else, this section alone is useful** — it needs no model to be right, so none of it can be
wrong in the way a forecast can.

### 6.1 Composite scores

- **Piotroski F-Score** (0–9) — fundamental strength.
- **Altman Z-Score** — distress risk. Variant selected by SIC (original manufacturing Z vs.
  non-manufacturing Z″); the variant used and its cut points are printed, since they differ
  materially (§9.2).
- **Beneish M-Score** — earnings-manipulation likelihood. Present carefully: it is a
  statistical flag, not an accusation.

### 6.2 Rule-based flags

Each rule is a small pure function returning `Flag(severity, message, SourceRef)`.
Registry pattern so adding a rule is one file, one test.

| Category | Checks |
|---|---|
| Accruals | Net income growing while OCF flat/declining; rising accrual ratio |
| Working capital | DSO trend up (revenue recognition pressure); inventory outgrowing revenue |
| Dilution | Share count CAGR; SBC as % of revenue and of OCF |
| Leverage | Net debt / EBITDA; interest coverage; near-term maturity wall |
| Efficiency | Asset turnover trend; inventory turnover; cash conversion cycle deterioration |
| Concentration | Customer/segment/geographic concentration from filing disclosures |
| Governance | Late filing (NT 10-K/NT 10-Q); material weakness in ICFR; auditor change and restatement (sourced from 8-K items, §6.6) |
| Going concern | Substantial-doubt language present |
| Margins | Gross margin compression over trailing 4 quarters |
| Cash | Runway in quarters if FCF negative |
| Capital allocation | Buybacks at high multiples; acquisition spend vs. organic reinvestment; comp structure vs. performance (§6.9) |
| Events | 8-K item triggers (§6.6) |
| Disclosure change | Material year-over-year change in Item 1A or MD&A (§6.7) |

### 6.3 Efficiency ratios

Cheap once the §4.2 tier-2 tag chains exist, and genuinely informative about operational
discipline: asset turnover, inventory turnover, receivables turnover, and the **cash
conversion cycle** (DSO + DIO − DPO). Trends matter more than levels — a lengthening cash
conversion cycle alongside flat revenue is one of the earliest observable signs of demand
softening or channel stuffing.

**Revenue per employee is deliberately excluded.** It's a standard tech metric and it looks
like it should be a one-line ratio, but employee count is not usable structured data:
`dei:EntityNumberOfEmployees` is reported by **5–12 filers per year** out of ~5,000, and the
few values present are wrong or non-comparable (one staffing franchisor reports 75,000,
which is its franchise-wide worker count; a mid-size steel company reports 244). The real
source is narrative text in Item 1 / Item 1C Human Capital, which means an extraction
pipeline with a sub-100% hit rate and definitional drift across filers (FTE vs. total,
contractors in or out, as-of date). Revisit only if narrative extraction exists for other
reasons.

### 6.4 Data-integrity flags

Distinct from company flags, and important: low tag coverage, missing Q4, a stitched
revenue series crossing the ASC 606 boundary, restatement detected in the window,
lookback shorter than requested. These attach to the report's confidence rating rather
than to the company's score.

### 6.5 Peer context

A 20% grower is exceptional in utilities and mediocre in SaaS. Pull the SIC code from the
submissions API, build a cohort, and fetch the same metrics via the `frames` API to
compute percentile ranks. Frames is ideal here — cross-sectional is exactly its job.

Caveat to surface: SIC codes are coarse and sometimes miscategorize. Allow
`--peers TICKER,TICKER,...` to override.

### 6.6 8-K event monitoring

The highest value per line of code anywhere in the system. 8-K items are **coded**, so
detection is a filter on `filings.recent.items` from the submissions API — no NLP required —
and several of these are the loudest signals available anywhere in public filings.

| Item | Official title | Why it matters |
|---|---|---|
| **4.02** | Non-Reliance on Previously Issued Financial Statements or a Related Audit Report or Completed Interim Review | **The single loudest accounting red flag that exists.** The company is saying its own prior numbers are wrong. Highest severity, unconditionally. |
| **4.01** | Changes in Registrant's Certifying Accountant | Auditor change. Benign at a Big-4-to-Big-4 transition, serious mid-audit or following a disagreement — the 8-K must disclose whether there were disagreements, so read the body. |
| **2.06** | Material Impairments | Goodwill or asset write-down; often the admission that an acquisition failed. |
| **3.01** | Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard; Transfer of Listing | Direct existential signal for a NASDAQ-listed universe. |
| **5.02** | Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain Officers; Compensatory Arrangements of Certain Officers | CFO departures cluster before restatements. **Caveat: routine comp-arrangement filings share this item code**, so item number alone can't distinguish them — the body must be read. |
| **1.05** | Material Cybersecurity Incidents | New in 2023; the only structured disclosure channel for breach events. |
| 1.01 / 1.02 | Entry into / Termination of a Material Definitive Agreement | Large contracts, financing, customer wins and losses. |
| 1.03 | Bankruptcy or Receivership | Terminal. |
| 2.01 / 2.05 | Completion of Acquisition or Disposition; Costs Associated with Exit or Disposal Activities | M&A and restructuring. **Recurring** 2.05 filings are their own flag — serial restructuring is a pattern. |
| 5.01 | Changes in Control of Registrant | — |

**Two-stage detection, and it degrades cleanly.** Item codes are the trigger and are purely
deterministic — that alone is enough for 4.02, 2.06, 3.01, 1.03 and 1.05, where the code's
presence *is* the signal. Only 4.01 and 5.02 are ambiguous by code (a Big-4-to-Big-4 auditor
transition versus one following a disagreement; a CFO resignation versus a routine
compensation amendment). For those two, the 8-K body is read to refine severity — a page or
two of text, so a cheap LLM target unlike a 10-K.

Under `--llm none` those two flags still fire, at a **capped severity with an "unclassified,
read the filing" message.** So the LLM sharpens the flag; it never creates or suppresses one,
which keeps this consistent with §7.2's rule that the LLM cannot override a deterministic
flag. The nuance is that here it isn't overriding — it's annotating a flag that has already
fired.

This also means §6.6 has no hard dependency on the LLM layer, which matters because it ships
in an earlier milestone.

**Earnings releases** live at **Item 2.02** (Results of Operations and Financial Condition)
with the release furnished as an **EX-99** exhibit. Two parser notes: the `.1` in "Exhibit
99.1" is filer convention, not rule — enumerate all `EX-99*` rather than hardcoding — and
per the SEC's Form 8-K CDIs, guidance-only announcements are usually furnished under **Item
7.01 (Reg FD)** rather than 2.02, since 2.02 covers forward estimates only when bundled with
historical results.

**Earnings surprise is computed as context, not treated as a signal.** It's nearly free from
XBRL, and useful for explaining a price move or screening. But post-earnings announcement
drift has substantially decayed: Martineau (*Critical Finance Review*, 2022) finds it gone
for non-microcaps by around 2006, and the recent papers claiming revival don't exclude
microcaps — doing so drops the t-statistic from 2.18 to 1.43. Any backtest here that looks
promising must be re-run with a microcap exclusion before it's believed.

**Guidance vs. actual is out of scope.** Guidance is never XBRL-tagged, there is no free
structured source, and normalizing point estimates, ranges, multi-year targets, and purely
directional language into a comparable series requires judgment calls. That difficulty is
exactly why commercial vendors charge for it.

### 6.7 Filing-diff analysis

Year-over-year textual change in Item 1A (Risk Factors) and Item 7 (MD&A), by section, using
similarity scoring (cosine or Jaccard on normalized text) plus a readable diff of what
changed.

**The evidence, stated accurately, because it's easy to oversell.** Cohen, Malloy & Nguyen,
"Lazy Prices" (*Journal of Finance* 75(3), 2020) sorts on year-over-year document similarity
and finds firms that changed their filings underperform those that didn't. Three caveats
that determine how this gets used:

1. The widely-quoted **188 bps/month figure for Item 1A alone is the paper's weakest
   identified result** (t = 2.76, the lowest of its headline numbers). The robust, repeatedly
   significant result is 18–63 bps/month.
2. **The effect is almost entirely on the short side** — the big-changers leg earns −44
   bps/month (t = 4.56) while the non-changers leg earns +19 bps/month (t = 1.87,
   insignificant). A long-oriented research tool captures very little of the tradeable part.
3. **The sample ends in 2014**, leaving a decade unexamined, and Brown & Tucker (*JAR* 2011)
   independently found MD&A modification informativeness *declining* over time.

So filing diffs are built as a **flag generator, not a return signal**: "Item 1A changed
materially versus last year — here's the diff, go read it." That framing is well supported
by the evidence and is exactly the kind of thing a human reviewer skims past. Claiming a
return edge from it is not supported, and no part of the verdict score is driven by it.

Litigation-related changes (Item 3) are worth a separate flag — the paper finds those among
the more informative per unit of change.

### 6.8 Ownership, insiders, and short interest

All free and mostly structured, which makes this cheaper than expected.

| Source | Access | Lag | Notes |
|---|---|---|---|
| **Form 4** insider transactions | XML since 2003; quarterly bulk [Insider Transactions Data Sets](https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets) | **2 business days** | Fastest free ownership signal. Filter transaction codes carefully: P/S are open-market buys/sells; A/M/F/G are grants, option exercises, tax withholding and gifts, and are mostly noise. Rule 10b5-1 planned sales carry little information. Dedup 4/A amendments. |
| **13D/13G** 5% holders | **Structured XML since 2024-12-18** (Modernization of Beneficial Ownership Reporting) | 5 business days (13D) | Cover-page facts are tagged: reporting person, aggregate amount, percent of class, voting/dispositive power. Item 4 purpose remains free text. |
| **13F** institutional holdings | XML; [bulk data sets](https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets) | **45 days** | Long-only US equity positions — no shorts, no derivatives economics, no debt. SEC states the data is "as filed" and may contain inconsistencies. Positions may be fully unwound before publication. |
| **Short interest** | [FINRA Rule 4560 data](https://www.finra.org/finra-data/browse-catalog/equity-short-interest) — **bulk file downloads are auth-free; the Query API is not** (requires FINRA API credentials and an OAuth2 `client_credentials` bearer token from `https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token`). Use the downloads. Dataset is `equityShortInterestStandardized`; the older `equityShortInterest` stopped publishing 2021-04-30. | ~7 business days, **twice monthly** | **Revisions overwrite rather than append — you must snapshot to build point-in-time history.** Don't confuse with FINRA's daily short *volume* files, which measure something different. |

**Do not plan on SEC Form SHO.** Rule 13f-2 was adopted in 2023 but remanded by the Fifth
Circuit, and the compliance date has been extended to **January 2, 2028**. FINRA is the only
free source and will remain so.

### 6.9 Proxy statements (DEF 14A)

Adds a capital-allocation and incentive-alignment dimension the design otherwise lacks: is
management paid for things that build value, or for things that flatter a chart?

What a parser actually faces:

- **Pay Versus Performance (Item 402(v)) is inline-XBRL tagged** via the ECD taxonomy — the
  2022 rule (Release 34-95607), applying to fiscal years ending on or after 2022-12-16. This
  is the one structured numeric source in a proxy.
- **Everything else is untagged narrative HTML**: Summary Compensation Table, equity awards,
  CD&A, CEO pay ratio, beneficial ownership tables, audit fees, proposals. LLM extraction
  target, not a data feed.
- Beneficial ownership is **better sourced from 13D/G and Form 4 XML** (§6.8) than from the
  proxy's narrative table.

Flags worth deriving: compensation weighted toward metrics management controls
cosmetically (EPS with buybacks, adjusted EBITDA) over returns on capital; large equity
grants after a price decline; audit fees dropping sharply; low insider ownership alongside
heavy grant-based comp.

### 6.10 Refuse rather than guess

If the company is a bank/insurer (SIC 6000–6499), a REIT, or pre-revenue biotech, the
FCF-based DCF is structurally wrong. **Detect these and emit a report that says so**,
covering the quality/flags/peer sections but omitting the valuation. Same below 12 quarters
of history (§5.1). A blank space with an explanation beats a confident wrong number.

---

## 7. LLM layer

Optional. `--llm none` must produce a complete report minus the narrative sections; that
constraint keeps the LLM genuinely decoupled rather than nominally so.

### 7.1 Provider abstraction

```python
class LLMProvider(Protocol):
    name: str
    def complete_structured(
        self, prompt: str, schema: type[BaseModel], *, max_tokens: int
    ) -> BaseModel: ...
```

Adapters: `anthropic`, `openai`, `gemini`, `null`. Selected via config or `--llm`. Prompts
are versioned files under `llm/prompts/`, and the prompt version is recorded in report
metadata — otherwise output changes are unattributable.

Cursor is an IDE rather than an API surface, so it isn't a provider. If you want Investo
usable *from* Cursor or Claude Desktop, that's the MCP-server path — a separate wrapper,
noted as a later phase in ROADMAP.md.

### 7.2 The LLM's job, narrowly scoped

**Allowed:** extract and classify text from filings — risk factors ranked by materiality,
MD&A tone shift vs. prior year, competitive moat claims, litigation and regulatory
exposure, segment commentary, plain-language explanation of numbers already computed.

**Forbidden:** producing any number that appears in a chart or table, setting any forecast
input, or overriding a deterministic flag.

Enforcement is structural, not advisory: the LLM stage receives filing text and *may* read
computed metrics for context, but its output schema has **no numeric fields that feed
downstream**. The report renderer draws numbers only from the analysis stage. A prompt
regression therefore cannot corrupt the model.

### 7.3 Citation enforcement

Every extracted claim must carry an accession number and a verbatim quoted span. After
generation, **verify the quote appears in the source document**; drop claims that fail.
This is the anti-hallucination mechanism and it's mechanical rather than trust-based.

### 7.4 Chunking

A median 10-K runs ~70–95k tokens (Dyer/Lang/Stice-Lawrence put median length near 50k
*words* and rising); 200k–300k describes large complex filers — banks and insurers, i.e.
the issuers §6.10 refuses anyway. So with modern context windows, "it won't fit" is *not* the
reason to chunk. The reasons are cost and precision: split by Item (1, 1A, 3, 7, 7A, 8, 9A)
using regex on the standard headings, and send each prompt only the items it needs.

Item-heading regex is brittle across filers. Collect failures as fixtures rather than
chasing a general parser.

### 7.5 Cost and caching

Multiple Items × several prompts × repeated runs is a real bill, and the design should own a
number rather than leave it implicit: budget roughly **50–150k input tokens per report** at
default settings, and record actual spend in report metadata. LLM responses are cached on
`(prompt version, document hash, model id)`, which both cuts repeat cost to zero and brings
the LLM path under the §11 determinism gate.

### 7.6 Prompt injection

Filing text is **untrusted input**. A filer could embed adversarial instructions in a 10-K
— unlikely, but the cost of defending is near zero. Wrap document text in explicit
delimiters, instruct the model to treat it as data, and rely on the schema constraint in
§7.2: even a fully successful injection can't reach a number in the report. The renderer
needs its own defenses (§9.0) — hardening the prompt does nothing for the PDF.

---

## 8. Backtesting

**This is the difference between a real tool and a plausible-looking one, and it is the
component most likely to get cut under time pressure. Don't cut it.** Without it there is
no evidence any of §5 works, and the system's own output can't be calibrated.

`investo backtest --universe nasdaq100 --start 2015 --horizons 1y,2y,5y`

Mechanism: reconstruct state as of date `T` using only facts with `filed <= T` (§4.2b),
generate the forecast, compare against what actually happened.

Metrics that matter:

- **MAPE on revenue/FCF, always reported against a naive baseline.** A MAPE number alone is
  uninterpretable. The baselines are free and non-optional: random walk with drift, and
  last-4Q-annualized. If the model doesn't beat both, it has no value and the report should
  say so.
- **Directional hit rate** — did we get the sign of growth right? Often more useful than
  magnitude.
- **Calibration** — do 80% of outcomes land inside the P10–P90 band? Applied to the
  *fundamental* forecasts, which are falsifiable. Explicitly **not** applied to §5.5's
  implied-return output, which is model uncertainty and contains no market-shock term, so
  failing that check there would mean nothing.
- **Verdict performance** — do favorable verdicts outperform an equal-weight NASDAQ
  benchmark? Reported honestly, including when they don't.

**Effective sample size is much smaller than it looks, and this is the easiest way to fool
yourself here.** Forecast errors are strongly cross-sectionally correlated — all NASDAQ-100
names share macro shocks — so "80% coverage across 100 names at one date" is closer to
*one* independent observation than a hundred. Overlapping walk-forward windows compound it.
Required: block bootstrap over time, errors clustered by date, and effective-N reported
alongside every metric. And `--start 2015` with a 5y horizon yields roughly two
non-overlapping windows (2015–20, 2020–25) in two distinct regimes, so **the 5y evidence is
anecdotal by construction** and must be labeled that way rather than presented as measured.

Three lookahead leaks to guard against, each of which silently inflates results:

1. Restatements — handled by `filed <= T` filtering.
2. **Survivorship** — a universe of currently-listed tickers has the failures deleted, and
   there is no stable permanent identifier (no CRSP `PERMNO`) to recover them. Needs a
   historical index constituent list; if we can't source one, the backtest's optimism must
   be stated as a known limitation rather than quietly ignored.
3. Peer/sector medians computed from data after `T` — frames is not point-in-time stable, so
   peer stats need the same as-of treatment.

Calibration feeds back into §5 — but as a **model choice, not a fudge factor.** Inflating an
analytically-wrong interval until it passes in-sample is not calibration; if widths are
being set from data anyway, the honest version is to bootstrap them from backtest residuals,
which is why §5.2 specifies a stochastic-trend state-space model rather than an OLS interval
that would need post-hoc rescuing. If §5.2's specification still fails to calibrate here,
the fix is to change the model or bootstrap widths from these residuals — not to scale the
analytic interval until the test goes green. The report carries a footnote with measured historical
accuracy at each horizon.

---

## 9. Report

`matplotlib` charts, `Jinja2` → HTML, `WeasyPrint` (**≥69.0**, pinned) → PDF. Chosen
because the styling iteration loop is CSS rather than layout code.

### 9.0 Renderer risks

Three things that will otherwise be discovered the hard way.

**SVG is per-chart, not global.** WeasyPrint does render SVG as true vectors, but
matplotlib's SVG output lands squarely on WeasyPrint's weakest features, with a long and
still-active defect history: `clipPath` (matplotlib wraps all axes content in one — issues
#1374, #1595, #526), `<use xlink:href>` glyph references from the default
`svg.fonttype="path"` (#2375 broke referenced SVGs in v64; `use`-tag inheritance was only
fixed in 69.0), and alpha (#2332 — text in an SVG with alpha < 1 gets cut off, and every
fan chart is `fill_between(alpha=…)`). So: **SVG for simple bar and line charts; PNG at
300 dpi for anything using clipping or alpha** — fan chart, tornado, margin stack. Chart
format is a per-chart decision with PNG as the sanctioned fallback, and SVG is referenced
as a file, not a `data:` URI (#134).

**Untrusted text reaches the renderer.** §7.3 requires verbatim spans from 10-Ks to be
printed. Jinja2 does *not* autoescape unless `select_autoescape` is set, and WeasyPrint has
live CVEs in exactly this area:

- **CVE-2025-68616** (CVSS 7.5, fixed 68.0) — SSRF protection bypass via HTTP redirect:
  `urllib` follows redirects *without* re-invoking a custom `url_fetcher`. Note this defeats
  the very mitigation prescribed below on any pre-68.0 version, which is why the version pin
  does the real work here, not the fetcher.
- **CVE-2026-49452** (fixed 69.0) — CSS injection via presentational hints, explicitly scoped
  to rendering untrusted HTML.

Required: autoescape on, a **deny-by-default `url_fetcher`** so no remote resource is ever
resolved, presentational hints off, **WeasyPrint ≥69.0**. §7.6 hardens the prompt; this
hardens the renderer, and both are needed.

**Byte-identical output needs explicit config.** Both stages inject nondeterminism by
default: WeasyPrint writes `/CreationDate` and a document `/ID` (needs `SOURCE_DATE_EPOCH`),
and matplotlib SVG emits `<dc:date>` plus random glyph `id`s (needs
`rcParams["svg.hashsalt"]` pinned and `metadata={"Date": None}`). Note that a fixed
hashsalt can collide across multiple charts composed into one document — namespace per
chart. Without all of this the §11 determinism gate fails on day one.

### 9.1 Structure

1. **Cover** — ticker, name, as-of date, verdict badge, confidence rating, disclaimer.
2. **Verdict** — one page. Score decomposition, the 3 strongest reasons for, the 3
   strongest against, and what would change the conclusion.
3. **Company snapshot** — business description, sector, market cap, current multiples vs.
   peer percentiles.
4. **Historical performance** — revenue by year (bar + YoY growth line), margin stack,
   FCF vs. net income (the accruals story), balance-sheet trend, share count, ROIC vs.
   WACC.
5. **Forecast** — revenue fan chart (P10/P50/P90), driver assumptions table, DCF bridge,
   sensitivity tornado, implied-return distribution at each horizon.
6. **Quality & flags** — F/Z/M scores with peer context; efficiency ratio trends; flags
   sorted by severity, each with its source citation.
7. **What changed** — 8-K events in the window with severity (§6.6), year-over-year Item 1A
   and MD&A diffs (§6.7), insider transaction summary and short-interest trend (§6.8),
   compensation/incentive notes (§6.9). Content here is computed, not narrated — the diffs
   and event codes are deterministic, and the two LLM-refinable severities (§6.6) degrade to
   a capped rating under `--llm none` rather than disappearing.
8. **Narrative risks** — LLM section, every claim quote-attributed. Omitted under
   `--llm none`.
9. **Caveats** — data coverage table, methodology limits, measured backtest accuracy,
   what this report cannot tell you.
10. **Appendix** — full financial tables, tag provenance per metric, config used, prompt
    versions, cache manifest hash.

Section 9 is not boilerplate. It's where the report earns trust, and it should be written
to be read.

**On bull case / bear case.** Competing designs generate these by asking an LLM to write
them, which makes the LLM the analyst. Section 2 gets the same *structure* — strongest
reasons for and against — but composed from the computed flags, score components, and peer
percentiles, each carrying its source. The LLM may render that material into prose in
section 8; it does not decide what goes in it.

### 9.2 Verdict rubric

A single number is unaccountable, so the verdict decomposes:

| Component | Weight | Source | Raw → sub-score |
|---|---|---|---|
| Financial quality | 25% | F-score, margin stability, ROIC − WACC spread | F-score 0–9 linear to 0–100; spread percentile-ranked in cohort |
| Growth | 20% | revenue/FCF trend | peer-cohort percentile rank |
| Balance sheet | 15% | Z-score, net debt/EBITDA, interest coverage | Z-score banded **per variant** — original manufacturing Z: <1.81 / 1.81–2.99 / >2.99; non-manufacturing Z″: <1.1 / 1.1–2.6 / >2.6. Most of the NASDAQ universe is non-manufacturing, so banding everything against the original cut points would misclassify the majority. |
| Valuation | 25% | price vs. modeled value **band** | see below |
| Red flags | 15% | severity-weighted penalty | starts at 100, each flag subtracts by severity |

Every sub-score is 0–100; the composite is the weighted mean. Band cut points:
`AVOID` <30, `CAUTION` 30–45, `NEUTRAL` 45–60, `FAVORABLE` 60–75, `STRONG` >75. All of
this lives in config, and the report prints the weights and cut points it used.

**The valuation component scores against the band, not a point.** Scoring "price vs. P50
fair value" would make the verdict 25% driven by a single fair-value estimate — which is a
price target with extra steps, and would quietly reintroduce exactly what §1.1 rules out.
Instead the sub-score is a function of *where the current price sits in the P10–P90
distribution*: near P10 scores high, near P90 scores low, and **a wide P10–P90 band
compresses the sub-score toward 50 automatically.** A company we can't value precisely
therefore contributes little signal to the verdict rather than a confident one, which is
the correct behavior and falls out of the mechanism instead of needing a special case.

**Confidence is rated separately**, on a 0–100 scale from: metric coverage (§4.2), quarters
of history available, presence of data-integrity flags (§6.4), Monte Carlo rejection rate
(§5.5), and measured backtest calibration at the relevant horizon (§8). High score + low
confidence is a meaningfully different message from high score + high confidence, and
collapsing them into one badge would throw away the most useful part of the output.

---

## 10. Compliance and framing

Not legal advice; worth a conversation with someone qualified before this leaves your
machine.

- Every report carries a prominent disclaimer: educational and informational, not
  investment advice, not a recommendation to buy or sell, no advisory relationship.
- Avoid the word "recommendation" in output. "Screen result" and "assessment" are accurate
  and less loaded.
- **Personal use is straightforward. Publishing or selling this is a different question** —
  in the US, providing securities advice for compensation can implicate the Investment
  Advisers Act. Also relevant: Tiingo's and yfinance/Yahoo's free tiers are
  non-commercial, so distribution needs licensed data regardless.
- EDGAR data is public domain; cite the SEC, don't use its seal or trademarks.
- No PII is collected. API keys via env only, never committed, never logged.

---

## 11. Testing

| Layer | Approach |
|---|---|
| Normalize | Golden fixtures: real `companyfacts` JSON for ~15 companies with known-hard cases (Apple's ASC 606 stitch, a bank, a REIT, a recent IPO, a restater, a Q4-less filer). Assert exact expected series. |
| EDGAR client | Rate-limiter unit tests; `responses`-mocked retries; one opt-in live smoke test. |
| Forecast | Synthetic series with known CAGR → assert recovery within tolerance. Interval width must scale ≈√h (the local-level-with-drift prior, §5.2), not merely increase — monotonic widening is a near-vacuous assertion that a badly miscalibrated interval passes. Reinvestment-consistency and `g < WACC` constraints tested at their boundaries. |
| Quality scores | Hand-computed fixtures from real filings, cross-checked against a published screener. **Not** "worked examples from the original papers" — Piotroski (2000), Altman (1968) and Beneish (1999) are portfolio-sort and discriminant-function studies; none contains a per-firm step-by-step computation to copy. |
| Flags | One test per rule, positive and negative. |
| LLM | `null` provider in CI. Recorded-cassette tests for adapters. Citation verifier gets adversarial fixtures (fabricated quotes must be dropped). Injection fixtures with instructions embedded in filing text. |
| Report | Render golden fixtures to PDF, assert page count. Overflow detection has no WeasyPrint API — use `Document.pages` box geometry against the page box, or golden-image diffing. Autoescape and `url_fetcher` denial tested with hostile fixtures. |
| End-to-end | Full run from cache fixtures for 3 tickers; determinism assertion — two runs, identical output hash. |

Determinism is a CI gate: fixed MC seed, `SOURCE_DATE_EPOCH`, pinned `svg.hashsalt`, cached
inputs, and `--llm none` must produce a byte-identical PDF (§9.0). The LLM path is brought
under the same gate by caching responses keyed on `(prompt version, document hash, model
id)` — otherwise it would be permanently exempt, which is where regressions hide.

---

## 12. Known unmodeled items

Called out so they're deliberate omissions rather than oversights. Each is a candidate for
a later phase; several would materially move a valuation.

- **NOL carryforwards** and deferred tax assets — effective rate is used instead (§5.3).
- **Multi-class share structures** are summed, but differential voting rights and any
  associated control discount are ignored (§5.4).
- **Non-USD reporting currencies** — out of scope with the US-only universe, but the
  `ifrs-full` taxonomy work in ROADMAP's later phases needs it.
- **10-K/A amendments** — currently treated as ordinary filings by `filed` date; a material
  amendment should arguably supersede rather than append.
- **Fiscal-year misalignment in peer comparisons** — filers with different year-ends are
  compared on nearest-period basis, which introduces up to two quarters of skew.
- **Staleness** — if the latest 10-Q is more than 4 months old, the report flags it but does
  not extrapolate to fill the gap.
- **Pension obligations, contingent liabilities, off-balance-sheet structures.**

## 13. Open questions

Listed in ROADMAP.md § Open questions. The three that change the architecture rather than
the details: peer-cohort strategy, whether backtesting is in v1, and whether this stays
personal-use only.

## 14. Operational notes

- **Exit codes:** 0 success; 2 ticker not found or not NASDAQ; 3 insufficient data (report
  still written, valuation omitted); 4 upstream fetch failure after retries; 5 config error
  (e.g. missing User-Agent). A missing required metric degrades coverage and confidence — it
  does not abort.
- **Runtime target:** under 60s warm, under 5 min cold, excluding LLM calls.
- **License:** Investo's own license is an open question — see §10 on why distribution is
  not a purely technical decision.

# Investo — Build Plan

Companion to DESIGN.md. Phases are ordered so each one ends with something runnable.
Estimates assume one person working part-time; treat them as relative sizing, not commitments.

---

## Sequencing principle

Build the **data spine first, forecast last.** The temptation is to start with the DCF
because it's the interesting part. That's backwards — the DCF is a day of work on clean
data and a month of work on dirty data, and Investo's data is dirty by default (see
DESIGN.md §4.2). Every phase below leaves the repo in a state where `investo` does
something useful.

Corollary: the report renderer comes early, with fake data. Seeing a real PDF in week two
surfaces layout and content problems while they're still cheap to fix.

---

## M0 — Skeleton (~2 days)

**Goal:** `investo --help` works, CI is green.

- Repo scaffold: `pyproject.toml` (uv), `ruff`, **`basedpyright` in strict mode**, `pytest`,
  pre-commit. BasedPyright rather than `mypy --strict` as originally drafted, so this repo uses
  the same type checker as the sibling `tradipy` — one Makefile target, one CI step, one
  pre-commit hook. Not the same setting: tradipy runs `standard`, investo runs `strict`. M0's
  intent is unchanged — the CLI and config layer type-check under a strict setting from the
  first commit.
- `typer` CLI shell. **All** documented commands — `analyze`, `facts`, `fetch`, `cache prune`,
  `backtest` — with every README flag declared and parsed, and bodies that exit 70 naming the
  milestone that fills them in. Broader than the "analyze stub" first drafted, because the
  exit criterion below asks for the full flag surface and the surface is cheapest to review
  before anything depends on it.
- `pydantic-settings` config: TOML file + env override
- Global CLI surface: `--out`, `--refresh`, `--as-of`, `--cache-dir`, and the exit-code
  taxonomy from DESIGN.md §14 (these are cross-cutting, so they land here rather than in a
  feature milestone; `--refresh` and `--as-of` become meaningful in M1 and M2 respectively)
- GitHub Actions: lint, type, test
- `.env.example` with `INVESTO_SEC_USER_AGENT`, `INVESTO_TIINGO_KEY`, and
  `INVESTO_{ANTHROPIC,OPENAI,GEMINI}_KEY` — LLM keys use the `INVESTO_` prefix so config
  resolution has one convention rather than two

**Exit:** `investo --help` renders the full flag surface; clean `basedpyright` in strict mode;
CI green. (Not "strict on an empty codebase," which is vacuous — the gate is that the CLI
skeleton and config layer type-check.) Retrofitting strict typing later costs far more than
starting with it.

"Full flag surface" is checked against README § Usage in both directions, not asserted: a
documented flag the CLI lacks fails, and an accepted flag README omits fails too. The second
direction is the one that matters as the CLI grows — see `tests/test_cli_surface.py`.

---

## M1 — Ingest + cache (~1.5 weeks)

**Goal:** `investo fetch AAPL` writes raw payloads to cache and prints a summary.

- `domain/models.py`, `periods.py`, `provenance.py` — frozen dataclasses, `SourceRef`,
  `FiscalPeriod`. Built first; everything downstream types against them.
- `ingest/cache.py` — content-addressed, append-only, `fetched_at` + schema version
- `ingest/edgar/client.py` — token-bucket limiter at 5 req/s, mandatory User-Agent,
  gzip, backoff on 403/429, CIK padding and accession-format transforms owned here
- `tickers.py` — `company_tickers_exchange.json`, filtered to NASDAQ
- `companyfacts.py`, `submissions.py`, `frames.py` — typed parsers
- `documents.py` — fetch primary doc, split by 10-K Item headings
- `events.py` — 8-K item codes from `filings.recent.items` + body fetch (DESIGN.md §6.6)
- `ownership.py` — Form 4 XML, 13D/G XML, 13F; `finra.py` — short interest with snapshotting
  (revisions overwrite upstream, so history only exists if we keep it)
- `proxy.py` — DEF 14A fetch + pay-versus-performance iXBRL extraction
- **`ingest/prices/`** — `base.py` protocol, `tiingo.py` (default), `yfinance_.py`,
  `stooq.py`. Easy to forget, but prices gate report sections 3 and 4, the valuation
  component of the verdict, and M7's benchmark comparison.
- Market cap computed as `price × dei:EntityCommonStockSharesOutstanding` across classes

**Exit:** cold fetch for 5 tickers under the rate limit; warm run makes zero HTTP calls;
startup fails loudly if User-Agent is unset; price provider swappable via config with all
three adapters returning identical schemas.

**Risks:** the accession/CIK padding inconsistency (padded on `data.sec.gov`, unpadded in
`/Archives/`, dashed vs. undashed) causes 404s that look like missing data. Unit-test the
transforms in isolation. Item-heading regex is brittle across filers — collect failures as
fixtures rather than chasing generality up front.

---

## M2 — Normalization (~1.5 weeks) ⚠️ hardest phase

**Goal:** `investo facts AAPL --lookback 5y` prints clean annual + quarterly statements
with a coverage report.

- `normalize/tags.py` — ordered fallback chains, **both tiers** from DESIGN.md §4.2: the
  DCF metrics *and* the ~10–15 additional chains the M4 quality scores need
  (`AssetsCurrent`, `LiabilitiesCurrent`, `RetainedEarningsAccumulatedDeficit`,
  `AccountsReceivableNetCurrent`, COGS, SG&A, D&A, interest expense, SBC, lease
  liabilities, share issuance). Building only the first tier means M4 stalls.
- `normalize/facts.py` — dedup by `(unit, start, end)`; `as_of` filtering on `filed`;
  duration bucketing (annual 350–380d, quarterly 80–100d, YTD dropped/differenced);
  Q4 derivation
- `normalize/statements.py` → `FinancialHistory` + `CoverageReport` with per-metric
  provenance
- ASC 606 revenue stitching across the 2018 boundary
- `report/serialize.py` → `report.json` (DESIGN.md §4.5); creates the `report/` package that
  M3 then fills in
- Golden fixtures: Apple, a bank, a REIT, a recent IPO, a restater, a Q4-less filer

**Exit:** ≥90% coverage across 20 NASDAQ names on **both** the DCF metric set and the
quality-score metric set; every fixture's expected series asserted exactly; `as_of`
demonstrably excludes later restatements.

**Risks:** this phase is where schedule slips. The traps in DESIGN.md §4.2 are confirmed
against live data, not hypothetical — `fy`/`fp` meaning the *filing's* fiscal year rather
than the fact's, quarterly restatements appearing four times, and discrete Q4 sometimes
never tagged (varies by issuer *and* by year within the same issuer). Budget generously.
If it runs long, cut M6 scope, not this.

---

## M3 — Report shell (~1 week)

**Goal:** `investo analyze AAPL` emits a real PDF with real historical charts and no forecast.

- `report/charts.py` — matplotlib, **per-chart SVG-or-PNG choice** per DESIGN.md §9.0
  (revenue + YoY, margin stack, FCF vs. net income, balance sheet, share count)
- `report/render.py` — Jinja2 (**autoescape on**) → HTML → WeasyPrint ≥69.0 with a
  deny-by-default `url_fetcher` and presentational hints off
- Determinism config up front: `SOURCE_DATE_EPOCH`, pinned per-chart `svg.hashsalt`,
  `metadata={"Date": None}`
- Sections 1, 3, 4, 9, 10 (cover, snapshot, history, caveats, appendix)
- Disclaimer on the cover, coverage table in caveats
- `--brief` variant

**Exit:** a PDF you'd actually read. Every number traceable to a `SourceRef` in the
appendix. Two runs produce a byte-identical file.

**Risks:** the matplotlib→WeasyPrint seam (DESIGN.md §9.0) is the largest un-hedged
implementation risk in the project — clipPath, `<use>` glyph refs, and alpha are all
long-standing WeasyPrint weak spots and matplotlib emits all three. Build the margin stack
and a `fill_between` chart **first**, as a spike, before committing to SVG anywhere.

**Why here:** deliberately before the forecast. A real artifact this early exposes content
and layout gaps cheaply, and gives the project a usable output long before the model exists.

---

## M4 — Fundamentals, quality, flags (~1 week)

**Goal:** the report gains a genuinely useful analysis section — and becomes worth using
even with no forecast at all.

- `analyze/fundamentals.py` — growth, margins, ROIC, working-capital ratios
- `analyze/quality.py` — Piotroski F, Altman Z (variant by SIC), Beneish M
- `analyze/efficiency.py` — asset/inventory/receivables turnover, cash conversion cycle
- `analyze/flags.py` — rule registry, one file per rule (DESIGN.md §6.2)
- `analyze/peers.py` — SIC cohort via frames API, percentile ranks, `--peers` override
- Data-integrity flags wired to the confidence rating
- Sector refusal logic: banks/REITs/pre-revenue get quality + flags, no valuation

**Exit:** flag rules validated against known cases — a company with a going-concern
qualification, one with heavy dilution, one with a restatement. Scores match hand-computed
fixtures cross-checked against a published screener (the original F/Z/M papers contain no
per-firm worked computation to copy — see DESIGN.md §11).

**Note:** a pure red-flag scanner with no forecast is already a real tool, and nothing here
depends on a model being right.

---

## M4.5 — Events, diffs, ownership (~1.5 weeks)

**Goal:** report section 7, "What changed" — the part that catches things a human skims past.
Added after reviewing a competing spec whose best ideas were all in this area.

- `analyze/events.py` — 8-K item codes → severity. **Item 4.02 (non-reliance on previously
  issued financials) is the highest-severity flag in the system**, unconditionally; then 4.01
  auditor change, 2.06 impairment, 3.01 delisting notice, 5.02 officer departure, 1.05 cyber,
  1.03 bankruptcy. Recurring 2.05 restructuring is its own pattern flag.
- `analyze/diffs.py` — year-over-year Item 1A and MD&A similarity + readable diff, as a
  **flag generator, not a return signal** (DESIGN.md §6.7 has the evidence and its limits)
- Insider transaction summary from Form 4 — filter to open-market P/S codes; grants,
  exercises, tax withholding and 10b5-1 sales are noise
- 13D/G 5% holders, 13F institutional trend, FINRA short-interest trend
- DEF 14A: pay-versus-performance from iXBRL, comp-structure flags from narrative
- Earnings surprise computed as **context only** — PEAD has largely decayed outside microcaps
- Report section 7 rendered; bull/bear structure in section 2 composed from computed flags

**Exit:** 4.02 and 4.01 detected on known real cases. Diff engine flags a company with
materially rewritten risk factors and stays quiet on one with boilerplate-only changes —
this discrimination is the whole feature, and a diff that fires on everything is useless.

**Risks:** 4.01 and 5.02 are ambiguous by item code alone — 5.02 covers both CEO departures
and routine compensation amendments. Refining their severity needs the 8-K body read, and the
`llm/` layer doesn't exist until M6. Resolved by design rather than resequencing: both flags
fire deterministically at a capped severity with "unclassified, read the filing," and M6
sharpens them later (DESIGN.md §6.6). **No part of M4.5 blocks on M6**, which is what keeps
M0–M4.5 a coherent standalone release.

---

## M5 — Forecast engine (~1.5 weeks)

**Goal:** 1y/2y/5y ranges in the report.

All paths under `analyze/forecast/`.

- `trend.py` — **local-level-with-drift** state space (`level='rwdrift'`) on log revenue with
  seasonal terms, giving √h interval scaling; HAC errors; deceleration test gated at ≥20
  quarters. Deliberately *not* local linear trend — see DESIGN.md §5.2 for why (h^1.5 scaling
  makes 5-year intervals useless).
- `drivers.py` — revenue → margin (soft-bounded) → FCFF, SBC as real cost, ASC 842 leases,
  terminal capex → D&A + growth capex
- Mean-reversion fade toward sector median, terminal growth **capped** at nominal GDP,
  tunable half-life; fade applied per-path so intervals travel with the central case
- `dcf.py` — two-stage; both Gordon and exit-multiple terminals side by side; CAPM cost of
  equity → WACC with cost of debt and market-value weights; shrunk beta; explicit
  EV→equity→per-diluted-share bridge; terminal reinvestment consistency check
- `mc.py` — 10,000 draws, fixed seed, `g < WACC` enforced, rejection rate reported. **One**
  engine feeding both the fan chart and the valuation quantiles.
- Fan chart, sensitivity tornado, assumptions table (printing the default exit multiple and
  its peer-cohort provenance)
- `analyze/score.py` — weighted rubric, band-based valuation sub-score, cut points, separate
  confidence rating
- `--assumptions` override file and `--explain` dump

**Exit:** synthetic series with known CAGR recovered within tolerance; **interval width
scales ≈√h** (a monotonic-widening test is near-vacuous and a miscalibrated interval passes
it); reinvestment and `g < WACC` constraints tested at their boundaries; two runs with the
same seed produce a byte-identical PDF.

**Risks:** the fade half-life is the parameter most likely to be quietly overfit. Set it
from theory (sector-median convergence) and leave it alone until M7 can justify a change.
Second risk: intervals from this model still may not be calibrated — M7 decides, and the
answer may be to bootstrap widths from backtest residuals rather than trust the analytic
form.

---

## M6 — LLM enrichment (~1 week)

**Goal:** `--llm anthropic` adds narrative risk analysis; `--llm none` still produces a
complete report.

- `llm/provider.py` protocol + registry; adapters for anthropic, openai, gemini, null
- Versioned prompts under `llm/prompts/`, version recorded in report metadata
- `llm/extract.py` — pydantic schemas, no numeric fields that feed downstream
- Citation verifier: reject any claim whose quote isn't found verbatim in the source
- Item-level chunking (1A, 3, 7, 7A) — for cost and precision, not context limits
- Response cache keyed on `(prompt version, document hash, model id)`, so the LLM path stays
  inside the determinism gate instead of being exempt from it
- Token spend recorded in report metadata against a ~50–150k/report budget
- Injection hardening: delimited untrusted text, data-not-instructions framing

**Exit:** `--llm none` produces a complete report; citation verifier drops fabricated
quotes in adversarial fixtures; all three adapters pass recorded-cassette tests; a filing
fixture containing embedded instructions changes no number in the PDF.

---

## M7 — Backtesting (~1.5 weeks)

**Goal:** evidence that any of M5 works.

- `backtest/asof.py` — point-in-time reconstruction, `filed <= T`
- `backtest/runner.py` — walk-forward over a universe and date grid
- `backtest/metrics.py` — MAPE **vs. random-walk-with-drift and last-4Q-annualized
  baselines** (a bare MAPE is uninterpretable), directional hit rate, interval calibration,
  verdict vs. equal-weight benchmark
- Block bootstrap over time, errors clustered by date, **effective-N reported next to every
  metric** — cross-sectional error correlation means 100 names at one date is closer to one
  observation than a hundred
- Peer/sector medians recomputed as-of (frames is **not** point-in-time stable)
- Feed calibration back into M5 — by re-deriving interval widths from residuals, not by
  inflating an analytic interval until it passes in-sample
- Print measured accuracy, effective-N, and baseline comparison in report section 9 (Caveats)

**Exit:** calibration measured and reported, with the 5y result labeled anecdotal (a 2015
start yields ~2 non-overlapping 5y windows, in two different regimes). Model must beat both
naive baselines on 1y and 2y MAPE, or the report says it doesn't.

**Risks:** survivorship bias needs a historical index-constituent list, and there's no
permanent identifier to recover delisted names. If one can't be sourced, state the resulting
optimism as a known limitation in the report rather than letting it silently inflate the
numbers.

**This is the phase most likely to get cut, and cutting it means the confidence numbers in
every report are decoration.**

---

## Considered and rejected

From a review of a competing platform-style spec (2026-07-31). Recorded so these don't get
re-proposed without new information. Bare `§` references below are to DESIGN.md.

| Rejected | Why |
|---|---|
| **LLM-generated investment thesis, bull/bear case, and confidence score** | Makes the LLM the analyst. A spec can't claim to separate "structured analytics from LLM reasoning" while having the LLM write the thesis and score its own confidence. We keep the report *structure* and compose it from computed flags (DESIGN.md §9.1). |
| **Natural-language query interface** | Text-to-SQL over data where "revenue" spans four competing XBRL tags with no majority (§4.2) produces answers that are wrong in ways nobody can see. A screening DSL with explicit tag selection is strictly better and much cheaper. |
| **Embeddings + semantic search over all filings** | EDGAR full-text search already covers retrieval for free. A vector store adds infrastructure and embedding cost for marginal gain at single-company scope. |
| **API + auth + multi-tenant platform, sub-2s SLAs, 4,000-company coverage** | This is what turns two months into eight, and it front-loads plumbing before a single defensible report exists. Also: latency targets asserted without reference to the work being done aren't requirements, they're wishes. |
| **Personalized recommendations** | Not a feature — that's the Investment Advisers Act line (DESIGN.md §10). |
| **Revenue per employee** | Looks like a ratio, is actually a narrative extraction pipeline. `dei:EntityNumberOfEmployees` has 5–12 filers a year and wrong values (§6.3). |
| **Guidance vs. actual** | Guidance is never XBRL-tagged and has no free structured source; normalizing ranges, point estimates and directional language requires judgment. That's why vendors charge for it (§6.6). |
| **Earnings surprise as a signal** | Demoted to context. PEAD is largely gone outside microcaps — Martineau (*CFR* 2022); recent revival claims don't exclude microcaps, and doing so drops t from 2.18 to 1.43 (§6.6). |
| **"AI adoption" as an analysis dimension** | A 2026 fashion artifact, not an analytical primitive. Competitive moat and reinvestment already cover the substance. |

Two things that spec got right and this one had wrong, both now fixed: **8-K event monitoring**
(Item 4.02 is the loudest accounting red flag there is, and it was missing) and **filing
diffs**. A third it got right in ambition: **10+ years of history** — longer windows cover a
real cycle, and the ASC 606 stitching problem is a difficulty to solve rather than a reason to
stay at five years.

---

## Later (unscheduled)

- **MCP server** — expose Investo as tools so Claude Desktop / Cursor can drive it. Natural
  fit once the core is stable; inverts control relative to the provider abstraction.
- **FastAPI + web UI** — ticker box, job queue, report download.
- **Watchlist + scheduled reruns** — diff two reports, alert on material change.
- **NYSE, then non-US** (20-F/40-F, `ifrs-full` taxonomy — a real normalization project).
- **Sector-specific models** — banks (NIM, credit provisions), REITs (FFO/AFFO), biotech
  (pipeline-stage), replacing M4's refusal path.
- **Bulk mode** via nightly `companyfacts.zip` for whole-market screening.
- **Earnings-call transcripts** as an LLM input (needs a licensed source).

---

## Rough total to a genuinely useful v1

M0–M5 ≈ **8.5–9.5 weeks part-time**. M6 and M7 add ~2.5 weeks.

Minimum defensible release is **M0–M4.5**: no forecast, but accurate normalized financials,
quality scores, efficiency trends, red flags, 8-K event detection, filing diffs, ownership
data, peer context, and a clean PDF. That's shippable and honest — and arguably the most
useful-per-week configuration in the whole plan, since none of it depends on a model being
right. A forecast without M7 is a forecast with unvalidated error bars: fine for personal use
if labeled as such, not fine to share.

---

## Decided during design

Recorded so they don't get re-litigated. Each is written into DESIGN.md.

- `--lookback` sets the **estimation window only**; it does not select horizons (§5.1). The
  horizon set is 1y/2y/5y *as currently drafted* — whether 1y survives is open question 4.
- **No target price.** Output is an implied-return distribution under stated assumptions,
  and the verdict's valuation component scores against the P10–P90 band rather than a P50
  point, so a wide band automatically contributes little signal (§9.2).
- The exit multiple gets a **default from the peer cohort**, printed with its provenance and
  overridable — not "no opinion" (§5.7).
- **Below 12 quarters of history, the valuation is omitted**, same as for banks/REITs (§6.10).
- `--assumptions`, `--explain`, `--brief` and `report.json` are **in scope** (M2/M3/M5), not
  optional extras — `--explain` in particular is what makes the numbers trustworthy. The full
  report is the default; `--brief` is the opt-in.
- `investo diff` is **not** in scope for v1, but `report.json` is designed so it's cheap to
  add later.
- Everything is **median-based**, stated explicitly rather than corrected for
  retransformation bias (§5.2).

Decided while building M0:

- **`src/` layout** (`src/investo/`), where DESIGN.md §3.1 draws a flat `investo/`. Stops tests
  importing the working tree instead of the installed package — the failure mode where CI is
  green on code that is not packaged. §3.1's tree is otherwise unchanged.
- **The §3.1 module tree is created per milestone, not up front.** An empty package cannot be
  meaningfully type-checked or tested, and goes stale if the design moves before it is filled.
- **Exit 70 for an unimplemented command**, deliberately outside §14's range so it can never be
  confused with a real outcome — in particular not with exit 3, which promises a written
  report. It disappears with the last stub.
- **A CLI flag that mirrors a config field defaults to `None`.** A typer default is
  indistinguishable from a value the user typed, so `--lookback` defaulting to `5y` would
  outrank `lookback` in the config file on every run and make that setting dead config that
  appears to work.
- **Bad flag values exit 5, not 4.** Exit 4 promises "upstream fetch failure after retries";
  reporting it for a malformed `--as-of` would misstate where the run stopped.
- **Python 3.13, not "3.12+"** as DESIGN §2 first recorded. Nothing written so far needs 3.13
  over 3.12 — it is the same call as BasedPyright: one runtime across this repo and the sibling
  `tradipy`, so a snippet moves between them without a version question. Cheap now and
  progressively less cheap later, since dropping back means re-testing every dependency added
  since. Revisit only if investo is ever distributed, where a floor that high costs users
  (open question 3).
- **A typer callback is not underscore-prefixed.** `reportUnusedFunction` reads a leading
  underscore as "private, expect a reference in this file", and a callback's only reference is
  its `@app.callback()` registration — so `_root` failed strict type-checking while every
  identically-registered command function passed. Named `root`, like its siblings.

---

## Open questions

Ordered by how much the answer changes the build.

### Architectural

1. **Is backtesting (M7) in v1?** If no, every confidence rating and interval in the report
   is unvalidated and must be labeled that way in **report section 9, Caveats** (DESIGN.md
   §9.1).
2. **Peer cohort strategy?** SIC codes are coarse and sometimes plain wrong, and the cohort
   now feeds more than context — it sets the fade target (§5.2) and the default exit
   multiple (§5.7), so cohort quality propagates straight into the valuation. Options: SIC
   only (free, imprecise), manual `--peers` per company (accurate, manual), or a curated
   sector map.
3. **Personal use only, or eventually shared?** Drives data licensing (Tiingo and
   yfinance/Yahoo free tiers are non-commercial), the regulatory question in DESIGN.md §10,
   Investo's own license, and whether yfinance can stay past prototyping. Also note the FINRA
  Query API needs credentials even though the bulk files don't.

### Scope

4. **Is 1-year output worth including?** It's near-noise (DESIGN.md §5.6), and including it
   risks it becoming the number people anchor on. Options: include with a warning, drop it,
   or show it only as a multiple sensitivity rather than a forecast.
5. **Equity risk premium source** — a config constant, but which one? Damodaran's implied
   ERP is the usual choice and is published free; a historical average is simpler and less
   defensible. This single number moves fair value materially.
6. **How much of the peer cohort's data to cache?** Whole-cohort frames pulls are the
   heaviest API usage in the system, and M7 needs them recomputed as-of at every backtest
   date, which multiplies the cost.
7. **Historical index constituents** — required to fix survivorship bias in M7. Is there a
   free source you'd accept, or do we ship the backtest with a stated optimism caveat?

### Detail

7a. **Exit code 2 is shared with Click's usage error.** DESIGN.md §14 assigns 2 to "ticker not
    found or not NASDAQ", but Click — which typer is built on — already exits 2 for a usage
    error, and that is not configurable without subclassing its exception machinery. So
    `investo analyze NOTATICKER` and `investo analyze --bogus-flag` are indistinguishable by
    exit code, though not by their stderr. Implemented as designed rather than quietly
    renumbered, with `tests/test_errors.py` pinning the collision so it stays visible. Options:
    accept and document it, or move `TICKER_NOT_FOUND` to 6. Matters only if something is ever
    going to branch on the code — a shell wrapper, or M7's batch runner.

8. **Analyst consensus estimates** — the real bar for a forecast is beating consensus, not
   beating a random walk. Not free. Worth a paid API?
9. **Insider transactions and institutional holdings** (Forms 4, 13F) — free on EDGAR,
   moderate signal, meaningful extra parsing work. In or out?
10. **Restatement display** — when a series is stitched across ASC 606 or contains restated
    periods, show both versions or just the current one?
11. **Restatement and stitch transparency in the appendix** — how much provenance detail is
    useful before it becomes noise?

### Suggested additions still undecided

- **`--compare TICKER,TICKER`** — one report, side-by-side. Comparison is how investment
  decisions actually get made, and a single-name report leaves the user doing the hard part.
  Probably the highest-value addition on this list.
- **Report diffing** — `investo diff old.json new.json`. Once you rerun quarterly, what
  *changed* matters more than the current level. Cheap now that `report.json` exists, but
  deliberately out of v1 scope.
- **Watchlist mode** — batch several tickers, emit a ranked one-page screen rather than
  full reports.

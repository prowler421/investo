# M1 — Ingest + cache

Status: **built.** See ROADMAP M1 for what landed and the three things that did not — fixture
curation, the `files[]` field-name confirmation, and the coverage floor.
Last updated: 2026-07-31

Design for ROADMAP M1. `DESIGN.md` and `ROADMAP.md` remain normative; this document is
subordinate to both. Where it proposes something they do not say, it says so and asks — it does
not decide. Those asks are collected in [§ Spec questions](#7-spec-questions) and none of them
should be resolved in code.

Read in order:

| File | Covers |
|---|---|
| [`01-domain-types.md`](01-domain-types.md) | `domain/models.py`, `periods.py`, `provenance.py` — the types everything downstream is written against |
| [`02-cache.md`](02-cache.md) | `ingest/cache.py` — on-disk format, append-only semantics, `--refresh`, `cache prune`, the manifest hash |
| [`03-edgar-client.md`](03-edgar-client.md) | `ingest/edgar/client.py` — token bucket, User-Agent, retry policy, the CIK/accession transforms, choke-point enforcement |
| [`04-parsers.md`](04-parsers.md) | `tickers`, `companyfacts`, `submissions` (M1a); `frames`, `documents`, `events`, `ownership`, `proxy`, `finra` (M1b) |
| [`05-prices.md`](05-prices.md) | `ingest/prices/` — the provider protocol, three adapters, market cap |
| [`06-testing.md`](06-testing.md) | fixture strategy, the guarantee→violation-test table, markers, the coverage floor |

---

## 1. What M1 delivers

ROADMAP M1's goal: `investo fetch AAPL` writes raw payloads to cache and prints a summary.

Nothing in M1 interprets a financial figure. M1 fetches bytes, records where they came from and
when, and turns them into typed rows keyed by **XBRL tag**. Choosing which tag answers to
"revenue" is `normalize/tags.py`, and that is M2. The seam is enforced, not merely intended —
see [§ The M1/M2 seam](#5-the-m1m2-seam).

### The four exit criteria, and where each is tested

ROADMAP M1 states four. All four are met by M1a alone, which is the load-bearing fact behind the
slicing proposal below.

| Exit criterion | Test |
|---|---|
| Cold fetch for 5 tickers under the rate limit | `test_client_ratelimit.py::test_five_ticker_cold_fetch_respects_bucket` — injected clock, asserts elapsed ≥ (n−1)/rate and that no two request timestamps are closer than 1/rate |
| Warm run makes zero HTTP calls | `test_cache_warm.py::test_second_fetch_makes_no_requests` — respx router with zero registered routes; any request raises |
| Startup fails loudly if User-Agent is unset | `test_config.py` (exists, M0) extended: `test_fetch_without_user_agent_exits_5`, asserting exit 5 **and** zero requests attempted |
| Price provider swappable via config, all three adapters returning identical schemas | `test_prices_contract.py` — one contract test parameterized over all three adapters |

The fourth deserves a note: "identical schemas" is not "identical content." Stooq supplies no
adjusted close, and the adapter reports `adj_close=None` rather than aliasing `close` into it.
Aliasing would satisfy a naive schema check while silently feeding unadjusted prices into a beta
estimate — the precise class of failure this project exists to avoid. The contract test asserts
the `None`.

---

## 2. Proposed slicing: M1a and M1b

**This is a proposal for sign-off, not a decision.** ROADMAP is normative on sequencing.

ROADMAP M1 lists thirteen modules for ~1.5 weeks: domain (×3), cache, EDGAR client, tickers,
companyfacts, submissions, frames, documents, events, ownership, proxy, finra, and three price
adapters. The proposal is to keep all of it in M1's design — this document — and split its
delivery.

### M1a — the spine everything downstream blocks on

```
domain/{models,periods,provenance}.py
ingest/cache.py
ingest/edgar/{client,tickers,companyfacts,submissions}.py
ingest/prices/{base,tiingo,yfinance_,stooq}.py
market_cap()                          # domain/models.py, pure
investo fetch TICKER                  # working, summary printed
```

Satisfies all four ROADMAP M1 exit criteria. Unblocks M2 (normalization) and therefore M3 (the
report shell) completely.

### M1b — the feeds only M4/M4.5 consume

```
ingest/edgar/frames.py                # needed by M4 analyze/peers.py
ingest/edgar/{documents,events,ownership,proxy}.py
ingest/finra.py
```

Nothing in M2, M3 or M4 except `peers.py` reads any of these. **M1b must land before M4** (for
`frames.py`) and the rest before M4.5.

### Why

ROADMAP's own sequencing principle is "build the data spine first" and "each phase ends with
something runnable," and its stated reason for putting M3 before the forecast is that *"a real
artifact this early exposes content and layout gaps cheaply."* M1b produces no artifact any
earlier milestone renders. Deferring it moves roughly a week off the critical path to the first
real PDF, which is the thing ROADMAP says it wants soonest.

### The honest counter-argument

ROADMAP M1 names two risks: the CIK/accession padding inconsistency, and the brittleness of the
10-K Item-heading regex across filers. Deferring `documents.py` defers discovery of the second
one, and "collect failures as fixtures rather than chasing generality" is cheaper the earlier it
starts.

Partial mitigation, not a full answer: M1a exercises every URL transform, including
`/Archives/` primary-document URL construction, because `submissions.py` resolves
`primaryDocument` into a URL even though M1a never fetches the body. So the padding risk — the
one that produces 404s that look like missing data — is fully retired in M1a. The regex risk is
genuinely deferred. If that trade is unacceptable, the alternative is to pull `documents.py`
alone forward into M1a and leave the rest in M1b.

### Sizing — M1a is ~2 weeks, not "the fast half of 1.5"

ROADMAP budgets M1 at ~1.5 weeks part-time. That estimate predates this design, and the design
added machinery that is justified but was not in the original count: a two-phase prune, a
fake-clock token bucket and retry matrix, an AST layering enforcer, a type-level test harness, a
manifest-hashing scheme, and two parse functions for submissions rather than one.

So the split is not a claim that M1a is cheap. It is a claim that M1a is *the part that
unblocks M2*. Sized honestly:

| Workstream | Estimate | Notes |
|---|---|---|
| `domain/` — models, periods, provenance | 1.5 d | Small surface, but `Derivation` and the two share-count types are decisions that touch everything after |
| `ingest/cache.py` + prune | 2 d | The prune ordering and `manifest_hash` are the fiddly parts, not the store |
| `ingest/edgar/client.py` | 2 d | Token bucket, retry matrix, transforms, the 403 classifier — with the fake clock, most of this is test code |
| `tickers` + `companyfacts` + `submissions` | 2.5 d | Two parse functions for submissions, and the empty-string/`null` normalization the live payload exposed |
| `ingest/prices/` ×3 + contract test | 1.5 d | Tiingo is simple; the yfinance partial-history check and the three-way contract test are the work |
| `market_cap` + `fetch` command + summary | 1 d | |
| Layering + typing test harnesses | 1 d | One-off cost, paid once for every later milestone |
| **Fixture curation** | **3 d** | **Own workstream — see below** |
| **Total** | **~14.5 days** | ≈ **2.5–3 weeks part-time** |

That is roughly double ROADMAP's figure for M1 as a whole, and M1b is on top. The estimate is
worth having even if it is wrong, because the alternative is discovering it in week three.
Recorded in ROADMAP § Decided during design if accepted.

### Fixture curation is research, not coding, and it is the schedule risk

Three days above, and it is the line most likely to be wrong in the bad direction.

The six hard-case fixtures each have to *provably* exhibit a specific trap. "Apple's FY2018
revenue is tagged `fy: 2019` and again `fy: 2020`" is a claim about a real payload that has to be
found, fetched, and verified by hand before `reduce_fixture.py` is worth writing. Finding a
filer that never tags discrete Q4 — and specifically one that does so inconsistently across
years, which §4.2(c) says is the real behaviour — means searching, not typing. The same goes for
a restater with four `filed` dates on one period, and for a NASDAQ bank whose SIC lands in
6000–6499.

Three properties make this its own workstream rather than a task inside "write the parsers":

- **It is unblocked by everything.** It needs the EDGAR client only in the sense that a browser
  would do. It can start on day one, in parallel, and it should.
- **It is done when a claim is verified, not when code runs.** There is no green test to tell
  you it is finished, which is exactly how a task silently eats three days while the code looks
  complete.
- **Its output is reusable and its absence is blocking.** M2's exit criterion — ≥90% coverage
  across 20 NASDAQ names on both metric tiers — is not assessable without it, and M2 is the
  phase ROADMAP already identifies as where the schedule slips.

Concretely: each fixture lands as a payload plus a one-paragraph note in
`tests/fixtures/edgar/PROVENANCE.md` stating the ticker, the accession, the trap, and the line in
the payload that demonstrates it. A fixture whose trap cannot be pointed at is a fixture that is
testing nothing, and it is better to discover that while curating than in M2.

---

## 3. `investo fetch` — the command surface

No new flags. `fetch` already accepts `--refresh`, `--cache-dir` and `--config` (M0), and that
is the whole surface it needs.

**How far back it fetches:** from `settings.lookback`, resolved by the existing
`load_settings` call in the command body. Not from a new `--lookback` flag on `fetch`, because
README § Usage documents `--lookback` on `analyze` and `facts` only, and adding it here would
require a README line and a `_FLAG_OWNER` entry (CLAUDE.md convention 5) to buy something the
config file already provides. `investo fetch AAPL` fetching the configured window is the
expected reading.

`fetch` also needs the window for a reason that is not obvious: the submissions endpoint
paginates, and how many pages to pull is a function of the window. See
[§ Spec question 1](#7-spec-questions).

### Summary output

Human-readable only. A `--json` variant is deliberately not added — it would need the README
line and the `_FLAG_OWNER` entry, and `report.json` (M2) is the machine-readable surface this
project already committed to.

```
investo fetch AAPL

AAPL  Apple Inc.  CIK 320193  Nasdaq  SIC 3571  FY end 0928

  source                     status     bytes    fetched_at
  tickers_exchange           cached     1.1 MB   2026-07-30T09:14:02Z
  submissions                fetched    2.4 MB   2026-07-31T11:02:18Z
  submissions +2 pages       fetched    3.1 MB   2026-07-31T11:02:19Z
  companyfacts               fetched   38.7 MB   2026-07-31T11:02:21Z
  prices (tiingo)            fetched    0.4 MB   2026-07-31T11:02:22Z

  absent
  companyfacts: no us-gaap:GrossProfit in any period
  DEF 14A: none filed in window          (M1b)

  17 requests · 5.0 req/s cap · 4.8s · manifest 9f2c1ab4
```

Three properties of that output are load-bearing rather than cosmetic:

- **`absent` is a section, not an error.** A 404 or a missing tag is an *absence*, recorded and
  printed. Whether an absence is fatal depends on what needs it, which is not `fetch`'s
  question. See [§ Exit codes](#4-exit-codes).
- **`fetched_at` is printed per source**, because a warm run's value is the whole point of the
  cache and a stale entry the user cannot see is a stale entry they will trust.
- **`manifest` is the hash of the entries this run read**, not of the whole cache. See
  [§ Spec question 4](#7-spec-questions).

---

## 4. Exit codes

No new codes. `errors.ExitCode` (M0, DESIGN §14) covers M1 exactly, which is worth confirming
because a milestone that needs a sixth code is a milestone that has misread §14.

| Condition | Code | Class |
|---|---|---|
| Ticker absent from `company_tickers_exchange.json` | 2 | `TickerNotFoundError` |
| Ticker present, exchange is not NASDAQ | 2 | `TickerNotFoundError` |
| `sec_user_agent` unset or invalid | 5 | `ConfigError` |
| HTTP 403 with SEC's undeclared-automated-tool body | 5 | `ConfigError` |
| Unknown `price_provider` in config | 5 | `ConfigError` |
| Retries exhausted on 429/5xx, or transport error | 4 | `UpstreamFetchError` |
| Price series returns implausibly few rows (yfinance partial-history) | 4 | `UpstreamFetchError` |
| 404 on any payload | — | not an error; recorded as an absence |
| A tag has no facts | — | not an error; recorded as an absence |

Two of these are design calls rather than readings.

**The undeclared-tool 403 is exit 5 and is never retried.** SEC returns 403 both for a missing
User-Agent and for throttling, so the class is decided by the response body. Retrying an
undeclared-tool 403 cannot succeed and does burn the rate budget, and DESIGN §4.1 notes the
penalty is not only ours to pay. `ConfigError` also carries the true statement about where the
run stopped, which exit 4 would not.

**404 is an absence, not a fetch failure.** DESIGN §14's governing distinction is between a run
that failed and a run that succeeded in reporting bad news. A NASDAQ filer with no
`companyfacts` has told us something true; reporting exit 4 would claim the network broke.
`fetch` exits 0 and prints the gap. `analyze`, which needs the data, raises
`InsufficientDataError` — exit 3, report still written.

---

## 5. The M1/M2 seam

M1 must not decide what a number means. The rule is:

> No module under `ingest/` may reference `Metric`, and no module under `ingest/` may contain a
> `us-gaap` tag literal.

Both halves are tested by an AST walk over the installed package
(`tests/test_layering.py`), not by convention. The reason for the second half is that a tag
literal in `ingest/` is the first line of a second, shadow copy of `normalize/tags.py` — and the
failure mode of two tag tables is that the report and the appendix disagree about which tag won.

**One carve-out, and it is narrow.** ROADMAP M1 puts market cap in M1, computed as
`price × dei:EntityCommonStockSharesOutstanding` across classes. That is a `dei` cover-page tag,
not a `us-gaap` financial metric, and it has no fallback chain — so the rule above permits it by
construction (`us-gaap` only) and the test asserts the `dei` allowance is exactly one tag long.
If a second `dei` tag ever needs to be named in `ingest/`, that is the signal that tag selection
has started leaking upstream.

---

## 6. Dependencies M1 adds

Per CLAUDE.md, dependencies arrive with the milestone that imports them.

| Package | Where | Spec | Why this pin |
|---|---|---|---|
| `httpx` | runtime, M1a | `>=0.28,<0.29` | 0.28.1 is the current stable; 1.0 is still at `1.0.dev2` and unreleased. A `<0.29` ceiling because a 0.x minor is a breaking change by convention. |
| `respx` | dev, M1a | `>=0.22,<0.23` | httpx mocking. See [§ Spec question 3](#7-spec-questions) — DESIGN §11 names `responses`, which cannot mock httpx. |
| `yfinance` | **optional extra**, M1a | `>=1.4,<2` | Not a default dependency. See below. |
| `lxml` | runtime, M1b | `>=5,<7` | iXBRL extraction from DEF 14A (§6.9). Form 4 and 13D/G are plain XML and use `xml.etree.ElementTree` from stdlib. Arrives with M1b, not M1a. |

Three deliberate non-additions:

- **No `tenacity`.** The retry policy needs an injectable clock and injectable jitter to be
  tested deterministically, which is more work to arrange around a library than to write. It is
  roughly thirty lines. See [`03-edgar-client.md`](03-edgar-client.md).
- **No `ijson`.** A 40 MB `companyfacts` payload parsed with `json.loads` peaks in the low
  hundreds of MB, which is acceptable on a developer machine and is the only place M1 runs.
  Whole-market scanning is the nightly bulk ZIPs (DESIGN §4.1), not a streaming parser.
- **No market-calendar package.** The yfinance partial-history check needs a trading-day count
  only to within about 10%; weekday count is sufficient and the check says so out loud.

### Why yfinance is an extra rather than a dependency

`>=1.4` because DESIGN §4.3 records that `curl_cffi` became optional at 1.4.0; `<2` because the
data source has no contract and a major bump is where that shows up.

But the stronger point is that DESIGN §4.3 calls yfinance "dev convenience only," notes its own
README describes it as intended for personal use, and concludes it is "not a defensible base for
anything shared or commercial." A default dependency contradicts all three: it puts a scraper
and its TLS-impersonation chain into every install, including one made by someone who set
`price_provider = "tiingo"` and will never import it.

So: `[project.optional-dependencies] yfinance = ["yfinance>=1.4,<2"]`, and `yfinance_.py`
imports lazily inside the adapter with a `ConfigError` naming the extra if the import fails. The
adapter ships; the dependency is opt-in. This is a proposed decision — see
[§ Spec question 8](#7-spec-questions).

---

## 7. Spec questions

**Status: all nine accepted as proposed (review, 2026-07-31). Recorded, not blocking.**

They are kept here in full rather than collapsed into a decision list, because each one is a
place where the code will diverge from DESIGN.md and the reason has to survive the person who
finds the divergence in a year. The proposed resolutions are now the resolutions; what remains
is folding them into DESIGN.md per [§ 10](#10-documentation-changes-m1-requires).

Two have since been settled empirically rather than by argument — 1 and, in part, 7. Ordered by
how much each changes M1.

**1. `filings.recent` is not the company's whole filing history, and DESIGN §6.6 assumes it is.**
*(Confirmed against live data — no longer an inference.)*

§6.6 says 8-K detection is "a filter on `filings.recent.items` from the submissions API — no NLP
required." Verified against SEC's own API documentation: `filings.recent` contains *"at least
one year's of filing or to 1,000 (whichever is more) of the most recent filings,"* and *"if the
entity has additional filings, `files` will contain an array of additional JSON files and the
date range for the filings each one contains."*

**Confirmed on Apple itself.** `https://data.sec.gov/submissions/CIK0000320193-submissions-001.json`
returns 200, and its newest accessions are from **2015** — so AAPL's `filings.recent` does not
reach 2015, and a 10y lookback on the flagship fixture reads an incomplete history without
pagination. The overflow page is also a *different shape* — flat columnar arrays, no `filings`
wrapper — so it needs its own parse function. Details and the resulting interface in
[`04-parsers.md` § Pagination](04-parsers.md#pagination).

Still unconfirmed: the field names *inside* a populated `files[]` entry. A payload with overflow
necessarily has ≥1,000 filings and is too large to inspect with the tooling available here. One
fetch to disk settles it, and it is **the last unconfirmed shape in M1a** and the first task of
implementation.

The two payloads that *could* be fetched reversed nine assumptions between them, so the
expectation for this one should be the same. Write the finding up before `pages_needed` is
implemented.

For a filer with heavy Form 4 traffic, 1,000 filings can be under three years — so a 5y window
puts older 8-Ks in the overflow, and a `recent`-only filter finds nothing wrong with a company
that filed a 4.02 four years ago. Since 4.02 is described as the loudest signal in the system,
this is not a detail.

Proposed: `submissions.py` transparently concatenates `recent` with every `files[]` page whose
range intersects the requested window, and DESIGN §6.6 gains a sentence saying so. Cost is one
to three extra requests per company. Spec'd in [`04-parsers.md`](04-parsers.md).

**2. `Fact.source: SourceRef` cannot describe a derived number, and M2 derives one immediately.**

DESIGN §3.2 gives every `Fact` a single `SourceRef`. But §4.2(c) requires
`Q4 = FY − (Q1+Q2+Q3)`, §4.2's table derives gross profit from revenue − COGS and total
liabilities from two other tags, and ROADMAP M1 derives market cap from a price and *n* share
counts. Each of those traces to several sources, and §3.2's rule is that a number which cannot
be traced is not printed.

Proposed: a `Derivation` record naming the rule and its input refs, with
`Provenance = SourceRef | Derivation`. Raised in M1 because `domain/provenance.py` lands in M1
and every later module types against it. Spec'd in [`01-domain-types.md`](01-domain-types.md).

**3. `responses` cannot mock httpx.** DESIGN §11 prescribes "`responses`-mocked retries" for the
EDGAR client. `responses` patches `requests`. The httpx equivalent is `respx` (0.22.0, requires
httpx ≥0.25). Proposed: §11 substitutes `respx`. Purely a tooling correction, listed because
§11 names a package.

**4. "Cache manifest hash" (§9.1, appendix) is ambiguous, and one reading breaks determinism.**

If it hashes the whole manifest, then fetching an unrelated ticker changes the hash printed in
every subsequent report, and the §11 determinism gate fails for a reason that has nothing to do
with the report. Proposed: it hashes the `(key, content_sha256)` pairs the run actually read,
sorted. That makes it a fingerprint of the inputs — which is what an appendix reader wants from
it. Spec'd in [`02-cache.md`](02-cache.md).

**5. `Fact` needs a `unit` field.** §3.2's sketch omits it; §4.2(b) dedups by `(unit, start,
end)`. §3.2 says "sketch, not final," so this is a confirmation rather than a conflict.

**6. The duration buckets are narrower than SEC's own frame tolerance — confirm that is
deliberate.** §4.2(c) specifies annual 350–380 days and quarterly 80–100. SEC's frames API uses
365 ± 30 (335–395) and 91 ± 30 (61–121). The narrower bands are defensible — they refuse
ambiguous durations rather than mislabelling them — but they route more facts to `OTHER`, and
whether `OTHER` is dropped or reported is M2's business. Flagged so M2 inherits an answer rather
than a coincidence.

**7. Tiingo's free-tier limits in §4.3 should be re-verified before the adapter ships.** §4.3
records 1,000 req/day, 50/hr, 500 unique symbols/month, 1 GB bandwidth. A search returned
different figures and none of them from Tiingo's own current documentation, so this document
does not assert a number and the adapter does not hardcode one — it reads the rate from config
and surfaces Tiingo's own 429 response. Confirm against Tiingo's docs when the key is issued.

**8. yfinance as an optional extra rather than a default dependency** — see
[§ Why yfinance is an extra](#why-yfinance-is-an-extra-rather-than-a-dependency).

**9. 404 as an absence rather than a fetch failure** — see [§ Exit codes](#4-exit-codes). A
reading of §14 rather than a contradiction of it, but it decides `fetch`'s exit code on a real
and common input, so it should be explicit somewhere normative.

### Raised while implementing (2026-07-31)

Two more, in the same form. Both are recorded in ROADMAP § Decided during design.

**10. `manifest_hash` cannot cover only the entries a run *read*, and this document says it does.**

[`02-cache.md` § 4](02-cache.md#4-manifest_hash--the-appendixs-cache-fingerprint) specifies that
`get` records a read and *"a miss records nothing"* — which makes the hash **empty on a cold run**,
because a cold run reads nothing. But [§ 3](#3-investo-fetch--the-command-surface)'s sample output
prints `manifest 9f2c1ab4` on a run whose sources are all `fetched`, not `cached`. Those two
statements cannot both hold.

Resolved as **entries *used*** — a cache hit or a fresh `put`. That is what makes a cold run and the
warm run after it produce the *same* fingerprint, which is the property the appendix value exists
for: *did this report see the same data as that one?* Under the read-only reading, the answer would
be "no" for a report and its own rerun, and §11's determinism gate would be measuring cache state
rather than report content.

DESIGN §9.1 now says so. Tested by `test_cache::test_manifest_hash_matches_between_a_cold_and_a_warm_run`.

**11. A missing price-provider key means `investo fetch` cannot run at all out of the box.**

[`05-prices.md` § 2](05-prices.md#2-tiingopy--the-default) says a missing `INVESTO_TIINGO_KEY` is a
`ConfigError` before any request — *"the same shape as the User-Agent rule."* That is right on its own
terms. The consequence it does not discuss: `Settings.price_provider` defaults to `tiingo`, so
`investo fetch AAPL` — this milestone's headline deliverable, and the subject of its cold-fetch exit
criterion — exits 5 for anyone without a Tiingo account, *after* the EDGAR half would have succeeded.

Implemented as specified rather than softened, and raised rather than resolved in code. The two
alternatives, if it becomes annoying:

- `fetch` records `prices: no provider configured` as an **absence** and exits 0, leaving `analyze`
  as the command that requires a price. Consistent with spec question 9's treatment of a 404, and it
  makes the EDGAR half of `fetch` usable with nothing but a User-Agent.
- Default `price_provider` to `stooq`, which needs no key. Cheaper still, but it makes the default
  path the one with **no adjusted close** — and §4.3's whole argument is that an unadjusted series
  feeding a beta estimate is the failure this project exists to avoid. So: not that one.

The error message names both escapes, so a first run is not a dead end.

### Two risks accepted, not resolved

Neither blocks M1; both are places where the design is knowingly buying something at a price.

**The typing harness is the most fragile test in the suite.** `test_typing.py` shells out to
`basedpyright --outputjson` and asserts on diagnostics and line numbers. A basedpyright release
that rephrases a diagnostic or attributes it to a different line breaks a test whose subject is a
*design* guarantee (§5.4's distinct share-count types), not a linter's behaviour — so the failure
would be maximally confusing: a red build that says nothing about the code.

Mitigations, in order of how much they help: basedpyright is already pinned `>=1.39,<2` and CI
runs `uv sync --frozen`, so the version only moves on a deliberate lock update; the assertion is
on **error count per file and the line number**, never on message text, which is the part most
likely to be rephrased; and the fixture files are three lines each, so a line-attribution change
is obvious on sight. The honest position is that this will need revisiting at some basedpyright
upgrade, and the alternative — deleting it — downgrades §5.4's "enforced by distinct types" to a
comment. Keep it, and expect to pay maintenance on it.

**`parse_float=Decimal` costs approximately nothing, and this is now measured rather than
assumed.** 1.12× on a realistic 33 MB payload, +0.01s; 1.22× if every value were a decimal.
Table and method in
[`04-parsers.md` § The cost, measured](04-parsers.md#the-cost-measured). Re-measure against a
real 40 MB payload once one is on disk — but the structural reason it is cheap (the C scanner is
retained, and the callable fires only on numbers with a decimal point, which in `companyfacts`
are the minority) means the conclusion is unlikely to move.

---

## 8. Proposed additions to ROADMAP § Decided during design

If the above are accepted, these are the sentences to record:

- **M1 splits into M1a (spine) and M1b (M4/M4.5 feeds).** M1's four exit criteria are met by
  M1a; M1b lands before M4.
- **`ingest/` may not name a `us-gaap` tag or reference `Metric`,** enforced by an AST test, with
  a single-tag `dei` carve-out for the cover-page share count that market cap needs.
- **404 and a missing tag are absences, not failures.** `fetch` exits 0 and prints them;
  the command that needs the data decides whether the absence is fatal.
- **Derived numbers carry a `Derivation`, not a `SourceRef`.**
- **yfinance is an optional extra.** The adapter ships; the dependency does not.
- **`--lookback` is not added to `fetch`;** the window comes from config.
- **The coverage floor is set in M1a from a measured figure,** as pyproject's `[tool.coverage]`
  comment already commits to, with the figure in the commit message.
- **M1a is re-estimated at ~2.5–3 weeks part-time**, against ROADMAP's ~1.5 weeks for all of M1.
  The design added machinery that was not in the original count; the split reduces what is on the
  critical path, not what M1a costs.
- **Fixture curation is its own workstream (~3 days), started in parallel on day one.** It is
  research rather than coding, it has no green test to declare it finished, and M2's exit
  criterion is not assessable without it.
- **`primaryDocument` needs an `xsl*/` strip for forms 3/4/5** before it names the machine-readable
  document — a transform, and it belongs with the others in the client.
- **SEC's endpoints disagree with each other, and the disagreements are normalized in one named
  module.** `ingest/edgar/_fields.py`: `cik` is a padded string from `submissions` and
  `companyfacts` but a bare `int` from `company_tickers_exchange.json`; absence is `""` on some
  fields and `null` on others, sometimes within one array. Boundary table in
  [`04-parsers.md` § 10.1](04-parsers.md#101-field-normalization-lives-in-one-module).
- **The company's display name comes from `submissions`, never from `companyfacts.entityName`,**
  which is EDGAR-conformed uppercase.
- **A filer with no `dei` section has no market cap**, recorded as an absence rather than a zero.

## 9. A defect in M0, found while writing this

Not a spec question — a test that does less than it claims, worth fixing in M1's first commit
rather than leaving as a trap.

`tests/test_errors.py::test_every_error_subclass_declares_a_code` walks
`InvestoError.__subclasses__()`, which returns **direct** subclasses only. Its docstring says it
*"walks the hierarchy instead of listing classes, so it covers subclasses added after it was
written"* — and today it does, because every error class is a direct child. M1 adds
`UndeclaredUserAgentError(ConfigError)` and `SecThrottledError(UpstreamFetchError)`, which are
grandchildren and would escape it silently. The guarantee would then read as enforced while the
first class that actually forgot its `exit_code` slipped through, reporting an upstream fetch
failure that never happened — the exact failure the test exists to prevent.

Fix: make the walk recursive. One-line change, and it should land before the first grandchild
does. Detail in [`03-edgar-client.md` § Errors](03-edgar-client.md#8-errors).

## 10. Documentation changes M1 requires

Per CLAUDE.md § Documentation requirements, and listed here so they are not discovered at
review time:

- **README.md** — no change to § Usage (no new flags). § Data sources gains nothing. If spec
  question 8 is accepted, the Quickstart gains one line for the `yfinance` extra.
- **DESIGN.md** — §3.2 (`Derivation`, `Fact.unit`), §6.6 (submissions pagination), §11
  (`respx` for `responses`), §9.1 (which manifest hash).
- **ROADMAP.md** — § Decided during design gains [§ 8](#8-proposed-additions-to-roadmap--decided-during-design); M1's
  entry gains the M1a/M1b split.

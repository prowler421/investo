# M1 — Parsers

Every parser here is a pure function from bytes to typed rows. None fetches, none caches, none
assigns a `Metric`, and none decides what a number means.

Shape, uniformly:

```python
def parse_x(body: bytes, *, source: SourceContext) -> X: ...
```

`SourceContext` carries what the parser cannot know — the URL it came from, its `fetched_at`, the
CIK — so that every row it emits can build a `SourceRef` without the parser reaching for a clock
or a network. It is also what makes every parser testable from a file on disk with no client
present.

M1a: [tickers](#1-tickerspy-m1a), [companyfacts](#2-companyfactspy-m1a),
[submissions](#3-submissionspy-m1a).
M1b: [frames](#4-framespy-m1b), [documents](#5-documentspy-m1b), [events](#6-eventspy-m1b),
[ownership](#7-ownershippy-m1b), [proxy](#8-proxypy-m1b), [finra](#9-finrapy-m1b).

---

## 1. `tickers.py` (M1a)

Source: `https://www.sec.gov/files/company_tickers_exchange.json`.

**Verified shape** (fetched 2026-07-31):

```json
{"fields":["cik","name","ticker","exchange"],
 "data":[[1045810,"NVIDIA CORP","NVDA","Nasdaq"],
         [1652044,"Alphabet Inc.","GOOGL","Nasdaq"],
         [320193,"Apple Inc.","AAPL","Nasdaq"], …]}
```

```python
@dataclass(frozen=True, slots=True)
class TickerRow:
    cik: int
    name: str
    ticker: str
    exchange: str

def parse_tickers(body: bytes, *, source: SourceContext) -> tuple[TickerRow, ...]: ...

def resolve(rows: Sequence[TickerRow], ticker: str) -> TickerRow:
    """Raises TickerNotFoundError (exit 2) if absent, or if not NASDAQ."""
```

Four things this parser must get right, three of which are invisible when wrong.

**Read the `fields` array; never index positionally.** The file ships its own column header, and a
parser that hardcodes `row[3] == exchange` is correct until SEC inserts a column — at which point
it reads company names as exchanges and every ticker becomes "not NASDAQ." Building a
`fields → index` map costs one line. **Test:** a fixture with the columns reordered parses
identically; a fixture missing a required field raises rather than mis-indexing.

**The exchange value is `"Nasdaq"`, mixed case.** Comparing against `"NASDAQ"` matches nothing and
the symptom is exit 2 for every ticker in the universe. Comparison is casefolded. **Test:** the
literal `"Nasdaq"` from the real fixture resolves, and a synthetic `"NASDAQ"` row resolves too.

**`cik` is an unpadded integer here.** Padding is the client's, per
[`03-edgar-client.md`](03-edgar-client.md#6-url-and-identifier-transforms). The parser does not
stringify it.

**One CIK can have several rows.** Multi-class issuers appear once per ticker (GOOGL and GOOG,
FOX and FOXA), which is exactly what §5.4's "sum all classes" needs. `resolve` returns the row
for the ticker asked for; the *other* classes are found by CIK, and that lookup belongs to market
cap.

### The exit-2 guarantee

README and §14: exit 2 is "ticker not found **or not NASDAQ**." Two violation tests, because a
happy-path test passes whether or not the second half is enforced:

- A ticker absent from the file → exit 2.
- A ticker present with `exchange: "NYSE"` → exit 2, **not** 0. This is the one that catches an
  implementation that resolved the CIK and forgot to check the exchange.

`company_tickers.json` — the CIK-only file, also listed in §4.1 — is deliberately unused. Two
lookup paths for the same question is how a NASDAQ filter comes to be bypassed by whichever call
site used the other one.

---

## 2. `companyfacts.py` (M1a)

Source: `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`. 10–40 MB for a large
filer.

**Confirmed against a live payload** — CIK 2093536 (ARXS), fetched 2026-07-31. Verbatim head:

```jsonc
{"cik":"0002093536","entityName":"ARXIS, INC.","facts":{
  "ffd":{"NetFeeAmt":{"label":"","description":"","units":{"USD":[
      {"end":"2026-04-06","val":153994.53,"accn":"0001193125-26-146309",
       "fy":null,"fp":null,"form":"S-1/A","filed":"2026-04-08","frame":"CY2026Q1I"}, …]}}},
  "us-gaap":{"AccountsPayableCurrent":{"label": …
      "units":{"pure":[{"start":"2025-01-01","end":"2025-03-31","val":0.367,
       "accn":"0001193125-26-243043","fy":2026,"fp":"Q1","form":"10-Q",
       "filed":"2026-05-28","frame":"CY2025Q1"}, …]}}}}}
```

The nesting is as DESIGN §4.2 assumes. Six details are not.

**1. `cik` is a zero-padded string here too — the open question from the last revision is
settled.** `"cik":"0002093536"`. So `companyfacts` agrees with `submissions` and **disagrees with
`company_tickers_exchange.json`**, which gives a bare `int`. Two of the three endpoints pad. That
makes the normalization helper load-bearing rather than defensive; it is named and specified in
[§ 10.1](#101-field-normalization-lives-in-one-module).

**2. `entityName` and `submissions.name` disagree on casing for the same company.**
`companyfacts` gives `"ARXIS, INC."`; `submissions` gives `"Arxis, Inc."`. The EDGAR-conformed
uppercase form is not what belongs on a report cover.

**Rule: the display name comes from `submissions.name`.** Without it the cover page's casing
depends on which parser ran last.

`companyfacts.entityName` is retained on `CompanyFacts` for provenance and debugging, and for
nothing else. In particular it is **not** an identity check that the two payloads describe the
same company — this very observation is the proof that it cannot be. Punctuation and casing
differ legitimately between the two endpoints for plenty of real filers whose CIK matches
perfectly, so a name comparison would raise on correct data. The identity check is
`as_cik(companyfacts["cik"]) == as_cik(submissions["cik"])`, post-normalization, using the field
both payloads carry for exactly this purpose.

**3. There is a taxonomy beyond `dei` / `us-gaap` / `srt`: `ffd`.** Filing Fee Disclosure, and it
sorts first so it is the first thing the parser sees. A taxonomy allowlist would have dropped it
— and more to the point, would drop the next one SEC adds. **The parser accepts any taxonomy key
and records what it found**; selecting `us-gaap` over `ffd` is tag-chain business, which is M2's.

**4. `dei` was absent entirely from this filer's payload**, with `ffd` first and `us-gaap`
second. Not proven impossible for it to appear elsewhere in the document, but its expected
position is before `ffd` and it is not there. A newly-listed filer that has not yet filed a 10-K
plausibly has no cover-page facts at all.

That matters more than it looks: `dei:EntityCommonStockSharesOutstanding` is the *only* source
for market cap (§4.3, and ROADMAP M1). So **a missing `dei` section means no market cap**, and
that has to be an absence recorded in the coverage report — not a `KeyError`, and not a zero.
The first NASDAQ IPO anyone runs `investo fetch` against will exercise this path.

**5. `start` is absent on instant facts — the key is missing, not `null`.** `row.get("start")`,
never `row["start"]`. This is what makes `classify(start=None, …)` → `INSTANT` correct, and it
confirms `PeriodKind.INSTANT` is detected by key absence rather than by a sentinel.

**6. `fy` and `fp` are `null` on facts from registration statements**, and `label` /
`description` can be `""`. `form` is also not restricted to periodic reports — `S-1/A` appears
here — so nothing may filter facts by assuming `10-K`/`10-Q`.

### The §4.2(a) trap is present in this payload, at minimum size

The `us-gaap` fact above covers `start 2025-01-01` → `end 2025-03-31` and carries **`fy: 2026`,
`fp: "Q1"`**. A calendar-Q1-2025 period, labelled fiscal year 2026, because it was reported in a
filing made in the issuer's fiscal 2026.

That is DESIGN §4.2(a) — *`fy`/`fp` are the fiscal year of the containing filing, not of the
fact* — demonstrated in a payload small enough to commit whole. It means `ARXS.json` earns a
second role: it is not only the awkward-values fixture but a working **§4.2(a) fixture**, and the
"never group by `fy`/`fp`" test does not have to wait for a reduced Apple payload to exist.

Finally, the unit key for per-share values is **`USD/shares`** in `companyfacts` — the
`USD-per-shares` spelling belongs to `frames` URLs only. `pure` is confirmed live, carrying
decimal values.

```python
def parse_companyfacts(body: bytes, *, source: SourceContext) -> CompanyFacts: ...

@dataclass(frozen=True, slots=True)
class CompanyFacts:
    cik: int
    entity_name: str
    facts: Mapping[tuple[str, str], tuple[RawFact, ...]]  # (taxonomy, tag) → facts
    tags_present: frozenset[tuple[str, str]]
    taxonomies_present: frozenset[str]                    # observed: ffd, us-gaap, dei, srt, …
```

Keyed by `(taxonomy, tag)` rather than by tag, because `Assets` exists in more than one taxonomy
and M2's chains name `dei:` and `us-gaap:` tags side by side.

`cik: int` is normalized, not cast: the payload gives a padded string. See
[§ 10.1](#101-field-normalization-lives-in-one-module).

`taxonomies_present: frozenset[str]` rather than a fixed set, because `ffd` was not anticipated
and the next addition will not be either.

### `Decimal`, and the exact mechanism

CLAUDE.md convention 8 is `Decimal` for money, never `float`. The trap is that
`json.loads` materializes `391035000000.01` as a `float` before any of our code sees it, so
`Decimal(row["val"])` is already too late — it converts a value that has already lost precision,
and `Decimal(0.1)` is exact and wrong.

The mechanism is the parse hook:

```python
payload = json.loads(body, parse_float=Decimal, parse_int=int)
```

`parse_float=Decimal` is called with the *source text* of the number, so no float is ever
constructed. Measured, on `391035000000.01`:

```
parse_float=Decimal : 391035000000.01
Decimal(float)      : 391035000000.010009765625
```

**Violation test:** that fixture round-trips to `Decimal("391035000000.01")` exactly, and the
test asserts `not isinstance(value, float)` — the second assertion is the one that fails if
someone later "simplifies" the hook away, because the first would still pass on a value that
happens to be representable.

`parse_int=int` is stated explicitly rather than left default so the pair reads as a deliberate
policy about numbers rather than an incantation.

### The cost, measured

The hook forces a Python callable per decimal literal, so it is fair to ask what it costs
against §14's 60s-warm / 5-minute-cold target. Benchmarked on a synthetic payload with
`companyfacts`' shape and numeric mix — most XBRL `val`s are large integers, with decimals
concentrated in `USD/shares` and `pure` units:

| Payload | `json.loads` default | with `parse_float=Decimal` | |
|---|---|---|---|
| 33 MB, ~12% decimal (realistic) | 0.12s | 0.14s | 1.12× |
| 32 MB, 100% decimal (worst case) | 0.12s | 0.14s | 1.22× |

So roughly **+0.01s on a large filer**, and under +0.05s even if every value were a decimal.
Against a 60-second warm target this is not a consideration, and the reason is structural rather
than lucky: the C scanner is retained when `parse_float` is supplied, and it invokes the callable
only for numbers carrying a decimal point or exponent — which in `companyfacts` is the minority.

Measured on CPython 3.10 rather than the 3.13 this project targets, so treat the ratio as sound
and the absolute figures as an upper bound. Re-measure once a real 40 MB payload is on disk; the
number to watch is total `fetch` wall time, not this line.

### Per-fact handling

- `period` from `classify(start, end)`. A `dei` cover-page count has no `start` → `INSTANT`.
- `unit` is the units dict key, verbatim: `"USD"`, `"USD/shares"`, `"shares"`, `"pure"`. Not
  normalized, not mapped. §4.2 twice warns that unit differences are value differences.
- `filing_fy` / `filing_fp` from `fy` / `fp`, carried and never used for grouping (§4.2a).
- `frame` carried when present, restricted to peer use (§4.2).
- `source` = `SourceRef(accession=Accession.parse(accn), taxonomy=…, tag=…, form=form,
  filed=filed, url=source.url, fetched_at=source.fetched_at)`.

### What is not an error

**A missing tag.** `tags_present` reports what was there; the absence is a coverage fact, not a
failure. §4.2's whole argument is that hardcoding one tag per metric silently produces sparse
data and a confidently wrong report.

**A missing custom extension.** SEC's API documentation states the XBRL APIs aggregate only facts
that *"use a non-custom taxonomy"* — company extension taxonomies are excluded by design. So some
line items are simply not there for some filers. §4.2: that is a coverage fact to surface, not a
bug to work around. The fetch summary prints it.

### Memory

40 MB of JSON parses to a few hundred MB of Python objects. Accepted for M1: this runs on a
developer machine against one company at a time, and whole-market work is the nightly bulk ZIPs
(§4.1), not a streaming parser. `parse_companyfacts` returns immutable tuples and drops the
intermediate dict, so peak is at parse time and not sustained.

---

## 3. `submissions.py` (M1a)

Source: `https://data.sec.gov/submissions/CIK##########.json`.

**Confirmed against a complete live payload** — CIK 2093536 (Arxis, Inc., ARXS), fetched
2026-07-31. A recent registrant was chosen deliberately: its filing history is short enough that
the whole document is inspectable, so the key list below is exhaustive rather than partial.

Top level: `cik`, `entityType`, `sic`, `sicDescription`, `ownerOrg`,
`insiderTransactionForOwnerExists`, `insiderTransactionForIssuerExists`, `name`, `tickers`,
`exchanges`, `ein`, `lei`, `description`, `website`, `investorWebsite`, `category`,
`fiscalYearEnd`, `stateOfIncorporation`, `stateOfIncorporationDescription`, `addresses`,
`phone`, `flags`, `formerNames`, `filings`.

`filings.recent`, columnar — the complete set of parallel arrays:

```
accessionNumber  filingDate  reportDate  acceptanceDateTime  act  form  fileNumber
filmNumber  items  core_type  size  isXBRL  isInlineXBRL  isXBRLNumeric
primaryDocument  primaryDocDescription
```

**Two of those were not in the previous draft of this document: `core_type` and
`isXBRLNumeric`.** Neither is needed by M1, but their absence from an "expected keys" list is
the kind of gap that turns into a strict-parsing failure on the first real payload.

### Five things the live payload contradicts or sharpens

Each of these was an assumption before the fetch, and four of the five were wrong.

**1. `cik` and `sic` are zero-padded strings here, not integers.** The payload reads
`"cik":"0002093536"` and `"sic":"3728"` — where `company_tickers_exchange.json` gives
`cik` as a bare `int`. So the same identifier arrives in two representations from two SEC
endpoints, and `CompanyProfile.cik: int` requires a conversion the parser must perform rather
than a cast it can assume. `sic` can also be the empty string for a filer without one, so
`int(payload["sic"])` raises on a real input. Both go through
[`_fields.py`](#101-field-normalization-lives-in-one-module), which owns the boundary table.

**2. Absent values are the empty string, not `null`.** `reportDate`, `act`, `fileNumber` and
`primaryDocDescription` all carry `""` in the observed payload — for example `reportDate` is
`""` on the Form 3 and `EFFECT` rows. A parser written against `None` produces
`date.fromisoformat("")` and a `ValueError`. Every optional scalar is normalized at the boundary
by [`_fields.py`](#101-field-normalization-lives-in-one-module).

`isXBRLNumeric` is the exception that proves the rule: it carries genuine JSON `null` values
mixed with `0`/`1` in the same array. So the column is not uniformly typed, and both spellings
of "absent" occur in one document.

**3. `items` is comma-separated with no spaces — and the degenerate case is real.** The observed
values include `"1.01,8.01,9.01"` and `"2.02,9.01"`, confirming the format. They also include a
row whose `items` is literally `",,"` — two commas, three empty tokens, on an `EFFECT` filing.
A naive `split(",")` on that yields `["", "", ""]`.

This is the design's own defence arriving with evidence: filtering to tokens matching
`^\d\.\d\d$` discards all three, `items_raw` preserves the original, and the reported parse rate
is unaffected because no code was ever meant to fire on an `EFFECT`. Had the parser trusted
`split(",")`, it would have produced three empty item codes on a real filing.

**4. `primaryDocument` can contain a subdirectory, and for ownership forms it points at the
wrong thing.** Every Form 3 and Form 4 row in the payload has
`primaryDocument = "xslF345X06/ownership.xml"` — an **XSL-rendered viewer path**, not the raw
XML. The machine-readable document is `ownership.xml` in the accession directory; the
`xslF345X06/` prefix serves a browser-facing HTML rendering.

`ownership.py` (M1b) parsing `primaryDocument` verbatim therefore fetches a styled document
rather than the Form 4 XML it expects. The rule: **for forms 3, 4 and 5, strip a leading
`xsl*/` path segment from `primaryDocument` before building the URL.** That is a documented
transform belonging with the others in
[`03-edgar-client.md`](03-edgar-client.md#6-url-and-identifier-transforms), and it gets a test
against this exact observed value. `primaryDocument` can also be a PDF
(`ARXS_8A_Cert_2093536.pdf`), so nothing may assume an `.htm` suffix.

**5. `files` is confirmed as the key name, and is `[]` for a filer without overflow.** The
observed payload ends `"files":[]`. So the key is always present and the empty case needs no
special handling — `pages_needed([])` returns `()` and no request is made.

The *field names inside* a populated `files[]` entry remain unconfirmed; see
[§ Pagination](#pagination).

```python
@dataclass(frozen=True, slots=True)
class CompanyProfile:
    cik: int
    name: str
    sic: int | None
    sic_description: str | None
    fiscal_year_end: str           # "MMDD", e.g. "0928"
    tickers: tuple[str, ...]
    exchanges: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class FilingRow:
    accession: Accession
    form: str
    filed: date                    # filingDate — §4.2b's as-of key
    report_date: date | None
    accepted_at: datetime | None
    primary_document: str
    items: tuple[str, ...]         # 8-K item codes, parsed
    items_raw: str                 # as filed, never discarded
    is_xbrl: bool
    is_inline_xbrl: bool
    size: int | None

    def primary_url(self, cik: int) -> str: ...
```

### The columnar transform, and the assertion that has to be there

All arrays in `filings.recent` must be the same length. A parser that zips them without checking
truncates to the shortest — silently losing the tail of the filing history, which looks exactly
like a company that stopped filing.

```python
lengths = {name: len(col) for name, col in recent.items()}
if len(set(lengths.values())) != 1:
    raise UpstreamFetchError(f"submissions.filings.recent columns disagree in length: {lengths}")
```

Exit 4 rather than a coverage note: a malformed payload is not an absence, and a partial filing
history that we cannot detect the extent of is worse than no run.

Required keys are named explicitly and their absence raises. The full key set is **not** asserted
as exhaustive — SEC adds fields — so the parser reads what it needs and ignores the rest.

### Pagination

Background and the DESIGN §6.6 conflict: [spec question 1](README.md#7-spec-questions).

`filings.recent` is **not the whole history.** SEC's API documentation: the property path contains
*"at least one year's of filing or to 1,000 (whichever is more) of the most recent filings"*, and
*"if the entity has additional filings, `files` will contain an array of additional JSON files and
the date range for the filings each one contains."*

```jsonc
// Field names from SEC's prose plus the widely-used page-naming convention.
// Not observed directly — the Apple fetch was truncated before reaching `files`.
// Confirm against the AAPL fixture before writing the parser.
"files": [ { "name": "CIK0000320193-submissions-001.json",
             "filingCount": 1000, "filingFrom": "…", "filingTo": "…" } ]
```

**Confirmed empirically, and on the flagship fixture.** Fetching the conventional overflow URL
for Apple —

```
https://data.sec.gov/submissions/CIK0000320193-submissions-001.json
```

— returns HTTP 200, `Content-Type: application/json`, and its first accessions are
`0001193125-15-177428`, `0001193125-15-175208`, `0001193125-15-173308`: **2015 filings**. So
Apple's own `filings.recent` does not reach 2015, and a 10y lookback on AAPL — the setting README
already flags as the highest-risk one — reads an incomplete filing history unless the parser
paginates. This is no longer an inference from SEC's prose about a hypothetical heavy filer; it
is the behaviour of the first company anyone will run.

**The overflow page has a different shape from the main payload, and this changes the
interface.** It is a **flat** object whose top-level keys are the columnar arrays themselves —
it begins `{"accessionNumber":[...]` — with no `filings` wrapper, no `recent`, and none of the
company metadata. One function cannot parse both.

```python
def parse_submissions(body: bytes, *, source: SourceContext) -> tuple[CompanyProfile, tuple[FilingRow, ...]]:
    """The main payload: company metadata plus filings.recent."""

def parse_submissions_page(body: bytes, *, source: SourceContext) -> tuple[FilingRow, ...]:
    """One overflow page. Flat columnar object; no profile to return."""

def pages_needed(files: Sequence[FilesEntry], *, window: tuple[date, date]) -> tuple[str, ...]:
    """The overflow pages whose [filingFrom, filingTo] intersects the window."""

def merge_pages(*groups: Sequence[FilingRow]) -> tuple[FilingRow, ...]:
    """Concatenate and sort by `filed` descending. Dedups by accession."""
```

Two parse functions, one row type. `merge_pages` dedups by accession because the page boundaries
are SEC's and nothing guarantees they do not overlap. Deciding *which* pages to fetch is the
caller's — the parser does not fetch. Cost is one to three extra requests per company.

The `FilesEntry` field names (`name`, `filingCount`, `filingFrom`, `filingTo`) are still taken
from SEC's prose rather than observed, because a payload with a populated `files[]` necessarily
has ≥1,000 filings and is far too large to inspect through the tooling available here. The
observed small-filer payload confirms only that the key exists and is `[]` when empty.

**One shape remains unconfirmed in M1a: the fields inside a populated `files[]` entry.**
`companyfacts` has since been confirmed against a live payload (§2), leaving only this.

Confirming it is one fetch of `submissions/CIK0000320193.json` straight to disk, and it is the
first task of implementation — a payload with overflow necessarily has ≥1,000 filings, which is
why it could not be settled with the tooling used to write this document. It is not speculative
work either way: that fetch also produces the `AAPL.json` fixture the suite needs.

Given that the two fetches which *were* possible reversed nine assumptions between them, the
expectation should be that this one surfaces something too. Write the finding up before
`pages_needed` is implemented, not after.

**Tests:** a fixture with a `files` entry and a window reaching into it asserts a filing from the
older page appears in the merged result; a window inside `recent` asserts **zero** extra pages are
requested — otherwise the optimization is a comment. Boundary: a window whose start equals a
page's `filingTo` includes that page.

### `items`

`items` is a string on 8-K rows, typically `"2.02,9.01"`. Parsed by splitting on commas,
stripping, and keeping tokens matching `^\d\.\d\d$`. **`items_raw` is kept regardless.**

The exact format across filers and years is not something this document asserts — it is confirmed
against fixtures at implementation time, and keeping `items_raw` is what makes being wrong
recoverable rather than lossy. A parser that recognizes nothing and discards the original has
destroyed the evidence that it failed. The parse rate is reported in the fetch summary, so a
format change shows up as a number dropping rather than as flags quietly ceasing to fire.

The item **codes** are stable and documented (SEC Webmaster FAQ enumerates them for filings since
2004): 1.01–1.05, 2.01–2.06, 3.01–3.03, 4.01, 4.02, 5.01–5.08, 6.01–6.10, 7.01, 8.01, 9.01.
Mapping a code to a severity is M4.5's `analyze/events.py`, not this parser's.

---

## 4. `frames.py` (M1b)

Source: `https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json`.

Period format, from SEC's documentation: `CY####` annual (duration 365 ± 30 days), `CY####Q#`
quarterly (91 ± 30), `CY####Q#I` instantaneous. Units with a denominator use `-per-`:
`USD-per-shares`.

```python
@dataclass(frozen=True, slots=True)
class FrameRow:
    cik: int
    entity_name: str
    accession: Accession
    value: Decimal
    end: date
    start: date | None
    fiscal_year: int | None
    fiscal_period: str | None
```

Two restrictions, both from §4.2, both enforced rather than documented:

- **Never for the subject company's history.** Frames is not point-in-time stable — a CY2025Q1
  frame can resolve to a 2026 filing. `frames.py` returns `FrameRow`, a different type from
  `RawFact`, so a frame value cannot be appended to a company series by accident. That type
  distinction *is* the enforcement.
- **`fetched_at` matters more here than anywhere else.** M7 recomputes peer medians as-of every
  backtest date (§8, leak 3), and frames mutates. The cache entry is the only record of what the
  cohort looked like when we asked.

Note that SEC's frame duration tolerances (335–395 and 61–121 days) are wider than our own buckets
(350–380, 80–100). See [spec question 6](README.md#7-spec-questions).

---

## 5. `documents.py` (M1b)

Primary document URL: `/Archives/edgar/data/{cik_unpadded}/{accession_nodashes}/{primaryDocument}`
— both transforms from [`03-edgar-client.md`](03-edgar-client.md#6-url-and-identifier-transforms).

```python
@dataclass(frozen=True, slots=True)
class FilingDocument:
    accession: Accession
    form: str
    items: Mapping[str, str]       # "1A" → text
    unrecognized: tuple[str, ...]  # headings found but not matched
    split_ok: bool

def split_items(text: str, *, form: str) -> FilingDocument: ...
```

Items split: 1, 1A, 1C, 3, 7, 7A, 8, 9A (§7.4). §7.4 also records why: not context limits, but
cost and precision.

**The regex is brittle across filers, and the design accepts that rather than fighting it.**
ROADMAP M1 and §7.4 both say: collect failures as fixtures rather than chasing generality up
front. So `split_ok` is a field, `unrecognized` is a field, and the parse rate is reported. A
filing that will not split is a filing whose narrative sections are absent from the report — a
coverage fact — not an aborted run.

Text extraction from HTML: `lxml` (M1b's one new dependency), tags stripped, whitespace
collapsed, `&nbsp;` normalized. §7.3 requires verbatim quote verification against this text, so
the normalization must be **stable and recorded** — a quote verified against one normalization and
searched under another fails for no reason. The normalizer is a single named function with its own
test, and M6 must call the same one.

---

## 6. `events.py` (M1b)

Extraction only. `analyze/events.py` (M4.5) maps codes to severity; this module has no severity
table, and that separation is what keeps ingest replaceable.

```python
@dataclass(frozen=True, slots=True)
class FilingEvent:
    accession: Accession
    filed: date
    items: tuple[str, ...]
    items_raw: str
    body_url: str | None           # fetched only when M4.5 asks (4.01, 5.02)
```

§6.6's two-stage design maps cleanly onto the M1/M4.5 split: the codes come from
`submissions.py`'s already-parsed `items`, so **detection needs no extra request at all**, which is
why §6.6 calls it the highest value per line of code in the system. Only 4.01 and 5.02 need the
body, and only when the LLM layer exists to refine them — under `--llm none` they fire at capped
severity with "unclassified, read the filing."

Earnings releases: item 2.02 with the release furnished as an `EX-99*` exhibit. §6.6's parser
note is normative — **enumerate all `EX-99*` rather than hardcoding `99.1`**, because the `.1` is
filer convention and not rule. Guidance-only announcements are usually 7.01 (Reg FD) rather than
2.02.

---

## 7. `ownership.py` (M1b)

Form 4, 13D/G, 13F. All XML; `xml.etree.ElementTree` from stdlib, no dependency.

- **Form 4** — XML since 2003, 2-business-day lag. Filter transaction codes: keep `P` and `S`
  (open-market), drop `A`, `M`, `F`, `G` (grants, exercises, tax withholding, gifts) as noise per
  §6.8. 10b5-1 planned sales flagged and excluded from the signal. **Dedup `4/A` amendments** by
  `(reporter, transaction date, code)`, newest `filed` winning.
- **13D/G** — structured XML only since 2024-12-18 (Beneficial Ownership Reporting
  Modernization). So a 5y window straddles the boundary: pre-2024 filings are narrative HTML. The
  parser returns rows for the structured era and records the pre-boundary filings as
  `unparsed_count` rather than pretending the history is complete.
- **13F** — 45-day lag, long-only US equity, "as filed" and may contain inconsistencies per SEC.
  Positions may be fully unwound before publication. Parsed, and the lag is printed wherever the
  number is.

`ownership.py` extracts; the P/S filtering rule is a §6.8 requirement and lives here because it is
a property of the *source format*, not an analysis. The judgment about what a cluster of sales
means is M4.5's.

---

## 8. `proxy.py` (M1b)

DEF 14A. One structured numeric source and a lot of narrative.

- **Pay Versus Performance (Item 402(v)) is inline-XBRL tagged** via the ECD taxonomy, for fiscal
  years ending on or after 2022-12-16 (Release 34-95607). Extracted with `lxml`, reading the iXBRL
  facts. This is the only numeric extraction in a proxy.
- **Everything else is untagged narrative** — Summary Compensation Table, CD&A, pay ratio, audit
  fees. Text is extracted and handed on; it is an M6 LLM target, not a data feed. `proxy.py`
  produces no numbers from narrative.
- **Beneficial ownership is not read from the proxy.** §6.8: Form 4 and 13D/G XML are the better
  source. Parsing the proxy's narrative table too would create a second answer to the same
  question.

A company with no DEF 14A in the window is an absence, printed in the summary.

---

## 9. `finra.py` (M1b)

Short interest, `equityShortInterestStandardized`. The older `equityShortInterest` dataset stopped
publishing 2021-04-30 and is not used.

**Use the bulk file downloads, which are auth-free.** §6.8: the FINRA Query API requires
credentials and an OAuth2 `client_credentials` bearer token. The downloads need neither, and
adding a credential requirement for data available without one would put an `INVESTO_FINRA_*`
variable in config for nothing.

**Snapshotting is the whole feature.** §6.8: *revisions overwrite rather than append — you must
snapshot to build point-in-time history.* So:

```
.cache/blobs/…                       # the payload, content-addressed as usual
```

and the cache's append-only manifest is the snapshot history: each fetch of the same settlement
date writes a new entry with its own `fetched_at`, and a revision produces a different
`content_sha256` and therefore a new blob. Nothing special is needed — but it only works because
the cache never overwrites, which is the load-bearing property from
[`02-cache.md`](02-cache.md). **Test:** two fetches of the same URL returning different bodies
leave both retrievable, and the newer one is what `get` returns.

Not to be confused with FINRA's daily short *volume* files, which measure something else. And per
§6.8, **do not plan on SEC Form SHO** — Rule 13f-2 was remanded and the compliance date is
2028-01-02, so FINRA is the only free source and will remain so.

---

## 10. Shared boundary handling

### 10.1 Field normalization lives in one module

`ingest/edgar/_fields.py`. Private to the EDGAR package, imported by `tickers.py`,
`companyfacts.py`, `submissions.py` and every M1b parser.

It exists because the same logical value arrives in different spellings from different SEC
endpoints, and every one of those differences was discovered by fetching rather than by reading
the documentation:

```python
def as_cik(value: object) -> int:
    """`"0002093536"` → 2093536; `320193` → 320193. Raises on anything else."""

def as_date(value: object) -> date | None:
    """`"2026-04-08"` → date; `""` and `None` → None."""

def as_optional_int(value: object) -> int | None:
    """`"3728"` → 3728; `""` and `None` → None. For `sic`, `fy`."""

def as_optional_str(value: object) -> str | None:
    """`""` → None. Everything SEC writes as an absent string."""

def as_bool(value: object) -> bool | None:
    """`1`/`0` → bool; `None` → None. `isXBRLNumeric` carries all three."""
```

Not in `domain/`, deliberately. These encode SEC's payload quirks — that `sic` is a string, that
absence is `""` on one endpoint and `null` on another — and `domain/` is meant to be the layer
that knows nothing about where data came from. Putting them there would make the domain types'
docstrings describe an HTTP API.

**One module, not one per parser.** Duplicating `as_cik` into two parsers is how the two come to
disagree, and the disagreement is silent: one path builds `CIK0000320193` and the other builds
`CIK320193`, and only the second 404s.

#### The boundary table it is tested against

`tests/test_fields.py`, one parameterized test per function, every row from an observed payload
rather than invented:

| Function | Input | Expected | Where this spelling was observed |
|---|---|---|---|
| `as_cik` | `"0002093536"` | `2093536` | `submissions`, `companyfacts` |
| `as_cik` | `320193` | `320193` | `company_tickers_exchange.json` |
| `as_cik` | `"320193"` | `320193` | unpadded string — not observed, accepted anyway |
| `as_cik` | `""` / `None` / `"abc"` | raises | a CIK is never optional |
| `as_date` | `"2026-04-08"` | `date(2026, 4, 8)` | `filingDate` |
| `as_date` | `""` | `None` | `reportDate` on Form 3 / `EFFECT` rows |
| `as_date` | `None` | `None` | defensive; both spellings occur in one document |
| `as_optional_int` | `"3728"` | `3728` | `sic` |
| `as_optional_int` | `""` | `None` | `sic` on a filer without one |
| `as_optional_int` | `null` | `None` | `fy` on registration-statement facts |
| `as_bool` | `1` / `0` | `True` / `False` | `isXBRL` |
| `as_bool` | `null` | `None` | `isXBRLNumeric`, mixed with `0`/`1` in one array |

The `as_cik` rows are the ones that matter most, because that is the function two endpoints
disagree about and the failure it prevents is a 404 that looks like a delisted company.

### 10.2 Cross-parser rules

Applying to all of the above, each with a test in
[`06-testing.md`](06-testing.md):

1. **No parser fetches.** No parser imports `httpx` or takes a client. Enforced by the layering
   AST test.
2. **No parser assigns a `Metric`.** Enforced by the same test.
3. **No parser calls `datetime.now()` or `date.today()`.** Time enters through `SourceContext`.
   A parser that reads the clock cannot be tested against a fixture twice with the same result,
   and its output cannot be byte-identical across runs.
4. **Every parser preserves what it could not interpret** — `items_raw`, `unrecognized`,
   `unparsed_count`, `tags_present`. The parse rate is reportable, so a silent upstream format
   change surfaces as a number rather than as a feature that stopped working.
5. **Numbers are constructed with `Decimal` via `parse_float=Decimal`**, never from a `float`.

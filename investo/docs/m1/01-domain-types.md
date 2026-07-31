# M1 — Domain types

`domain/models.py`, `domain/periods.py`, `domain/provenance.py`. Built first, because everything
downstream types against them and every later milestone pays for a change here.

Zero I/O in this package. No module under `domain/` imports `httpx`, `investo.ingest`, or reads
a file. Tested — see [`06-testing.md`](06-testing.md).

DESIGN §3.2 gives a sketch and labels it "sketch, not final." What follows is the proposed final
form, with every departure from the sketch marked **[extends §3.2]** and carried into
[README § Spec questions](README.md#7-spec-questions).

---

## 1. `domain/provenance.py`

### `Accession`

The accession number appears in three spellings and DESIGN §4.1 makes the client responsible for
the transforms. A value type is the cheaper place: the transforms then have one home and one
test, and no caller has to remember which spelling an endpoint wants.

```python
@dataclass(frozen=True, slots=True, order=True)
class Accession:
    """An EDGAR accession number, canonically dashed."""

    value: str  # "0000320193-25-000079"

    @classmethod
    def parse(cls, raw: str) -> Accession: ...
    @property
    def nodashes(self) -> str: ...        # "000032019325000079"  — /Archives/ directory name
    def index_url(self, cik: int) -> str: ...  # ".../{accn}-index.htm"
```

`parse` accepts either spelling and normalizes to dashed. It rejects anything that is not 18
digits plus two dashes in the 11th and 14th positions, because a silently accepted malformed
accession becomes a 404 that looks like missing data — ROADMAP M1's named risk.

**One rule stated as a rule, because getting it wrong is invisible:** the accession's leading ten
digits are the CIK of *the entity that submitted the filing*, which for most companies is a
filer agent, not the company. Nothing may derive a company CIK from an accession. Apple's own
filing history contains both patterns — `0000320193-26-000013` (Apple submitting for itself) and
`0001140361-26-025622` (an agent submitting on Apple's behalf) — so the wrong rule produces
correct answers on some filings and a nonexistent CIK on others. `Accession` therefore exposes
no `cik` property at all, and `index_url` takes the CIK as an argument.

### `SourceRef`

```python
@dataclass(frozen=True, slots=True)
class SourceRef:
    accession: Accession
    taxonomy: str | None      # "us-gaap" | "dei" | "srt" | None for non-XBRL
    tag: str | None
    form: str                 # "10-K", "10-Q", "8-K", "DEF 14A", "4"
    filed: date
    url: str
    fetched_at: datetime      # tz-aware, UTC
```

**[extends §3.2]** `taxonomy` is new. §4.2 requires distinguishing `dei:EntityCommonStock...`
from `us-gaap:WeightedAverageNumberOfDilutedShares...`, and a bare tag string cannot: `Assets`
exists in more than one taxonomy. The appendix prints "tag provenance per metric" (§9.1), and
`us-gaap:Assets` is the useful form of that.

`fetched_at` is tz-aware UTC, always. A naive datetime in a provenance record is a timestamp
whose meaning depends on the machine that wrote it, and the cache is meant to be the immutable
record of what the model saw.

### `Derivation` — [extends §3.2]

§3.2's rule is that a number which cannot be traced is not printed. Several numbers the report
prints are computed from more than one fact:

| Derived value | Inputs | First needed |
|---|---|---|
| Q4 = FY − (Q1+Q2+Q3) | 4 facts | M2 (§4.2c) |
| Gross profit = revenue − COGS | 2 facts | M2 (§4.2 table) |
| Total liabilities = L&SE − equity | 2 facts | M2 (§4.2 table) |
| Market cap = price × Σ shares by class | 1 price + *n* facts | **M1** |
| Revenue stitched across ASC 606 | 2 tags, *n* facts | M2 |

A single `SourceRef` cannot describe any of them, and the fallback — printing the derived number
with one of its inputs' refs — is worse than printing nothing, because it looks traced.

```python
@dataclass(frozen=True, slots=True)
class Derivation:
    rule: str                      # "market_cap", "q4_from_annual_minus_quarters"
    inputs: tuple[Provenance, ...]
    note: str | None = None        # e.g. "classes: GOOGL, GOOG"

type Provenance = SourceRef | Derivation
```

Recursive by construction, since a stitched series feeds a derived margin. `rule` is a plain
string rather than an enum: the set of rules grows every milestone from M2 to M5, and an enum in
`domain/` would have to be edited by each of them.

The reason to settle this in M1 rather than M2 is that `Fact.source` is annotated in M1 and every
module written between now and then would be written against the narrower type.

---

## 2. `domain/periods.py`

### `PeriodKind` and `FiscalPeriod`

DESIGN §4.2(c): annual vs. quarterly is duration arithmetic, not `form`. The buckets are
normative there; this module is the only place they are written down.

```python
class PeriodKind(StrEnum):
    INSTANT = "instant"    # balance-sheet fact: no start
    QUARTER = "quarter"    #  80–100 days
    ANNUAL  = "annual"     # 350–380 days
    YTD     = "ytd"        # 101–349 days — difference or drop (M2's call)
    OTHER   = "other"      # < 80 or > 380

@dataclass(frozen=True, slots=True, order=True)
class FiscalPeriod:
    end: date                                  # first, so ordering is chronological
    kind: PeriodKind                           # StrEnum, so it compares
    start: date | None = field(compare=False)  # None ⟺ kind is INSTANT

    @property
    def days(self) -> int | None: ...
```

**`start` is excluded from comparison, and it has to be.** With `order=True` and `start`
participating, sorting a list that contains both an `INSTANT` (start `None`) and a duration
ending the same day evaluates `None < date(...)`, which raises `TypeError`. That list is not
hypothetical — it is what you get the first time a balance-sheet fact and an income-statement
fact for the same period end land in one series, which is every filer, every quarter. So
comparison is on `(end, kind)`, `start` is `compare=False`, and the crash cannot happen.

The consequence is that two durations with the same `end` and the same `kind` compare equal.
That is acceptable: they are the same period by §4.2's own grouping rule, and if they carry
different values that is a restatement, which is M2's to resolve by `filed` date — not
something a sort should be quietly breaking ties on.

```python
QUARTER_DAYS: Final = range(80, 101)    # inclusive of 100
ANNUAL_DAYS:  Final = range(350, 381)   # inclusive of 380

def classify(start: date | None, end: date) -> PeriodKind: ...
```

`classify` is total and exhaustive — every `(start, end)` pair lands in exactly one kind, and
`OTHER` is a named bucket rather than an exception. A parser that raises on an odd duration
cannot ingest a filer with a 53-week year, and refusing to ingest is not the same as refusing to
*use*: M2 decides what to do with `OTHER`, and it can only decide if M1 hands it over labelled.

**Boundary tests are mandatory, per CLAUDE.md.** 349/350/380/381 and 79/80/100/101 each get an
assertion. A `>` where `>=` belongs survives every test that only probes 90 and 365.

**`fy` and `fp` are not on this type, deliberately.** §4.2(a): they are the fiscal year of the
containing filing, not of the fact. They are preserved on the raw row (see
[`04-parsers.md`](04-parsers.md)) so a fixture can demonstrate the trap, and there is no path by
which a `FiscalPeriod` can be constructed from them.

### `LOOKBACK` and window arithmetic

`config.parse_lookback` (M0) already turns `"5y"` into `5`. This module turns 5 into a window:

```python
def window(years: int, *, as_of: date) -> tuple[date, date]: ...
```

Returns `(start, as_of)` where `start = as_of` minus `years` calendar years, then floored to the
first day of that month. Floored because a lookback that starts mid-quarter includes a partial
period whose inclusion depends on the day the command was run — and two runs a day apart would
then legitimately produce different reports, which the determinism gate would eventually catch as
a bug that isn't one.

Beneish needs one year of history beyond the nominal lookback (§4.2). That extra year is M4's
concern, but `window` takes `years` rather than reading config so M4 can ask for `years + 1`
without a second function.

---

## 3. `domain/models.py`

### `Metric`

```python
class Metric(StrEnum):
    REVENUE = "revenue"
    NET_INCOME = "net_income"
    GROSS_PROFIT = "gross_profit"
    OPERATING_INCOME = "operating_income"
    ASSETS = "assets"
    LIABILITIES = "liabilities"
    EQUITY = "equity"
    CASH = "cash"
    OPERATING_CASH_FLOW = "operating_cash_flow"
    CAPEX = "capex"
    LONG_TERM_DEBT = "long_term_debt"
    SHARES_COVER = "shares_cover"
    SHARES_DILUTED_WEIGHTED = "shares_diluted_weighted"
    EPS_DILUTED = "eps_diluted"
    # tier 2 (§4.2, needed by M4's F/Z/M scores) — declared in M1, unmapped until M2
    ASSETS_CURRENT = "assets_current"
    LIABILITIES_CURRENT = "liabilities_current"
    RETAINED_EARNINGS = "retained_earnings"
    RECEIVABLES = "receivables"
    COGS = "cogs"
    SGA = "sga"
    DEPRECIATION_AMORTIZATION = "depreciation_amortization"
    INTEREST_EXPENSE = "interest_expense"
    SBC = "sbc"
    OPERATING_LEASE_LIABILITY = "operating_lease_liability"
    SHARE_ISSUANCE_PROCEEDS = "share_issuance_proceeds"
```

Both tiers from §4.2 are declared in M1 even though nothing maps to them until M2, for the reason
ROADMAP M2 gives: *"Building only the first tier means M4 stalls."* Declaring both now makes the
omission visible in M2 as an unmapped enum member rather than invisible as a metric nobody
thought of.

**`Metric` is defined here and referenced nowhere in `ingest/`.** See
[README § The M1/M2 seam](README.md#5-the-m1m2-seam).

### The two share counts are different types

DESIGN §4.2 and §5.4: using the cover-page count as an EPS or DCF denominator is a classic
error, and §5.4 says the distinction is "enforced by distinct types." A comment is not
enforcement, and an enum member is not either — both `Metric.SHARES_COVER` and
`Metric.SHARES_DILUTED_WEIGHTED` carry a `Decimal`, and a `Decimal` goes anywhere.

```python
CoverShares = NewType("CoverShares", Decimal)
"""dei:EntityCommonStockSharesOutstanding. Market cap only. Never a per-share denominator."""

DilutedShares = NewType("DilutedShares", Decimal)
"""us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding. Per-share math only."""
```

`NewType` over `Decimal` gives basedpyright a real, one-directional barrier at zero runtime cost:
`CoverShares` is assignable to `Decimal`, and `Decimal` is not assignable to `CoverShares`, so a
function annotated `def per_share(equity: Money, shares: DilutedShares)` rejects a
`CoverShares` argument.

Per CLAUDE.md — *for any sentence of the form "X cannot happen", write the test that attempts X
and asserts it fails* — this guarantee needs a test that performs the violation. A runtime test
cannot: both are `Decimal` at runtime, which is the whole point. So the test is type-level, and
it is the only test in the suite that works this way. See
[`06-testing.md` § Type-level guarantees](06-testing.md#5-type-level-guarantees).

### `Money`

```python
type Money = Decimal
```

An alias, not a `NewType`. CLAUDE.md's rule is `Decimal` for money, never `float`; the enemy is
`float`, and `Decimal` already wins that. A `NewType` here would force a wrapper call at every
arithmetic site — `Money(a + b)` — for no error it catches, and the cost of that friction is
paid by M5, which is nothing but arithmetic. The two share counts get `NewType` because there the
enemy is *another `Decimal`*, which an alias cannot see.

Where `Decimal` values are constructed is specified in [`04-parsers.md`](04-parsers.md), and it
matters more than the type: `Decimal(0.1)` is exact and wrong.

### `RawFact` — what M1 actually emits

```python
@dataclass(frozen=True, slots=True)
class RawFact:
    """One XBRL fact as filed. No metric assigned: that is M2's."""

    taxonomy: str                 # "us-gaap" | "dei" | "srt"
    tag: str
    unit: str                     # "USD" | "USD/shares" | "shares" | "pure"
    value: Decimal
    period: FiscalPeriod
    source: SourceRef

    # Preserved as filed, for M2's dedup and for the fixtures that demonstrate §4.2's traps.
    filing_fy: int | None         # §4.2(a): the *filing's* fiscal year. Never group by this.
    filing_fp: str | None         # "FY" | "Q1".."Q4". Same warning.
    frame: str | None             # SEC's own dedup selection. Peers only, never subject history.
```

`unit` is on the fact **[extends §3.2]** because §4.2(b) dedups by `(unit, start, end)`, so a
fact that does not carry its unit cannot be deduped correctly. It is also the field that catches
the mistake §4.2 warns about twice: revenue *excluding* vs. *including* assessed tax are
different numbers, and EPS arrives under `USD/shares` rather than `USD`.

`frame` is carried but its use is restricted: §4.2 says SEC's frame selection is not
point-in-time stable, so it is legitimate for peer cross-sections and illegitimate for the
subject company's history. Carrying it and forbidding one use is better than dropping it, because
M4's `peers.py` genuinely wants it. The restriction gets a test.

`filing_fy` / `filing_fp` are carried for one reason: §4.2(a) is the trap most likely to be
"fixed" by a future contributor who finds grouping by `start`/`end` awkward. A fixture that shows
Apple's FY2018 revenue tagged `fy: 2019` and again `fy: 2020` is the argument, and it needs the
fields to exist.

### `Fact`

```python
@dataclass(frozen=True, slots=True)
class Fact:
    """A normalized, metric-assigned figure. Constructed in M2, not M1."""

    metric: Metric
    value: Decimal
    period: FiscalPeriod
    source: Provenance          # SourceRef, or Derivation for a computed figure
    unit: str
```

Declared in M1 so M2 has a target, constructed only in M2. `source: Provenance` rather than
`SourceRef` is spec question 2.

### `market_cap`

ROADMAP M1 puts market cap in M1. It is pure arithmetic over facts and a price, so it lives here
rather than in `ingest/` — which also keeps `ingest/` free of the one place a share-count tag has
to be named.

```python
def market_cap(
    *,
    price: Decimal,
    price_source: SourceRef,
    share_facts: Sequence[RawFact],
) -> tuple[Money, Derivation] | None: ...
```

- Sums `share_facts` across classes (§5.4: GOOGL/GOOG, FOX/FOXA), and the returned `Derivation`
  names the classes in `note` so the report can state which were included, as §5.4 requires.
- Returns the `Derivation`, not just the value. A caller that wants only the number has to
  discard the provenance explicitly, which is the point.

### Empty input returns `None`; malformed input raises

The return is optional, and the reason is a live observation rather than defensiveness: a
`companyfacts` payload can contain no `dei` section at all, so a NASDAQ filer can have no
cover-page share count and therefore no market cap
([`04-parsers.md` §2](04-parsers.md#2-companyfactspy-m1a)).

The two failure modes are different and are handled differently:

| Input | Result | Why |
|---|---|---|
| `share_facts` empty | `None` | An **absence**. Expected, common, and the caller records it in the coverage report. |
| A fact that is not `INSTANT`, not `dei:EntityCommonStockSharesOutstanding`, not unit `shares` | raises | Malformed. Someone passed the wrong facts. |
| Facts with differing `end` dates | raises | A market cap summed across two cover pages. Plausible-looking and wrong — the failure this project exists to avoid. |

That split is DESIGN §14's own distinction — a run that failed versus a run that succeeded in
reporting bad news — applied at function scope. `price` is non-optional, so an empty
`share_facts` is the function's *only* absence condition and a bare `None` is unambiguous
about which one occurred.

**The check lives here rather than in the caller**, and that is the decision this section exists
to record. Putting it in the `fetch` command would work today and would have to be repeated in
M3's renderer and M4's `peers.py`, each of which reaches for a market cap independently. One of
the three would eventually forget, and the symptom is not a crash — it is a `0` propagating into
every multiple in report section 3 and into the valuation sub-score, which is the specific
outcome [`05-prices.md` § Market cap](05-prices.md#6-market-cap) rules out. An `Optional` return
makes forgetting a type error instead.

Multi-class voting rights are ignored — recorded in DESIGN §12 as a known unmodeled item, not
rediscovered here.

---

## 4. What this package does not contain

Stated because each is a plausible place for the wrong thing to land:

- **No tag fallback chains.** `normalize/tags.py`, M2.
- **No `as_of` filtering.** Filtering happens in `normalize` (§4.2b). `domain/` has no opinion
  about which restatement wins, and neither does the cache — see
  [`02-cache.md`](02-cache.md#5-what-the-cache-must-not-do).
- **No `FinancialHistory` or `CoverageReport`.** §3.2 sketches both; both are M2's output and
  neither has an M1 consumer. Declaring them now would fix their shape before the code that
  fills them exists, which is what ROADMAP's "created per milestone" rule exists to prevent.
- **No Q4 derivation.** M2. But the `Derivation` record it needs is here, which is the whole
  argument for spec question 2.

# M2 — Statements and coverage

`normalize/statements.py`. Assembles the resolved facts into `FinancialHistory`, and measures what
is missing into `CoverageReport`.

DESIGN §3.2 sketches both types and labels the surrounding block "sketches until M2 builds them."
What follows is the proposed final form, with every departure marked **[extends §3.2]** and
carried into [README § Spec questions](README.md#7-spec-questions).

The measurement half is the more consequential one. §4.2's closing argument is that coverage
"below a configurable floor degrades the report's confidence rating and can trigger an
'insufficient data' verdict" — so the coverage number feeds §9.2's confidence rating directly, and
a coverage figure with an unstated denominator is a confidence rating with an unstated meaning.

---

## 1. `FinancialHistory`

```python
@dataclass(frozen=True, slots=True)
class FinancialHistory:
    # identity — §3.2's sketch
    cik: int
    ticker: str
    fiscal_year_end: str | None                # "MMDD", from CompanyProfile; None if absent

    # series — §3.2's sketch, retyped
    annual: Mapping[Metric, tuple[Fact, ...]]
    quarterly: Mapping[Metric, tuple[Fact, ...]]
    coverage: CoverageReport
    as_of: date                                # no fact with filed > as_of is included

    # [extends §3.2]
    name: str                                  # display name, from submissions — never companyfacts
    sic: int | None
    sic_description: str | None
    window: tuple[date, date]                  # the lookback window actually applied
    quarters_available: int
    restatements: tuple[Restatement, ...]
    market_cap: tuple[Money, Derivation] | None = None
```

Five departures, each with a consumer that does not work without it.

**`Mapping[Metric, tuple[Fact, ...]]`, not `dict[Metric, list[Fact]]`.** A frozen dataclass whose
fields are a mutable dict of mutable lists is frozen in name only, and every other type in
`domain/` is frozen with tuples. The concrete gain is not aesthetic: `report.json` and the
determinism gate both need the series to be unable to change between being built and being
serialized, and M4 receives this object and computes over it.

**`name`, `sic`, `sic_description`** come from `CompanyProfile` and are here because the consumers
are downstream of normalization and would otherwise each have to carry `CompanyProfile` alongside.
`sic` in particular is read by three separate things — §6.10's bank/REIT refusal, §6.1's Altman
variant selection, and §6.5's peer cohort — and threading it separately is how one of them ends up
reading a different value. The **display name comes from submissions, never from
`companyfacts.entityName`**, which is EDGAR-conformed uppercase; that rule is M1's and is restated
here because this is the type the cover page reads from.

**`window` and `quarters_available`** because §5.1 gates on quarters of history at two thresholds
(below 12, valuation omitted; 12–20, low-confidence banner) and §6.4 lists "lookback shorter than
requested" as a data-integrity flag. Both need the requested window and the delivered one to be
comparable, and only the object that applied the window knows both.

**`market_cap`** is M1's, computed in `fetch.py`, and it is a company-level figure with no series.
It is carried through rather than recomputed. §9.1's section 3 prints it against peer percentiles
and there is nowhere else for it to live that M3 can reach without also reaching into `FetchResult`.

**Not here: `manifest_hash`, config, prompt versions.** §9.1's appendix prints all three and they
are run metadata rather than financial history. They belong to `report.json`'s envelope — see
[`04-serialize.md` § 1](04-serialize.md#1-what-reportjson-is-at-m2). Putting a cache fingerprint
inside a `FinancialHistory` would make two histories built from the same facts compare unequal.

### The assembly function

```python
def build_history(
    facts: CompanyFacts | None,
    *,
    ticker: str,
    cik: int,
    name: str,
    profile: CompanyProfile | None,
    filings: Sequence[FilingRow],
    window: tuple[date, date],
    as_of: date | None,
    market_cap: tuple[Money, Derivation] | None = None,
) -> FinancialHistory: ...
```

Pure, and every argument is something `FetchResult` already holds — so the `facts` command is
`run_fetch` followed by `build_history`, with no second fetch path to keep in sync. That is the
reason the signature takes the parsed objects rather than a ticker: a normalization layer that can
fetch is a normalization layer that will, and then a warm run makes an HTTP call.

### Both payloads are optional, because M1 already makes them optional

`FetchResult.facts` and `FetchResult.profile` are both `| None`, and each has a live path that
leaves them so: `fetch.py:191` records `submissions: CIK … has no submissions payload` on a 404,
and `fetch.py:253` records `companyfacts: none published for CIK …`. Both are **absences** under
M1's own rule — the run exits 0 and prints them — and [README § 4](README.md#4-exit-codes) lists
"no `companyfacts` published for the CIK" as a normal outcome of `facts`.

A non-optional signature would contradict that in two ways at once: it would not type-check under
strict basedpyright against `FetchResult`, and the first thin-coverage ticker anyone tried would
crash on a documented, common condition. So both widen, and the two absences are kept
**independent** rather than collapsed, because they degrade differently:

| Absent | Consequence |
|---|---|
| `facts is None` | Every metric is absent. A `FinancialHistory` is still returned, with a spine (the filings are unaffected), every `MetricCoverage` at `filled=0`, and a `companyfacts_absent` finding. |
| `profile is None` | No `sic`, no `sic_description`, no `fiscal_year_end`, and no filings — so the spine is empty and falls back to `OBSERVED`. The series are unaffected. |
| both | An empty history with an `OBSERVED` spine over nothing. `facts` prints a table of dashes and two findings, and exits 0. |

**`cik` and `name` are therefore separate arguments rather than read off `profile`.** Both are
always known: a ticker that did not resolve exited 2 in `tickers.py` before `build_history` was
reachable, and `TickerRow` carries a mixed-case `name` from `company_tickers_exchange.json`.

Getting them there costs **the one edit M2 makes to an M1 file**: `FetchResult` gains
`cik: int | None` and `name: str | None`, populated in `_resolve_ticker`, which already has the
`TickerRow` and currently lets it fall out of scope (`fetch.py:135`). Both are optional to match
the dataclass's incremental-fill style and both are set by the time `run_fetch` returns, since
`_resolve_ticker` raises exit 2 on the path that would leave them unset.

When
`profile` is present its name wins — M1's rule that the display name comes from submissions is
about *`companyfacts.entityName`* being EDGAR-conformed uppercase, and the ticker file is not.
When `profile` is absent, the ticker file's name is the only one there is, and printing it beats
printing the CIK.

**`fiscal_year_end` widens to `str | None`** for the same reason: it exists only on
`CompanyProfile`, and there is no honest value to invent for a filer whose submissions payload
404'd. A departure from §3.2's sketch, folded in with the rest.

**Returning an empty history rather than raising is the §6.10 argument applied one layer down.**
*"A blank space with an explanation beats a confident wrong number"* — and an exception is not a
blank space with an explanation, it is a traceback. §14 says the same thing in the exit-code
taxonomy: thin data degrades coverage and confidence, it does not abort.

`as_of=None` resolves to `date.today()`? **No.** It stays `None` through the pipeline as "no
filtering" and is recorded on the output as the resolved `as_of` the command computed at its
boundary. Nothing under `normalize/` reads a clock — see
[`05-testing.md` § 4](05-testing.md#4-new-layering-rules).

---

## 2. The period spine

**[extends §3.2]** — and the part of this document most likely to matter in a year.

"% of periods filled" needs a denominator, and there are three candidates:

| Denominator | Problem |
|---|---|
| Periods in the requested window | A company that IPO'd two years into a 5-year window reports ~40% coverage on perfect data. Coverage then measures company age. |
| Periods for which *any* metric has a fact | Circular. A filer that tags nothing reports 100% of nothing. |
| **Periods the company actually reported in the window** | — |

The third is the one that measures what the number is supposed to mean: *of the periods this
company filed for, how many did we successfully tag?* It is also the only one independent of the
facts, which is what stops a tagging failure from shrinking its own denominator.

```python
class SpineOrigin(StrEnum):
    FILINGS  = "filings"      # derived from the filing history — the normal case
    OBSERVED = "observed"     # fallback; the coverage report says so

@dataclass(frozen=True, slots=True)
class PeriodSpine:
    annual_ends: tuple[date, ...]
    quarterly_ends: tuple[date, ...]
    origin: SpineOrigin

ANNUAL_FORMS:    Final = frozenset({"10-K", "10-KT"})
QUARTERLY_FORMS: Final = frozenset({"10-Q", "10-QT"})
```

Built from `FilingRow.report_date`, not `filed`: the report date is the period end, the filing date
is two months later, and using the wrong one shifts the whole spine by a quarter.

Four construction rules, each of which is wrong in a specific way if omitted:

1. **Amendments collapse into the filing they amend.** `10-K/A` matches `ANNUAL_FORMS` after
   stripping the `/A` suffix, and the spine is deduped on `(kind, report_date)`. Without this, a
   filer that amended two years of 10-Ks has an annual denominator two larger than the number of
   years it existed, and coverage caps out around 66%.
2. **Annual report dates are also quarterly spine entries.** A filer files three 10-Qs a year; the
   fourth quarter's end date appears only on the 10-K. A quarterly denominator built from 10-Qs
   alone is three per year, and any filer whose Q4s were derived reports 133% coverage.
3. **A `FilingRow` with `report_date is None` contributes nothing.** The `ARXS` fixture carries
   `reportDate: ""` on several rows, normalized to `None` by `_fields.as_date` (M1). Those filings
   are still in the list; they are just not spine evidence.
4. **The spine is windowed on `report_date`, using the same window the facts are.** Otherwise the
   numerator and denominator are measured over different intervals, which produces coverage above
   100% at the near edge and below it at the far edge, for filers whose fiscal year ends near the
   window boundary.

**A spine can be empty in one bucket and populated in the other, and that is the common case, not
the edge.** `ARXS` is the worked example: its submissions payload holds one `10-Q`
(`reportDate: 2026-03-31`) and **no `10-K` at all** — the rest is `S-1/A`, `8-K`, `3`, `4`,
`8-A12B`, `EFFECT`. So its annual `expected` is zero while its quarterly `expected` is one. Annual
coverage is then `None`, not 0% and not 100%, which is the whole reason `fill_rate` is optional
([§ 3](#3-coveragereport)) — a recent registrant that has filed one quarterly report and no annual
report has not failed to be tagged.

**The fallback, and why it is labelled rather than silent.** A filer with **no** periodic filing of
either kind in the window — a registrant whose only forms are `S-1/A` and `8-K` — has a wholly
empty spine, and dividing by it is not a coverage number. The spine then falls back to `OBSERVED`:
the union of period ends across every resolved metric. That denominator is circular and coverage
computed against it is close to meaningless, which is exactly why `origin` is a field and why the
coverage report and the `facts` output both print it. A 100% figure that quietly came from an
`OBSERVED` spine is the single most misleading number this milestone could produce.

**No fixture currently produces an `OBSERVED` spine** — `ARXS`, the closest candidate, has that
10-Q. That gap is recorded in [`05-testing.md` § 2](05-testing.md#2-fixtures) alongside the other
five.

### Spine dates and fact dates are matched within ±3 days, one-to-one

The spine is `FilingRow.report_date`, from the filing header. A fact's `period.end` comes from the
XBRL context in the instance document. They are *usually* the same date and they are not the same
field, and every other date comparison in this design already carries an explicit tolerance for
exactly that reason — [`02-facts.md` § 5](02-facts.md#5-residual-recovery-one-rule-two-names)'s
guards 3 and 4 allow ±3 days at each seam because filers record period boundaries inconsistently
at the day level.

An exact-equality match here would silently *undercount* coverage on any filer whose two dates
disagree by a day: the fact is present, the metric is tagged, and the report says it is missing.
That is the same "wrong quietly" shape the rest of this document is built to refuse, arriving in
the one number that gates the milestone.

So the match is **nearest spine date within 3 days, and one-to-one** — each fact end claims at most
one spine slot and each slot is claimed at most once. One-to-one matters: without it, two facts a
day apart could both satisfy one spine date and push `filled` past `expected`, which is the bug the
100%-bound is supposed to make impossible.

Three days is safe at both granularities by a wide margin — annual spine dates are a year apart and
quarterly ones about ninety days, so the nearest match is never ambiguous.

**Inexact matches are counted per metric** (`spine_date_inexact`). One or two is ordinary. A filer
where every period matches inexactly has a systematic disagreement between its filing header and
its own XBRL contexts, which is a finding about that filer rather than a tolerance to widen.

**Facts outside the spine are kept and counted, not dropped.** A period whose end date is not
within tolerance of any spine date is real data — usually a fiscal-year change, or a report date
amended after the fact. It stays in the series, does not contribute to the numerator, and is
counted as
`periods_outside_spine` per metric. Coverage is therefore bounded at 100% by construction, and the
count is what tells you whether the bound is doing any work.

---

## 3. `CoverageReport`

§3.2 asks for "which metrics, which tag won, % filled." §9.1's appendix asks for "tag provenance
per metric," §9.2's confidence rating consumes the aggregate, and ROADMAP M2's exit criterion is
stated *per tier*. So the report is per-metric with tier aggregates, not a single number.

```python
@dataclass(frozen=True, slots=True)
class MetricCoverage:
    metric: Metric
    tags_used: tuple[str, ...]          # qualified, in first-use order: ("us-gaap:SalesRevenueNet", …)
    derived_periods: int                # from a cross-metric derivation
    recovered_periods: int              # from Q4 or YTD residual recovery
    filled: int                         # spine periods with a value
    expected: int                       # spine periods
    periods_outside_spine: int
    dropped_other_bucket: int
    dropped_unit_mismatch: int
    sign_anomalies: int

    @property
    def fill_rate(self) -> Decimal | None: ...   # None when expected == 0 — not 0.0, not 1.0

@dataclass(frozen=True, slots=True)
class CoverageReport:
    spine: PeriodSpine
    annual: Mapping[Metric, MetricCoverage]
    quarterly: Mapping[Metric, MetricCoverage]
    findings: tuple[Finding, ...]

    def tier_fill_rate(self, tier: Tier, bucket: Bucket) -> Decimal | None: ...
```

Three details that are decisions:

**`fill_rate` is `None` when `expected` is zero, not `0` and not `1`.** Both defaults are lies in
opposite directions and both are the kind of lie that propagates into a weighted mean. §9.2's
confidence rating averages over metrics; a metric with no expected periods must be excluded from
that average, and `None` is what makes excluding it the only thing a caller can do.

**`tags_used` is a tuple because a stitch is normal.** A single "which tag won" field cannot
represent Apple's revenue, which is `SalesRevenueNet` for FY2016–17 and
`RevenueFromContractWithCustomerExcludingAssessedTax` for FY2018 onward. The appendix prints the
tuple, and `len(tags_used) > 1` is the stitch finding.

**Both tiers are measured separately and the split is declared in the registry, not inferred.**
ROADMAP M2's exit criterion is "≥90% coverage across 20 NASDAQ names on **both** the DCF metric set
and the quality-score metric set." A single aggregate hides a tier-2 failure behind tier-1 success
— which is precisely the outcome ROADMAP's "building only the first tier means M4 stalls" is
warning about, arriving one milestone later and disguised as a passing gate.

---

## 4. Findings M2 records

```python
@dataclass(frozen=True, slots=True)
class Finding:
    code: str                          # stable and machine-readable; the report.json key
    metric: Metric | None
    detail: str                        # human-readable, printed by `facts` and in §9.1's caveats
    evidence: tuple[Provenance, ...] = ()
```

**No severity field, deliberately.** §6.2 gives severity to `analyze/flags.py`'s rule registry, one
rule per file with its own test. A severity assigned in `normalize/` is a severity assigned twice,
and the two copies diverge on the first rule M4 tunes. M2's job is to state what is true about the
data; deciding what it means is M4's, and the boundary is worth keeping sharp because every one of
these findings is a candidate flag.

The findings, with the §6.4 items marked — those are the ones DESIGN already commits to rendering:

| Code | Meaning | §6.4 |
|---|---|---|
| `coverage_below_floor` | a metric's fill rate is under the configured floor | ✓ |
| `q4_derived` | one or more Q4s came from `FY − (Q1+Q2+Q3)` | ✓ |
| `q4_absent` | an annual period with no Q4, filed or derivable | ✓ |
| `series_stitched` | more than one chain member contributed | ✓ (ASC 606) |
| `restated` | a period's **value changed** across filings in the window | ✓ |
| `window_truncated` | history is shorter than the requested lookback | ✓ |
| `companyfacts_absent` | no XBRL facts published for the CIK; every metric absent — [§ 1](#both-payloads-are-optional-because-m1-already-makes-them-optional) | |
| `submissions_absent` | no filing history; no SIC, no fiscal year end, and the spine falls back | |
| `spine_observed` | the coverage denominator is circular — [§ 2](#2-the-period-spine) | |
| `spine_date_inexact` | a fact's period end matched a spine date within tolerance but not exactly — [§ 2](#spine-dates-and-fact-dates-are-matched-within-3-days-one-to-one) | |
| `exclusivity_switch` | a filer moved permanently between two members of an exclusivity group — [`01-tags.md` § 5](01-tags.md#5-exclusivity-groups) | ✓ |
| `net_income_scope_mismatch` | `ProfitLoss` resolved while equity is parent-only — [`01-tags.md` § 3](01-tags.md#3-tier-1--the-dcf-metric-set) | |
| `liabilities_nci_approximated` | the derivation used parent-only equity — [`01-tags.md` § 9](01-tags.md#9-the-equity-trap-in-the-liabilities-derivation) | |
| `sga_composed` | SG&A summed from two component tags | |
| `sign_anomaly` | a fact contradicts its metric's declared sign — [`01-tags.md` § 8](01-tags.md#8-sign-conventions) | |
| `unit_mismatch` | facts excluded for unit — [`01-tags.md` § 7](01-tags.md#7-units) | |
| `other_bucket_drops` | facts dropped as `OTHER` — [`02-facts.md` § 4](02-facts.md#4-bucketing-and-the-two-questions-m1-deferred) | |
| `periods_outside_spine` | periods present that the filing history does not account for | |

**`restated` fires on a value change, not on a re-filing.** The AAPL fixture's quarter ending
2019-06-29 appears under four accessions with four `filed` dates and the same value each time.
That is a comparative carried forward, not a restatement, and flagging it would put a false
accounting signal on the flagship fixture — which is a good reason for the fixture to exist. The
`Restatement` record keeps all four generations regardless
([`02-facts.md` § 8](02-facts.md#8-the-restatement-record)); the *finding* is keyed on whether the
number moved.

---

## 5. The two gates M2 records and does not enforce

§6.10 refuses a valuation for banks, insurers, REITs and pre-revenue biotech; §5.1 refuses one
below 12 quarters of history. Both decisions are M4's and M5's. M2 supplies the inputs and
**must not make the call**, for a reason that is not obvious: a refusal reached inside
normalization is a refusal with no report attached. §6.10's whole argument is that *"a blank space
with an explanation beats a confident wrong number"* — the explanation is a rendered section, and
a `normalize/` layer that returned early or raised would produce neither. §14 says the same thing
in the exit-code taxonomy: exit 3 is "insufficient data, **report still written**."

So `FinancialHistory` carries what the gates read:

| Gate | Input | Where |
|---|---|---|
| §6.10 banks and insurers | SIC 6000–6499 | `history.sic` |
| §6.10 REITs | SIC 6798 | `history.sic` |
| §6.10 pre-revenue | a `REVENUE` series that is absent or all-zero | `coverage.annual[Metric.REVENUE]` |
| §5.1 valuation floor | fewer than 12 quarters | `history.quarters_available` |
| §5.1 low-confidence band | 12–20 quarters | same |

Tested against the fixtures: `BANK` (SIC 6022, no `OperatingIncomeLoss` at all) and `REIT` (no
operating income, and a capex chain miss on top) must both produce a `FinancialHistory` with
populated revenue, net income and assets series and a coverage report naming the misses — not an
exception, not an empty history. `IPO` has exactly six quarters, so `quarters_available == 6`
lands on the wrong side of §5.1's 12-quarter boundary, and the assertion is on the count rather
than on any downstream refusal, which does not exist yet.

**`BANK` and `REIT` have no submissions fixture**, so their SIC lives in `PROVENANCE.md` rather
than in a payload M2 can read. That gap is recorded there and it means the SIC half of §6.10 is
not testable end-to-end in M2 — see [`05-testing.md` § 2](05-testing.md#2-fixtures).

---

## 6. What `statements.py` does not do

- **No ratios, no growth rates, no scores.** `analyze/fundamentals.py` and `analyze/quality.py`,
  M4. A margin computed here would be computed again there, and the two would diverge.
- **No peer comparison.** §6.5, M4. Peer cohorts come from the frames API, which M1b parses and
  which §4.2 forbids for the subject company's own history.
- **No verdict, no confidence rating.** §9.2, M5. M2 supplies coverage; the rating is a weighted
  composite over coverage, quarters of history, data-integrity flags, Monte Carlo rejection rate
  and backtest calibration — four of whose five inputs do not exist yet.
- **No severity.** [§ 4](#4-findings-m2-records).
- **No decision about which restatement to display.** ROADMAP open question 10. The data to answer
  it either way is retained.

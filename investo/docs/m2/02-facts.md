# M2 — Fact normalization

`normalize/facts.py`. Dedup, `as_of` filtering, duration bucketing, YTD differencing and Q4
derivation. DESIGN §4.2(a)(b)(c) is normative on all five; this document fixes the **order** they
run in, which §4.2 does not state and which two of them are wrong without.

Everything here is pure. No I/O, no clock, no network — see
[`05-testing.md` § 4](05-testing.md#4-new-layering-rules).

---

## 1. The pipeline order, and why it is not negotiable

```
CompanyFacts.facts                       (taxonomy, tag) -> RawFact rows, M1
   │
   ├─ 1. as_of filter        drop every RawFact with source.filed > as_of
   ├─ 2. unit filter         drop facts whose unit is not the chain's                    (tags.py)
   ├─ 3. dedup               (taxonomy, tag, unit, start, end) -> the survivor with max(filed)
   ├─ 4. window filter       drop periods wholly outside the lookback window
   ├─ 5. chain resolution    period-wise, per metric                                     (tags.py)
   ├─ 6. residual recovery   Q4 from annual; quarters from YTD
   ├─ 7. cross-metric        gross profit, liabilities, EPS                              (tags.py)
   └─ 8. sort                a total key, every series
   │
   ▼
Fact series, per metric, per bucket
```

Three orderings in that list are decisions rather than convenience, and each has a wrong
alternative that produces plausible output.

**`as_of` runs before dedup, and reversing them is a lookahead leak.** §4.2(b) gives the
point-in-time rule as `max(filed) where filed <= as_of`. Deduping first and filtering after
evaluates `max(filed)` over the full set, and then discards the winner if it was filed too late —
leaving a **hole** where the correct answer is the value that was current on that date. Checked
against `tests/fixtures/edgar/companyfacts/RESTATER.trimmed.json`, whose single period
2020-01-01 → 2020-12-31 carries four filings:

| `filed` | `val` |
|---|---|
| 2021-02-24 | 812,000,000 |
| 2021-08-05 | 806,500,000 |
| 2022-02-23 | 791,200,000 |
| 2023-02-22 | 774,900,000 |

At `--as-of 2021-06-30`, filter-then-dedup yields **812,000,000** — the number that was true on
that date, which is the whole point of §8's point-in-time reconstruction. Dedup-then-filter yields
nothing, and a backtest that silently loses its most recent fiscal year at every date is a
backtest measuring something else. `PROVENANCE.md` records 812,000,000 as this fixture's expected
answer, so the assertion is already written down; the ordering is what makes it come out.

**The window filter runs after dedup, not before.** A fact's `filed` date and its period have no
fixed relationship — a comparative in a later 10-K is filed years after the period it describes.
Filtering the window on `filed` would drop restatements of in-window periods; filtering it on
`period.end` before dedup is harmless but does no work, since dedup is keyed within a period
anyway. It runs after so that the dedup step sees every generation of an in-window period.

**Residual recovery runs after chain resolution, not before.** `Q4 = FY − (Q1+Q2+Q3)` must
subtract quarters of *the same metric*, and "the same metric" is only defined once the chain has
chosen a tag per period. Deriving Q4 per tag and then resolving would subtract three
`SalesRevenueNet` quarters from a `RevenueFromContractWithCustomer…` year on any filer straddling
the ASC 606 boundary mid-year — the number comes out, and it is not revenue.

---

## 2. `as_of`

```python
def filter_as_of(facts: Sequence[RawFact], *, as_of: date | None) -> tuple[RawFact, ...]: ...
```

Drops every fact with `source.filed > as_of`. `None` means no filtering, which is the current
view — §4.2(b)'s `max(filed)`, "right for what is true now."

Two properties, both tested by attempting the violation:

- **The filter is on `filed`, never on `period.end`, `report_date`, or `accepted_at`.** Filing an
  amendment on the last day before `as_of` for a period ending after it is legal and happens;
  `filed` is the only date that answers "could we have known this then."
- **`as_of` is resolved once, at the command boundary, and threaded down.** `cli._resolve_as_of`
  (M0) already rejects a future date with exit 5. Nothing below the command reads a clock to
  default it — a default resolved deep in the pipeline makes two runs on either side of midnight
  produce different reports, and §11's determinism gate would report it as a nondeterminism bug
  rather than as the design mistake it is.

`as_of` does **not** filter prices. M1's `_fetch_prices` already takes the last bar at or before
`as_of`, tested by `test_market_cap::test_price_is_last_bar_at_or_before_as_of`. M2 does not
revisit it and does not recompute market cap; the two `as_of` paths are separate and both are
tested, which is worth stating because a single "as-of filter" that appears to cover everything is
how one of them quietly stops being applied.

---

## 3. Dedup

§4.2(b): dedup by `(unit, start, end)`, then take `max(filed)`.

**The full key is `(taxonomy, tag, unit, start, end)**, and the difference is a clarification
rather than a conflict. `companyfacts` nests facts under taxonomy → tag → unit, so §4.2's
three-part key is already within-tag by construction. Writing the key out in full matters because
M2 flattens that nesting to resolve chains, and a three-part key applied to the flattened set
would dedup a `Revenues` fact against a `SalesRevenueNet` fact for the same period — two
different concepts collapsed to whichever was filed later.

```python
def dedup(facts: Sequence[RawFact]) -> tuple[RawFact, tuple[RawFact, ...]]: ...
#          -> (survivor, superseded)
```

The superseded facts are **returned, not discarded.** They are the restatement record
([§ 8](#8-the-restatement-record)), and recovering them later would mean re-parsing.

**Ties on `filed` break on `accession`, ascending.** Two accessions filed the same day carrying
the same period is ordinary — a 10-K and an 8-K exhibit, or an original and an amendment filed
together. Without an explicit tiebreak the survivor depends on dict iteration order over the
parsed payload, which is stable in CPython and is not a guarantee anybody should be resting a
report on. `Accession` is `order=True` (M1), so the tiebreak is one sort key rather than a
special case.

**Equal values are still deduped, and the survivor's `SourceRef` is the late one.** In the AAPL
fixture the quarter ending 2019-06-29 appears under four accessions with four `filed` dates and
**the same value** — 53,809,000,000 each time. The number does not move, so no test asserting on
values catches a broken dedup here; what moves is the accession printed in the appendix. The test
therefore asserts on `fact.source.accession`, not on `fact.value`, which is the CLAUDE.md rule
about asserting the derivation rather than the value applied to a case where the value is
uninformative by construction.

---

## 4. Bucketing, and the two questions M1 deferred

`domain/periods.classify` (M1) is total: every `(start, end)` lands in exactly one of `INSTANT`,
`QUARTER`, `ANNUAL`, `YTD`, `OTHER`. M1 deliberately handed `YTD` and `OTHER` over labelled rather
than deciding what to do with them, and `docs/m1/README.md` spec question 6 says M2 inherits an
answer. Answering both:

**`YTD` is differenced where it recovers a period the filer did not report discretely, and dropped
otherwise.** See [§ 7](#7-ytd-differencing). It is never carried into a series as-is: a 180-day
figure sitting in a quarterly series is a doubled quarter, and a chart of it looks like a good
half-year.

**`OTHER` is dropped, and counted.** The bucket holds durations under 80 days and over 380. The
short end is transition periods and stub periods after a fiscal-year change; the long end is
multi-year cumulative disclosures and the occasional 53-week year filed with a mis-stated start.
None is usable in an annual or quarterly series and none is recoverable without judgment about
what the filer meant. Dropping is right; dropping **silently** is not, so the count appears in the
coverage report per metric. A filer whose facts are 40% `OTHER` has had a fiscal-year change in
the window, and that is a §6.4 data-integrity finding rather than an ingestion detail.

**The narrow buckets are confirmed as deliberate, and the cost is now stated.** §4.2(c) sets
annual at 350–380 days and quarterly at 80–100. SEC's own frames API uses 365 ± 30 (335–395) and
91 ± 30 (61–121). `docs/m1/README.md` spec question 6 flagged the divergence and asked M2 to
inherit an answer rather than a coincidence. Keep the narrow bands: they refuse an ambiguous
duration instead of mislabelling it, and a 61-day period admitted as a quarter is a two-month stub
charted as a quarter of revenue. The cost is that more facts route to `OTHER`, which is exactly why
`OTHER` is counted per metric rather than dropped into a global total — the measurement of what
the narrow bands cost is a per-metric number the coverage report prints, and if it turns out to be
large for a real filer that is an argument to revisit made of evidence.

**53-week years fall inside `ANNUAL` and need no special case.** A 53-week year is 371 days, and
371 is in `range(350, 381)`. Stated because it is the first thing a reader assumes the narrow band
breaks.

---

## 5. Residual recovery: one rule, two names

Q4 derivation and YTD differencing are the same operation — subtract a set of shorter periods that
tile the front of a longer one, and keep the residual — so they are one function with two rule
labels for provenance.

```python
def residual(
    whole: Fact,
    parts: Sequence[Fact],
    *,
    rule: str,
) -> Fact | None: ...
```

`None` unless **all** of the following hold. Each is a guard against a specific way the naive
version produces a wrong number that looks right:

1. **The metric is `FLOW` and `subtractable`.** `INSTANT` and `PER_SHARE` are excluded by
   [`01-tags.md` § 6](01-tags.md#6-aggregation-class); so is `SHARES_DILUTED_WEIGHTED`, whose
   annual figure is a weighted average and not a sum.
2. **Every part shares the whole's `unit`.**
3. **The parts are non-overlapping and ordered**, and `parts[0].period.start` is within 3 days of
   `whole.period.start`.
4. **No seam gap exceeds 3 days.** Filers record period boundaries inconsistently at the day level
   — one filer's Q1 ends 2019-03-30 and Q2 starts 2019-03-31, another's Q2 starts 2019-04-01 — and
   a zero-tolerance check fails on correct data. Three days absorbs that and is far short of any
   real missing period.
5. **The residual period classifies as the kind being recovered.** This is the load-bearing guard
   and it subsumes most of the others. The residual runs from `parts[-1].period.end + 1 day` to
   `whole.period.end`, and `domain.periods.classify` must return `QUARTER` for it. If a quarter is
   missing from `parts`, the residual is ~180 days, classifies as `YTD`, and the derivation does
   not fire — where the naive version would have emitted a two-quarter figure as Q4.

The returned `Fact` carries `Derivation(rule=..., inputs=(whole.source, *[p.source for p in
parts]))`. `Derivation.refs()` therefore flattens to four accessions for a derived Q4, which is
what §3.2 requires and what the appendix prints.

**Nothing is derived from a derived part.** A Q4 recovered by subtraction is not eligible to be a
part in another subtraction, and a quarter recovered from YTD is not eligible to be one of the
three quarters in a Q4 derivation. Two levels of subtraction accumulate two rounding differences
and a compounding of any single mis-tagged input, and the resulting figure traces to eight
accessions in a way no reader can check. The rule is enforced by construction — residual recovery
runs once, over as-filed facts only — and it is tested by attempting the second level.

---

## 6. Q4 derivation

§4.2(c): *"discrete Q4 is often never tagged: derive `Q4 = FY − (Q1+Q2+Q3)`, and don't assume
either behavior, since it varies by issuer **and** by year within the same issuer."*

The last clause is what makes an unconditional rule wrong, and
`tests/fixtures/edgar/companyfacts/NOQ4.trimmed.json` is built to prove it:

| Fiscal year | Facts present | Correct behaviour |
|---|---|---|
| 2022 | Q1 240M, Q2 258M, Q3 266M, FY 1,065M | derive Q4 = 301M |
| 2023 | Q1 240M, Q2 258M, Q3 266M, **Q4 301M**, FY 1,065M | derive **nothing** |

A rule that always subtracts emits a second Q4 for 2023 and the series has five quarters in a
year. A rule that never subtracts loses 2022's fourth quarter — 28% of the year's revenue — and
reports the remaining three as the year. So:

```python
def derive_q4(annual: Fact, quarters: Sequence[Fact]) -> Fact | None: ...
```

fires **only** when no quarter in `quarters` ends on `annual.period.end`. The presence test is on
the period end date, not on a count and not on `filing_fp` — §4.2(a) forbids reading `fp`, and a
count of three would fire on a year missing its Q2 as readily as on one missing its Q4.

The derived period for FY2022 is 2022-10-01 → 2022-12-31: 92 days inclusive, which
`classify` returns `QUARTER` for, so guard 5 passes. The arithmetic is
`1,065 − (240 + 258 + 266) = 301`, which equals the Q4 the fixture reports for 2023 — the fixture
is constructed so the derived answer for one year is independently checkable against a filed
answer for the other.

**The test asserts the derivation, not the number.** Per CLAUDE.md: an assertion that FY2022's Q4
is 301,000,000 passes under a rule that subtracts the wrong things and happens to agree at this
input. The assertions are that FY2023 produces exactly four quarters (not five), that FY2022's
derived Q4 carries a `Derivation` whose `refs()` names four accessions, and that removing Q2 from
FY2022 produces **no** Q4 rather than a 550-day figure.

---

## 7. YTD differencing

A 10-Q carries the discrete quarter *and* the cumulative year-to-date figure. For most filers the
discrete quarter is present and the YTD fact is redundant; for filers that present cumulatively
only, the discrete quarters exist nowhere and the series is empty without differencing.

The rule is [§ 5](#5-residual-recovery-one-rule-two-names) again, with `parts` being the shorter
YTD period and `whole` the longer:

```
Q2 = H1 − Q1        parts = [YTD through Q1], whole = [YTD through Q2]
Q3 = 9M − H1
```

and it fires only where the discrete quarter is absent, tested the same way as Q4. The residual
must classify as `QUARTER`, so a filer that files YTD at Q1 and then nothing until the 10-K
produces no differenced quarters rather than a 270-day figure labelled Q3.

Where both the YTD fact and the discrete quarter exist, **the discrete quarter wins and the YTD
fact is dropped, not reconciled.** A reconciliation that flags a mismatch sounds better than it
is: small differences between a filer's discrete and cumulative figures are usually intra-period
reclassifications, they are routine, and a flag that fires on most filers is not a flag. The count
of YTD facts dropped as redundant appears in the coverage report, so the population is visible
without anyone having to act on it.

---

## 8. The restatement record

Dedup returns the losers. They are kept, per metric and per period:

```python
@dataclass(frozen=True, slots=True)
class Restatement:
    metric: Metric
    period: FiscalPeriod
    current: Decimal                              # the surviving value
    superseded: tuple[tuple[date, Decimal, Accession], ...]   # (filed, value, accn), ascending
```

Three uses, and it is cheap enough to be worth keeping for any one of them:

- **§6.4 lists "restatement detected in the window" as a data-integrity flag.** M4 renders the
  flag; M2 supplies the finding. Without the record, M4 would have to re-derive it, which means
  re-parsing.
- **ROADMAP open question 10** asks whether a restated series shows both versions or only the
  current one. That is a *display* question and this document does not answer it — but it cannot
  be answered later at all if the superseded values were thrown away in M2. Keeping them costs a
  few hundred bytes per affected period and keeps the question open.
- **It is the evidence that `as_of` works.** A report generated with `--as-of 2021-06-30` on the
  RESTATER fixture shows 812,000,000 with three *future* restatements absent from the record
  entirely — because the `as_of` filter ran first, so they were never candidates. That the record
  is empty at that date, rather than containing three entries marked "not yet filed," is the
  observable difference between filtering and post-hoc suppression.

A period whose four filings all carry the same value — the AAPL 2019-06-29 quarter — produces a
`Restatement` with three `superseded` entries and no value change. That is a **re-filing**, not a
restatement, and calling it one would put a false accounting flag on Apple. The finding
[`03-statements.md` § 4](03-statements.md#4-findings-m2-records) records is therefore keyed on
*value change*, while the record itself keeps every generation.

---

## 9. Every sort key must be total

`FiscalPeriod` is `order=True` and compares on `(end, kind)` — `start` is `compare=False`, because
M1 needed `None` not to be compared against a `date` when an instant and a duration share an end
date. `docs/m1/01-domain-types.md` § `PeriodKind and FiscalPeriod` records the consequence and
hands it here:

> two durations with the same `end` and the same `kind` compare equal … if they carry different
> values that is a restatement, which is M2's to resolve by `filed` date — not something a sort
> should be quietly breaking ties on.

Python's sort is stable, so `sorted(facts)` over an incompletely-ordered key returns input order
for the tied elements — and input order here descends from `dict` iteration over a parsed JSON
payload. That is deterministic in practice and is not a property anyone should be resting §11's
byte-identical-output gate on, particularly since the tie is broken differently the moment
`reduce_fixture.py` reorders anything.

**So no sort in M2 uses a bare `FiscalPeriod` or a bare `Fact` as its key.** Every sort names a
tuple that is total:

```python
_FACT_KEY = lambda f: (f.period.end, f.period.kind, f.period.start or date.min, f.unit,
                       f.source_sort_key)
```

with `source_sort_key` resolving to the accession for a `SourceRef` and to `(rule, first ref)` for
a `Derivation`. Enforced by an AST test that fails on `sorted(...)` or `.sort()` with no `key=`
anywhere under `normalize/` and `report/` — the same shape as the existing layering rules, and the
reason it is an AST test rather than a convention is that the failure it prevents is invisible in
every run that happens to agree.

---

## 10. What `facts.py` does not do

- **No tag knowledge.** It never names a `us-gaap` tag; it receives chains from
  [`01-tags.md`](01-tags.md) and applies them. Enforced by the allowlist in
  [`05-testing.md` § 4](05-testing.md#4-new-layering-rules), which holds `normalize/tags.py` and
  nothing else.
- **No coverage arithmetic**, for the reason in [`01-tags.md` § 11](01-tags.md#11-what-tagspy-does-not-do).
- **No interpolation, ever.** A missing period stays missing. Carrying a value forward or
  averaging two neighbours produces a fact with no `SourceRef`, and §3.2's rule is that such a
  number is not printed. There is no flag for it and no config option to enable it; the guarantee
  is that no code path constructs a `Fact` without a `Provenance`, which the type system already
  gives since `Fact.source` is non-optional.
- **No outlier rejection.** A revenue figure three orders of magnitude off its neighbours is
  either a filer error or a unit problem, and both are findings. Dropping it makes the series look
  clean and the coverage report look complete.
- **No currency conversion.** [`01-tags.md` § 7](01-tags.md#7-units) filters on unit; §12 records
  non-USD reporting currencies as out of scope.
- **No fiscal-year alignment across companies.** §12 records the up-to-two-quarter skew in peer
  comparison as a known unmodeled item. M2 produces one company's series on that company's fiscal
  calendar; aligning cohorts is M4's problem and M4 should inherit the skew visibly.

# M3 — The view model and the templates

`src/investo/report/format.py`, `src/investo/report/model.py`, `src/investo/report/templates/`.
DESIGN §9.1 is normative on structure; ROADMAP M3 selects sections 1, 3, 4, 9 and 10.

---

## 1. `format.py` — the only place a number becomes text

One module, no float, every function taking a `Decimal` and returning a `str`.

```python
def money(value: Decimal, *, scale: Scale = Scale.MILLIONS) -> str   # "391,035"
def exact(value: Decimal) -> str                                     # "391035000000.01"
def per_share(value: Decimal) -> str                                 # "6.13"
def shares(value: Decimal) -> str                                    # "15,744 M sh"
def percent(value: Decimal | None) -> str                            # "92.9%" | "n/a"
def ratio(value: Decimal | None) -> str                              # "1.42x" | "n/a"
def signed_percent(value: Decimal | None) -> str                     # "+8.2%" | "−3.1%"
```

Three properties are load-bearing, and the first two are inherited from `facts.py` deliberately —
the table `investo facts` prints and the table in the PDF appendix must agree digit for digit, and
two modules deciding what a million looks like is how they stop agreeing.

- **The unit decides the format, never the metric.** A per-share figure rounds to two places
  because rounding EPS to millions prints every filer's earnings as `0`; a share count is suffixed
  so `15,744` in the shares row cannot be read as $15.7bn.
- **`None` is `n/a`, never `0`.** A fill rate with no denominator and a fill rate of zero are
  different claims, and `0.0%` asserts the wrong one.
- **`exact` exists, and the appendix uses it.** `money` rounds for readability; the appendix's
  provenance table prints the value as filed, because that is the number a reader is checking
  against EDGAR. `391035000000.01` and `391,035` are both correct and only one of them can be
  verified.

Rounding is `Decimal.quantize` with `ROUND_HALF_EVEN`, stated because the default rounding of a
`Decimal` context is a process-global setting and a report whose figures depend on it is a report
that changes when something else in the process calls `getcontext()`.

---

## 2. `model.py` — what the template is allowed to see

```python
@dataclass(frozen=True, slots=True)
class ReportModel:
    cover: Cover
    snapshot: Snapshot
    history: HistorySection
    caveats: Caveats
    appendix: Appendix
    run: RunInfo
    brief: bool
    sources_used: tuple[Provenance, ...]
```

Every leaf in those five sections is a `str`, a `bool`, an `int`, a `ChartImage`, or a sequence of
them. The charts live on `HistorySection` rather than in a top-level mapping, so a template reaches
them through the section that renders them and there is no second key to keep in step.

`sources_used` is the one field nothing renders. It carries the `Provenance` behind every figure the
model produced, so `test_report_model::test_every_rendered_source_is_in_the_appendix` can assert
that set is covered by `report.json`'s interned array — two independent walks compared as a subset,
which is how ROADMAP M3's *"every number traceable"* becomes an assertion rather than a claim about
how carefully the templates were written.

**No `Decimal` and no `Fact` reaches a template.** That is the rule
[`README.md` § 7 question 6](README.md#7-spec-questions) proposes, and everything below is
downstream of it:

- A template **cannot** do arithmetic on a figure, because the figure is a string. The AST check
  over the template source is a second lock on a door that is already shut, and it is worth having
  because the first lock is a convention about what goes in a dataclass.
- A template **cannot** reformat a figure, so the appendix and the axis label cannot disagree.
- Everything printable is decided in Python, where it is unit-testable without rendering anything.

`model.py` is also where the three derived series live — free cash flow, the margin set, and
year-over-year growth — each computed over `Decimal` and each carrying a `Derivation`:

| Derived | Rule | `Derivation.rule` |
|---|---|---|
| Free cash flow | `OPERATING_CASH_FLOW − CAPEX`, capex positive-as-filed | `free_cash_flow` |
| Gross / operating / net margin | `metric ÷ REVENUE`, per period, both facts required | `margin` |
| Revenue YoY | `(rₜ − rₜ₋₁) ÷ rₜ₋₁`, consecutive spine years only | `yoy_growth` |

**Both facts required, per period, with no interpolation.** A margin for a year where revenue is
present and gross profit is not is not computed and not plotted — the point is dropped and the
drop is counted. The tempting alternative, carrying the previous year's numerator forward, produces
a flat segment on the margin chart that looks like stability and means "missing".

`build_model(history, run, *, brief) -> ReportModel` is the one entry point, and it takes the same
`FinancialHistory` and `RunInfo` that `serialize` takes. `brief` is passed because the brief model
carries fewer sections, not different values — [§ 5](#5---brief).

---

## 3. The five sections

§9.1 numbers ten. ROADMAP M3 builds 1, 3, 4, 9 and 10. Sections 2, 5, 6, 7 and 8 are **not
stubbed** — an empty "Verdict" page is worse than a report that has no verdict page — with one
exception, the cover's badge, which is a slot in a section that does exist.

### 1 — Cover

Ticker, name, as-of date, verdict badge, confidence rating, disclaimer.

The badge reads **`NOT ASSESSED`** and carries the milestone: *"Score and confidence rating arrive
with the forecast engine (ROADMAP M5)."* Three of the confidence rating's five inputs already
exist, which is exactly why no partial number is printed —
[`README.md` § 7 question 2](README.md#7-spec-questions). What the cover prints instead is
measurement rather than rating: tier-1 annual coverage with its spine origin, and quarters
available. Both are numbers whose denominator is stated on the same line.

The disclaimer is the full paragraph from README § Disclaimer, on the cover, in the same type size
as the body. §10 requires it prominent; it is not a footer.

### 3 — Company snapshot

Name, CIK, SIC and description, fiscal year end, market cap with its `Derivation` note (which
names the share classes summed, per §5.4), latest annual revenue / net income / FCF, and quarters
of history.

*"Current multiples vs. peer percentiles"* is §9.1's and needs M4's cohort. It renders as a stated
omission naming the milestone, in the same treatment as an absent metric. Not silently dropped:
someone comparing the report against §9.1 should be able to see that the gap is known.

**Business description.** §9.1 asks for one. `sic_description` is what M1 has; 10-K Item 1 is
available through `documents.py` but is several thousand words and summarizing it is §7.2's job in
M6. The snapshot prints the SIC description and says where a real one comes from. Recorded rather
than quietly satisfied with a sector name.

### 4 — Historical performance

The five charts, each with a caption naming the metrics and the number of periods drawn, and each
followed by nothing — the numbers behind them are in the appendix, and repeating them under the
chart is where a formatting divergence would first appear.

### 9 — Caveats

The section §9.1 says *"is not boilerplate. It's where the report earns trust, and it should be
written to be read."* Five blocks:

1. **Data coverage** — the per-metric table, both buckets, with `filled/expected`, the tags that
   won, and the spine origin **printed in the header**, with the `OBSERVED` case carrying its
   warning inline rather than in a footnote.
2. **What this report does not contain, and when it will** — forecast (M5), quality scores and
   peer context (M4), 8-K events and filing diffs (M4.5), narrative risk (M6). A list of named
   absences, because a reader who does not know what is missing cannot calibrate what is present.
3. **Measured accuracy: none.** ROADMAP open question 1 says that if M7 is not in v1, every
   confidence figure *"must be labeled that way in report section 9."* At M3 there is nothing to
   label yet and the honest sentence is that no forecast has been made and no accuracy has been
   measured. This block is where M7's numbers land.
4. **The two recorded gates** — §6.10's bank/REIT and §5.1's 12-quarter threshold, rendered as
   notices when they apply. They suppress a valuation that does not exist yet, so at M3 they are
   information rather than consequence, and the wording says so.
5. **Findings** — every `Finding` M2 emitted, code and detail, unsorted by severity because M2
   assigns none and M4's registry owns it.

### 10 — Appendix

Full annual and quarterly tables (`exact` values), per-metric tag provenance, the config used, the
cache manifest hash, and the interned `sources` array.

The `sources` array is rendered **directly from the same interning function `serialize` uses**, not
re-derived. `docs/m2/04-serialize.md` already observed that the array *"is also §9.1's appendix,
already deduplicated, so M3 renders it directly instead of walking every fact to collect distinct
refs."* Two walks would be two chances to disagree about what "distinct" means, in the one table
whose job is to be checkable.

---

## 4. Template layout

```
report/templates/
├── report.html.j2      # full: sections 1, 3, 4, 9, 10
├── brief.html.j2       # 2 pages: section 1 + a condensed 3/4
├── _macros.html.j2     # figure(), omission(), table(), chart()
└── report.css          # print CSS, @page rules, inlined into a <style> element
```

`report.css` is handed to WeasyPrint as a **separate `CSS` object** rather than linked or written
into a `<style>` element, which is what lets the `url_fetcher` deny every URL
([`02-render.md` § 3](02-render.md#3-the-url_fetcher-denies-everything)) — and, more usefully, means
**the stylesheet never passes through the template engine at all.** No template references it, so no
model value can reach a CSS context, which is the one injection surface autoescape does not cover.

Four macros, and `omission()` is the one that matters: every "not available" in the document goes
through it, so the treatment is identical whether the cause is an absent metric, an unbuilt
milestone or a chart below the point threshold, and a reader learns to recognize one shape rather
than four.

`@page` gives A4 with a running footer carrying ticker, as-of date and page number, and
`@page :first` drops the footer for the cover. Page breaks are `break-before: page` on each
section, not manual spacing — spacing-based pagination is what breaks the moment a company has one
more finding than the fixture did.

---

## 5. `--brief`

Two pages: the cover, and one page carrying the snapshot, the revenue chart, and the coverage
summary line.

**It selects a template, not a data path.** `build_model(..., brief=True)` returns a model with the
same values and fewer sections — it does not compute anything differently, and it does not skip the
work of building the sections it omits. That costs a few milliseconds of formatting nobody reads,
and it buys the property that a figure cannot appear in the full report and disagree with the brief.

**`report.json` is byte-identical between the two.** `--brief` is a presentation flag; the run
record is a record of the run. A test asserts it, because the alternative — a brief run producing a
smaller document — would make `investo diff` results depend on which flag the two runs used.

The 2-page count is asserted as an equality, unlike the full report's band. README promises "a
2-page summary" and that is a promise about the artifact, not about its content.

---

## 6. What the templates do not do

- **No arithmetic.** Enforced by parsing each template with `jinja2.Environment.parse` and failing
  on an arithmetic node. See [`05-testing.md` § 4](05-testing.md#4-new-layering-rules).
- **No filters that format numbers.** No `|round`, no `|format`, no custom `|money` filter. A
  filter is arithmetic-and-formatting wearing a different syntax, and it would move the decision
  about what a million looks like back out of `format.py`.
- **No conditionals that hide a figure.** `{% if value %}` is false for a legitimately zero figure,
  so an absent value and a zero value render the same. Absence is a *field* on the model
  (`Figure.omitted`) and the template branches on that.
- **No `|safe`.** Autoescape is unconditional and there is nothing in an M3 report that needs to
  carry markup. A `|safe` in a template is where M6's filing text would go wrong, and the rule is
  cheaper to establish now than to retrofit against a template that already has one. The one thing
  that *would* have needed it — the stylesheet — is not templated at all, which is why the rule can
  be absolute rather than "except for the CSS".
- **No reference to the stylesheet.** Asserted, because "except for the CSS" is exactly the
  exception that would be added back the first time someone wants a themed colour.

# M3 — Charts

`src/investo/report/charts.py`. DESIGN §9.0 and §9.1 section 4 are normative; ROADMAP M3 names the
five charts. This document is subordinate to both.

The module's job is narrower than "draw charts". It is the **only** place in the package where a
`Decimal` becomes a `float`, and most of what follows is about keeping that conversion from
spreading.

---

## 1. The five charts

ROADMAP M3 lists five; DESIGN §9.1 section 4 lists six. The sixth is ROIC vs. WACC, and WACC is
§5.4's CAPM build in M5, so ROADMAP's list is the correct one for this milestone and the omission
is deliberate rather than an oversight ([`README.md` § 7 question 12](README.md#7-spec-questions)).

| Chart | Metrics | Shape | Bucket |
|---|---|---|---|
| `revenue` | `REVENUE` | bars, plus a YoY-growth line on a secondary axis | annual |
| `margins` | `REVENUE`, `GROSS_PROFIT`, `OPERATING_INCOME`, `NET_INCOME` | three margin lines | annual |
| `fcf_vs_net_income` | `OPERATING_CASH_FLOW`, `CAPEX`, `NET_INCOME` | two lines; FCF is a `Derivation` | annual |
| `balance_sheet` | `ASSETS`, `LIABILITIES`, `EQUITY`, `CASH`, `LONG_TERM_DEBT` | stacked bars | annual |
| `share_count` | `SHARES_DILUTED_WEIGHTED` | bars | annual |

Annual for all five, at M3. The quarterly series exists and is rendered as a table in the
appendix; charting it as well doubles the chart count for a milestone whose exit criterion is
"a PDF you'd actually read", and a 20-quarter bar chart of five balance-sheet lines is not that.
Quarterly charts arrive when there is a reason for one — the accruals story in section 4 is
annual, and §6.4's staleness flag is a sentence rather than a picture.

**`margins` is named `margins`, not `margin_stack`.** ROADMAP says "margin stack" and a stacked
area of gross/operating/net margin is what that names, but the three are **nested, not additive** —
net margin is inside operating is inside gross — so stacking them plots a total of 1.8× gross
margin, which is a number that does not exist. Three lines on one axis says the same thing and is
arithmetically true. Recorded here rather than silently renamed.

**FCF is derived, and the derivation is the chart's own.** `OPERATING_CASH_FLOW − CAPEX`, with
`CAPEX` in the sign convention `normalize/tags.py` declares (positive as filed, per
`docs/m2/01-tags.md` § 8). The subtraction happens in `report/model.py` over `Decimal` and produces
a `Derivation(rule="free_cash_flow", inputs=(ocf.source, capex.source))`, so the number on the
chart traces exactly like any other. **It is not computed in `charts.py`** — a chart module that
does arithmetic is a chart module that can print a number nothing else knows about.

---

## 2. A chart takes `Fact`s, never numbers

```python
def build(spec: ChartSpec, series: Sequence[PlotSeries]) -> ChartImage: ...
```

`PlotSeries` carries `tuple[Fact, ...]` and a label. The builder derives the x positions from
`fact.period.end` and the y values from `fact.value`, and there is no overload taking bare numbers.

This is the whole provenance mechanism at chart scope, and it works because of what it makes
impossible rather than what it checks: a caller with a number and no `Fact` cannot draw it. The
alternative signature — `build(xs, ys, label)` — is more flexible, is what every charting example
in the world looks like, and would let M4 plot a peer median with no source attached. There is no
test that catches that; there is only a signature that does not accept it.

`ChartImage` carries the bytes, the media type, the `ChartSpec`, and **the `Provenance` of every
fact it drew**:

```python
@dataclass(frozen=True, slots=True)
class ChartImage:
    spec: ChartSpec
    media_type: str            # "image/png" | "image/svg+xml"
    payload: bytes
    sources: tuple[Provenance, ...]
    omitted: str | None = None # a stated reason, when nothing was drawn
```

`sources` is what lets `report/model.py` assert that the appendix's interned array covers
everything on the page, and what makes §9.1's *"every number traceable"* a test rather than a
promise ([`05-testing.md` § 5](05-testing.md#5-the-guaranteeviolation-test-table)).

---

## 3. The one float in the package

CLAUDE.md convention 8 bans `float` under `normalize/` and `report/`, and
`test_layering::test_no_float_in_normalize_or_report` enforces it structurally on both `float()`
calls and float literals. matplotlib requires floats for plotted coordinates and for every
geometry parameter it takes. Both cannot hold.

The resolution keeps the ban and makes `charts.py` its single allowlisted module, with two
sub-rules that do the work the blanket ban was doing:

**One conversion site.**

```python
def coord(value: Decimal) -> float:
    """The only Decimal → float conversion in the package.

    A plotted coordinate is a *position*, and a position is allowed to be approximate: the
    difference between 391035000000.01 and 391035000000.010009765625 is 0.0000000000026 of a
    pixel. What is not allowed is that number coming back out as text, which is why nothing in
    this module formats one.
    """
    return float(value)
```

Enforced by a new AST rule: every `float()` call site under `report/` must be lexically inside a
`FunctionDef` named `coord`, in `charts.py`. A conversion in a comprehension three functions away
fails the build. This is stricter than "the module is allowlisted", and it is the difference
between an exemption and a boundary.

**Named geometry constants.** Every float literal in `charts.py` is a module-level assignment with
an uppercase name:

```python
FIGSIZE_WIDE: Final = (6.4, 2.8)
FIGSIZE_SQUARE: Final = (3.2, 2.8)
BAR_WIDTH: Final = 0.62
GRID_ALPHA: Final = 0.25
LINE_WIDTH: Final = 1.4
```

So the file's entire float surface is a reviewable list at the top, and `alpha=0.35` cannot appear
inline in a plotting call where nobody will look at it again.

**And the guarantee neither rule states directly: the float positions a mark; it never produces a
printed figure.** Axis tick labels, data labels, the legend's value column — every string a reader
can read comes from `report/format.py`, which takes a `Decimal` and has no float anywhere. So a
tick label is built by calling `format.money(fact.value)` and handing matplotlib the resulting
*string*, not by letting matplotlib's default formatter render the float it was given.

The violation test is behavioural, because a structural one would pass on a correct-looking
implementation that formats the float. The AAPL fixture carries `391035000000.01` for exactly this
purpose, and the assertion is that the exact decimal string appears in the appendix and on the
chart's own axis label.

---

## 4. Determinism, per chart

Three sources of nondeterminism, and the first two are the ones §9.0 does not cover because §9.0
was written about SVG.

**PNG carries a `Software` chunk.** matplotlib writes `Software: Matplotlib version X.Y.Z,
https://matplotlib.org/` into every PNG unless told not to. Two matplotlib patch releases therefore
produce different bytes for an identical chart, and §11's gate reports it as a regression in our
code. `savefig(..., metadata={"Software": None})`.

**SVG carries `<dc:date>` and random glyph ids.** §9.0's mitigations: `metadata={"Date": None}`
and a pinned `svg.hashsalt`. Inert while every chart is PNG, and written now anyway, because the
spike promoting one chart to SVG should not also have to discover this.

**The hashsalt is namespaced per chart, through `rc_context`.** §9.0 warns that *"a fixed hashsalt
can collide across multiple charts composed into one document."* The obvious implementation —
setting `rcParams["svg.hashsalt"]` once at import — is exactly the colliding one. Instead each
build runs inside `matplotlib.rc_context({"svg.hashsalt": spec.chart_id, ...})`, so the salt is a
function of which chart is being drawn and the scoping is structural rather than a discipline.

**`charts.py` never imports `pyplot`.** The `Figure` / `FigureCanvasAgg` object API needs no global
figure registry, no `matplotlib.use("Agg")` before the first import, and no `plt.close()` after
each chart to avoid leaking five figures per run. `pyplot`'s global state is also a second place
`rcParams` could be mutated from, which would undo the `rc_context` scoping above without any
diff in this file.

---

## 5. Absence, and what goes in the slot

A chart needs **two points in the bucket** to be drawn. One point is not a trend and a single
floating bar reads as one; zero points is an empty axes, which reads as a rendering failure.

Below the threshold, `build` returns a `ChartImage` with `payload=b""`, `omitted` set to a stated
reason, and `sources=()`. The template renders the reason in the slot, in the same visual treatment
as an absent metric in the appendix table, and the reason **names the metric and points at the
coverage table** — because "no chart" is a fact about our data, and the coverage table is where the
reader finds out whether it is a fact about the company.

```
Operating margin not charted — operating_income has 0 of 5 annual periods.
See § Caveats, data coverage.
```

Three cases this covers, all of which occur in the fixture set:

| Fixture | Chart | Reason |
|---|---|---|
| `BANK` | `margins` | no `OPERATING_INCOME`; §6.10 says a bank's income statement does not have one in the tagged sense |
| `IPO` | every annual chart | fewer than two annual periods in the window |
| `NOPERIODIC` | every chart | an `OBSERVED` spine and no filings — the placeholder must say *that*, not "no data" |

The distinction in the third row is the one worth a test. A filer with no periodic filings and a
filer that filed and tagged nothing produce the same empty chart and mean completely different
things, and the coverage report already knows which is which
(`SpineOrigin.OBSERVED` vs. a `FILINGS` spine with zero fills).

---

## 6. What `charts.py` does not do

- **No arithmetic over values.** FCF, margins and YoY growth are all computed in `report/model.py`
  over `Decimal`, with a `Derivation`. A ratio computed here would be a number on the page with no
  entry in `report.json` — which is the failure `README.md` § 2 rejects the JSON-as-input
  architecture to avoid, arriving from the other side.
- **No layout.** Figure size is a constant here; where the figure sits on the page is CSS.
  A chart that knows about page position cannot be reused in `--brief`, and §9.0's per-chart
  PNG/SVG decision is already one cross-cutting concern too many.
- **No formatting decisions.** `format.py` decides that money is millions and a share count is
  suffixed; `charts.py` calls it. Two modules deciding what a million looks like is how the axis
  and the table come to disagree, in a document whose entire claim is that they do not.
- **No file writing.** `build` returns bytes. The renderer decides whether they become a `data:`
  URI or a file on disk, and that decision is coupled to the `url_fetcher`'s strictness
  ([`02-render.md` § 3](02-render.md#3-the-url_fetcher-denies-everything)) rather than to anything
  a chart knows.
- **No colour policy beyond a declared palette.** Colours are module constants named for their
  role (`SERIES_PRIMARY`, `SERIES_MUTED`, `RULE_GREY`), because the same palette has to appear in
  the CSS and a hex code written twice is a hex code that drifts.

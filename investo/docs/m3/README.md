# M3 — Report shell

Status: **designed and built, one exit criterion unassessable.** The fifteen spec questions in
[§ 7](#7-spec-questions) were folded into DESIGN.md and ROADMAP.md and the code landed 2026-08-01:
`report/format.py`, `report/model.py`, `report/charts.py`, `report/render.py`, the three templates
and the stylesheet, `analyze.py`, four new layering rules and five new test modules.

**The renderer spike has not run**, and it is this milestone's equivalent of M2's two open research
workstreams — no green test declares it finished, and until it does *"every chart is PNG"* is an
interim decision rather than a measurement. [`SPIKE.md`](SPIKE.md) is the record and is deliberately
empty; `tests/spike_renderer.py` is the probe.

Last updated: 2026-08-01

Design for ROADMAP M3, the milestone that turns `report.json` into a PDF. `DESIGN.md` and
`ROADMAP.md` remain normative; this document is subordinate to both, and to the M1 and M2 designs
in [`../m1/`](../m1/README.md) and [`../m2/`](../m2/README.md) where those have been folded into
them. Where it proposes something they do not say, it says so and asks — it does not decide.

Read in order:

| File | Covers |
|---|---|
| [`01-charts.md`](01-charts.md) | `report/charts.py` — the five charts, the one float in the package, absence placeholders, per-chart determinism |
| [`02-render.md`](02-render.md) | `report/render.py` — Jinja2, WeasyPrint, the `url_fetcher`, `SOURCE_DATE_EPOCH`, page geometry |
| [`03-templates.md`](03-templates.md) | `report/model.py` and `report/templates/` — the view model, why the template sees strings, sections 1/3/4/9/10, `--brief` |
| [`04-analyze-command.md`](04-analyze-command.md) | `analyze.py` — the command body, the output contract, exit 3, `--explain` |
| [`05-testing.md`](05-testing.md) | the spike, hostile fixtures, the determinism gate, four new layering rules, the guarantee→violation-test table |

---

## 1. What M3 delivers

ROADMAP M3's goal: `investo analyze AAPL` emits a real PDF with real historical charts and no
forecast.

M2 ends with a `FinancialHistory` in which every figure is keyed by `Metric` and traces to a
`SourceRef` or a `Derivation`, and a `report.json` that says so. M3 is the first milestone whose
output a person looks at rather than parses. That changes what "wrong" means: after M2 a bad
number is a bad row in a document nobody reads by hand; after M3 it is a bar on a chart, and a
chart is *persuasive* in a way a JSON document is not. The whole of this design is organized
around one consequence of that — **a number that reaches the page must have arrived there as a
`Fact`**, and the layering rules below exist to make the alternative physically difficult rather
than merely discouraged.

### The three exit criteria, and where each is tested

| Exit criterion | Test | Status |
|---|---|---|
| "A PDF you'd actually read" | not assertable; the [renderer spike](05-testing.md#1-the-spike-runs-first) and a human read | **judgement** — sized as a workstream, not as a test |
| Every number traceable to a `SourceRef` in the appendix | `test_report_model::test_every_rendered_figure_is_interned`, plus the no-arithmetic-in-template rule | ready by construction — see [spec question 6](#7-spec-questions) |
| Two runs produce a byte-identical file | `test_render_determinism::test_two_runs_are_byte_identical` | ready, **with a scope caveat that must not be discovered at review** — see [spec question 8](#7-spec-questions) |

The third needs its caveat stated here rather than in a test docstring, because the phrase "two
runs" is doing more work than it looks like. Two runs *of the same inputs on one machine* are
byte-identical and that is a CI gate. Two runs on **different machines** are not, and cannot be
made so without pinning a font file and a FreeType version into the repository. matplotlib
rasterizes glyphs through whatever FreeType it was built against, and WeasyPrint resolves
`font-family` through the host's fontconfig. §11's gate is a regression detector — it catches "the
renderer changed" — and it is not a reproducible-build claim. Recorded as
[spec question 8](#7-spec-questions) and proposed as a DESIGN §12 entry, because a reader who
assumes the stronger property will eventually publish a hash.

---

## 2. Considered and rejected: rendering from `report.json` instead of from the objects

`report.json` exists, it holds everything the report needs, and a renderer that consumed it would
get two properties for free: `investo diff` and the PDF would provably agree, and the render step
would be independently runnable against a checked-in document. It is the obvious architecture and
it is wrong for this milestone.

**It inverts the provenance guarantee.** In `report.json` a value is a *string* and its source is
an *integer index into an array*. A renderer over that shape can print `"391035000000.01"` next to
index `7` without anything in the type system objecting if the index is wrong — the association is
by convention, re-established at parse time, and a renderer that got it wrong would produce a
report whose numbers are all correct and whose provenance is all shifted by one. Rendering from
`FinancialHistory` keeps the value and its `Provenance` welded together in one frozen object all
the way to the page, which is the property M2 spent a milestone establishing and which §3.2 states
as the mechanism that makes the report auditable.

**And there is no reader.** `docs/m2/04-serialize.md` §7 declined to write one on the grounds that
`investo diff` is out of v1 scope and a deserialization contract fixed before it has a consumer is
a contract fixed wrong. M3 needing one would be a reason to revisit that; M3 not needing one is
not.

So `serialize` and `render` are **two consumers of one `FinancialHistory`, not a pipeline**, and
the test that keeps them honest compares them against each other rather than trusting either
([`05-testing.md` § 5](05-testing.md#5-the-guaranteeviolation-test-table)).

### Also considered and rejected: building the charts from `Decimal`

matplotlib cannot plot a `Decimal` without a units registry nobody wants to maintain, and the
alternatives to converting — a fixed-point integer axis, a custom `Formatter` over `object` dtype —
each buy a different bug in exchange. The float conversion is real and it is designed rather than
suppressed; see [spec question 3](#7-spec-questions) and
[`01-charts.md` § 3](01-charts.md#3-the-one-float-in-the-package).

---

## 3. `investo analyze` — the command surface

`analyze` already declares every flag it needs (M0): `--lookback`, `--out`, `--cache-dir`,
`--llm`, `--peers`, `--assumptions`, `--as-of`, `--refresh`, `--explain`, `--brief` and
`--config`, with a body that raises `NotImplementedYetError.at("M3", …)`. **M3 adds no flag**, which
is worth stating because it means `tests/test_cli_surface.py` needs no README change and cannot
catch a mistake here — the checks that matter for this milestone are elsewhere.

Four of those flags belong to milestones that have not landed. They are accepted, validated, and
then either recorded or refused, never silently ignored:

| Flag | M3 behaviour | Why not "ignore it" |
|---|---|---|
| `--llm` | accepted; anything but `none` is **exit 5** naming M6 | Accepting `--llm anthropic` and producing a report with no narrative section is a report that silently cost nothing and did nothing. §14's exit 5 is "config error", and asking for a provider that does not exist is one. |
| `--peers` | accepted, validated, recorded in `run`; section 3 states the omission | M4 owns the cohort. The list is a real input to a later run and belongs in the run record. |
| `--assumptions` | accepted, existence-checked, recorded in `run`; **exit 5 if it is not readable TOML** | M5 owns the contents. Checking it parses now costs nothing and moves the failure from "after a 40-second fetch" to "before it". |
| `--explain` | accepted, recorded in `run.explain`; **no other effect at M3** | There are no intermediate calculations yet. [Spec question 9](#7-spec-questions) and a test asserting the PDF is unchanged. |

The body becomes the same shape `facts` already has, which is deliberate — one fetch path, one
normalization path, one clock read:

```python
settings = _settings(config_file=config_file, out=out, cache_dir=cache_dir,
                     llm_provider=llm, lookback=lookback)
parse_lookback(settings.lookback)
_require_no_llm(settings.llm_provider)          # exit 5 until M6
resolved = _resolve_as_of(as_of)                # None here; run_analyze reads the clock once
outcome  = run_analyze(
    ticker, settings=settings, refresh=refresh, as_of=resolved,
    brief=brief, explain=explain, peers=_split_list(peers, flag="--peers"),
    assumptions=assumptions, version=_version(),
)
print(render_analyze_summary(outcome))
raise typer.Exit(int(outcome.exit_code))
```

`run_analyze` lives in **`src/investo/analyze.py`**, next to `fetch.py` and `facts.py`. That name
collides with DESIGN §3.1's `analyze/` *package* for M4, and the collision is real rather than
cosmetic — a module and a package of the same name cannot coexist in one package directory.
[Spec question 1](#7-spec-questions).

### What it writes

Two files into `settings.out_dir`, and the directory layout is a decision rather than an
accident:

```
reports/AAPL/2026-08-01/report.pdf
reports/AAPL/2026-08-01/report.json
```

README says *"Every run also writes `report.json` next to the PDF"*, which fixes the two names and
their adjacency and says nothing about the path above them. Flat `reports/report.pdf` makes the
second ticker overwrite the first; `reports/AAPL.pdf` makes tomorrow's run overwrite today's,
which destroys the input to the `investo diff` §4.5 exists for. Keyed by ticker **and `as_of`**,
so a re-run of the same point-in-time reconstruction overwrites itself — which is what makes
[§ 11's determinism gate](05-testing.md#3-determinism) runnable as *"run it twice and compare"*
rather than needing a temp directory. Recorded as a ROADMAP addition rather than left to the
implementation.

---

## 4. Exit codes

**M3 is the first milestone that can exit 3**, and that makes §14's third code the one to read
carefully. It promises "insufficient data, **report still written**, valuation omitted."

| Condition | Code | Class |
|---|---|---|
| Report written, everything present | 0 | — |
| Ticker absent, or not NASDAQ | 2 | `TickerNotFoundError` (M1) |
| **No `companyfacts` published for the CIK — report written, and it says so** | **3** | `InsufficientDataError` (M0), raised *after* the write |
| Tier-1 annual coverage below `coverage_floor`, where one is configured | 3 | as above |
| `--llm` anything but `none` | 5 | `ConfigError` — M6 |
| Bad `--lookback`, future `--as-of`, unreadable `--assumptions` | 5 | `ConfigError` (M0) |
| Retries exhausted, transport error | 4 | `UpstreamFetchError` (M1) |
| A metric resolves to nothing; fewer than 12 quarters; a bank's absent operating income | — | rendered as a stated omission; exits 0 |
| **The forecast, peer and narrative sections are absent** | — | **not exit 3** |

The last row is the one that needed deciding. Every M3 report omits the valuation, so reading
§14's "valuation omitted" as the trigger would make *every* run exit 3 — a code that fires on
every invocation carries no information, and the first thing anyone would do is stop checking it.
**An unbuilt milestone is not insufficient data**; the distinction is between "we could not learn
this about this company" and "this program does not do that yet", and only the first is a property
of the run. [Spec question 5](#7-spec-questions).

The second row is the one that surprises. `InsufficientDataError` is raised **after** the PDF and
`report.json` are on disk, because exit 3's own wording promises the artifact. A command that
raises before writing turns §14's most carefully worded code into a lie. Implemented as an
`exit_code` on the returned outcome rather than as a raise through the write path, so the
"written, then reported" ordering is structural.

---

## 5. The seams

### M2/M3, inherited

Six rules from M2 apply unchanged to everything M3 adds under `report/`: no `us-gaap` literal, no
clock read, no keyless sort, no `float`, no I/O imports, no upward imports. Three of the six are
about to be tested for the first time by code that actually wants to break them — `report/` at M2
was one pure function returning a string.

- The **`us-gaap` ban** now faces a chart that wants an axis label. `docs/m2/README.md` § 5 named
  this exact case: *"a chart label, a hardcoded fallback for a metric that came back empty."* The
  label comes from `Metric`, and the tag it resolved to comes from `MetricCoverage.tags_used`.
- The **clock ban** now faces a PDF that wants a creation date. It gets one, derived from `as_of`
  at the command boundary — [`02-render.md` § 4](02-render.md#4-source_date_epoch).
- The **`float` ban** now faces matplotlib, and this is the one that cannot be satisfied as
  written. [§ 7 question 3](#7-spec-questions).

### M3/M4, new

Four rules, each of which is cheap now and expensive to retrofit:

- **`report/charts.py` is the only module in the package that may construct a `float`**, and every
  conversion in it goes through one named function. Same shape as the `us-gaap` allowlist: one key,
  asserted, so a second is a visible edit.
- **No template may perform arithmetic.** A number computed in a template has no `Fact` behind it
  and therefore no provenance, and it is invisible to every test that walks the model. Checked by
  parsing each template with Jinja's own parser and failing on an arithmetic node — the template
  language's AST, for the same reason `tests/test_layering.py` walks Python's.
- **The template receives strings, not numbers.** `report/model.py` formats every figure through
  `report/format.py`, which takes a `Decimal`. This is what makes the rule above nearly free, and
  it is what guarantees that no number a reader sees has been through IEEE-754 — the float decides
  where a bar ends, never what is printed next to it.
- **Nothing under `normalize/` or `report/` may import `numpy`.** It arrives at M3 as a matplotlib
  dependency, two milestones before ROADMAP declares it, and a `numpy.float64` array is the `float`
  ban's back door — it is constructed by no `float()` call and holds no float literal.

### What M3 still does not do

- **No severity, no verdict, no score.** §6.2's registry is M4's and §9.2's rubric is M5's. The
  cover's verdict slot renders `NOT ASSESSED` with the milestone named ([spec question 2](#7-spec-questions)).
- **No refusal.** §6.10's bank/REIT gate and §5.1's 12-quarter gate stay recorded-and-not-enforced,
  exactly as M2 left them, because what they suppress is a valuation that does not exist yet.
- **No `report.json` reader**, for the reason [§ 2](#2-considered-and-rejected-rendering-from-reportjson-instead-of-from-the-objects) gives.

---

## 6. Dependencies M3 adds

Three, and each is named in DESIGN §9 or §9.0 rather than chosen here.

| Package | Pin | Why the ceiling is where it is |
|---|---|---|
| `matplotlib` | `>=3.10,<4` | The chart API is stable across minors; a major is where the default style changes, and a default-style change is a byte-identical-output failure with no commit of ours to blame. |
| `jinja2` | `>=3.1.6,<4` | 3.1.6 is the floor because the sandbox-escape fixes through the 3.1.x line matter for a template engine that will render filing text at M6. |
| `weasyprint` | `>=69,<70` | §9.0 requires ≥69.0 for CVE-2025-68616 (SSRF via redirect, which defeats a custom `url_fetcher`) and CVE-2026-49452 (CSS injection via presentational hints), and because `use`-tag inheritance was only fixed in 69.0. The `<70` ceiling is not caution about bugs — it is that a renderer change is *exactly* what §11's gate is built to detect, so an unpinned major would turn the gate into a source of false alarms. |

Two things arrive that are **not** declared, and both are recorded rather than left to be noticed:

- **`numpy` and `pillow`**, transitively via matplotlib. ROADMAP puts `numpy` in M5. It is not
  added to `[project.dependencies]` here — nothing of ours imports it — and it is added to the
  forbidden-import set for `normalize/` and `report/` in the same commit, which is the only way an
  undeclared transitive dependency stays undeclared. [Spec question 14](#7-spec-questions).
- **A system-library requirement.** WeasyPrint needs Pango, cairo and their GObject bindings from
  the platform, not from PyPI. This is the first dependency in the project that `uv sync` cannot
  satisfy alone, and README's Quickstart has to say so or the first `investo analyze` on a clean
  machine fails with an import error that reads like a bug in investo.

Deliberate non-additions: **no `pandas`** (M5, per M2's argument — a DataFrame column drops the
`SourceRef`), **no chart-templating library**, and **no PDF post-processor**. A `pikepdf` pass to
strip metadata would be the obvious way to force determinism, and it would hide the fact that the
renderer is not deterministic — which is the property the gate is supposed to measure.

---

## 7. Spec questions

Fifteen. Ordered by how much the answer changes M3. Each is a place the code will diverge from
DESIGN.md or ROADMAP.md, and the reason has to survive the person who finds the divergence in a
year. **None should be resolved in code.**

**Two change documented behaviour rather than internals**, and they are the two most likely to be
skimmed on the way to coding: **5** (`analyze` exits 3 on a condition §14 does not name, and *not*
on the one it does) and **9** (`--explain` is accepted and inert). Neither is caught by
`tests/test_cli_surface.py`, which checks flag *existence* in both directions and says nothing
about what a flag does.

**1. `analyze.py` and `analyze/` cannot both exist, and both are specified.**

M3 needs a command body, and the two that exist are `fetch.py` and `facts.py` at the package root
— a convention that is not merely stylistic: `test_layering::test_every_clock_reading_module_is_a_command_body`
asserts `"/" not in rel` for every module permitted a clock read, so a command body **must** sit at
the root. DESIGN §3.1 gives M4 an `analyze/` package holding `fundamentals.py`, `quality.py`,
`efficiency.py`, `flags.py`, `diffs.py`, `events.py`, `peers.py`, `forecast/` and `score.py`.
`src/investo/analyze.py` and `src/investo/analyze/` are the same import name.

Proposed: **the command body is `analyze.py`; DESIGN §3.1's `analyze/` package is renamed
`analysis/`.** The command-body convention is pinned by a test and has to be satisfied this
milestone; the package is a label in a tree diagram with no code behind it until M4, and §3.1 is
explicit that the tree *"is created per milestone, not up front."* Moving the one that does not yet
exist costs a documentation edit; moving the one that does costs a naming exception that every
future reader has to be told about. The alternative spellings — `analyze_cmd.py`, `report_cmd.py` —
break the symmetry with `fetch.py`/`facts.py` for the sake of a package that could just as well be
called something else.

**2. §9.1's cover carries a verdict badge and a confidence rating, and M3 can compute neither
honestly.**

The score is §9.2's weighted rubric over five components, four of which are M4's and M5's. The
confidence rating draws on five inputs, of which **three already exist at M2** — metric coverage,
quarters of history, data-integrity findings — and two do not: Monte Carlo rejection rate (M5) and
measured backtest calibration (M7). So a partial confidence number is *computable*, which is
precisely the trap: a 0–100 rating produced from three of its five inputs is printed on the same
scale as the real one, reads as the real one, and is wrong in a direction nobody can see.

Proposed: **no partial score and no partial rating.** The verdict slot renders `NOT ASSESSED` with
the milestone that fills it, and the cover prints the **measured** coverage figure and quarter
count directly instead — a measurement with a stated denominator, rather than a rating with an
unstated one. The slot is present rather than omitted, for the same reason §4.5's empty keys are
declared rather than absent: a cover with nothing where the badge goes is indistinguishable from a
rendering bug, and M5 filling a labelled slot is a one-line diff.

**3. `float` is banned under `report/`, and matplotlib cannot plot a `Decimal`.**

CLAUDE.md convention 8 and `test_layering::test_no_float_in_normalize_or_report` fail on any
`float()` call **or float literal** anywhere under `report/`. matplotlib needs floats for the
plotted coordinates, and also for `figsize`, `alpha`, `linewidth` and every other geometry
constant — `figsize=(6.5, 3.0)` is two violations before a single number has been plotted.

Proposed, in three parts, because the ban is protecting two different things and only one of them
survives contact with a chart:

1. **`report/charts.py` is the one allowlisted module**, keyed and asserted exactly like
   `normalize/tags.py` in the `us-gaap` allowlist.
2. **Every `Decimal → float` conversion in it happens inside one function**, `coord()`, asserted by
   an AST rule that finds every `float()` call site and checks its enclosing `FunctionDef`. A
   conversion anywhere else in the file fails the build.
3. **Every float literal in it is a named module-level constant**, so the file's float surface is a
   reviewable list at the top rather than a scatter of magic numbers inside plotting calls.

And the part that carries the actual guarantee: **the float decides where a bar ends; it never
decides what is printed.** Every visible number — axis tick labels included — is formatted from
the `Decimal` by `report/format.py`. The violation test is behavioural rather than structural:
the AAPL fixture carries `391035000000.01` specifically because it is unrepresentable in binary,
and the assertion is that it appears in the appendix and on the axis label exactly as filed.
[`01-charts.md` § 3](01-charts.md#3-the-one-float-in-the-package).

**4. Every chart is PNG at M3, and that buys something the per-chart split does not.**

§9.0 specifies a per-chart choice — SVG for simple bars and lines, PNG at 300 dpi for anything
using clipping or alpha — and ROADMAP says to settle it with a spike *before* committing, because
the matplotlib→WeasyPrint seam is *"the largest un-hedged implementation risk in the project."*
The spike has not run.

Proposed: **PNG for all five charts, with `format` as a per-chart field** so the spike promotes
one chart at a time, in a diff that names it. That much is just deferral. The part worth recording
is the coupling nobody would notice later: a PNG can be embedded as a `data:` URI, so the
`url_fetcher` can deny **every** URL unconditionally — no path allowlist, no `file://` handling, no
way for a relative URL in a template to resolve to anything at all. §9.0 requires SVG to be
referenced as a *file* (WeasyPrint issue #134), so promoting one chart to SVG converts the
unconditional deny into an allowlist of absolute paths, which is a materially weaker security
property. **That is a cost of the promotion and it belongs in the spike's decision**, not
discovered afterwards. [`02-render.md` § 3](02-render.md#3-the-url_fetcher-denies-everything).

**5. Exit 3's trigger at M3 is not the one §14 names.**

Covered in [§ 4](#4-exit-codes). Proposed: exit 3 when `companyfacts` was absent, or when tier-1
annual coverage falls below a configured floor — **and not** because the forecast section is
missing. Plus: `Settings` gains `coverage_floor: Decimal | None = None`, defaulting to **no floor**
until `docs/m2/COVERAGE.md` supplies a measured distribution to set it from. §4.2 sanctions a
*configurable* floor; it does not supply a number, and inventing one now means a gate that fires
arbitrarily. This is the same reasoning `pyproject.toml`'s unset `fail_under` already carries, and
it should be resolved the same way: measure, then set.

**6. A number computed in a template has no provenance, and nothing currently prevents one.**

`{{ revenue / shares }}` in a Jinja template produces a figure that is in the PDF, is not in
`report.json`, has no `Fact` behind it, and is invisible to every test that walks the model. It is
also the single most natural thing for someone to write when a template is two lines from the data
it needs.

Proposed: **the model hands the template pre-formatted strings** (`report/model.py` over
`report/format.py`), and a test parses every template with `jinja2.Environment.parse` and fails on
any `Add`/`Sub`/`Mul`/`Div`/`FloorDiv`/`Mod` node. Jinja exposes its own AST, so this is the same
technique `tests/test_layering.py` uses on Python and it costs about twenty lines. The rule is
worth stating in CLAUDE.md § Non-negotiable conventions, because its violation is silent and
looks like good template hygiene.

**7. PNG has its own nondeterminism, and it is a different key from the one §9.0 names.**

§9.0 addresses SVG: `<dc:date>` needs `metadata={"Date": None}` and random glyph `id`s need a
pinned `svg.hashsalt`. Neither applies to PNG, which instead carries a `Software` text chunk
naming the matplotlib version — so with the interim PNG-everywhere choice, §9.0's two mitigations
are inert and the actual nondeterminism is unmitigated.

Proposed: `metadata={"Software": None}` on every PNG save, `metadata={"Date": None}` on every SVG
save, and the hashsalt pinned **per chart** through `matplotlib.rc_context` rather than by mutating
global `rcParams` — §9.0 warns that one salt across several charts in one document can collide,
and `rc_context` makes the namespacing structural. Also: **`charts.py` never imports `pyplot`.** The
`Figure`/`FigureCanvasAgg` object API needs no global state, no backend selection at import time,
and no `close()` discipline to avoid a memory leak across five charts.

**8. "Two runs produce a byte-identical file" is a per-machine claim.**

Covered in [§ 1](#the-three-exit-criteria-and-where-each-is-tested). Proposed: state the scope in
DESIGN §11 next to the gate and add a DESIGN §12 entry, because §12 is exactly the list of
"deliberate omissions rather than oversights" and a reader who assumes cross-machine
reproducibility will eventually publish a hash and be wrong in public.

**9. `--explain` has nothing to dump at M3.**

README documents it as *"dump all intermediate calculations to report.json"*. There are no
intermediate calculations until M5's driver build. Accepting the flag and doing nothing is a
silent no-op; rejecting it contradicts README.

Proposed: **recorded in `report.json`'s `run.explain` and otherwise inert**, with a test asserting
the PDF bytes are unchanged by it. Adding a key to `run` is not a `schema_version` bump per §4.5's
own rule. The flag then already *does* something checkable — it marks the run — and M5 widens it
rather than implementing it from scratch.

**10. `--brief` selects a template, not a data path.**

Proposed as a rule rather than an implementation detail: the same `ReportModel` is built either
way, `--brief` chooses which template renders it, and **`report.json` is byte-identical between
the two**. A brief report that took a different data path could disagree with the full one about a
figure, and the disagreement would be visible only to someone who ran both.

**11. A chart with too few points, and what goes in its place.**

Proposed: a chart needs **≥2 points in the bucket** to be drawn. Below that the slot renders a
stated reason naming the metric and pointing at the coverage table — never an empty axes, and
never a single floating bar, which reads as a trend. §6.10's argument, applied to a picture: *"a
blank space with an explanation beats a confident wrong number."* The bank fixture, which has no
operating income, is the test case.

**12. Sections 3 and 4 each contain something whose milestone has not landed.**

§9.1 section 3 asks for *"current multiples vs. peer percentiles"* — the cohort is M4's. §9.1
section 4 lists six charts and ROADMAP M3 lists five; the sixth is **ROIC vs. WACC**, and WACC is
§5.4's CAPM build in M5. ROADMAP's shorter list is therefore correct and its omission is
deliberate, which is not currently written down anywhere.

Proposed: both render as **stated omissions naming the milestone**, in the same visual treatment
as an absent metric, and ROADMAP M3's five-chart list gains a sentence saying why it is five and
not §9.1's six.

**13. The template is read from the installed package, never from a path relative to the source.**

The `src/` layout exists specifically so that tests import the installed package rather than the
working tree (ROADMAP § Decided during design). A renderer that resolves its template with
`Path(__file__).parent / "templates"` re-introduces exactly the failure that layout prevents: CI
green against a template that was never packaged.

Proposed: `importlib.resources.files("investo.report") / "templates"`, plus a `hatch` build check
that the directory is in the wheel. `tests/test_layering.py`'s `_NORMALIZE_FORBIDDEN` already
anticipates this — `pathlib` was deliberately left out of the forbidden set with the comment *"M3's
renderer will legitimately need to read a template from disk."*

**14. `numpy` arrives at M3, two milestones before it is declared.**

Proposed: not added to `[project.dependencies]` — nothing of ours imports it — and **added to the
forbidden-import set for `normalize/` and `report/`** in the same commit. A `numpy.float64` is
constructed by no `float()` call and contains no float literal, so it passes both existing
detectors; an array of them is the float ban's back door, and it is the natural thing to reach for
when a chart needs a series.

**15. WeasyPrint gets a `<70` ceiling, which is tighter than "≥69.0 pinned."**

§9.0 says ≥69.0. A ceiling is not in the design. Proposed and argued in
[§ 6](#6-dependencies-m3-adds): the ceiling exists because a renderer change is what §11's gate
detects, so without one the gate reports a dependency bump as a regression in our code. Recorded
because "pinned" and "`>=69,<70`" are not the same claim and the difference will look like drift.

### One risk accepted, not resolved

**"A PDF you'd actually read" is not assertable, and this milestone's exit criterion is one third
judgement.** Page count, box geometry and byte-identity are all checkable and none of them
distinguishes a good report from a correctly-typeset bad one. The mitigation is ordering rather
than testing: the [spike](05-testing.md#1-the-spike-runs-first) produces a real PDF from the AAPL
fixture on day one, before any template exists, so the content and layout problems ROADMAP says
this milestone exists to surface early are surfaced while they are still cheap. The residual risk
is that the spike passes and the finished report is still not worth reading, and there is no test
for that.

---

## 8. Proposed additions to ROADMAP § Decided during design

If the above are accepted, these are the sentences to record:

- **The `analyze` command body is `analyze.py` at the package root; DESIGN §3.1's `analyze/`
  package is renamed `analysis/`.** A command body must sit at the root — a layering test asserts
  it — and the package that does not exist yet is the cheaper one to move.
- **M3 computes no verdict and no confidence rating, and prints `NOT ASSESSED` rather than a
  partial one.** Three of the confidence rating's five inputs exist, which is what makes the
  partial number dangerous rather than unavailable.
- **`report/charts.py` is the only module in the package that may construct a `float`**, every
  conversion goes through one function, and every float literal is a named module-level constant.
  The float positions a mark; it never produces a printed figure.
- **Every chart is PNG until the spike promotes one**, and PNG-as-`data:`-URI is what lets the
  `url_fetcher` deny every URL unconditionally. Promoting a chart to SVG costs that and gains a
  path allowlist.
- **The template receives pre-formatted strings and may not perform arithmetic**, checked by
  parsing each template with Jinja's own parser.
- **`analyze` exits 3 when `companyfacts` is absent or coverage is below a configured floor, and
  never because a later milestone's section is missing.** An unbuilt milestone is not insufficient
  data. Exit 3 is returned **after** the files are written, because §14's wording promises them.
- **`Settings` gains `coverage_floor: Decimal | None`, defaulting to no floor** until
  `docs/m2/COVERAGE.md` supplies a measured distribution — the same posture as `fail_under`.
- **`--explain` is recorded in `run.explain` and is otherwise inert at M3**, with a test that the
  PDF is unchanged by it.
- **`--brief` selects a template, not a data path**, and `report.json` is byte-identical between
  the two.
- **`--llm` anything but `none` is exit 5 until M6.** A report that silently drops the section the
  user paid for is worse than one that refuses.
- **Byte-identical output is a per-machine, same-inputs claim**, not a reproducible-build claim;
  fonts and FreeType make the cross-machine version false. Recorded in DESIGN §11 and §12.
- **Reports are written to `out_dir/TICKER/AS_OF/`.** Flat output makes the second ticker overwrite
  the first and destroys the input `investo diff` exists for.
- **Nothing under `normalize/` or `report/` may import `numpy`**, which arrives transitively with
  matplotlib at M3 and is the float ban's back door.
- **A chart needs ≥2 points; below that the slot states why**, naming the metric and pointing at
  the coverage table.
- **ROADMAP M3's chart list is five, not §9.1's six** — ROIC vs. WACC needs M5's WACC.
- **WeasyPrint is `>=69,<70`.** The ceiling keeps §11's gate meaningful rather than guarding
  against bugs.
- **M3 is re-estimated at ~2.5–3 weeks part-time**, against ROADMAP's ~1 week.

---

## 9. Sizing

ROADMAP budgets M3 at ~1 week. That estimate predates this design, and it predates the two
re-estimates that came before it: M1 went from ~1.5 weeks to ~2.5–3, M2 from ~1.5 to ~3.5, both
after detailed design and both at roughly 2.3×. ROADMAP § Rough total already says to *"assume the
same multiple applies to M3–M5 until a milestone lands that disproves it."* This estimate does not
disprove it.

| Workstream | Estimate | Notes |
|---|---|---|
| **The renderer spike** | **1 d** | **Runs first, before any template exists. Margin stack + a `fill_between` chart → PDF, per ROADMAP's instruction. No green test at the end — the output is a decision.** |
| `format.py` + `model.py` | 1.5 d | Small, but it is where the "template sees strings" guarantee is established, and the absence cases are most of the surface |
| `charts.py` — five charts, absence placeholders, determinism | 2.5 d | The five charts are a day; the `coord()` discipline, the per-chart `rc_context` and the PNG metadata are the rest |
| `render.py` — env, `url_fetcher`, determinism, page geometry | 2 d | The `url_fetcher` and the `SOURCE_DATE_EPOCH` context manager are small and each has a hostile test |
| Templates + CSS — sections 1, 3, 4, 9, 10, and `--brief` | 2.5 d | Print CSS is iteration, and the appendix table is the fiddly one |
| `analyze.py` — command body, output paths, exit 3 | 1 d | |
| Four new layering rules | 0.5 d | AST machinery exists; the Jinja-AST one is new but small |
| Tests — hostile fixtures, determinism, geometry, model↔serializer agreement | 2.5 d | |
| Documentation folds | 0.5 d | |
| **Total** | **~14 d** | ≈ **2.8 weeks part-time**; ~13 d of it coding, and the 1 d spike is the only research |

Two differences from M1's and M2's shape are worth noting, because they cut in opposite
directions. **M3 has almost no research workstream** — one day of spike, against M2's 4.5 days —
so the estimate is less likely to hide a silent week. And **M3 has an exit criterion that is a
judgement**, which no amount of estimating makes tractable: "a PDF you'd actually read" can absorb
an arbitrary number of days in CSS. The 2.5-day template line is a budget, not a measurement, and
it is the line most likely to be wrong.

---

## 10. Documentation changes M3 requires

To be applied **on acceptance of [§ 7](#7-spec-questions)**, in one pass, and recorded here the
way `docs/m2/README.md` § 10 records M2's — so a reader who finds a divergence knows which document
to trust.

**DESIGN.md**

- §3.1 — `analyze/` → `analysis/` (question 1); `report/` gains `format.py`, `model.py` and the
  `templates/` contents; `analyze.py` appears at the root next to `cli.py`.
- §9.0 — the PNG metadata key (question 7), `rc_context` per chart rather than global `rcParams`,
  the `data:`-URI/unconditional-deny coupling (question 4), and the note that `pyplot` is not used.
- §9.1 — section 1's verdict slot at M3 (question 2); sections 3 and 4's stated omissions and the
  five-vs-six chart count (question 12).
- §11 — the determinism row gains its scope: same inputs, same machine (question 8). A new
  **Report** row detail: page geometry rather than a page-count assertion alone.
- §12 — a new entry for cross-machine rendering variance (question 8).
- §14 — exit 3's M3 trigger, and the rule that it is returned after the write (question 5).
- §4.5 — `run` gains `explain`, `brief`, `peers` and `assumptions`; not a `schema_version` bump.

**ROADMAP.md**

- M3's entry — this document's link, the ~2.8 week re-estimate, the spike-first ordering, and the
  five-chart note.
- § Decided during design gains a "Decided while designing M3" block — [§ 8](#8-proposed-additions-to-roadmap--decided-during-design).
- Open question 1 (is backtesting in v1) — M3 is where "unvalidated must be labeled that way in
  report section 9" first becomes real code, so the question gains a pointer to the caveats
  section rather than an answer.

**README.md**

- Status banner — `analyze` works; the PDF is historical-only.
- § Quickstart — the WeasyPrint system-library note. This is the first dependency `uv sync` cannot
  satisfy on its own.
- § Usage — no flag changes. A sentence on the `out_dir/TICKER/AS_OF/` layout, and on `--llm` being
  exit 5 until M6.
- The `investo analyze NVDA --llm anthropic` line in Quickstart currently promises something that
  now exits 5. It changes.

**CLAUDE.md**

- Status line and § Current layout.
- § Non-negotiable conventions gains the four M3 rules: the one-module float allowlist and its
  `coord()` discipline, no arithmetic in a template, the template-sees-strings rule, and the
  `numpy` ban.
- § Review checklist gains them as one line.

**`pyproject.toml`** — three dependencies, and `fail_under` is *still* not the thing to set here;
it is set from a `make coverage` run, per its own comment.

**What M3 touched outside its own tree**, stated as a checkable claim the way `docs/m2/README.md`
§ 10 did — and it is a longer list than the design predicted, which is worth recording rather than
quietly correcting:

| File | Change | Why it was unavoidable |
|---|---|---|
| `src/investo/cli.py` | `analyze`'s body replaces its stub | the deletion this milestone exists to perform |
| `src/investo/config.py` | `coverage_floor: Decimal \| None` | exit 3's second trigger needs a setting to compare against ([question 5](#7-spec-questions)) |
| `src/investo/report/serialize.py` | `intern_sources` exported | so the appendix and `report.json` share one walk rather than two definitions of "distinct" |
| `tests/test_layering.py` | four rules, three set edits | every one is a set M3 grows; leaving them would make the rules pass vacuously |
| `tests/test_cli_surface.py` | six tests re-subjected | all six asserted `NOT_IMPLEMENTED` for `analyze` ([`04-analyze-command.md` § 6](04-analyze-command.md#6-clipys-change)) |
| `tests/test_typing.py` | one fixture added to the pinned set | convention 16's violation test |
| **eight files, docstrings only** | `analyze/` → `analysis/` | [question 1](#7-spec-questions). Four modules under `ingest/` and `normalize/` and three test files carry prose references to M4's package by name. |
| `.pre-commit-config.yaml`, `tests/test_documentation.py` | hooks now cd to the project directory | a latent M0 bug, found by running the hooks — see below |

That last row is the one the design did not predict — it said "no M1 or M2 code file is touched" and
that is now false, for prose rather than for behaviour. It is corrected here rather than left,
because a name written in eight places that is wrong in eight places is exactly what
`tests/test_documentation.py` exists to prevent, and the cheapest moment to fix it is the moment it
changes.

**And the prediction itself should stop being made.** M2 claimed exactly one M1 code file would be
touched and was right about the count but framed it as a near-miss; M3 claimed zero and touched
three, plus eight files' worth of prose. Two milestones running, "this milestone touches nothing
outside its own tree" has turned out false in some form — which is unsurprising in hindsight, since
a milestone that adds a layer necessarily edits the seam above it and the tests that pin the seam.

So the useful artifact is the **table**, not the promise. A design should say what it *expects* to
touch and the implementation should correct the list; a design that predicts zero is making a claim
it has no way to check and that its own author is the last person positioned to falsify. Proposed as
a ROADMAP § Decided line so M4 does not make it a third time.

### The pre-commit hooks had never run what they claimed to run

Found by running them, on the first commit of this milestone, and worth recording because it is
three milestones old and because of *how* it hid.

**The git root is one level above this project.** pre-commit runs hooks from the git root, so
`entry: uv run ruff check --fix src tests` was resolving `src` and `tests` against a directory that
has neither. Three symptoms, each misleading in a different direction:

| Hook | What happened | Why it did not read as a cwd problem |
|---|---|---|
| `lint` | `E902 No such file or directory` on `src` and `tests` | reads as a broken checkout |
| `ruff-format` | **passed** | its entry named no paths, so pre-commit handed it real git-root-relative filenames |
| `basedpyright` | ran, found no `pyproject.toml`, fell back to its default mode | **51 errors and 760 warnings from rules this project disables on purpose** |

The third is the one that matters. A linter given a bad path fails loudly; **a type checker given no
config does not.** It runs, and its output is indistinguishable from the code having regressed —
`reportAny`, `reportExplicitAny`, `reportUnusedCallResult` and `reportMissingTypeStubs` are all
switched off in `pyproject.toml` with reasons, and all four were firing. Nothing in the output says
"I did not read your configuration."

Fixed by giving each local hook a `test -f pyproject.toml || cd investo` guard and
`pass_filenames: false`, so the hooks run exactly what the Makefile runs — which is what the
config's header has asserted since M0. And pinned by two tests in `test_documentation.py`, which is
the module that exists for facts stated in more than one file: one asserts every hook that names a
relative path establishes its own working directory, the other asserts the hooks and `make check`
run the same set of tools. Comparing tools rather than flag strings, because `make check` runs
`ruff format --check` and the hook runs `ruff format` — CI verifies, a commit hook fixes, and that
difference is intended.

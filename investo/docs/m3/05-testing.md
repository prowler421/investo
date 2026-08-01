# M3 — Testing

DESIGN §11's **Report** row: *"Render golden fixtures to PDF, assert page count. Overflow detection
has no WeasyPrint API — use `Document.pages` box geometry against the page box, or golden-image
diffing. Autoescape and `url_fetcher` denial tested with hostile fixtures."* Plus the determinism
gate, which becomes real at this milestone: `report.json` has been under it since M2, the PDF has
not been under anything.

---

## 1. The spike runs first

ROADMAP M3: *"the matplotlib→WeasyPrint seam (DESIGN.md §9.0) is the largest un-hedged
implementation risk in the project… Build the margin stack and a `fill_between` chart **first**, as
a spike, before committing to SVG anywhere."*

`tests/spike_renderer.py`, marked `spike` and deselected by default like `network` is. It is not a
test — it has no assertion worth making — and it is in `tests/` because that is where the fixtures
are and because a throwaway script outside the repo cannot be re-run when someone doubts the
result.

What it produces, from the AAPL fixture and nothing else:

1. Both charts, **each saved twice — once SVG, once PNG** — and both embedded in one minimal HTML.
2. A PDF, written to a path it prints.
3. A second PDF from an identical second run, and a printed byte comparison.
4. The `Document.pages` geometry walk from [`02-render.md` § 6](02-render.md#6-page-geometry),
   printing every box that overflows.

What it decides, and each of these is a line in the commit message rather than a test outcome:

- whether SVG survives WeasyPrint at all for a `clipPath`-wrapped axes (§9.0's issues #1374, #1595,
  #526)
- whether `<use xlink:href>` glyph references render, or produce blank text (#2375)
- whether `fill_between` alpha clips the text near it (#2332) — the one that decides the fan chart
  at M5, so the answer is worth having two milestones early
- whether the PNG path is byte-identical run to run once `metadata={"Software": None}` is set
- whether the geometry walk works against the installed WeasyPrint's private `_page_box`

**It has no green test at the end**, which is the property that makes it eat a day silently if it
is not sized as a workstream. It is sized as one — [`README.md` § 9](README.md#9-sizing) — and its
output is a paragraph in `docs/m3/SPIKE.md` recording what was observed against which versions, so
the next person to wonder why every chart is a PNG can read the answer instead of re-running it.

---

## 2. Fixtures

The thirteen `companyfacts` fixtures M1 and M2 built are the input set; M3 adds two, and neither is
a financial-data fixture.

### What the existing set already covers, for M3's purposes

| Fixture | What it exercises in the renderer |
|---|---|
| `AAPL.trimmed` | the happy path, **and** `391035000000.01` — the value that catches a float round-trip in the appendix and on an axis label |
| `BANK.trimmed` | a chart with no `OPERATING_INCOME`: the omission slot, not an empty axes |
| `IPO.trimmed` | fewer than two annual periods: every chart omitted, and a report that is still worth printing |
| `NOPERIODIC.trimmed` | an `OBSERVED` spine, which must be labelled on the cover *and* in the coverage table |
| `RESTATER.trimmed` | a restatement record long enough to test the appendix's pagination |
| `NCI.trimmed` | the `liabilities_nci_approximated` finding, rendered in caveats with its detail intact |
| *(none)* | `companyfacts` absent — passed as `None`, no fixture needed; this is the **exit 3** case |

### One new fixture, and a hostile value that needs none

**The hostile company name is constructed, not stored.** `conftest.history()` already takes `name=`,
so `test_render.py` passes `<script>alert(1)</script> & "Sons" <img src=x onerror=1>` directly. A
`HOSTILE.json` submissions payload was the first plan and is worse: it puts the attack sixteen
columns away from the assertion that depends on it, and it invites the reader to wonder which of the
sixteen matters. An ampersand in a company name is not hypothetical, which is the whole reason this
is tested at M3 rather than deferred to M6 with the LLM.

The CSS-injection half needs no fixture at all, and that is the stronger outcome. The stylesheet is
handed to WeasyPrint as a separate `CSS` object, so **no model value passes through it and no
template references it** — `test_layering::test_the_stylesheet_is_never_templated` asserts that
structurally rather than asserting that a particular value did not get interpolated.

**`tests/fixtures/report/hostile_urls.html`** — a hand-written HTML document with one placeholder,
substituted per case with `https://`, `http://`, `file:///etc/passwd`, a protocol-relative
`//evil.invalid/x`, and a `data:image/svg+xml`. Rendered directly through `render.layout` to assert
the `url_fetcher` raises on each.

Two properties of that fixture are deliberate. **Hand-written**, because no investo template can
produce any of these — which is the point, and also the reason the fetcher would otherwise be tested
only against inputs that never reach it. And **one URL per render**, because the fetcher raises on
the first one it sees, so a single document carrying all five would only ever exercise whichever
WeasyPrint happened to resolve first.

### What is still self-referential, and it is inherited

Every `companyfacts` fixture is synthetic — M1's carried-over curation workstream, recorded in
`tests/fixtures/edgar/PROVENANCE.md`. M3 does not change that and does not make it worse: the
renderer's assertions are about *shape* (a chart exists, a value is escaped, a page count is in
band, two runs agree), and none of them is a claim about a filer's real numbers. The one M3
assertion that reads like a value assertion — `391035000000.01` in the appendix — is a claim about
the **format pipeline**, and it holds whether or not Apple ever filed that number.

---

## 3. Determinism

The gate, stated precisely, because the loose version is what makes it fail for reasons that are
not bugs:

> Two runs of `investo analyze TICKER --as-of DATE` against one cache, on one machine, with
> `--llm none`, produce byte-identical `report.pdf` and `report.json`.

Four qualifiers, each earning its place:

- **`--as-of DATE`.** Without it `as_of` is today, and `SOURCE_DATE_EPOCH` is derived from `as_of`,
  so two runs either side of midnight legitimately differ. The gate is about the renderer, not the
  calendar.
- **One cache.** A run after `--refresh` sees different `fetched_at` values and produces different
  bytes. That is the gate working — the document is a function of the cache — and
  `docs/m2/04-serialize.md` § 5 already says so for `report.json`.
- **One machine.** Fonts and FreeType. [`README.md` § 7 question 8](README.md#7-spec-questions);
  proposed for DESIGN §11 and §12 rather than left as folklore.
- **`--llm none`.** M6's response cache brings the LLM path inside the gate; until then it is
  outside it by not existing.

Three tests:

| Test | Asserts |
|---|---|
| `test_two_runs_are_byte_identical` | the full command twice into two directories, `report.pdf` and `report.json` compared as bytes |
| `test_chart_bytes_are_stable` | one `ChartImage` built twice — isolates matplotlib from WeasyPrint, so a failure names the layer |
| `test_source_date_epoch_is_restored` | the environment variable is absent after a render that started without it |

The second exists because a single end-to-end determinism test that fails tells you the report is
nondeterministic and nothing else, and the first thing anyone does is start bisecting the stack by
hand.

---

## 4. New layering rules

Four, added to `tests/test_layering.py`, each following the file's existing shape: a predicate over
the AST, a rule, and a pinned allowlist so that widening it is a visible edit.

**1. The float allowlist holds exactly one key.**

```python
FLOAT_ALLOWED = {"report/charts.py": "matplotlib plots coordinates; see docs/m3/01-charts.md §3"}
```

`test_no_float_in_normalize_or_report` gains the exclusion, and a companion asserts
`set(FLOAT_ALLOWED) == {"report/charts.py"}` — the same treatment `USGAAP_LITERAL_ALLOWED` gets,
for the same reason.

**2. Every `float()` call in `charts.py` is inside `coord`.**

The rule that makes the allowlist a boundary rather than an exemption. Walk `charts.py`'s AST,
collect every `ast.Call` to `float`, and assert the enclosing `FunctionDef` is named `coord`. The
converse is asserted too — `coord` must contain one, or the rule is passing because the function
was renamed.

**3. Every float literal in `charts.py` is a named module-level constant.**

An `ast.Constant` of type `float` must be reachable from a module-level `ast.Assign`/`ast.AnnAssign`
whose target is an uppercase `Name`. `alpha=0.35` inside a plotting call fails; `GRID_ALPHA` at the
top does not.

**4. No template performs arithmetic.**

Not a Python AST rule — Jinja's. `jinja2.Environment(...).parse(source)` returns a node tree;
walking it for `nodes.Add`, `Sub`, `Mul`, `Div`, `FloorDiv`, `Mod` and `Pow` fails the build on
`{{ revenue / shares }}`. Two companions, both necessary:

- `test_the_template_arithmetic_detector_detects` — parses a snippet that must trip it, the same
  treatment `test_the_sort_and_float_detectors_actually_detect` gives the other two new predicates.
- `test_templates_exist_to_be_checked` — the rule iterates a directory, and an empty directory
  passes vacuously. Same argument as `test_m2_trees_exist_to_be_checked`, one layer up.

**Plus two set edits, which are not new rules but are the two most likely to be forgotten:**

- `COMMAND_CLOCK_READ_ALLOWED` gains `"analyze.py"`, and
  `test_every_clock_reading_module_is_a_command_body`'s pinned set with it.
- `_NORMALIZE_FORBIDDEN` gains `"numpy"` ([`README.md` § 7 question 14](README.md#7-spec-questions)).

---

## 5. The guarantee→violation-test table

CLAUDE.md: *"For any sentence of the form 'X cannot happen', write the test that attempts X and
asserts it fails."* M3's sentences, and the test that attempts each.

| Guarantee | The attempt |
|---|---|
| No number reaches the page without a `Fact` behind it | `test_every_rendered_figure_is_interned` — walk the `ReportModel` for every source it carries, walk `report.json`'s `sources`, assert the first is a subset of the second |
| A chart cannot be drawn from bare numbers | type-level: `tests/fixtures/typing/chart_from_floats.py` passes a `list[float]` to `build`, and `test_typing` asserts basedpyright rejects it |
| A template cannot compute a figure | `test_no_arithmetic_in_templates`, plus its detector test |
| The renderer resolves no URL | `test_url_fetcher_denies_every_scheme` over the hostile HTML fixture — `https`, `file`, `data:image/svg+xml`, and a protocol-relative `//evil.invalid/x` |
| Untrusted text cannot inject markup | `test_hostile_company_name_is_escaped` — the escaped form is present, `<script` is absent, and the PDF still renders |
| Untrusted text cannot reach the stylesheet | `test_no_model_value_is_interpolated_into_css` — the CSS file is static; assert the rendered `<style>` content equals the file byte for byte |
| A `Decimal` cannot round-trip through a float into printed text | `test_unrepresentable_value_prints_exactly` — `391035000000.01` in the appendix and on the axis label |
| `--explain` changes nothing but the run record | `test_explain_does_not_change_the_pdf` — two runs, PDFs equal, `report.json` differing in exactly one key |
| `--brief` changes nothing but the template | `test_brief_report_json_is_identical` |
| A pre-69 WeasyPrint cannot be used | `test_version_floor_is_enforced` — monkeypatch `weasyprint.__version__` to `"68.0"`, assert `ConfigError` |
| `SOURCE_DATE_EPOCH` cannot leak | `test_source_date_epoch_is_restored` |
| An unbuilt milestone cannot cause exit 3 | `test_missing_forecast_section_exits_0` — a complete AAPL run exits 0 with four sections absent |
| Exit 3 cannot be returned without the files | `test_exit_3_still_writes_both_files` — `companyfacts=None`, assert code 3 **and** both paths exist and parse |

The last two are the pair that matters most, because they are the two halves of the one exit code
this milestone introduces and each passes on its own under an implementation that gets the other
wrong.

---

## 6. Markers, and what is not in CI

| Marker | Selected by default | Why |
|---|---|---|
| `spec` | yes | as before |
| `surface` | yes | as before |
| `network` | no | as before |
| `typing` | yes | as before; M3 adds one violation fixture |
| **`spike`** | **no** | new. Writes files, prints rather than asserts, and takes tens of seconds. `addopts` becomes `-m 'not network and not spike'`. |

**Rendering runs in CI**, which is a change: WeasyPrint needs Pango and cairo from the platform, so
the GitHub Actions workflow gains an `apt-get install` step. The alternative — marking every render
test as opt-in — would put the milestone's entire test surface outside the gate, and
`test_every_declared_dependency_is_importable` would fail anyway the moment the three dependencies
are declared.

The coverage floor is *still* not set here. `pyproject.toml`'s comment specifies the procedure —
run `make coverage`, set `fail_under` to the measured total rounded down to the nearest 5, put the
measurement in the commit message — and M3 is a reasonable milestone to finally do it, since the
suite now runs. That is a task, not a design decision.

---

## 7. What M3 does not test, and why

- **That the PDF looks good.** [`README.md` § 7](README.md#7-spec-questions)'s accepted risk. Page
  count, geometry and byte-identity are all satisfied by a correctly-typeset bad report.
- **Cross-machine byte-identity.** Not a property we have — see [§ 3](#3-determinism).
- **Chart pixel content.** No golden-image diffing: it needs a rendering backend pinned to a font
  stack we do not control, and its failures are uninterpretable without opening an image.
  `test_chart_bytes_are_stable` catches *change*, which is what a regression test is for; whether
  the bars are the right height is `test_report_model`'s job, over the `Fact`s that went in.
- **WeasyPrint's own CSS conformance.** Pinned to a version range, and their test suite is theirs.
- **The `report.json` reader path.** Still no reader — `docs/m2/04-serialize.md` § 7, unchanged.

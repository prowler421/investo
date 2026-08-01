# M3 — Rendering

`src/investo/report/render.py`. DESIGN §9.0 is normative and unusually specific: it names two CVEs,
a minimum version, three required settings and three sources of nondeterminism. This document works
through each and adds the parts §9.0 leaves to the implementation.

---

## 1. The pipeline

```
ReportModel  →  Jinja2 (autoescape, StrictUndefined)  →  HTML string
             →  WeasyPrint HTML(string=…, url_fetcher=deny)  →  Document
             →  Document.write_pdf()  →  bytes
```

Two intermediate values are exposed rather than hidden inside one call, and both exist for tests
rather than for callers:

- `render_html(model, *, brief) -> str` — what the autoescape and no-arithmetic tests read, and
  what a human debugs. Rendering to HTML is fast and diffable; rendering to PDF is neither.
- `layout(html) -> Document` — WeasyPrint's `Document`, before `write_pdf`. §11 says overflow
  detection *"has no WeasyPrint API — use `Document.pages` box geometry against the page box"*,
  and that needs the `Document`, not the bytes. [§ 6](#6-page-geometry).

`render_report(model, *, brief, source_date_epoch) -> Rendered` composes the three and returns the
bytes **plus the two things only the laid-out `Document` knows** — page count and overflow. One pass,
not two: laying an A4 document out twice to print a page number in the command's summary would
double the slowest step in the run, and re-laying it out is also how the number and the file come to
disagree. `render_pdf` is the bytes-only wrapper, and it is what §11's determinism assertion calls.

Neither writes. The same rule `serialize` follows, for the same reason: the command owns the path,
and §11's assertion is then a bytes comparison rather than a filesystem fixture.

---

## 2. The Jinja environment

```python
Environment(
    loader=FileSystemLoader(_templates_dir()),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    auto_reload=False,
)
```

**`autoescape=True`, not `select_autoescape(...)`.** §9.0 requires autoescape and notes Jinja does
not enable it by default. `select_autoescape` chooses by file extension, which means a template
renamed from `.html.j2` to `.j2` silently loses escaping — a rename is not a change anyone reviews
as a security edit. There is exactly one output format here, so the unconditional form is both
simpler and stronger.

**`StrictUndefined`.** A typo'd variable renders as the empty string by default, which in this
document means a missing figure looks exactly like a figure that is legitimately absent — and the
absent case has a *deliberate* rendering (`—`, plus a stated reason) that a blank does not match.
The same argument `facts.py` makes about its table: *"a blank row is indistinguishable from a
rendering bug."*

**`auto_reload=False`** and no bytecode cache. Auto-reload stats the template file on every render;
the cache writes `.pyc`-alike files into a directory whose location depends on the environment.
Neither is wanted, and the second is a determinism hazard that only shows up on the second run.

**The templates directory comes from the installed package.**

```python
def _templates_dir() -> Path:
    return Path(str(importlib.resources.files("investo.report") / "templates"))
```

Not `Path(__file__).parent / "templates"`. The `src/` layout exists so tests import the installed
package rather than the working tree (ROADMAP § Decided during design), and `__file__` re-opens
exactly that hole: CI green against a template that was never packaged, discovered by the first
person to `pip install` it. `tests/test_layering.py` already anticipates the `pathlib` import —
`pathlib` was deliberately left out of `_NORMALIZE_FORBIDDEN` with a comment naming this milestone.

A build test asserts the directory is in the wheel, because `importlib.resources` finding nothing
is a runtime error rather than a build one.

---

## 3. The `url_fetcher` denies everything

§9.0: *"a **deny-by-default `url_fetcher`** so no remote resource is ever resolved."* At M3 that is
strengthened from deny-by-default to **deny, full stop**, and the strengthening is bought by the
PNG choice.

```python
def _deny(url: str) -> NoReturn:
    raise RenderSecurityError(f"the renderer resolved no URLs; refused: {url[:120]}")
```

Every chart is a PNG embedded as `data:image/png;base64,…`, and the CSS is handed to WeasyPrint as
a separate `CSS` object rather than written into a `<style>` element or linked. So there is no
legitimate URL in the document at all, and the fetcher has no allow branch to get wrong.

**The separate `CSS` object is not just tidier — it is what closes the CSS-injection context.**
`<style>` is an HTML raw-text element, so entity references are *not* parsed inside it: autoescaping
a stylesheet through Jinja would turn every `"` in a `font-family` into `&quot;` and break the rule,
and the repair anyone reaches for is `|safe`, which reopens the injection §9.0's CVE-2026-49452 is
about. Keeping the stylesheet out of the template engine entirely means **no model value can reach
it**, asserted structurally by `test_layering::test_the_stylesheet_is_never_templated` rather than by
a test that one particular value was not interpolated. An `<img src="https://…">` that reached the template — through a
company name, a finding detail, or an M6 filing quote — raises rather than fetching.

**This is a property of the PNG decision, not of the design, and it is the part worth carrying into
the spike.** §9.0 requires SVG to be referenced as a *file*, not a `data:` URI (WeasyPrint issue
#134). Promoting one chart to SVG therefore replaces the unconditional deny with an allowlist of
absolute paths — which is still safe, and is a materially weaker thing to have to reason about:
`file://` plus a path check is a comparison, and comparisons have edge cases (symlinks, `..`,
case-insensitive filesystems, percent-encoding) that "raise unconditionally" does not.
[`README.md` § 7 question 4](README.md#7-spec-questions).

**Version, not fetcher, is what closes the SSRF.** §9.0 is explicit that CVE-2025-68616 —
`urllib` following redirects *without* re-invoking a custom `url_fetcher` — *"defeats the very
mitigation prescribed"* on any pre-68.0 version. The fetcher above is worth nothing against it. So
the `>=69` floor is load-bearing and is asserted at import:

```python
if tuple(int(p) for p in weasyprint.__version__.split(".")[:1]) < (69,):
    raise ConfigError("WeasyPrint >= 69 is required (DESIGN.md §9.0: CVE-2025-68616, CVE-2026-49452).")
```

A version check in code as well as in `pyproject.toml`, because a lockfile is a claim about one
environment and someone will eventually install into another.

**Presentational hints off.** `HTML(...).render(presentational_hints=False)` — the default, stated
explicitly rather than relied on, because CVE-2026-49452 is scoped precisely to rendering untrusted
HTML with them on, and "we get this by default" is a fact about a version.

---

## 4. `SOURCE_DATE_EPOCH`

WeasyPrint writes a `/CreationDate` and a document `/ID` derived from the current time. It honours
`SOURCE_DATE_EPOCH` — a build-reproducibility convention, read from the environment — and that is
the mechanism §9.0 prescribes.

Reading the environment is fine; **setting it is where the design decision is.** Three constraints
collide:

1. Nothing under `report/` may read a clock (`test_no_clock_read_in_normalize_or_report`, empty
   allowlist).
2. A library function that mutates `os.environ` and does not restore it has changed the behaviour
   of everything that runs after it in the same process — including the *next* test.
3. A determinism setting the caller has to remember to apply is a setting that gets forgotten by
   the next caller, which at M7 is a batch runner over hundreds of tickers.

Resolved as: **the value is computed at the command boundary from `as_of`; the renderer applies it
and restores it.**

```python
@contextmanager
def deterministic_pdf(epoch: int) -> Iterator[None]:
    previous = os.environ.get("SOURCE_DATE_EPOCH")
    os.environ["SOURCE_DATE_EPOCH"] = str(epoch)
    try:
        yield
    finally:
        if previous is None:
            del os.environ["SOURCE_DATE_EPOCH"]
        else:
            os.environ["SOURCE_DATE_EPOCH"] = previous
```

`render_pdf` wraps its `write_pdf` call in it, so constraint 3 is satisfied structurally, and the
epoch arrives as a parameter, so constraint 1 is too. A test asserts the variable is absent after a
render that started without it — a leaked environment variable is the kind of thing that makes one
test suite's results depend on the order it ran in.

**The epoch is `as_of` at midnight UTC**, computed in `analyze.py`:
`int(datetime.combine(as_of, time.min, tzinfo=UTC).timestamp())`. Not `0`, which would put every
report's creation date at 1970 and make the field actively misleading; not the wall clock, which
would break the gate. `as_of` is an *input to the run*, so a PDF whose creation date is `as_of` is
a function of its inputs — which is the whole claim.

Two runs on different days therefore produce different bytes, and that is correct: they had
different inputs. §11's gate is *"two runs, identical output hash"* and the runs it means are runs
of the same thing. A test pins the point by rendering twice with an explicit `--as-of` and
comparing.

---

## 5. What reaches the renderer that we did not write

M3 has no LLM, so it is tempting to read §9.0's *"untrusted text reaches the renderer"* as a M6
problem. It is not. Four strings in an M3 report come from outside:

| String | Source | What is in it |
|---|---|---|
| company name | `submissions`, or the SEC ticker file | `&` is common; `"` and `'` occur |
| `sic_description` | `submissions` | SEC-authored, still not ours |
| finding `detail` | `normalize/statements.py`, interpolating tags and dates | ours, but assembled |
| `--peers`, `--assumptions` path | the user | echoed into the run block on the appendix page |

Autoescape handles all four. The reason to enumerate them is that the hostile fixture has to
contain them: `tests/fixtures/edgar/submissions/HOSTILE.json` carries a company name of
`<script>alert(1)</script> & "Sons"`, and the test asserts the rendered HTML contains the escaped
form and does not contain a `<script` tag — and, separately, that the PDF still renders, because an
escaping bug that crashes is a much better outcome than one that does not and is a different test.

---

## 6. Page geometry

§11: *"Render golden fixtures to PDF, assert page count. Overflow detection has no WeasyPrint API —
use `Document.pages` box geometry against the page box, or golden-image diffing."*

Page count alone is a weak assertion — a report whose table runs off the right edge has the same
page count as one that does not. So the check is geometric:

```python
def overflows(document: Document) -> tuple[str, ...]:
    """Boxes whose right or bottom edge is outside the page box, with their text."""
```

Walking `page._page_box` descendants and comparing each box's `position_x + width` against the page
box's content edge. This reaches into WeasyPrint internals, which is a liability worth stating: the
`_page_box` attribute is private and the walk will break on some future version. That is what the
`<70` ceiling is for, and the test that uses it asserts a helpful message on breakage rather than
an opaque `AttributeError`.

Golden-image diffing is the alternative §11 offers and is rejected for M3: it requires a rendering
backend in CI, produces failures that a human has to look at an image to interpret, and its
sensitivity is exactly the cross-machine font variance that
[`README.md` § 7 question 8](README.md#7-spec-questions) says we do not have. Geometry is coarser
and it fails for a reason you can read.

Two assertions on the fixtures: **the full report is within a page-count band** (a hard equality
fails on any content change, which is a gate that gets deleted the second time it fires), and
**`--brief` is exactly 2 pages** — that one is an equality, because "2-page summary" is what README
promises and a 3-page brief is a broken promise rather than a content change.

---

## 7. What `render.py` does not do

- **It does not write a file.** Same rule as `serialize`.
- **It does not choose the output path**, or create a directory, or decide what happens if one
  exists. `analyze.py` owns all three.
- **It does not build charts.** It receives a `ReportModel` that already holds `ChartImage`s, so
  `render_html` can be tested with no matplotlib import in the test at all — which matters, because
  matplotlib is the slowest import in the dependency set and the escaping tests are the ones that
  should be cheap enough to run constantly.
- **It does not read config.** `Settings` reaches it as the already-allowlisted `RunInfo.config`
  mapping that `serialize` built, so the appendix and `report.json` print the same fields by
  construction rather than by two matching allowlists.

# M3 — The renderer spike

Status: **not run.** This file is the record and it is empty on purpose.

ROADMAP M3 calls the matplotlib→WeasyPrint seam *"the largest un-hedged implementation risk in the
project"* and says to build the margin stack and a `fill_between` chart **first**, as a spike,
before committing to SVG anywhere. `tests/spike_renderer.py` is that probe. It is marked `spike` and
deselected by default, because its output is a decision rather than a pass or a fail.

```bash
uv run pytest tests/spike_renderer.py -m spike -s
```

**Until this file has answers in it, "every chart is a PNG" is an interim decision rather than a
measurement** — [`README.md` § 7 question 4](README.md#7-spec-questions). `ChartFormat` is declared
per-chart specifically so that promoting one is a single line in a diff that names it, and
`normalize`-style: the guess is visible where it is made rather than buried in a default.

This file exists unwritten for the same reason `docs/m2/COVERAGE.md` does: a research workstream
with no green test attached is one that silently eats a day while the code looks complete, and the
only defence is a document whose emptiness is itself the status.

---

## The five questions, and what each decides

| # | Question | DESIGN §9.0 reference | What the answer changes |
|---|---|---|---|
| 1 | Does SVG survive WeasyPrint for a `clipPath`-wrapped axes? | issues #1374, #1595, #526 | Whether any chart can be SVG at all. matplotlib wraps *all* axes content in one `clipPath`. |
| 2 | Do `<use xlink:href>` glyph references render, or come out blank? | #2375; fixed in 69.0 | Whether `svg.fonttype` can stay at `"none"`, or has to be `"path"` (larger files, no selectable text). |
| 3 | Does `fill_between` alpha clip the text near it? | #2332 | **M5's fan chart.** Worth answering two milestones early, which is why the spike draws one now. |
| 4 | Is the PNG path byte-identical run to run with `metadata={"Software": None}`? | — (this is the key §9.0 does not name) | Whether §11's determinism gate holds across matplotlib patch releases. |
| 5 | Does the `Document.pages` geometry walk work against the installed WeasyPrint's private `_page_box`? | §11's overflow detection | Whether `report/render.overflows` is a test or a liability. |

Questions 4 and 5 are the only two the spike can answer without a human looking at a picture. The
other three are why it prints a path and says **open it**.

---

## Findings

*(To be written on the run. One line per question, naming the exact matplotlib and WeasyPrint
versions observed — a finding without a version is a finding that cannot be rechecked.)*

```
matplotlib  <version>
weasyprint  <version>
platform    <os / arch>

1. clipPath  →
2. <use>     →
3. alpha     →
4. PNG bytes →
5. geometry  →
```

## Decision

*(To be written.)*

**Before writing it, note that "SVG renders correctly" is not by itself a reason to promote
anything.** That reading is the obvious one and it is wrong, because the PNG choice is load-bearing
for something other than rendering: a PNG embeds as a `data:` URI, and that is what lets
`report/render.py` refuse **every** URL unconditionally. §9.0 requires an SVG be referenced as a
*file*, not a `data:` URI (WeasyPrint #134), so promoting even one chart converts the unconditional
deny into an allowlist of absolute paths — a comparison, with the edge cases comparisons have
(symlinks, `..`, case-insensitive filesystems, percent-encoding).

So the decision has two inputs, not one:

| Spike says | Promote? |
|---|---|
| SVG breaks (blank axes, missing glyphs, clipped text) | **No.** Settled, and the interim decision becomes a measured one. |
| SVG renders correctly | **Not automatically.** The question becomes whether vector text in five charts is worth trading an unconditional deny for a path allowlist. State the answer here either way. |

The second row is the one worth deciding deliberately, because the natural thing to do with a green
spike result is to act on it. Whatever is chosen, this file records *both* the rendering finding and
the security trade — otherwise a future reader sees "SVG works" next to "every chart is PNG" and
reasonably concludes someone forgot to finish the job.

"""The matplotlib→WeasyPrint spike (ROADMAP M3, run first).

ROADMAP calls this seam *"the largest un-hedged implementation risk in the project"* and says to
*"build the margin stack and a `fill_between` chart **first**, as a spike, before committing to SVG
anywhere."* This is that spike.

**It is not a test.** It writes files, prints findings, and asserts almost nothing — its output is a
decision, recorded in ``docs/m3/SPIKE.md``, not a pass or a fail. Marked ``spike`` and deselected by
default for the same reason ``network`` is: a run that is not a test should not be able to fail the
build, and should not cost anyone ten seconds on every ``make test``.

It lives in ``tests/`` rather than in a scratch directory because that is where the fixtures are,
and because a throwaway script outside the repository cannot be re-run by the next person who
doubts the result.

    uv run pytest tests/spike_renderer.py -m spike -s

Five questions it exists to answer, each of which decides something:

1. Does SVG survive WeasyPrint for a ``clipPath``-wrapped axes? (§9.0: issues #1374, #1595, #526)
2. Do ``<use xlink:href>`` glyph references render, or come out blank? (#2375)
3. Does ``fill_between`` alpha clip the text near it? (#2332) — **this one decides M5's fan chart**,
   which is why it is worth answering two milestones early.
4. Is the PNG path byte-identical run to run with ``metadata={"Software": None}``?
5. Does the ``Document.pages`` geometry walk work against the installed WeasyPrint's private
   ``_page_box``?
"""

# pyright: reportUnknownMemberType=false
#
# Same matplotlib and WeasyPrint stub gaps as `report/charts.py` and `report/render.py`, and this
# file reaches for both directly — it exists to poke at the seam between them.

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from investo.domain.periods import FiscalPeriod, PeriodKind
from investo.domain.provenance import Derivation
from investo.report import charts, render
from investo.report.charts import ChartFormat, ChartKind, ChartSpec, Datum, PlotSeries

OUT = Path("spike-output")
"""Written into the working directory, which the autouse ``clean_env`` fixture has chdir'd into a
``tmp_path``. The path is printed, because a spike whose artifacts nobody can find is a spike that
gets run twice."""


def _points(*values: str) -> tuple[Datum, ...]:
    return tuple(
        Datum(
            value=Decimal(text),
            period=FiscalPeriod(
                start=date(2020 + index, 1, 1),
                end=date(2020 + index, 12, 31),
                kind=PeriodKind.ANNUAL,
            ),
            source=Derivation(rule="spike", inputs=()),
        )
        for index, text in enumerate(values)
    )


def _margins() -> tuple[ChartSpec, tuple[PlotSeries, ...]]:
    """ROADMAP's "margin stack" — three nested margins, drawn as lines.

    Nested, not additive: net is inside operating is inside gross, so stacking them plots a total of
    roughly 1.8× gross margin, which is a number that does not exist. The spike draws the *shape the
    report will use*, because a spike of a chart nothing renders answers a question nobody asked.
    """
    spec = ChartSpec(
        chart_id="spike_margins",
        title="Gross, operating and net margin",
        kind=ChartKind.LINES,
        y_label="% of revenue",
        percent_axis=True,
    )
    return spec, (
        PlotSeries("Gross", _points("0.44", "0.43", "0.45", "0.46"), charts.SERIES_PRIMARY),
        PlotSeries("Operating", _points("0.30", "0.29", "0.30", "0.32"), charts.SERIES_SECONDARY),
        PlotSeries("Net", _points("0.25", "0.24", "0.25", "0.27"), charts.SERIES_TERTIARY),
    )


@pytest.mark.spike
def test_spike_svg_and_png_through_weasyprint(tmp_path: Path) -> None:
    """Render both formats into one PDF and print what happened. Question 1, 2 and 3."""
    OUT.mkdir(exist_ok=True)
    spec, series = _margins()

    images: list[tuple[str, bytes, str]] = []
    for chart_format in (ChartFormat.PNG, ChartFormat.SVG):
        image = charts.build(
            ChartSpec(
                chart_id=f"{spec.chart_id}_{chart_format}",
                title=spec.title,
                kind=spec.kind,
                y_label=spec.y_label,
                percent_axis=spec.percent_axis,
                chart_format=chart_format,
            ),
            series,
        )
        path = OUT / f"margins.{chart_format}"
        _ = path.write_bytes(image.payload)
        images.append((str(chart_format), image.payload, image.media_type))
        print(f"[spike] {chart_format}: {len(image.payload):,} bytes -> {path.resolve()}")

    # Both embedded as data: URIs so the deny-everything fetcher is exercised too. Note that §9.0
    # says an SVG must be referenced as a *file* (#134) — so if the SVG below renders blank, that is
    # a finding about the data: URI rather than about SVG, and the follow-up is the file form.
    import base64

    body = "".join(
        f"<h2>{label}</h2>"
        f'<img src="data:{media};base64,{base64.b64encode(payload).decode("ascii")}">'
        for label, payload, media in images
    )
    html = f"<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>{body}</body></html>"

    document = render.layout(html)
    pdf = document.write_pdf()
    target = OUT / "spike.pdf"
    _ = target.write_bytes(bytes(pdf))
    print(
        f"[spike] pdf: {len(bytes(pdf)):,} bytes, {len(document.pages)} pages -> {target.resolve()}"
    )
    print("[spike] OPEN IT. A blank box where a chart should be is the answer to question 1 or 2.")


@pytest.mark.spike
def test_spike_fill_between_alpha(tmp_path: Path) -> None:
    """Question 3, on its own, because it decides M5's fan chart.

    §9.0: *"#2332 — text in an SVG with alpha < 1 gets cut off, and every fan chart is
    `fill_between(alpha=…)`."* Built with the object API and saved both ways; if the SVG's tick
    labels are missing next to the band, the fan chart is a PNG at M5 and that is decided now.
    """
    import matplotlib
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    OUT.mkdir(exist_ok=True)
    xs = list(range(12))
    low = [charts.coord(Decimal(100 + index * 4)) for index in xs]
    high = [charts.coord(Decimal(140 + index * 12)) for index in xs]

    for suffix, saver in (("png", {"dpi": charts.PNG_DPI}), ("svg", {})):
        with matplotlib.rc_context({"svg.hashsalt": f"spike_fan_{suffix}", "svg.fonttype": "none"}):
            figure = Figure(figsize=charts.FIGSIZE_WIDE)
            _ = FigureCanvasAgg(figure)
            axes = figure.add_axes(charts.AXES_RECT)
            _ = axes.fill_between(
                xs, low, high, alpha=charts.GRID_ALPHA, color=charts.SERIES_PRIMARY
            )
            axes.set_title("fill_between with alpha — does the text survive?")
            path = OUT / f"fan.{suffix}"
            figure.savefig(path, format=suffix, **saver)  # pyright: ignore[reportArgumentType]
            print(f"[spike] fan.{suffix}: {path.stat().st_size:,} bytes -> {path.resolve()}")


@pytest.mark.spike
def test_spike_determinism_and_geometry(tmp_path: Path) -> None:
    """Questions 4 and 5. The only two the spike can answer without a human looking at a picture."""
    spec, series = _margins()
    first = charts.build(spec, series)
    second = charts.build(spec, series)
    print(f"[spike] png bytes identical across two builds: {first.payload == second.payload}")
    print(f"[spike] png mentions matplotlib: {b'matplotlib' in first.payload.lower()}")

    document = render.layout("<!DOCTYPE html><html><body><p>geometry probe</p></body></html>")
    try:
        overflowing = render.overflows(document)
    except RuntimeError as error:  # pragma: no cover - that is the finding
        print(f"[spike] geometry walk BROKEN against this WeasyPrint: {error}")
    else:
        print(
            f"[spike] geometry walk works; {len(overflowing)} overflowing boxes on a trivial page"
        )

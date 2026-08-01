"""matplotlib charts for report section 4 (ROADMAP M3, DESIGN.md §9.0 and §9.1).

**This is the only module in the package permitted to construct a ``float``** (CLAUDE.md convention
13), and most of what is below is about keeping that permission from spreading. Three rules, each
enforced by an AST test rather than by review:

1. Every ``Decimal → float`` conversion happens inside :func:`coord`. A conversion in a
   comprehension three functions away fails the build — the allowlist is a *boundary*, not an
   exemption.
2. Every float literal is a named module-level constant, so the file's entire float surface is the
   reviewable block below rather than a scatter of magic numbers inside plotting calls.
3. Nothing here formats a number into text. Every visible string — axis tick labels included —
   comes from :mod:`investo.report.format`, which takes a ``Decimal``.

Rule 3 is the one carrying the guarantee. A plotted coordinate is a *position*, and the difference
between ``391035000000.01`` and its nearest double is a few trillionths of a pixel. What is not
acceptable is that number coming back out as text, so the tick *positions* go through
:func:`coord` and the tick *labels* are formatted from the ``Decimal`` the position came from. The
violation test is behavioural: ``test_charts::test_unrepresentable_value_labels_exactly``.

**A chart takes traced points, never numbers** (convention 16). :class:`Datum` cannot be
constructed without a ``Provenance`` and :class:`PlotSeries` accepts nothing else, so a caller
holding a bare number cannot draw it. That is a signature rather than a check, and it is
deliberate — the failure it prevents (M4 plotting a peer median with no source attached) has no
test that would catch it after the fact.

**No ``pyplot``.** The ``Figure``/``FigureCanvasAgg`` object API needs no global figure registry, no
backend selection at import time, and no ``close()`` discipline to avoid leaking five figures per
run. It also removes the second place ``rcParams`` could be mutated from, which would silently undo
the per-chart ``rc_context`` scoping below.
"""

# pyright: reportUnknownMemberType=false
#
# matplotlib ships inline types, and it types nearly every Artist method with `**kwargs: Unknown` —
# `set_yticks`, `bar`, `plot`, `legend`, `twinx`, `rc_context` and a dozen more. Under `strict` that
# is an error per call site, and there is no annotation on our side that fixes it: the unknown is in
# the library's own signature.
#
# Scoped to this file rather than to `report/`, because `format.py`, `model.py` and `serialize.py`
# have no third-party surface and should keep the rule. `render.py` carries the same comment for
# WeasyPrint, which ships no types at all.
#
# What this does **not** relax is anything the milestone actually guarantees. The float boundary is
# an AST rule, not a type rule (`test_layering::test_every_float_call_is_inside_coord`); `Datum` and
# `PlotSeries` are ours and fully typed; and `coord` is annotated `Decimal -> float`. Suppressing
# what matplotlib does not tell us costs none of that.

from __future__ import annotations

import base64
import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any, Final

import matplotlib
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from investo.domain.models import Fact
from investo.domain.periods import FiscalPeriod
from investo.domain.provenance import Provenance
from investo.normalize.statements import Bucket
from investo.report import format as fmt

__all__ = [
    "MIN_POINTS",
    "PNG",
    "SVG",
    "ChartFormat",
    "ChartKind",
    "ChartSpec",
    "Datum",
    "PlotSeries",
    "ChartImage",
    "coord",
    "build",
    "axis_ticks",
]

# ---------------------------------------------------------------------------
# The float surface. Every float literal in this module is here.
# ---------------------------------------------------------------------------
FIGSIZE_WIDE: Final = (6.4, 2.9)
"""Inches. 6.4 fits an A4 content column at the margins ``report.css`` sets."""

AXES_RECT: Final = (0.13, 0.20, 0.74, 0.72)
"""``add_axes`` rectangle, in figure fractions.

An explicit rectangle rather than ``tight_layout`` or ``constrained_layout``. Both of those solve
for a layout from measured text extents, which makes the figure's geometry a function of the host's
font metrics — and DESIGN §11's gate is about bytes. A fixed rect is the same on every run of the
same matplotlib, which is exactly the scope the gate claims.

The right edge stops at 0.87 rather than 0.97 to leave room for the secondary axis the revenue
chart carries; charts without one pay a little whitespace for one layout constant instead of two.
"""

BAR_WIDTH: Final = 0.62
GROUPED_BAR_WIDTH: Final = 0.26
LINE_WIDTH: Final = 1.6
MARKER_SIZE: Final = 3.5
GRID_ALPHA: Final = 0.28
SPINE_WIDTH: Final = 0.8
ZERO_RULE_WIDTH: Final = 1.0
LABEL_PAD: Final = 4.0
Y_HEADROOM: Final = Decimal("1.06")
"""The top tick sits ~6% above the largest value, so a bar never touches the axes frame."""

# ---------------------------------------------------------------------------
# Palette. Named for role, not for colour — the same values appear in report.css,
# and a hex code written twice is a hex code that drifts.
# ---------------------------------------------------------------------------
SERIES_PRIMARY: Final = "#1f3a5f"
SERIES_SECONDARY: Final = "#c2703d"
SERIES_TERTIARY: Final = "#5b8266"
SERIES_MUTED: Final = "#9aa5b1"
RULE_GREY: Final = "#4a4a4a"
GRID_GREY: Final = "#c9ced4"

FONT_SIZE_TICK: Final = 7
FONT_SIZE_LABEL: Final = 8
FONT_SIZE_LEGEND: Final = 7
PNG_DPI: Final = 300
"""§9.0: *"PNG at 300 dpi for anything using clipping or alpha."* Applied to every chart while
every chart is a PNG, so a promotion to SVG does not also change the raster resolution of the ones
left behind."""

MIN_POINTS: Final = 2
"""A chart needs two points in the bucket to be drawn.

One point is not a trend and a single floating bar reads as one; zero points is an empty axes, which
reads as a rendering failure. Below the threshold the slot renders a stated reason instead —
§6.10's *"a blank space with an explanation beats a confident wrong number"*, applied to a picture.
"""

PNG: Final = "image/png"
SVG: Final = "image/svg+xml"


class ChartFormat(StrEnum):
    """Per-chart, per DESIGN §9.0 — **not** a global setting.

    Every M3 chart is :attr:`ChartFormat.PNG` until the renderer spike promotes one
    (``docs/m3/SPIKE.md``). That is deferral, and it also buys something: a PNG embeds as a
    ``data:`` URI, so with the stylesheet inlined there is no legitimate URL in the document and
    ``report/render.py``'s ``url_fetcher`` denies **every** URL unconditionally. §9.0 requires SVG to
    be referenced as a *file* (WeasyPrint #134), so promoting one chart converts that into an
    allowlist of absolute paths. That cost belongs in the promotion decision.
    """

    PNG = "png"
    SVG = "svg"

    @property
    def media_type(self) -> str:
        return PNG if self is ChartFormat.PNG else SVG


class ChartKind(StrEnum):
    """The four shapes section 4 needs. Five charts, four kinds — `revenue` is the odd one."""

    BARS = "bars"
    GROUPED_BARS = "grouped_bars"
    LINES = "lines"
    BARS_WITH_RATE_LINE = "bars_with_rate_line"


@dataclass(frozen=True, slots=True)
class Datum:
    """A plottable number **and the provenance it came from**. There is no other constructor.

    Not a :class:`~investo.domain.models.Fact`, and the difference is the reason this type exists.
    A ``Fact`` carries a ``Metric``, which is a *selection* out of ``normalize/tags.py``'s registry
    — and three of the series section 4 charts are not selections. Free cash flow, a margin and a
    year-over-year growth rate are each computed in ``report/model.py`` over two filed values, and
    giving one of them ``Metric.OPERATING_CASH_FLOW`` because it was built from one would make the
    appendix name a tag that did not produce the number.

    Not in ``domain/`` either: it is an input to a renderer, and it exists because a chart needs a
    shape that a derived value fits. ``domain/`` is settled and a type added there acquires
    consumers.

    What it keeps is the only property the chart layer needs — **a number cannot be constructed
    here without a `Provenance`**, so a number cannot be plotted without one.
    """

    value: Decimal
    period: FiscalPeriod
    source: Provenance

    @classmethod
    def of(cls, fact: Fact) -> Datum:
        return cls(value=fact.value, period=fact.period, source=fact.source)


@dataclass(frozen=True, slots=True)
class PlotSeries:
    """One line or bar set, and the traced points behind it.

    ``points`` rather than values is the whole provenance mechanism at chart scope: there is no
    overload accepting bare coordinates, so a caller holding a number with no source cannot draw it.
    ``rate`` marks a series that belongs on the secondary percentage axis (revenue's YoY line)
    rather than on the value axis.
    """

    label: str
    points: tuple[Datum, ...]
    colour: str = SERIES_PRIMARY
    rate: bool = False

    @classmethod
    def of_facts(
        cls, label: str, facts: Sequence[Fact], *, colour: str = SERIES_PRIMARY
    ) -> PlotSeries:
        return cls(label=label, points=tuple(Datum.of(fact) for fact in facts), colour=colour)

    @property
    def sources(self) -> tuple[Provenance, ...]:
        return tuple(point.source for point in self.points)


@dataclass(frozen=True, slots=True)
class ChartSpec:
    """What to draw, and how the output is identified.

    ``chart_id`` is the ``svg.hashsalt`` namespace as well as the template's key for the image.
    §9.0 warns that one salt shared across several charts in a document can collide, so the salt is
    a function of *which chart* rather than a module-level constant — and scoping it through
    ``rc_context`` makes that structural rather than a discipline.
    """

    chart_id: str
    title: str
    kind: ChartKind
    bucket: Bucket = Bucket.ANNUAL
    y_label: str = ""
    rate_label: str = ""
    scale: fmt.Scale = fmt.Scale.MILLIONS
    percent_axis: bool = False
    chart_format: ChartFormat = ChartFormat.PNG
    figsize: tuple[float, float] = FIGSIZE_WIDE


@dataclass(frozen=True, slots=True)
class ChartImage:
    """A rendered chart, or a stated reason there is none.

    ``sources`` is what lets ``report/model.py`` assert that the appendix's interned array covers
    everything on the page — it is what turns §9.1's *"every number traceable"* from a promise into
    ``test_report_model::test_every_rendered_figure_is_interned``.
    """

    spec: ChartSpec
    media_type: str = PNG
    payload: bytes = b""
    sources: tuple[Provenance, ...] = ()
    points: int = 0
    omitted: str | None = None
    labels: tuple[str, ...] = field(default_factory=tuple)
    """Every tick label the chart printed, so the format guarantee is assertable without OCR."""

    @property
    def drawn(self) -> bool:
        return self.omitted is None

    @property
    def data_uri(self) -> str:
        """``data:image/png;base64,…`` — how the template embeds it.

        A ``data:`` URI rather than a file reference, which is what lets the renderer refuse every
        URL. Raises for an omitted chart rather than returning an empty URI, because an ``<img>``
        with an empty ``src`` renders as a broken-image glyph and the template is supposed to have
        branched on :attr:`drawn` before reaching here.
        """
        if self.omitted is not None:
            raise ValueError(f"chart {self.spec.chart_id} was omitted: {self.omitted}")
        return f"data:{self.media_type};base64,{base64.b64encode(self.payload).decode('ascii')}"


def coord(value: Decimal) -> float:
    """**The only ``Decimal`` → ``float`` conversion in the package.**

    A plotted coordinate is a position, and a position may be approximate: the gap between
    ``391035000000.01`` and its nearest double is about 0.0000000000026 of a pixel at any figure
    size this report uses. What may *not* be approximate is a printed figure, and nothing in this
    module prints one — see the module docstring's rule 3.

    ``tests/test_layering.py`` asserts that every ``float()`` call site under ``report/`` is
    lexically inside this function, and that this function contains one. The second half matters:
    without it the rule passes when the function is renamed away.
    """
    return float(value)


def build(
    spec: ChartSpec, series: Sequence[PlotSeries], *, reason: str | None = None
) -> ChartImage:
    """Render ``series`` to bytes, or return a stated omission.

    Args:
        spec: What to draw.
        series: One or more :class:`PlotSeries`. Every series is plotted against the union of the
            period ends across all of them, so a metric absent for one year leaves a gap rather
            than shifting the rest of its line left by one position.
        reason: An omission reason the caller already knows — a bank with no operating income, a
            filer with no periodic filings. Supplied rather than inferred, because "no data" and
            "this kind of company does not report that" look identical from here and mean different
            things to a reader.

    Returns:
        A :class:`ChartImage`. ``omitted`` is set and ``payload`` is empty when ``reason`` was given
        or when fewer than :data:`MIN_POINTS` distinct periods are available.
    """
    periods = _periods(series)
    if reason is not None:
        return ChartImage(spec=spec, media_type=spec.chart_format.media_type, omitted=reason)
    if len(periods) < MIN_POINTS:
        return ChartImage(
            spec=spec,
            media_type=spec.chart_format.media_type,
            points=len(periods),
            omitted=(
                f"{spec.title} not charted — {len(periods)} of the {MIN_POINTS} periods needed. "
                "See § Caveats, data coverage."
            ),
        )

    # `rc_context` types its parameter as `dict[RcKeyType, Any]`, where `RcKeyType` is a Literal
    # union of every rcParam name — so a plain `dict[str, ...]` is not assignable, invariantly.
    # Ignored rather than worked around: the alternative is repeating four Literal keys in a
    # `cast`, which type-checks and asserts nothing, and a wrong key raises at runtime anyway.
    with matplotlib.rc_context(_rc(spec)):  # pyright: ignore[reportArgumentType]
        figure = Figure(figsize=spec.figsize)
        _ = FigureCanvasAgg(figure)
        axes = figure.add_axes(AXES_RECT)
        labels = _draw(axes, spec, series, periods)
        payload = _save(figure, spec)

    return ChartImage(
        spec=spec,
        media_type=spec.chart_format.media_type,
        payload=payload,
        sources=tuple(source for item in series for source in item.sources),
        points=len(periods),
        labels=labels,
    )


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
def _rc(spec: ChartSpec) -> dict[str, Any]:
    """Per-chart rcParams, scoped by ``rc_context``.

    ``svg.hashsalt`` is namespaced on ``chart_id`` per §9.0. Setting it once globally is the
    colliding case that section warns about, and scoping it here means the namespacing cannot be
    undone by an import order.

    The font family is stated rather than inherited so that a machine with an unusual matplotlib
    config produces the same layout as CI — which is a weaker claim than cross-machine byte
    identity, and is the most that is available (DESIGN §12).
    """
    return {
        "svg.hashsalt": spec.chart_id,
        "svg.fonttype": "none",
        "font.family": "sans-serif",
        "font.size": FONT_SIZE_TICK,
        "axes.linewidth": SPINE_WIDTH,
        "path.simplify": False,
    }


def _save(figure: Figure, spec: ChartSpec) -> bytes:
    """Write the figure to bytes, with the metadata key that format needs stripped.

    Two different keys, which is the part §9.0 does not cover because §9.0 was written about SVG:

    - **PNG** carries a ``Software`` text chunk naming the matplotlib version, so two patch releases
      produce different bytes for an identical chart and §11's gate reports it as a regression in
      our code.
    - **SVG** carries ``<dc:date>``, which is §9.0's ``metadata={"Date": None}``.

    Both are set, though only one is live while every chart is a PNG — the spike promoting a chart
    should not also have to discover the other.
    """
    buffer = io.BytesIO()
    if spec.chart_format is ChartFormat.PNG:
        figure.savefig(buffer, format="png", dpi=PNG_DPI, metadata={"Software": None})
    else:
        figure.savefig(buffer, format="svg", metadata={"Date": None})
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------
def _periods(series: Sequence[PlotSeries]) -> tuple[date, ...]:
    """Every period end across every series, oldest first.

    The union rather than per-series, so two metrics tagged for different years still line up on one
    x axis. ``key=`` is stated on the sort because CLAUDE.md convention 10 forbids a keyless one
    under ``report/``; a list of dates is already totally ordered, and saying so is the point.
    """
    ends = {point.period.end for item in series for point in item.points}
    return tuple(sorted(ends, key=_identity))


def _identity(value: date) -> date:
    """The total sort key convention 10 asks to be stated rather than omitted."""
    return value


def _draw(
    axes: Axes, spec: ChartSpec, series: Sequence[PlotSeries], periods: Sequence[date]
) -> tuple[str, ...]:
    """Dispatch on kind, then apply the shared axis treatment. Returns every printed label."""
    value_series = [item for item in series if not item.rate]
    rate_series = [item for item in series if item.rate]

    if spec.kind is ChartKind.GROUPED_BARS:
        _grouped_bars(axes, value_series, periods)
    elif spec.kind is ChartKind.LINES:
        _lines(axes, value_series, periods)
    else:
        _bars(axes, value_series, periods)

    x_labels = tuple(_period_label(day, spec.bucket) for day in periods)
    axes.set_xticks(list(range(len(periods))))
    axes.set_xticklabels(x_labels, fontsize=FONT_SIZE_TICK)
    axes.set_xlim(-1 + BAR_WIDTH, len(periods) - BAR_WIDTH)

    y_labels = _apply_value_axis(axes, spec, value_series)
    rate_labels: tuple[str, ...] = ()
    if rate_series:
        rate_labels = _rate_axis(axes, spec, rate_series, periods)

    _frame(axes, spec)
    # The legend covers the value series only. A rate series is identified by the secondary axis
    # label instead, because a combined legend across an axes and its twin means collecting handles
    # by hand — and the one chart with a twin has exactly one line on it.
    if len(value_series) > 1:
        axes.legend(loc="best", fontsize=FONT_SIZE_LEGEND, frameon=False)
    return (*x_labels, *y_labels, *rate_labels)


def _bars(axes: Axes, series: Sequence[PlotSeries], periods: Sequence[date]) -> None:
    for item in series:
        by_end = _by_end(item)
        present = [(index, by_end[day]) for index, day in enumerate(periods) if day in by_end]
        axes.bar(
            [index for index, _ in present],
            [coord(value) for _, value in present],
            width=BAR_WIDTH,
            color=item.colour,
            label=item.label,
        )


def _grouped_bars(axes: Axes, series: Sequence[PlotSeries], periods: Sequence[date]) -> None:
    """Side by side, not stacked.

    Assets, liabilities and equity are related by an identity rather than being three parts of a
    whole, and stacking them would plot a total of twice assets. The same reasoning renames
    ROADMAP's "margin stack" to three lines: gross, operating and net margin are nested, so a stack
    of them totals something that does not exist.
    """
    offsets = _offsets(len(series))
    for item, offset in zip(series, offsets, strict=True):
        by_end = _by_end(item)
        present = [(index, by_end[day]) for index, day in enumerate(periods) if day in by_end]
        axes.bar(
            [index + offset for index, _ in present],
            [coord(value) for _, value in present],
            width=GROUPED_BAR_WIDTH,
            color=item.colour,
            label=item.label,
        )


def _lines(axes: Axes, series: Sequence[PlotSeries], periods: Sequence[date]) -> None:
    for item in series:
        by_end = _by_end(item)
        present = [(index, by_end[day]) for index, day in enumerate(periods) if day in by_end]
        axes.plot(
            [index for index, _ in present],
            [coord(value) for _, value in present],
            color=item.colour,
            linewidth=LINE_WIDTH,
            marker="o",
            markersize=MARKER_SIZE,
            label=item.label,
        )


def _offsets(count: int) -> tuple[float, ...]:
    """Bar offsets within a group, centred on the tick.

    Built from ``coord`` over ``Decimal`` arithmetic rather than from float division, because a
    float literal here would have to be a module constant and there is one per group size.
    """
    width = Decimal(str(GROUPED_BAR_WIDTH))
    centre = (Decimal(count) - Decimal(1)) / Decimal(2)
    return tuple(coord((Decimal(index) - centre) * width) for index in range(count))


def _by_end(item: PlotSeries) -> dict[date, Decimal]:
    return {point.period.end: point.value for point in item.points}


def _apply_value_axis(axes: Axes, spec: ChartSpec, series: Sequence[PlotSeries]) -> tuple[str, ...]:
    """Ticks from :func:`axis_ticks`, **labelled from the ``Decimal``**.

    This is where rule 3 in the module docstring is actually implemented. Letting matplotlib choose
    ticks and format them would produce labels rendered from the float it was handed, which is the
    one thing the float is not allowed to do. So the positions are chosen in ``Decimal``, converted
    once through :func:`coord`, and the text beside each one is formatted from the ``Decimal`` that
    produced the position.
    """
    values = [point.value for item in series for point in item.points]
    if not values:
        return ()
    ticks = axis_ticks(values)
    labels = tuple(
        fmt.percent(tick) if spec.percent_axis else fmt.money(tick, scale=spec.scale)
        for tick in ticks
    )
    axes.set_yticks([coord(tick) for tick in ticks])
    axes.set_yticklabels(labels, fontsize=FONT_SIZE_TICK)
    axes.set_ylim(coord(ticks[0]), coord(ticks[-1]))
    if spec.y_label:
        axes.set_ylabel(spec.y_label, fontsize=FONT_SIZE_LABEL, labelpad=LABEL_PAD)
    if ticks[0] < 0:
        axes.axhline(coord(Decimal(0)), color=RULE_GREY, linewidth=ZERO_RULE_WIDTH)
    return labels


def _rate_axis(
    axes: Axes, spec: ChartSpec, series: Sequence[PlotSeries], periods: Sequence[date]
) -> tuple[str, ...]:
    """The secondary percentage axis — revenue's YoY line."""
    twin = axes.twinx()
    _lines(twin, series, periods)
    values = [point.value for item in series for point in item.points]
    ticks = axis_ticks(values)
    labels = tuple(fmt.percent(tick) for tick in ticks)
    twin.set_yticks([coord(tick) for tick in ticks])
    twin.set_yticklabels(labels, fontsize=FONT_SIZE_TICK)
    twin.set_ylim(coord(ticks[0]), coord(ticks[-1]))
    if spec.rate_label:
        twin.set_ylabel(spec.rate_label, fontsize=FONT_SIZE_LABEL, labelpad=LABEL_PAD)
    for side in ("top", "left", "bottom"):
        twin.spines[side].set_visible(False)
    twin.spines["right"].set_color(GRID_GREY)
    return labels


def _frame(axes: Axes, spec: ChartSpec) -> None:
    axes.set_title(spec.title, fontsize=FONT_SIZE_LABEL, loc="left")
    axes.grid(axis="y", color=GRID_GREY, alpha=GRID_ALPHA, linewidth=SPINE_WIDTH)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(GRID_GREY)
    axes.tick_params(length=0, labelsize=FONT_SIZE_TICK)


def _period_label(day: date, bucket: Bucket) -> str:
    """``FY2024`` or ``2024-06-30``.

    The **calendar** year of the period end, never ``filing_fy`` — §4.2(a) forbids reading that, and
    it labels a calendar-Q1-2025 period as fiscal 2026. Same rule ``facts.py`` applies to its table,
    so the chart's x axis and the appendix agree on what a year is called.
    """
    return f"FY{day.year}" if bucket is Bucket.ANNUAL else day.isoformat()


# ---------------------------------------------------------------------------
# tick selection, in Decimal
# ---------------------------------------------------------------------------
_NICE_STEPS: Final = (Decimal("1"), Decimal("2"), Decimal("2.5"), Decimal("5"), Decimal("10"))
_TARGET_TICKS: Final = 5


def axis_ticks(values: Sequence[Decimal], *, target: int = _TARGET_TICKS) -> tuple[Decimal, ...]:
    """Round tick values spanning ``values``, computed entirely in ``Decimal``.

    Exported and tested directly, because it is the part of the float discipline that is easiest to
    get subtly wrong and hardest to see in a rendered chart: a tick chosen in float space and then
    converted back would reintroduce exactly the round trip this module exists to avoid.

    The axis includes zero whenever the data is one-signed, because a bar chart whose baseline is
    not zero exaggerates every difference on it — the most common way a truthful chart misleads.
    """
    low = min(values, key=_decimal_key)
    high = max(values, key=_decimal_key)
    if low > 0:
        low = Decimal(0)
    if high < 0:
        high = Decimal(0)
    high = high * Y_HEADROOM if high > 0 else high
    low = low * Y_HEADROOM if low < 0 else low

    span = high - low
    if span == 0:
        return (Decimal(0), Decimal(1))

    # `key=identity` on a two-integer max, per convention 10. The rule is blunt — it fails any
    # keyless reduction because it cannot tell which are safe — and `normalize/tags.py` established
    # the idiom: state the claim that the order is total rather than omit it.
    step = _nice_step(span / Decimal(max((target - 1, 1), key=_int_key)))
    first = _floor_to(low, step)
    last = _ceil_to(high, step)

    ticks: list[Decimal] = []
    current = first
    while current <= last:
        ticks.append(current)
        current = current + step
    return tuple(ticks)


def _decimal_key(value: Decimal) -> Decimal:
    """Convention 10 again: ``min``/``max`` over a total order, said out loud."""
    return value


def _int_key(value: int) -> int:
    return value


def _nice_step(raw: Decimal) -> Decimal:
    """The smallest of 1, 2, 2.5, 5, 10 × 10ⁿ that is at least ``raw``."""
    magnitude = Decimal(1).scaleb(raw.adjusted())
    normalized = raw / magnitude
    for candidate in _NICE_STEPS:
        if normalized <= candidate:
            return candidate * magnitude
    return _NICE_STEPS[-1] * magnitude


def _floor_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step

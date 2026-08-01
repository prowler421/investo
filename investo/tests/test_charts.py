"""``report/charts.py`` — the one float in the package, and what constrains it (ROADMAP M3).

Three things are tested here that no other file can test: that tick *labels* come from the
``Decimal`` rather than from the plotted float, that a chart below the point threshold states a
reason instead of drawing an empty axes, and that one chart's bytes are stable across two builds.

The third is deliberately narrower than the end-to-end determinism gate. A single failing
end-to-end assertion tells you the report is nondeterministic and nothing else, and the first thing
anyone does is start bisecting matplotlib against WeasyPrint by hand.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from investo.domain.periods import FiscalPeriod, PeriodKind
from investo.domain.provenance import Derivation
from investo.report import charts
from investo.report.charts import ChartKind, ChartSpec, Datum, PlotSeries, axis_ticks, build

UNREPRESENTABLE = Decimal("391035000000.01")


def _points(*values: str, start_year: int = 2021) -> tuple[Datum, ...]:
    return tuple(
        Datum(
            value=Decimal(text),
            period=FiscalPeriod(
                start=date(start_year + index, 1, 1),
                end=date(start_year + index, 12, 31),
                kind=PeriodKind.ANNUAL,
            ),
            source=Derivation(rule="test", inputs=()),
        )
        for index, text in enumerate(values)
    )


def _spec(**overrides: object) -> ChartSpec:
    base = {"chart_id": "test", "title": "Test chart", "kind": ChartKind.BARS}
    return ChartSpec(**{**base, **overrides})  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# The float discipline
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_an_unrepresentable_value_labels_exactly() -> None:
    """CLAUDE.md convention 13, asserted behaviourally rather than structurally.

    The AST rules in ``test_layering`` say *where* a ``float()`` may appear. They cannot say that
    the result never becomes text, and a correct-looking implementation that let matplotlib format
    its own ticks would satisfy every one of them.

    ``391035000000.01`` is chosen because it has no exact binary representation. The tick label the
    chart prints is derived from the ``Decimal``, so the digits are the filer's.
    """
    image = build(
        _spec(chart_id="unrepresentable"),
        (PlotSeries(label="Revenue", points=_points(str(UNREPRESENTABLE), "383285000000")),),
    )
    assert image.drawn
    assert all("0100097" not in label for label in image.labels), image.labels


@pytest.mark.spec
def test_coord_is_the_only_conversion_and_it_round_trips_positionally() -> None:
    """The conversion is allowed to lose precision. It is not allowed to lose *magnitude*."""
    converted = charts.coord(UNREPRESENTABLE)
    assert abs(converted - 391035000000.0) < 1.0


# ---------------------------------------------------------------------------
# Tick selection, in Decimal
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_ticks_are_decimals_and_include_zero_for_one_signed_data() -> None:
    """A bar chart whose baseline is not zero exaggerates every difference on it.

    The most common way a truthful chart misleads, so the axis includes zero whenever the data does
    not straddle it.
    """
    ticks = axis_ticks([Decimal("90"), Decimal("100")])
    assert all(isinstance(tick, Decimal) for tick in ticks)
    assert Decimal(0) in ticks
    assert ticks[-1] >= Decimal("100")


def test_ticks_span_negative_data_and_keep_zero() -> None:
    ticks = axis_ticks([Decimal("-40"), Decimal("100")])
    assert ticks[0] <= Decimal("-40")
    assert ticks[-1] >= Decimal("100")
    assert Decimal(0) in ticks


def test_ticks_are_round_numbers() -> None:
    """1, 2, 2.5, 5 or 10 × 10ⁿ. A tick of 37,142 is a tick nobody reads off a chart."""
    ticks = axis_ticks([Decimal(0), Decimal("371428")])
    step = ticks[1] - ticks[0]
    mantissa = step.scaleb(-step.adjusted())
    assert mantissa in {Decimal(1), Decimal(2), Decimal("2.5"), Decimal(5)}, step


def test_a_flat_series_still_produces_an_axis() -> None:
    """Zero span is a real input — a filer with two identical years — and ``0/0`` is not."""
    assert axis_ticks([Decimal(0), Decimal(0)]) == (Decimal(0), Decimal(1))


# ---------------------------------------------------------------------------
# Absence
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_one_point_is_not_a_chart() -> None:
    """A single floating bar reads as a trend; an empty axes reads as a bug.

    ``MIN_POINTS`` is 2 and the slot states why instead — §6.10's *"a blank space with an
    explanation beats a confident wrong number"*, applied to a picture.
    """
    image = build(_spec(chart_id="thin"), (PlotSeries(label="Revenue", points=_points("100")),))
    assert not image.drawn
    assert image.payload == b""
    assert image.omitted is not None
    assert "Caveats" in image.omitted


@pytest.mark.spec
def test_a_supplied_reason_wins_over_the_point_count() -> None:
    """ "No operating income, because banks do not report one" and "only one year of data" both
    produce no chart and mean different things. Only the caller knows which."""
    reason = "Margins not charted — this filer reports no operating income."
    image = build(_spec(chart_id="bank"), (PlotSeries(label="x", points=()),), reason=reason)
    assert image.omitted == reason


def test_an_omitted_chart_refuses_to_produce_a_data_uri() -> None:
    """An ``<img>`` with an empty ``src`` renders as a broken-image glyph.

    The template is supposed to branch on ``drawn`` first, and raising is how it finds out that it
    did not — rather than shipping a PDF with five broken-image icons in it.
    """
    image = build(_spec(chart_id="thin"), (PlotSeries(label="Revenue", points=_points("100")),))
    with pytest.raises(ValueError, match="omitted"):
        _ = image.data_uri


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_a_chart_reports_every_source_it_drew() -> None:
    """What makes "every number traceable" a test rather than a promise about care."""
    series = PlotSeries(label="Revenue", points=_points("100", "110", "120"))
    image = build(_spec(chart_id="sources"), (series,))
    assert len(image.sources) == 3
    assert set(image.sources) == {point.source for point in series.points}


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_chart_bytes_are_stable_across_two_builds() -> None:
    """The narrow half of §11's gate, so a failure names the layer.

    ``metadata={"Software": None}`` is what makes this hold across matplotlib patch releases — the
    default PNG writer embeds its own version, which §9.0 does not mention because §9.0 was written
    about SVG.
    """
    series = (PlotSeries(label="Revenue", points=_points("100", "140", "180")),)
    first = build(_spec(chart_id="determinism"), series)
    second = build(_spec(chart_id="determinism"), series)
    assert first.payload == second.payload
    assert first.payload != b""


@pytest.mark.spec
def test_the_png_carries_no_matplotlib_version() -> None:
    """The assertion behind the test above, stated so a failure is diagnosable.

    Without it, a version bump breaks byte-identity and the report reads "the renderer is
    nondeterministic" — which sends the reader to the serializer.
    """
    image = build(
        _spec(chart_id="metadata"), (PlotSeries(label="Revenue", points=_points("100", "140")),)
    )
    assert b"matplotlib" not in image.payload.lower()


# ---------------------------------------------------------------------------
# The palette is written twice, so it is asserted once
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_the_palette_agrees_with_the_stylesheet() -> None:
    """A hex code written twice is a hex code that drifts.

    The chart series and the CSS rules that frame them have to be the same colour, and there is no
    way to share the value — matplotlib does not read CSS custom properties. So the duplication is
    accepted and asserted.
    """
    from investo.report.render import stylesheet_text

    css = stylesheet_text()
    for name, value in (
        ("--series-primary", charts.SERIES_PRIMARY),
        ("--series-secondary", charts.SERIES_SECONDARY),
        ("--series-tertiary", charts.SERIES_TERTIARY),
        ("--series-muted", charts.SERIES_MUTED),
        ("--rule", charts.GRID_GREY),
    ):
        assert f"{name}: {value};" in css, f"{name} disagrees with charts.py"

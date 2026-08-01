"""``report/model.py`` — the derived series and the traceability guarantee (ROADMAP M3).

The load-bearing test is :func:`test_every_rendered_source_is_in_the_appendix`, which is ROADMAP
M3's second exit criterion — *"every number traceable to a `SourceRef` in the appendix"* — turned
into an assertion instead of a claim about how carefully the templates were written.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from investo.config import Settings
from investo.domain.models import Metric
from investo.domain.provenance import Derivation, Provenance, SourceRef
from investo.normalize.statements import Bucket, FinancialHistory
from investo.report.model import (
    ABSENT_SECTIONS,
    NOT_ASSESSED,
    build_model,
    free_cash_flow,
    margin_series,
    yoy_series,
)
from investo.report.serialize import RunInfo, run_info, serialize
from tests.conftest import M2_WINDOW, VALID_USER_AGENT, filing_rows, history, submissions


def _run(name: str = "AAPL") -> RunInfo:
    settings = Settings(sec_user_agent=VALID_USER_AGENT, tiingo_key="k")
    return run_info(
        settings,
        ticker=name,
        as_of=date(2026, 6, 30),
        window=M2_WINDOW,
        lookback_years=5,
        manifest_hash="0" * 64,
        version="0.1.0",
    )


def _aapl() -> FinancialHistory:
    profile, filings = submissions("AAPL.json", cik=320193)
    return history("AAPL.trimmed.json", ticker="AAPL", cik=320193, profile=profile, filings=filings)


# ---------------------------------------------------------------------------
# The exit criterion
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_every_rendered_source_is_in_the_appendix() -> None:
    """ROADMAP M3 exit criterion 2, as a subset assertion over two independent walks.

    ``report/model.py`` collects the sources behind everything it rendered; ``report.json`` interns
    every distinct ref it found. The first must be covered by the second. Independent walks rather
    than one shared one, because a single walk would make this vacuous — it would be comparing a
    value against itself.
    """
    subject = _aapl()
    model = build_model(subject, _run())
    document = json.loads(serialize(subject, run=_run()))

    interned = {
        (entry["accession"], entry["taxonomy"], entry["tag"]) for entry in document["sources"]
    }
    assert interned, "the fixture produced no sources; the assertion below would be vacuous"

    for source in model.sources_used:
        for ref in _leaves(source):
            key = (ref.accession.value, ref.taxonomy, ref.tag)
            assert key in interned, f"{key} is rendered but absent from the appendix"


def _leaves(source: Provenance) -> tuple[SourceRef, ...]:
    """Every leaf ref under a provenance tree — the same walk ``serialize`` does.

    Typed rather than left at ``object``: the assertion below reads ``ref.accession``, and a walk
    that returns ``object`` makes that unreachable to the type checker *and* to a reader trying to
    work out what the test compares.
    """
    return source.refs() if isinstance(source, Derivation) else (source,)


@pytest.mark.spec
def test_the_appendix_indices_match_report_json() -> None:
    """The appendix prints an index and says it matches ``report.json``'s array. It has to.

    Both come from ``serialize.intern_sources``, so this asserts the single-walk property rather
    than re-deriving it — two walks would be two definitions of "distinct", in the one table whose
    whole job is being checkable by hand.
    """
    subject = _aapl()
    model = build_model(subject, _run())
    document = json.loads(serialize(subject, run=_run()))
    rendered = [row.cells[0] for row in model.appendix.sources.rows]
    assert rendered == [entry["accession"] for entry in document["sources"]]


# ---------------------------------------------------------------------------
# Derived series
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_free_cash_flow_subtracts_capex_and_names_both_inputs() -> None:
    """Asserted as a *derivation*, not a value — CLAUDE.md's rule, and the fixtures are synthetic.

    A hard-coded expected number would pass under the opposite sign rule for any filer whose capex
    happened to be tagged negative, which is exactly the trap ``normalize/tags.py``'s sign
    convention exists to close.
    """
    subject = _aapl()
    ocf = subject.series(Metric.OPERATING_CASH_FLOW, Bucket.ANNUAL)
    cash = {f.period.end: f.value for f in ocf}
    capex = {f.period.end: f.value for f in subject.series(Metric.CAPEX, Bucket.ANNUAL)}
    points = free_cash_flow(subject, Bucket.ANNUAL)

    assert points, "the fixture has no overlapping OCF/capex years; the assertion is vacuous"
    for point in points:
        end = point.period.end
        assert point.value == cash[end] - capex[end]
        assert isinstance(point.source, Derivation)
        assert point.source.rule == "free_cash_flow"
        assert len(point.source.inputs) == 2


@pytest.mark.spec
def test_a_period_missing_either_input_is_dropped_not_interpolated() -> None:
    """Carrying the previous year forward draws a flat segment that reads as stability."""
    subject = _aapl()
    cash = {f.period.end for f in subject.series(Metric.OPERATING_CASH_FLOW, Bucket.ANNUAL)}
    capex = {f.period.end for f in subject.series(Metric.CAPEX, Bucket.ANNUAL)}
    ends = {point.period.end for point in free_cash_flow(subject, Bucket.ANNUAL)}
    assert ends == cash & capex


@pytest.mark.spec
def test_a_margin_against_zero_revenue_is_dropped() -> None:
    """Undefined, not infinite — and a pre-revenue filer is a real one, not an edge case."""
    subject = history("IPO.trimmed.json", ticker="IPO")
    for point in margin_series(subject, Metric.NET_INCOME, Bucket.ANNUAL):
        assert point.value.is_finite()


@pytest.mark.spec
def test_yoy_skips_a_non_consecutive_pair() -> None:
    """A two-year gap charted as one year's growth overstates it by roughly the square.

    The gap shows on the chart as a missing point, which is the honest rendering — and the test is
    on the *rule* rather than on a fixture, because no fixture currently has a gap year.
    """
    from investo.domain.models import Fact
    from investo.domain.periods import FiscalPeriod, PeriodKind

    def fact(year: int, value: str) -> Fact:
        return Fact(
            metric=Metric.REVENUE,
            value=Decimal(value),
            period=FiscalPeriod(
                start=date(year, 1, 1), end=date(year, 12, 31), kind=PeriodKind.ANNUAL
            ),
            source=Derivation(rule="test", inputs=()),
            unit="USD",
        )

    contiguous = yoy_series([fact(2022, "100"), fact(2023, "110")])
    assert len(contiguous) == 1
    assert contiguous[0].value == Decimal("110") / Decimal("100") - 1

    gapped = yoy_series([fact(2022, "100"), fact(2025, "133")])
    assert gapped == ()


# ---------------------------------------------------------------------------
# The cover
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_no_partial_verdict_or_confidence_is_printed() -> None:
    """Three of the confidence rating's five inputs exist, which is what makes a partial dangerous.

    A number built from three of five renders on the same 0–100 scale as the real one. The cover
    prints a *measurement* with its denominator instead.
    """
    model = build_model(_aapl(), _run())
    assert model.cover.verdict == NOT_ASSESSED
    assert model.cover.confidence == NOT_ASSESSED
    assert "M5" in model.cover.verdict_note
    assert "coverage" in model.cover.coverage_line.lower()


@pytest.mark.spec
def test_the_cover_labels_an_observed_denominator() -> None:
    """A 100% figure that quietly came from a circular denominator is the most misleading number
    this pipeline can produce — ``normalize/statements.py``'s words, on the page it reaches."""
    subject = history("NOPERIODIC.trimmed.json", ticker="NOPER")
    model = build_model(subject, _run("NOPER"))
    assert "observed" in model.cover.coverage_line
    assert model.caveats.spine_warning != ""


@pytest.mark.spec
def test_the_disclaimer_is_on_the_cover_in_full() -> None:
    """§10 requires it prominent. A truncated disclaimer is not a shorter disclaimer."""
    model = build_model(_aapl(), _run())
    assert "not investment advice" in model.cover.disclaimer.lower()
    assert len(model.cover.disclaimer) > 200


# ---------------------------------------------------------------------------
# Absences
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_the_absent_milestones_are_listed_on_a_complete_run() -> None:
    """A reader who does not know what is missing cannot calibrate what is present.

    Listed on *every* run, including a perfect one — the most likely misreading of an M3 report is
    that the tool has no opinion on valuation rather than that the opinion has not been built.
    """
    model = build_model(_aapl(), _run())
    assert model.caveats.absent_sections == ABSENT_SECTIONS
    milestones = {milestone for _, milestone in model.caveats.absent_sections}
    assert {"M4", "M4.5", "M5", "M6", "M7"} <= milestones


@pytest.mark.spec
def test_a_bank_gets_a_recorded_gate_not_a_refusal() -> None:
    """§6.10's gate suppresses a *valuation*, and there is none at M3.

    So it is information rather than consequence, and the wording says so. M2 recorded it and made
    no refusal; a refusal here would be the second copy of a rule M4 and M5 own.
    """
    profile, filings = submissions("ARXS.json")
    subject = history("BANK.trimmed.json", ticker="BANK", profile=profile, filings=filings)
    model = build_model(subject, _run("BANK"))
    if subject.sic is not None and 6000 <= subject.sic <= 6499:
        assert any("6.10" in notice or "REIT" in notice for notice in model.caveats.gates)
    # Whatever the fixture's SIC, no gate ever becomes an exception or an empty section.
    assert isinstance(model.caveats.gates, tuple)


@pytest.mark.spec
def test_a_filer_with_no_companyfacts_still_produces_a_model() -> None:
    """§6.10 again: *"a blank space with an explanation beats a confident wrong number"*, and a
    traceback is neither."""
    subject = history(
        None, ticker="EMPTY", filings=filing_rows(("10-K", "2024-02-01", "2023-12-31"))
    )
    model = build_model(subject, _run("EMPTY"))
    assert model.cover.ticker == "EMPTY"
    assert all(not image.drawn for image in model.history.charts)
    assert model.caveats.findings


# ---------------------------------------------------------------------------
# --brief
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_brief_changes_no_value() -> None:
    """``--brief`` selects a template, not a data path.

    A brief report that took a different data path could disagree with the full one about a figure,
    and the disagreement would be visible only to someone who ran both.
    """
    subject = _aapl()
    full = build_model(subject, _run())
    brief = build_model(subject, _run(), brief=True)
    assert brief.brief is True
    assert brief.cover == full.cover
    assert brief.snapshot == full.snapshot
    assert brief.appendix == full.appendix

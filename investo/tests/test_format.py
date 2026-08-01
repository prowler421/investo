"""``report/format.py`` — the only place a number becomes text (ROADMAP M3).

The module carries a guarantee that nothing else can: **no printed figure has been through a
float.** These tests are what makes that checkable, and the load-bearing one is
:func:`test_an_unrepresentable_value_survives_exactly` — every other assertion here would pass over
an implementation that formatted a ``float``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from investo.report import format as fmt

UNREPRESENTABLE = Decimal("391035000000.01")
"""The value the AAPL fixture carries specifically to catch a float round trip.

``float("391035000000.01")`` is ``391035000000.010009765625``. Any formatter that goes through a
double prints the second, and every other test in this file passes either way.
"""


# ---------------------------------------------------------------------------
# The guarantee
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_an_unrepresentable_value_survives_exactly() -> None:
    """CLAUDE.md convention 13: the float positions a mark, it never produces a printed figure."""
    assert fmt.exact(UNREPRESENTABLE) == "391035000000.01"
    assert "0100097" not in fmt.exact(UNREPRESENTABLE)


@pytest.mark.spec
def test_exact_does_not_normalize_the_filed_representation() -> None:
    """``Decimal("1E+2")`` and ``Decimal("100")`` are equal and print differently.

    ``str`` rather than ``normalize()`` deliberately: normalizing discards the significant figures
    the filer chose, and ``report/serialize.py`` makes the same call, so ``report.json`` and the
    appendix show one string for one fact.
    """
    assert fmt.exact(Decimal("1E+2")) == "1E+2"
    assert fmt.exact(Decimal("100.00")) == "100.00"


# ---------------------------------------------------------------------------
# Absence is a value
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize(
    "renderer", [fmt.money, fmt.exact, fmt.per_share, fmt.shares], ids=lambda f: f.__name__
)
def test_absent_money_is_a_dash_not_a_blank(renderer: object) -> None:
    """A blank cell is indistinguishable from a rendering bug — ``facts.py``'s rule, on a page."""
    assert renderer(None) == fmt.ABSENT  # pyright: ignore[reportCallIssue]
    assert fmt.ABSENT.strip() != ""


@pytest.mark.spec
def test_an_undefined_rate_is_not_zero() -> None:
    """``n/a`` and ``0.0%`` are different claims, and only one of them is true of no denominator."""
    assert fmt.percent(None) == fmt.NOT_APPLICABLE
    assert fmt.ratio(None) == fmt.NOT_APPLICABLE
    assert fmt.percent(Decimal(0)) == "0.0%"
    assert fmt.percent(None) != fmt.percent(Decimal(0))


# ---------------------------------------------------------------------------
# The unit decides the format
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_a_per_share_figure_is_not_scaled_to_millions() -> None:
    """Rounding EPS to millions prints every filer's earnings as ``0``.

    A plausible number, and wrong for all of them — which is why the *unit* decides the format and
    never the metric.
    """
    assert fmt.per_share(Decimal("6.13")) == "6.13"
    assert fmt.money(Decimal("6.13")) == "0"


def test_a_share_count_is_suffixed() -> None:
    """An unsuffixed 15,744 in a column of dollars reads as $15.7bn — three orders out."""
    assert fmt.shares(Decimal("15744000000")) == "15,744 M sh"


def test_money_scales_and_groups() -> None:
    assert fmt.money(UNREPRESENTABLE) == "391,035"
    assert fmt.money(UNREPRESENTABLE, scale=fmt.Scale.BILLIONS) == "391"
    assert fmt.money(UNREPRESENTABLE, scale=fmt.Scale.UNITS) == "391,035,000,000"


# ---------------------------------------------------------------------------
# Signs
# ---------------------------------------------------------------------------
def test_a_negative_uses_the_typographic_minus() -> None:
    """A hyphen is narrower than a digit, so a right-aligned column of figures looks misaligned."""
    assert fmt.money(Decimal("-4200000000")).startswith("−")
    assert "-" not in fmt.money(Decimal("-4200000000"))


def test_exact_keeps_ascii_because_its_output_gets_pasted() -> None:
    """The appendix figure is for searching EDGAR with, and U+2212 does not match."""
    assert fmt.exact(Decimal("-4200000000")) == "-4200000000"


def test_signed_percent_marks_the_positive_case() -> None:
    assert fmt.signed_percent(Decimal("0.082")) == "+8.2%"
    assert fmt.signed_percent(Decimal("-0.031")) == "−3.1%"
    assert fmt.signed_percent(Decimal(0)) == "0.0%"


# ---------------------------------------------------------------------------
# Agreement with the other renderer
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_agrees_with_the_facts_table() -> None:
    """``investo facts`` and the PDF must print the same digits for the same fact.

    Two renderers with two implementations — ``facts.py`` predates this module and keeps its own
    table — so the duplication is real, and this is what stops it drifting. Asserted through the
    *public* rendering of each rather than by importing one's private cell formatter into the
    other's tests: a test that reaches for a private name asserts an implementation detail, and this
    property is about what the two commands print.
    """
    from investo.domain.models import Metric
    from investo.facts import render_facts
    from investo.normalize.statements import Bucket
    from tests.conftest import history, submissions

    profile, filings = submissions("AAPL.json", cik=320193)
    subject = history(
        "AAPL.trimmed.json", ticker="AAPL", cik=320193, profile=profile, filings=filings
    )
    table = render_facts(subject)

    revenue = subject.series(Metric.REVENUE, Bucket.ANNUAL)
    assert revenue, "the AAPL fixture has no annual revenue; the assertion below is vacuous"
    for fact in revenue:
        assert fmt.money(fact.value) in table, (
            f"the PDF would print {fmt.money(fact.value)} where the facts table does not"
        )

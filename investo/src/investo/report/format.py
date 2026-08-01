"""``Decimal`` → ``str``. The only place in the package where a number becomes text (ROADMAP M3).

Everything a reader can read in the PDF passes through this module: table cells, chart axis tick
labels, the cover's coverage figure, the caveats percentages. That is the mechanism behind
CLAUDE.md convention 13 — ``report/charts.py`` is allowed to convert a ``Decimal`` to a ``float``
because a plotted coordinate is a *position*, and the reason that is safe is that the float never
comes back out as text. It cannot, because the only function that produces text takes a ``Decimal``
and this module contains no ``float`` at all.

Two further consequences worth stating, because both are guarantees that come from the module
existing rather than from anything it does:

**The appendix cannot disagree with an axis label**, since both call the same function on the same
``Decimal``. Two formatters is how a chart says ``391,035`` and a table says ``391,034.99``.

**And ``investo facts`` and the PDF print the same digits.** The rules below are ``facts.py``'s,
restated rather than reimplemented: money in millions, the *unit* deciding the format rather than
the metric, ``—`` for an absent value and ``n/a`` for an undefined rate. ``facts.py`` predates this
module and keeps its own table rendering; what is shared is the reasoning, and
``test_format::test_agrees_with_the_facts_table`` asserts the outputs agree on the fixture set so
the duplication cannot drift silently.

Rounding is ``ROUND_HALF_EVEN``, passed explicitly on every ``quantize``. The ambient decimal
context is process-global and any library in the process can change it; a report whose figures
depend on that is a report that changes for reasons nothing in this repository can see.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Final

__all__ = [
    "ABSENT",
    "NOT_APPLICABLE",
    "Scale",
    "money",
    "exact",
    "per_share",
    "shares",
    "percent",
    "signed_percent",
    "ratio",
    "count",
    "iso_date",
]

ABSENT: Final = "—"
"""An em dash. A **value**, not a gap.

``facts.py``'s argument, and it applies harder to a typeset page than to a terminal: *"a blank row
is indistinguishable from a rendering bug."* Every absent figure in the report renders as this, and
the template branches on an explicit field rather than on truthiness — ``{% if value %}`` is false
for a legitimately zero figure.
"""

NOT_APPLICABLE: Final = "n/a"
"""For a rate whose denominator was zero. **Never ``0.0%``**, which is a different claim.

A metric with nothing expected of it and a metric expected everywhere and filled nowhere are
opposite situations, and printing ``0.0%`` for the first one asserts the second.
"""

_MILLION: Final = Decimal(1_000_000)
_BILLION: Final = Decimal(1_000_000_000)
_HUNDRED: Final = Decimal(100)

_WHOLE: Final = Decimal("1")
_TWO_PLACES: Final = Decimal("0.01")
_ONE_PLACE: Final = Decimal("0.1")

_MINUS: Final = "−"
"""U+2212 MINUS SIGN, not ASCII hyphen-minus.

A typographic decision that is load-bearing exactly once: a column of right-aligned figures in
which the negatives are a hyphen narrower than the positives reads as misaligned, and the reader
concludes the table is broken rather than that the font is. Applied only to *displayed* figures —
:func:`exact` keeps ASCII, because its output is meant to be pasted into a search box.
"""


class Scale(StrEnum):
    """How a money figure is divided before printing, and what suffix says so."""

    UNITS = "units"
    MILLIONS = "millions"
    BILLIONS = "billions"


_DIVISOR: Final = {Scale.UNITS: Decimal(1), Scale.MILLIONS: _MILLION, Scale.BILLIONS: _BILLION}


def money(value: Decimal | None, *, scale: Scale = Scale.MILLIONS) -> str:
    """A money figure, scaled and grouped. ``391,035`` for Apple's FY2025 revenue in millions.

    Millions by default because that is the unit every financial statement in the report is read in,
    and because it is what ``investo facts`` prints — the two have to agree digit for digit or the
    appendix and the terminal become two sources of truth.

    The scaling happens **here, at the renderer**, over a value that is exact ``Decimal`` all the way
    from the filing. Nothing upstream rescales: ``companyfacts`` values are already in the unit they
    name, and a scaling step in the pipeline would be a second place for a factor-of-1000 error to
    live — one that a chart and a table could disagree about.
    """
    if value is None:
        return ABSENT
    scaled = (value / _DIVISOR[scale]).quantize(_WHOLE, rounding=ROUND_HALF_EVEN)
    return _grouped(scaled)


def exact(value: Decimal | None) -> str:
    """The value as filed, unrounded and unscaled: ``391035000000.01``.

    The appendix uses this and the body does not. ``money`` rounds for readability, which is right
    for a page someone reads and wrong for a page someone *checks* — and checking is what the
    appendix is for. A reader comparing a figure against EDGAR needs the digits the filer filed.

    ``str(Decimal)`` deliberately, with no normalization: ``Decimal("1E+2")`` and ``Decimal("100")``
    are equal and print differently, and normalizing would discard the significant figures the filer
    chose. ``report/serialize.py`` makes the same call for the same reason, so ``report.json`` and
    the appendix show one string.

    ASCII hyphen for a negative, unlike every other function here — this output is for pasting into
    a search field, and U+2212 does not match.
    """
    return ABSENT if value is None else str(value)


def per_share(value: Decimal | None) -> str:
    """Two decimal places: ``6.13``.

    The *unit* decides this, never the metric. Rounding a per-share figure to millions prints every
    filer's earnings as ``0``, which is a plausible-looking number and is wrong for all of them.
    """
    if value is None:
        return ABSENT
    return _sign(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN))


def shares(value: Decimal | None) -> str:
    """A share count in millions, suffixed: ``15,744 M sh``.

    Suffixed because an unsuffixed ``15,744`` in a column of dollar figures reads as $15.7bn, and
    the two differ by three orders of magnitude in a document about valuation.
    """
    if value is None:
        return ABSENT
    scaled = (value / _MILLION).quantize(_WHOLE, rounding=ROUND_HALF_EVEN)
    return f"{_grouped(scaled)} M sh"


def percent(rate: Decimal | None, *, places: int = 1) -> str:
    """A rate given as a fraction, printed as a percentage: ``0.929`` → ``92.9%``.

    ``None`` is :data:`NOT_APPLICABLE`. The input is a fraction rather than an
    already-multiplied percentage because that is what ``MetricCoverage.fill_rate`` and every margin
    in ``report/model.py`` produce, and a function that accepted either would eventually be handed
    the wrong one — ``0.929`` and ``92.9`` are both plausible inputs and only one is right.
    """
    if rate is None:
        return NOT_APPLICABLE
    quantum = _ONE_PLACE if places == 1 else Decimal(1).scaleb(-places)
    return f"{_sign((rate * _HUNDRED).quantize(quantum, rounding=ROUND_HALF_EVEN))}%"


def signed_percent(rate: Decimal | None, *, places: int = 1) -> str:
    """As :func:`percent`, with an explicit ``+`` on positives: ``+8.2%``, ``−3.1%``.

    For year-over-year growth, where the sign is the message. An unsigned ``8.2%`` next to a
    ``−3.1%`` makes the reader supply the plus, and readers supply it inconsistently when scanning.
    """
    if rate is None:
        return NOT_APPLICABLE
    text = percent(rate, places=places)
    return f"+{text}" if rate > 0 else text


def ratio(value: Decimal | None) -> str:
    """A multiple: ``1.42x``. ``None`` is :data:`NOT_APPLICABLE`."""
    if value is None:
        return NOT_APPLICABLE
    return f"{_sign(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN))}x"


def count(value: int | None) -> str:
    """A whole number of things — periods, findings, pages. ``None`` is :data:`ABSENT`."""
    return ABSENT if value is None else f"{value:,}"


def iso_date(value: date | None) -> str:
    """``2026-08-01``. ``None`` is :data:`ABSENT`.

    ISO everywhere, never a localized format. The report is read by whoever generated it and by a
    tool comparing two of them, and ``08/01/2026`` means two different days depending on the reader.

    Typed ``date`` rather than a duck-typed ``object`` with a ``getattr(value, "isoformat")`` probe.
    The probe was the first draft and it does not type-check — ``getattr`` on ``object`` returns
    ``object``, so the return type widens to ``object | str`` — but the reason to prefer this
    signature is not the type checker. Every caller passes a ``date``, and ``datetime`` is a
    subclass, so accepting anything with the right method name only bought the ability to be handed
    something that formats differently and say nothing about it.
    """
    return ABSENT if value is None else value.isoformat()


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _grouped(value: Decimal) -> str:
    """Thousands separators, with the typographic minus."""
    return _sign(value, text=f"{abs(value):,}")


def _sign(value: Decimal, *, text: str | None = None) -> str:
    """``text`` (or ``value``) prefixed with U+2212 when negative.

    Formatting ``abs(value)`` and prefixing, rather than replacing ``-`` in the output, because a
    string replace would also rewrite a hyphen inside a value that legitimately contains one — and
    the next caller to pass a date range through here would not find out.
    """
    body = text if text is not None else str(abs(value))
    return f"{_MINUS}{body}" if value < 0 else body

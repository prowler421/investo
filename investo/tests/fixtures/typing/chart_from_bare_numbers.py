"""CLAUDE.md convention 16: a chart takes traced points, never numbers.

There is no overload of `PlotSeries` accepting bare coordinates, so a caller holding a number with
no provenance cannot draw it. That is a *signature* rather than a check, which means the guarantee
has no runtime violation to attempt — the attempt does not compile, and this file is it.

The failure it prevents is M4 plotting a peer median with no source attached. Nothing at runtime
would notice: the chart would render, the numbers would be plausible, and the appendix would not
mention them.
"""

from decimal import Decimal

from investo.report.charts import PlotSeries

_ = PlotSeries(
    label="Revenue",
    # The marker sits on the argument, not on the call. basedpyright attributes an argument-type
    # error to the argument's own line, and `test_typing` asserts the diagnostic lands on the
    # marked line — so a marker one line off fails the test for a reason that has nothing to do
    # with the guarantee.
    points=(Decimal("391035000000.01"), Decimal("383285000000")),  # ERROR: Decimal is not a Datum
)

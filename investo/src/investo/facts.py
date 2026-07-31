"""``investo facts TICKER`` — print the normalized statements and the coverage report (ROADMAP M2).

The body lives here rather than in ``cli.py`` for the reason :mod:`~investo.fetch` gives: ``cli.py``
stays the declared flag surface and nothing else, because ``tests/test_cli_surface.py`` reads that file
against README § Usage in both directions and orchestration in the middle would make the check harder
to trust rather than easier.

**One flag is new: ``--json``**, which writes DESIGN.md §4.5's ``report.json`` document to stdout.
Raised as ``docs/m2/README.md`` § Spec question 4 and accepted. Without it M2 would ship a serializer no
command emits and §11's determinism gate would have no end-to-end path until M3. It goes to stdout
rather than acquiring an ``--out``: ``facts`` writes no files, it composes with redirection, and
``docs/m1/README.md`` §3 already declined ``--json`` on ``fetch`` on the grounds that *"``report.json``
(M2) is the machine-readable surface this project already committed to"* — which is an argument for
putting it on this command.

**``facts`` never exits 3**, and thin coverage is exactly the situation that invites it. Exit 3 is
"insufficient data, **report still written**" (§14); ``facts`` writes no report, so returning 3 would
promise an artifact that does not exist. Every absence here — no ``companyfacts`` for the CIK, a metric
that resolves to nothing in every period, fewer than twelve quarters, a bank's missing operating income
— is printed and exits 0. That extends M1's rule (*"a 404 and a missing tag are absences, not failures;
the command that needs the data decides whether the absence is fatal"*) one command further: ``facts``
never needs the data, it reports on it.

Four properties of the table are load-bearing rather than cosmetic:

- **``absent`` is a value, not a gap.** A metric with no data prints ``—`` on every period and
  ``absent`` in the tag column, because a blank row is indistinguishable from a rendering bug.
- **The tag is printed next to the series**, because which tag won is the thing this command exists to
  let you check, and it is what §9.1's appendix promises.
- **Coverage prints its spine origin.** A percentage against an ``OBSERVED`` denominator is close to
  meaningless and must never be printed without saying so.
- **Findings are printed in full, not counted.** "3 findings" is a number nobody acts on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

from investo.config import Settings, parse_lookback
from investo.domain.models import Fact, Metric
from investo.domain.periods import window as lookback_window
from investo.domain.provenance import Derivation
from investo.fetch import run_fetch
from investo.normalize.statements import Bucket, FinancialHistory, build_history
from investo.normalize.tags import CHAINS, Tier, metrics_in_tier
from investo.report.serialize import RunInfo, run_info, serialize

__all__ = ["run_facts", "render_facts", "render_json"]

_SCALE: Final = Decimal(1_000_000)
"""Money is printed in millions.

A presentation choice made **once, at the renderer**, over values that are exact ``Decimal``
throughout. Nothing upstream rescales — ``companyfacts`` values are already in the unit named, and a
scaling step in the pipeline would be a second place for a factor-of-1000 error to live.
"""

_SHARE_UNITS: Final = frozenset({"shares"})
_PER_SHARE_UNITS: Final = frozenset({"USD/shares"})


def run_facts(
    ticker: str,
    *,
    settings: Settings,
    refresh: bool = False,
    as_of: date | None = None,
    version: str = "0.1.0",
) -> tuple[FinancialHistory, RunInfo]:
    """Fetch, normalize, and return the history plus the run envelope.

    ``run_fetch`` followed by ``build_history`` — the same wiring ``fetch`` already uses, with no second
    fetch path to keep in sync.

    **The clock is read at the caller and nowhere below it.** ``as_of`` arrives resolved; when it is
    ``None`` this function substitutes ``date.today()`` *once*, immediately, and threads the result
    down. Nothing under ``normalize/`` or ``report/`` may read a clock — a default resolved deeper makes
    two runs either side of midnight differ, which §11's determinism gate reports as nondeterminism
    rather than as the design mistake it is.

    Args:
        ticker: NASDAQ symbol.
        settings: Resolved configuration.
        refresh: Bypass the cache read.
        as_of: Point-in-time date, already validated as not in the future by ``cli._resolve_as_of``.
        version: Package version, for ``report.json``'s ``generated_by``.

    Raises:
        TickerNotFoundError: absent from SEC's ticker file, or not NASDAQ. Exit 2.
        UpstreamFetchError: a malformed payload, or retries exhausted. Exit 4.
        ConfigError: an unusable cache, a bad lookback, or a price provider whose key is missing.
            Exit 5.
    """
    years = parse_lookback(settings.lookback)
    resolved = as_of if as_of is not None else date.today()
    span = lookback_window(years, as_of=resolved)

    result = run_fetch(ticker, settings=settings, refresh=refresh, as_of=resolved)
    history = build_history(
        result.facts,
        ticker=result.ticker,
        # Non-`None` by construction: `_resolve_ticker` raises exit 2 on the path that would leave
        # them unset, so `run_fetch` cannot return with either missing.
        cik=result.cik or 0,
        name=result.name or result.ticker,
        profile=result.profile,
        filings=result.filings,
        window=span,
        as_of=resolved,
        market_cap=result.market_cap,
    )
    envelope = run_info(
        settings,
        ticker=result.ticker,
        as_of=resolved,
        window=span,
        lookback_years=years,
        manifest_hash=result.manifest_hash,
        version=version,
    )
    return history, envelope


def render_facts(history: FinancialHistory) -> str:
    """The human-readable table. See the module docstring on its four load-bearing properties."""
    lines: list[str] = [_header(history), ""]
    for bucket in (Bucket.ANNUAL, Bucket.QUARTERLY):
        lines.extend(_table(history, bucket))
        lines.append("")
    lines.extend(_coverage_block(history))
    lines.append("")
    lines.extend(_findings_block(history))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _header(history: FinancialHistory) -> str:
    parts = [history.ticker, history.name, f"CIK {history.cik}"]
    if history.sic is not None:
        parts.append(f"SIC {history.sic}")
    if history.fiscal_year_end:
        parts.append(f"FY end {history.fiscal_year_end}")
    count = history.quarters_available
    # Printed on the header rather than buried in coverage, because §5.1 gates the valuation on it at
    # two thresholds and a reader who sees "6 quarters" knows why M5 will refuse before it does.
    parts.append(f"{count} quarter{'' if count == 1 else 's'}")
    return "  ".join(parts) + f"       as of {history.as_of.isoformat()}"


def _table(history: FinancialHistory, bucket: Bucket) -> list[str]:
    """One bucket's table: a row per metric, the newest periods last, the winning tag on the right.

    Periods are the union across metrics rather than per metric, so a filer whose revenue and assets
    are tagged for different years still lines up in one grid — and a metric absent for a period the
    others have prints ``—`` there, which is the distinction between "no data" and "not asked for".
    """
    periods = _period_columns(history, bucket)
    if not periods:
        return [f"  {bucket}: no periods"]

    width = 12
    head = f"  {str(bucket):<26}" + "".join(f"{_label(day, bucket):>{width}}" for day in periods)
    lines = [f"{head}    tag"]
    for metric in _metric_order():
        facts = history.series(metric, bucket)
        by_end = {fact.period.end: fact for fact in facts}
        cells = "".join(f"{_cell(by_end.get(day)):>{width}}" for day in periods)
        lines.append(f"  {_name(metric):<26}{cells}    {_tag_column(history, metric, bucket)}")
    lines.append(f"  {'':<26}{'USD millions unless noted':>{width * len(periods)}}")
    return lines


def _period_columns(history: FinancialHistory, bucket: Bucket) -> tuple[date, ...]:
    """Every period end present in the bucket, oldest first, newest last."""
    ends = {fact.period.end for metric in CHAINS for fact in history.series(metric, bucket)}
    return tuple(sorted(ends))


def _label(day: date, bucket: Bucket) -> str:
    """``FY2024`` or ``2024-06-30``.

    A fiscal-year label rather than a date for the annual table, because that is how the periods are
    referred to everywhere else in the report — and the *calendar* year of the period end, never
    ``filing_fy``, which §4.2(a) forbids reading and which labels a calendar-Q1-2025 period fiscal
    2026.
    """
    return f"FY{day.year}" if bucket is Bucket.ANNUAL else day.isoformat()


def _name(metric: Metric) -> str:
    return str(metric).replace("_", " ")


def _cell(fact: Fact | None) -> str:
    """One figure, scaled and grouped — or ``—``, which is a value and not a gap.

    The **unit** decides the format, never the metric: a per-share figure is printed to two places
    because rounding EPS to millions would print every filer's earnings as ``0``, and a share count is
    suffixed so a 18,596 in the shares row cannot be read as $18.6bn.
    """
    if fact is None:
        return "—"
    if fact.unit in _PER_SHARE_UNITS:
        return f"{fact.value:,.2f}"
    if fact.unit in _SHARE_UNITS:
        return f"{fact.value / _SCALE:,.0f} sh"
    return f"{fact.value / _SCALE:,.0f}"


def _tag_column(history: FinancialHistory, metric: Metric, bucket: Bucket) -> str:
    """What produced this row: the winning tag, ``derived:``, or ``absent``.

    A stitched series prints both tags joined by an arrow, because a single "which tag won" string
    cannot represent Apple's revenue and truncating to the first one would describe the ASC 606
    transition backwards.
    """
    coverage = history.coverage.for_bucket(bucket).get(metric)
    facts = history.series(metric, bucket)
    if coverage is not None and coverage.tags_used:
        return " → ".join(_short(tag) for tag in coverage.tags_used)
    # No chain member matched, but facts exist — so every period came from a cross-metric derivation,
    # which names its own tags. `isinstance` rather than `hasattr`: `Provenance` has exactly two arms,
    # and a structural check would also match whatever else grows a `rule` attribute later.
    rules = sorted({fact.source.rule for fact in facts if isinstance(fact.source, Derivation)})
    if rules:
        return "derived: " + ", ".join(rules)
    return "absent"


def _short(tag: str) -> str:
    """``us-gaap:RevenueFromContractWith…`` — trimmed to keep the row on one line."""
    return tag if len(tag) <= 46 else tag[:45] + "…"


def _coverage_block(history: FinancialHistory) -> list[str]:
    """Tier aggregates for both buckets, **with the spine's origin printed beside them.**"""
    spine = history.coverage.spine
    origin = (
        f"spine: {spine.origin} "
        f"({len(spine.annual_ends)} annual, {len(spine.quarterly_ends)} quarterly)"
    )
    if str(spine.origin) == "observed":
        origin += " — circular denominator, see findings"
    lines = [f"  {'coverage':<26}{'annual':>10}{'quarterly':>12}    {origin}"]
    for tier, label in ((Tier.DCF, "tier 1 (DCF)"), (Tier.QUALITY, "tier 2 (F/Z/M)")):
        annual = history.coverage.tier_fill_rate(tier, Bucket.ANNUAL)
        quarterly = history.coverage.tier_fill_rate(tier, Bucket.QUARTERLY)
        lines.append(
            f"  {label:<26}{_percent(annual):>10}{_percent(quarterly):>12}"
            f"    {len(metrics_in_tier(tier))} metrics"
        )
    return lines


def _percent(rate: Decimal | None) -> str:
    """``92.9%``, or ``n/a`` when nothing was expected — never ``0.0%``, which is a different claim."""
    if rate is None:
        return "n/a"
    return f"{rate * Decimal(100):.1f}%"


def _findings_block(history: FinancialHistory) -> list[str]:
    findings = history.coverage.findings
    if not findings:
        return ["  findings: none"]
    lines = ["  findings"]
    for finding in findings:
        lines.append(f"  {finding.code:<28} {finding.detail}")
    return lines


def _metric_order() -> tuple[Metric, ...]:
    """Tier 1 then tier 2, in registry order — the order §9.1's appendix prints."""
    return (*metrics_in_tier(Tier.DCF), *metrics_in_tier(Tier.QUALITY))


def render_json(history: FinancialHistory, envelope: RunInfo) -> str:
    """``report.json`` as a string.

    A one-line wrapper so ``cli.py`` imports one module rather than two, and so the ``--json`` branch
    reads as the alternative rendering it is rather than as a reach into ``report/``.
    """
    return serialize(history, run=envelope)

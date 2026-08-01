"""``investo analyze TICKER`` — fetch, normalize, render, write (ROADMAP M3).

The body lives here rather than in ``cli.py`` for the reason :mod:`~investo.fetch` and
:mod:`~investo.facts` both give: ``cli.py`` stays the declared flag surface and nothing else,
because ``tests/test_cli_surface.py`` reads that file against README § Usage in both directions and
orchestration in the middle would make the check harder to trust rather than easier.

**The module is `analyze.py` and M4's package is `analysis/`.** A command body has to sit at the
package root — ``test_layering::test_every_clock_reading_module_is_a_command_body`` asserts
``"/" not in rel`` for every module permitted a clock read — and ``analyze.py`` and ``analyze/`` are
the same import name. DESIGN §3.1 originally drew the latter; the package with no code behind it
until M4 is the cheaper of the two to move. Recorded in ROADMAP § Decided while designing M3.

**This is the first command that can exit 3**, and §14's wording is what shapes the implementation:
"insufficient data, **report still written**, valuation omitted." Two consequences.

The code is a **field on the returned outcome, not an exception**. A function that raises
``InsufficientDataError`` before returning cannot have written anything, so the ordering §14
promises is made structural rather than left to somebody remembering to write before raising.

And *"valuation omitted"* is **not** the trigger. Every M3 report omits the valuation, because M5
has not landed — reading the clause that way would make every run exit 3, and a code that fires on
every invocation carries no information. An unbuilt milestone is not insufficient data. The
triggers are an absent ``companyfacts`` and coverage below a configured floor, and the floor is
unset by default until ``docs/m2/COVERAGE.md`` supplies a distribution to set it from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Final

from investo.config import Settings, parse_lookback
from investo.domain.periods import window as lookback_window
from investo.errors import ConfigError, ExitCode
from investo.fetch import run_fetch
from investo.normalize.statements import Bucket, FinancialHistory, build_history
from investo.normalize.tags import Tier
from investo.report import format as fmt
from investo.report.model import ABSENT_SECTIONS, build_model
from investo.report.render import render_report
from investo.report.serialize import SCHEMA_VERSION, RunInfo, run_info, serialize

__all__ = [
    "PDF_NAME",
    "JSON_NAME",
    "AnalyzeOutcome",
    "run_analyze",
    "render_analyze_summary",
    "require_no_llm",
    "output_dir",
    # Public because each is a decision with its own violation test, and a test that reaches for a
    # private name is a test asserting an implementation detail. `outcome_code` in particular is the
    # whole of exit 3's reading of §14, and it is worth being able to call on a history.
    "write_atomic",
    "source_date_epoch",
    "record_flags",
    "outcome_code",
]

PDF_NAME: Final = "report.pdf"
JSON_NAME: Final = "report.json"
"""README fixes these two names and their adjacency — *"Every run also writes `report.json` next to
the PDF"* — and says nothing about the directory above them. :func:`output_dir` decides that."""


def output_dir(settings: Settings, *, ticker: str, as_of: date) -> Path:
    """``out_dir/TICKER/AS_OF``. Three candidate layouts, and only one survives.

    Flat ``reports/report.pdf`` makes the second ticker overwrite the first. ``reports/AAPL.pdf``
    makes tomorrow's run overwrite today's, which destroys the input DESIGN §4.5's ``investo diff``
    exists for.

    Keyed on ``as_of`` rather than on run time, so **re-running one point-in-time reconstruction
    overwrites itself**. That is what makes §11's gate runnable as "run it twice and compare the
    file", and it makes a directory listing a list of distinct reconstructions rather than a list of
    times somebody typed the command.
    """
    return settings.out_dir / ticker.upper() / as_of.isoformat()


@dataclass(frozen=True, slots=True)
class AnalyzeOutcome:
    """What one run produced. Rendered by :func:`render_analyze_summary`."""

    ticker: str
    name: str
    pdf_path: Path
    json_path: Path
    pages: int
    exit_code: ExitCode
    reason: str | None
    history: FinancialHistory
    run: RunInfo
    charts_drawn: int
    charts_omitted: int
    overflowing: int | None
    """Boxes running outside the page box, or ``None`` when the geometry walk could not run.

    ``None`` and ``0`` are different claims — "not checked" versus "checked and clean" — and the
    summary prints them differently. See ``report/render.Rendered.geometry_available``.
    """

    @property
    def ok(self) -> bool:
        return self.exit_code is ExitCode.SUCCESS


def require_no_llm(provider: str) -> None:
    """Refuse a provider M6 has not built. Exit 5.

    Accepting ``--llm anthropic`` and producing a report with no narrative section is a report that
    silently did not do the thing it was asked for — and unlike a missing metric, the user has no
    way to tell from the artifact. §14's exit 5 is "config error", and requesting a provider that
    does not exist is one.
    """
    if provider != "none":
        raise ConfigError(
            f"--llm {provider} is not available yet.",
            hint=(
                "Narrative analysis arrives with ROADMAP M6. Run with --llm none, which is the "
                "default and produces a complete report for everything built so far."
            ),
        )


def run_analyze(
    ticker: str,
    *,
    settings: Settings,
    refresh: bool = False,
    as_of: date | None = None,
    brief: bool = False,
    explain: bool = False,
    peers: tuple[str, ...] | None = None,
    assumptions: Path | None = None,
    version: str = "0.1.0",
) -> AnalyzeOutcome:
    """Fetch, normalize, render, and write both artifacts.

    **The clock is read here, once, and nowhere below.** ``run_fetch``, ``build_history``,
    ``build_model`` and ``render_pdf`` all receive the resolved date; nothing under ``normalize/`` or
    ``report/`` may read one at all, which an AST rule enforces with an empty allowlist. A default
    resolved deeper makes two runs either side of midnight differ, and §11's gate would report that
    as nondeterminism rather than as the design mistake it is.

    Args:
        ticker: NASDAQ symbol.
        settings: Resolved configuration.
        refresh: Bypass the cache read.
        as_of: Point-in-time date, already validated as not future by ``cli._resolve_as_of``.
        brief: Selects the two-page template. Changes nothing else — the same model is built.
        explain: Recorded in ``run.explain`` and otherwise inert at M3; there are no intermediate
            calculations until M5's driver build.
        peers: Recorded in ``run.peers``. M4 owns the cohort; the list is a real input to a later
            run and belongs in the run record now so the two can be compared.
        assumptions: Recorded as a path. M5 owns the contents.
        version: Package version, for ``generated_by``.

    Raises:
        TickerNotFoundError: absent from SEC's ticker file, or not NASDAQ. Exit 2.
        UpstreamFetchError: a malformed payload, or retries exhausted. Exit 4.
        ConfigError: bad lookback, unusable cache, missing price-provider key, or ``--llm``. Exit 5.
        RenderSecurityError: a template or a model value reached for a URL. Exit 5.
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
        coverage_floor=settings.coverage_floor,
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
    envelope = record_flags(
        envelope, brief=brief, explain=explain, peers=peers, assumptions=assumptions
    )

    model = build_model(history, envelope, brief=brief)
    document = serialize(history, run=envelope)
    rendered = render_report(model, source_date_epoch=source_date_epoch(resolved), brief=brief)

    # `report.json` first. If the render raises, the run leaves the machine-readable half of a
    # documented pair on disk, which is recoverable; the reverse leaves a PDF whose run record does
    # not exist, which is the artifact §3.2's traceability claim is about.
    destination = output_dir(settings, ticker=result.ticker, as_of=resolved)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = write_atomic(destination / JSON_NAME, document.encode("utf-8"))
    pdf_path = write_atomic(destination / PDF_NAME, rendered.pdf)

    code, reason = outcome_code(history, settings)
    drawn = sum(1 for image in model.history.charts if image.drawn)
    return AnalyzeOutcome(
        ticker=result.ticker,
        name=history.name,
        pdf_path=pdf_path,
        json_path=json_path,
        pages=rendered.pages,
        exit_code=code,
        reason=reason,
        history=history,
        run=envelope,
        charts_drawn=drawn,
        charts_omitted=len(model.history.charts) - drawn,
        overflowing=len(rendered.overflowing) if rendered.overflowing is not None else None,
    )


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
def write_atomic(path: Path, payload: bytes) -> Path:
    """Write through a temporary file in the same directory, then ``Path.replace``.

    Atomic within a filesystem, so a partial file is never visible under the final name. A
    half-written ``report.json`` that happens to parse is worse than one that does not — the second
    is an error and the first is a wrong answer.

    Written in the same directory rather than in ``/tmp`` because the rename is only atomic
    within one filesystem, and an output directory on a different mount is the normal case for
    anyone writing to a network share.
    """
    staging = path.with_name(f".{path.name}.partial")
    _ = staging.write_bytes(payload)
    staging.replace(path)
    return path


def source_date_epoch(as_of: date) -> int:
    """``as_of`` at midnight UTC, as a POSIX timestamp — WeasyPrint's ``SOURCE_DATE_EPOCH``.

    Not ``0``, which would date every report to 1970 and make the field actively misleading rather
    than merely uninformative. Not the wall clock, which breaks §11's gate. ``as_of`` is an input to
    the run, so a PDF whose creation date is ``as_of`` is a function of its inputs — which is the
    whole of the claim the gate makes.
    """
    return int(datetime.combine(as_of, time.min, tzinfo=UTC).timestamp())


def record_flags(
    envelope: RunInfo,
    *,
    brief: bool,
    explain: bool,
    peers: tuple[str, ...] | None,
    assumptions: Path | None,
) -> RunInfo:
    """Record the four flags whose behaviour belongs to a later milestone.

    Adding keys to ``run`` is not a ``schema_version`` bump — §4.5's rule is that adding a key is
    not, and changing a key's type, units or meaning is.

    ``assumptions`` records the **path**, not the contents. M5 may put anything in that file, and a
    run record that grew a schema per milestone would need its own version.
    """
    config = dict(envelope.config)
    config["brief"] = str(brief).lower()
    config["explain"] = str(explain).lower()
    config["peers"] = ",".join(peers) if peers else ""
    config["assumptions"] = str(assumptions) if assumptions is not None else ""
    return RunInfo(
        ticker=envelope.ticker,
        as_of=envelope.as_of,
        window=envelope.window,
        lookback_years=envelope.lookback_years,
        manifest_hash=envelope.manifest_hash,
        config=config,
        generated_by=envelope.generated_by,
    )


def outcome_code(history: FinancialHistory, settings: Settings) -> tuple[ExitCode, str | None]:
    """Exit 3's two triggers, and nothing else. See the module docstring."""
    if history.coverage.findings_for("companyfacts_absent"):
        return (
            ExitCode.INSUFFICIENT_DATA,
            "SEC publishes no companyfacts for this CIK — the report is written and empty.",
        )
    floor = settings.coverage_floor
    if floor is not None:
        rate = history.coverage.tier_fill_rate(Tier.DCF, Bucket.ANNUAL)
        if rate is not None and rate < floor:
            return (
                ExitCode.INSUFFICIENT_DATA,
                f"tier-1 annual coverage {fmt.percent(rate)} is below the configured floor "
                f"{fmt.percent(floor)}.",
            )
    return ExitCode.SUCCESS, None


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
def render_analyze_summary(outcome: AnalyzeOutcome) -> str:
    """What the command prints. Three properties are load-bearing rather than cosmetic.

    - **Omissions are counted and named**, and ``0 omitted`` is printed rather than suppressed. A
      line that only appears when something is wrong is a line nobody learns to read.
    - **The absent milestones are listed on every successful run.** This is the summary's most
      useful line for the next several milestones and the one most likely to be cut as noise. It is
      what stops someone concluding the tool has no opinion on valuation when it has not been built.
    - **Findings are named, not just counted.** ``facts.py``'s argument, one command over: "3
      findings" is a number nobody acts on.
    """
    history = outcome.history
    coverage = history.coverage
    annual = coverage.tier_fill_rate(Tier.DCF, Bucket.ANNUAL)
    quality = coverage.tier_fill_rate(Tier.QUALITY, Bucket.ANNUAL)
    codes = sorted({finding.code for finding in coverage.findings}, key=_text)

    lines = [
        f"{outcome.ticker}  {outcome.name}  CIK {history.cik}"
        f"       as of {history.as_of.isoformat()}",
        "",
        f"  {'report.pdf':<16}{f'{outcome.pages} pages':<18}{outcome.pdf_path}",
        f"  {'report.json':<16}{f'schema {SCHEMA_VERSION}':<18}{outcome.json_path}",
        "",
        f"  {'coverage':<16}tier 1 annual {fmt.percent(annual)}   "
        f"tier 2 annual {fmt.percent(quality)}   spine: {coverage.spine.origin}",
        f"  {'history':<16}{len(coverage.spine.annual_ends)} annual, "
        f"{len(coverage.spine.quarterly_ends)} quarterly period ends; "
        f"{history.quarters_available} quarters available",
        f"  {'charts':<16}{outcome.charts_drawn} rendered, {outcome.charts_omitted} omitted"
        + _overflow_note(outcome.overflowing),
        f"  {'findings':<16}{len(coverage.findings)}"
        + (f"   ({', '.join(codes)})" if codes else "   none"),
        "",
        "  not in this report: "
        + "; ".join(f"{what.split(' — ')[0]} ({milestone})" for what, milestone in ABSENT_SECTIONS),
    ]
    if outcome.reason is not None:
        lines.extend(["", f"  exit {int(outcome.exit_code)}: {outcome.reason}"])
    return "\n".join(lines)


def _overflow_note(count: int | None) -> str:
    """``None`` and ``0`` are different claims, so they print differently.

    "Checked and clean" is worth nothing to a reader who cannot tell it from "not checked", and the
    geometry walk *can* go dark on a WeasyPrint that renames a private attribute
    (``report/render.Rendered.geometry_available``). Silence in that case would be the worst of the
    three options.
    """
    if count is None:
        return "   (layout check unavailable on this WeasyPrint)"
    return "" if count == 0 else f"   ({count} boxes overflow the page)"


def _text(value: str) -> str:
    return value

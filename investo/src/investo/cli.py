"""The ``investo`` command line interface (ROADMAP M0).

Every command and every flag documented in README § Usage is declared and parsed here. The
bodies are not implemented — each raises :class:`~investo.errors.NotImplementedYetError`
naming the milestone that fills it in. That is the point of M0: the flag surface is reviewable
while it is still free to change, and each later milestone deletes one stub rather than
inventing an interface under deadline.

**Where the global flags live.** ROADMAP M0 calls ``--out``, ``--refresh``, ``--as-of`` and
``--cache-dir`` cross-cutting, but README § Usage shows them *after* the subcommand
(``investo analyze AAPL --out ./reports``). Declaring them on typer's top-level callback would
require ``investo --out ./reports analyze AAPL`` instead, which contradicts the documented
usage. They are therefore declared once as the ``Out``/``Refresh``/``AsOf``/``CacheDir``
aliases below and reused by each command that takes them — one definition, README's ergonomics.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Final

import typer

from investo.config import DEFAULT_LOOKBACK, LLMProvider, Settings, load_settings, parse_lookback
from investo.errors import ConfigError, ExitCode, InvestoError, NotImplementedYetError

__all__ = ["app", "main"]

_DATE_FORMAT: Final = "%Y-%m-%d"


# ---------------------------------------------------------------------------
# Shared parameter declarations
# ---------------------------------------------------------------------------
# `Annotated[...]` rather than a `typer.Option(...)` default. Both work, but the default form
# puts a function call in a default argument, which is the pattern flake8-bugbear's B008 exists
# to catch — and silencing B008 for this file would also silence it for a genuine mutable
# default written here later.

Ticker = Annotated[str, typer.Argument(metavar="TICKER", help="NASDAQ ticker symbol, e.g. AAPL")]

Out = Annotated[
    Path | None,
    typer.Option("--out", metavar="PATH", help="Output directory. Default: ./reports"),
]

CacheDir = Annotated[
    Path | None,
    typer.Option("--cache-dir", metavar="PATH", help="Raw-payload cache. Default: ./.cache"),
]

Refresh = Annotated[
    bool,
    typer.Option("--refresh", help="Re-fetch from upstream instead of using the cache."),
]

AsOf = Annotated[
    datetime | None,
    typer.Option(
        "--as-of",
        metavar="DATE",
        formats=[_DATE_FORMAT],
        help=(
            "Reconstruct using only filings available on DATE (YYYY-MM-DD), restatements "
            "included. Point-in-time; this is what makes the pipeline replayable."
        ),
    ),
]

ConfigFile = Annotated[
    Path | None,
    typer.Option(
        "--config",
        metavar="FILE",
        help="TOML config file. Default: ./investo.toml, then ~/.config/investo/investo.toml",
    ),
]

# Defaults to `None`, not to `DEFAULT_LOOKBACK`, and the same goes for `--llm`.
#
# A typer default is indistinguishable from a value the user typed, so a flag defaulting to
# "5y" is passed to `load_settings` on every run and silently outranks `lookback` in the config
# file — the file would be dead config that appears to work. `None` means "not specified", the
# `is not None` filter in `load_settings` drops it, and the declared field default applies.
Lookback = Annotated[
    str | None,
    typer.Option(
        "--lookback",
        metavar="DURATION",
        help="Estimation window in whole years, minimum 3y. Independent of the forecast "
        f"horizons, which are always 1y/2y/5y. Default: {DEFAULT_LOOKBACK}.",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fail(error: InvestoError) -> typer.Exit:
    """Print ``error`` to stderr and return the ``typer.Exit`` to raise.

    Returns rather than raises so the call site reads ``raise _fail(e)`` — which keeps the
    control flow visible to a reader and to the type checker, instead of hiding a
    never-returns call inside a helper.
    """
    print(f"error: {error.message}", file=sys.stderr)
    if error.hint:
        print(f"\n{error.hint}", file=sys.stderr)
    return typer.Exit(int(error.exit_code))


# Bad flag values raise `ConfigError` — exit 5 — rather than the `InvestoError` base, whose
# default code is 4, "upstream fetch failure". Reporting a fetch failure for a malformed date
# would be a lie about where the run stopped, and exit 5's promise is exactly the true one: the
# run was misconfigured and nothing was fetched.


def _resolve_as_of(value: datetime | None) -> date | None:
    """Narrow typer's ``datetime`` to a ``date`` and reject a future one.

    typer parses ``--as-of`` as a ``datetime`` because that is the type it knows how to build
    from a format string; the domain only ever means a calendar day. A future date is rejected
    here because the alternative is a run that silently includes every filing in the cache and
    calls the result point-in-time — which in a backtest looks like clairvoyance.
    """
    if value is None:
        return None
    as_of = value.date()
    today = date.today()
    if as_of > today:
        raise ConfigError(
            f"--as-of {as_of.isoformat()} is in the future (today is {today.isoformat()}).",
            hint="A point-in-time reconstruction needs a date that has already happened.",
        )
    return as_of


def _split_list(value: str | None, *, flag: str) -> tuple[str, ...] | None:
    """Split a comma-separated flag value, per README's ``--peers TICKER,...`` spelling.

    Comma-separated rather than a repeated flag because that is what README documents. Empty
    items are an error, not silently dropped: ``--peers MSFT,,GOOG`` is a typo, and accepting
    it hides a shell-quoting mistake that would otherwise be obvious.
    """
    if value is None:
        return None
    items = tuple(part.strip() for part in value.split(","))
    if any(not item for item in items):
        raise ConfigError(
            f"{flag} {value!r} has an empty entry.",
            hint="Separate values with single commas and no trailing comma.",
        )
    return items


def _settings(
    *,
    config_file: Path | None,
    out: Path | None = None,
    cache_dir: Path | None = None,
    **overrides: object,
) -> Settings:
    return load_settings(config_file=config_file, out_dir=out, cache_dir=cache_dir, **overrides)


def _version() -> str:
    """The installed version, or a marker. Shared by ``--version`` and ``report.json``.

    One resolution path, because ``report.json``'s ``generated_by`` and ``investo --version`` disagreeing
    would make a run record impossible to attribute to a build.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("investo")
    except PackageNotFoundError:
        # Running from a checkout with no install — `pythonpath` set, nothing on the metadata path.
        # Reported rather than raised: `--version` failing is worse than `--version` being vague.
        return "unknown (not installed)"


def _version_callback(value: bool) -> None:
    if value:
        print(f"investo {_version()}")
        raise typer.Exit(ExitCode.SUCCESS)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = typer.Typer(
    name="investo",
    help=(
        "Fundamental due-diligence reports for NASDAQ-listed companies, from SEC filings.\n\n"
        "Not investment advice. Output is generated by statistical models from historical "
        "data and will be wrong. See README § Disclaimer."
    ),
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

cache_app = typer.Typer(name="cache", help="Manage the raw-payload cache.", no_args_is_help=True)
app.add_typer(cache_app)


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Root callback. Exists to host ``--version``; per the module docstring, the
    cross-cutting flags are declared per-command so README's flag placement works.

    Not underscore-prefixed, unlike this module's other internal helpers: basedpyright's
    ``reportUnusedFunction`` treats a leading underscore as a private-symbol marker and expects
    it to be referenced elsewhere in the file. A typer callback is only ever reached through the
    ``@app.callback()`` registration, the same as ``analyze``, ``facts`` and the other command
    functions below — which is why none of those are underscore-prefixed either.
    """


@app.command()
def analyze(
    ticker: Ticker,
    lookback: Lookback = None,
    out: Out = None,
    cache_dir: CacheDir = None,
    llm: Annotated[
        LLMProvider | None,
        typer.Option(
            "--llm",
            help="Narrative-analysis provider. `none` produces a complete report. "
            "Default: none, or `llm_provider` from config.",
        ),
    ] = None,
    peers: Annotated[
        str | None,
        typer.Option(
            "--peers",
            metavar="TICKER,...",
            help="Override the SIC-derived peer cohort. The cohort also sets the fade target "
            "and the default exit multiple, so this changes the valuation.",
        ),
    ] = None,
    assumptions: Annotated[
        Path | None,
        typer.Option(
            "--assumptions",
            metavar="FILE",
            help="Hand-set growth, margin, WACC and exit multiple.",
        ),
    ] = None,
    as_of: AsOf = None,
    refresh: Refresh = False,
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Dump all intermediate calculations to report.json."),
    ] = False,
    brief: Annotated[
        bool,
        typer.Option("--brief", help="2-page summary instead of the full report."),
    ] = False,
    config_file: ConfigFile = None,
) -> None:
    """Generate a due-diligence report for TICKER.

    Writes report.pdf and report.json to ``<out>/TICKER/<as-of>/``. Keyed by ticker *and* as-of
    date, so a second ticker does not overwrite the first and re-running one point-in-time
    reconstruction overwrites itself rather than accumulating (DESIGN.md §14).

    **The PDF is historical only at M3** — cover, snapshot, charts, caveats and appendix. There is no
    forecast, score, peer cohort, event feed or narrative; the verdict badge reads ``NOT ASSESSED``
    and the caveats section names each absence and the milestone that fills it.

    Exits 3 when the report was written and is thin — no ``companyfacts`` for the CIK, or tier-1
    coverage below a configured floor. **Not** because a later milestone's section is missing: an
    unbuilt milestone is not insufficient data, and a code that fired on every run would carry no
    information.
    """
    from investo.analyze import render_analyze_summary, require_no_llm, run_analyze

    try:
        settings = _settings(
            config_file=config_file,
            out=out,
            cache_dir=cache_dir,
            llm_provider=llm,
            lookback=lookback,
        )
        parse_lookback(settings.lookback)
        # Before the fetch, not after it. Both of these are free to check and expensive to discover
        # forty seconds in, and `--llm` in particular would otherwise produce a complete-looking
        # report with the section the user asked for silently missing.
        require_no_llm(settings.llm_provider)
        if assumptions is not None and not assumptions.is_file():
            raise ConfigError(f"--assumptions file not found: {assumptions}")
        # The clock is read inside `run_analyze`, once, and nowhere below it — enforced by an AST
        # rule with an empty allowlist under `normalize/` and `report/`.
        outcome = run_analyze(
            ticker,
            settings=settings,
            refresh=refresh,
            as_of=_resolve_as_of(as_of),
            brief=brief,
            explain=explain,
            peers=_split_list(peers, flag="--peers"),
            assumptions=assumptions,
            version=_version(),
        )
        print(render_analyze_summary(outcome))
        # Raised from the code on the outcome rather than from inside `run_analyze`, so exit 3's
        # promise — "insufficient data, *report still written*" — holds by construction: both files
        # are on disk before this line is reached.
        if outcome.exit_code is not ExitCode.SUCCESS:
            raise typer.Exit(int(outcome.exit_code))
    except InvestoError as error:
        raise _fail(error) from error


@app.command()
def facts(
    ticker: Ticker,
    lookback: Lookback = None,
    as_of: AsOf = None,
    refresh: Refresh = False,
    json_: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Write report.json to stdout instead of the table. Composes with redirection.",
        ),
    ] = False,
    cache_dir: CacheDir = None,
    config_file: ConfigFile = None,
) -> None:
    """Print normalized financials and the coverage report for TICKER.

    Exits 0 even when data is missing, and **never exits 3**. Exit 3 means "insufficient data, report
    still written" (DESIGN.md §14) and this command writes no report, so returning it would promise an
    artifact that does not exist. Thin coverage, a metric that resolves to nothing, fewer than twelve
    quarters and a CIK with no ``companyfacts`` are all printed and exit 0.

    ``--json`` emits DESIGN.md §4.5's ``report.json`` document on stdout rather than the table. No
    ``--out``: this command writes no files.
    """
    from investo.facts import render_facts, render_json, run_facts

    try:
        settings = _settings(config_file=config_file, cache_dir=cache_dir, lookback=lookback)
        parse_lookback(settings.lookback)
        # The clock is read here and only here. Everything below the command boundary receives the
        # resolved date, so nothing under `normalize/` or `report/` can make two runs either side of
        # midnight differ — enforced by an AST rule, not by convention.
        history, envelope = run_facts(
            ticker,
            settings=settings,
            refresh=refresh,
            as_of=_resolve_as_of(as_of),
            version=_version(),
        )
        # `serialize` already ends its document with a newline, so `--json` prints with `end=""`:
        # the bytes on stdout are then exactly the serializer's, which is what §11's gate compares
        # and what `> report.json` has to produce for M3 to inherit the gate rather than rebuild it.
        if json_:
            print(render_json(history, envelope), end="")
        else:
            print(render_facts(history))
    except InvestoError as error:
        raise _fail(error) from error


@app.command()
def fetch(
    ticker: Ticker,
    refresh: Refresh = False,
    cache_dir: CacheDir = None,
    config_file: ConfigFile = None,
) -> None:
    """Populate the cache for TICKER without producing a report.

    Exits 0 even when data is missing. A 404 or an untagged metric is an *absence*, printed in the
    summary's ``absent`` section — whether an absence is fatal depends on what needs it, which is
    ``analyze``'s question and not this command's (DESIGN.md §14, and docs/m1/README.md §4).

    How far back it fetches comes from ``lookback`` in config, not from a flag: README § Usage
    documents ``--lookback`` on ``analyze`` and ``facts`` only.
    """
    from investo.fetch import render_summary, run_fetch

    try:
        settings = _settings(config_file=config_file, cache_dir=cache_dir)
        result = run_fetch(ticker, settings=settings, refresh=refresh)
        print(render_summary(result))
    except InvestoError as error:
        raise _fail(error) from error


@cache_app.command("prune")
def cache_prune(
    older_than: Annotated[
        str,
        typer.Option(
            "--older-than",
            metavar="DURATION",
            help="Drop entries whose fetch timestamp is older than this, e.g. 90d.",
        ),
    ],
    cache_dir: CacheDir = None,
    config_file: ConfigFile = None,
) -> None:
    """Drop cache entries older than a given age.

    Keeps at least one entry per key regardless of age. Pruning the only entry for a key turns the
    next run into a cold fetch of something the user believes is cached, and pruning the newest while
    keeping an older one silently reverts the cache to a stale view (docs/m1/02-cache.md §6).
    """
    from datetime import UTC, timedelta

    from investo.fetch import open_cache

    try:
        settings = _settings(config_file=config_file, cache_dir=cache_dir)
        days = _parse_days(older_than)
        report = open_cache(settings).prune(older_than=timedelta(days=days), now=datetime.now(UTC))
        # Printed, because a prune that reports nothing is a prune the user runs twice.
        print(
            f"pruned {report.entries_removed} entr{'y' if report.entries_removed == 1 else 'ies'}, "
            f"kept {report.entries_kept}; removed {report.blobs_removed} blob(s), "
            f"reclaimed {report.bytes_reclaimed / 1_048_576:.1f} MB"
        )
    except InvestoError as error:
        raise _fail(error) from error


@app.command()
def backtest(
    universe: Annotated[
        str, typer.Option("--universe", help="Ticker universe, e.g. nasdaq100.")
    ] = "nasdaq100",
    start: Annotated[
        int, typer.Option("--start", metavar="YEAR", help="First year to run.")
    ] = 2015,
    horizons: Annotated[
        str,
        typer.Option("--horizons", metavar="DURATION,...", help="Forecast horizons to score."),
    ] = "1y,2y,5y",
    out: Out = None,
    cache_dir: CacheDir = None,
    config_file: ConfigFile = None,
) -> None:
    """Walk forward over a universe and score the forecasts against naive baselines."""
    try:
        _settings(config_file=config_file, out=out, cache_dir=cache_dir)
        # Deliberately not validated with `parse_lookback`: that enforces a 3-year minimum,
        # which is right for an estimation window and wrong for a forecast horizon — 1y is a
        # documented horizon. Two different constraints on the same `Ny` spelling.
        _parse_horizons(horizons)
        raise NotImplementedYetError.at("M7", "backtest")
    except InvestoError as error:
        raise _fail(error) from error


def _parse_days(value: str) -> int:
    """Parse a ``--older-than`` duration such as ``"90d"`` into days."""
    text = value.strip().lower()
    if not text.endswith("d") or not text[:-1].isdigit():
        raise ConfigError(
            f"--older-than {value!r} is not a recognised duration.",
            hint="Use whole days, e.g. 90d.",
        )
    return int(text[:-1])


def _parse_horizons(value: str) -> tuple[int, ...]:
    """Parse ``--horizons 1y,2y,5y`` into whole years, rejecting anything else."""
    horizons: list[int] = []
    for item in _split_list(value, flag="--horizons") or ():
        text = item.lower()
        if not text.endswith("y") or not text[:-1].isdigit() or int(text[:-1]) == 0:
            raise ConfigError(
                f"--horizons entry {item!r} is not a recognised horizon.",
                hint="Use whole years, e.g. 1y,2y,5y.",
            )
        horizons.append(int(text[:-1]))
    return tuple(horizons)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m investo``, returning an exit code rather than raising.

    ``app()`` is what the ``investo`` console script calls; this wrapper exists so
    ``__main__`` and the tests can observe the code.

    Click's ``standalone_mode`` stays on, deliberately. Turning it off looks tidier — the call
    then *returns* the code instead of raising — but it also stops Click rendering usage errors,
    so ``investo analyze --bogus`` would raise a bare ``UsageError`` with nothing on stderr.
    Catching ``SystemExit`` keeps Click's error output and still yields the code.
    """
    try:
        app(args=argv, standalone_mode=True)
    except SystemExit as exit_:
        # `code` is None for a bare `sys.exit()`, and a str if anything ever exits with a
        # message; neither should be reported as success by accident.
        if exit_.code is None:
            return int(ExitCode.SUCCESS)
        return exit_.code if isinstance(exit_.code, int) else 1
    return int(ExitCode.SUCCESS)

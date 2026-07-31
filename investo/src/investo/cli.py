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


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import PackageNotFoundError, version

        try:
            resolved = version("investo")
        except PackageNotFoundError:
            # Running from a checkout with no install — `pythonpath` set, nothing on the
            # metadata path. Reported rather than raised: `--version` failing is worse than
            # `--version` being vague.
            resolved = "unknown (not installed)"
        print(f"investo {resolved}")
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

    Writes report.pdf and report.json to the output directory.
    """
    try:
        settings = _settings(
            config_file=config_file,
            out=out,
            cache_dir=cache_dir,
            llm_provider=llm,
            lookback=lookback,
        )
        parse_lookback(settings.lookback)
        _resolve_as_of(as_of)
        _split_list(peers, flag="--peers")
        if assumptions is not None and not assumptions.is_file():
            raise ConfigError(f"--assumptions file not found: {assumptions}")
        raise NotImplementedYetError.at("M3", f"analyze {ticker}")
    except InvestoError as error:
        raise _fail(error) from error


@app.command()
def facts(
    ticker: Ticker,
    lookback: Lookback = None,
    as_of: AsOf = None,
    refresh: Refresh = False,
    cache_dir: CacheDir = None,
    config_file: ConfigFile = None,
) -> None:
    """Print normalized financials and the coverage report for TICKER."""
    try:
        settings = _settings(config_file=config_file, cache_dir=cache_dir, lookback=lookback)
        parse_lookback(settings.lookback)
        _resolve_as_of(as_of)
        raise NotImplementedYetError.at("M2", f"facts {ticker}")
    except InvestoError as error:
        raise _fail(error) from error


@app.command()
def fetch(
    ticker: Ticker,
    refresh: Refresh = False,
    cache_dir: CacheDir = None,
    config_file: ConfigFile = None,
) -> None:
    """Populate the cache for TICKER without producing a report."""
    try:
        _settings(config_file=config_file, cache_dir=cache_dir)
        raise NotImplementedYetError.at("M1", f"fetch {ticker}")
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
    """Drop cache entries older than a given age."""
    try:
        _settings(config_file=config_file, cache_dir=cache_dir)
        _parse_days(older_than)
        raise NotImplementedYetError.at("M1", "cache prune")
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

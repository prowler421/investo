"""Exit codes and the exception hierarchy that produces them (DESIGN.md §14).

The taxonomy is normative in DESIGN.md §14 and is reproduced here as the single place the
numbers are written down. Everything that ends a run abnormally raises an ``InvestoError``
subclass; ``cli`` translates it to a message on stderr and the code below.

The governing distinction is between **a run that failed** and **a run that succeeded in
reporting bad news**. A missing metric degrades coverage and confidence — it does not abort
(§14). A company that cannot be valued still gets a report; that is exit 3, and the report is
written. Only codes 2, 4 and 5 mean no output was produced.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "ExitCode",
    "InvestoError",
    "ConfigError",
    "TickerNotFoundError",
    "InsufficientDataError",
    "UpstreamFetchError",
    "NotImplementedYetError",
]


class ExitCode(IntEnum):
    """Process exit codes, per DESIGN.md §14.

    Two codes are *not* ours to assign, and both absences are deliberate:

    ``1``
        Left to the interpreter. An unhandled exception exits 1, which makes 1 mean
        "investo has a bug" and keeps that meaning distinct from every code below. Nothing
        in the codebase should return it on purpose.

    ``2``
        Shared, and this is a known wart. DESIGN.md §14 assigns 2 to "ticker not found or not
        NASDAQ", but Click — which typer is built on — already exits 2 for a usage error, and
        that is not configurable without subclassing its exception machinery. So
        ``investo analyze NOTATICKER`` and ``investo analyze --bogus-flag`` are
        indistinguishable by exit code alone, though not by their stderr output.

        Implemented as designed rather than quietly renumbered: DESIGN.md is normative, and a
        spec conflict gets raised, not resolved in code. ``tests/test_errors.py`` pins the
        collision so it stays visible. If it needs fixing, the cheap fix is moving
        TICKER_NOT_FOUND to 6.
    """

    SUCCESS = 0
    TICKER_NOT_FOUND = 2
    """Ticker unknown to EDGAR, or known but not NASDAQ-listed. No report written."""

    INSUFFICIENT_DATA = 3
    """Report **was** written, with the valuation section omitted — banks, REITs, pre-revenue
    filers, or fewer than 12 quarters of history (DESIGN.md §6.10). A deliberate refusal to
    value, not a failure to run."""

    UPSTREAM_FETCH_FAILURE = 4
    """An upstream source failed after the client exhausted its retries. No report written."""

    CONFIG_ERROR = 5
    """Configuration is missing or invalid — most often an unset ``INVESTO_SEC_USER_AGENT``,
    which SEC requires and for which there is deliberately no default (DESIGN.md §4.1).
    Raised before any network call."""

    NOT_IMPLEMENTED = 70
    """**Not part of the DESIGN.md §14 taxonomy.** Scaffolding: the command parsed and its
    configuration resolved, but the milestone that implements it has not landed.

    Numbered outside §14's range on purpose, so it can never be mistaken for a real outcome —
    in particular not for exit 3, which means a report *was* written. 70 is ``EX_SOFTWARE``
    from ``sysexits.h``, the nearest standard sense of "this binary cannot do that".

    This member disappears when the last stub in ``cli`` does. If it is still here after M7,
    something was never finished.
    """


class InvestoError(Exception):
    """Base class for every condition that ends a run with a nonzero code.

    Subclasses set ``exit_code``; the CLI reads it rather than mapping exception types to
    numbers at the call site, so a new error class cannot be introduced without also stating
    how it exits.
    """

    exit_code: ExitCode = ExitCode.UPSTREAM_FETCH_FAILURE

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        """
        Args:
            message: What went wrong, in one line, phrased for someone who did not write this.
            hint: Optional second line telling them what to do about it. Present when there is
                a concrete action — set this variable, pass this flag — and absent when there
                is not, because a hint that only restates the message trains people to skip
                reading hints.
        """
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(InvestoError):
    """Configuration is missing or invalid. Exit 5."""

    exit_code = ExitCode.CONFIG_ERROR


class TickerNotFoundError(InvestoError):
    """Ticker is unknown to EDGAR, or is not NASDAQ-listed. Exit 2."""

    exit_code = ExitCode.TICKER_NOT_FOUND


class InsufficientDataError(InvestoError):
    """Not enough data to value the company. Exit 3, and a report is still written.

    Raised by the valuation path only. Anything that merely thins coverage belongs in the
    ``CoverageReport`` and the confidence rating instead — see DESIGN.md §14.
    """

    exit_code = ExitCode.INSUFFICIENT_DATA


class UpstreamFetchError(InvestoError):
    """An upstream source failed after retries were exhausted. Exit 4."""

    exit_code = ExitCode.UPSTREAM_FETCH_FAILURE


class NotImplementedYetError(InvestoError):
    """A command exists and parses, but its milestone has not landed. Exit 70.

    Temporary. Each milestone deletes the raise that names it; when M7 lands, this class and
    :attr:`ExitCode.NOT_IMPLEMENTED` both go.
    """

    exit_code = ExitCode.NOT_IMPLEMENTED

    @classmethod
    def at(cls, milestone: str, what: str) -> NotImplementedYetError:
        """Build the error for ``what``, naming the ROADMAP milestone that implements it.

        The milestone is in the message rather than only in ROADMAP.md because the person who
        hits this is at a terminal, not reading the build plan.
        """
        return cls(
            f"`investo {what}` is not implemented yet.",
            hint=(
                f"Arrives in {milestone} — see ROADMAP.md. Configuration and arguments were "
                f"validated, so this is the interface, not a failure."
            ),
        )

"""The exit-code taxonomy (DESIGN.md §14) and the exceptions that carry it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from investo.errors import (
    ConfigError,
    ExitCode,
    InsufficientDataError,
    InvestoError,
    NotImplementedYetError,
    TickerNotFoundError,
    UpstreamFetchError,
)

DESIGN = Path(__file__).parent.parent / "DESIGN.md"

_TAXONOMY = {
    ExitCode.SUCCESS: 0,
    ExitCode.TICKER_NOT_FOUND: 2,
    ExitCode.INSUFFICIENT_DATA: 3,
    ExitCode.UPSTREAM_FETCH_FAILURE: 4,
    ExitCode.CONFIG_ERROR: 5,
}


@pytest.mark.spec
@pytest.mark.parametrize(("code", "value"), list(_TAXONOMY.items()))
def test_codes_match_design_section_14(code: ExitCode, value: int) -> None:
    """The numbers are normative in DESIGN.md §14 and are not ours to renumber."""
    assert int(code) == value


@pytest.mark.spec
def test_design_section_14_still_states_these_numbers() -> None:
    """Read the numbers back out of DESIGN.md rather than trusting the table above.

    A duplicated constant drifts silently; this fails if §14 is edited without the enum
    following. It is a coarse check — it asserts the digit appears next to its phrase, not the
    full sentence — because a stricter match would break on rewording and get deleted.
    """
    text = DESIGN.read_text(encoding="utf-8")
    section = text.split("## 14. Operational notes", 1)[1]
    for digit, phrase in [
        (2, "ticker not found"),
        (3, "insufficient data"),
        (4, "upstream fetch failure"),
        (5, "config error"),
    ]:
        pattern = rf"{digit}\s+{re.escape(phrase)}"
        assert re.search(pattern, section, re.IGNORECASE), f"§14 no longer says '{digit} {phrase}'"


@pytest.mark.spec
def test_no_code_is_one() -> None:
    """1 is reserved for an unhandled exception, so it can keep meaning "investo has a bug"."""
    assert 1 not in {int(code) for code in ExitCode}


def test_codes_are_distinct() -> None:
    """An IntEnum silently aliases duplicate values, which would make two outcomes
    indistinguishable at the shell with nothing in the source to notice."""
    values = [int(code) for code in ExitCode]
    assert len(values) == len(set(values))


@pytest.mark.spec
def test_click_usage_error_collides_with_ticker_not_found() -> None:
    """Pin the known wart documented on `ExitCode`.

    Click exits 2 for a usage error and DESIGN.md §14 assigns 2 to "ticker not found", so the
    two are indistinguishable by code. The collision is implemented as designed rather than
    quietly renumbered — this test exists so it stays visible, and so that renumbering
    TICKER_NOT_FOUND is a deliberate act that fails a named test rather than a silent edit.
    """
    from click.exceptions import UsageError

    assert UsageError.exit_code == int(ExitCode.TICKER_NOT_FOUND)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ConfigError, ExitCode.CONFIG_ERROR),
        (TickerNotFoundError, ExitCode.TICKER_NOT_FOUND),
        (InsufficientDataError, ExitCode.INSUFFICIENT_DATA),
        (UpstreamFetchError, ExitCode.UPSTREAM_FETCH_FAILURE),
        (NotImplementedYetError, ExitCode.NOT_IMPLEMENTED),
    ],
)
def test_each_error_declares_its_code(error: type[InvestoError], code: ExitCode) -> None:
    assert error("x").exit_code == code


def test_every_error_subclass_declares_a_code() -> None:
    """A new error class cannot be added without stating how it exits.

    `InvestoError` has a fallback `exit_code`, so a subclass that forgets to set one inherits
    exit 4 and reports an upstream fetch failure that never happened. This walks the hierarchy
    instead of listing classes, so it covers subclasses added after it was written.
    """
    subclasses = InvestoError.__subclasses__()
    assert subclasses, "hierarchy should not be empty"
    for cls in subclasses:
        assert "exit_code" in vars(cls), f"{cls.__name__} does not declare its own exit_code"


def test_not_implemented_is_outside_the_design_taxonomy() -> None:
    """Scaffolding must never be confusable with a real outcome — especially not with exit 3,
    which promises a written report."""
    assert int(ExitCode.NOT_IMPLEMENTED) not in {int(code) for code in _TAXONOMY}
    assert int(ExitCode.NOT_IMPLEMENTED) > max(int(code) for code in _TAXONOMY)


def test_not_implemented_names_its_milestone() -> None:
    error = NotImplementedYetError.at("M3", "analyze AAPL")
    assert "analyze AAPL" in error.message
    assert error.hint is not None
    assert "M3" in error.hint


def test_hint_is_optional() -> None:
    """Absent when there is no action to name — a hint that restates the message trains
    people to stop reading hints."""
    assert InvestoError("something broke").hint is None

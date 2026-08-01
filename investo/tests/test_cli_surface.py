"""The CLI's flag surface, checked against README § Usage in both directions.

M0's exit criterion is that ``investo --help`` renders the full flag surface. "Full" is only
checkable against something, so these tests read README's usage block and compare it to what
the app actually accepts — a documented flag that does not exist fails, and an accepted flag
that is not documented fails too.

The second direction is the one that matters over time. A flag added in M4 and never written
down is how a CLI acquires undocumented behaviour that nobody dares change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from investo.cli import app, main
from investo.errors import ExitCode
from tests.conftest import VALID_USER_AGENT

README = Path(__file__).parent.parent / "README.md"

runner = CliRunner()

# Flags Click and typer provide, which README has no reason to list.
_BUILTIN_FLAGS = frozenset({"--help", "-h", "--version"})

# Which command owns each documented flag. README lists the `analyze` options as a block and
# then shows the other commands as one-liners, so ownership is not derivable from the text.
_FLAG_OWNER = {
    "--lookback": ("analyze", "facts"),
    "--out": ("analyze", "backtest"),
    "--cache-dir": ("analyze", "facts", "fetch", "backtest", "cache prune"),
    "--config": ("analyze", "facts", "fetch", "backtest", "cache prune"),
    "--llm": ("analyze",),
    "--peers": ("analyze",),
    "--assumptions": ("analyze",),
    "--as-of": ("analyze", "facts"),
    "--refresh": ("analyze", "facts", "fetch"),
    # M2. `report.json` on stdout rather than an `--out`: `facts` writes no files, and
    # `docs/m1/README.md` §3 declined `--json` on `fetch` on the grounds that report.json is the
    # machine-readable surface this project committed to — which is an argument for putting it here.
    "--json": ("facts",),
    "--explain": ("analyze",),
    "--brief": ("analyze",),
    "--older-than": ("cache prune",),
    "--universe": ("backtest",),
    "--start": ("backtest",),
    "--horizons": ("backtest",),
}

_COMMANDS = ("analyze", "facts", "fetch", "backtest", "cache prune")


def _usage_block() -> str:
    """The fenced block under README's `## Usage` heading."""
    body = README.read_text(encoding="utf-8").split("\n## Usage\n", 1)[1]
    return body.split("```", 2)[1]


def _documented_flags() -> set[str]:
    return set(re.findall(r"--[a-z][a-z-]*", _usage_block()))


def _help(*command: str) -> str:
    result = runner.invoke(app, [*command, "--help"])
    assert result.exit_code == 0, f"`{' '.join(command)} --help` exited {result.exit_code}"
    # Rich soft-wraps help text, so a long flag can be split across lines. Collapse whitespace
    # before matching or the assertions become a test of the terminal width.
    return re.sub(r"\s+", " ", result.output)


def _accepted_flags(*command: str) -> set[str]:
    return set(re.findall(r"--[a-z][a-z-]*", _help(*command)))


def _stderr(result: Result) -> str:
    """``result.stderr`` where Click separates the streams, else the combined output.

    Click 8.2 always captures stderr separately; 8.1 raises ``ValueError`` unless the runner was
    built with ``mix_stderr=False``. Falling back keeps the assertion meaningful on both rather
    than pinning a Click minor version for the sake of one test.
    """
    try:
        return result.stderr
    except ValueError:
        return result.output


# ---------------------------------------------------------------------------
# Help renders at all
# ---------------------------------------------------------------------------
@pytest.mark.surface
def test_root_help_renders_and_lists_every_command() -> None:
    output = _help()
    for command in ("analyze", "facts", "fetch", "backtest", "cache"):
        assert command in output


@pytest.mark.surface
def test_bare_invocation_shows_help() -> None:
    """`no_args_is_help`: typing `investo` prints help rather than failing blankly.

    It exits **2**, not 0 — Click raises `NoArgsIsHelpError`, a `UsageError`, and its exit code
    is not configurable without subclassing Click's exception machinery. Pinned rather than
    worked around: that is the same exit-2 overlap documented on `ExitCode` and raised as
    ROADMAP open question 7a, and a test asserting 0 here would quietly fail the day someone
    tried to fix it.
    """
    result = runner.invoke(app, [])
    assert "analyze" in result.output
    assert result.exit_code == 2


@pytest.mark.surface
@pytest.mark.parametrize("command", _COMMANDS)
def test_each_command_help_renders(command: str) -> None:
    assert _help(*command.split())


@pytest.mark.surface
def test_root_help_carries_the_disclaimer() -> None:
    """README § Disclaimer is not decoration. Someone running `--help` before their first
    report should see that the output is a model's, and will be wrong."""
    output = _help().lower()
    assert "not investment advice" in output


# ---------------------------------------------------------------------------
# README ↔ CLI, both directions
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.surface
def test_every_documented_flag_is_accepted_by_its_command() -> None:
    """Direction 1: README documents nothing the CLI does not implement."""
    for flag in sorted(_documented_flags()):
        owners = _FLAG_OWNER.get(flag)
        assert owners is not None, f"{flag} is in README § Usage but unowned in _FLAG_OWNER"
        for owner in owners:
            assert flag in _accepted_flags(*owner.split()), f"`investo {owner}` lacks {flag}"


@pytest.mark.spec
@pytest.mark.surface
@pytest.mark.parametrize("command", _COMMANDS)
def test_no_undocumented_flags(command: str) -> None:
    """Direction 2: the CLI accepts nothing README does not document.

    This is the check that keeps README honest as commands grow. Adding a flag means adding a
    line to README § Usage and an entry to `_FLAG_OWNER` — three seconds, and the alternative
    is a CLI whose real surface only the source knows.
    """
    undocumented = _accepted_flags(*command.split()) - _documented_flags() - _BUILTIN_FLAGS
    assert not undocumented, f"`investo {command}` accepts undocumented {sorted(undocumented)}"


@pytest.mark.surface
def test_flag_owner_table_has_no_stale_entries() -> None:
    """`_FLAG_OWNER` must not outlive README. A stale entry makes direction 1 vacuous for
    that flag — it is never looked up, so nothing checks it."""
    assert not set(_FLAG_OWNER) - _documented_flags()


@pytest.mark.surface
def test_readme_documents_the_exit_codes() -> None:
    """The taxonomy is user-visible behaviour, so it belongs in README, not only DESIGN.md."""
    body = README.read_text(encoding="utf-8")
    assert "Exit codes:" in body
    for code in (2, 3, 4, 5):
        assert f"`{code}`" in body


# ---------------------------------------------------------------------------
# Config errors reach the shell as exit 5
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize("command", ["analyze AAPL", "facts AAPL", "fetch AAPL"])
def test_missing_user_agent_exits_5(command: str) -> None:
    """ROADMAP M1 exit criterion, enforced from M0: "startup fails loudly if User-Agent is
    unset". Loudly means before any network call and with the variable named."""
    result = runner.invoke(app, command.split())
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "INVESTO_SEC_USER_AGENT" in result.output


def test_config_error_message_goes_to_stderr(configured: None) -> None:
    """Errors on stderr so `investo facts AAPL > out.txt` leaves them on the terminal."""
    result = runner.invoke(app, ["analyze", "AAPL", "--config", "/nonexistent/investo.toml"])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "not found" in _stderr(result)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("command", "milestone"),
    [
        ("backtest", "M7"),
    ],
)
def test_stub_exits_70_and_names_its_milestone(
    command: str, milestone: str, configured: None
) -> None:
    """Every unimplemented command parses and resolves config, then says which milestone fills it in.

    Exit 70 rather than 0: a script must not read "parsed successfully" as "ran".

    **`fetch` and `cache prune` left this list in M1, `facts` in M2 and `analyze` in M3**, which is
    the intended lifecycle — ROADMAP § Decided during design: *"Exit 70 for an unimplemented
    command... It disappears with the last stub."* Removing a row here is what landing a milestone
    looks like. **`backtest` is the last one**, and when M7 lands, `NotImplementedYetError` and
    `ExitCode.NOT_IMPLEMENTED` are deleted with it — along with this test and its companion below.
    """
    result = runner.invoke(app, command.split())
    assert result.exit_code == int(ExitCode.NOT_IMPLEMENTED)
    assert milestone in result.output


@pytest.mark.spec
def test_implemented_commands_do_not_report_not_implemented(configured: None) -> None:
    """The converse, and it is the half that rots silently.

    A command whose body has landed must never exit 70 again. Without this, a refactor that
    reintroduced a stub — or a merge that reverted one — would leave the suite green while
    `investo fetch AAPL` told the user the milestone had not shipped.

    Asserted on the *code*, not the output, and with no network configured: `fetch` will fail at
    exit 2, 4 or 5 depending on how far it gets, and any of those is fine. 70 is the only wrong
    answer.
    """
    for command in (
        ["fetch", "AAPL"],
        ["facts", "AAPL"],
        ["analyze", "AAPL"],
        ["cache", "prune", "--older-than", "90d"],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code != int(ExitCode.NOT_IMPLEMENTED), (
            f"`investo {' '.join(command)}` still reports exit 70; its body did not land"
        )


def test_stub_is_reached_only_after_config_resolves() -> None:
    """Ordering matters: config errors must win over the not-implemented notice.

    Otherwise a command would report "not implemented" for a run that would have failed at exit 5
    anyway, and the config layer would go unexercised.

    Uses `backtest`, the last remaining stub. This test was written against `fetch` in M0, moved to
    `facts` in M1 and to `analyze` in M2, and moves again here — which is the point: the property is
    about the *ordering* of config resolution against the stub raise, not about any one command, so
    its subject has to be whichever command is still unimplemented. When M7 lands it has no subject
    and is deleted.
    """
    result = runner.invoke(app, ["backtest"])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "M7" not in result.output


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_as_of_in_the_future_is_rejected(configured: None) -> None:
    """A point-in-time reconstruction as of tomorrow silently means "everything", which would
    make a backtest look clairvoyant."""
    result = runner.invoke(app, ["analyze", "AAPL", "--as-of", "2099-01-01"])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "future" in result.output


def test_as_of_today_is_accepted(configured: None) -> None:
    """The boundary is inclusive — today has happened.

    Asserted as "not a config error" rather than as a specific code, because `analyze`'s body now
    runs: with no network it will fail at 2 or 4 depending on how far it gets, and either is fine.
    Exit 5 is the only wrong answer, and it is the one a `>` where `>=` belongs would produce.
    """
    from datetime import date

    result = runner.invoke(app, ["analyze", "AAPL", "--as-of", date.today().isoformat()])
    assert result.exit_code != int(ExitCode.CONFIG_ERROR)


@pytest.mark.parametrize("value", ["01-01-2020", "2020/01/01", "yesterday"])
def test_as_of_rejects_other_date_formats(value: str, configured: None) -> None:
    result = runner.invoke(app, ["analyze", "AAPL", "--as-of", value])
    assert result.exit_code != int(ExitCode.NOT_IMPLEMENTED)


def test_lookback_below_the_minimum_is_rejected(configured: None) -> None:
    result = runner.invoke(app, ["analyze", "AAPL", "--lookback", "1y"])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "minimum" in result.output


def test_peers_accepts_a_comma_separated_list(configured: None) -> None:
    result = runner.invoke(app, ["analyze", "AAPL", "--peers", "MSFT,GOOG,AMZN"])
    assert result.exit_code != int(ExitCode.CONFIG_ERROR)


def test_peers_rejects_an_empty_entry(configured: None) -> None:
    """`--peers MSFT,,GOOG` is a shell-quoting mistake; dropping the blank hides it."""
    result = runner.invoke(app, ["analyze", "AAPL", "--peers", "MSFT,,GOOG"])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "empty entry" in result.output


def test_assumptions_file_must_exist(configured: None) -> None:
    result = runner.invoke(app, ["analyze", "AAPL", "--assumptions", "/nonexistent/a.toml"])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "not found" in result.output


def test_older_than_rejects_a_bad_duration(configured: None) -> None:
    result = runner.invoke(app, ["cache", "prune", "--older-than", "90 days"])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)


def test_older_than_is_required(configured: None) -> None:
    """Pruning with no age would empty the cache, so there is no safe default."""
    result = runner.invoke(app, ["cache", "prune"])
    assert result.exit_code != int(ExitCode.NOT_IMPLEMENTED)


@pytest.mark.spec
def test_backtest_accepts_a_one_year_horizon(configured: None) -> None:
    """1y is a documented horizon even though 1y is below `--lookback`'s minimum.

    Two different constraints share the `Ny` spelling — an estimation window needs 3 years of
    history, a forecast horizon does not. Validating horizons with `parse_lookback` would
    reject the default set, which is exactly the bug this pins.
    """
    result = runner.invoke(app, ["backtest", "--horizons", "1y,2y,5y"])
    assert result.exit_code == int(ExitCode.NOT_IMPLEMENTED)


@pytest.mark.parametrize("value", ["1y,,2y", "1q", "0y", "one-year"])
def test_backtest_rejects_bad_horizons(value: str, configured: None) -> None:
    result = runner.invoke(app, ["backtest", "--horizons", value])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)


@pytest.mark.spec
def test_bad_flag_value_does_not_report_an_upstream_failure(configured: None) -> None:
    """Exit 4 promises "upstream fetch failure after retries", so a malformed flag must not
    produce it.

    `InvestoError`'s base `exit_code` is 4, which means every raise site that forgets to pick a
    subclass reports a network failure that never happened. This is the test that catches that
    class of mistake rather than one instance of it.
    """
    for command in (
        ["analyze", "AAPL", "--as-of", "2099-01-01"],
        ["analyze", "AAPL", "--peers", "MSFT,,GOOG"],
        ["analyze", "AAPL", "--assumptions", "/nonexistent/a.toml"],
        ["cache", "prune", "--older-than", "90 days"],
        ["backtest", "--horizons", "1q"],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code != int(ExitCode.UPSTREAM_FETCH_FAILURE), command


# ---------------------------------------------------------------------------
# Flag plumbing
# ---------------------------------------------------------------------------
def test_cli_flags_override_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--out` and `--cache-dir` reach `Settings`, not just the parser.

    Checked by capture rather than by output, because M0 has no output to inspect — and a flag
    that parses but never reaches config is the exact defect this milestone could otherwise
    ship invisibly.
    """
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", VALID_USER_AGENT)
    monkeypatch.setenv("INVESTO_OUT_DIR", "/env/reports")
    seen: dict[str, object] = {}

    from investo import cli as cli_module

    original = cli_module.load_settings

    def spy(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(cli_module, "load_settings", spy)
    runner.invoke(app, ["analyze", "AAPL", "--out", str(tmp_path), "--cache-dir", str(tmp_path)])

    assert seen["out_dir"] == tmp_path
    assert seen["cache_dir"] == tmp_path


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------
def test_main_returns_the_exit_code(configured: None) -> None:
    """`main` reports codes rather than raising, so `python -m investo` and the console script
    agree.

    `backtest` rather than `fetch`, `facts` or `analyze`: the other three are implemented now, and
    this test needs a command whose exit code is known without a network round trip. It has moved
    once per milestone for that reason, and M7 will have to give it a different subject — a
    `--config` pointing at a missing file, most likely, which is exit 5 with no network either.
    """
    assert main(["backtest"]) == int(ExitCode.NOT_IMPLEMENTED)


def test_main_returns_zero_for_help() -> None:
    assert main(["--help"]) == int(ExitCode.SUCCESS)


def test_main_returns_config_error_code() -> None:
    assert main(["fetch", "AAPL"]) == int(ExitCode.CONFIG_ERROR)


def test_version_flag(configured: None) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "investo" in result.output

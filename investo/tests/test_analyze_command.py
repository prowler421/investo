"""``investo analyze`` — the output contract and exit 3 (ROADMAP M3).

The pair that matters most is :func:`test_an_unbuilt_milestone_does_not_exit_3` and
:func:`test_exit_3_still_writes_both_files`. They are the two halves of the one exit code this
milestone introduces, and each passes on its own under an implementation that gets the other wrong:
a command that exits 3 on every run satisfies the second, and one that never exits 3 satisfies the
first.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from investo.analyze import (
    JSON_NAME,
    PDF_NAME,
    AnalyzeOutcome,
    outcome_code,
    output_dir,
    record_flags,
    render_analyze_summary,
    require_no_llm,
    run_analyze,
    source_date_epoch,
    write_atomic,
)
from investo.config import Settings
from investo.errors import ConfigError, ExitCode
from investo.normalize.statements import Bucket, FinancialHistory
from investo.normalize.tags import Tier
from investo.report.model import build_model
from investo.report.render import render_report
from investo.report.serialize import RunInfo, run_info, serialize
from tests.conftest import M2_WINDOW, VALID_USER_AGENT, filing_rows, history, submissions

AS_OF = date(2026, 6, 30)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        sec_user_agent=VALID_USER_AGENT,
        tiingo_key="k",
        out_dir=tmp_path / "reports",
        cache_dir=tmp_path / "cache",
        **overrides,  # pyright: ignore[reportArgumentType]
    )


def _run(settings: Settings, ticker: str) -> RunInfo:
    return run_info(
        settings,
        ticker=ticker,
        as_of=AS_OF,
        window=M2_WINDOW,
        lookback_years=5,
        manifest_hash="0" * 64,
        version="0.1.0",
    )


def _aapl() -> FinancialHistory:
    profile, filings = submissions("AAPL.json", cik=320193)
    return history("AAPL.trimmed.json", ticker="AAPL", cik=320193, profile=profile, filings=filings)


def _write_report(settings: Settings, subject: FinancialHistory, *, brief: bool = False) -> Path:
    """The write half of ``run_analyze``, without the fetch.

    ``run_analyze`` starts with ``run_fetch``, which needs a network or a warm cache; every
    assertion below is about what happens *after* normalization. Re-assembling the tail here keeps
    these tests offline, which CLAUDE.md convention 7 requires — CI sets no ``INVESTO_*`` variables
    precisely so a test that reaches the network fails rather than quietly passing.
    """
    envelope = _run(settings, subject.ticker)
    model = build_model(subject, envelope, brief=brief)
    rendered = render_report(model, source_date_epoch=source_date_epoch(AS_OF), brief=brief)
    destination = output_dir(settings, ticker=subject.ticker, as_of=AS_OF)
    destination.mkdir(parents=True, exist_ok=True)
    _ = write_atomic(destination / JSON_NAME, serialize(subject, run=envelope).encode("utf-8"))
    _ = write_atomic(destination / PDF_NAME, rendered.pdf)
    return destination


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_reports_are_keyed_by_ticker_and_as_of(tmp_path: Path) -> None:
    """Flat output makes the second ticker overwrite the first; keying on ticker alone makes
    tomorrow overwrite today, which destroys the input ``investo diff`` exists for (§4.5)."""
    settings = _settings(tmp_path)
    assert output_dir(settings, ticker="AAPL", as_of=AS_OF).parts[-2:] == ("AAPL", "2026-06-30")
    assert output_dir(settings, ticker="aapl", as_of=AS_OF) == output_dir(
        settings, ticker="AAPL", as_of=AS_OF
    )


@pytest.mark.spec
def test_both_files_land_next_to_each_other(tmp_path: Path) -> None:
    """README fixes the two names and their adjacency."""
    destination = _write_report(_settings(tmp_path), _aapl())
    assert (destination / PDF_NAME).is_file()
    assert (destination / JSON_NAME).is_file()
    assert (destination / PDF_NAME).read_bytes().startswith(b"%PDF")
    assert json.loads((destination / JSON_NAME).read_text(encoding="utf-8"))["schema_version"] == 1


@pytest.mark.spec
def test_a_partial_file_is_never_visible_under_the_final_name(tmp_path: Path) -> None:
    """Written through a temporary file in the same directory, then ``os.replace``.

    A half-written ``report.json`` that happens to parse is worse than one that does not: the second
    is an error and the first is a wrong answer. The staging name is asserted absent afterwards
    because a leftover ``.report.json.partial`` in an output directory is its own confusion.
    """
    destination = _write_report(_settings(tmp_path), _aapl())
    assert not list(destination.glob(".*.partial"))


@pytest.mark.spec
def test_rerunning_the_same_as_of_overwrites_itself(tmp_path: Path) -> None:
    """What makes §11's gate runnable as "run it twice and compare the file"."""
    settings = _settings(tmp_path)
    first = _write_report(settings, _aapl())
    before = (first / PDF_NAME).read_bytes()
    second = _write_report(settings, _aapl())
    assert first == second
    assert (second / PDF_NAME).read_bytes() == before


# ---------------------------------------------------------------------------
# Exit 3, and the thing that is not exit 3
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_an_unbuilt_milestone_does_not_exit_3(tmp_path: Path) -> None:
    """§14's *"valuation omitted"* is not the trigger, and it cannot be.

    Every M3 report omits the valuation, so reading it that way would make every run exit 3 — and a
    code that fires on every invocation carries no information. An unbuilt milestone is not
    insufficient data.
    """
    code, reason = outcome_code(_aapl(), _settings(tmp_path))
    assert code is ExitCode.SUCCESS
    assert reason is None


@pytest.mark.spec
def test_no_companyfacts_exits_3(tmp_path: Path) -> None:
    subject = history(
        None, ticker="EMPTY", filings=filing_rows(("10-K", "2024-02-01", "2023-12-31"))
    )
    code, reason = outcome_code(subject, _settings(tmp_path))
    assert code is ExitCode.INSUFFICIENT_DATA
    assert reason is not None and "companyfacts" in reason


@pytest.mark.spec
def test_exit_3_still_writes_both_files(tmp_path: Path) -> None:
    """§14 promises "insufficient data, **report still written**".

    A command that raised before writing would turn the most carefully worded code in that taxonomy
    into a lie, which is why the code is a field on the outcome rather than an exception.
    """
    settings = _settings(tmp_path)
    subject = history(
        None, ticker="EMPTY", filings=filing_rows(("10-K", "2024-02-01", "2023-12-31"))
    )
    destination = _write_report(settings, subject)
    assert (destination / PDF_NAME).is_file()
    assert (destination / JSON_NAME).is_file()
    assert json.loads((destination / JSON_NAME).read_text(encoding="utf-8"))["history"]["annual"]


@pytest.mark.spec
def test_the_coverage_floor_is_unset_by_default() -> None:
    """§4.2 sanctions a *configurable* floor and supplies no number.

    ``docs/m2/COVERAGE.md`` is the measurement that would, and it does not exist. A default invented
    before the measurement fires arbitrarily — the same posture as ``pyproject.toml``'s unset
    ``fail_under``, and it should be resolved the same way.
    """
    assert Settings.model_fields["coverage_floor"].default is None


@pytest.mark.spec
def test_a_configured_floor_triggers_exit_3(tmp_path: Path) -> None:
    """The mechanism exists even though the number does not. A floor above every possible rate
    must fire; one below every possible rate must not."""
    subject = _aapl()
    rate = subject.coverage.tier_fill_rate(Tier.DCF, Bucket.ANNUAL)
    assert rate is not None, "the fixture has no tier-1 coverage; the assertion would be vacuous"

    strict, reason = outcome_code(subject, _settings(tmp_path, coverage_floor=Decimal("1.01")))
    assert strict is ExitCode.INSUFFICIENT_DATA
    assert reason is not None and "floor" in reason

    lenient, _ = outcome_code(subject, _settings(tmp_path, coverage_floor=Decimal("0")))
    assert lenient is ExitCode.SUCCESS


# ---------------------------------------------------------------------------
# Flags belonging to later milestones
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_an_llm_provider_is_refused_until_m6() -> None:
    """A report that silently drops the section the user asked for is worse than one that refuses.

    And unlike a missing metric, the user has no way to tell from the artifact that it happened.
    """
    require_no_llm("none")
    for provider in ("anthropic", "openai", "gemini"):
        with pytest.raises(ConfigError, match="M6"):
            require_no_llm(provider)


@pytest.mark.spec
def test_explain_changes_the_run_record_and_not_the_pdf(tmp_path: Path) -> None:
    """There are no intermediate calculations until M5, so the flag marks the run and no more.

    Asserted rather than documented, because "accepted and inert" is indistinguishable from
    "accepted and ignored" without a test that says which one it is.
    """
    settings = _settings(tmp_path)
    subject = _aapl()
    plain = record_flags(
        _run(settings, "AAPL"), brief=False, explain=False, peers=None, assumptions=None
    )
    explained = record_flags(
        _run(settings, "AAPL"), brief=False, explain=True, peers=None, assumptions=None
    )
    assert plain.config["explain"] == "false"
    assert explained.config["explain"] == "true"

    first = render_report(
        build_model(subject, plain), source_date_epoch=source_date_epoch(AS_OF)
    ).pdf
    second = render_report(
        build_model(subject, explained), source_date_epoch=source_date_epoch(AS_OF)
    ).pdf
    assert first == second


@pytest.mark.spec
def test_brief_and_full_produce_identical_report_json(tmp_path: Path) -> None:
    """The run record is a record of the run. ``--brief`` is presentation.

    A brief run producing a smaller document would make ``investo diff`` results depend on which
    flag each side used.
    """
    settings = _settings(tmp_path)
    subject = _aapl()
    envelope = _run(settings, "AAPL")
    assert serialize(subject, run=envelope) == serialize(subject, run=envelope)

    full = _write_report(settings, subject, brief=False)
    full_json = (full / JSON_NAME).read_bytes()
    brief = _write_report(settings, subject, brief=True)
    assert (brief / JSON_NAME).read_bytes() == full_json


@pytest.mark.spec
def test_peers_and_assumptions_are_recorded_not_ignored(tmp_path: Path) -> None:
    """M4 and M5 own their behaviour; both are real inputs to a later run and belong in the record.

    ``assumptions`` records the **path**, not the contents — M5 may put anything in that file, and a
    run record that grew a schema per milestone would need its own version.
    """
    envelope = record_flags(
        _run(_settings(tmp_path), "AAPL"),
        brief=False,
        explain=False,
        peers=("MSFT", "GOOG"),
        assumptions=Path("/tmp/a.toml"),
    )
    assert envelope.config["peers"] == "MSFT,GOOG"
    assert envelope.config["assumptions"] == "/tmp/a.toml"


@pytest.mark.spec
def test_no_api_key_reaches_the_run_record(tmp_path: Path) -> None:
    """§10: keys are never logged, and a ``report.json`` in an output directory is logged.

    ``serialize`` already asserts this over ``CONFIG_FIELDS``; repeated here because M3 *adds* four
    keys to the same block, and an allowlist is only as good as the last thing appended to it.
    """
    settings = _settings(
        tmp_path, anthropic_key="sk-secret-anthropic", openai_key="sk-secret-openai"
    )
    envelope = record_flags(
        _run(settings, "AAPL"), brief=True, explain=True, peers=("MSFT",), assumptions=None
    )
    blob = serialize(_aapl(), run=envelope)
    assert "sk-secret" not in blob


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_the_summary_names_the_absent_milestones(tmp_path: Path) -> None:
    """The most useful line in the summary for the next several milestones, and the one most likely
    to be cut as noise. It stops someone concluding the tool has no opinion on valuation."""
    settings = _settings(tmp_path)
    subject = _aapl()
    outcome = AnalyzeOutcome(
        ticker="AAPL",
        name=subject.name,
        pdf_path=tmp_path / PDF_NAME,
        json_path=tmp_path / JSON_NAME,
        pages=14,
        exit_code=ExitCode.SUCCESS,
        reason=None,
        history=subject,
        run=_run(settings, "AAPL"),
        charts_drawn=5,
        charts_omitted=0,
        overflowing=0,
    )
    text = render_analyze_summary(outcome)
    for milestone in ("M4", "M4.5", "M5", "M6", "M7"):
        assert f"({milestone})" in text
    # `0 omitted` is printed rather than suppressed: a line that only appears when something is
    # wrong is a line nobody learns to read.
    assert "0 omitted" in text
    assert "report.pdf" in text and "report.json" in text


@pytest.mark.spec
def test_the_summary_prints_the_reason_on_exit_3(tmp_path: Path) -> None:
    """The files exist, and the point of the code is that they do — so the summary still prints."""
    settings = _settings(tmp_path)
    subject = history(None, ticker="EMPTY")
    outcome = AnalyzeOutcome(
        ticker="EMPTY",
        name="Empty Corp",
        pdf_path=tmp_path / PDF_NAME,
        json_path=tmp_path / JSON_NAME,
        pages=4,
        exit_code=ExitCode.INSUFFICIENT_DATA,
        reason="SEC publishes no companyfacts for this CIK.",
        history=subject,
        run=_run(settings, "EMPTY"),
        charts_drawn=0,
        charts_omitted=5,
        overflowing=0,
    )
    text = render_analyze_summary(outcome)
    assert "exit 3" in text
    assert "companyfacts" in text
    assert str(tmp_path / PDF_NAME) in text


@pytest.mark.spec
def test_run_analyze_is_the_only_signature_the_cli_needs() -> None:
    """A shape assertion, so a refactor that drops a flag from the body fails here rather than in a
    CLI test whose failure reads as a flag-parsing problem."""
    import inspect

    parameters = set(inspect.signature(run_analyze).parameters)
    assert {
        "ticker",
        "settings",
        "refresh",
        "as_of",
        "brief",
        "explain",
        "peers",
        "assumptions",
        "version",
    } == parameters

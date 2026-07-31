"""`investo facts`, end to end from fixtures: the table, `--json`, and the exit code.

ROADMAP M2's goal is that `investo facts AAPL --lookback 5y` prints clean annual and quarterly
statements with a coverage report. That is a claim about the whole command — the wiring, the absence
paths and the exit code — so it is tested through the CLI rather than through `build_history`.

Two mechanics, inherited from `test_fetch_command.py` and worth restating once. `respx` registers no
routes by default and raises on an unregistered request, so a command reaching a URL nobody registered
fails loudly rather than silently returning an absence. And `run_fetch` takes no clock, so the fake one
goes in at `client.SystemClock`: at 5 req/s the limiter would otherwise sleep 200ms per request through
every test in this file.

**`--as-of` is passed on every invocation**, and not for point-in-time reasons. The fixtures describe
2015-2019 (AAPL) and 2024-2025 (the newer ones), so a run at today's date would apply a window that
excludes the data and assert nothing about normalization. Pinning `--as-of` makes the window a property
of the test rather than of the day the suite runs — which is the same reason `conftest.M2_WINDOW`
exists.

The one thing this file asserts that no unit test can: **`facts` never exits 3.** Exit 3 means
"insufficient data, report still written" (§14) and this command writes no report, so every absence
here — no `companyfacts` for the CIK, six quarters of history, a metric that resolves to nothing in
every period — has to come back 0 with the reason printed.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import respx
from click.testing import Result
from typer.testing import CliRunner

from investo.cli import app
from investo.errors import ExitCode
from investo.ingest.edgar import client as edgar_client
from investo.ingest.edgar.client import (
    companyfacts_url,
    submissions_page_url,
    submissions_url,
    tickers_exchange_url,
)
from tests.conftest import VALID_USER_AGENT, FakeClock, fixture_bytes

runner = CliRunner()

STOOQ_URL = "https://stooq.com/q/d/l/"

APPLE = 320193
IPO = 1908259
NOPERIODIC = 1000052
TIER2 = 1000048

AAPL_AS_OF = "2019-12-31"
"""Inside the AAPL fixture's history, so a 5y window covers FY2016-FY2019."""


def _ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body)


def _absent() -> httpx.Response:
    return httpx.Response(404, content=b'{"error": "not found"}')


@pytest.fixture(autouse=True)
def fake_edgar_clock(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> FakeClock:
    """Every `EdgarClient` in this module gets the fake clock — see the module docstring."""

    def factory() -> FakeClock:
        return clock

    monkeypatch.setattr(edgar_client, "SystemClock", factory)
    return clock


@pytest.fixture(autouse=True)
def stooq_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A price provider that needs no key, and the User-Agent SEC requires.

    `price_provider` defaults to `tiingo`, which raises `ConfigError` before any request when its key
    is missing — correct behaviour, and not what any test here is about.
    """
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", VALID_USER_AGENT)
    monkeypatch.setenv("INVESTO_PRICE_PROVIDER", "stooq")


def _register_common() -> None:
    _ = respx.get(tickers_exchange_url()).mock(
        return_value=_ok(fixture_bytes("edgar", "company_tickers_exchange.trimmed.json"))
    )
    _ = respx.get(STOOQ_URL).mock(return_value=_ok(fixture_bytes("prices", "stooq", "AAPL.csv")))


def _register_apple(*, facts: bool = True) -> None:
    _ = respx.get(submissions_url(APPLE)).mock(
        return_value=_ok(fixture_bytes("edgar", "submissions", "AAPL.json"))
    )
    _ = respx.get(submissions_page_url("CIK0000320193-submissions-001.json")).mock(
        return_value=_ok(fixture_bytes("edgar", "submissions", "AAPL-submissions-001.json"))
    )
    _ = respx.get(companyfacts_url(APPLE)).mock(
        return_value=_ok(fixture_bytes("edgar", "companyfacts", "AAPL.trimmed.json"))
        if facts
        else _absent()
    )


def _register(cik: int, *, companyfacts: str, submissions: str | None = None) -> None:
    """One company, with its submissions payload absent unless a fixture is named.

    A 404 on submissions is a documented exit-0 outcome, and three of the tickers in the exchange
    fixture have no submissions payload at all — so the absent case is the default here rather than an
    exception.
    """
    _ = respx.get(companyfacts_url(cik)).mock(
        return_value=_ok(fixture_bytes("edgar", "companyfacts", companyfacts))
    )
    _ = respx.get(submissions_url(cik)).mock(
        return_value=_ok(fixture_bytes("edgar", "submissions", submissions))
        if submissions
        else _absent()
    )


def _run(*args: str) -> Result:
    return runner.invoke(app, ["facts", *args])


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------
@respx.mock
@pytest.mark.spec
def test_facts_prints_the_series_the_coverage_and_the_findings() -> None:
    """ROADMAP M2's goal, as one assertion: statements plus a coverage report.

    Asserted on the *structure* of the output rather than on any figure — the header identifies the
    company, both buckets appear, coverage names both tiers, and the findings are printed. A test that
    grepped for one revenue number would pass on an output missing the coverage block entirely, which
    is half of what the milestone promises.
    """
    _register_common()
    _register_apple()
    result = _run("AAPL", "--as-of", AAPL_AS_OF, "--lookback", "5y")

    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    output = result.output
    assert "AAPL" in output
    assert "Apple Inc." in output
    assert "CIK 320193" in output
    assert "SIC 3571" in output
    assert "annual" in output and "quarterly" in output
    assert "revenue" in output
    assert "tier 1 (DCF)" in output and "tier 2 (F/Z/M)" in output
    assert "findings" in output


@respx.mock
@pytest.mark.spec
def test_absent_is_printed_as_a_value_not_as_a_blank_row() -> None:
    """A metric with no data prints a dash on every period and `absent` in the tag column.

    A blank row is indistinguishable from a rendering bug — and this command exists to let someone
    check which tag won, so a row that says nothing is worse than no row. `AAPL.trimmed.json` has no
    capex, cash or long-term-debt tag, so there are several.
    """
    _register_common()
    _register_apple()
    result = _run("AAPL", "--as-of", AAPL_AS_OF, "--lookback", "5y")

    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    assert "absent" in result.output
    assert "capex" in result.output
    assert "—" in result.output, "a dash is the value for a period with no fact"


@respx.mock
@pytest.mark.spec
def test_the_winning_tag_is_printed_next_to_the_series() -> None:
    """Which tag won is the thing this command exists to let you check (§9.1).

    And a stitched series prints both, joined — Apple's revenue crosses the ASC 606 boundary inside a
    5y window ending 2019, so the row names `SalesRevenueNet` *and* the 606 tag. A renderer that
    printed only the first would describe the transition backwards.
    """
    _register_common()
    _register_apple()
    result = _run("AAPL", "--as-of", AAPL_AS_OF, "--lookback", "5y")

    assert "us-gaap:SalesRevenueNet" in result.output
    assert "us-gaap:RevenueFromContra" in result.output, "truncated, but present"
    assert "→" in result.output, "the stitch is rendered as a transition"


@respx.mock
@pytest.mark.spec
def test_the_coverage_block_prints_its_spine_origin() -> None:
    """A percentage against an `OBSERVED` denominator must never be printed without saying so.

    Here the spine comes from the filing history, so the label says `filings` — the positive case,
    which is the one that would be silently dropped by a renderer that only annotated the fallback.
    """
    _register_common()
    _register_apple()
    result = _run("AAPL", "--as-of", AAPL_AS_OF, "--lookback", "5y")

    assert "spine: filings" in result.output


@respx.mock
@pytest.mark.spec
def test_observed_spine_is_printed() -> None:
    """The circular denominator, labelled in the coverage block **and** in the findings.

    `EXNP`'s only forms are `S-1/A`, `EFFECT` and `8-K`, so there is no periodic filing to build a
    denominator from and coverage is measured against the periods the facts themselves carry. A 100%
    figure from that spine is the single most misleading number this milestone could produce, which is
    why it is labelled twice.
    """
    _register_common()
    _register(NOPERIODIC, companyfacts="NOPERIODIC.trimmed.json", submissions="NOPERIODIC.json")
    result = _run("EXNP", "--as-of", "2026-06-30", "--lookback", "3y")

    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    assert "spine: observed" in result.output
    assert "circular" in result.output
    assert "spine_observed" in result.output


@respx.mock
@pytest.mark.spec
def test_findings_are_printed_in_full_rather_than_counted() -> None:
    """"3 findings" is a number nobody acts on.

    `EXT2` produces a stitch, an exclusivity switch, a summed SG&A and a scope mismatch, so the block
    has to carry several codes and their details. Asserted on codes *and* on a detail fragment, because
    printing the codes alone would be a list nobody can act on either.
    """
    _register_common()
    _register(TIER2, companyfacts="TIER2.trimmed.json")
    result = _run("EXT2", "--as-of", "2025-06-30", "--lookback", "5y")

    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    assert "series_stitched" in result.output
    assert "exclusivity_switch" in result.output
    assert "sga_composed" in result.output
    assert "net_income_scope_mismatch" in result.output
    assert "noncontrolling interest" in result.output, "the detail, not just the code"


@respx.mock
def test_the_header_states_the_quarters_of_history() -> None:
    """§5.1 gates the valuation on it at two thresholds, so a reader should see it before M5 refuses.

    `EXNC` has exactly six quarters, which is the wrong side of the 12-quarter floor — and printing it
    on the header is what makes the later refusal legible rather than surprising.
    """
    _register_common()
    _register(IPO, companyfacts="IPO.trimmed.json")
    result = _run("EXNC", "--as-of", "2025-09-30", "--lookback", "5y")

    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    assert "6 quarters" in result.output


# ---------------------------------------------------------------------------
# the exit code
# ---------------------------------------------------------------------------
@respx.mock
@pytest.mark.spec
def test_thin_coverage_exits_0() -> None:
    """**`facts` never exits 3.** Exit 3 promises a written report and this command writes none.

    `EXNC` has six quarters, no annual periods at all, and no submissions payload — three separate
    reasons a valuation would be refused later. All three are printed and the run succeeds, because
    whether an absence is fatal is a question for the command that needs the data, and this one reports
    on it.
    """
    _register_common()
    _register(IPO, companyfacts="IPO.trimmed.json")
    result = _run("EXNC", "--as-of", "2025-09-30", "--lookback", "5y")

    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    assert result.exit_code != int(ExitCode.INSUFFICIENT_DATA)
    assert "submissions_absent" in result.output


@respx.mock
@pytest.mark.spec
def test_companyfacts_404_exits_0() -> None:
    """A CIK with no XBRL facts is an absence, and every metric is absent with it.

    M1's rule — *"a 404 and a missing tag are absences, not failures"* — one command further. The
    history is still built: the filings are unaffected, so the spine and the coverage denominator
    survive, and the finding says why the numerator is zero.
    """
    _register_common()
    _register_apple(facts=False)
    result = _run("AAPL", "--as-of", AAPL_AS_OF, "--lookback", "5y")

    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    assert "companyfacts_absent" in result.output
    assert "Apple Inc." in result.output, "identity survives an absent companyfacts"


@respx.mock
@pytest.mark.spec
def test_an_unknown_ticker_still_exits_2() -> None:
    """The one absence that is not an absence: a ticker that resolves to nothing has no company.

    Exit 2 rather than 0, because there is nothing to report on — and unlike every other condition in
    this file, no amount of coverage reporting would make the run meaningful.
    """
    _register_common()
    result = _run("NOTATICKER", "--as-of", AAPL_AS_OF, "--lookback", "5y")

    assert result.exit_code == int(ExitCode.TICKER_NOT_FOUND)


@respx.mock
@pytest.mark.spec
def test_a_non_nasdaq_ticker_exits_2() -> None:
    """Present in SEC's file, listed on NYSE. The exchange check is not optional.

    `JPM` is in the exchange fixture for exactly this test — an implementation that resolved the CIK
    and forgot the exchange would otherwise produce a perfectly good report for a company outside the
    documented universe.
    """
    _register_common()
    result = _run("JPM", "--as-of", AAPL_AS_OF, "--lookback", "5y")

    assert result.exit_code == int(ExitCode.TICKER_NOT_FOUND)


def test_a_bad_lookback_exits_5_before_any_request() -> None:
    """Config errors win over everything, and nothing is fetched.

    No `respx.mock` decorator on this one, deliberately: if the command reached the network it would
    raise a connection error rather than exit 5, so the absence of a router is the assertion.
    """
    result = _run("AAPL", "--lookback", "1y")
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "minimum" in result.output


def test_an_as_of_in_the_future_exits_5() -> None:
    """A point-in-time reconstruction as of tomorrow silently means "everything".

    Which in a backtest looks like clairvoyance. Rejected at the command boundary, which is the only
    place that reads a clock at all.
    """
    result = _run("AAPL", "--as-of", "2099-01-01")
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "future" in result.output


# ---------------------------------------------------------------------------
# --json
# ---------------------------------------------------------------------------
@respx.mock
@pytest.mark.spec
def test_json_flag_writes_report_json_to_stdout() -> None:
    """The M2 end of §11's determinism gate: a document a consumer can read, on stdout.

    Without this flag M2 would ship a serializer no command emits, and the gate would have no
    end-to-end path until M3. No `--out`: `facts` writes no files, and stdout composes with
    redirection.
    """
    _register_common()
    _register_apple()
    result = _run("AAPL", "--as-of", AAPL_AS_OF, "--lookback", "5y", "--json")

    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    document = json.loads(result.output)
    assert document["schema_version"] == 1
    assert document["company"]["cik"] == 320193
    assert document["run"]["as_of"] == AAPL_AS_OF
    assert document["run"]["lookback_years"] == 5
    assert document["forecast"] is None, "declared, and empty until M5"


@respx.mock
@pytest.mark.spec
def test_json_and_the_table_are_alternatives_not_additions() -> None:
    """`--json` replaces the table, so the output is parseable without stripping a preamble.

    A document with a human-readable header above it is a document every consumer has to know to skip
    — and `investo facts AAPL --json > report.json` is the composition the flag exists for.
    """
    _register_common()
    _register_apple()
    result = _run("AAPL", "--as-of", AAPL_AS_OF, "--lookback", "5y", "--json")

    assert result.output.lstrip().startswith("{")
    assert "tier 1 (DCF)" not in result.output


@respx.mock
@pytest.mark.spec
def test_the_json_document_carries_the_manifest_hash_of_this_run() -> None:
    """§9.1's cache fingerprint, over the entries **this run** used.

    Hashing the whole manifest file would make an AAPL report's hash change when someone fetches MSFT,
    which would break the one property the field is for: two runs over the same data agree, and a run
    over different data does not.
    """
    _register_common()
    _register_apple()
    result = _run("AAPL", "--as-of", AAPL_AS_OF, "--lookback", "5y", "--json")

    document = json.loads(result.output)
    assert len(document["run"]["manifest_hash"]) == 64, "a sha256 hex digest"


@respx.mock
@pytest.mark.spec
def test_values_in_the_emitted_document_are_strings() -> None:
    """The `Decimal` rule survives the command boundary, which is where a `float` would be introduced.

    Every layer below this has an AST rule forbidding `float`; the command is the first place a value is
    handed to something that formats. Asserting it here as well as in `test_serialize` is the
    difference between "the serializer is correct" and "the output is correct".
    """
    _register_common()
    _register_apple()
    result = _run("AAPL", "--as-of", AAPL_AS_OF, "--lookback", "5y", "--json")

    document = json.loads(result.output, parse_float=Decimal)
    revenue = document["history"]["annual"]["revenue"]
    assert revenue, "the window covers Apple's annual revenue"
    for entry in revenue:
        assert isinstance(entry["value"], str)
    assert '"value": "391035000000.01"' in result.output


@respx.mock
def test_two_runs_over_one_cache_emit_identical_documents() -> None:
    """The gate, at the command level: the second run makes no request and produces the same bytes.

    The warm half is testable by omission — the cache is populated by the first invocation, and the
    second runs against a router that has served every route already. If the command re-fetched and the
    payload were byte-identical the document would match anyway, so the assertion that carries weight
    is the equality *plus* `test_serialize`'s subprocess comparison.
    """
    _register_common()
    _register_apple()
    first = _run("AAPL", "--as-of", AAPL_AS_OF, "--lookback", "5y", "--json")
    second = _run("AAPL", "--as-of", AAPL_AS_OF, "--lookback", "5y", "--json")

    assert first.exit_code == int(ExitCode.SUCCESS), first.output
    assert second.exit_code == int(ExitCode.SUCCESS), second.output
    assert first.output == second.output

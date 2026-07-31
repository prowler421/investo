"""`investo fetch`, end to end from fixtures: the four ROADMAP M1 exit criteria.

ROADMAP M1 is done when a cold fetch of five tickers stays under the rate limit, a warm run makes
zero HTTP calls, startup fails loudly if the User-Agent is unset, and the price provider is
swappable via config. Each of those is a claim about the whole command rather than about a parser,
so each is tested through `run_fetch` or the CLI rather than through a unit.

Two mechanics worth stating once. `respx` registers no routes by default and raises on an
unregistered request, which is what makes "zero HTTP calls" testable **by omission** — the warm run
below runs inside a router with nothing registered. And `run_fetch` takes no clock, so the fake one
is injected at `client.SystemClock`: a CLI command has no business threading a test seam through its
signature, and the module attribute is the seam that already exists.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from investo.cli import app
from investo.config import Settings, load_settings
from investo.errors import ExitCode
from investo.fetch import render_summary, run_fetch
from investo.ingest.cache import Cache
from investo.ingest.edgar import client as edgar_client
from investo.ingest.edgar.client import companyfacts_url, submissions_url, tickers_exchange_url
from tests.conftest import VALID_USER_AGENT, FakeClock, fixture_bytes

runner = CliRunner()

AS_OF = date(2026, 7, 31)
"""Passed explicitly so price selection does not depend on the day the suite runs."""

STOOQ_URL = "https://stooq.com/q/d/l/"
TIINGO_URL = "https://api.tiingo.com/tiingo/daily/aapl/prices"
TIINGO_KEY = "tiingo-test-key-91b0c4"

RATE_TOLERANCE = 1e-6
"""Float slack for the rate assertion, and it is needed rather than defensive.

The limiter sleeps for `next_allowed - now` over accumulated `float` time, so after a few requests
the elapsed figure lands a few ULPs either side of `(requests - 1) / rate`. Measured: the fourth
ticker's elapsed comes out `0.19999999999999996` against an interval of `0.2`. An exact comparison
would fail there for a reason that has nothing to do with the rate, and the slack is nine orders of
magnitude below the 0.2s spacing it is checking.
"""

APPLE = 320193
ARXS = 2093536
UNPOPULATED = (36104, 1063761, 1908259)
"""EXBK, EXPT and EXNC: NASDAQ rows in the exchange file with no submissions fixture.

They answer 404, which is the point — a 404 is an absence, so the five-ticker run exercises the
absence path as well as the happy one.
"""


def _absent() -> httpx.Response:
    return httpx.Response(404, content=b'{"error": "not found"}')


def _ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body)


def _register_tickers() -> None:
    body = fixture_bytes("edgar", "company_tickers_exchange.trimmed.json")
    _ = respx.get(tickers_exchange_url()).mock(return_value=_ok(body))


def _register_apple() -> None:
    submissions = fixture_bytes("edgar", "submissions", "AAPL.json")
    facts = fixture_bytes("edgar", "companyfacts", "AAPL.trimmed.json")
    _ = respx.get(submissions_url(APPLE)).mock(return_value=_ok(submissions))
    _ = respx.get(companyfacts_url(APPLE)).mock(return_value=_ok(facts))


def _register_arxs() -> None:
    submissions = fixture_bytes("edgar", "submissions", "ARXS.json")
    facts = fixture_bytes("edgar", "companyfacts", "ARXS.json")
    _ = respx.get(submissions_url(ARXS)).mock(return_value=_ok(submissions))
    _ = respx.get(companyfacts_url(ARXS)).mock(return_value=_ok(facts))


def _register_absent(cik: int) -> None:
    _ = respx.get(submissions_url(cik)).mock(return_value=_absent())
    _ = respx.get(companyfacts_url(cik)).mock(return_value=_absent())


def _register_stooq() -> None:
    body = fixture_bytes("prices", "stooq", "AAPL.csv")
    _ = respx.get(STOOQ_URL).mock(return_value=_ok(body))


def _register_tiingo() -> None:
    body = fixture_bytes("prices", "tiingo", "AAPL.json")
    _ = respx.get(TIINGO_URL).mock(return_value=_ok(body))


def _env(monkeypatch: pytest.MonkeyPatch, *, provider: str = "stooq") -> None:
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", VALID_USER_AGENT)
    monkeypatch.setenv("INVESTO_PRICE_PROVIDER", provider)
    monkeypatch.setenv("INVESTO_TIINGO_KEY", TIINGO_KEY)


def _settings(monkeypatch: pytest.MonkeyPatch, *, provider: str = "stooq") -> Settings:
    _env(monkeypatch, provider=provider)
    return load_settings()


@pytest.fixture(autouse=True)
def fake_edgar_clock(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> FakeClock:
    """Give every `EdgarClient` built in this module the fake clock.

    Autouse, because `conftest`'s rule is that no fixture reads the wall clock: at 5 req/s the
    limiter sleeps 200ms per request against `time.sleep`, so a handful of end-to-end runs would
    spend seconds asleep proving nothing. `run_fetch` takes no clock — a CLI command has no business
    threading a test seam through its signature — so the seam is the module attribute
    `EdgarClient.__init__` already looks up.
    """

    def factory() -> FakeClock:
        return clock

    monkeypatch.setattr(edgar_client, "SystemClock", factory)
    return clock


# ---------------------------------------------------------------------------
# Criterion 1: a cold fetch of five tickers stays under the rate limit
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_cold_fetch_for_five_tickers_stays_under_the_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
) -> None:
    """ROADMAP M1 exit criterion 1, asserted as the token bucket's own invariant.

    With capacity 1, a client may emit at most `1 + elapsed * rate` requests: one immediately, then
    one per `1/rate` thereafter. The assertion is per ticker because `fetch` builds a client per
    run, so the aggregate across five runs is allowed five "first" requests — writing the aggregate
    bound instead would be the looser claim, and it would pass for an implementation whose limiter
    reset mid-run.

    A `FakeClock` rather than the wall clock: time only moves when something sleeps, so the elapsed
    figure is exactly the limiter's own spacing and the test costs no seconds. `max(sleeps)` closes
    the other side — a limiter that slept for a minute per request would satisfy the rate bound and
    make the five-minute cold-run target in DESIGN.md §14 unreachable.
    """
    settings = _settings(monkeypatch)
    rate = settings.edgar_requests_per_second
    total = 0

    with respx.mock:
        _register_tickers()
        _register_apple()
        _register_arxs()
        for cik in UNPOPULATED:
            _register_absent(cik)
        _register_stooq()

        for ticker in ("AAPL", "ARXS", "EXBK", "EXPT", "EXNC"):
            before = clock.monotonic()
            result = run_fetch(ticker, settings=settings, as_of=AS_OF)
            elapsed = clock.monotonic() - before
            assert result.requests - 1 <= elapsed * rate + RATE_TOLERANCE, ticker
            total += result.requests

    assert total > 5, "a run that fetched almost nothing would satisfy any rate bound"
    assert clock.sleeps, "the limiter has to have actually spaced something"
    assert max(clock.sleeps) <= 1.0 / rate + RATE_TOLERANCE


# ---------------------------------------------------------------------------
# Criterion 2: a warm run makes zero HTTP calls
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_second_whole_command_run_makes_no_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """ROADMAP M1 exit criterion 2, tested by omission: the second router has no routes.

    So a single request would raise rather than merely be counted, and the assertion on
    `respx.calls` is the belt to that braces. The manifest hash is asserted equal across the two
    runs because that is the property the appendix value exists for — a warm run and the cold run
    before it saw the same data, and `docs/m1/06-testing.md` §6 makes it one of M1's three
    determinism gates.

    Named separately from `test_cache_warm::test_second_fetch_makes_no_requests`, which the
    guarantee table assigns that name to. That one exercises `EdgarClient` alone; this one exercises
    the whole command including the price adapter, which uses a different HTTP client and could
    satisfy the client-level test while still hitting the network here.
    """
    settings = _settings(monkeypatch)

    with respx.mock:
        _register_tickers()
        _register_apple()
        _register_stooq()
        cold = run_fetch("AAPL", settings=settings, as_of=AS_OF)

    assert cold.requests > 0

    with respx.mock:
        warm = run_fetch("AAPL", settings=settings, as_of=AS_OF)
        assert respx.calls.call_count == 0

    assert warm.requests == 0
    assert warm.manifest_hash == cold.manifest_hash

    statuses = {source.label: source.status for source in warm.sources}
    assert statuses["tickers_exchange"] == "cached"
    assert statuses["submissions"] == "cached"
    assert statuses["companyfacts"] == "cached"


# ---------------------------------------------------------------------------
# Criterion 3: startup fails loudly without a User-Agent
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_fetch_without_user_agent_exits_5() -> None:
    """DESIGN.md §4.1 and CLAUDE.md convention 2: no default, and no request before the check.

    The exit code is the load-bearing assertion, and it is stronger than it looks: no routes are
    registered, so any attempted request raises inside the command and the CLI would exit 1 instead
    of 5. `respx.calls` is asserted as well, because reading "exit 5" alone leaves a reader guessing
    which of the two guarantees is being pinned.

    `conftest` clears the whole `INVESTO_*` environment, so this test cannot pass or fail on
    whatever the developer has exported.
    """
    with respx.mock:
        result = runner.invoke(app, ["fetch", "AAPL"])
        assert respx.calls.call_count == 0

    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "INVESTO_SEC_USER_AGENT" in result.output


# ---------------------------------------------------------------------------
# Criterion 4: the price provider is swappable via config
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize("provider", ["stooq", "tiingo"])
def test_price_provider_is_swappable_via_the_config_file(provider: str, tmp_path: Path) -> None:
    """ROADMAP M1 exit criterion 4, driven from a TOML file rather than the environment.

    A setting that only works from the environment is half a feature, and with `INVESTO_*` cleared
    by `conftest` the file is provably the only source. `yfinance` is exercised in
    `test_prices_contract` instead, because it needs an optional package rather than a route.
    """
    config = tmp_path / "fixtures" / "investo.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f'sec_user_agent = "{VALID_USER_AGENT}"\n'
        f'price_provider = "{provider}"\n'
        f'tiingo_key = "{TIINGO_KEY}"\n'
    )
    _ = config.write_text(body, encoding="utf-8")
    settings = load_settings(config_file=config)

    with respx.mock:
        _register_tickers()
        _register_apple()
        _register_stooq()
        _register_tiingo()
        result = run_fetch("AAPL", settings=settings, as_of=AS_OF)

    assert result.prices is not None
    assert result.prices.provider == provider
    assert f"prices ({provider})" in [source.label for source in result.sources]


# ---------------------------------------------------------------------------
# Exit codes for the two outcomes `fetch` distinguishes
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_companyfacts_404_exits_0_and_prints_an_absent_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 is an absence, not a failure — `docs/m1/README.md` §4, and DESIGN.md §14's split.

    `fetch` exits 0 and prints the gap; whether the gap is fatal depends on what needs it, which
    is `analyze`'s question. Exit 4 here would report an upstream fetch failure that did not
    happen, and a NASDAQ filer with no `companyfacts` has told us something true.
    """
    _env(monkeypatch)

    with respx.mock:
        _register_tickers()
        _ = respx.get(submissions_url(APPLE)).mock(
            return_value=_ok(fixture_bytes("edgar", "submissions", "AAPL.json"))
        )
        _ = respx.get(companyfacts_url(APPLE)).mock(return_value=_absent())
        _register_stooq()
        result = runner.invoke(app, ["fetch", "AAPL"])

    assert result.exit_code == int(ExitCode.SUCCESS)
    assert "absent" in result.output
    assert "companyfacts" in result.output


@pytest.mark.spec
def test_non_nasdaq_ticker_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """README and DESIGN.md §14: exit 2 covers "not NASDAQ", and it costs exactly one request.

    The request count is asserted because the exchange check is a property of the file already in
    hand — an implementation that resolved the CIK and then fetched `submissions` before noticing
    would still exit 2, and would spend SEC's rate budget learning something it could have read.
    """
    _env(monkeypatch)

    with respx.mock:
        _register_tickers()
        result = runner.invoke(app, ["fetch", "JPM"])
        assert respx.calls.call_count == 1

    assert result.exit_code == int(ExitCode.TICKER_NOT_FOUND)
    assert "NYSE" in result.output


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_summary_prints_fetched_at_per_source_the_count_and_the_manifest_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three are load-bearing rather than cosmetic, per `fetch.py`'s own docstring.

    `fetched_at` per source, because a warm run's value is the whole point of the cache and a stale
    entry the user cannot see is one they will trust. The request count, because it is the number
    the rate-limit criterion is about. The manifest hash, because it answers "did this report see
    the same data as that one" — and it is the hash of the entries *this run used*, not of the file,
    or an AAPL report's hash would change when someone fetched MSFT.
    """
    settings = _settings(monkeypatch)

    with respx.mock:
        _register_tickers()
        _register_apple()
        _register_stooq()
        result = run_fetch("AAPL", settings=settings, as_of=AS_OF)

    summary = render_summary(result)

    assert result.sources
    for source in result.sources:
        assert source.fetched_at is not None, source.label
        assert source.label in summary
        assert source.fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ") in summary

    assert f"{result.requests} requests" in summary
    assert result.manifest_hash
    assert f"manifest {result.manifest_hash[:8]}" in summary


@pytest.mark.spec
def test_summary_reports_market_cap_with_the_classes_it_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DESIGN.md §5.4 requires the report to state which share classes were counted.

    Which means the class labels have to survive the whole path: `tickers.py` supplies them per CIK,
    `fetch` passes them down, and `Derivation.note` carries them to the page. Any one of those links
    can be dropped without breaking the number, which is why the assertion is on the note.
    """
    settings = _settings(monkeypatch)

    with respx.mock:
        _register_tickers()
        _register_apple()
        _register_stooq()
        result = run_fetch("AAPL", settings=settings, as_of=AS_OF)

    assert result.market_cap is not None
    value, derivation = result.market_cap
    assert value > 0
    assert derivation.rule == "market_cap"
    assert derivation.note is not None
    assert "AAPL" in derivation.note
    assert "market cap" in render_summary(result)


@pytest.mark.spec
def test_a_filer_without_a_dei_section_reports_market_cap_as_an_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live-confirmed path: `ffd` and `us-gaap` and no `dei` at all, so no cover-page count.

    Exit 0 with a printed absence, not a zero. A market cap of 0 would flow into every multiple in
    report section 3 and into the valuation sub-score, and it would look computed.
    """
    settings = _settings(monkeypatch)

    with respx.mock:
        _register_tickers()
        _register_arxs()
        _register_stooq()
        result = run_fetch("ARXS", settings=settings, as_of=AS_OF)

    assert result.market_cap is None
    assert any("market cap" in note for note in result.absent)
    assert "dei:EntityCommonStockSharesOutstanding" in "\n".join(result.absent)


# ---------------------------------------------------------------------------
# cache prune
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_cache_prune_prints_its_prune_report(
    tmp_path: Path,
    configured: None,
) -> None:
    """A prune that reports nothing is a prune the user runs twice.

    Two entries for one key, pruned at `--older-than 0d`: the superseded one goes, the newest one
    survives *regardless of age*, and the blob only the superseded entry referenced is collected.
    Asserting the counts rather than the exit code is what makes this a test of the report — an
    implementation that printed zeros would exit 0 too.
    """
    root = tmp_path / "prune-cache"
    cache = Cache(root)
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    key = Cache.key_for("GET", url, None)
    for body in (b'{"generation": 1}', b'{"generation": 2}'):
        _ = cache.put(
            key=key,
            url=url,
            method="GET",
            params={},
            status=200,
            headers={"content-type": "application/json"},
            body=body,
        )

    result = runner.invoke(
        app,
        ["cache", "prune", "--older-than", "0d", "--cache-dir", str(root)],
    )

    assert result.exit_code == int(ExitCode.SUCCESS)
    assert "pruned 1 entry" in result.output
    assert "kept 1" in result.output
    assert "removed 1 blob(s)" in result.output

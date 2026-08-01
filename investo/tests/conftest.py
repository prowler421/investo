"""Shared fixtures.

The whole suite runs with the ``INVESTO_*`` environment cleared. Without that, a developer who
exports ``INVESTO_SEC_USER_AGENT`` in their shell — which everyone working on this will — makes
``test_missing_user_agent_is_config_error`` pass for the wrong reason locally and fail in CI,
or worse, the reverse. Autouse, so a test cannot forget.

M1 adds the payload fixtures, a fake clock, and a cache in ``tmp_path``. Two rules those follow:

- **No fixture reads the wall clock.** :class:`FakeClock` is injected into every client under test,
  because a rate limiter that sleeps against real time cannot be tested in under a second per
  request, and a retry policy tested against a real socket is not tested.
- **respx registers no routes by default.** A request to an unregistered route raises, which is what
  makes ROADMAP M1's *"warm run makes zero HTTP calls"* testable **by omission**: register nothing,
  and any request fails the test.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from investo.domain.models import RawFact
from investo.domain.provenance import Accession, Derivation, SourceContext
from investo.ingest.cache import Cache
from investo.ingest.edgar.companyfacts import CompanyFacts, parse_companyfacts
from investo.ingest.edgar.submissions import CompanyProfile, FilingRow, parse_submissions
from investo.normalize.statements import FinancialHistory, build_history

VALID_USER_AGENT = "Investo test suite tests@investo.invalid"
"""A User-Agent that passes validation. ``.invalid`` is RFC 2606-reserved and, unlike
``example.com``, is not on the rejected-placeholder list — so it exercises the accept path
without being an address anyone might mistake for real."""

FIXTURES = Path(__file__).parent / "fixtures"

FETCHED_AT = datetime(2026, 7, 31, 11, 2, 21, tzinfo=UTC)
"""A fixed, timezone-aware fetch timestamp for every parser test.

Fixed because a ``SourceRef`` built from ``datetime.now()`` makes a parser's output differ between
runs, and DESIGN.md §11 makes byte-identical output a gate from M3. Aware because a naive timestamp
in a provenance record means something different on every machine — ``SourceRef`` rejects one.
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Remove every ``INVESTO_*`` variable and run from an empty directory.

    ``chdir`` matters as much as the variable clearing: ``Settings`` reads ``.env`` and
    ``investo.toml`` relative to the working directory, so a suite run from the repo root would
    pick up whatever the developer has there.
    """
    for key in [k for k in os.environ if k.startswith("INVESTO_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # typer renders help through rich, which soft-wraps to the terminal width. At 80 columns a
    # long flag can break mid-token, so `test_no_undocumented_flags` would be measuring the
    # terminal rather than the CLI. NO_COLOR keeps ANSI escapes out of the matched text.
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the one required variable, so a test can exercise something past config."""
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", VALID_USER_AGENT)


# ---------------------------------------------------------------------------
# clock
# ---------------------------------------------------------------------------
class FakeClock:
    """A monotonic clock that only moves when something sleeps.

    Satisfies :class:`~investo.ingest.edgar.client.Clock`. Every sleep is recorded, which is what
    lets the rate-limit test assert *spacing* rather than only total elapsed time — the weaker
    assertion passes for an implementation that emits everything at once and then sleeps.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        """Move time without recording a sleep — for simulating elapsed wall time."""
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    """A fresh cache under ``tmp_path``. Never the developer's ``.cache``."""
    return Cache(tmp_path / "cache")


# ---------------------------------------------------------------------------
# payload fixtures
# ---------------------------------------------------------------------------
def fixture_bytes(*parts: str) -> bytes:
    """Read a fixture verbatim.

    Bytes, not text, and not parsed: the parsers take bytes, and a test that hands them a re-encoded
    string is testing a different input than production sees.
    """
    return (FIXTURES.joinpath(*parts)).read_bytes()


def fixture_json(*parts: str) -> Any:
    return json.loads(fixture_bytes(*parts))


def context(url: str = "https://data.sec.gov/test", cik: int | None = None) -> SourceContext:
    """A :class:`SourceContext` with the fixed timestamp."""
    return SourceContext(url=url, fetched_at=FETCHED_AT, cik=cik)


@pytest.fixture
def tickers_body() -> bytes:
    return fixture_bytes("edgar", "company_tickers_exchange.trimmed.json")


@pytest.fixture
def aapl_companyfacts() -> bytes:
    return fixture_bytes("edgar", "companyfacts", "AAPL.trimmed.json")


@pytest.fixture
def arxs_companyfacts() -> bytes:
    """The live-shape small payload: `ffd` first, **no `dei`**, absent `start`, `null` `fy`."""
    return fixture_bytes("edgar", "companyfacts", "ARXS.json")


@pytest.fixture
def arxs_submissions() -> bytes:
    """Every awkward value observed live, in one committable document."""
    return fixture_bytes("edgar", "submissions", "ARXS.json")


@pytest.fixture
def aapl_submissions() -> bytes:
    """Main payload with a populated `files[]` — the pagination case."""
    return fixture_bytes("edgar", "submissions", "AAPL.json")


@pytest.fixture
def aapl_submissions_page() -> bytes:
    """An overflow page: flat columnar, no `filings` wrapper."""
    return fixture_bytes("edgar", "submissions", "AAPL-submissions-001.json")


@pytest.fixture
def undeclared_403_body() -> bytes:
    return fixture_bytes("edgar", "malformed", "undeclared_403.txt")


# ---------------------------------------------------------------------------
# M2 — parsed payloads, and one place that builds a history
# ---------------------------------------------------------------------------
# The M2 suite works over *parsed* objects rather than bytes, because `build_history`'s signature
# takes `CompanyFacts` and `CompanyProfile` — a normalization layer that could take a ticker is one
# that could fetch. These helpers are here rather than in each module so twelve test files cannot
# drift on how a fixture is loaded, which is the one thing that would make their assertions
# incomparable.
M2_WINDOW = (date(2015, 1, 1), date(2026, 6, 30))
"""A window wide enough for every fixture, so a test that is not about windowing says nothing about it.

Fixed rather than derived from `date.today()`: the whole M2 pipeline is a function of its inputs, and a
window that moved with the calendar would make one assertion in this suite fail on one day of the year.
"""


def company_facts(name: str, *, cik: int | None = None) -> CompanyFacts:
    """Parse a `companyfacts` fixture by file name."""
    return parse_companyfacts(fixture_bytes("edgar", "companyfacts", name), source=context(cik=cik))


def submissions(
    name: str, *, cik: int | None = None
) -> tuple[CompanyProfile, tuple[FilingRow, ...]]:
    """Parse a `submissions` fixture, returning the profile and `filings.recent`."""
    profile, recent, _ = parse_submissions(
        fixture_bytes("edgar", "submissions", name), source=context(cik=cik)
    )
    return profile, recent


def filing_rows(*specs: tuple[str, str, str | None]) -> tuple[FilingRow, ...]:
    """Build `(form, filed, report_date)` rows without a payload.

    The spine's four construction rules are about *forms and report dates*, and a JSON fixture per
    case would bury that in sixteen columns of irrelevant detail. Where a test is about a real
    payload's awkwardness — `ARXS`'s `reportDate: ""` — it uses the payload instead.
    """
    rows: list[FilingRow] = []
    for position, (form, filed, report_date) in enumerate(specs):
        rows.append(
            FilingRow(
                accession=Accession.parse(f"000000000{position % 10}-20-00000{position % 10}"),
                form=form,
                filed=date.fromisoformat(filed),
                report_date=date.fromisoformat(report_date) if report_date else None,
                accepted_at=None,
                primary_document="x.htm",
                items=(),
                items_raw="",
                is_xbrl=True,
                is_inline_xbrl=True,
                size=1,
            )
        )
    return tuple(rows)


def history(
    fixture: str | None,
    *,
    ticker: str = "TEST",
    cik: int = 1000000,
    name: str = "Test Corp",
    profile: CompanyProfile | None = None,
    filings: Sequence[FilingRow] = (),
    window: tuple[date, date] = M2_WINDOW,
    as_of: date | None = None,
    market_cap: tuple[Decimal, Derivation] | None = None,
    coverage_floor: Decimal | None = None,
) -> FinancialHistory:
    """Build a `FinancialHistory` from a fixture name. `None` exercises the absent-payload path."""
    return build_history(
        company_facts(fixture, cik=cik) if fixture is not None else None,
        ticker=ticker,
        cik=cik,
        name=name,
        profile=profile,
        filings=filings,
        window=window,
        as_of=as_of,
        market_cap=market_cap,
        coverage_floor=coverage_floor,
    )


def raw_facts(fixture: str, *taxonomy_tag: tuple[str, str]) -> tuple[RawFact, ...]:
    """Raw facts for specific `(taxonomy, tag)` keys, for the unit-level pipeline tests."""
    parsed = company_facts(fixture)
    return tuple(fact for key in taxonomy_tag for fact in parsed.get(*key))


@pytest.fixture
def throttled_403_body() -> bytes:
    return fixture_bytes("edgar", "malformed", "throttled_403.txt")

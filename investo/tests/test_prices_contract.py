"""One contract, three adapters — plus the aliasing bug the contract exists to forbid.

`docs/m1/05-prices.md` §1 makes ROADMAP M1's *"all three adapters returning identical schemas"*
into six properties, and the interesting one is that `adj_close` is `Optional`. Stooq supplies no
adjusted close, and the tempting way to satisfy "identical schemas" is to alias `close` into the
field: every adapter then returns a fully populated struct and a naive schema check passes, while
beta is estimated over five years of *unadjusted* weekly returns and every dividend and split in
the window reads as a real return. Nothing in the report would say so, because the field was
populated.
"""

from __future__ import annotations

import builtins
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx

from investo.config import Settings, load_settings
from investo.errors import ConfigError, ExitCode, UpstreamFetchError
from investo.ingest.cache import Cache
from investo.ingest.prices.base import (
    PriceHttp,
    PriceProvider,
    PriceSeries,
    provider_for,
    weekdays_between,
)
from investo.ingest.prices.stooq import StooqProvider
from investo.ingest.prices.tiingo import TiingoProvider
from investo.ingest.prices.yfinance_ import PARTIAL_HISTORY_FLOOR, YFinanceProvider
from tests.conftest import VALID_USER_AGENT, fixture_bytes, fixture_json

START = date(2026, 7, 24)
END = date(2026, 7, 31)
"""The window the three cassettes cover: Friday to Friday, six trading days."""

TIINGO_URL = "https://api.tiingo.com/tiingo/daily/aapl/prices"
STOOQ_URL = "https://stooq.com/q/d/l/"
TIINGO_KEY = "tiingo-test-key-91b0c4"

MONETARY_FIELDS = ("close", "adj_close", "open", "high", "low")


def _assign(target: object, name: str, value: object) -> None:
    """Set an attribute through a variable name, so ruff's B010 and the type checker stay quiet."""
    setattr(target, name, value)


# ---------------------------------------------------------------------------
# The yfinance seam: `yfinance.download`, and a frame-like object
# ---------------------------------------------------------------------------
class _FakeRow:
    """One row of the frame. `yfinance_._to_bars` only ever indexes it by column name."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def __getitem__(self, column: str) -> object:
        return self._values[column]


class _FakeFrame:
    """The three attributes of pandas' surface that `yfinance_._to_bars` touches.

    A real `DataFrame` is not used because pandas is not a dependency — it arrives only with the
    `yfinance` extra — and building the adapter's input by hand is what makes the adapter's
    *contract* with pandas explicit: `empty`, `columns`, and `iterrows()` yielding `(index, row)`.
    """

    def __init__(self, records: Sequence[dict[str, Any]]) -> None:
        self._records = list(records)
        first = self._records[0] if self._records else {}
        self.columns: tuple[str, ...] = tuple(name for name in first if name != "Date")

    @property
    def empty(self) -> bool:
        return not self._records

    def iterrows(self) -> Iterator[tuple[date, _FakeRow]]:
        for record in self._records:
            yield date.fromisoformat(str(record["Date"])), _FakeRow(record)


class _FakeYFinance:
    """Records what the adapter asked for, so the request can be asserted as well as the
    response."""

    def __init__(self, frame: _FakeFrame) -> None:
        self._frame = frame
        self.calls: list[dict[str, object]] = []

    def download(
        self,
        ticker: str,
        *,
        start: str,
        end: str,
        auto_adjust: bool,
        actions: bool,
        progress: bool,
        threads: bool,
    ) -> _FakeFrame:
        self.calls.append(
            {
                "ticker": ticker,
                "start": start,
                "end": end,
                "auto_adjust": auto_adjust,
                "actions": actions,
                "progress": progress,
                "threads": threads,
            }
        )
        return self._frame


def _install_yfinance(
    monkeypatch: pytest.MonkeyPatch, records: Sequence[dict[str, Any]]
) -> _FakeYFinance:
    fake = _FakeYFinance(_FakeFrame(records))
    module = ModuleType("yfinance")
    _assign(module, "download", fake.download)
    monkeypatch.setitem(sys.modules, "yfinance", module)
    return fake


def _cassette_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = fixture_json("prices", "yfinance", "AAPL.json")
    return records


def _records_for(days: Sequence[str]) -> list[dict[str, Any]]:
    """Synthetic bars, for the row-count boundary where the six-bar cassette is the wrong size."""
    return [
        {
            "Date": day,
            "Open": 210.0,
            "High": 212.0,
            "Low": 209.0,
            "Close": 211.0,
            "Adj Close": 210.5,
            "Volume": 41_000_000,
        }
        for day in days
    ]


# ---------------------------------------------------------------------------
# HTTP seam
# ---------------------------------------------------------------------------
def _http(router: respx.Router, *, cache: Cache | None = None) -> PriceHttp:
    """`PriceHttp` over a respx router, through httpx's own transport seam.

    The transport is injected rather than patched globally, because these tests assert on the
    *request* — Tiingo's token has to be in a header and nowhere else — and an injected transport is
    the only place a request can be inspected before it leaves.
    """
    return PriceHttp(cache=cache, transport=httpx.MockTransport(router.handler))


def _settings(
    monkeypatch: pytest.MonkeyPatch, provider: str, *, key: str | None = None
) -> Settings:
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", VALID_USER_AGENT)
    monkeypatch.setenv("INVESTO_PRICE_PROVIDER", provider)
    if key is not None:
        monkeypatch.setenv("INVESTO_TIINGO_KEY", key)
    return load_settings()


# ---------------------------------------------------------------------------
# The contract, once, over all three
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Fetched:
    """A series and the window it was asked for, so the contract test can check both."""

    name: str
    series: PriceSeries


@pytest.fixture(params=["tiingo", "yfinance", "stooq"])
def fetched(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Fetched:
    """One `PriceSeries` per adapter, all from the cassettes in `tests/fixtures/prices/`."""
    name = str(request.param)
    settings = _settings(monkeypatch, name, key=TIINGO_KEY)
    router = respx.Router()
    provider: PriceProvider

    if name == "tiingo":
        _ = router.get(TIINGO_URL).mock(
            return_value=httpx.Response(200, content=fixture_bytes("prices", "tiingo", "AAPL.json"))
        )
        provider = TiingoProvider(settings, http=_http(router))
    elif name == "stooq":
        _ = router.get(STOOQ_URL).mock(
            return_value=httpx.Response(200, content=fixture_bytes("prices", "stooq", "AAPL.csv"))
        )
        provider = StooqProvider(settings, http=_http(router))
    else:
        _ = _install_yfinance(monkeypatch, _cassette_records())
        provider = YFinanceProvider(settings)

    return Fetched(name=name, series=provider.daily("AAPL", start=START, end=END))


@pytest.mark.spec
def test_every_adapter_satisfies_the_same_contract(fetched: Fetched) -> None:
    """ROADMAP M1: three adapters, one schema — stated as six properties, checked as six.

    The last one is the violation test for CLAUDE.md convention 8 on this path. Every adapter
    reaches `Decimal` by a different route — a JSON parse hook, `Decimal(csv_field)`, and
    `repr(float)` out of pandas — so "prices are `Decimal`" is three separate claims that only one
    assertion can cover. `isinstance(x, float)` is spelled out rather than left implied by
    `isinstance(x, Decimal)`, because the two are not each other's complement for a subclass
    someone adds later.
    """
    series = fetched.series
    days = [bar.day for bar in series.bars]

    assert days, "an empty series makes every assertion below vacuous"
    assert all(earlier < later for earlier, later in zip(days, days[1:], strict=False))
    assert len(set(days)) == len(days)
    assert all(START <= day <= END for day in days)
    assert series.adjusted == all(bar.adj_close is not None for bar in series.bars)
    assert series.provider == fetched.name
    assert series.ticker == "AAPL"

    for bar in series.bars:
        for field in MONETARY_FIELDS:
            value = getattr(bar, field)
            if value is None:
                continue
            assert isinstance(value, Decimal), f"{fetched.name}.{field} is {type(value).__name__}"
            assert not isinstance(value, float), f"{fetched.name}.{field} is a float"
        assert bar.volume is None or isinstance(bar.volume, int)


@pytest.mark.spec
def test_provider_name_matches_the_adapters_own_name(fetched: Fetched) -> None:
    """`PriceSeries.provider` is what the appendix prints, so it must not be a second spelling.

    Two places naming the same provider is how a report comes to say `yahoo` in the appendix and
    `yfinance` in the coverage table.
    """
    assert fetched.series.source.form == f"price:{fetched.name}"


# ---------------------------------------------------------------------------
# Stooq: `None`, not an alias
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_stooq_adj_close_is_none_not_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stooq reports no adjusted close rather than aliasing the raw close into the field.

    This test deliberately does **not** assert `bar.adj_close != bar.close`. That is the lazy
    spelling, and it passes whether the field is `None` or genuinely different — so it would go
    green against the aliasing bug it exists to catch, on a fixture whose closes and adjusted
    closes differ. `is None` is the only spelling that fails on the alias.

    `adjusted is False` is asserted alongside because the two travel together: a populated
    `adj_close` with `adjusted=False` would still feed a beta to any caller that read the bars
    directly.
    """
    settings = _settings(monkeypatch, "stooq")
    router = respx.Router()
    _ = router.get(STOOQ_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("prices", "stooq", "AAPL.csv"))
    )

    series = StooqProvider(settings, http=_http(router)).daily("AAPL", start=START, end=END)

    assert series.bars
    for bar in series.bars:
        assert bar.adj_close is None
    assert series.adjusted is False


@pytest.mark.spec
def test_stooq_rejects_a_plain_text_body_for_an_unknown_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stooq answers an unknown symbol with plain text and HTTP 200, not a 404.

    Without the header check the caller receives an empty series and records "no price history" — an
    absence — when the truth is a rejected request. DESIGN.md §8 needs those two distinguishable,
    because conflating them is what hides survivorship bias.
    """
    settings = _settings(monkeypatch, "stooq")
    router = respx.Router()
    _ = router.get(STOOQ_URL).mock(return_value=httpx.Response(200, content=b"Exceeded the daily"))

    with pytest.raises(UpstreamFetchError) as caught:
        _ = StooqProvider(settings, http=_http(router)).daily("NOPE", start=START, end=END)
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE


# ---------------------------------------------------------------------------
# Tiingo: the key, and where it must not appear
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_tiingo_without_a_key_exits_5_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing key -> `ConfigError` (exit 5), raised **before any request**.

    Same shape as the User-Agent rule: a config problem detected at startup rather than after a
    fetch has begun. The request count is what makes this a violation test — asserting only that
    the exception was raised would pass for an implementation that fetched first, got a 401, and
    mapped it to a `ConfigError`, which would burn a request against a daily quota to learn
    something it already knew.
    """
    settings = _settings(monkeypatch, "tiingo")
    assert settings.tiingo_key is None

    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"[]")

    router = respx.Router()
    _ = router.get(TIINGO_URL).mock(side_effect=respond)

    with pytest.raises(ConfigError) as caught:
        _ = TiingoProvider(settings, http=_http(router))

    assert caught.value.exit_code == ExitCode.CONFIG_ERROR
    assert "INVESTO_TIINGO_KEY" in (caught.value.hint or "")
    assert seen == [], "the key check must run before the network, not after it"


@pytest.mark.spec
def test_tiingo_sends_the_key_as_a_header_and_never_in_a_url(
    monkeypatch: pytest.MonkeyPatch, cache: Cache, tmp_path: Path
) -> None:
    """`Authorization: Token <key>`, not `?token=<key>`.

    DESIGN.md §10: API keys go via env only, never committed, never logged — and a cache manifest
    is a file on disk. A query-parameter token would land in the manifest's `url` field and in the
    cache key, which also means rotating a key would silently invalidate every cached price
    series.

    So three assertions: the header carries it, the request URL does not, and the manifest — read
    back off disk rather than through the `Cache` API — contains it nowhere. The cache key is
    recomputed from the URL and params alone, which is what proves the token is not an input to it.
    """
    settings = _settings(monkeypatch, "tiingo", key=TIINGO_KEY)
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=fixture_bytes("prices", "tiingo", "AAPL.json"))

    router = respx.Router()
    _ = router.get(TIINGO_URL).mock(side_effect=respond)

    _ = TiingoProvider(settings, http=_http(router, cache=cache)).daily(
        "AAPL", start=START, end=END
    )

    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == f"Token {TIINGO_KEY}"
    assert TIINGO_KEY not in str(seen[0].url)

    manifest = (tmp_path / "cache" / "manifest.jsonl").read_text(encoding="utf-8")
    assert manifest
    assert TIINGO_KEY not in manifest

    expected_key = Cache.key_for(
        "GET", TIINGO_URL, {"startDate": START.isoformat(), "endDate": END.isoformat()}
    )
    assert expected_key in manifest


# ---------------------------------------------------------------------------
# yfinance: partial history, and the optional extra
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_yfinance_short_series_raises_and_names_the_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial history that looks complete is the dominant yfinance failure.

    Throttling returns a short series with HTTP 200, so the row count is validated rather than
    trusted. The message is asserted, not just the exception type: a genuinely recent IPO also
    returns few bars, the check cannot tell the two apart, and the user can — so a message that only
    said "too few bars" would send someone hunting for a network fault that is not there.
    """
    settings = _settings(monkeypatch, "yfinance")
    _ = _install_yfinance(monkeypatch, _cassette_records())
    long_window_start = date(2026, 1, 1)
    expected = weekdays_between(long_window_start, END)
    assert len(_cassette_records()) < PARTIAL_HISTORY_FLOOR * expected

    with pytest.raises(UpstreamFetchError) as caught:
        _ = YFinanceProvider(settings).daily("AAPL", start=long_window_start, end=END)

    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE
    assert "throttling" in caught.value.message
    assert "listing history" in caught.value.message
    assert str(expected) in caught.value.message


@pytest.mark.spec
def test_yfinance_row_count_boundary_is_inclusive_of_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`len(bars) < floor * expected` — so exactly at the floor is **accepted**.

    Ten weekdays at a 0.9 floor makes nine bars the boundary. A `<=` where `<` belongs rejects a
    complete series that happened to lose one bar to a market holiday, which is the failure this
    boundary exists to permit; both sides are asserted, because a test at 6 and 152 bars passes
    either way.
    """
    settings = _settings(monkeypatch, "yfinance")
    window_start = date(2026, 7, 20)
    weekdays = [
        "2026-07-20",
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
        "2026-07-27",
        "2026-07-28",
        "2026-07-29",
        "2026-07-30",
    ]
    assert weekdays_between(window_start, END) == 10
    assert len(weekdays) == 9

    _ = _install_yfinance(monkeypatch, _records_for(weekdays))
    at_the_floor = YFinanceProvider(settings).daily("AAPL", start=window_start, end=END)
    assert len(at_the_floor.bars) == 9

    _ = _install_yfinance(monkeypatch, _records_for(weekdays[1:]))
    with pytest.raises(UpstreamFetchError):
        _ = YFinanceProvider(settings).daily("AAPL", start=window_start, end=END)


@pytest.mark.spec
def test_yfinance_pins_auto_adjust_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """DESIGN.md §4.3: `auto_adjust` back-adjusts, so two pulls on different dates disagree.

    Left to the library default it has flipped between yfinance releases, and the symptom is
    historical closes that change when a dividend is paid — which the cache would then faithfully
    record as two different truths.
    """
    settings = _settings(monkeypatch, "yfinance")
    fake = _install_yfinance(monkeypatch, _cassette_records())

    _ = YFinanceProvider(settings).daily("AAPL", start=START, end=END)

    assert len(fake.calls) == 1
    assert fake.calls[0]["auto_adjust"] is False
    assert fake.calls[0]["end"] == "2026-08-01", "`end` is exclusive in yfinance's API"


@pytest.mark.spec
def test_missing_yfinance_package_is_a_config_error_naming_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """yfinance is an optional extra, so its absence is a configuration problem, not a crash.

    The import is blocked here rather than relying on the package genuinely being absent, because
    `uv sync --extra yfinance` on a developer machine would otherwise make this test silently stop
    testing anything.
    """
    settings = _settings(monkeypatch, "yfinance")
    real_import = builtins.__import__

    def guarded(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "yfinance":
            raise ImportError("No module named 'yfinance'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "yfinance", raising=False)
    monkeypatch.setattr(builtins, "__import__", guarded)

    with pytest.raises(ConfigError) as caught:
        _ = YFinanceProvider(settings).daily("AAPL", start=START, end=END)

    assert caught.value.exit_code == ExitCode.CONFIG_ERROR
    assert "extra" in caught.value.message
    assert "--extra yfinance" in (caught.value.hint or "")


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize("name", ["tiingo", "yfinance", "stooq"])
def test_provider_for_resolves_every_literal_from_the_environment(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROADMAP M1: the price provider is swappable via config, and all three literals resolve.

    `Settings.price_provider` is a `Literal`, so a fourth name is a pydantic error rather than a
    registry miss — which is why this asserts the three that exist rather than that an unknown one
    fails.
    """
    settings = _settings(monkeypatch, name, key=TIINGO_KEY)
    assert provider_for(settings).name == name


@pytest.mark.spec
@pytest.mark.parametrize("name", ["tiingo", "yfinance", "stooq"])
def test_provider_for_resolves_from_a_config_file_too(name: str, tmp_path: Path) -> None:
    """A setting that only works from the environment is half a feature.

    `docs/m1/05-prices.md` §5 asks for this specifically. Written to a path outside
    `default_config_paths()` and passed explicitly, so the test cannot pass by accident through
    project-local discovery — and with the `INVESTO_*` environment cleared by `conftest`, the TOML
    file is provably the only source.
    """
    config = tmp_path / "fixtures" / "investo.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'sec_user_agent = "{VALID_USER_AGENT}"\n'
        f'price_provider = "{name}"\n'
        f'tiingo_key = "{TIINGO_KEY}"\n',
        encoding="utf-8",
    )

    settings = load_settings(config_file=config)

    assert settings.price_provider == name
    assert provider_for(settings).name == name


# ---------------------------------------------------------------------------
# weekdays_between
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 7, 27), date(2026, 7, 31), 5),  # Monday to Friday
        (date(2026, 7, 27), date(2026, 8, 2), 5),  # a whole week, weekend included
        (date(2026, 7, 24), date(2026, 7, 24), 1),  # one Friday, inclusive of both ends
        (date(2026, 7, 25), date(2026, 7, 25), 0),  # one Saturday
        (date(2026, 7, 25), date(2026, 7, 26), 0),  # a bare weekend
        (date(2026, 7, 24), date(2026, 7, 31), 6),  # the cassette window
        (date(2026, 7, 31), date(2026, 7, 24), 0),  # end before start
    ],
)
def test_weekdays_between_at_its_boundaries(start: date, end: date, expected: int) -> None:
    """Inclusive of both endpoints, and zero rather than negative when the window is inverted.

    A weekday count rather than a market calendar is deliberate — the only consumer needs 10%
    accuracy — but the endpoints still have to be right: an exclusive `end` shifts yfinance's
    partial-history floor by one bar for every window, and an inverted window returning a negative
    number would make `len(bars) < 0.9 * negative` accept anything at all.
    """
    assert weekdays_between(start, end) == expected

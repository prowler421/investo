"""The token bucket, with an injected clock.

DESIGN.md §4.1 sets the limiter at ~5 req/s against SEC's documented cap of 10, and its reasoning is
the reason this file asserts spacing rather than politeness: *the penalty for being slightly too
fast is minutes of downtime, and the reward for being exactly at the limit is nothing.* The downtime
is not only ours either — SEC throttles the address, and an office or a CI runner shares one.

**Every test here asserts the spacing between requests, not the total elapsed time.** Asserting only
elapsed time is strictly weaker: it passes for an implementation that fires all five requests in the
first instant and then sleeps for a second, which is precisely the burst SEC's monitoring is looking
for. The recorded stamps come from inside the mocked transport, so they are the moments requests
were actually made.

The clock is :class:`tests.conftest.FakeClock`, which only moves when something sleeps. A limiter
tested against the wall clock costs a second per request and cannot assert anything exactly.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Final

import httpx
import pytest
import respx

from investo.ingest.cache import Cache
from investo.ingest.edgar.client import EdgarClient, TokenBucket, companyfacts_url
from tests.conftest import VALID_USER_AGENT, FakeClock

RATE: Final = 5.0
"""The configured default: half of SEC's cap (``Settings.edgar_requests_per_second``)."""

INTERVAL: Final = 1.0 / RATE
TOLERANCE: Final = 1e-9
"""Slack for float accumulation only: 0.2 is not exact in binary, so four of them overshoot 0.8."""

FIVE_TICKERS: Final = (320193, 789019, 1018724, 1326801, 1045810)
"""Five real NASDAQ CIKs — the workload ROADMAP M1's exit criterion describes."""


def _router() -> respx.Router:
    """A router with no routes registered yet.

    ``assert_all_mocked`` stays at its default ``True``, so a request to an unregistered route
    raises instead of being auto-mocked with a 200. Used as a transport rather than by patching
    httpx globally, because ``EdgarClient`` declares ``transport`` as its test seam and going
    through it keeps the patching local to the client under test.
    """
    return respx.Router()


def _client(
    cache: Cache,
    router: respx.Router,
    clock: FakeClock,
    *,
    rate: float = RATE,
) -> EdgarClient:
    return EdgarClient(
        user_agent=VALID_USER_AGENT,
        requests_per_second=rate,
        cache=cache,
        clock=clock,
        transport=httpx.MockTransport(router.handler),
    )


def _recording_router(clock: FakeClock, stamps: list[float], *ciks: int) -> respx.Router:
    """Route each CIK's ``companyfacts`` URL to a 200, recording the clock at request time."""
    router = _router()

    def respond(request: httpx.Request) -> httpx.Response:
        stamps.append(clock.monotonic())
        return httpx.Response(200, content=b'{"facts":{}}')

    for cik in ciks:
        _ = router.get(companyfacts_url(cik)).mock(side_effect=respond)
    return router


# ---------------------------------------------------------------------------
# The bucket itself
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_n_acquires_advance_the_clock_by_at_least_n_minus_one_intervals(clock: FakeClock) -> None:
    """§4.1's arithmetic: ten requests at 5 req/s cannot finish in less than nine intervals.

    ``n - 1`` and not ``n``, because the first request is free — a limiter that slept before it
    would add ``1/rate`` to every run in exchange for nothing, and on a warm-ish run of a dozen
    requests that is a visible delay with no upstream justification.
    """
    bucket = TokenBucket(rate=RATE, clock=clock)
    for _ in range(10):
        bucket.acquire()
    assert clock.monotonic() >= 9 * INTERVAL - TOLERANCE


@pytest.mark.spec
def test_the_first_acquire_does_not_sleep(clock: FakeClock) -> None:
    """The other half of the boundary above, asserted on the sleep log rather than the clock."""
    TokenBucket(rate=RATE, clock=clock).acquire()
    assert clock.sleeps == []


@pytest.mark.spec
def test_capacity_is_one_so_the_second_request_is_spaced(clock: FakeClock) -> None:
    """§3: **capacity 1, not a burst.**

    A bucket with capacity 5 at 5 req/s can emit five requests in the first instant — an
    instantaneous rate well above what the config asks for and uncomfortably near the 10 req/s SEC
    monitors. This is the test that fails under such a bucket: with capacity 5 the second acquire
    returns immediately and the clock never moves.
    """
    bucket = TokenBucket(rate=RATE, clock=clock)
    bucket.acquire()
    first = clock.monotonic()
    bucket.acquire()
    assert clock.monotonic() - first >= INTERVAL - TOLERANCE


@pytest.mark.spec
@pytest.mark.parametrize("rate", [0.0, -1.0, -5.0])
def test_the_bucket_rejects_a_non_positive_rate(rate: float, clock: FakeClock) -> None:
    """A rate of zero is an infinite wait, and the failure would look like a hung fetch.

    ``Settings.edgar_requests_per_second`` already validates ``gt=0, le=10``, so this is the second
    line of defence for a caller that constructs a bucket directly — and it fails loudly rather than
    blocking forever, which is the difference between a bug report and a mystery.
    """
    with pytest.raises(ValueError, match="positive rate"):
        _ = TokenBucket(rate=rate, clock=clock)


@pytest.mark.spec
def test_time_already_elapsed_counts_towards_the_interval(clock: FakeClock) -> None:
    """A request that took longer than the interval must not then wait again.

    The limiter spaces requests; it does not add a delay after each one. Without this the run pays
    ``1/rate`` on top of every response time, which on a 40 MB ``companyfacts`` download is a delay
    charged for a limit that was never approached.
    """
    bucket = TokenBucket(rate=RATE, clock=clock)
    bucket.acquire()
    clock.advance(INTERVAL * 3)
    bucket.acquire()
    assert clock.sleeps == [], "the wait was already served by the time the response took"


# ---------------------------------------------------------------------------
# Through the client
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_five_ticker_cold_fetch_respects_bucket(cache: Cache, clock: FakeClock) -> None:
    """ROADMAP M1's workload, at the configured rate: five cold ``companyfacts`` fetches.

    Two assertions, and the second is the one that matters. Total elapsed time being at least
    ``(n-1)/rate`` is necessary but weak — it passes for an implementation that emits every request
    at once and then sleeps for the sum. So the gaps between the recorded request times are asserted
    individually, and each one has to be at least ``1/rate``.
    """
    stamps: list[float] = []
    router = _recording_router(clock, stamps, *FIVE_TICKERS)
    client = _client(cache, router, clock)

    for cik in FIVE_TICKERS:
        _ = client.get(companyfacts_url(cik))

    assert client.request_count == len(FIVE_TICKERS)
    assert len(stamps) == len(FIVE_TICKERS)
    assert clock.monotonic() >= (len(FIVE_TICKERS) - 1) * INTERVAL - TOLERANCE
    gaps = [later - earlier for earlier, later in pairwise(stamps)]
    assert len(gaps) == len(FIVE_TICKERS) - 1
    for index, gap in enumerate(gaps):
        assert gap >= INTERVAL - TOLERANCE, f"requests {index} and {index + 1} were {gap}s apart"


@pytest.mark.spec
def test_the_spacing_follows_the_configured_rate(cache: Cache, clock: FakeClock) -> None:
    """The interval is derived from the configured rate, not a constant that matches the default.

    ``INVESTO_EDGAR_REQUESTS_PER_SECOND`` is a documented setting, so a hard-coded 0.2s sleep would
    honour the default and silently ignore every other value. Asserted at a rate no default
    produces.
    """
    stamps: list[float] = []
    slow_rate = 2.0
    router = _recording_router(clock, stamps, *FIVE_TICKERS[:3])
    client = _client(cache, router, clock, rate=slow_rate)

    for cik in FIVE_TICKERS[:3]:
        _ = client.get(companyfacts_url(cik))

    for gap in (later - earlier for earlier, later in pairwise(stamps)):
        assert gap >= 1.0 / slow_rate - TOLERANCE


@pytest.mark.spec
def test_a_warm_cache_takes_no_tokens(cache: Cache, clock: FakeClock) -> None:
    """§2: the cache is checked **before** the limiter.

    So a warm run sleeps not at all, which is what makes iterating against a cached payload bearable
    — and it means the limiter's state is a function of network requests only, which is what every
    other test in this file assumes. The second client's router has no routes, so any request at all
    would raise rather than quietly re-fetching and re-sleeping.
    """
    urls = [companyfacts_url(cik) for cik in FIVE_TICKERS[:3]]
    stamps: list[float] = []
    cold = _client(cache, _recording_router(clock, stamps, *FIVE_TICKERS[:3]), clock)
    for url in urls:
        _ = cold.get(url)
    assert clock.monotonic() > 0.0, "the cold run did spend tokens"

    warm_clock = FakeClock()
    warm = _client(cache, _router(), warm_clock)
    for url in urls:
        assert warm.get(url).from_cache is True

    assert warm.request_count == 0
    assert warm_clock.sleeps == []
    assert warm_clock.monotonic() == 0.0

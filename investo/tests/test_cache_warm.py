"""The warm run makes zero HTTP calls — a test by omission.

ROADMAP M1's exit criterion, and the only way to assert it honestly. There is no positive assertion
that proves a request did *not* happen: ``request_count == 0`` is necessary but it is a counter the
code under test maintains, and a counter can be wrong in exactly the way the test is looking for.

So the second client here is built against a respx router with **no routes registered at all**.
``assert_all_mocked`` defaults to ``True``, which makes any unmatched request raise rather than
being auto-mocked with a 200. The absence of a route is the assertion; ``request_count`` is the
corroboration. ``docs/m1/06-testing.md`` §2 calls this out as the reason respx is configured that
way suite-wide, and :func:`test_a_router_with_no_routes_raises_on_any_request` pins the mechanism so
this file cannot go quietly vacuous if that default ever changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import httpx
import pytest
import respx

from investo.ingest.cache import Cache
from investo.ingest.edgar.client import EdgarClient, companyfacts_url, tickers_exchange_url
from tests.conftest import VALID_USER_AGENT, FakeClock

URL: Final = tickers_exchange_url()
UNCACHED_URL: Final = companyfacts_url(320193)


def _client(
    cache: Cache,
    router: respx.Router,
    clock: FakeClock,
    *,
    refresh: bool = False,
) -> EdgarClient:
    return EdgarClient(
        user_agent=VALID_USER_AGENT,
        requests_per_second=5.0,
        cache=cache,
        refresh=refresh,
        clock=clock,
        transport=httpx.MockTransport(router.handler),
    )


def _serving(url: str, body: bytes) -> respx.Router:
    router = respx.Router()
    _ = router.get(url).mock(return_value=httpx.Response(200, content=body))
    return router


@pytest.mark.spec
def test_second_fetch_makes_no_requests(
    cache: Cache, clock: FakeClock, tmp_path: Path, tickers_body: bytes
) -> None:
    """ROADMAP M1: a warm run makes zero HTTP calls.

    The second client gets a router with nothing registered, so a single request would raise before
    ``request_count`` was ever read. It also gets a **new** ``Cache`` over the same directory,
    because the criterion is about the next *run* rather than the next call — an in-memory index
    would satisfy the weaker version and evaporate between processes.

    The body is compared byte for byte against the fixture the cold run was served, which is the
    property everything downstream rests on: DESIGN.md §4.4's "regenerates byte-identically from
    cache" is about these bytes, not about a payload that merely parses to the same thing.
    """
    cold = _client(cache, _serving(URL, tickers_body), clock)
    first = cold.get(URL)
    assert first.status == 200
    assert first.body == tickers_body
    assert first.from_cache is False
    assert cold.request_count == 1

    warm_clock = FakeClock()
    warm = _client(Cache(tmp_path / "cache"), respx.Router(), warm_clock)
    second = warm.get(URL)

    assert warm.request_count == 0
    assert second.body == tickers_body
    assert second.from_cache is True
    assert warm_clock.sleeps == []


@pytest.mark.spec
def test_a_router_with_no_routes_raises_on_any_request(cache: Cache, clock: FakeClock) -> None:
    """The mechanism the test above depends on, asserted directly.

    If respx ever auto-mocked unmatched requests with a 200 — which is what
    ``assert_all_mocked=False`` does — then "register nothing" would stop proving anything and the
    warm-run test would pass for a client that re-fetched everything. This is the test that fails
    first if that changes.
    """
    warm = _client(cache, respx.Router(), clock)
    with pytest.raises(AssertionError, match="not mocked"):
        _ = warm.get(UNCACHED_URL)


@pytest.mark.spec
def test_the_warm_response_carries_the_recorded_fetch_time(
    cache: Cache, clock: FakeClock, tickers_body: bytes
) -> None:
    """A cache hit reports when the payload was *fetched*, not when it was read.

    ``fetched_at`` is what the fetch summary and the appendix print, and a warm run that stamped it
    with the current time would claim the data is fresh — which is the one thing the cache exists to
    keep honest. It is displayed and never read arithmetically, so this asserts equality with the
    cold run rather than any relation to now.
    """
    cold = _client(cache, _serving(URL, tickers_body), clock)
    first = cold.get(URL)

    warm = _client(cache, respx.Router(), FakeClock())
    second = warm.get(URL)

    assert second.fetched_at == first.fetched_at
    assert second.fetched_at.tzinfo is not None
    assert second.url == first.url


@pytest.mark.spec
def test_refresh_bypasses_the_warm_cache_and_appends(
    cache: Cache, clock: FakeClock, tmp_path: Path, tickers_body: bytes
) -> None:
    """§3: ``--refresh`` bypasses the read and forces a fetch; the result is a *new* entry.

    Both halves matter. Not fetching would make the flag a no-op on a warm cache, which is the only
    state anyone would use it in; and overwriting would destroy the previous view, which is what
    makes upstream drift inspectable rather than a mystery. Asserted through the manifest, since the
    read path deliberately shows only the newest generation.
    """
    warm_first = _client(cache, _serving(URL, tickers_body), clock)
    _ = warm_first.get(URL)

    refreshed = b'{"0":{"cik":320193,"name":"Apple Inc.","ticker":"AAPL","exchange":"Nasdaq"}}'
    refresher = _client(
        Cache(tmp_path / "cache"), _serving(URL, refreshed), FakeClock(), refresh=True
    )
    response = refresher.get(URL)

    assert refresher.request_count == 1, "a refresh does not read the cache first"
    assert response.body == refreshed
    assert response.from_cache is False

    manifest = (tmp_path / "cache" / "manifest.jsonl").read_text(encoding="utf-8")
    assert len(manifest.strip().splitlines()) == 2, "appended, not overwritten"

    reader = _client(Cache(tmp_path / "cache"), respx.Router(), FakeClock())
    assert reader.get(URL).body == refreshed, "and the newest generation is what a warm run sees"
    assert reader.request_count == 0

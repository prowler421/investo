"""The retry matrix from ``docs/m1/03-edgar-client.md`` §4, including every no-retry case.

The load-bearing distinction is between SEC's two meanings for 403. An undeclared automated tool and
a rate-limit rejection arrive with the same status and need opposite handling: one is a
configuration error no amount of retrying fixes, the other is exactly what retrying is for. The body
tells them apart, which is brittle by nature — so the mitigation is that being wrong is survivable
in one direction and *tested* in the other.

Hence the shape of the first test: it asserts ``request_count == 1``, not merely that the exception
was raised. "Never retried" is a claim about the number of requests, and an implementation that
retried four times before raising the right exception would satisfy every assertion about the
exception type.

Every test uses :class:`tests.conftest.FakeClock`, so the backoff sleeps cost nothing and can be
read back exactly.
"""

from __future__ import annotations

from typing import Final

import httpx
import pytest
import respx

from investo.errors import (
    ConfigError,
    ExitCode,
    SecThrottledError,
    UndeclaredUserAgentError,
    UpstreamFetchError,
)
from investo.ingest.cache import Cache
from investo.ingest.edgar.client import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    RETRY_AFTER_CAP_SECONDS,
    RETRY_STATUSES,
    EdgarClient,
    companyfacts_url,
)
from tests.conftest import VALID_USER_AGENT, FakeClock

URL: Final = companyfacts_url(320193)
FAST_RATE: Final = 1000.0
"""A rate whose interval (1ms) cannot be confused with a backoff sleep.

The limiter and the retry policy both sleep on the same clock, so a test that reads sleep durations
has to make one of the two negligible. The spacing itself is asserted in
``test_client_ratelimit.py``.
"""


def _router(
    status: int, *, body: bytes = b"", headers: dict[str, str] | None = None
) -> respx.Router:
    """A router whose only route answers ``URL`` with one repeated response.

    ``assert_all_mocked`` stays at its default ``True``: a request to any other URL raises rather
    than being auto-mocked, so a test cannot pass by accidentally reaching an endpoint it did not
    register.
    """
    router = respx.Router()
    _ = router.get(URL).mock(return_value=httpx.Response(status, content=body, headers=headers))
    return router


def _client(
    cache: Cache,
    router: respx.Router,
    clock: FakeClock,
    *,
    max_attempts: int = 5,
    jitter_seed: int = 0,
    rate: float = FAST_RATE,
) -> EdgarClient:
    return EdgarClient(
        user_agent=VALID_USER_AGENT,
        requests_per_second=rate,
        cache=cache,
        clock=clock,
        transport=httpx.MockTransport(router.handler),
        max_attempts=max_attempts,
        jitter_seed=jitter_seed,
    )


# ---------------------------------------------------------------------------
# The two meanings of 403
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_undeclared_403_makes_one_request(
    cache: Cache, clock: FakeClock, undeclared_403_body: bytes
) -> None:
    """``docs/m1/06-testing.md`` §4: an undeclared-tool 403 is never retried.

    **The count is the assertion.** Retrying cannot succeed — SEC is rejecting the User-Agent, which
    will not change between attempts — and it does spend rate budget against a limit whose penalty
    DESIGN.md §4.1 notes is not only ours to pay. A retry loop that eventually raised the same
    exception would look correct from the outside and be exactly the behaviour this forbids.

    Exit 5, not 4: nothing upstream is wrong and nothing was fetched. The configuration is.
    """
    router = _router(403, body=undeclared_403_body)
    client = _client(cache, router, clock)

    with pytest.raises(UndeclaredUserAgentError) as caught:
        _ = client.get(URL)

    assert client.request_count == 1
    assert clock.sleeps == [], "and it did not back off before giving up either"
    assert caught.value.exit_code == ExitCode.CONFIG_ERROR
    assert isinstance(caught.value, ConfigError)
    assert caught.value.hint is not None
    assert "INVESTO_SEC_USER_AGENT" in caught.value.hint


@pytest.mark.spec
def test_a_throttling_403_is_retried(
    cache: Cache, clock: FakeClock, throttled_403_body: bytes
) -> None:
    """The other 403, and the opposite handling — from the same status code.

    SEC's rate-limit page says "Request Rate Threshold Exceeded", which is transient by definition,
    so this is the branch retrying exists for. ``SecThrottledError`` rather than a bare
    ``UpstreamFetchError`` so the test can assert *which* condition fired: the guarantee above is
    about the condition, not the exit code, and both 403 paths would otherwise be indistinguishable.
    """
    attempts = 3
    router = _router(403, body=throttled_403_body)
    client = _client(cache, router, clock, max_attempts=attempts)

    with pytest.raises(SecThrottledError) as caught:
        _ = client.get(URL)

    assert client.request_count == attempts
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE
    assert not isinstance(caught.value, ConfigError), "a throttle is not a configuration error"


@pytest.mark.spec
def test_an_unmatched_403_body_is_treated_as_throttling(cache: Cache, clock: FakeClock) -> None:
    """§4: an unrecognized 403 body is retried, not reported as a config error.

    The asymmetry is deliberate. Guessing "config error" on a body SEC has reworded turns a
    transient upstream change into a hard exit 5 the user cannot act on; guessing "throttle" costs
    at most four retries and then reports exit 4 honestly. So the *default* branch for an unknown
    body is the retryable one, and this is the test that pins that direction.
    """
    attempts = 2
    router = _router(403, body=b"<html><body>Service temporarily unavailable</body></html>")
    client = _client(cache, router, clock, max_attempts=attempts)

    with pytest.raises(SecThrottledError):
        _ = client.get(URL)
    assert client.request_count == attempts


# ---------------------------------------------------------------------------
# 429 and Retry-After
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_429_is_retried_until_the_attempts_run_out(cache: Cache, clock: FakeClock) -> None:
    """§4 row 1, and the error names the request count and the elapsed time.

    Because the useful next action after a throttle is to wait: DESIGN.md §4.1 records that SEC's
    threshold clears after about ten minutes below the rate, so "how long have I been hammering it"
    is the one fact that makes the message actionable.
    """
    attempts = 4
    client = _client(cache, _router(429), clock, max_attempts=attempts)

    with pytest.raises(SecThrottledError) as caught:
        _ = client.get(URL)

    assert client.request_count == attempts
    assert f"{attempts} request(s)" in caught.value.message


@pytest.mark.spec
def test_retry_after_is_honoured_when_it_is_short(cache: Cache, clock: FakeClock) -> None:
    """§4: ``Retry-After`` is honoured when present and ``<= 120s``.

    Asserted through the sleep log rather than the elapsed time, because the whole point is that the
    header replaced the computed backoff: attempt 1's jittered ceiling is 1 second, so a 5-second
    sleep cannot have come from anywhere else.
    """
    router = _router(429, headers={"Retry-After": "5"})
    client = _client(cache, router, clock, max_attempts=2)

    with pytest.raises(SecThrottledError):
        _ = client.get(URL)
    assert clock.sleeps == [5.0]


@pytest.mark.spec
def test_retry_after_is_capped(cache: Cache, clock: FakeClock) -> None:
    """§4: capped, so a hostile or mistaken header cannot hang the run.

    Ten minutes in a header would otherwise become ten minutes of a five-minute cold-run budget
    (DESIGN.md §14) spent in ``sleep``, with nothing on stdout to explain it. The cap is asserted as
    the constant rather than as ``120``, so the two cannot drift apart.
    """
    router = _router(429, headers={"Retry-After": "600"})
    client = _client(cache, router, clock, max_attempts=2)

    with pytest.raises(SecThrottledError):
        _ = client.get(URL)
    assert clock.sleeps == [RETRY_AFTER_CAP_SECONDS]
    assert max(clock.sleeps) < 600.0


@pytest.mark.spec
def test_a_retry_after_that_is_not_a_number_falls_back_to_backoff(
    cache: Cache, clock: FakeClock
) -> None:
    """The HTTP-date spelling of ``Retry-After`` is legal, and SEC does not use it.

    Falling back to exponential backoff is strictly safer than parsing a date format we have never
    seen in the wild: the worst case for the fallback is a slightly shorter wait, and the worst case
    for a wrong date parse is a sleep measured in years.
    """
    router = _router(429, headers={"Retry-After": "Fri, 31 Jul 2026 10:58:11 GMT"})
    client = _client(cache, router, clock, max_attempts=2)

    with pytest.raises(SecThrottledError):
        _ = client.get(URL)
    assert clock.sleeps, "it still backed off"
    assert max(clock.sleeps) <= BACKOFF_BASE_SECONDS


@pytest.mark.spec
def test_retry_after_is_ignored_on_a_403(
    cache: Cache, clock: FakeClock, throttled_403_body: bytes
) -> None:
    """Only 429 carries a ``Retry-After`` we honour.

    SEC does not document one on a 403 or a 5xx, and trusting an undocumented header to set a sleep
    duration is how a run comes to hang for two minutes for no stated reason.
    """
    router = _router(403, body=throttled_403_body, headers={"Retry-After": "120"})
    client = _client(cache, router, clock, max_attempts=2)

    with pytest.raises(SecThrottledError):
        _ = client.get(URL)
    assert max(clock.sleeps) <= BACKOFF_BASE_SECONDS


# ---------------------------------------------------------------------------
# 5xx and transport failures
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_retried(status: int, cache: Cache, clock: FakeClock) -> None:
    """§4 row 4. Retried, and then reported as an upstream failure rather than a throttle.

    The type matters: ``SecThrottledError``'s hint tells the user to wait ten minutes, which is the
    wrong advice for a 500 and would send them away from a problem that is not theirs.
    """
    attempts = 3
    client = _client(cache, _router(status), clock, max_attempts=attempts)

    with pytest.raises(UpstreamFetchError) as caught:
        _ = client.get(URL)

    assert client.request_count == attempts
    assert not isinstance(caught.value, SecThrottledError)
    assert str(status) in caught.value.message


@pytest.mark.spec
def test_the_retry_status_set_is_the_documented_one() -> None:
    """§4's table, in one place. 403 is handled separately because its body decides.

    Pinned because the set is the whole policy: adding 400 to it would retry a malformed request
    four times, and removing 503 would fail a run on a CDN blip that a second attempt would have
    fixed.
    """
    assert RETRY_STATUSES == frozenset({429, 500, 502, 503, 504})


@pytest.mark.spec
def test_a_transport_error_is_retried(cache: Cache, clock: FakeClock) -> None:
    """§4 row 5: connect, read and timeout failures are retried.

    A dropped connection is the most common transient failure of all and it never reaches a status
    code, so a policy written only against statuses would abandon a run on one bad packet.
    """
    attempts = 3
    router = respx.Router()
    _ = router.get(URL).mock(side_effect=httpx.ConnectError)
    client = _client(cache, router, clock, max_attempts=attempts)

    with pytest.raises(UpstreamFetchError) as caught:
        _ = client.get(URL)

    assert client.request_count == attempts
    assert "ConnectError" in caught.value.message, "the message names what actually failed"


@pytest.mark.spec
def test_a_transport_error_that_clears_is_not_fatal(cache: Cache, clock: FakeClock) -> None:
    """The point of retrying: the second attempt succeeds and the run continues.

    Every other test here exhausts the attempts, which would all pass under a policy that always
    fails after ``max_attempts`` regardless of the response. This is the one that fails under it.
    """
    router = respx.Router()
    _ = router.get(URL).mock(
        side_effect=[httpx.ReadTimeout("timed out"), httpx.Response(200, content=b'{"ok":true}')]
    )
    client = _client(cache, router, clock, max_attempts=3)

    response = client.get(URL)
    assert response.status == 200
    assert response.body == b'{"ok":true}'
    assert client.request_count == 2


@pytest.mark.spec
def test_backoff_is_bounded_by_the_documented_ceiling(cache: Cache, clock: FakeClock) -> None:
    """§4: ``min(base * 2**(attempt-1), cap)`` with full jitter.

    Full jitter — uniform over ``[0, computed]`` — rather than equal jitter, because the failure
    being defended against is several requests retrying in lockstep after a shared throttle.
    Asserted as bounds rather than as values: the exact draws are the generator's business, and
    pinning them would make this test a fixture for CPython's PRNG.
    """
    attempts = 5
    client = _client(cache, _router(503), clock, max_attempts=attempts)

    with pytest.raises(UpstreamFetchError):
        _ = client.get(URL)

    ceiling = BACKOFF_BASE_SECONDS * 2 ** (attempts - 2)
    assert clock.sleeps, "five attempts means four waits"
    for sleep in clock.sleeps:
        assert 0.0 <= sleep <= min(ceiling, BACKOFF_CAP_SECONDS)


@pytest.mark.spec
def test_jitter_is_reproducible_from_its_seed(cache: Cache) -> None:
    """``jitter_seed`` exists so a retry path is reproducible in a test, and nothing more.

    Retries only happen on network paths, which are outside DESIGN.md §11's determinism gate — the
    gate runs from cached inputs — so this is a testing affordance rather than a determinism
    requirement. Stated here so nobody later concludes the gate depends on it.
    """

    def sleeps(seed: int) -> list[float]:
        clock = FakeClock()
        client = _client(cache, _router(503), clock, max_attempts=4, jitter_seed=seed)
        with pytest.raises(UpstreamFetchError):
            _ = client.get(URL)
        return list(clock.sleeps)

    assert sleeps(7) == sleeps(7)
    assert sleeps(7) != sleeps(8)


# ---------------------------------------------------------------------------
# What is not retried
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_404_is_not_retried_and_is_cached(cache: Cache, clock: FakeClock) -> None:
    """§2: a 404 is an **absence**, recorded with a ``fetched_at`` rather than treated as an error.

    Caching it looks wrong until you consider the alternative: a company with no DEF 14A would be
    re-requested on every run forever, spending rate budget to re-learn a stable fact. The second
    ``get_json`` below is served from that cached absence — ``request_count`` stays at one — and it
    returns ``None`` rather than trying to parse an error page as JSON.
    """
    router = _router(404, body=b"Not Found")
    client = _client(cache, router, clock)

    response = client.get(URL)
    assert response.status == 404
    assert client.request_count == 1
    assert cache.get(Cache.key_for("GET", URL, None)) is not None

    assert client.get_json(URL) is None
    assert client.request_count == 1, "the recorded absence answered the second call"


@pytest.mark.spec
@pytest.mark.parametrize("status", [400, 401, 405, 418, 451])
def test_other_4xx_is_not_retried(status: int, cache: Cache, clock: FakeClock) -> None:
    """§4 row 7: anything else in the 4xx range is our fault, and retrying it cannot help.

    A malformed request retried five times is five requests against a rate budget for a response
    that will not change. Asserted on the count, for the same reason as the undeclared-403 test.
    """
    client = _client(cache, _router(status), clock)

    with pytest.raises(UpstreamFetchError) as caught:
        _ = client.get(URL)

    assert client.request_count == 1
    assert clock.sleeps == []
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE
    assert not isinstance(caught.value, SecThrottledError)


@pytest.mark.spec
@pytest.mark.parametrize("status", [401, 500, 503])
def test_a_non_200_that_is_not_404_is_never_cached(
    status: int, cache: Cache, clock: FakeClock
) -> None:
    """§2: a cached 503 would be a poisoned entry.

    It would answer every later run from disk — including runs made after the outage cleared — and
    ``--refresh`` would be the only way out of a state the user cannot see. 404 is the single
    exception, because an absence is a fact about the filer rather than about the server.
    """
    client = _client(cache, _router(status), clock, max_attempts=2)

    with pytest.raises(UpstreamFetchError):
        _ = client.get(URL)
    assert cache.get(Cache.key_for("GET", URL, None)) is None


@pytest.mark.spec
def test_a_request_to_another_host_raises_before_any_traffic(
    cache: Cache, clock: FakeClock
) -> None:
    """CLAUDE.md convention 6, enforced at runtime as well as by ``test_layering``.

    This client carries SEC's declared contact ``User-Agent`` and SEC's rate budget. Sending either
    to ``api.finra.org`` would misrepresent who is calling and would slow EDGAR requests to protect
    a limit that does not apply — so FINRA gets its own client, and this is the guard that says so.
    """
    client = _client(cache, respx.Router(), clock)

    with pytest.raises(UpstreamFetchError, match="sec.gov"):
        _ = client.get("https://api.finra.org/data/group/otcMarket/name/weeklySummary")

    assert client.request_count == 0
    assert clock.sleeps == []

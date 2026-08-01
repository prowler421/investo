"""The EDGAR client: the only module that may talk to sec.gov (DESIGN.md §4.1).

§4.1 is normative and specific: a token-bucket limiter at ~5 req/s against SEC's cap of 10, a
mandatory ``User-Agent`` from config with **no default**, ``Accept-Encoding: gzip, deflate``,
exponential backoff on 403/429, and the CIK/accession transforms owned here.

Confirmed against SEC's Webmaster FAQ: the documented maximum is *"10 requests per second...
carefully monitored to preserve equitable access,"* and the sample declared-bot headers are
``User-Agent: Sample Company Name AdminContact@<sample company domain>.com``,
``Accept-Encoding: gzip, deflate`` and ``Host``.

CLAUDE.md convention 6 — nothing outside this module may call sec.gov — is a convention that holds
until someone is in a hurry, so ``tests/test_layering.py`` enforces it by walking the AST of every
module in the package. It asserts no ``sec.gov`` string literal outside this file, and no ``httpx``
import outside this file, ``ingest/prices/`` and ``ingest/finra.py`` (different hosts, different
limits, entitled to their own clients — but nothing else is).
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Protocol

import httpx

from investo.domain.provenance import Accession
from investo.errors import SecThrottledError, UndeclaredUserAgentError, UpstreamFetchError
from investo.ingest.cache import Cache

__all__ = [
    "Clock",
    "SystemClock",
    "TokenBucket",
    "Response",
    "EdgarClient",
    "cik_path",
    "archives_cik",
    "companyfacts_url",
    "submissions_url",
    "submissions_page_url",
    "frames_url",
    "archives_doc_url",
    "tickers_exchange_url",
    "ownership_doc",
    "frames_unit",
]

_DATA_HOST: Final = "https://data.sec.gov"
_WWW_HOST: Final = "https://www.sec.gov"
_SEC_DOMAIN: Final = "sec.gov"

RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})
BACKOFF_BASE_SECONDS: Final = 1.0
BACKOFF_CAP_SECONDS: Final = 30.0
RETRY_AFTER_CAP_SECONDS: Final = 120.0
"""``Retry-After`` is honoured only up to this, so a hostile or mistaken header cannot hang the
run."""

_UNDECLARED: Final = re.compile(rb"undeclared\s+automated\s+tool", re.IGNORECASE)
_THROTTLED: Final = re.compile(rb"request\s+rate", re.IGNORECASE)
"""Matched against the response body, case-insensitively, on bytes.

SEC returns 403 for both an undeclared automated tool and rate-limit rejection, and the two need
opposite handling. **An unmatched 403 is treated as throttling** — the retryable branch — because
guessing "config error" on an unrecognized body would turn a transient upstream change into a hard
exit 5 the user cannot act on, while guessing "throttle" costs at most four retries and then
reports exit 4 honestly.

Regex on a response body is brittle by nature. The mitigation is that being wrong is survivable in
one direction and tested in the other: ``test_client_retry.py`` asserts that an undeclared-tool
403 makes **exactly one request** and raises, which is the violation test for "an undeclared-tool
403 is never retried".
"""


# ---------------------------------------------------------------------------
# Injection seams
# ---------------------------------------------------------------------------
class Clock(Protocol):
    """Time, injectable. A rate limiter that sleeps against the wall clock cannot be tested in
    under a second per request, and a retry policy tested against a real socket is not tested."""

    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock. The default, so production code names nothing."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class TokenBucket:
    """A strict request spacer.

    **Capacity 1, not a burst.** A bucket with capacity 5 at 5 req/s can emit five requests in the
    first instant, which is an instantaneous rate well above what the config asks for and
    uncomfortably near the 10 req/s SEC monitors. Capacity 1 makes this a spacer: every request
    waits until ``1/rate`` has elapsed since the last one.

    §4.1's own reasoning applies — *the penalty for being slightly too fast is minutes of
    downtime, and the reward for being exactly at the limit is nothing* — and a burst buys nothing
    here, because M1's workload is a dozen sequential requests rather than a queue.
    """

    def __init__(self, *, rate: float, capacity: float = 1.0, clock: Clock) -> None:
        if rate <= 0:
            raise ValueError(f"TokenBucket needs a positive rate, got {rate}")
        self._interval = 1.0 / rate
        self._capacity = capacity
        self._clock = clock
        self._next_allowed: float | None = None

    def acquire(self) -> None:
        """Block until a request may be made, then reserve the slot."""
        now = self._clock.monotonic()
        if self._next_allowed is not None and now < self._next_allowed:
            self._clock.sleep(self._next_allowed - now)
            now = self._next_allowed
        self._next_allowed = now + self._interval


@dataclass(frozen=True, slots=True)
class Response:
    """One fetched (or cached) payload.

    ``from_cache`` is what makes ROADMAP M1's "warm run makes zero HTTP calls" observable from the
    outside without counting sockets.
    """

    status: int
    body: bytes
    headers: Mapping[str, str]
    url: str
    fetched_at: datetime
    from_cache: bool


# ---------------------------------------------------------------------------
# URL and identifier transforms (DESIGN.md §4.1)
# ---------------------------------------------------------------------------
# Functions rather than f-strings at call sites, so the padding rules have one implementation and
# one test. Each is tested against a hand-written expected URL — the only place in the suite where
# asserting a literal is right, because the literal *is* the specification and there is no
# derivation to assert instead.
#
# The boundary that would otherwise be found in production: a CIK below 1,000,000 pads to ten
# digits on data.sec.gov and does **not** pad in /Archives/. Apple (320193) is such a CIK, so the
# default fixture exercises it.


def cik_path(cik: int) -> str:
    """``320193`` -> ``"CIK0000320193"``. The ``data.sec.gov`` spelling: ``CIK`` + 10-digit pad."""
    return f"CIK{cik:010d}"


def archives_cik(cik: int) -> str:
    """``320193`` -> ``"320193"``. The ``/Archives/`` spelling: unpadded decimal."""
    return str(cik)


def companyfacts_url(cik: int) -> str:
    """All XBRL facts for one company. 10-40 MB for a large filer."""
    return f"{_DATA_HOST}/api/xbrl/companyfacts/{cik_path(cik)}.json"


def submissions_url(cik: int) -> str:
    """Company metadata plus ``filings.recent`` — **not** the whole filing history.

    See :func:`submissions_page_url` and ``submissions.pages_needed``.
    """
    return f"{_DATA_HOST}/submissions/{cik_path(cik)}.json"


def submissions_page_url(name: str) -> str:
    """One overflow page, from a ``filings.files[].name`` such as
    ``"CIK0000320193-submissions-001.json"``.

    Takes the name SEC gave rather than composing it from a CIK and an index, because the naming
    is SEC's and a composed URL would be a guess that 404s on the first filer whose pages are
    numbered differently.
    """
    return f"{_DATA_HOST}/submissions/{name}"


def frames_url(taxonomy: str, tag: str, unit: str, period: str) -> str:
    """One cross-company frame.

    ``unit`` uses the ``-per-`` spelling here (``USD-per-shares``), where ``companyfacts`` uses
    ``/`` (``USD/shares``). Same unit, two spellings, two places — and mixing them up is a 404 in
    one direction and a ``KeyError`` in the other. :func:`frames_unit` does the conversion so the
    pair has one implementation.
    """
    return f"{_DATA_HOST}/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"


def frames_unit(companyfacts_unit: str) -> str:
    """``"USD/shares"`` -> ``"USD-per-shares"``. The ``frames`` URL spelling of a unit."""
    return companyfacts_unit.replace("/", "-per-")


def archives_doc_url(cik: int, accession: Accession, document: str) -> str:
    """A document inside a filing's ``/Archives/`` directory.

    Both transforms at once: the CIK is **unpadded** here and the accession is **undashed**, which
    is the opposite of what ``data.sec.gov`` wants for each. This is the pairing ROADMAP M1 names
    as a risk, because getting either wrong is a 404 that looks like missing data.
    """
    return f"{_WWW_HOST}/Archives/edgar/data/{archives_cik(cik)}/{accession.nodashes}/{document}"


def tickers_exchange_url() -> str:
    """The ticker-to-CIK-and-exchange file. ``company_tickers.json`` is deliberately unused —
    two lookup paths for the same question is how a NASDAQ filter comes to be bypassed."""
    return f"{_WWW_HOST}/files/company_tickers_exchange.json"


def ownership_doc(primary_document: str, *, form: str) -> str:
    """Strip a leading ``xsl*/`` segment for forms 3, 4 and 5.

    Every Form 3 and Form 4 row in the observed ``submissions`` payload has
    ``primaryDocument = "xslF345X06/ownership.xml"`` — an **XSL-rendered viewer path**, not the
    raw XML. The machine-readable document is ``ownership.xml`` in the accession directory; the
    prefix serves a browser-facing HTML rendering.

    Using it verbatim fetches a styled document where ``ownership.py`` expects Form 4 XML, and the
    failure looks like a parser bug rather than a URL bug. Confirmed against a live payload
    (``docs/m1/04-parsers.md`` §3).

    Restricted to forms 3/4/5 and their amendments rather than applied unconditionally: an
    ``xsl``-prefixed path on some other form would be a different thing, and stripping it blindly
    would turn a URL we do not understand into a URL that silently 404s.
    """
    if form.split("/", 1)[0].strip().upper() not in {"3", "4", "5"}:
        return primary_document
    head, sep, tail = primary_document.partition("/")
    if sep and head.lower().startswith("xsl"):
        return tail
    return primary_document


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class EdgarClient:
    """A rate-limited, cache-backed HTTP client for sec.gov.

    ``user_agent`` is a required keyword with **no default**, mirroring
    ``Settings.sec_user_agent``. There is no code path that constructs a client without one, which
    is the enforcement of §4.1's "startup fails if unset" — and the reason ``tests/conftest.py``
    clears the whole ``INVESTO_*`` environment (CLAUDE.md convention 2).

    Request flow::

        get(url, params)
          |
          |- key = Cache.key_for("GET", url, params)
          |- if not refresh:  hit = cache.get(key) -> Response(from_cache=True)
          |
          |- limiter.acquire()
          |- httpx.get(url, headers=...)
          |
          |- classify(status, body)
          |    |- 200            -> cache.put(...) -> Response(from_cache=False)
          |    |- 404            -> cache.put(...) -> Response  (absence, not an error)
          |    |- 403 undeclared -> UndeclaredUserAgentError  (exit 5, no retry)
          |    |- 403 throttled  -> retry
          |    |- 429            -> retry, honouring Retry-After
          |    |- 5xx            -> retry
          |    `- other 4xx      -> UpstreamFetchError        (exit 4, no retry)
          |
          `- attempts exhausted  -> SecThrottledError / UpstreamFetchError  (exit 4)

    Two details there are decisions rather than mechanics.

    **The cache is checked before the limiter.** A warm run therefore takes no tokens and sleeps
    not at all, which is what makes ``investo facts AAPL`` fast enough to iterate against. It also
    means the limiter's state is a function of network requests only, which is what the rate-limit
    test asserts.

    **A 404 is cached.** Caching an absence looks wrong until you consider the alternative: a
    company with no DEF 14A would be re-requested on every run forever, spending rate budget to
    re-learn a stable fact. The cached 404 is a recorded absence with a ``fetched_at``, and
    ``--refresh`` re-checks it. Non-200 statuses **other than** 404 are not cached — a cached 503
    would be a poisoned entry.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        requests_per_second: float,
        cache: Cache,
        refresh: bool = False,
        clock: Clock | None = None,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = 5,
        jitter_seed: int = 0,
    ) -> None:
        """
        Args:
            user_agent: From ``Settings.sec_user_agent``, already validated in M0 to contain an
                ``@`` and not to be the ``example.com`` placeholder.
            requests_per_second: From ``Settings.edgar_requests_per_second``, already validated
                ``gt=0, le=10``.
            cache: Shared, host-agnostic.
            refresh: Bypass the read and force a fetch. The result is ``put`` as a *new* entry;
                nothing is deleted or overwritten.
            clock: Test seam. Defaults to the real clock.
            transport: Test seam for ``respx``. Defaults to httpx's own.
            max_attempts: Total attempts per request, including the first.
            jitter_seed: Makes backoff reproducible in tests. Retries only happen on network
                paths, which are outside DESIGN.md §11's determinism gate — the gate runs from
                cached inputs — so this is a testing affordance rather than a determinism
                requirement. Stated so nobody later concludes the gate depends on it.
        """
        self._user_agent = user_agent
        self._cache = cache
        self._refresh = refresh
        self._clock = clock if clock is not None else SystemClock()
        self._bucket = TokenBucket(rate=requests_per_second, clock=self._clock)
        self._max_attempts = max_attempts
        # Not cryptographic, deliberately: this seeds retry jitter, and a seedable generator is
        # what makes backoff reproducible in a test.
        self._jitter = random.Random(jitter_seed)
        self._request_count = 0
        self._started = self._clock.monotonic()
        self._client = httpx.Client(
            transport=transport,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            # Within sec.gov the /os/ -> /about/ style moves are real. Off-host redirects are not
            # followed, because a redirect to a third party would send SEC's declared contact
            # address somewhere it does not belong.
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def request_count(self) -> int:
        """Network requests only; cache hits excluded.

        That exclusion is the point: it is the number ROADMAP M1's "warm run makes zero HTTP
        calls" criterion is about.
        """
        return self._request_count

    # -- fetch -------------------------------------------------------------
    def get(self, url: str, *, params: Mapping[str, str] | None = None) -> Response:
        """Fetch ``url``, from cache when possible.

        Raises:
            UndeclaredUserAgentError: on a 403 whose body says SEC did not recognize the tool.
                Exit 5, never retried.
            SecThrottledError: when retries are exhausted on 429 or a throttling 403. Exit 4.
            UpstreamFetchError: on any other non-retryable failure, or exhausted retries on 5xx
                and transport errors. Exit 4.
        """
        if _SEC_DOMAIN not in httpx.URL(url).host:
            raise UpstreamFetchError(
                f"EdgarClient was asked for {url!r}, which is not on {_SEC_DOMAIN}.",
                hint=(
                    "This client carries SEC's declared contact User-Agent and SEC's rate "
                    "budget. Another host needs its own client — see docs/m1/03-edgar-client.md "
                    "§7."
                ),
            )

        key = Cache.key_for("GET", url, params)
        if not self._refresh:
            hit = self._cache.get(key)
            if hit is not None:
                entry, body = hit
                return Response(
                    status=entry.status,
                    body=body,
                    headers=entry.headers,
                    url=entry.url,
                    fetched_at=entry.fetched_at,
                    from_cache=True,
                )
        return self._fetch(url, params or {}, key)

    def get_json(self, url: str, *, params: Mapping[str, str] | None = None) -> Any:
        """Fetch and decode JSON, preserving ``Decimal`` for every non-integer number.

        ``parse_float=Decimal`` is set here rather than in each parser because
        ``json.loads`` materializes ``391035000000.01`` as a ``float`` before any of our code
        sees it, so ``Decimal(row["val"])`` is already too late — it converts a value that has
        already lost precision. The hook is called with the *source text*, so no ``float`` is ever
        constructed.

        Measured cost on a realistic ``companyfacts`` numeric mix: 1.12x, about +0.01s on a large
        filer. The C scanner is retained when ``parse_float`` is supplied and the callable fires
        only for numbers carrying a decimal point, which in ``companyfacts`` are the minority.
        See ``docs/m1/04-parsers.md`` § The cost, measured.

        Returns ``None`` for a 404, which is an absence rather than an error.
        """
        response = self.get(url, params=params)
        if response.status == 404:
            return None
        return json.loads(response.body, parse_float=Decimal, parse_int=int)

    # -- internals ---------------------------------------------------------
    def _fetch(self, url: str, params: Mapping[str, str], key: str) -> Response:
        last_error: str = "no attempt was made"
        for attempt in range(1, self._max_attempts + 1):
            # Retries wait *and then* take a token. Backoff is not a substitute for the limiter:
            # a 429 followed by an un-limited retry is precisely the pattern that gets an IP
            # throttled for ten minutes.
            self._bucket.acquire()
            self._request_count += 1
            try:
                raw = self._client.get(url, params=dict(params) or None)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == self._max_attempts:
                    break
                self._clock.sleep(self._backoff(attempt, retry_after=None))
                continue

            status = raw.status_code
            body = raw.content

            if status == 200 or status == 404:
                entry = self._cache.put(
                    key=key,
                    url=url,
                    method="GET",
                    params=params,
                    status=status,
                    headers=dict(raw.headers),
                    body=body,
                )
                return Response(
                    status=status,
                    body=body,
                    headers=entry.headers,
                    url=url,
                    fetched_at=entry.fetched_at,
                    from_cache=False,
                )

            if status == 403 and _UNDECLARED.search(body):
                raise UndeclaredUserAgentError(
                    "SEC rejected the request as an undeclared automated tool.",
                    hint=(
                        "SEC requires a User-Agent naming you and a contact address, e.g.\n"
                        '  export INVESTO_SEC_USER_AGENT="Investo research you@your-domain.com"\n'
                        f"The one sent was: {self._user_agent!r}. See DESIGN.md §4.1."
                    ),
                )

            retryable = status in RETRY_STATUSES or status == 403
            if not retryable:
                raise UpstreamFetchError(
                    f"{url} returned HTTP {status}.",
                    hint=(
                        "Not a retryable status. A 404 would be recorded as an absence; this is "
                        "something else."
                    ),
                )

            last_error = f"HTTP {status}"
            if attempt == self._max_attempts:
                break
            self._clock.sleep(self._backoff(attempt, retry_after=_retry_after(raw.headers, status)))

        elapsed = self._clock.monotonic() - self._started
        if last_error.startswith("HTTP 429") or last_error == "HTTP 403":
            raise SecThrottledError.after(requests=self._request_count, elapsed=elapsed)
        raise UpstreamFetchError(
            f"{url} failed after {self._max_attempts} attempts ({last_error}).",
            hint="Everything already fetched is cached, so re-running resumes rather than restarts.",
        )

    def _backoff(self, attempt: int, *, retry_after: float | None) -> float:
        """``min(base * 2**(attempt-1), cap)`` with **full** jitter, or ``Retry-After``.

        Full jitter — uniform over ``[0, computed]`` — rather than equal jitter, because the
        failure mode being defended against is several requests retrying in lockstep after a
        shared throttle, and full jitter decorrelates them hardest.

        Worst case is roughly 1+2+4+8 = 15s of sleeping before giving up, which is inside §14's
        five-minute cold-run target with room for the rest of the fetch.
        """
        if retry_after is not None:
            return min(retry_after, RETRY_AFTER_CAP_SECONDS)
        ceiling = min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_CAP_SECONDS)
        return self._jitter.uniform(0.0, ceiling)


def _retry_after(headers: Mapping[str, str], status: int) -> float | None:
    """``Retry-After`` in seconds, when present, sane, and relevant.

    Only honoured for 429. A ``Retry-After`` on a 403 or a 5xx is not something SEC documents, and
    trusting an undocumented header to set a sleep duration is how a run comes to hang for two
    minutes for no stated reason.
    """
    if status != 429:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        # The HTTP-date spelling is legal and SEC does not use it. Falling back to exponential
        # backoff is strictly safer than parsing a date we have never seen in the wild.
        return None
    return seconds if seconds >= 0 else None


def utcnow() -> datetime:
    """Timezone-aware now, for a :class:`~investo.domain.provenance.SourceContext`.

    Lives here rather than in a parser because ``tests/test_layering.py`` asserts that no module
    under ``ingest/edgar/`` **except this one** calls ``datetime.now`` — a parser that reads the
    clock cannot be run twice against one fixture with the same result.
    """
    return datetime.now(UTC)

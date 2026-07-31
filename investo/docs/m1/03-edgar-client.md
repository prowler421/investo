# M1 — EDGAR client

`ingest/edgar/client.py`. The single place in the codebase that may talk to sec.gov.

DESIGN §4.1 is normative and specific: a token-bucket limiter at ~5 req/s against SEC's cap of
10, a mandatory `User-Agent` from config with **no default**, `Accept-Encoding: gzip, deflate`,
exponential backoff on 403/429, and the CIK/accession transforms owned here. Nothing else in the
codebase may make an HTTP call to sec.gov.

Confirmed against SEC's own Webmaster FAQ (reviewed Aug 2024): the documented maximum is *"10
requests per second… carefully monitored to preserve equitable access,"* and the sample declared
bot headers are `User-Agent: Sample Company Name AdminContact@<sample company domain>.com`,
`Accept-Encoding: gzip, deflate`, and `Host`.

---

## 1. Interface

```python
class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...

@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str]
    url: str
    fetched_at: datetime
    from_cache: bool

class EdgarClient:
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
    ) -> None: ...

    def get(self, url: str, *, params: Mapping[str, str] | None = None) -> Response: ...
    def get_json(self, url: str, *, params: Mapping[str, str] | None = None) -> Any: ...

    @property
    def request_count(self) -> int: ...   # network requests only; cache hits excluded
```

`user_agent` is a required keyword with no default, mirroring `Settings.sec_user_agent`. There is
no code path that constructs an `EdgarClient` without one — which is the enforcement of §4.1's
"startup fails if unset," and the reason `tests/conftest.py` clears the whole `INVESTO_*`
environment (CLAUDE.md convention 2).

`clock` and `transport` are injection seams for tests, both defaulting to the real thing. A rate
limiter that sleeps against the wall clock cannot be tested in under a second per request, and a
retry policy tested against a real socket is not tested.

`jitter_seed` makes backoff reproducible in tests. Retries only happen on network paths, which
are outside the determinism gate (the gate runs from cached inputs), so the seed is a testing
affordance rather than a determinism requirement — stated so nobody later concludes the gate
depends on it.

`request_count` counts network requests and excludes cache hits, because that is the number
ROADMAP's "warm run makes zero HTTP calls" criterion is about.

---

## 2. Request flow

```
get(url, params)
  │
  ├─ key = Cache.key_for("GET", url, params)
  ├─ if not refresh:  hit = cache.get(key) → return Response(from_cache=True)
  │
  ├─ limiter.acquire()                      # blocks until a token is available
  ├─ httpx.get(url, headers=…)
  │
  ├─ classify(status, body)
  │    ├─ 200            → cache.put(...) → Response(from_cache=False)
  │    ├─ 404            → cache.put(...) → Response  (absence, not an error)
  │    ├─ 403 undeclared → ConfigError            (exit 5, no retry)
  │    ├─ 403 throttled  → retry
  │    ├─ 429            → retry, honouring Retry-After
  │    ├─ 5xx            → retry
  │    └─ other 4xx      → UpstreamFetchError     (exit 4, no retry)
  │
  └─ attempts exhausted  → UpstreamFetchError     (exit 4)
```

Two details in that flow are decisions rather than mechanics.

**The cache is checked before the limiter.** A warm run therefore takes no tokens and sleeps not
at all, which is what makes `investo facts AAPL` on a warm cache fast enough to iterate against.
It also means the limiter's state is a function of network requests only, which is what the
rate-limit test asserts.

**A 404 is cached.** Caching an absence looks wrong until you consider the alternative: a company
with no DEF 14A would be re-requested on every run forever, spending rate budget to re-learn a
stable fact. The cached 404 is a recorded absence with a `fetched_at`, and `--refresh` re-checks
it. Non-200 statuses other than 404 are **not** cached — a cached 503 would be a poisoned entry.

---

## 3. Token bucket

```python
class TokenBucket:
    def __init__(self, *, rate: float, capacity: float = 1.0, clock: Clock) -> None: ...
    def acquire(self) -> None: ...
```

**Capacity 1, not a burst.** A bucket with capacity 5 at 5 req/s can emit five requests in the
first instant, which is an instantaneous rate well above the 5 req/s the config asks for and
uncomfortably near the 10 req/s SEC monitors. Capacity 1 makes the limiter a strict spacer: every
request waits until 1/rate has elapsed since the last. §4.1's own reasoning applies — *the
penalty for being slightly too fast is minutes of downtime, and the reward for being exactly at
the limit is nothing* — and a burst buys nothing here, because M1's workload is a dozen sequential
requests, not a queue.

`rate` comes from `Settings.edgar_requests_per_second`, which M0 already validates as
`gt=0, le=10`.

Retries wait **and then** take a token. Backoff is not a substitute for the limiter: a 429
followed by an un-limited retry is precisely the pattern that gets an IP throttled for ten
minutes.

**Test:** with a fake clock, *n* `acquire` calls advance it by at least `(n-1)/rate`, and no two
recorded request times are closer than `1/rate`. Asserting only total elapsed time is weaker —
it passes for an implementation that emits everything at once and then sleeps.

---

## 4. Retry policy

| Trigger | Retried | Notes |
|---|---|---|
| 429 | yes | `Retry-After` honoured when present and ≤ 120s; capped so a hostile header cannot hang the run |
| 403, throttle body | yes | see classification below |
| 403, undeclared-tool body | **no** | `ConfigError`, exit 5 |
| 500, 502, 503, 504 | yes | |
| `httpx.TransportError` (connect, read, timeout) | yes | |
| 404 | no | absence |
| other 4xx | no | `UpstreamFetchError` |

Backoff: `min(base * 2**(attempt-1), cap)` with full jitter, `base = 1.0s`, `cap = 30.0s`,
`max_attempts = 5`. Worst case ≈ 1+2+4+8 = 15s of sleeping before giving up, which is inside the
5-minute cold-run target (§14) with room for the rest of the fetch.

Full jitter (uniform over `[0, computed]`) rather than equal jitter, because the failure mode
being defended against is several requests retrying in lockstep after a shared throttle, and full
jitter decorrelates them hardest.

### Classifying a 403

SEC returns 403 for both an undeclared automated tool and rate-limit rejection, and the two need
opposite handling: one is a config error that no amount of retrying fixes, the other is exactly
what retrying is for. The response body distinguishes them.

```python
_UNDECLARED = re.compile(rb"undeclared\s+automated\s+tool", re.IGNORECASE)
_THROTTLED  = re.compile(rb"request\s+rate", re.IGNORECASE)
```

Matched against the body, case-insensitively, on bytes. An unmatched 403 is treated as
**throttling** — the retryable branch — because guessing "config error" on an unrecognized body
would turn a transient upstream change into a hard exit 5 that the user cannot act on, while
guessing "throttle" costs at most four retries and then reports exit 4 honestly.

Regex on a body string is brittle by nature, and the mitigation is that being wrong is survivable
in one direction and tested in the other: the undeclared-tool fixture asserts **exactly one
request was made** and `ConfigError` raised. That is the violation test for "an undeclared-tool
403 is never retried."

---

## 5. Headers

```python
{
    "User-Agent": user_agent,              # from config; validated in M0
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",          # get_json only
}
```

`Host` is set by httpx. `From` is not sent — SEC's documented sample puts the contact address in
the `User-Agent`, and `Settings._validate_user_agent` (M0) already rejects a UA without an `@`
and rejects the `example.com` placeholder.

No cookies, no redirects across hosts. `follow_redirects=True` within sec.gov (the `/os/` →
`/about/` style moves are real), `False` off-host, because a redirect to a third party would send
SEC's declared contact address somewhere it does not belong.

---

## 6. URL and identifier transforms

§4.1: the client owns these; callers pass a plain `int` CIK and an `Accession`. Getting one wrong
produces a 404 that looks like missing data, which ROADMAP M1 names as a risk.

| Need | Form | Example | Owner |
|---|---|---|---|
| CIK on `data.sec.gov` | `CIK` + zero-pad to 10 | `CIK0000320193` | `client.cik_path()` |
| CIK in `/Archives/` path | unpadded decimal | `320193` | `client.archives_cik()` |
| Accession, canonical | dashed | `0000320193-25-000079` | `Accession.value` |
| Accession, `/Archives/` directory | undashed | `000032019325000079` | `Accession.nodashes` |
| Filing index page | dashed + `-index.htm` | `0000320193-25-000079-index.htm` | `Accession.index_url` |
| `frames` unit in path | `-per-` | `USD-per-shares` | `frames.py` |
| `companyfacts` unit key | `/` | `USD/shares` | `companyfacts.py` |
| Ownership doc, forms 3/4/5 | strip leading `xsl*/` | `xslF345X06/ownership.xml` → `ownership.xml` | `client.ownership_doc()` |
| Submissions overflow page | `CIK` + 10-pad + `-submissions-NNN.json` | `CIK0000320193-submissions-001.json` | `submissions_page_url()` |

Rows 6 and 7 are the same unit in two spellings, in two different places, and mixing them up is a
404 in one direction and a `KeyError` in the other. They are listed together so the pair is
visible.

Row 8 is the one that would otherwise be found by an M1b parser failing on well-formed input.
Every Form 3 and Form 4 in the observed submissions payload has
`primaryDocument = "xslF345X06/ownership.xml"` — an XSL-rendered viewer path. Using it verbatim
fetches a styled HTML document where `ownership.py` expects Form 4 XML, and the failure looks
like a parser bug rather than a URL bug. Confirmed against a live payload; see
[`04-parsers.md`](04-parsers.md#five-things-the-live-payload-contradicts-or-sharpens).

### URL builders

```python
def companyfacts_url(cik: int) -> str: ...
def submissions_url(cik: int) -> str: ...
def submissions_page_url(name: str) -> str: ...        # filings.files[].name
def frames_url(taxonomy: str, tag: str, unit: str, period: str) -> str: ...
def archives_doc_url(cik: int, accession: Accession, document: str) -> str: ...
def tickers_exchange_url() -> str: ...
```

Functions rather than f-strings at call sites, so the padding rules have one implementation and
one test. Each builder is tested against a hand-written expected URL — the only place in the
suite where asserting a literal is right, because the literal *is* the specification and there is
no derivation to assert instead.

**Boundary case that will otherwise be found in production:** a CIK below 1,000,000 pads to ten
digits on `data.sec.gov` and does not pad in `/Archives/`. Apple (320193) is such a CIK, so the
default fixture exercises it. A CIK with ten digits already, and CIK 1, both get an assertion.

---

## 7. The choke point, enforced

CLAUDE.md convention 6: nothing outside `ingest/edgar/client.py` may call sec.gov. A convention
that is only written down is a convention that holds until someone is in a hurry.

`tests/test_layering.py` walks the AST of every module in the installed package and asserts:

1. **No string literal containing `sec.gov`** outside `ingest/edgar/client.py`. URL builders live
   in the client; every other module receives a URL or calls a builder.
2. **No import of `httpx`** outside `ingest/edgar/client.py`, `ingest/prices/*.py`, and
   `ingest/finra.py`. Prices and FINRA are different hosts with different rate limits and are
   entitled to their own clients — but nothing else is.
3. **No import of `investo.ingest` from `investo.domain`.** The dependency flow in §3 is
   one-directional, and `domain/` is the bottom.

AST rather than grep, so a literal split across a concatenation or an f-string is still caught,
and so a comment mentioning sec.gov does not fail the build.

### FINRA gets its own client, and that is not a loophole

`ingest/finra.py` (M1b) talks to `api.finra.org`. It does not reuse `EdgarClient`, for two
reasons that are easy to get backwards: reusing it would send SEC's declared contact `User-Agent`
to FINRA, which is at best meaningless and at worst misrepresents who is calling; and it would
put FINRA's traffic through SEC's token bucket, so a FINRA fetch would slow EDGAR requests to
protect a limit that does not apply to it.

It shares the `Cache`, which is host-agnostic by design.

---

## 8. Errors

```python
class UndeclaredUserAgentError(ConfigError):
    exit_code = ExitCode.CONFIG_ERROR              # declared, not inherited — see below

class SecThrottledError(UpstreamFetchError):
    exit_code = ExitCode.UPSTREAM_FETCH_FAILURE
```

Both restate `exit_code` in their own body even though the parent already sets the same value.
CLAUDE.md convention 1 requires it — *a new error class declares its own `exit_code`* — and
`tests/test_errors.py::test_every_error_subclass_declares_a_code` enforces it with
`assert "exit_code" in vars(cls)`, which an inherited attribute does not satisfy.

**That test needs one change in M1, and it is easy to miss.** It walks
`InvestoError.__subclasses__()`, which returns *direct* subclasses only. These two are
grandchildren, so today they would escape the check entirely — the guarantee would appear
enforced while the first class that actually forgot its code slipped through. M1 makes the walk
recursive. That is a change to an M0 test in service of an M0 convention, so it belongs in M1's
first commit rather than being discovered later.

`SecThrottledError`'s message names the request count and the elapsed time, because the useful
next action after a throttle is to wait, and §4.1 says the threshold clears after ten minutes
below the rate.

Neither class is strictly necessary — `ConfigError` and `UpstreamFetchError` would do — but the
distinct types are what let a test assert *which* condition fired rather than only which exit
code, and "an undeclared-tool 403 is not retried" is a guarantee about the condition.

---

## 9. Live smoke test

§11 asks for "one opt-in live smoke test." It is marked `network`, deselected by default via
`addopts = "-m 'not network'"`, and it fetches exactly one small payload
(`company_tickers_exchange.json`) with the UA from the environment.

Opt-in and single-request because CI sets no `INVESTO_*` variables (CLAUDE.md convention 7), so
the test *cannot* run there — a test that reaches the network should fail rather than quietly
succeed. Its value is local: it is the thing you run when a fixture-driven suite is green and
EDGAR still returns 403, which is a real morning.

Adding the `network` marker means adding it to `[tool.pytest.ini_options] markers` — `--strict-markers`
is on, so an unregistered marker is an error rather than a typo that silently runs everything.

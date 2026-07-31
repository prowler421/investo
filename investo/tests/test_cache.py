"""The raw-payload cache: append-only, content-addressed, and deliberately incapable of four things.

DESIGN.md §4.4 is normative — key is ``sha256(url + params)``, value is raw bytes plus
``fetched_at`` plus headers, entries are never mutated and never evicted by default, each carries a
schema version, and ``--refresh`` writes a *new* entry rather than overwriting. §4.4 also says why
this is load-bearing rather than an optimization: **upstream drift.** yfinance's back-adjustment and
EDGAR's ``frames`` both mutate historical values, so the cache is the only immutable record of what
the model actually saw. A cache that can be silently rewritten is not that record.

Half the tests here are about what the cache must *not* do (``docs/m1/02-cache.md`` §5): filter by
``as_of``, parse, evict, or lose the previous view on refresh. Each of those is something the cache
is well placed to do, and each would be invisible in the report if it happened.
"""

from __future__ import annotations

import gzip
import hashlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode

import pytest

from investo.errors import ConfigError, ExitCode
from investo.ingest.cache import CACHE_FORMAT_VERSION, STORED_HEADERS, Cache, CacheEntry

URL: Final = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
OTHER_URL: Final = "https://data.sec.gov/submissions/CIK0000320193.json"
BODY: Final = b'{"cik":320193,"entityName":"Apple Inc."}'
OTHER_BODY: Final = b'{"cik":320193,"entityName":"Apple Inc.","revised":true}'
CACHE_DIR_NAME: Final = "cache"
"""What ``conftest.cache`` names its directory. Pinned by the first test below."""


def _root(tmp_path: Path) -> Path:
    return tmp_path / CACHE_DIR_NAME


def _manifest_lines(root: Path) -> list[dict[str, Any]]:
    """Every manifest record, in file order.

    Reads the file rather than calling a private method: the on-disk layout is specified in
    ``docs/m1/02-cache.md`` §1, so a test that goes through the public API only could not tell the
    difference between "appended a line" and "rewrote the file".
    """
    text = (root / "manifest.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _blobs(root: Path) -> list[Path]:
    """Every stored blob, excluding the ``blobs/tmp`` staging directory."""
    staging = root / "blobs" / "tmp"
    return sorted(path for path in (root / "blobs").rglob("*.gz") if staging not in path.parents)


def _blob_path(root: Path, digest: str) -> Path:
    """The two-level fan-out path from §1: ``blobs/<aa>/<bb>/<sha256>.gz``."""
    return root / "blobs" / digest[:2] / digest[2:4] / f"{digest}.gz"


def _put(
    cache: Cache,
    key: str,
    body: bytes,
    *,
    url: str = URL,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
) -> CacheEntry:
    return cache.put(
        key=key,
        url=url,
        method="GET",
        params={},
        status=status,
        headers=headers or {},
        body=body,
    )


def test_the_cache_fixture_lives_under_tmp_path(cache: Cache, tmp_path: Path) -> None:
    """``conftest.cache`` builds ``tmp_path / "cache"``, and several tests here rely on that path.

    Asserted rather than assumed, because the helpers above reach into the directory directly. If
    the fixture moved, they would silently inspect an empty tree and the append-only assertions
    would pass over nothing.
    """
    key = Cache.key_for("GET", URL, None)
    _ = _put(cache, key, BODY)
    assert (_root(tmp_path) / "FORMAT").exists()
    assert _manifest_lines(_root(tmp_path))


# ---------------------------------------------------------------------------
# key_for
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_key_is_order_independent_for_params() -> None:
    """Two callers passing the same query in a different dict order hit the same entry.

    Without canonical ordering the cache would miss on a request it already holds, and the symptom
    is not a wrong answer — it is a second identical fetch that spends rate budget and writes a
    duplicate blob nobody can explain.
    """
    forward = Cache.key_for("GET", URL, {"a": "1", "b": "2", "c": "3"})
    reverse = Cache.key_for("GET", URL, {"c": "3", "b": "2", "a": "1"})
    assert forward == reverse


@pytest.mark.spec
def test_key_distinguishes_the_method_the_url_and_the_params() -> None:
    """All three are part of the request, so all three are part of the key.

    A key that ignored any one of them would serve one payload for two different requests, which is
    the one cache bug that produces a plausible number from the wrong document.
    """
    base = Cache.key_for("GET", URL, None)
    assert base != Cache.key_for("POST", URL, None)
    assert base != Cache.key_for("GET", OTHER_URL, None)
    assert base != Cache.key_for("GET", URL, {"a": "1"})
    assert Cache.key_for("GET", URL, {"a": "1"}) != Cache.key_for("GET", URL, {"a": "2"})


@pytest.mark.spec
def test_key_is_the_documented_digest_and_omits_the_format_version() -> None:
    """§4.4: the key is ``sha256(method \\n url \\n canonical_params)`` — and nothing else.

    Recomputed here from the documented formula, which is the only way to assert the *absence* of
    the format version: folding it in would change every digest and fail this line. §4.4 says
    entries *carry* a schema version so a parser change can invalidate derived data without
    discarding raw payloads, and a versioned key would orphan every blob on a format bump instead.
    """
    params = {"CIK": "0000320193", "type": "10-K"}
    canonical = urlencode(sorted(params.items()))
    expected = hashlib.sha256(f"GET\n{URL}\n{canonical}".encode()).hexdigest()
    assert Cache.key_for("GET", URL, params) == expected


def test_method_is_normalized_but_the_url_is_not() -> None:
    """HTTP methods are case-insensitive; URLs are not.

    Lowercasing a URL would merge ``/CIK0000320193.json`` with a lowercase spelling that SEC does
    not serve, so the cache would answer for a request that 404s in production.
    """
    assert Cache.key_for("get", URL, None) == Cache.key_for("GET", URL, None)
    assert Cache.key_for("GET", URL.lower(), None) != Cache.key_for("GET", URL, None)


def test_no_params_and_empty_params_are_one_request() -> None:
    """``None`` and ``{}`` describe the same request, so they resolve to one entry.

    The client passes ``params or {}`` in one place and ``None`` in another; if those disagreed, a
    warm cache would miss on exactly the endpoints that take no query string, which is most of them.
    """
    assert Cache.key_for("GET", URL, None) == Cache.key_for("GET", URL, {})


# ---------------------------------------------------------------------------
# get / put
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_get_returns_exact_bytes(cache: Cache) -> None:
    """``docs/m1/02-cache.md`` §5: the cache returns bytes and never parses.

    The body here is deliberately not valid JSON and not valid UTF-8. A cache that parsed — or that
    round-tripped through ``str`` — would put the parser inside the reproducibility boundary, so a
    parser change would invalidate the raw payloads, which is the opposite of what §4.4's schema
    version exists to allow. It would also fail outright on a CSV, and ``stooq`` returns one.
    """
    body = b"Date,Close\n2026-07-31,214.50\n\x00\xff\xfe not json {"
    key = Cache.key_for("GET", URL, None)
    _ = _put(cache, key, body)
    hit = cache.get(key)
    assert hit is not None
    assert hit[1] == body


@pytest.mark.spec
def test_put_then_get_round_trips_the_entry(cache: Cache) -> None:
    """Everything §4.4 says an entry carries: bytes, ``fetched_at``, headers, and a schema version.

    ``content_sha256`` is asserted as the digest of the body rather than against a literal, because
    the property the store depends on is that the blob name *is* the content hash — a name derived
    any other way makes deduplication a coincidence.
    """
    key = Cache.key_for("GET", URL, None)
    written = _put(cache, key, BODY, headers={"content-type": "application/json"})
    hit = cache.get(key)
    assert hit is not None
    entry, body = hit
    assert body == BODY
    assert entry.key == key
    assert entry.url == URL
    assert entry.method == "GET"
    assert entry.status == 200
    assert entry.content_sha256 == hashlib.sha256(BODY).hexdigest()
    assert entry.content_length == len(BODY)
    assert entry.format_version == CACHE_FORMAT_VERSION
    assert entry.fetched_at.tzinfo is not None, "a naive fetched_at means something per machine"
    assert entry.content_sha256 == written.content_sha256


def test_get_on_a_missing_key_is_none(cache: Cache) -> None:
    """A miss is ``None``, not an exception: a cold fetch is the normal case, not an error."""
    assert cache.get(Cache.key_for("GET", URL, None)) is None


@pytest.mark.spec
def test_refresh_keeps_prior_entry_retrievable(cache: Cache, tmp_path: Path) -> None:
    """§4.4 and §3: ``--refresh`` supersedes without destroying.

    This is what makes yfinance's back-adjustment survivable. Two pulls on different dates
    legitimately disagree, and after a refresh both are still on disk with their own ``fetched_at``,
    so the disagreement is inspectable rather than a mystery. An implementation that overwrote the
    manifest line or the blob would pass every read-path test in this file and destroy the record
    §4.4 exists to keep.
    """
    key = Cache.key_for("GET", URL, None)
    first = _put(cache, key, BODY)
    second = _put(cache, key, OTHER_BODY)
    root = _root(tmp_path)

    lines = _manifest_lines(root)
    assert len(lines) == 2, "a refresh appends; it does not overwrite"
    assert lines[0]["content_sha256"] == first.content_sha256
    assert lines[1]["content_sha256"] == second.content_sha256

    prior = _blob_path(root, first.content_sha256)
    assert gzip.decompress(prior.read_bytes()) == BODY, "the superseded view is still readable"


@pytest.mark.spec
def test_get_returns_the_newest_generation(cache: Cache) -> None:
    """Three entries for one key, one answer, and it is the newest.

    Newest rather than oldest because ``--refresh`` exists to supersede: an append-only store that
    served the first write would make ``--refresh`` a no-op on the next run. Also part of what M1
    can gate on determinism — the resolution is by ``fetched_at`` then line order, so it is the same
    every time rather than whatever the filesystem returns first.
    """
    key = Cache.key_for("GET", URL, None)
    for body in (b"generation-1", b"generation-2", b"generation-3"):
        _ = _put(cache, key, body)
    hit = cache.get(key)
    assert hit is not None
    assert hit[1] == b"generation-3"


@pytest.mark.spec
def test_identical_content_writes_no_new_blob_but_a_new_manifest_line(
    cache: Cache, tmp_path: Path
) -> None:
    """§4.4: a refresh that gets identical bytes back writes a new entry and no new blob.

    This is how "append-only" and "don't store companyfacts forty times" coexist. Both halves are
    asserted, because each without the other is a different bug: no new line loses the second
    ``fetched_at``, and a new blob makes twenty refreshes of a 40 MB payload cost 800 MB.
    """
    key = Cache.key_for("GET", URL, None)
    _ = _put(cache, key, BODY)
    _ = _put(cache, key, BODY)
    root = _root(tmp_path)
    assert len(_manifest_lines(root)) == 2
    assert len(_blobs(root)) == 1


@pytest.mark.spec
def test_blob_is_addressed_and_stamped_by_its_content(tmp_path: Path) -> None:
    """``docs/m1/02-cache.md`` §1: the blob name is the content hash and the gzip mtime is zero.

    gzip writes a timestamp into its header by default, and a blob whose bytes change on every write
    is not content-addressed. Two separate caches rather than two writes into one, because the
    second write into one cache is skipped by design and would prove nothing about the bytes.

    The stronger claim — that the whole file is a function of the content — is asserted by the test
    below, which currently fails.
    """
    key = Cache.key_for("GET", URL, None)
    first = Cache(tmp_path / "one")
    second = Cache(tmp_path / "two")
    digest = _put(first, key, BODY).content_sha256
    assert _put(second, key, BODY).content_sha256 == digest

    one = _blob_path(tmp_path / "one", digest).read_bytes()
    two = _blob_path(tmp_path / "two", digest).read_bytes()
    assert one[4:8] == b"\x00\x00\x00\x00", "bytes 4:8 of a gzip header are its mtime"
    assert two[4:8] == b"\x00\x00\x00\x00"
    assert gzip.decompress(one) == gzip.decompress(two) == BODY


@pytest.mark.spec
def test_blob_bytes_are_a_function_of_content_only(tmp_path: Path) -> None:
    """``docs/m1/06-testing.md`` §6: write the same body twice, get identical files on disk.

    Two *different* cache directories, deliberately, rather than two writes to one — a second write
    to the same cache is a no-op once the content hash matches, so it would pass without the
    guarantee holding at all.

    This caught a real defect. ``_write_blob`` stages through ``blobs/tmp/<uuid>.gz`` and hands the
    open file to ``gzip.GzipFile``, which — given a ``fileobj`` and no explicit ``filename`` —
    defaults to ``fileobj.name`` and writes that basename into the gzip header with the FNAME flag
    set. So the stored bytes carried a random UUID: ``mtime`` was zero exactly as the design says,
    and the file still differed on every write. Fixed with ``filename=""``.

    The lesson is why this test asserts the *bytes* rather than the digest: ``content_sha256`` is
    computed over the body before compression, so every hash-level assertion in this file passed
    while the on-disk bytes were non-deterministic.
    """
    key = Cache.key_for("GET", URL, None)
    digest = _put(Cache(tmp_path / "one"), key, BODY).content_sha256
    _ = _put(Cache(tmp_path / "two"), key, BODY)
    one = _blob_path(tmp_path / "one", digest).read_bytes()
    two = _blob_path(tmp_path / "two", digest).read_bytes()
    assert one == two
    # FLG bit 3 is FNAME. Asserted directly so a regression names its own cause instead of
    # producing an inequality nobody can read.
    assert one[3] & 0x08 == 0, "gzip header carries a filename; blob bytes are not content-only"


@pytest.mark.spec
def test_entries_survive_many_writes(cache: Cache, tmp_path: Path) -> None:
    """§4.4 and §5: the cache never evicts without ``prune``. No TTL, no size cap, no LRU.

    Written as a volume test because eviction is the kind of feature that gets added for a good
    reason — a cache directory that grew unexpectedly — and it would break the property that a run
    is reproducible from cache. Nothing else in the suite would notice: every other test writes a
    handful of entries.
    """
    keys = [Cache.key_for("GET", f"{URL}?page={index}", None) for index in range(60)]
    for index, key in enumerate(keys):
        _ = _put(cache, key, f"payload-{index}".encode())
    for index, key in enumerate(keys):
        hit = cache.get(key)
        assert hit is not None, f"entry {index} was evicted"
        assert hit[1] == f"payload-{index}".encode()
    assert len(_manifest_lines(_root(tmp_path))) == 60


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_headers_are_allowlisted(cache: Cache) -> None:
    """§1: an allowlist, not a denylist.

    ``set-cookie`` is something we should not be persisting at all, and a CDN request ID is
    non-deterministic — storing the whole block would put a per-request value into a file the
    appendix hashes, breaking determinism for no gain. Both directions are asserted: the useful
    headers survive, and the rest are gone rather than merely unread.
    """
    key = Cache.key_for("GET", URL, None)
    entry = _put(
        cache,
        key,
        BODY,
        headers={
            "Content-Type": "application/json",
            "ETag": '"a1b2c3"',
            "Set-Cookie": "session=secret; Path=/",
            "X-Amz-Request-Id": "1234567890",
            "Date": "Fri, 31 Jul 2026 10:58:11 GMT",
        },
    )
    assert "set-cookie" not in entry.headers
    assert "x-amz-request-id" not in entry.headers
    assert entry.headers["content-type"] == "application/json"
    assert entry.headers["etag"] == '"a1b2c3"'
    assert set(entry.headers) <= STORED_HEADERS
    assert set(entry.headers) == {"content-type", "etag"}


def test_header_names_are_lowercased(cache: Cache) -> None:
    """HTTP header names are case-insensitive and the manifest is not.

    Storing ``ETag`` as sent would make the allowlist depend on the server's capitalization, so the
    same response served by two CDNs would produce two different manifest records — and the appendix
    hash would differ for reasons that are not about the data.
    """
    key = Cache.key_for("GET", URL, None)
    entry = _put(cache, key, BODY, headers={"CONTENT-TYPE": "text/csv"})
    assert entry.headers == {"content-type": "text/csv"}


# ---------------------------------------------------------------------------
# Format version
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_future_format_version_exits_5(tmp_path: Path) -> None:
    """§2: a cache written by a newer format is not guessed at.

    That is the one scenario where "reproducible from cache" silently stops being true, so it fails
    loudly and names the fix. Exit 5 rather than 4 because nothing was fetched and nothing upstream
    is wrong — the configuration points at a directory this build cannot read.
    """
    root = tmp_path / "future"
    root.mkdir()
    (root / "FORMAT").write_text(f"{CACHE_FORMAT_VERSION + 1}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        _ = Cache(root)
    assert caught.value.exit_code == ExitCode.CONFIG_ERROR
    assert caught.value.hint is not None, "an unreadable cache needs an actionable next step"


@pytest.mark.spec
def test_the_current_format_version_is_accepted(tmp_path: Path) -> None:
    """The boundary: ``>`` and not ``>=``.

    Without this, an off-by-one in the version check would reject every cache the build itself
    wrote, and the failure would look like corruption rather than like a comparison operator.
    """
    root = tmp_path / "current"
    root.mkdir()
    (root / "FORMAT").write_text(f"{CACHE_FORMAT_VERSION}\n", encoding="utf-8")
    assert Cache(root).get(Cache.key_for("GET", URL, None)) is None


def test_an_older_format_version_is_readable(tmp_path: Path) -> None:
    """§2: downgrading a known-older format is a migration, and there are none yet.

    So an older version reads as-is rather than raising. Asserted because the symmetrical
    implementation — reject anything that is not exactly this version — would make the first format
    bump delete every user's cache instead of migrating it.
    """
    root = tmp_path / "older"
    root.mkdir()
    (root / "FORMAT").write_text("0\n", encoding="utf-8")
    assert Cache(root).get(Cache.key_for("GET", URL, None)) is None


@pytest.mark.spec
def test_a_format_file_that_is_not_a_version_exits_5(tmp_path: Path) -> None:
    """A ``FORMAT`` holding something that is not an integer is the same class of problem.

    Treating it as version 1 would be a guess about a directory whose provenance is unknown, and the
    hint is the point: point ``--cache-dir`` elsewhere, or delete it.
    """
    root = tmp_path / "garbled"
    root.mkdir()
    (root / "FORMAT").write_text("banana\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        _ = Cache(root)
    assert caught.value.exit_code == ExitCode.CONFIG_ERROR


def test_a_new_directory_is_initialized_with_the_current_version(tmp_path: Path) -> None:
    """An absent ``FORMAT`` is a new cache, not a broken one.

    First run is the common case, and it has to be silent. Reading the file back rather than
    trusting the constructor, because "initialize" is what makes every later version check work.
    """
    root = tmp_path / "brand-new"
    _ = Cache(root)
    assert (root / "FORMAT").read_text(encoding="utf-8").strip() == str(CACHE_FORMAT_VERSION)


# ---------------------------------------------------------------------------
# The absence that is the enforcement
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_cache_api_has_no_as_of() -> None:
    """``docs/m1/02-cache.md`` §5: the cache cannot filter by ``as_of``. The absence is the rule.

    ``--as-of`` is about *filing* dates and belongs to ``normalize`` (§4.2b). A cache that dropped
    entries fetched after ``as_of`` would make a warm run differ from a cold one for reasons
    invisible in the report, and would break the property that ``as_of`` reconstruction is a pure
    function of the cached payloads.

    There is nothing to call and nothing to assert on, so the test inspects the signatures instead.
    The expected method set is asserted first: an empty loop would make this pass by accident, which
    for a test whose subject is an absence is the exact failure to guard against.
    """
    public = [name for name in dir(Cache) if not name.startswith("_")]
    assert {"get", "put", "prune", "manifest_hash", "key_for"} <= set(public)
    for name in [*public, "__init__"]:
        member = getattr(Cache, name)
        if not callable(member):
            continue
        parameters = inspect.signature(member).parameters
        assert "as_of" not in parameters, f"Cache.{name} takes an as_of"
    assert "as_of" not in {field.name for field in fields(CacheEntry)}


# ---------------------------------------------------------------------------
# manifest_hash
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_manifest_hash_is_stable_and_order_independent(cache: Cache, tmp_path: Path) -> None:
    """DESIGN.md §9.1 item 10: a fingerprint of the entries this run used.

    Stable so two runs over the same cache agree, and order-independent so the fingerprint describes
    the run's *inputs* rather than the order the fetcher happened to visit them in — which is a
    detail a future parallel fetcher would change without changing what the report saw.
    """
    first, second = Cache.key_for("GET", URL, None), Cache.key_for("GET", OTHER_URL, None)
    _ = _put(cache, first, BODY)
    _ = _put(cache, second, OTHER_BODY, url=OTHER_URL)
    expected = cache.manifest_hash()
    assert cache.manifest_hash() == expected, "a second call must not change the answer"

    backwards = Cache(_root(tmp_path))
    assert backwards.get(second) is not None
    assert backwards.get(first) is not None
    assert backwards.manifest_hash() == expected


@pytest.mark.spec
def test_manifest_hash_ignores_an_unrelated_key(tmp_path: Path) -> None:
    """The assertion that argues for spec question 4, and the reason it is not a whole-file hash.

    Hashing ``manifest.jsonl`` would make the value printed in an AAPL report change when somebody
    fetches MSFT — a change to the *directory*, not to the report — and DESIGN.md §11's
    byte-identical-output gate would then fail on an unrelated cache write. Three separate ``Cache``
    instances, because "this run" means the instance's lifetime.
    """
    root = _root(tmp_path)
    subject = Cache.key_for("GET", URL, None)
    unrelated = Cache.key_for("GET", OTHER_URL, None)

    first_run = Cache(root)
    _ = _put(first_run, subject, BODY)
    expected = first_run.manifest_hash()

    intervening = Cache(root)
    _ = _put(intervening, unrelated, OTHER_BODY, url=OTHER_URL)
    assert intervening.manifest_hash() != expected, "a different run read different things"

    third_run = Cache(root)
    assert third_run.get(subject) is not None
    assert third_run.manifest_hash() == expected


@pytest.mark.spec
def test_manifest_hash_matches_between_a_cold_and_a_warm_run(tmp_path: Path) -> None:
    """Spec question 10, and the divergence ``manifest_hash``'s own docstring records.

    ``docs/m1/02-cache.md`` §4 says ``get`` records a read and a miss records nothing, which would
    make the hash empty on a cold run — while the fetch summary in ``docs/m1/README.md`` §3 prints a
    non-empty ``manifest`` value beside sources whose status is ``fetched``. Both cannot hold. The
    implementation records entries *used*, a hit or a fresh ``put``, so the cold run and the warm
    run after it produce the same fingerprint. That is the property the appendix value is for: *did
    this report see the same data as that one?*
    """
    root = _root(tmp_path)
    keys = [Cache.key_for("GET", f"{URL}?page={index}", None) for index in range(3)]

    cold = Cache(root)
    for index, key in enumerate(keys):
        _ = _put(cold, key, f"payload-{index}".encode())

    warm = Cache(root)
    for key in keys:
        assert warm.get(key) is not None

    assert warm.manifest_hash() == cold.manifest_hash()


@pytest.mark.spec
def test_manifest_hash_changes_when_the_content_changes(tmp_path: Path) -> None:
    """The converse, or the fingerprint would answer "yes" to every question.

    Keyed on ``(key, content_sha256)`` rather than on the key alone, so a payload that drifted
    upstream between two runs produces a different fingerprint even though the request was identical
    — which is the case §4.4 says the cache exists to make visible.
    """
    root = _root(tmp_path)
    key = Cache.key_for("GET", URL, None)

    before = Cache(root)
    _ = _put(before, key, BODY)

    after = Cache(root)
    _ = _put(after, key, OTHER_BODY)

    assert after.manifest_hash() != before.manifest_hash()


def test_a_miss_is_not_recorded_in_the_manifest_hash(cache: Cache) -> None:
    """§4: ``get`` records the entry it returned, and a miss records nothing.

    A miss has no ``content_sha256`` to record, so folding it in would mean fingerprinting the
    absence of data — and two runs that missed on different keys would then look like they saw
    different inputs when neither saw any.
    """
    empty = cache.manifest_hash()
    assert cache.get(Cache.key_for("GET", "https://data.sec.gov/nothing.json", None)) is None
    assert cache.manifest_hash() == empty


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_corrupt_manifest_line_exits_5(cache: Cache, tmp_path: Path) -> None:
    """A malformed record means the directory is unusable as configured, not that a line is skipped.

    Skipping it would silently drop a payload the user believes is cached, and the run would then
    refetch — or worse, report thinner coverage — with nothing on stderr. The line number is in the
    message because the next question is always which line.
    """
    key = Cache.key_for("GET", URL, None)
    _ = _put(cache, key, BODY)
    manifest = _root(tmp_path) / "manifest.jsonl"
    with manifest.open("a", encoding="utf-8") as handle:
        _ = handle.write("this is not json\n")

    with pytest.raises(ConfigError) as caught:
        _ = cache.get(key)
    assert caught.value.exit_code == ExitCode.CONFIG_ERROR
    assert "line 2" in caught.value.message


@pytest.mark.spec
def test_a_structurally_wrong_manifest_record_exits_5(cache: Cache, tmp_path: Path) -> None:
    """Valid JSON with the wrong shape is the more likely corruption, and it fails the same way.

    A hand-edited manifest, or one written by a build whose record shape differed, parses as JSON
    and then raises ``KeyError`` several frames away from the file that caused it. Converted to a
    ``ConfigError`` naming the file, because that is the only way the message is actionable.
    """
    manifest = _root(tmp_path) / "manifest.jsonl"
    with manifest.open("a", encoding="utf-8") as handle:
        _ = handle.write(json.dumps({"key": "abc", "url": URL}) + "\n")
    with pytest.raises(ConfigError):
        _ = cache.get(Cache.key_for("GET", URL, None))


@pytest.mark.spec
def test_a_naive_fetched_at_in_the_manifest_exits_5(cache: Cache, tmp_path: Path) -> None:
    """The one field whose *value* is validated on read, and it is validated for the usual reason.

    A record whose ``fetched_at`` carries no offset would compare against ``now`` in ``prune`` and
    against other entries in ``get``, both of which raise or mislead depending on the machine.
    Better to refuse the file than to assume a timezone for it.
    """
    key = Cache.key_for("GET", URL, None)
    entry = _put(cache, key, BODY)
    manifest = _root(tmp_path) / "manifest.jsonl"
    record = _manifest_lines(_root(tmp_path))[0]
    record["fetched_at"] = entry.fetched_at.replace(tzinfo=None).isoformat()
    manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        _ = cache.get(key)


@pytest.mark.spec
def test_interrupted_put_leaves_no_dangling_entry(
    cache: Cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§1: **blob before manifest, always.**

    A blob with no manifest line is invisible garbage that ``prune`` collects. A manifest line with
    no blob is a dangling reference that crashes a warm run. The ordering makes the first failure
    mode possible and the second impossible, so this test crashes ``put`` in the window between the
    two writes and asserts the cache is still readable afterwards.

    The append is patched through ``monkeypatch.setattr`` with the name as a string, which is also
    how the reverse ordering would be caught: patch the blob write instead and the manifest line
    would already be on disk.
    """
    key = Cache.key_for("GET", URL, None)
    root = _root(tmp_path)

    def boom(entry: CacheEntry) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(cache, "_append", boom)
    with pytest.raises(OSError, match="no space"):
        _ = _put(cache, key, BODY)

    assert cache.get(key) is None, "no half-written entry is visible"
    assert Cache(root).get(key) is None, "and not to a later process either"
    assert len(_blobs(root)) == 1, "the orphaned blob is on disk, which is the survivable half"

    report = Cache(root).prune(older_than=timedelta(0), now=datetime.now(UTC))
    assert report.blobs_removed == 1
    assert _blobs(root) == []

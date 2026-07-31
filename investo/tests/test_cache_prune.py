"""``cache prune``: the two-phase guarantee, and the boundary README's wording depends on.

``docs/m1/02-cache.md`` §6 states the correctness argument as an ordering. Select, then rewrite the
manifest, then collect the blobs no survivor references — because the reverse leaves a window in
which the manifest points at deleted files. So the guarantee under test is: *after any prune, every
surviving manifest entry still has its blob*, and no unreferenced blob is left behind.

The second condition on selection is the one README does not mention and that is not optional: an
entry is prunable only if it is **not the newest for its key**. Pruning the only entry for a key
turns the next run into a cold fetch of something the user believes is cached; pruning the newest
while keeping an older one silently reverts the cache to a stale view.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from investo.ingest.cache import Cache, CacheEntry

URL: Final = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
BODY: Final = b'{"cik":320193,"generation":"first"}'
OTHER_BODY: Final = b'{"cik":320193,"generation":"second"}'
THIRD_BODY: Final = b'{"cik":320193,"generation":"third"}'
NINETY_DAYS: Final = timedelta(days=90)

NOW: Final = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
"""The ``now`` every prune below is given.

Fixed and passed in, because a function that reads the clock cannot be tested at a boundary — and
"older than 90 days" has a boundary that decides whether ``--older-than 90d`` means what README
says.
"""


def _root(tmp_path: Path) -> Path:
    """Where ``conftest.cache`` puts its directory; pinned by ``test_cache.py``."""
    return tmp_path / "cache"


def _manifest_lines(root: Path) -> list[dict[str, Any]]:
    text = (root / "manifest.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _blobs(root: Path) -> list[Path]:
    """Every stored blob, excluding the ``blobs/tmp`` staging directory."""
    staging = root / "blobs" / "tmp"
    return sorted(path for path in (root / "blobs").rglob("*.gz") if staging not in path.parents)


def _put(cache: Cache, key: str, body: bytes) -> CacheEntry:
    return cache.put(key=key, url=URL, method="GET", params={}, status=200, headers={}, body=body)


def _age(root: Path, ages: Sequence[timedelta]) -> None:
    """Backdate manifest line *i* to ``NOW - ages[i]``.

    ``put`` stamps ``datetime.now(UTC)``, which is right for production and useless for a boundary
    test, so the records are rewritten afterwards. Rewritten from what ``put`` produced rather than
    hand-authored: a hand-written manifest would keep passing after a record-shape change, which is
    the failure mode that makes fixture-shaped tests worthless a milestone later.
    """
    manifest = root / "manifest.jsonl"
    records = _manifest_lines(root)
    assert len(records) == len(ages), "the ages list has to describe every line"
    for record, age in zip(records, ages, strict=True):
        record["fetched_at"] = (NOW - age).isoformat()
    manifest.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


@pytest.mark.spec
def test_every_surviving_entry_resolves(cache: Cache, tmp_path: Path) -> None:
    """``docs/m1/02-cache.md`` §6: after any prune, every surviving entry still has its blob.

    The cache holds three generations of one key plus two unrelated keys, one of which is old enough
    to prune and is the only entry for its key. A prune that got the ordering backwards — collect
    blobs, then rewrite the manifest — would leave the manifest pointing at deleted files, and the
    next warm run would raise instead of serving a payload the user believes is cached.
    """
    root = _root(tmp_path)
    subject = Cache.key_for("GET", URL, None)
    ancient = Cache.key_for("GET", f"{URL}?p=ancient", None)
    recent = Cache.key_for("GET", f"{URL}?p=recent", None)

    _ = _put(cache, subject, BODY)
    _ = _put(cache, subject, OTHER_BODY)
    _ = _put(cache, subject, THIRD_BODY)
    _ = _put(cache, ancient, b"ancient-payload")
    _ = _put(cache, recent, b"recent-payload")
    _age(
        root,
        [
            timedelta(days=200),
            timedelta(days=150),
            timedelta(days=1),
            timedelta(days=400),
            timedelta(days=1),
        ],
    )

    report = Cache(root).prune(older_than=NINETY_DAYS, now=NOW)
    assert report.entries_removed == 2, "the two superseded generations of the subject key"

    reader = Cache(root)
    for key, expected in ((subject, THIRD_BODY), (ancient, b"ancient-payload")):
        hit = reader.get(key)
        assert hit is not None, "a key that had an entry still has one"
        assert hit[1] == expected
    assert reader.get(recent) is not None


@pytest.mark.spec
def test_prune_leaves_no_unreferenced_blob(cache: Cache, tmp_path: Path) -> None:
    """The other half of the same guarantee: nothing orphaned, nothing dangling.

    Asserted as an equality between the two sets rather than a count, because the two failure modes
    are opposite and a count catches neither: a leftover blob is wasted disk that the next prune has
    to find, and a missing one is a crash on the next warm run.
    """
    root = _root(tmp_path)
    key = Cache.key_for("GET", URL, None)
    for body in (BODY, OTHER_BODY, THIRD_BODY):
        _ = _put(cache, key, body)
    _age(root, [timedelta(days=200), timedelta(days=150), timedelta(days=1)])

    _ = Cache(root).prune(older_than=NINETY_DAYS, now=NOW)

    referenced = {record["content_sha256"] for record in _manifest_lines(root)}
    on_disk = {path.stem for path in _blobs(root)}
    assert on_disk == referenced
    assert (root / "blobs" / "tmp").is_dir(), "the staging directory is not collected"


@pytest.mark.spec
def test_sole_entry_survives_regardless_of_age(cache: Cache, tmp_path: Path) -> None:
    """§6: ``prune`` keeps at least one entry per key, always.

    Ten years old and still kept, because the alternative turns the next run into a cold fetch of
    something the user believes is cached — and on a 40 MB ``companyfacts`` payload that is the
    difference between a warm run and a rate-limited one. This is the condition README's description
    of ``--older-than`` leaves out, so it is the one most likely to be "simplified" away.
    """
    root = _root(tmp_path)
    key = Cache.key_for("GET", URL, None)
    _ = _put(cache, key, BODY)
    _age(root, [timedelta(days=3650)])

    report = Cache(root).prune(older_than=NINETY_DAYS, now=NOW)
    assert report.entries_removed == 0
    assert report.entries_kept == 1
    assert report.blobs_removed == 0

    hit = Cache(root).get(key)
    assert hit is not None
    assert hit[1] == BODY


@pytest.mark.spec
def test_the_newest_generation_is_the_one_kept(cache: Cache, tmp_path: Path) -> None:
    """§6: pruning the newest while keeping an older one silently reverts the cache to a stale view.

    Which is worse than a cold fetch, because the run succeeds and reports numbers from the payload
    that ``--refresh`` was used to replace. Asserted on the *body* rather than on the counts, since
    the counts are identical either way.
    """
    root = _root(tmp_path)
    key = Cache.key_for("GET", URL, None)
    _ = _put(cache, key, BODY)
    _ = _put(cache, key, THIRD_BODY)
    _age(root, [timedelta(days=200), timedelta(days=150)])

    _ = Cache(root).prune(older_than=NINETY_DAYS, now=NOW)
    hit = Cache(root).get(key)
    assert hit is not None
    assert hit[1] == THIRD_BODY, "both were prunable by age; the newest is the survivor"


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_an_entry_aged_exactly_older_than_is_kept(cache: Cache, tmp_path: Path) -> None:
    """§6: the comparison is ``>``, not ``>=``, so ``--older-than 90d`` means *older than* 90 days.

    Exactly 90 days is not older than 90 days. A ``>=`` here would make the flag's meaning differ
    from its name by one instant, which nobody would ever notice from the output — the entry would
    simply be gone.
    """
    root = _root(tmp_path)
    key = Cache.key_for("GET", URL, None)
    _ = _put(cache, key, BODY)
    _ = _put(cache, key, THIRD_BODY)
    _age(root, [NINETY_DAYS, timedelta(days=1)])

    report = Cache(root).prune(older_than=NINETY_DAYS, now=NOW)
    assert report.entries_removed == 0
    assert report.entries_kept == 2


@pytest.mark.spec
def test_an_entry_one_second_past_older_than_is_pruned(cache: Cache, tmp_path: Path) -> None:
    """The other side of the same boundary, one second across it.

    Paired with the test above deliberately: either assertion alone passes under a comparison that
    is wrong in the other direction, and the pair is what pins the operator.
    """
    root = _root(tmp_path)
    key = Cache.key_for("GET", URL, None)
    _ = _put(cache, key, BODY)
    _ = _put(cache, key, THIRD_BODY)
    _age(root, [NINETY_DAYS + timedelta(seconds=1), timedelta(days=1)])

    report = Cache(root).prune(older_than=NINETY_DAYS, now=NOW)
    assert report.entries_removed == 1
    assert report.entries_kept == 1


# ---------------------------------------------------------------------------
# The report, and the clock
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_prune_report_counts_what_it_did(cache: Cache, tmp_path: Path) -> None:
    """A prune that reports nothing is a prune the user runs twice.

    Every field is checked against the directory rather than against a literal: entries against the
    manifest before and after, blobs against the files, and ``bytes_reclaimed`` against the sizes
    the deleted files actually had. A hard-coded byte count would be a claim about gzip's output,
    which is not what the number means.
    """
    root = _root(tmp_path)
    key = Cache.key_for("GET", URL, None)
    for body in (BODY, OTHER_BODY, THIRD_BODY):
        _ = _put(cache, key, body)
    _age(root, [timedelta(days=200), timedelta(days=150), timedelta(days=1)])

    before = _manifest_lines(root)
    sizes = {path.stem: path.stat().st_size for path in _blobs(root)}

    report = Cache(root).prune(older_than=NINETY_DAYS, now=NOW)

    after = _manifest_lines(root)
    remaining = {path.stem for path in _blobs(root)}
    reclaimed = sum(size for digest, size in sizes.items() if digest not in remaining)

    assert report.entries_kept == len(after)
    assert report.entries_removed == len(before) - len(after)
    assert report.blobs_removed == len(sizes) - len(remaining)
    assert report.bytes_reclaimed == reclaimed
    assert report.bytes_reclaimed > 0, "three distinct bodies, two of them collected"


@pytest.mark.spec
def test_now_is_an_argument_and_never_the_wall_clock(cache: Cache, tmp_path: Path) -> None:
    """§2: ``prune`` takes ``now`` rather than calling ``datetime.now()``.

    The violation test for it is this: every entry is stamped years in the past by the real clock,
    ``older_than`` is zero — so a prune that read the clock would delete everything prunable — and
    ``now`` is passed as the moment those entries were written. Nothing may be removed.
    """
    root = _root(tmp_path)
    key = Cache.key_for("GET", URL, None)
    _ = _put(cache, key, BODY)
    _ = _put(cache, key, THIRD_BODY)
    _age(root, [timedelta(days=3650), timedelta(days=3650)])

    report = Cache(root).prune(older_than=timedelta(0), now=NOW - timedelta(days=3650))
    assert report.entries_removed == 0
    assert report.entries_kept == 2


@pytest.mark.spec
def test_prune_requires_both_arguments_by_keyword() -> None:
    """The signature is part of the guarantee: neither argument has a default to fall back on.

    A default ``now=datetime.now(UTC)`` would be evaluated at import time and reintroduce exactly
    the untestable clock read the parameter exists to remove — and positional arguments would let a
    caller swap the two, which type-checks under no annotation at all.
    """
    parameters = inspect.signature(Cache.prune).parameters
    for name in ("older_than", "now"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Shared blobs, and the empty case
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_a_shared_blob_survives_for_the_key_that_still_references_it(
    cache: Cache, tmp_path: Path
) -> None:
    """Two keys, one payload, one blob — and the sweep is by reference, not by key.

    Content addressing means two endpoints that return identical bytes share a blob. Collecting a
    blob because *this* key's entry went away would delete a payload another key still points at,
    and the failure surfaces on some later run as a dangling reference the manifest cannot explain.
    Two identical bodies is not a contrived case: an empty ``files: []`` submissions page and a 404
    body both repeat.
    """
    root = _root(tmp_path)
    subject = Cache.key_for("GET", URL, None)
    sibling = Cache.key_for("GET", f"{URL}?p=sibling", None)

    _ = _put(cache, subject, BODY)
    _ = _put(cache, subject, THIRD_BODY)
    shared = _put(cache, sibling, BODY)
    _age(root, [timedelta(days=200), timedelta(days=1), timedelta(days=1)])

    report = Cache(root).prune(older_than=NINETY_DAYS, now=NOW)
    assert report.entries_removed == 1
    assert report.blobs_removed == 0, "the removed entry's blob is still referenced by the sibling"

    hit = Cache(root).get(sibling)
    assert hit is not None
    assert hit[1] == BODY
    assert shared.content_sha256 in {path.stem for path in _blobs(root)}


def test_prune_on_an_empty_cache_is_a_no_op(cache: Cache, tmp_path: Path) -> None:
    """The first run of ``investo cache prune`` happens before the manifest exists.

    Zeros rather than a crash, because the command is documented in README and a user is entitled to
    run it on a directory investo has only just created.
    """
    report = cache.prune(older_than=NINETY_DAYS, now=NOW)
    assert (report.entries_removed, report.entries_kept) == (0, 0)
    assert (report.blobs_removed, report.bytes_reclaimed) == (0, 0)
    assert _blobs(_root(tmp_path)) == []

"""The raw-payload cache: content-addressed, append-only, on disk (DESIGN.md §4.4).

§4.4 is normative: the key is ``sha256(url + params)``, the value is raw bytes plus ``fetched_at``
plus response headers, entries are never mutated and never evicted by default, each carries a
schema version, and ``--refresh`` writes a *new* entry rather than overwriting.

§4.4 also states why this is load-bearing rather than an optimization, and the third reason is
the one that shapes the format: **upstream drift.** yfinance's back-adjustment and EDGAR's
``frames`` both mutate historical values, so the cache is the only immutable record of what the
model actually saw. A cache that can be silently rewritten is not that record.

The cache has never heard of sec.gov. That is what lets the price adapters and FINRA share it
without importing anything EDGAR-shaped — see :ref:`what it must not do <must-not>` below.

On-disk layout::

    .cache/
    |-- FORMAT                      # one line: the cache format version
    |-- manifest.jsonl              # append-only, one JSON object per line, newest last
    `-- blobs/
        `-- <aa>/<bb>/<sha256>.gz   # response body, decoded then gzipped by us

Two levels of hex fan-out on the content hash, because a warm cache for a few dozen tickers is
already thousands of blobs and a single flat directory degrades on some filesystems long before
it degrades on ext4.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode

from investo.errors import ConfigError

__all__ = [
    "CACHE_FORMAT_VERSION",
    "STORED_HEADERS",
    "CacheEntry",
    "PruneReport",
    "Cache",
]

CACHE_FORMAT_VERSION: Final = 1
"""The on-disk format version. Written to ``FORMAT`` and onto every manifest record.

Deliberately **not** part of the lookup key. §4.4 says entries *carry* a schema version so a
parser change can invalidate derived data without discarding raw payloads; folding the version
into the key would orphan every blob on a format bump, which is exactly the discarding §4.4
rules out.
"""

STORED_HEADERS: Final = frozenset(
    {"content-type", "content-encoding", "last-modified", "etag", "retry-after"}
)
"""Response headers worth keeping. **An allowlist, not a denylist.**

Everything else — ``set-cookie``, ``x-amz-*``, CDN request IDs, ``date`` — is useless,
non-deterministic, or something we should not be persisting. Storing the whole header block would
also put a per-request ID into a file the appendix hashes, which breaks determinism for no gain.
"""

_FORMAT_FILE: Final = "FORMAT"
_MANIFEST_FILE: Final = "manifest.jsonl"
_BLOBS_DIR: Final = "blobs"
_TMP_DIR: Final = "tmp"
_GZIP_LEVEL: Final = 6
"""Fixed, so a blob's bytes are a function of its content and nothing else."""


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One manifest record: what was fetched, when, and where its body is.

    ``fetched_at`` is timezone-aware UTC. It is displayed (the fetch summary, the appendix) and
    it participates in :meth:`Cache.prune`. **Nothing downstream may read it arithmetically** —
    a figure whose value depended on when it was fetched would make DESIGN.md §11's
    byte-identical-output gate unsatisfiable.
    """

    key: str
    url: str
    method: str
    params: Mapping[str, str]
    status: int
    content_sha256: str
    content_length: int
    headers: Mapping[str, str]
    fetched_at: datetime
    format_version: int


@dataclass(frozen=True, slots=True)
class PruneReport:
    """What a prune did. Printed by ``investo cache prune``, because a prune that reports
    nothing is a prune the user runs twice."""

    entries_removed: int
    entries_kept: int
    blobs_removed: int
    bytes_reclaimed: int


class Cache:
    """A content-addressed, append-only store of HTTP responses.

    Single-process only. That is what M1 is, and it is worth writing down rather than assuming:
    a future parallel fetcher needs a lock, and the absence of one here is not evidence that it
    is safe.

    .. _must-not:

    **What this class must not do**, each of which it is well placed to do and each of which gets
    a test:

    *It must not filter by ``as_of``.* ``--as-of`` is about *filing* dates and belongs to
    ``normalize`` (§4.2b). A cache that dropped entries fetched after ``as_of`` would make a warm
    run differ from a cold one for reasons invisible in the report, and would break the property
    that ``as_of`` reconstruction is a pure function of the cached payloads. There is no ``as_of``
    parameter anywhere in this interface, and that absence *is* the enforcement.

    *It must not parse.* :meth:`get` returns bytes. A cache that returned parsed JSON would put
    the parser inside the reproducibility boundary, so a parser change would invalidate the raw
    payloads — the opposite of what §4.4's schema version exists to allow.

    *It must not evict.* :meth:`prune` is explicit and user-invoked. No TTL, no size cap, no LRU.

    *It must not retry, rate-limit, or know about sec.gov.* Those belong to the client.
    """

    def __init__(self, root: Path) -> None:
        """Open or initialize a cache at ``root``.

        Raises:
            ConfigError: if ``FORMAT`` holds a version this build does not know. A cache written
                by a future format and read by today's parser is the one scenario where
                "reproducible from cache" silently stops being true, so it fails loudly and names
                the fix. Exit 5.
        """
        self._root = root
        self._blobs = root / _BLOBS_DIR
        self._manifest = root / _MANIFEST_FILE
        self._used: dict[str, str] = {}
        self._prepare()

    # -- lifecycle ---------------------------------------------------------
    def _prepare(self) -> None:
        self._blobs.mkdir(parents=True, exist_ok=True)
        (self._blobs / _TMP_DIR).mkdir(exist_ok=True)
        format_file = self._root / _FORMAT_FILE
        if not format_file.exists():
            format_file.write_text(f"{CACHE_FORMAT_VERSION}\n", encoding="utf-8")
            return
        raw = format_file.read_text(encoding="utf-8").strip()
        try:
            found = int(raw)
        except ValueError as exc:
            raise ConfigError(
                f"{format_file} does not contain a cache format version (found {raw!r}).",
                hint="Point --cache-dir somewhere else, or delete the directory to start over.",
            ) from exc
        if found > CACHE_FORMAT_VERSION:
            raise ConfigError(
                f"Cache at {self._root} is format {found}; this build understands "
                f"{CACHE_FORMAT_VERSION}.",
                hint=(
                    "A newer investo wrote this cache. Upgrade investo, point --cache-dir "
                    "somewhere else, or delete the directory."
                ),
            )
        # A known-older format would be a migration, and there are none yet. The first bump
        # writes one; until then, an older version is readable as-is.

    # -- keys --------------------------------------------------------------
    @staticmethod
    def key_for(method: str, url: str, params: Mapping[str, str] | None) -> str:
        """The lookup key: ``sha256(method \\n url \\n canonical_params)``.

        ``canonical_params`` is the params sorted by name and percent-encoded, so two callers
        that pass the same query in a different dict order hit the same entry.

        The format version is not in the key — see :data:`CACHE_FORMAT_VERSION`.
        """
        canonical = urlencode(sorted((params or {}).items()))
        return hashlib.sha256(f"{method.upper()}\n{url}\n{canonical}".encode()).hexdigest()

    def _blob_path(self, content_sha256: str) -> Path:
        return self._blobs / content_sha256[:2] / content_sha256[2:4] / f"{content_sha256}.gz"

    # -- read --------------------------------------------------------------
    def get(self, key: str) -> tuple[CacheEntry, bytes] | None:
        """The newest entry for ``key`` and its body, or ``None``.

        **Newest**, by ``fetched_at`` and then by line order, rather than oldest: ``--refresh``
        exists to supersede, and an append-only store that served the first write would make
        ``--refresh`` a no-op on the next run. Three entries for one key resolve to one answer
        every time, which is part of what M1 can gate on determinism.

        Records the entry for :meth:`manifest_hash`. A miss records nothing.
        """
        entries = [entry for entry in self._read_manifest() if entry.key == key]
        if not entries:
            return None
        entry = max(enumerate(entries), key=lambda pair: (pair[1].fetched_at, pair[0]))[1]
        blob = self._blob_path(entry.content_sha256)
        if not blob.exists():
            # A manifest line without its blob is the failure mode `put`'s ordering makes
            # impossible; reaching it means the directory was edited by hand or a prune was
            # interrupted between its own two phases.
            raise ConfigError(
                f"Cache entry {entry.key[:12]} references a missing blob "
                f"({entry.content_sha256[:12]}).",
                hint="Run `investo cache prune --older-than 0d` to rebuild, or delete the cache.",
            )
        body = gzip.decompress(blob.read_bytes())
        self._used[entry.key] = entry.content_sha256
        return entry, body

    # -- write -------------------------------------------------------------
    def put(
        self,
        *,
        key: str,
        url: str,
        method: str,
        params: Mapping[str, str],
        status: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> CacheEntry:
        """Append an entry. Never overwrites, never deletes.

        ``body`` is the **decoded** body. We send ``Accept-Encoding: gzip, deflate`` (§4.1) and
        httpx decodes transparently; the blob we store is that decoded body, gzipped by us with
        ``mtime=0`` and a fixed level.

        The reason is content-addressing. If we stored the wire bytes, the same JSON served with
        a different compression level — which a CDN is entitled to do — would hash differently
        and produce a second blob for identical content. Deduplication would then depend on the
        server's mood, and so would §4.4's "regenerates byte-identically from cache". The
        ``content-encoding`` we received is recorded in the headers, so nothing is lost.

        ``mtime=0`` because gzip writes a timestamp into its header by default, and a blob whose
        bytes change on every write is not content-addressed.

        **Blob before manifest, always.** A blob with no manifest line is invisible garbage that
        :meth:`prune` collects. A manifest line with no blob is a dangling reference that crashes
        a warm run. This ordering makes the first failure mode possible and the second
        impossible.
        """
        content_sha256 = hashlib.sha256(body).hexdigest()
        self._write_blob(content_sha256, body)

        entry = CacheEntry(
            key=key,
            url=url,
            method=method.upper(),
            params=dict(sorted(params.items())),
            status=status,
            content_sha256=content_sha256,
            content_length=len(body),
            headers={
                name.lower(): value
                for name, value in sorted(headers.items())
                if name.lower() in STORED_HEADERS
            },
            fetched_at=datetime.now(UTC),
            format_version=CACHE_FORMAT_VERSION,
        )
        self._append(entry)
        self._used[entry.key] = entry.content_sha256
        return entry

    def _write_blob(self, content_sha256: str, body: bytes) -> None:
        """Write the blob atomically, skipping the work if the content is already stored.

        Identical content is a no-op, which is how "append-only" and "don't store companyfacts
        forty times" coexist: a ``--refresh`` that gets the same bytes back writes a new manifest
        line and no new blob.
        """
        final = self._blob_path(content_sha256)
        if final.exists():
            return
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._blobs / _TMP_DIR / f"{uuid.uuid4().hex}.gz"
        with tmp.open("wb") as raw:
            # `filename=""` is load-bearing, not tidiness. Given a `fileobj`, GzipFile defaults
            # `filename` to `fileobj.name` and writes that basename into the gzip header with the
            # FNAME flag set — so the blob's bytes would embed the random temp filename and two
            # caches given identical bodies would produce different files. That breaks "a blob's
            # bytes are a function of its content" (02-cache.md §8) for the same reason `mtime=0`
            # does, and it is less visible.
            with gzip.GzipFile(
                filename="", fileobj=raw, mode="wb", compresslevel=_GZIP_LEVEL, mtime=0
            ) as gz:
                _ = gz.write(body)
            raw.flush()
            os.fsync(raw.fileno())
        # Atomic within a filesystem, so a reader never sees a partial blob.
        os.replace(tmp, final)

    def _append(self, entry: CacheEntry) -> None:
        """Append one complete line in a single write. Single-process only."""
        line = json.dumps(_to_record(entry), separators=(",", ":"), sort_keys=True) + "\n"
        with self._manifest.open("a", encoding="utf-8") as handle:
            _ = handle.write(line)
            handle.flush()

    # -- fingerprint -------------------------------------------------------
    def manifest_hash(self) -> str:
        """Fingerprint of the entries this run used, sorted. DESIGN.md §9.1 item 10.

        §9.1 lists a "cache manifest hash" in the appendix without saying *which* manifest, and
        one reading breaks determinism: hashing the whole file makes the hash printed in an AAPL
        report change when someone fetches MSFT, so §11's byte-identical-output gate would fail
        for a reason that has nothing to do with the report. Spec question 4 resolves it to the
        entries this run touched — which is the question an appendix reader is actually asking:
        *did this report see the same data as that one?*

        **One divergence from ``docs/m1/02-cache.md``, raised rather than resolved silently.**
        That document says :meth:`get` records a read and a miss records nothing, which would make
        this hash empty on a cold run — while the fetch summary in ``docs/m1/README.md`` §3 prints
        a non-empty ``manifest 9f2c1ab4`` next to sources whose status is ``fetched``, not
        ``cached``. Those two cannot both hold. Implemented as *entries used* — a cache hit or a
        fresh ``put`` — because that is what makes a cold run and the warm run after it produce
        the same fingerprint, which is the property the appendix value is for. Recorded as spec
        question 10 in ``docs/m1/README.md``.
        """
        joined = "\n".join(f"{key} {digest}" for key, digest in sorted(self._used.items()))
        return hashlib.sha256(joined.encode()).hexdigest()

    @property
    def used(self) -> Mapping[str, str]:
        """``key -> content_sha256`` for every entry this run read or wrote."""
        return dict(self._used)

    # -- prune -------------------------------------------------------------
    def prune(self, *, older_than: timedelta, now: datetime) -> PruneReport:
        """Drop superseded entries older than ``older_than``, then collect orphaned blobs.

        ``now`` is an argument rather than a call to ``datetime.now()``, because a function that
        reads the clock cannot be tested at a boundary and "older than 90d" has a boundary that
        matters.

        Two phases, and the ordering is the whole correctness argument:

        1. **Select.** An entry is prunable if ``now - fetched_at > older_than`` **and** it is
           not the newest entry for its key. The second condition is not in README's description
           and is not optional: pruning the only entry for a key turns the next run into a cold
           fetch of something the user believes is cached, and pruning the newest while keeping
           an older one silently reverts the cache to a stale view. At least one entry per key
           survives, always, regardless of age.
        2. **Rewrite, then collect.** Write the surviving manifest to a temp file and
           ``os.replace`` it; *then* delete blobs no surviving entry references.

        Rewrite before delete, because the reverse leaves a window in which the manifest
        references deleted blobs. With this ordering the failure window contains only
        unreferenced blobs, which the next prune collects.

        The boundary is ``>``, not ``>=``: an entry aged exactly ``older_than`` is **kept**, so
        ``--older-than 90d`` means "older than 90 days".
        """
        entries = self._read_manifest()
        newest_line: dict[str, int] = {}
        for index, entry in enumerate(entries):
            current = newest_line.get(entry.key)
            if current is None or (entry.fetched_at, index) >= (
                entries[current].fetched_at,
                current,
            ):
                newest_line[entry.key] = index

        survivors: list[CacheEntry] = []
        removed = 0
        for index, entry in enumerate(entries):
            is_newest = newest_line[entry.key] == index
            too_old = (now - entry.fetched_at) > older_than
            if too_old and not is_newest:
                removed += 1
            else:
                survivors.append(entry)

        self._rewrite_manifest(survivors)

        referenced = {entry.content_sha256 for entry in survivors}
        blobs_removed = 0
        bytes_reclaimed = 0
        for blob in self._iter_blobs():
            if blob.stem in referenced:
                continue
            bytes_reclaimed += blob.stat().st_size
            blob.unlink()
            blobs_removed += 1

        return PruneReport(
            entries_removed=removed,
            entries_kept=len(survivors),
            blobs_removed=blobs_removed,
            bytes_reclaimed=bytes_reclaimed,
        )

    def _iter_blobs(self) -> Iterable[Path]:
        """Every stored blob, excluding the temp staging directory."""
        tmp = self._blobs / _TMP_DIR
        for path in self._blobs.rglob("*.gz"):
            if tmp not in path.parents:
                yield path

    def _rewrite_manifest(self, entries: list[CacheEntry]) -> None:
        tmp = self._manifest.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for entry in entries:
                _ = handle.write(
                    json.dumps(_to_record(entry), separators=(",", ":"), sort_keys=True) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._manifest)

    # -- manifest ----------------------------------------------------------
    def _read_manifest(self) -> list[CacheEntry]:
        """Every record, in file order.

        Re-read on each call rather than cached in memory. M1's workload is a few dozen entries
        and an in-memory index would be a second source of truth that ``put`` has to keep in
        sync — the class of bug that makes a cache serve a body it no longer has.
        """
        if not self._manifest.exists():
            return []
        entries: list[CacheEntry] = []
        with self._manifest.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    entries.append(_from_record(json.loads(text)))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    # Exit 5 rather than skipping the line. A malformed record means the
                    # directory is unusable as configured, which is the same class of problem as
                    # an unknown FORMAT version — and skipping it would silently drop a payload
                    # the user believes is cached.
                    raise ConfigError(
                        f"{self._manifest} line {number} is not a valid cache record: {exc}",
                        hint=(
                            "Point --cache-dir somewhere else, or delete the directory. The "
                            "blobs are content-addressed, so nothing is recoverable from a "
                            "manifest that cannot be read."
                        ),
                    ) from exc
        return entries


def _to_record(entry: CacheEntry) -> dict[str, Any]:
    return {
        "key": entry.key,
        "url": entry.url,
        "method": entry.method,
        "params": dict(entry.params),
        "status": entry.status,
        "content_sha256": entry.content_sha256,
        "content_length": entry.content_length,
        "headers": dict(entry.headers),
        "fetched_at": entry.fetched_at.isoformat(),
        "format_version": entry.format_version,
    }


def _from_record(record: Mapping[str, Any]) -> CacheEntry:
    fetched_at = datetime.fromisoformat(str(record["fetched_at"]))
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must carry a timezone offset")
    return CacheEntry(
        key=str(record["key"]),
        url=str(record["url"]),
        method=str(record["method"]),
        params=dict(record.get("params") or {}),
        status=int(record["status"]),
        content_sha256=str(record["content_sha256"]),
        content_length=int(record["content_length"]),
        headers=dict(record.get("headers") or {}),
        fetched_at=fetched_at,
        format_version=int(record["format_version"]),
    )

# M1 — Cache

`ingest/cache.py`. Content-addressed, append-only, on disk.

DESIGN §4.4 is normative: key is `sha256(url + params)`, value is raw bytes plus `fetched_at`
plus response headers, never mutated, never evicted by default, entries carry a schema version,
`--refresh` writes a *new* entry rather than overwriting.

§4.4 also states why this is load-bearing rather than an optimization, and the third reason is
the one that shapes the format: **upstream drift.** yfinance's adjustments and EDGAR's `frames`
both mutate historical values, so the cache is the only immutable record of what the model
actually saw. A cache that can be silently rewritten is not that record.

---

## 1. On-disk layout

```
.cache/
├── FORMAT                      # one line: the cache format version, e.g. "1"
├── manifest.jsonl              # append-only, one JSON object per line, newest last
└── blobs/
    └── <aa>/<bb>/<sha256>.gz   # response body, gzip-compressed by us
```

Two levels of hex fan-out on the content hash, because a warm cache for a few dozen tickers is
already thousands of blobs and a single flat directory degrades on some filesystems long before
it degrades on ext4.

### Manifest record

```jsonc
{
  "key": "3f1a…",              // sha256 of the canonical request — the lookup key
  "url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
  "params": {},                 // sorted; empty object when none
  "method": "GET",
  "status": 200,
  "content_sha256": "9c4e…",   // sha256 of the *decoded* body — the blob name
  "content_length": 40613882,   // decoded length, bytes
  "headers": {                  // allowlisted, see below
    "content-type": "application/json",
    "content-encoding": "gzip",
    "last-modified": "Fri, 31 Jul 2026 10:58:11 GMT",
    "etag": "\"a1b2c3\""
  },
  "fetched_at": "2026-07-31T11:02:21.184Z",
  "format_version": 1
}
```

**Headers are allowlisted, not stored wholesale.** `content-type`, `content-encoding`,
`last-modified`, `etag`, `retry-after`. Everything else — `set-cookie`, `x-amz-*`, CDN request
IDs, `date` — is either useless, non-deterministic, or something we should not be persisting.
Storing the whole header block would also put a per-request ID into a file the appendix hashes,
which breaks determinism for no gain.

### Two hashes, and they do different jobs

- **`key`** = `sha256(f"{method}\n{url}\n{canonical_params}")`, where `canonical_params` is the
  params sorted by name and percent-encoded. This is the lookup key. **The format version is not
  in it.** §4.4 says entries *carry* a schema version so parser changes can invalidate derived
  data without discarding raw payloads; folding the version into the key would orphan every blob
  on a format bump, which is exactly the discarding §4.4 rules out.
- **`content_sha256`** = `sha256(decoded_body)`. This is the blob name. So a `--refresh` that
  gets identical bytes back writes a new manifest line and **no new blob** — which is how
  "append-only" and "don't store companyfacts forty times" coexist.

### Bodies are stored decoded, then re-compressed by us

We send `Accept-Encoding: gzip, deflate` (§4.1) and httpx decodes transparently. The stored blob
is the *decoded* body, gzipped by us with `mtime=0` and a fixed compression level.

The reason is content-addressing. If we stored the wire bytes, the same JSON served with a
different compression level — which a CDN is entitled to do — would hash differently and produce
a second blob for identical content. Deduplication would then depend on the server's mood, and
"regenerates byte-identically from cache" (§4.4) would depend on it too. The `content-encoding`
we received is recorded in the headers, so nothing about the response is lost.

`mtime=0` because gzip writes a timestamp into its header by default, and a blob whose bytes
change every write is not content-addressed.

### Atomicity

Blob: write to `blobs/tmp/<uuid>`, `fsync`, then `os.replace` into place. `os.replace` is atomic
within a filesystem, so a reader never sees a partial blob.

Manifest: open in append mode, write one complete line including its newline in a single `write`
call, `flush`. Single-process only — which M1 is, and which is worth writing down rather than
assuming, because a future parallel fetcher needs a lock and the absence of one here is not
evidence that it is safe.

**Blob before manifest, always.** A blob with no manifest line is invisible garbage that `prune`
collects. A manifest line with no blob is a dangling reference that crashes a warm run. The
ordering makes the first failure mode possible and the second impossible.

---

## 2. Public interface

```python
CACHE_FORMAT_VERSION: Final = 1

@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    url: str
    status: int
    content_sha256: str
    content_length: int
    headers: Mapping[str, str]
    fetched_at: datetime          # tz-aware UTC
    format_version: int

class Cache:
    def __init__(self, root: Path) -> None: ...

    def get(self, key: str) -> tuple[CacheEntry, bytes] | None:
        """The newest entry for `key`, or None. Records the read for `manifest_hash`."""

    def put(self, *, key: str, url: str, method: str, params: Mapping[str, str],
            status: int, headers: Mapping[str, str], body: bytes) -> CacheEntry: ...

    def manifest_hash(self) -> str:
        """Fingerprint of the entries this run read. See §4."""

    def prune(self, *, older_than: timedelta, now: datetime) -> PruneReport: ...

    @staticmethod
    def key_for(method: str, url: str, params: Mapping[str, str] | None) -> str: ...
```

`prune` takes `now` as an argument rather than calling `datetime.now()`. A function that reads
the clock cannot be tested at a boundary, and "older than 90d" has a boundary that matters — see
[`06-testing.md`](06-testing.md).

`get` returns the **newest** entry for a key, by `fetched_at`, breaking ties by file order (later
line wins). Newest rather than oldest because `--refresh` exists to supersede, and an
append-only store that served the first write would make `--refresh` a no-op on the next run.

### Opening a cache with an unknown format version

`Cache.__init__` reads `FORMAT`. If it is absent, the directory is initialized. If it holds a
version this build does not know — a newer one — the constructor raises `ConfigError` (exit 5)
rather than guessing. A cache written by a future format read by today's parser is the one
scenario where "reproducible from cache" silently stops being true, so it fails loudly and names
the fix (`--cache-dir` elsewhere, or delete).

Downgrading a known-older format is a migration, and there are none yet. The first bump writes
one.

---

## 3. `--refresh`

`--refresh` bypasses the read and forces a fetch; the result is `put` as a new entry. It does not
delete, and it does not overwrite. So `--refresh` on a payload that has not changed upstream costs
one request and one manifest line, and the previous view remains reconstructible.

This is what makes yfinance's back-adjustment (§4.3) survivable: two pulls on different dates
legitimately disagree, and after `--refresh` both are still on disk with their `fetched_at`, so
the disagreement is inspectable rather than a mystery.

---

## 4. `manifest_hash` — the appendix's cache fingerprint

DESIGN §9.1 item 10 lists "cache manifest hash" in the appendix without saying which manifest.
[Spec question 4](README.md#7-spec-questions) proposes this reading:

```
manifest_hash = sha256("\n".join(f"{key} {content_sha256}" for key, content_sha256 in sorted(entries_read)))
```

Over the entries **this run read**, sorted, not the whole file.

The alternative — hashing the file — makes the hash printed in an AAPL report change when someone
fetches MSFT. That is not a property of the report, and §11 makes byte-identical output a CI
gate, so the gate would fail on an unrelated cache write. Hashing the entries read makes the
value a fingerprint of the run's inputs, which is the question an appendix reader is actually
asking: *did this report see the same data as that one?*

`Cache` therefore tracks reads for the lifetime of the instance. `get` records `(key,
content_sha256)`; a miss records nothing.

---

## 5. What the cache must not do

Each of these is a thing the cache is well placed to do and must not, and each gets a test.

**It must not filter by `as_of`.** `--as-of` is about *filing* dates and belongs to `normalize`
(§4.2b). A cache that dropped entries fetched after `as_of` would make a warm run differ from a
cold one for reasons invisible in the report, and would break the property that `as_of`
reconstruction is a pure function of the cached payloads. The cache has no `as_of` parameter
anywhere in its interface, which is the enforcement.

**It must not parse.** `get` returns bytes. A cache that returned parsed JSON would put the
parser inside the reproducibility boundary, so a parser change would invalidate the raw payloads
— the opposite of what §4.4's schema version exists to allow.

**It must not evict.** `prune` is explicit and user-invoked (`investo cache prune`, README).
No TTL, no size cap, no LRU. §4.4: never evicted by default.

**It must not retry, rate-limit, or know about sec.gov.** Those are the client's. The cache takes
a key and bytes and has never heard of EDGAR — which is also what lets the price adapters share
it without importing anything EDGAR-shaped.

---

## 6. `cache prune --older-than`

Two-phase, and the ordering is the whole correctness argument.

1. **Select.** Read the manifest. An entry is prunable if `now - fetched_at > older_than` **and**
   it is not the newest entry for its key. The second condition is not in README's description and
   is not optional: pruning the only entry for a key turns the next run into a cold fetch of
   something the user believes is cached, and pruning the newest while keeping an older one
   silently reverts the cache to a stale view. `prune` keeps at least one entry per key, always.
2. **Rewrite, then collect.** Write the surviving manifest to `manifest.jsonl.tmp` and
   `os.replace` it. *Then* delete blobs no surviving entry references.

Rewrite before delete, because the reverse leaves a window in which the manifest references
deleted blobs. With this ordering the failure window contains only unreferenced blobs, which the
next `prune` collects.

```python
@dataclass(frozen=True, slots=True)
class PruneReport:
    entries_removed: int
    blobs_removed: int
    bytes_reclaimed: int
    entries_kept: int
```

Printed by the command, because a prune that reports nothing is a prune the user runs twice.

**The guarantee, and its violation test:** *after any prune, every surviving manifest entry has
its blob.* The test prunes a cache holding several generations of the same key plus unrelated
keys, then asserts `get` succeeds for every key that had an entry, and asserts no `blobs/` file
is unreferenced. Boundary: an entry at exactly `older_than` is **not** pruned — `>` not `>=`, so
`--older-than 90d` means "older than 90 days," and both 90d and 90d+1s are asserted.

---

## 7. Size, and the reason a floor is not set

§4.4 notes `companyfacts` alone is 10–40 MB for a large filer. With our own gzip that is roughly
3–8 MB per blob on disk, and a warm cache for twenty NASDAQ names — M2's exit criterion — lands
in the low hundreds of MB. That is fine, and it is also why `prune` exists.

No size cap and no warning threshold in M1. A threshold is a claim about what is too big, and
that can only come from a measurement nobody has taken yet. M2 fetches twenty names and can set
one from a real number.

---

## 8. Interaction with the determinism gate

§11 makes two runs producing a byte-identical PDF a CI gate, and §9.0 lists what must be pinned
for it. The cache's contribution:

- Blobs are gzipped with `mtime=0`, so a blob's bytes are a function of its content alone.
- `manifest_hash` covers entries read, so it is a function of the run rather than of the
  directory.
- `get` returns the newest entry deterministically — `fetched_at` then line order — so a cache
  holding three generations of a payload resolves the same way every time.
- `fetched_at` is recorded but is **not** an input to any computed number. It is printed (the
  fetch summary, the appendix's provenance) and it participates in `prune`. A figure whose value
  depended on when it was fetched would make the gate unsatisfiable, so nothing downstream may
  read it arithmetically. `SourceRef.fetched_at` exists to be displayed.

The last point is worth a test in M3 rather than M1, since M1 renders nothing — noted here so it
is not lost.

# M1 — Testing

CLAUDE.md § Testing expectations governs. Three rules from it shape everything below:

- **Assert the derivation, not the value.** A test that hard-codes an expected number passes under
  a wrong rule that happens to agree at that input.
- **For any sentence of the form "X cannot happen", write the test that attempts X and asserts it
  fails.** A happy-path test passes whether or not the guarantee is enforced.
- **Boundaries get their own test.**

DESIGN §11 is normative on approach per layer. It prescribes `responses` for the EDGAR client,
which cannot mock httpx — see [spec question 3](README.md#7-spec-questions).

---

## 1. Layout

```
tests/
├── conftest.py                     # M0: clears INVESTO_*; extended with the fixtures below
├── fixtures/
│   ├── edgar/
│   │   ├── company_tickers_exchange.trimmed.json
│   │   ├── companyfacts/{AAPL,BANK,REIT,IPO,RESTATER,NOQ4}.trimmed.json
│   │   ├── submissions/{AAPL.json, AAPL-submissions-001.json, ARXS.json}
│   │   ├── malformed/{short_column.json, bad_accession.json, undeclared_403.txt}
│   │   └── PROVENANCE.md            # the trap each fixture carries, and which are synthetic
│   ├── prices/{tiingo,yfinance,stooq}/AAPL.json|csv
│   └── typing/                     # snippets that must FAIL basedpyright — see §5
├── reduce_fixture.py               # the script that produced every .trimmed.json
├── make_fixtures.py                # the *synthetic* fixtures, until curation happens
├── test_layering.py                # AST rules: choke point, no Metric in ingest, flow direction
├── test_fields.py                  # the _fields.py boundary table (04-parsers.md §10.1)
├── test_periods.py                 # duration buckets, at their boundaries
├── test_provenance.py              # Accession transforms, Derivation
├── test_cache.py                   # append-only, refresh, manifest hash
├── test_cache_warm.py              # zero-HTTP warm run; respx with no routes registered
├── test_cache_prune.py             # the prune guarantee, at its boundary
├── test_client_ratelimit.py        # token bucket with an injected clock
├── test_client_retry.py            # the retry matrix, incl. the no-retry cases
├── test_client_urls.py             # every transform against a literal
├── test_tickers.py                 # incl. exit 2 for a non-NASDAQ listing
├── test_companyfacts.py            # incl. the Decimal violation test
├── test_submissions.py             # incl. pagination and the column-length assertion
├── test_prices_contract.py         # one test, three adapters
├── test_market_cap.py
├── test_fetch_command.py           # end-to-end from fixtures; the four exit criteria
├── test_frames.py                  # M1b: FrameRow is not a RawFact
├── test_documents.py               # M1b: normalize_text idempotence, item splitting
├── test_events.py                  # M1b: extraction only, no severity table
├── test_ownership.py               # M1b: P/S filtering, the 2024-12-18 13D/G boundary
├── test_proxy.py                   # M1b: ecd iXBRL only, no numbers from narrative
├── test_finra.py                   # M1b: snapshotting, which is a cache test in disguise
└── test_typing.py                  # §5
```

---

## 2. Fixtures

### Trimming, and why the script is checked in

Apple's `companyfacts` is roughly 40 MB. Committing it is unreasonable; hand-editing it produces a
fixture nobody can regenerate or justify. So every trimmed fixture is the output of
`tests/reduce_fixture.py`, which is checked in and takes the tags M1 and M2 name plus a period
window, preserving structure exactly.

The point is not disk space. It is that a reviewer can ask "why does this fixture contain these
facts" and get an answer, and that regenerating fixtures after a DESIGN change is a command rather
than an afternoon. §11 calls for "real `companyfacts` JSON for ~15 companies with known-hard
cases" — real, reduced, reproducibly.

One full-size payload is kept **out of git**, fetched on demand into a gitignored path, and used
by a single `network`-marked test that asserts parse time and peak memory stay in the range
[`04-parsers.md`](04-parsers.md#memory) claims. A claim about a 40 MB payload tested only against a
40 KB one is not tested.

### The hard cases, and what each is for

§11 and ROADMAP M2 name six. Their fixtures land in M1 even though M2 consumes them, because M1's
parser is what has to survive them and because collecting them is the slow part.

| Fixture | The trap it carries |
|---|---|
| **AAPL** | ASC 606 stitch; FY2018 revenue tagged `fy: 2019` *and* `fy: 2020` (§4.2a); one quarter appearing across four accessions (§4.2b); a CIK under 1,000,000 that pads on `data.sec.gov` and not in `/Archives/`; both filer-agent and self-filed accessions |
| **BANK** (SIC 6000–6499) | no operating-income line; the §6.10 refusal path |
| **REIT** | same, plus a capex chain miss |
| **IPO** | fewer than 12 quarters — §5.1's valuation-omitted boundary |
| **RESTATER** | the same period at four `filed` dates, which is what `--as-of` has to cut |
| **NOQ4** | discrete Q4 never tagged, and — per §4.2c — inconsistently *within* the same issuer across years |

Three submissions fixtures, and the reason there are three:

| Fixture | Role |
|---|---|
| `AAPL.json` | main payload with a populated `files[]` — the pagination case, confirmed real: Apple's overflow page 001 holds 2015 filings |
| `AAPL-submissions-001.json` | an overflow page — **flat** columnar shape, no `filings` wrapper, exercising the second parse function |
| `ARXS.json` | a complete small-filer payload. Every awkward value observed live lives here: `"cik":"0002093536"` as a padded string, `"sic":"3728"`, `reportDate` and `act` as `""`, `isXBRLNumeric` carrying real `null`s, an `items` value of `",,"`, `primaryDocument` of `"xslF345X06/ownership.xml"`, and `"files":[]` |

`ARXS.json` earns its place by being small enough to commit whole and untrimmed. Every other
fixture is reduced, which means a reviewer has to trust `reduce_fixture.py`; this one is the real
document, so it is the fixture that catches a normalization bug the reduction script might have
smoothed over.

Its `companyfacts` counterpart, `companyfacts/ARXS.json`, is the same idea and does more work
than expected. It is small enough to commit whole and it carries, live: a padded-string `cik`, an
`entityName` whose casing differs from `submissions`, the unanticipated `ffd` taxonomy, **no
`dei` section at all**, instant facts with the `start` key *absent*, `fy`/`fp` as `null` on
registration-statement facts, and a `pure` unit with decimal values.

It also carries the §4.2(a) trap: a fact spanning `2025-01-01`–`2025-03-31` tagged `fy: 2026`,
`fp: "Q1"`. So the "never group by `fy`/`fp`" test — which the hard-case table below assigns to a
reduced Apple payload — has a working fixture on day one, before fixture curation finishes. That
is the one test in M2's critical path that no longer waits on research.

### Recorded HTTP

`respx` (dev, `>=0.22,<0.23`). Every non-`network` test registers routes explicitly, and a request
to an unregistered route raises. That default is what makes "warm run makes zero HTTP calls"
testable by *omission*: register nothing, and any request fails the test.

---

## 3. Markers

`--strict-markers` is on, so each addition goes in `[tool.pytest.ini_options] markers`.

| Marker | Meaning | Status |
|---|---|---|
| `spec` | asserts a rule stated normatively in DESIGN.md or ROADMAP.md | exists (M0) |
| `surface` | asserts the CLI's documented flag surface | exists (M0) |
| `network` | reaches the real internet; deselected by default | **new** |
| `typing` | runs basedpyright over a snippet and asserts the diagnostic | **new** |

`addopts` gains `-m "not network"`. Two consequences worth stating: `make test` and CI both skip
network tests without anyone remembering to, and — since CI sets no `INVESTO_*` variables
(CLAUDE.md convention 7) — a network test that leaked into the default selection would fail rather
than quietly pass, which is the behaviour convention 7 exists to produce.

Every test named in [§4](#4-the-guaranteeviolation-test-table) carries `spec`.

---

## 4. The guarantee→violation-test table

Every "X cannot happen" in this design, and the test that attempts X. This table is the
deliverable of the testing spec; a guarantee absent from it is a guarantee that is not enforced.

| Guarantee | Source | Violation test |
|---|---|---|
| SEC User-Agent has no default | §4.1, CLAUDE 2 | `test_fetch_without_user_agent_exits_5` — asserts exit 5 **and** `client.request_count == 0` |
| Nothing outside `client.py` calls sec.gov | CLAUDE 6 | `test_layering::test_no_secgov_literal_outside_client` — AST over every module |
| `ingest/` never assigns a `Metric` | this design | `test_layering::test_metric_unreferenced_in_ingest` |
| `ingest/` names no `us-gaap` tag | this design | `test_layering::test_no_usgaap_literal_in_ingest`; the `dei` allowlist is asserted to hold exactly one tag |
| `domain/` does not import `ingest/` | §3 | `test_layering::test_domain_imports_nothing_upward` |
| An undeclared-tool 403 is never retried | [`03`](03-edgar-client.md) | `test_client_retry::test_undeclared_403_makes_one_request` — asserts the count, not just the exception |
| A non-NASDAQ ticker exits 2 | README, §14 | `test_tickers::test_nyse_listing_exits_2` |
| No money is ever a `float` | CLAUDE 8 | `test_companyfacts::test_decimal_from_source_text` — round-trips `…000.01` exactly **and** asserts `not isinstance(v, float)` |
| Cover-page shares cannot be a per-share denominator | §5.4 | `test_typing::test_cover_shares_rejected_as_diluted` — see [§5](#5-type-level-guarantees) |
| Warm run makes zero HTTP calls | ROADMAP M1 | `test_cache_warm::test_second_fetch_makes_no_requests` — respx with no routes |
| Prune never leaves a dangling blob | [`02`](02-cache.md) | `test_cache_prune::test_every_surviving_entry_resolves`, plus an unreferenced-blob sweep |
| Prune never removes the last entry for a key | [`02`](02-cache.md) | `test_cache_prune::test_sole_entry_survives_regardless_of_age` |
| `--refresh` supersedes without destroying | §4.4 | `test_cache::test_refresh_keeps_prior_entry_retrievable` |
| The cache cannot filter by `as_of` | [`02`](02-cache.md) | `test_cache::test_cache_api_has_no_as_of` — inspects the signature; the enforcement is the absence |
| Frames values cannot enter a company series | §4.2 | `FrameRow` is a distinct type; `test_typing::test_framerow_not_a_rawfact` |
| Stooq reports no adjusted close rather than aliasing | [`05`](05-prices.md) | `test_prices_contract::test_stooq_adj_close_is_none_not_close` |
| A short submissions column is not silently truncated | [`04`](04-parsers.md) | `test_submissions::test_short_column_raises` against `malformed/short_column.json` |
| A malformed accession is rejected, not passed through | [`03`](03-edgar-client.md) | `test_provenance::test_bad_accession_raises` |
| No company CIK is derived from an accession | [`01`](01-domain-types.md) | `Accession` exposes no `cik`; `test_typing::test_accession_has_no_cik_attribute` |
| No parser reads the clock | [`04`](04-parsers.md) | `test_layering::test_no_now_call_in_parsers` — AST for `datetime.now` / `date.today` |
| Market cap never sums share counts from different cover pages | [`01`](01-domain-types.md) | `test_market_cap::test_mixed_end_dates_raise` |
| Market cap never uses a price later than `as_of` | [`05`](05-prices.md) | `test_market_cap::test_price_is_last_bar_at_or_before_as_of` — a series with bars after `as_of` must not change the result |
| The cache returns bytes and never parses | [`02`](02-cache.md) | `test_cache::test_get_returns_exact_bytes` — a body that is not valid JSON round-trips unchanged |
| The cache never evicts without `prune` | [`02`](02-cache.md) | `test_cache::test_entries_survive_many_writes` — no TTL, no size cap |
| A manifest line never precedes its blob | [`02`](02-cache.md) | `test_cache::test_interrupted_put_leaves_no_dangling_entry` — `put` failing after the blob write leaves the cache readable |
| A cache written by a newer format is not guessed at | [`02`](02-cache.md) | `test_cache::test_future_format_version_exits_5` |
| `tickers.py` never indexes columns positionally | [`04`](04-parsers.md) | `test_tickers::test_reordered_fields_parse_identically`, and a missing-field fixture raises |
| A parser never discards what it could not interpret | [`04`](04-parsers.md) | `test_submissions::test_unrecognised_items_kept_in_items_raw` |
| Every error subclass declares its own `exit_code`, at any depth | CLAUDE 1 | `test_errors::test_every_error_subclass_declares_a_code`, **made recursive in M1** — see [`03`](03-edgar-client.md#8-errors) |
| An empty-string scalar never becomes a date, an int, or a crash | [`04`](04-parsers.md) | `test_submissions::test_empty_string_scalars_become_none` — `reportDate`, `act`, `sic` all `""` in one fixture row |
| A degenerate `items` value never yields empty item codes | [`04`](04-parsers.md) | `test_submissions::test_items_double_comma_yields_no_codes` — the observed `",,"` value, asserting `items == ()` and `items_raw == ",,"` |
| An overflow page is never parsed as a main payload | [`04`](04-parsers.md) | `test_submissions::test_page_parser_rejects_wrapped_payload` and its converse — the two shapes are not interchangeable |
| Forms 3/4/5 never fetch the XSL viewer path | [`03`](03-edgar-client.md) | `test_client_urls::test_ownership_doc_strips_xsl_prefix` — against the observed `xslF345X06/ownership.xml` |
| A padded-string `cik` never reaches a URL builder unconverted | [`04`](04-parsers.md) | `test_fields::test_as_cik` — the full boundary table, one place, both endpoint spellings |
| Empty `share_facts` yields no market cap, not zero | [`01`](01-domain-types.md#empty-input-returns-none-malformed-input-raises) | `test_market_cap::test_absent_dei_returns_none` — asserts `market_cap(...) is None`, and separately that no `0` reaches a multiple |
| Malformed `share_facts` raises rather than returning a plausible number | [`01`](01-domain-types.md#empty-input-returns-none-malformed-input-raises) | `test_market_cap::test_mixed_end_dates_raise`, `::test_wrong_tag_raises` — the absence/failure split, tested on both sides |
| The taxonomy set is never hardcoded | [`04`](04-parsers.md) | `test_companyfacts::test_unknown_taxonomy_preserved` — `ffd` survives parsing and appears in `taxonomies_present` |
| The report's company name never comes from `companyfacts` | [`04`](04-parsers.md) | `test_companyfacts::test_entity_name_not_used_as_display_name` |
| Company identity is checked on `cik`, never on name | [`04`](04-parsers.md) | `test_companyfacts::test_identity_check_tolerates_name_casing` — `"ARXIS, INC."` vs `"Arxis, Inc."` on one CIK must **not** raise |

---

## 5. Type-level guarantees

Two guarantees in this design are enforced by the type checker and cannot be tested at runtime,
because at runtime both sides are the same object. §5.4's share-count distinction is the important
one: `CoverShares` and `DilutedShares` are both `Decimal`, so a runtime assertion is impossible by
construction — which is the whole reason `NewType` was chosen.

CLAUDE.md still applies: the violation has to be attempted and has to fail. So:

```
tests/fixtures/typing/cover_shares_as_diluted.py     # must produce a basedpyright error
tests/fixtures/typing/framerow_as_rawfact.py
tests/fixtures/typing/accession_cik_attribute.py
```

Each is a small module that performs the forbidden thing. `test_typing.py` runs
`basedpyright --outputjson` over the directory and asserts that **each file produces at least one
error**, and that the errors are on the expected lines.

Three notes, because this is the least conventional part of the suite:

- The fixture directory is **excluded** from the main `[tool.basedpyright] include`, or `make
  typecheck` fails on files that are supposed to fail.
- Marked `typing`, selected by default. It is fast — one basedpyright invocation over three tiny
  files — and it is the only evidence that §5.4's "enforced by distinct types" is true.
- If it turns out to be slow enough to be annoying, the fix is to fold it into `make typecheck`
  rather than to delete it. Deleting it downgrades a guarantee to a comment.

**This is the most fragile test in the suite, and the fragility is accepted deliberately.** It
asserts on the output of a tool that is free to rephrase its diagnostics. Three things keep the
blast radius small:

1. **Assert on error count per file and on line number. Never on message text.** Wording is what
   changes across releases; the presence of an error on line 7 is what the guarantee is about.
2. **basedpyright is pinned `>=1.39,<2`, and CI runs `uv sync --frozen`**, so the version moves
   only on a deliberate lock update — which is a commit someone is looking at.
3. **The fixture files are three lines each**, so a line-attribution change is obvious rather
   than mysterious.

Expect to revisit this at some basedpyright upgrade. That is the price of the guarantee, and it
is cheaper than the alternative, which is §5.4 being enforced by a docstring.

---

## 6. Determinism, at M1's scope

§11 makes byte-identical PDF output a CI gate, and M1 renders nothing. What M1 can gate:

- **A blob's bytes are a function of its content.** Gzip with `mtime=0`; write the same body twice
  and assert identical files on disk.
- **`manifest_hash` is a function of the entries read.** Two runs over the same cache produce the
  same hash; fetching an unrelated ticker in between does not change it. That second assertion is
  the one that would fail under the whole-file reading in
  [spec question 4](README.md#7-spec-questions), so the test is also the argument for it.
- **`Cache.get` resolves multiple generations deterministically.** Three entries for one key, one
  answer, every time — and it is the newest.

The PDF gate arrives in M3, and §9.0's `SOURCE_DATE_EPOCH` / `svg.hashsalt` / `metadata={"Date":
None}` list is that milestone's. Noted here only so the M1 tests are not mistaken for the whole
gate.

---

## 7. The coverage floor

`pyproject.toml` currently sets no `fail_under`, with a comment committing to setting it in M1 from
a measured figure, and explaining why a number picked to match today's run is a gate that means
nothing.

Honouring that:

1. After M1a is green, run `make coverage`.
2. Set `fail_under` to the measured total **rounded down to the nearest 5**.
3. Put the measured figure in the commit message, so the next person can tell whether coverage
   moved or the floor did.

Rounded down, not to the measured value exactly, because a floor equal to the measurement fails on
any refactor that removes a covered line — and a gate that fires on non-events gets raised past
the point of meaning.

M1a is a reasonable place to set it: unlike M0, it has real logic — a limiter, a retry matrix, a
cache, three parsers — rather than command bodies that raise by design.

---

## 8. What M1 does not test, and why

Stated so the gaps are deliberate.

- **Live EDGAR behaviour beyond one smoke test.** CI sets no credentials and must not
  (convention 7). Recorded fixtures are the contract; when EDGAR changes, the smoke test is what
  tells you, and it is run by a person.
- **Whether the tag fallback chains are right.** M2. M1 asserts that facts arrive intact with
  their taxonomy, tag and unit — which is the precondition for M2's chains being testable at all.
- **Whether `OTHER`-bucketed periods should be dropped or differenced.** M2, and
  [spec question 6](README.md#7-spec-questions). M1 asserts only that the classification is total
  and correct at its boundaries.
- **The item-heading regex against the long tail of filers.** M1b starts the fixture collection;
  §7.4 and ROADMAP M1 both say generality is not chased up front. The parse rate is reported so
  the gap is measured rather than assumed.

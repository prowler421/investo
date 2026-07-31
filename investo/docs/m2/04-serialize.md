# M2 — Serialization

`report/serialize.py`, and the `facts` command's human-readable table. Creates the `report/`
package that M3 then fills in with charts and templates.

DESIGN §4.5 is normative: every run writes `report.json` carrying the full `FinancialHistory`, all
computed metrics, forecast summary, flags, scores, and the config and prompt versions used; it is
what `--explain` dumps and what a future `investo diff` compares; it is versioned independently of
the PDF template. *"Without it the PDF is a dead end — nothing downstream can consume a run."*

M2 fills the parts that exist. The value of writing the envelope now rather than in M5 is that
every later milestone adds a key to a document whose shape and determinism rules are already
settled and tested.

---

## 1. What `report.json` is at M2

```jsonc
{
  "schema_version": 1,
  "generated_by": "investo 0.1.0",
  "run": {
    "ticker": "AAPL",
    "as_of": "2026-07-31",
    "window": ["2021-07-01", "2026-07-31"],
    "lookback_years": 5,
    "manifest_hash": "9f2c1ab4…",
    "config": { "price_provider": "tiingo", "llm_provider": "none", "lookback": "5y" }
  },
  "company": { "cik": 320193, "name": "Apple Inc.", "sic": 3571, "…": "…" },
  "sources": [ /* interned; see §3 */ ],
  "history": { "annual": { "revenue": [ /* facts */ ] }, "quarterly": { "…": [] } },
  "coverage": { "spine": {}, "annual": {}, "quarterly": {}, "findings": [] },
  "restatements": [],
  "market_cap": null,

  // declared, empty until the milestone that fills them
  "analysis": null,     // M4
  "forecast": null,     // M5
  "narrative": null,    // M6 — prompt versions and token spend land in `run` alongside `config`
  "backtest": null      // M7
}
```

**The empty keys are declared rather than omitted.** A consumer written against M2's output should
break loudly when it reaches for a forecast that is not there, not receive a `KeyError` that is
indistinguishable from a typo. It also makes the document's growth visible in a diff: M5 changes
`"forecast": null` to an object, which is one reviewable line rather than a new top-level key
appearing.

**`run.manifest_hash` is the cache fingerprint,** §9.1: the `sha256` over the sorted
`(key, content_sha256)` pairs of the entries **this run used** — a hit or a fresh `put` — not the
whole manifest file. `Cache.manifest_hash()` (M1) returns it. It belongs in the envelope rather
than in `FinancialHistory` for the reason [`03-statements.md` § 1](03-statements.md#1-financialhistory)
gives: two histories built from the same facts must compare equal, and a cache fingerprint would
make them differ.

**`config` records only the resolved settings that affected the run**, never a key. `Settings`
carries `tiingo_key`, `anthropic_key`, `openai_key` and `gemini_key`; §10 says API keys are
"via env only, never committed, **never logged**," and a `report.json` in an output directory is
about as logged as a value gets. The serializer names the fields it emits explicitly — an
allowlist, not a `model_dump()` with exclusions — because the failure mode of a denylist is that
the next field added is emitted by default, and the next field added is as likely to be a key as
not. That inversion is asserted by a test that constructs `Settings` with every key populated and
greps the serialized document for the values.

---

## 2. `schema_version`

An integer, independent of the package version and of the PDF template, incremented when a
consumer written against the previous value would misread the new one.

Adding a key is not a version bump; changing a key's type, its units, or the meaning of its value
is. The distinction matters because the document gains a top-level key at four of the next five
milestones, and a version that increments on every addition tells a reader nothing about whether
their parser still works.

---

## 3. Serializing a `Provenance` tree

`Fact.source` is `SourceRef | Derivation`, and `Derivation.inputs` is recursive. A derived Q4 over
a stitched revenue series is three levels deep and names four accessions.

Written inline, every fact carries its full ancestry, and the refs repeat: one `SourceRef` is
about 220 bytes, and a five-year AAPL run resolves roughly 25 metrics × (5 annual + 20 quarterly)
periods, most of which trace to the same handful of 10-K and 10-Q accessions. So refs are
**interned** into a top-level `sources` array and referenced by index:

```jsonc
"sources": [
  {"accession": "0000320193-19-000119", "taxonomy": "us-gaap",
   "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
   "form": "10-K", "filed": "2019-10-31", "url": "https://…", "fetched_at": "2026-07-31T11:02:21Z"}
],

"history": {"annual": {"revenue": [
  {"value": "265595000000", "period": {"start": "2017-10-01", "end": "2018-09-29", "kind": "annual"},
   "unit": "USD", "source": 0},
  {"value": "301000000",
   "source": {"rule": "q4_from_annual_minus_quarters", "inputs": [3, 4, 5, 6], "note": null}}
]}}
```

An integer is a `SourceRef` index; an object is a `Derivation`, whose `inputs` are the same union
recursively. The discriminator is the JSON type, which needs no tag field and cannot be ambiguous.

Two reasons this is worth the indirection, beyond size:

- **`investo diff` is the stated purpose of this document** (§4.5, and ROADMAP's undecided
  addition). With inline refs, a single restated value shows up in a textual diff as a changed
  number *plus* forty lines of identical provenance moved around. Interned, it is one line.
- **The `sources` array is the appendix.** §9.1 asks for "tag provenance per metric" and a cache
  manifest hash; the array *is* the former, already deduplicated, and M3 renders it directly
  instead of walking every fact to collect distinct refs.

**Index assignment is by sorted key, not by encounter order** — `(accession, taxonomy, tag, filed,
url)`. Encounter order is a function of dict iteration over the resolved metrics, which is stable
but is not something §11's byte-identical gate should depend on, and which changes the moment a
metric is added to the registry. Sorting makes the array a function of its contents.

---

## 4. `Decimal`

`json.dumps` cannot serialize a `Decimal` and will happily serialize the `float` someone converts
it to. CLAUDE.md convention 8 is `Decimal` for money, never `float`, and a serializer that
round-trips through `float` breaks it at the last step, after every earlier layer has been careful.

**Values are emitted as JSON strings**, from `str(value)` — not as JSON numbers. A JSON number is
an IEEE double to most parsers, so `391035000000.01` — the exact value the AAPL fixture carries
specifically to catch this — reads back as `391035000000.010009765625`. The string round-trips
through `Decimal(s)` exactly.

`str(Decimal)` is not normalized, and that is deliberate: `Decimal("1E+2")` and `Decimal("100")`
are equal and print differently, and normalizing would discard the significant figures the filer
filed. `parse_float=Decimal` (M1) preserves the source text's precision, so `str()` reproduces
what SEC published. A test round-trips every fixture value and asserts equality as `Decimal`, not
as string — the guarantee is the value, not the spelling.

**A round-trip test is not sufficient on its own, and this is the trap.** The obvious test reads
the document back with `json.loads(..., parse_float=Decimal)` and compares — and that **passes even
if the value was emitted as a bare JSON number**, because `parse_float=Decimal` reconstructs it
exactly on the way in. It would then be green right up until some other consumer, or a reader
written in anything but Python, parsed the same document with default settings and got
`391035000000.010009765625`.

So there are two assertions and they check different things:

| Assertion | Catches |
|---|---|
| `Decimal(doc["…"]["value"]) == expected` | a value corrupted in transit |
| `'"value": "391035000000.01"' in raw_text` — the **quoting**, in the serialized bytes | the encoding silently changing from string to number |

The second one is what makes this rule durable rather than decorative. Emitting a JSON number is
the change a future contributor makes for readability, it is a one-character diff, and every
value-level test in the suite keeps passing. The AST no-`float` rule
([`05-testing.md` § 4](05-testing.md#4-new-layering-rules)) covers the `float(value)` route into
the same bug; it does not cover a custom `JSONEncoder` that passes a `Decimal` through unquoted,
which is the other route and the more likely one.

Dates are ISO-8601 (`YYYY-MM-DD`); `fetched_at` is ISO-8601 with a `Z` suffix, always UTC, which
`SourceRef.__post_init__` already guarantees is not naive.

---

## 5. Determinism

§11 makes byte-identical output a CI gate. M3 owns the PDF half; M2 owns this document, and it is
the first artifact in the project that the gate applies to.

```python
json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), indent=2)
```

plus a trailing newline, and UTF-8 without a BOM. `sort_keys=True` because `dict` insertion order
is a property of the code that built it, and refactoring the builder should not change the file.

Three things that would otherwise leak nondeterminism, each already handled upstream and restated
because the serializer is where they become visible:

- **No clock read.** Nothing in the document comes from `datetime.now()`. `run.as_of` is the
  command-boundary value; `sources[].fetched_at` comes from the cache entry. Enforced by the AST
  rule in [`05-testing.md` § 4](05-testing.md#4-new-layering-rules).
- **`fetched_at` makes the document a function of the cache, not of the wall clock.** Two runs
  against the same cache produce identical bytes; a run after `--refresh` produces different bytes
  because it saw different data. That is the gate working, not failing, and it is the same
  argument §4.4 makes for the cache being the immutable record of what the model saw.
- **No set or frozenset is serialized directly.** `CompanyFacts.tags_present` and
  `taxonomies_present` are `frozenset`, whose iteration order is a function of hash values.
  Anything reaching the document goes through `sorted()` first.

The M2 gate: `test_serialize::test_two_runs_produce_identical_bytes` — build the document twice
from the same fixtures in one process and assert `bytes` equality, and separately assert it across
a subprocess boundary so `PYTHONHASHSEED` participates. Asserting on bytes rather than on a parsed
comparison is the lesson M1 recorded when `gzip` wrote a filename into the blob header while every
hash-level assertion passed.

---

## 6. The `facts` command's table

`facts` has no `--out` flag and does not acquire one. Its default output is a human-readable table
on stdout; `report.json` goes to stdout under `--json`, which is the flag surface change raised as
[README § Spec question 4](README.md#7-spec-questions).

```
investo facts AAPL --lookback 5y

AAPL  Apple Inc.  CIK 320193  SIC 3571  FY end 0928       as of 2026-07-31

  annual                    FY2022     FY2023     FY2024     FY2025     tag
  revenue                  394,328    383,285    391,035    416,161    us-gaap:RevenueFromContra…
  net income                99,803     96,995     93,736    103,214    us-gaap:NetIncomeLoss
  gross profit             170,782    169,148    180,683    195,940    derived: revenue − cogs
  operating income         119,437    114,301    123,216    133,010    us-gaap:OperatingIncomeLoss
  long-term debt                 —          —          —          —    absent
                                                              USD millions

  coverage                 annual   quarterly       spine: filings (5 annual, 20 quarterly)
  tier 1 (DCF)              92.9%       88.6%
  tier 2 (F/Z/M)            72.7%       63.6%

  findings
  series_stitched      revenue: us-gaap:SalesRevenueNet → us-gaap:RevenueFromContract… at FY2018
  q4_derived           revenue, net income: 1 period each, FY2022
  coverage_below_floor long_term_debt: 0% annual (chain: LongTermDebtNoncurrent → …)
```

Four properties of that output are load-bearing rather than cosmetic, and the first three are the
same ones `investo fetch`'s summary already has:

- **`absent` is a value, not a gap.** A metric with no data prints `—` on every period and `absent`
  in the tag column. A blank row is indistinguishable from a rendering bug.
- **The tag is printed next to the series**, because which tag won is the thing this command exists
  to let you check, and it is the thing §9.1's appendix promises.
- **Coverage prints its spine origin.** A percentage against an `OBSERVED` spine
  ([`03-statements.md` § 2](03-statements.md#2-the-period-spine)) is close to meaningless and must
  never be printed without saying so.
- **Findings are printed in full, not counted.** `3 findings` is a number nobody acts on.

The scale note (`USD millions`) is a presentation choice made once, at the renderer, over values
that are exact `Decimal` throughout. Nothing upstream rescales — see
[`01-tags.md` § 7](01-tags.md#7-units).

---

## 7. What `serialize.py` does not do

- **No PDF, no HTML, no charts, no templates.** M3. `report/` exists after M2 with one module in
  it, which is ROADMAP M2's stated intent: *"creates the `report/` package that M3 then fills in."*
- **No reading.** A `report.json` **reader** is what `investo diff` needs, and `diff` is explicitly
  out of v1 scope. Writing a reader now fixes the deserialization contract before there is a
  consumer to test it against — the same argument ROADMAP makes for building the module tree per
  milestone.
- **No schema migration.** `schema_version` is written and not interpreted; there is one version.
- **No LLM metadata.** §7.5's prompt versions and token spend land in `run` at M6, next to
  `config`. The key is not stubbed, because an empty `prompt_versions` object is indistinguishable
  from a run where the LLM was on and recorded nothing.

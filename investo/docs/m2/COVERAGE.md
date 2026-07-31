# M2 — Coverage measurement

Status: **not run.** The universe is not yet pinned.
Last updated: 2026-08-01 — **M2's code is complete and this measurement is still outstanding**, which
is the state [`README.md` § 11](README.md#11-what-landed-and-what-did-not) predicted and records. Two
consequences worth stating where someone will hit them rather than only in the roadmap: the ≥90%
criterion is **not met and not assessable**, and the eleven tier-2 chain orderings in
`normalize/tags.py` are **proposals** until this file has numbers in it. `tests/test_tags.py::test_probe_covers_every_chain_member`
pins the probe's tag lists against the registry, so the two cannot drift apart while this is pending.

Evidence for ROADMAP M2's first exit criterion — *"≥90% coverage across 20 NASDAQ names on **both**
the DCF metric set and the quality-score metric set"* — and for
[`README.md` § Spec question 6](README.md#7-spec-questions), the tier-2 chain orderings.

This file is to the coverage claim what `tests/fixtures/edgar/PROVENANCE.md` is to the fixtures: a
claim with a procedure attached that someone else can repeat. Until it has numbers in it, the exit
criterion is not met and should not be reported as met.

---

## How to run it

```bash
# 1. Pin the universe. Once, by hand. Paste the output into tests/coverage_probe.py.
uv run python -m tests.coverage_probe select

# 2. Measure.
export INVESTO_SEC_USER_AGENT="Investo research your.email@example.com"
uv run pytest -m network tests/coverage_probe.py -s

# 3. Paste the printed report below, or set COVERAGE_REPORT_OUT=docs/m2/COVERAGE.md to have it written here.
```

The probe is `network`-marked and deselected by default. CLAUDE.md convention 7: CI sets no
`INVESTO_*` variables, so a test that reaches the network must fail rather than quietly succeed,
and keeping this out of the default selection is how that stays true.

It reads through the ordinary cache, so a second run is free and the measurement is reproducible
from the same bytes. Roughly 40 requests at 5 req/s — under a minute of wall time, most of it
downloading `companyfacts` payloads that are 10–40 MB each.

---

## The universe, and why it is pinned rather than sampled

**Pinned**, because a universe recomputed on each run makes two measurements incomparable: a
coverage figure that moved could be the chains improving or the sample changing, and nothing in the
output would distinguish them.

**Stratified**, because tag coverage correlates with filer size and twenty NASDAQ-100 names would
measure around 97% while predicting nothing about the twenty-first company someone runs. §4.2's own
figures make this concrete — `PaymentsToAcquirePropertyPlantAndEquipment` is tagged by 2,696 of
roughly 5,000 filers, and the 2,300 who skip it are not the large ones.

The criteria are **asserted, not merely described** —
`coverage_probe.py::test_the_universe_satisfies_its_own_criteria` fails the build on a universe
that does not meet them. That matters because the person choosing the universe is also the person
who wants it to pass:

- exactly 20 names, no duplicate CIK
- **four per market-cap quintile**, quintiles taken over the NASDAQ listing
- **at least one bank or insurer** (SIC 6000–6499) and **one REIT** (SIC 6798) — §6.10's refusal
  path is otherwise unmeasured
- **at least one filer with under three years of history** — the thin-coverage case, which is the
  one a user is most likely to hit and least likely to be warned about
- **none of the seven fixture companies**, so the measurement is out-of-sample against chains that
  were written while looking at them

Market cap is computed as price × cover-page shares summed across classes (§4.3), not fetched from
a quote API, so every number in the table below is traceable by the same rule as every other number
in this project.

| ticker | CIK | name | SIC | quintile | market cap (USD, as of pin) | first filing |
|---|---|---|---|---|---|---|
| _not yet selected_ | | | | | | |

---

## Results

_Not run._

<!-- Paste the probe's output below this line. Keep prior runs; a coverage figure is only
     interesting next to the one before it, and a regression is the thing this file exists to
     make visible. -->

### Per-metric fill rate

| metric | tier | annual | quarterly |
|---|---|---|---|
| — | | | |

### Tier aggregates — the exit criterion

| tier | annual | quarterly |
|---|---|---|
| 1 (DCF) | — | — |
| 2 (F/Z/M) | — | — |

### Chain member hit counts

**This table decides the tier-2 chain orderings.** `docs/m2/01-tags.md` §4's orderings are
proposals; §4.2 carries measured entity counts for tier 1 and nothing for tier 2. Order the tier-2
chains by this column, then revise `01-tags.md` §4 and fold the result into DESIGN §4.2 — in the
same commit as this file, per `docs/m2/README.md` §2.

| tag | periods |
|---|---|
| — | |

### Per-filer absences

_Not run._

---

## Reading the result

**A figure under 90% is a decision, not a bug**, and it should be made from the table rather than
by adding chain members until the number moves. §4.2 says `LongTermDebtNoncurrent` covers 1,532 of
~5,000 filers and calls that chain *"the weakest of the set. Expect misses; mark leverage metrics
low-confidence."* A hard 90% floor there will be met by a member that means something slightly
different, which moves the number and leaves the report's leverage figures quietly wrong for
exactly the filers the member was added to catch.

So the probe asserts nothing about 90%. If the measurement lands under it, the honest response is
**per-metric floors with the weak ones named** — a ROADMAP amendment made of evidence. ROADMAP's M2
entry already records that as an expected outcome rather than a failure.

**Two figures worth comparing once `normalize/` exists.** The probe measures *presence*: a metric
counts as filled if any chain member has a fact ending on a spine date. That is an upper bound on
what the resolver achieves. Running both and diffing them measures what the pipeline drops —
exclusivity collapses, unit mismatches, `OTHER`-bucket periods — and a large gap is a finding about
the pipeline rather than about the filers.

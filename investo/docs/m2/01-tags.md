# M2 — Tag chains

`normalize/tags.py`. The registry that answers "which XBRL tag is revenue for this filer, in this
period" — and the module that owns every `us-gaap` literal in the codebase.

DESIGN §4.2 is normative on the chains and on why hardcoding one tag per metric is the failure
this project exists to avoid. What follows is the resolution algorithm those chains are run
through, plus four per-metric properties §4.2 does not name and which the chains are unusable
without: **aggregation class, unit, sign, and exclusivity.** Each is marked **[extends §4.2]** and
carried into [README § Spec questions](README.md#7-spec-questions).

---

## 1. Resolution is period-wise, and this is the decision the milestone turns on

DESIGN §4.2's table says "first match wins" and does not say *at what granularity*. There are two
readings and they disagree on every filer with a long history.

**Series-level:** walk the chain once, take the first member that has any fact in the window, use
that tag for the whole series.

**Period-level:** for each period independently, walk the chain and take the first member that has
a fact for *that* period.

The revenue chain is ordered `RevenueFromContractWithCustomerExcludingAssessedTax` →
`...IncludingAssessedTax` → `Revenues` → `SalesRevenueNet`. Under the series-level reading, Apple
resolves to the ASC 606 tag — and Apple did not use that tag before FY2018. Checked against
`tests/fixtures/edgar/companyfacts/AAPL.trimmed.json`:

| Period | `SalesRevenueNet` | `RevenueFromContractWithCustomer…ExcludingAssessedTax` |
|---|---|---|
| FY2016 (2015-09-27 → 2016-09-24) | 215,639,000,000 | — |
| FY2017 (2016-09-25 → 2017-09-30) | 229,234,000,000 | — |
| FY2018 (2017-10-01 → 2018-09-29) | — | 265,595,000,000 |
| FY2019 (2018-09-30 → 2019-09-28) | — | 391,035,000,000.01 |

Series-level resolution returns two of those four and reports 100% coverage on the two it found.
The other two are not recorded as missing, because from the resolver's point of view nothing is
missing — the tag it chose simply has no facts back there. **A hole that presents as full coverage
is worse than a hole**, and this one lands on the flagship fixture.

So: **resolution is period-wise.** ASC 606 stitching then is not a feature — it is the absence of
a bug, and §4.2's "long histories **must** stitch across the boundary or the series has a hole" is
satisfied by construction rather than by a special case keyed on 2018.

A second argument, independent of the first and arguably harder to work around. A series-level
primary depends on *what is in the window*, so the same fiscal year resolves to a different tag
under `--lookback 5y` and `--lookback 10y`. Two reports on the same company would then disagree
about FY2020's revenue tag with no restatement having occurred, and the appendix — which prints
tag provenance per metric (§9.1) — would print two different answers for one fact. Period-wise
resolution is window-independent: the tag chosen for FY2020 is a function of FY2020's facts alone.

**The cost, stated plainly.** Period-wise resolution can mix tags within one series, and §4.2 says
in terms that excluding-assessed-tax and including-assessed-tax revenue "are different numbers;
never mix within one series." That constraint is real and is not handled by the resolution
algorithm. It is handled by [§ 5, exclusivity groups](#5-exclusivity-groups), which is a property
of the chain declaration — the right place for it, because *which* tags are mutually incompatible
is domain knowledge about the taxonomy, not a property of any filer's data.

### The interface

```python
def resolve(
    metric: Metric,
    facts: Mapping[tuple[str, str], Sequence[RawFact]],
    *,
    periods: Iterable[FiscalPeriod],
) -> tuple[Resolution, ...]: ...

@dataclass(frozen=True, slots=True)
class Resolution:
    period: FiscalPeriod
    facts: tuple[RawFact, ...]    # () is an absence, and absences are returned, not skipped
    chain_index: int | None       # position in the chain; 0 is the preferred tag
```

**`facts` is a tuple rather than a single `RawFact | None`, and the summing member is why.**
[§ 4](#4-tier-2--what-the-fzm-scores-need)'s SG&A member matches a period only when *both*
`GeneralAndAdministrativeExpense` and `SellingAndMarketingExpense` are present, and its value is
their sum over a two-ref `Derivation`. A single-fact field cannot carry that, and the alternative —
computing the sum in `facts.py` — puts a piece of tag knowledge outside `tags.py`, which is exactly
what [§ 11](#11-what-tagspy-does-not-do) forbids and what the `us-gaap` allowlist is enforcing.

So the resolver owns it end to end: an ordinary member yields a one-tuple, the summing member
yields a two-tuple, and the caller's rule is uniform — empty is an absence, length one is a filed
fact carrying its own `SourceRef`, length greater than one is a `Derivation` over all of them with
`rule="sga_summed_components"`. `tags_used` reports every tag in the tuple, so the appendix prints
both components rather than implying one tag produced the number. That the pluralised field reads
slightly awkwardly for the twenty-four metrics that never need it is the price of the one that
does, and it is cheaper than a second code path.

`resolve` returns one `Resolution` per requested period, including the empty ones. A resolver that
returns only what it found makes the coverage denominator unknowable — see
[`03-statements.md` § The period spine](03-statements.md#2-the-period-spine).

`chain_index` is what makes a stitch detectable: a metric whose resolutions span more than one
index used more than one tag, and that is the §6.4 data-integrity finding.

---

## 2. The chain declaration

One record per metric. The chain itself is §4.2's; the four other fields are new.

```python
@dataclass(frozen=True, slots=True)
class Chain:
    metric: Metric
    members: tuple[Member, ...]        # ordered; first match wins, per period
    aggregation: Aggregation           # FLOW | INSTANT | PER_SHARE      [extends §4.2]
    unit: str                          # the only unit accepted               [extends §4.2]
    exclusive: frozenset[str] = frozenset()   # exclusivity group name  [extends §4.2]

@dataclass(frozen=True, slots=True)
class Member:
    taxonomy: str                      # "us-gaap" | "dei"
    tag: str
    flip_sign: bool = False            # this member's element is signed opposite to the metric
    note: str | None = None            # printed in the appendix beside the tag

CHAINS: Final[Mapping[Metric, Chain]]
```

`CHAINS` is a mapping over the whole of `Metric`, and the completeness of that mapping is a test —
see [`05-testing.md`](05-testing.md#5-the-guaranteeviolation-test-table). `Metric` declares both
tiers already (M1, `domain/models.py`), which is what makes an unmapped metric a visible failure
rather than a metric nobody thought of. That was the stated reason for declaring tier 2 early, and
it only pays out if M2 asserts on it.

---

## 3. Tier 1 — the DCF metric set

Chains verbatim from §4.2's table; entity counts are §4.2's, from the CY2025Q1 frames API, and are
reproduced so the ordering is auditable rather than folkloric.

| Metric | Chain, in order | Agg | Unit | Sign |
|---|---|---|---|---|
| `REVENUE` | `RevenueFromContractWithCustomerExcludingAssessedTax` (2,543) → `…IncludingAssessedTax` → `Revenues` (1,836) → `SalesRevenueNet` | FLOW | USD | as filed |
| `NET_INCOME` | `NetIncomeLoss` (5,315) → `ProfitLoss` (2,724) | FLOW | USD | as filed |
| `GROSS_PROFIT` | `GrossProfit` (2,023) → **derived** `REVENUE − COGS` | FLOW | USD | as filed |
| `OPERATING_INCOME` | `OperatingIncomeLoss` | FLOW | USD | as filed |
| `ASSETS` | `Assets` (5,633) | INSTANT | USD | as filed |
| `LIABILITIES` | `Liabilities` (4,998) → **derived**, see [§ 9](#9-the-equity-trap-in-the-liabilities-derivation) | INSTANT | USD | as filed |
| `EQUITY` | `StockholdersEquity` (5,452) | INSTANT | USD | as filed |
| `CASH` | `CashAndCashEquivalentsAtCarryingValue` (4,508) | INSTANT | USD | as filed |
| `OPERATING_CASH_FLOW` | `NetCashProvidedByUsedInOperatingActivities` (4,784) → `…ContinuingOperations` (205) | FLOW | USD | as filed |
| `CAPEX` | `PaymentsToAcquirePropertyPlantAndEquipment` (2,696) → `PaymentsToAcquireProductiveAssets` → `PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets` → `PaymentsForCapitalImprovements` | FLOW | USD | **outflow positive** |
| `LONG_TERM_DEBT` | `LongTermDebtNoncurrent` (1,532) → `LongTermDebt` → `LongTermDebtAndCapitalLeaseObligations` | INSTANT | USD | as filed |
| `SHARES_COVER` | `dei:EntityCommonStockSharesOutstanding` (4,747) | INSTANT | shares | as filed |
| `SHARES_DILUTED_WEIGHTED` | `WeightedAverageNumberOfDilutedSharesOutstanding` | FLOW | shares | as filed |
| `EPS_DILUTED` | `EarningsPerShareDiluted` (4,605) → `EarningsPerShareBasicAndDiluted` → **derived** | PER_SHARE | **USD/shares** | as filed |

Three entries carry the notes §4.2 attaches, restated as rules the code enforces:

- **`CASH` does not fall back to `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents`.**
  §4.2: the latter includes restricted cash and ties to the cash-flow statement; they are
  different numbers. A chain is an ordering over *substitutes*, and these are not substitutes. If
  a filer tags only the restricted-inclusive concept, `CASH` is absent for that filer and the
  coverage report says so. Adding it as a fallback would make the EV bridge (§5.4) add restricted
  cash to equity value for some filers and not others, with nothing in the output distinguishing
  them.
- **`NET_INCOME`'s two members are not interchangeable and the chain order encodes a pairing.**
  `NetIncomeLoss` is parent-only and `ProfitLoss` includes NCI; §4.2 pairs parent-only net income
  with `StockholdersEquity`, which is also parent-only. So a filer resolving to `ProfitLoss`
  produces an ROE whose numerator and denominator have different scopes. This is recorded as a
  finding, not silently corrected — see [`03-statements.md` § 4](03-statements.md#4-findings-m2-records).
- **`SHARES_DILUTED_WEIGHTED` is `FLOW`, which reads oddly and is correct.** It is a weighted
  *average over a period*, so it has a start and an end and buckets by duration like any flow. It
  is not additive across quarters — see [§ 6](#6-aggregation-class).

---

## 4. Tier 2 — what the F/Z/M scores need

ROADMAP M2: *"Building only the first tier means M4 stalls."* §4.2 names roughly ten to fifteen
additional chains, several as fragmented as capex, and does not order them. The orderings below
are this document's and are the weakest-evidenced part of it — they are stated as proposals in
[README § Spec question 6](README.md#7-spec-questions) rather than presented as measured.

| Metric | Chain, in order | Agg | Unit | Sign |
|---|---|---|---|---|
| `ASSETS_CURRENT` | `AssetsCurrent` | INSTANT | USD | as filed |
| `LIABILITIES_CURRENT` | `LiabilitiesCurrent` | INSTANT | USD | as filed |
| `RETAINED_EARNINGS` | `RetainedEarningsAccumulatedDeficit` | INSTANT | USD | as filed |
| `RECEIVABLES` | `AccountsReceivableNetCurrent` → `ReceivablesNetCurrent` → `AccountsReceivableGrossCurrent` | INSTANT | USD | as filed |
| `COGS` | `CostOfGoodsAndServicesSold` → `CostOfRevenue` → `CostOfGoodsSold` → `CostOfServices` | FLOW | USD | as filed |
| `SGA` | `SellingGeneralAndAdministrativeExpense` → `GeneralAndAdministrativeExpense` + `SellingAndMarketingExpense` (**summed**, not substituted) | FLOW | USD | as filed |
| `DEPRECIATION_AMORTIZATION` | `DepreciationDepletionAndAmortization` → `DepreciationAmortizationAndAccretionNet` → `Depreciation` | FLOW | USD | as filed |
| `INTEREST_EXPENSE` | `InterestExpense` → `InterestExpenseNonoperating` → `InterestIncomeExpenseNet` (**flip**) | FLOW | USD | **expense positive** |
| `SBC` | `ShareBasedCompensation` → `AllocatedShareBasedCompensationExpense` | FLOW | USD | as filed |
| `OPERATING_LEASE_LIABILITY` | `OperatingLeaseLiabilityNoncurrent` → `OperatingLeaseLiability` | INSTANT | USD | as filed |
| `SHARE_ISSUANCE_PROCEEDS` | `ProceedsFromIssuanceOfCommonStock` → `ProceedsFromIssuanceOrSaleOfEquity` | FLOW | USD | as filed |

**`SGA` is the one entry that is not a chain**, and it is called out because the type has to
accommodate it or the metric is wrong for every filer that splits the line. A filer reports either
the combined `SellingGeneralAndAdministrativeExpense` *or* the two components separately;
substituting one component for the combined figure understates the metric by the other component,
silently, and Piotroski's margin test would then improve for a filer that merely changed its
presentation. So `Member` needs a sum variant. The narrow form — a member that names two tags and
requires **both** present for that period — is enough, keeps the resolution algorithm unchanged
(the member either matches the period or it does not), and produces a `Derivation` naming both
refs. Recorded as [spec question 7](README.md#7-spec-questions).

**The sum variant is used exactly once, and that is asserted.** It is the kind of construct that
spreads once it exists — several tier-2 concepts have a plausible components-summing reading, and
each one added is another place where a filer's presentation choice changes a metric's value. So
the registry gets the same treatment M1 gave the `dei` carve-out: a test asserts that exactly one
`Member` in `CHAINS` is a sum, and names it. A second use is then a visible edit with a reviewer
attached, rather than a pattern that arrived one row at a time. If a second one is genuinely
needed, that is the signal to ask whether the components should be their own `Metric` instead —
which is the answer for anything M4 might want to read separately.

**EBIT is not in `Metric`, and it should not be added here.** §4.2 lists "EBIT (derived)" among
tier 2. It is `OPERATING_INCOME`, or `NET_INCOME + INTEREST_EXPENSE + tax` where operating income
is absent, and the choice between those two depends on what Altman's variant needs — which is
M4's question, over M2's output, and answerable without a new `Metric` member. Adding a metric
here that no chain maps to would break the completeness test for a value that is a one-line
arithmetic in the consumer.

---

## 5. Exclusivity groups

**[extends §4.2]** §4.2: excluding-assessed-tax and including-assessed-tax revenue "are different
numbers; never mix within one series." Period-wise resolution ([§ 1](#1-resolution-is-period-wise-and-this-is-the-decision-the-milestone-turns-on))
would happily alternate between them from one quarter to the next if a filer tagged both
inconsistently, and the resulting series would show a step change that is a tagging artifact
presented as a growth rate.

An exclusivity group names a set of chain members of which **at most one may contribute to a
series**. Resolution runs in two passes:

1. Resolve every period period-wise, as [§ 1](#1-resolution-is-period-wise-and-this-is-the-decision-the-milestone-turns-on) describes.
2. For each exclusivity group, if more than one of its members appears among the resolutions, keep
   the member with the **most periods resolved** and re-resolve every period it did not cover
   through the chain with the losing members removed. Ties break to the earlier chain index.

The groups:

| Group | Members | Why |
|---|---|---|
| `revenue_assessed_tax` | `RevenueFromContractWithCustomerExcludingAssessedTax`, `…IncludingAssessedTax` | §4.2 states it directly. The two differ by sales and excise taxes collected — for a filer with material excise, a mixed series has a visible discontinuity that looks like growth. |
| `net_income_scope` | `NetIncomeLoss`, `ProfitLoss` | Parent-only vs. including-NCI. Mixing produces a series whose year-over-year change is partly a change in minority interest. |
| `debt_lease_scope` | `LongTermDebt`, `LongTermDebtAndCapitalLeaseObligations` | The second includes capitalized leases and the first does not; §5.3 treats leases as debt separately, so a mixed series double-counts leases in some years. |

`SalesRevenueNet` is deliberately **not** in `revenue_assessed_tax`. It is the pre-2018 concept the
whole series has to stitch to, and putting it in the group would defeat [§ 1](#1-resolution-is-period-wise-and-this-is-the-decision-the-milestone-turns-on).
The stitch is a *temporal* substitution across a standards boundary; the assessed-tax pair is a
*definitional* substitution within one standard. One is required and one is forbidden, and the
group is what distinguishes them.

The re-resolution in step 2 is why the exclusivity check runs after a full pass rather than
greedily. A greedy check would lock in whichever member happened to appear in the earliest period,
and the earliest period in a window is the one most likely to be an off-pattern legacy tagging.

### A permanent switch is not the same as flip-flopping, and majority-wins gets it wrong

Step 2 as stated assumes a filer using two members of a group is *noise*. There is a second case
where it is not: a filer that **switches permanently** — a new tax nexus makes assessed taxes
material, and every period from that point on is tagged including-assessed-tax. Majority-wins then
pushes the minority side down the chain to a weaker fallback, silently. That is the ASC 606 failure
this document opens with, recurring one level down, and `SalesRevenueNet`'s hand-written exclusion
from the group is no help because it is specific to that pair.

The signal that distinguishes them is temporal, and it is the same signal that makes the ASC 606
stitch legitimate: **do the members partition the timeline, or interleave?**

| Shape | Reading | Behaviour |
|---|---|---|
| One member covers a contiguous prefix, the other a contiguous suffix, no interleaving | A switch. The filer changed what it tags, on a date. | **Stitch.** Keep both, emit `exclusivity_switch` naming the boundary date and both tags, and `series_stitched`. |
| The members alternate | Noise. Inconsistent tagging, no event behind it. | Majority-wins, as step 2 describes. |

So the escape hatch is derived from the data rather than hardcoded, which makes it better than the
one `SalesRevenueNet` gets — a future group needs no hand-written exception to be handled
correctly.

**Stitching a permanent switch does leave a level shift in the series**, and that is not hidden: a
revenue series that starts including assessed taxes steps up for a reason that is not growth. But
majority-wins produces a discontinuity too — it just relabels one side to a tag whose definition
may be further from the other, with nothing in the output saying so. Between two discontinuities,
take the one that is named, dated and attached to a finding. Deciding what the finding *means* is
M4's, per [`03-statements.md` § 4](03-statements.md#4-findings-m2-records).

The test that separates the two cases is the point of the rule, and a fixture that only flip-flops
would let a stitch-everything implementation pass. `TIER2` carries both shapes.

---

## 6. Aggregation class

**[extends §4.2]** Three classes, and the distinction is load-bearing for
[`02-facts.md`](02-facts.md)'s Q4 derivation.

| Class | Meaning | Q4 derivation | YTD differencing | Example |
|---|---|---|---|---|
| `FLOW` | additive over consecutive periods | yes | yes | revenue, capex, OCF |
| `INSTANT` | a balance at a point in time | **no** | **no** | assets, cash, long-term debt |
| `PER_SHARE` | a ratio whose denominator varies by period | **no** | **no** | diluted EPS |

`INSTANT` is straightforward: the balance-sheet fact at the fiscal year end *is* the Q4 balance,
and subtracting three quarterly balances from an annual balance produces a number with no meaning.

`PER_SHARE` is the one that would otherwise be got wrong, and it would look right.
`Q4 EPS = FY EPS − (Q1+Q2+Q3 EPS)` is arithmetically well-formed and close enough to plausible
that no eyeball catches it, but it is wrong whenever the share count moved during the year —
which is every year for any company with a buyback or an equity comp program, i.e. most of the
NASDAQ universe. The error is largest for exactly the fast-diluting companies whose EPS matters
most. So `PER_SHARE` metrics are never derived by subtraction; a missing Q4 EPS stays missing, and
M4 recomputes it from `NET_INCOME / SHARES_DILUTED_WEIGHTED` if it wants one.

`SHARES_DILUTED_WEIGHTED` is `FLOW` for bucketing (it has a duration) but the Q4 rule needs it
excluded from subtraction for the same reason as EPS — the annual weighted average is not the sum
of the quarterly ones. It is therefore declared `FLOW` with a `subtractable = False` field on the
chain. Two axes rather than one, because bucketing and derivation are asking different questions
and one enum answering both would force `SHARES_DILUTED_WEIGHTED` to lie about one of them.

---

## 7. Units

**[extends §4.2]** Each chain declares exactly one acceptable unit, and a fact in any other unit is
excluded and counted in the coverage report as a unit mismatch.

Three things this catches, all of which §4.2 warns about and none of which is caught by anything
else in the pipeline:

- **EPS arrives under `USD/shares`, not `USD`.** §4.2 says so explicitly. A resolver that ignores
  unit finds an `EarningsPerShareDiluted` fact under `USD` — some filers do tag one — and reports
  an EPS three orders of magnitude off.
- **Non-USD reporting currencies.** §12 records these as out of scope for the US-only universe. A
  unit filter turns "out of scope" from a comment into an absence that appears in the coverage
  report, which is the difference between a known limitation and a wrong number.
- **`pure`-unit facts under a money tag.** The `ARXS` fixture carries a `pure` unit with decimal
  values; a filer that tags a ratio under a concept the chain names would otherwise contribute it
  to a dollar series.

The check is on the verbatim `RawFact.unit` string, which M1 preserves as the units-dict key
without interpretation. No unit *conversion* happens anywhere in M2: a thousands-denominated
filing is not rescaled, because `companyfacts` values are already in the unit named and a scaling
step would be a second place for a factor-of-1000 error to live.

---

## 8. Sign conventions

**[extends §4.2]** DESIGN says nothing about sign, and the failure mode is silent, filer-dependent,
and lands in the middle of the FCF build.

`PaymentsToAcquirePropertyPlantAndEquipment` has a natural debit balance and is filed as a
**positive** number — the amount paid. §5.3's build is `+ D&A − capex − ΔWC − SBC`, so the
consumer subtracts. `InterestIncomeExpenseNet`, if a filer uses it, is signed the other way: a net
*expense* appears negative. If M2 hands M5 a `CAPEX` series that is positive for most filers and
negative for a few, FCF is wrong for that few, in the direction that flatters them, and nothing in
the report distinguishes them.

The rule:

- **Each chain declares the convention M2 emits.** `CAPEX` is outflow-positive; `INTEREST_EXPENSE`
  is expense-positive; everything else is as filed. The convention is printed in the appendix
  alongside the tag.
- **The flip is a property of the chain member, not of the data.** `Member.flip_sign` is set on
  `InterestIncomeExpenseNet` because that element is signed opposite to the metric's convention by
  construction. It is never set in response to observing a negative value.
- **A fact whose sign contradicts the convention is kept, and counted.** A negative capex quarter
  is real — a disposal netted against acquisitions — and dropping it makes FCF wrong in the other
  direction. It appears in the coverage report as a sign anomaly, which is a §6.4 data-integrity
  finding for M4 rather than something M2 corrects.
- **A flip produces a `Derivation`,** `rule="sign_normalized"`, with the single input ref and a
  `note` naming the convention. `Derivation.refs()` flattens to that one ref, so nothing downstream
  changes. Where no flip occurs — the overwhelming majority of facts — the `Fact` carries a bare
  `SourceRef` and `report.json` stays small.

The alternative considered and rejected was to emit everything as filed and document the
convention per metric. That pushes a per-metric sign question into M3's charts, M4's ratios and
M5's FCF build independently, and one of the three eventually gets it wrong on a chain member the
other two never see.

---

## 9. The equity trap in the liabilities derivation

§4.2's table gives the total-liabilities fallback as:

> derive `LiabilitiesAndStockholdersEquity − StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`

and separately gives `EQUITY` as `StockholdersEquity` (parent-only). **These are two different
equity tags and the derivation must use the second one, not `Metric.EQUITY`.**

Written as `L&SE − Metric.EQUITY`, which is the natural way to write it once `EQUITY` is a
resolved metric sitting right there, the result overstates total liabilities by exactly the
noncontrolling interest. For a filer with material NCI that inflates net debt, deflates interest
coverage, and moves the Altman Z leverage term — for the ~11% of filers §4.2 says never tag
`Liabilities` at all, which is precisely the population that reaches this branch.

So the derivation names its own tags rather than composing over metrics:

```
LIABILITIES  = LiabilitiesAndStockholdersEquity
             − StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest
```

with `StockholdersEquity` as a **last** fallback for the subtrahend, used only when the
including-NCI tag is absent, and that use recorded as a finding because it is the approximation
the paragraph above warns about. Approximating is better than omitting the metric entirely; doing
it invisibly is not.

This is the argument for cross-metric derivations naming tags directly rather than composing over
`Metric`, and it generalizes: a metric is a *selection*, and two selections that share a name do
not share a definition.

---

## 10. Cross-metric derivations, and their order

Chain resolution is per-metric and can run in any order. The derivations cannot: `GROSS_PROFIT`
needs `REVENUE` and `COGS` resolved first.

```python
DERIVATIONS: Final[tuple[DerivedMetric, ...]]      # declared in dependency order
```

| Derived | Rule | Inputs | Fires when |
|---|---|---|---|
| `GROSS_PROFIT` | `gross_profit_from_revenue_minus_cogs` | `REVENUE`, `COGS` | `GrossProfit` absent for that period |
| `LIABILITIES` | `liabilities_from_lse_minus_equity` | two tags, [§ 9](#9-the-equity-trap-in-the-liabilities-derivation) | `Liabilities` absent for that period |
| `EPS_DILUTED` | `eps_from_net_income_over_diluted_shares` | `NET_INCOME`, `SHARES_DILUTED_WEIGHTED` | both EPS tags absent for that period |

Four rules govern all of them:

1. **Per period, not per series.** A filer that tags `GrossProfit` in three years of four gets the
   fourth derived and the other three as filed, and the coverage report distinguishes them.
2. **Both inputs must be present for the same period, with the same `unit`.** A derivation over a
   period one input is missing is not attempted, and the metric stays absent.
3. **The order is declared and acyclic**, asserted by a test. Nothing here is currently cyclic; the
   test exists because the first cycle would be introduced by a plausible-looking addition (deriving
   `COGS` from `REVENUE − GROSS_PROFIT`, which is exactly as true and exactly as tempting), and it
   would present as a recursion error in a report run rather than as a design mistake.
4. **The `Derivation` is recursive and is not flattened.** `EPS_DILUTED` derived from a
   `NET_INCOME` that is itself as-filed carries one level; a derived margin over a stitched revenue
   series carries three. `Derivation.inputs: tuple[Provenance, ...]` was specified recursively in
   M1 for this.

`EPS_DILUTED`'s derivation is the one §4.2's table writes as a bare "→ derive". Its unit is
`USD/shares` and both inputs are `USD` and `shares`, so it is the one place in M2 where the output
unit is not an input unit — worth a test, because a unit check that only ever compares equal is a
unit check that has not been exercised.

---

## 11. What `tags.py` does not do

- **No I/O, no clock, no cache.** It is a table and a resolution function over facts already in
  memory. Enforced by the layering rules in [`05-testing.md` § 4](05-testing.md#4-new-layering-rules).
- **No `as_of` filtering and no dedup.** It receives facts that are already filtered and deduped —
  see [`02-facts.md` § 1](02-facts.md#1-the-pipeline-order-and-why-it-is-not-negotiable) for why
  that order and not the other.
- **No coverage arithmetic.** It returns `Resolution` records including the empty ones;
  [`03-statements.md`](03-statements.md) counts them, because counting needs the period spine and
  the spine comes from the filing history rather than from the facts.
- **No sector logic.** §6.10's bank/REIT refusal keys on SIC, which is `CompanyProfile`'s, and it
  suppresses the *valuation*, not the chains. A bank still gets a revenue series; what it does not
  get is a DCF. Nothing in this module knows the difference.
- **No peer or frames handling.** `RawFact.frame` is carried by M1 and restricted to peer
  cross-sections (§4.2). `resolve` ignores it entirely, and a test asserts that a fact's `frame`
  value cannot change which fact wins — SEC's own selection is not point-in-time stable, and
  letting it break a tie would put a lookahead leak inside the resolver.

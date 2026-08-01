"""Measures tag coverage across a pinned universe of NASDAQ filers. ROADMAP M2's exit criterion.

This is **not a unit test**, despite living under ``tests/``. It reaches the live EDGAR API, it is
marked ``network`` and therefore deselected by default, and it is run by a person. CLAUDE.md
convention 7 is why: CI sets no ``INVESTO_*`` variables, so a test that reaches the network must
fail rather than quietly succeed, and the way to keep that true is to keep this out of the default
selection.

    uv run pytest -m network tests/coverage_probe.py -s

Its output is transcribed into ``docs/m2/COVERAGE.md`` with the date and the universe. That file is
the evidence for the exit criterion, in the same way ``tests/fixtures/edgar/PROVENANCE.md`` is the
evidence for the fixtures: a claim with a procedure attached that someone else can repeat.

Design notes, all from ``docs/m2/05-testing.md`` §3:

**The universe is pinned, not sampled at run time.** A universe recomputed on each run makes two
measurements incomparable — a coverage figure that moved could be the chains improving or the
sample changing, and nothing in the output distinguishes them. So :func:`select_universe` is run
*once*, by hand, and its output is pasted into :data:`UNIVERSE` and into ``COVERAGE.md``.

**It is stratified, because tag coverage correlates with filer size.** Twenty NASDAQ-100 names
would measure around 97% and predict nothing about the twenty-first company someone runs. §4.2's
own figures make the point: ``PaymentsToAcquirePropertyPlantAndEquipment`` is tagged by 2,696 of
roughly 5,000 filers, and the filers who skip it are not the large ones.

**It measures chain members, not metrics — for now.** ``normalize/`` does not exist when this file
is first written, and waiting for it would push the measurement to the end of the milestone, which
is exactly the sequencing ``docs/m2/README.md`` §2 argues against. So the probe reports raw tag
presence per period, per chain member, which is the input the chain orderings need and is
answerable on day one. :func:`measure` grows a second mode once ``normalize.statements`` lands, and
the two must agree on tier aggregates for a filer with no derivations — a check worth running once,
because a disagreement means the resolver drops facts the payload contains.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from investo.config import load_settings
from investo.domain.periods import window as lookback_window
from investo.domain.provenance import SourceContext
from investo.errors import ConfigError
from investo.fetch import open_cache
from investo.ingest.edgar import client as edgar
from investo.ingest.edgar.companyfacts import parse_companyfacts
from investo.ingest.edgar.submissions import parse_submissions

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from investo.ingest.edgar.companyfacts import CompanyFacts
    from investo.ingest.edgar.submissions import FilingRow

pytestmark = pytest.mark.network

REPORT_PATH: Final = Path(__file__).parent.parent / "docs" / "m2" / "COVERAGE.md"

# Set to any path to have the report written there as well as printed.
#
# **Deliberately not `INVESTO_`-prefixed, and that is not a style choice.** `Settings` sets
# ``env_prefix="INVESTO_"`` with ``extra="forbid"`` (config.py), so *any* unrecognised `INVESTO_*`
# variable in the environment is a validation error — which `load_settings` turns into exit 5 with
# the offending key named. An `INVESTO_WRITE_COVERAGE` would therefore break every command in the
# CLI for as long as it was exported, including the one this probe calls. The behaviour is correct
# (a typo in a config key should not be silent) and it means the `INVESTO_` namespace is reserved
# for settings fields exclusively. Worth knowing before adding a debug flag.
REPORT_OUT_VAR: Final = "COVERAGE_REPORT_OUT"

# The seven companies whose payloads are checked in as fixtures. Excluded from the universe so the
# measurement is out-of-sample against the chains — which were written while looking at them.
FIXTURE_CIKS: Final[frozenset[int]] = frozenset(
    {320193, 2093536, 1000045, 1000046, 1908259, 36104, 1063761}
)

ANNUAL_FORMS: Final = frozenset({"10-K", "10-KT"})
QUARTERLY_FORMS: Final = frozenset({"10-Q", "10-QT"})

BANK_SIC: Final = range(6000, 6500)
REIT_SIC: Final = 6798


# ---------------------------------------------------------------------------
# The chain members under measurement
# ---------------------------------------------------------------------------
# Deliberately a literal here rather than an import from ``normalize.tags``, and this is the one
# place in the repo where duplicating the tag list is right. Two reasons:
#
#   1. The probe has to run before ``normalize/tags.py`` exists, which is the whole point of
#      starting the measurement on day one.
#   2. Importing the registry would make the probe measure the registry's own opinion. The
#      question it answers is "how often is each tag actually filed", which is prior to any
#      ordering — and it is the evidence that decides the tier-2 orderings
#      (``docs/m2/README.md`` spec question 6).
#
# ``tests/test_tags.py::test_probe_covers_every_chain_member`` asserts this list is a superset of
# the registry's members once the registry exists, so the duplication cannot silently drift into
# measuring a different question than the one the chains ask.

TIER1: Final[Mapping[str, tuple[str, ...]]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "assets": ("Assets",),
    "liabilities": ("Liabilities", "LiabilitiesAndStockholdersEquity"),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "cash": ("CashAndCashEquivalentsAtCarryingValue",),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
        "PaymentsForCapitalImprovements",
    ),
    "long_term_debt": (
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
    ),
    "shares_diluted_weighted": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "eps_diluted": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
}

TIER2: Final[Mapping[str, tuple[str, ...]]] = {
    "assets_current": ("AssetsCurrent",),
    "liabilities_current": ("LiabilitiesCurrent",),
    "retained_earnings": ("RetainedEarningsAccumulatedDeficit",),
    "receivables": (
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
        "AccountsReceivableGrossCurrent",
    ),
    "cogs": ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold", "CostOfServices"),
    "sga": (
        "SellingGeneralAndAdministrativeExpense",
        "GeneralAndAdministrativeExpense",
        "SellingAndMarketingExpense",
    ),
    "depreciation_amortization": (
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "Depreciation",
    ),
    "interest_expense": (
        "InterestExpense",
        "InterestExpenseNonoperating",
        "InterestIncomeExpenseNet",
    ),
    "sbc": ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"),
    "operating_lease_liability": ("OperatingLeaseLiabilityNoncurrent", "OperatingLeaseLiability"),
    "share_issuance_proceeds": (
        "ProceedsFromIssuanceOfCommonStock",
        "ProceedsFromIssuanceOrSaleOfEquity",
    ),
}

# The cover-page share count is tier 1 but lives in `dei`, so it is keyed separately.
DEI_SHARES: Final = ("dei", "EntityCommonStockSharesOutstanding")


# ---------------------------------------------------------------------------
# The universe
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Name:
    """One universe member, with the evidence for its stratum recorded alongside it.

    ``market_cap_usd`` and ``quintile`` are the values *as measured on* ``selected_on``. They are
    not refreshed, and they are not read by the probe — they exist so a reader can check that the
    universe satisfies the criteria it claims to, without re-running the selection. A stratum
    asserted without its evidence is a stratum nobody can audit.
    """

    ticker: str
    cik: int
    name: str
    sic: int | None
    quintile: int  # 1 = largest, 5 = smallest
    market_cap_usd: int | None  # None where no cover-page share count was published
    first_filing: date | None  # for the sub-3y criterion
    note: str = ""


SELECTED_ON: Final = date(1970, 1, 1)  # replaced by select_universe()'s output

UNIVERSE: Final[tuple[Name, ...]] = ()
"""The pinned twenty. Empty until :func:`select_universe` has been run once.

Deliberately not populated with a plausible-looking slate. Every entry carries a market cap and a
SIC that have to be true on ``SELECTED_ON``, and inventing them would put unsourced numbers into
the file that decides whether this project's coverage claim is met — which is the failure the whole
design exists to prevent. Run the selector, paste its output here and into ``COVERAGE.md``.
"""


def _require_universe() -> tuple[Name, ...]:
    if not UNIVERSE:
        pytest.skip(
            "UNIVERSE is empty. Run `uv run python -m tests.coverage_probe select` to build it, "
            "then paste the output into UNIVERSE and docs/m2/COVERAGE.md. "
            "See docs/m2/05-testing.md §3.",
        )
    return UNIVERSE


@pytest.mark.spec
def test_the_universe_satisfies_its_own_criteria() -> None:
    """The stratification is asserted, not just described.

    ``docs/m2/README.md`` § One risk accepted names the failure this guards: a universe chosen so
    the criterion passes. Twenty large caps would measure ~97% and mean nothing. Encoding the
    criteria as assertions makes a convenient universe fail the build rather than produce a
    flattering number — which is the only mechanism that helps, since the person choosing the
    universe is also the person who wants it to pass.
    """
    universe = _require_universe()

    assert len(universe) == 20, f"universe has {len(universe)} names, not 20"
    assert len({n.cik for n in universe}) == 20, "duplicate CIK in the universe"
    assert not {n.cik for n in universe} & FIXTURE_CIKS, (
        "a fixture company is in the universe; the measurement would be in-sample against chains "
        "that were written while looking at it"
    )

    per_quintile = Counter(n.quintile for n in universe)
    assert set(per_quintile) == {1, 2, 3, 4, 5}, f"quintiles present: {sorted(per_quintile)}"
    assert all(count == 4 for count in per_quintile.values()), (
        f"not four per quintile: {dict(sorted(per_quintile.items()))}"
    )

    assert any(n.sic is not None and n.sic in BANK_SIC for n in universe), (
        "no bank or insurer (SIC 6000–6499) — §6.10's refusal path is unmeasured"
    )
    assert any(n.sic == REIT_SIC for n in universe), "no REIT (SIC 6798)"

    # `window` rather than `date(year - 3, month, day)`: the naive form raises on a Feb-29 pin
    # date, and floor-to-first-of-month is the project's existing, tested answer to exactly this
    # (domain/periods.py). Reusing it also means the cutoff here and the lookback window elsewhere
    # move together if that rule ever changes.
    cutoff, _ = lookback_window(3, as_of=SELECTED_ON)
    assert any(n.first_filing is not None and n.first_filing > cutoff for n in universe), (
        "no filer with under three years of history — the thin-coverage case is unmeasured, and "
        "it is the case a user is most likely to hit and least likely to be warned about"
    )


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Filer:
    """One filer's measurement."""

    ticker: str
    cik: int
    quintile: int
    annual_expected: int = 0
    quarterly_expected: int = 0
    spine_origin: str = "filings"
    # (metric, bucket) -> number of spine periods with at least one chain member present
    filled: Counter[tuple[str, str]] = field(default_factory=Counter)
    # qualified tag -> periods it appeared in, so the chain ORDER can be decided from data
    member_hits: Counter[str] = field(default_factory=Counter)
    absent: list[str] = field(default_factory=list)


def _spine(
    filings: Sequence[FilingRow],
    *,
    window: tuple[date, date],
) -> tuple[set[date], set[date]]:
    """Period ends the filer actually reported, per ``docs/m2/03-statements.md`` §2.

    Amendments collapse into the filing they amend, and an annual report date is also a quarterly
    spine entry — a filer files three 10-Qs a year and the fourth quarter's end appears only on the
    10-K, so a quarterly denominator built from 10-Qs alone reads 133% for any filer whose Q4s were
    derived.
    """
    start, end = window
    annual: set[date] = set()
    quarterly: set[date] = set()
    for row in filings:
        if row.report_date is None or not (start <= row.report_date <= end):
            continue
        form = row.form.removesuffix("/A")
        if form in ANNUAL_FORMS:
            annual.add(row.report_date)
            quarterly.add(row.report_date)
        elif form in QUARTERLY_FORMS:
            quarterly.add(row.report_date)
    return annual, quarterly


def _ends_present(facts: CompanyFacts, tag: str, *, taxonomy: str = "us-gaap") -> set[date]:
    """Period ends for which this tag has at least one fact. No dedup, no as-of — presence only."""
    return {row.period.end for row in facts.get(taxonomy, tag)}


def measure(
    *,
    ticker: str,
    cik: int,
    quintile: int,
    facts: CompanyFacts,
    filings: Sequence[FilingRow],
    window: tuple[date, date],
) -> Filer:
    """Per-metric, per-bucket presence against the period spine.

    Presence, not resolution: a metric counts as filled for a period if **any** member of its chain
    has a fact ending on that date. That is an upper bound on what the resolver will achieve, and
    the gap between this number and ``normalize``'s is itself worth knowing — it is facts the
    payload contains and the pipeline drops.
    """
    out = Filer(ticker=ticker, cik=cik, quintile=quintile)
    annual_ends, quarterly_ends = _spine(filings, window=window)

    if not annual_ends and not quarterly_ends:
        out.spine_origin = "observed"
        observed = {row.period.end for row in facts.all_facts()}
        annual_ends, quarterly_ends = observed, observed
        out.absent.append("no periodic filing in window; denominator is circular")

    out.annual_expected = len(annual_ends)
    out.quarterly_expected = len(quarterly_ends)

    for tier in (TIER1, TIER2):
        for metric, members in tier.items():
            covered_annual: set[date] = set()
            covered_quarterly: set[date] = set()
            for tag in members:
                ends = _ends_present(facts, tag)
                if ends:
                    out.member_hits[f"us-gaap:{tag}"] += len(ends)
                covered_annual |= ends & annual_ends
                covered_quarterly |= ends & quarterly_ends
            out.filled[metric, "annual"] = len(covered_annual)
            out.filled[metric, "quarterly"] = len(covered_quarterly)
            if not covered_annual and not covered_quarterly:
                out.absent.append(f"{metric}: no chain member tagged in any period")

    dei_ends = _ends_present(facts, DEI_SHARES[1], taxonomy=DEI_SHARES[0])
    out.filled["shares_cover", "annual"] = len(dei_ends & annual_ends)
    out.filled["shares_cover", "quarterly"] = len(dei_ends & quarterly_ends)
    if not dei_ends:
        out.absent.append("shares_cover: no dei section (no market cap)")

    return out


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _fetch_one(client: edgar.EdgarClient, cik: int) -> tuple[CompanyFacts, Sequence[FilingRow]]:
    subs = client.get(edgar.submissions_url(cik))
    ctx = SourceContext(url=subs.url, fetched_at=subs.fetched_at, cik=cik)
    _profile, filings, _files = parse_submissions(subs.body, source=ctx)

    cf = client.get(edgar.companyfacts_url(cik))
    facts = parse_companyfacts(
        cf.body,
        source=SourceContext(url=cf.url, fetched_at=cf.fetched_at, cik=cik),
    )
    return facts, filings


def _iter_filers(window: tuple[date, date]) -> Iterator[Filer]:
    settings = load_settings()
    cache = open_cache(settings)
    with edgar.EdgarClient(
        user_agent=settings.sec_user_agent,
        requests_per_second=settings.edgar_requests_per_second,
        cache=cache,
    ) as client:
        for entry in _require_universe():
            facts, filings = _fetch_one(client, entry.cik)
            yield measure(
                ticker=entry.ticker,
                cik=entry.cik,
                quintile=entry.quintile,
                facts=facts,
                filings=filings,
                window=window,
            )


def _tier_rate(filers: Sequence[Filer], tier: Mapping[str, tuple[str, ...]], bucket: str) -> float:
    filled = sum(f.filled[m, bucket] for f in filers for m in tier)
    key = "annual_expected" if bucket == "annual" else "quarterly_expected"
    expected = sum(getattr(f, key) for f in filers) * len(tier)
    return filled / expected if expected else float("nan")


@pytest.mark.spec
def test_coverage_across_the_universe(capsys: pytest.CaptureFixture[str]) -> None:
    """Prints the table ROADMAP M2's exit criterion is assessed against. Asserts nothing about 90%.

    **The threshold is deliberately not asserted here**, and that is the design rather than an
    omission. ``docs/m2/README.md`` § One risk accepted: §4.2's own counts say
    ``LongTermDebtNoncurrent`` is tagged by 1,532 of ~5,000 filers and call the chain "the weakest
    of the set", so a hard 90% gate on it will be met by adding a chain member that means something
    slightly different — moving the number without improving the data. A red build creates exactly
    that pressure. So the probe measures and reports; the decision about per-metric floors is made
    from the table, by a person, and lands in ROADMAP.
    """
    window = lookback_window(10, as_of=SELECTED_ON)
    filers = list(_iter_filers(window))

    lines = [
        "# M2 — Coverage measurement",
        "",
        f"Run: {datetime.now(UTC).date().isoformat()} · universe pinned {SELECTED_ON.isoformat()} "
        f"· window {window[0].isoformat()}..{window[1].isoformat()}",
        "",
        "| metric | tier | annual | quarterly |",
        "|---|---|---|---|",
    ]
    for tier_name, tier in (("1", TIER1), ("2", TIER2)):
        for metric in tier:
            a_fill = sum(f.filled[metric, "annual"] for f in filers)
            a_exp = sum(f.annual_expected for f in filers)
            q_fill = sum(f.filled[metric, "quarterly"] for f in filers)
            q_exp = sum(f.quarterly_expected for f in filers)
            a = f"{a_fill / a_exp:.1%}" if a_exp else "—"
            q = f"{q_fill / q_exp:.1%}" if q_exp else "—"
            lines.append(f"| {metric} | {tier_name} | {a} | {q} |")

    lines += [
        "",
        "| tier | annual | quarterly |",
        "|---|---|---|",
        f"| 1 (DCF) | {_tier_rate(filers, TIER1, 'annual'):.1%} "
        f"| {_tier_rate(filers, TIER1, 'quarterly'):.1%} |",
        f"| 2 (F/Z/M) | {_tier_rate(filers, TIER2, 'annual'):.1%} "
        f"| {_tier_rate(filers, TIER2, 'quarterly'):.1%} |",
        "",
        "## Chain member hit counts",
        "",
        "Periods each tag appeared in, across the universe. **This is the evidence that decides "
        "the tier-2 chain orderings** (`docs/m2/README.md` spec question 6) — order by this "
        "column, not by intuition.",
        "",
        "| tag | periods |",
        "|---|---|",
    ]
    totals: Counter[str] = Counter()
    for f in filers:
        totals.update(f.member_hits)
    lines += [f"| `{tag}` | {n} |" for tag, n in totals.most_common()]

    lines += ["", "## Per-filer absences", ""]
    for f in filers:
        for note in f.absent:
            lines.append(f"- **{f.ticker}** (Q{f.quintile}): {note}")

    report = "\n".join(lines) + "\n"
    with capsys.disabled():
        print(report)
    destination = os.environ.get(REPORT_OUT_VAR)
    if destination:
        Path(destination).write_text(report, encoding="utf-8")


# ---------------------------------------------------------------------------
# Universe selection — run once, by hand
# ---------------------------------------------------------------------------


def select_universe() -> str:
    """Build a stratified twenty and print it as a paste-ready ``UNIVERSE`` literal.

    Market cap is computed the way the rest of the project computes it — price × cover-page shares
    summed across classes (§4.3) — rather than fetched from a quote API, so the numbers in the
    universe are traceable by the same rule as every other number here.

    This is expensive: it needs a companyfacts pull per candidate. It is run **once**, and its
    output is pinned. Sampling at run time would make two coverage measurements incomparable, since
    a moved figure could be the chains improving or the sample changing.
    """
    raise NotImplementedError(
        "Selection needs a price provider pass over the NASDAQ listing. Implement when the "
        "measurement is run; the criteria it must satisfy are already asserted by "
        "test_the_universe_satisfies_its_own_criteria, so the output is checkable even though "
        "the procedure is manual.",
    )


if __name__ == "__main__":  # pragma: no cover
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "select":
        try:
            print(select_universe())
        except (NotImplementedError, ConfigError) as error:
            print(json.dumps({"error": str(error)}, indent=2))
            raise SystemExit(1) from error

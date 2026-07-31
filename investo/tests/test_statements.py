"""`FinancialHistory` assembly: provenance, gaps, the two absences, and the gates M2 must not enforce.

`docs/m2/03-statements.md` §§1 and 5 are normative. Four properties are asserted here and each of them
has a failure mode that produces a *report* rather than an error:

**Every figure traces to a filing.** README's first guarantee and DESIGN.md §3.2: *"if it cannot be
traced, it is not printed."* `Fact.source` is non-optional, so the violation test is a construction
that omits it — a happy-path walk over the series passes whether or not the field could be `None`.

**No value is interpolated or carried forward.** A gap in a series is data the filer did not report,
and the tempting repair — average the neighbours, or repeat the last value — produces a fact with no
`SourceRef` and a chart with no hole. There is no flag for it and no config option to enable it, so
the assertion is that a hole with values on *both* sides stays a hole.

**Thin data degrades coverage; it does not raise.** §6.10 refuses valuations for banks and REITs and
§5.1 refuses one below 12 quarters, and both decisions belong to M4 and M5 — "a refusal reached inside
normalization is a refusal with **no report attached**". §14 agrees in the exit-code taxonomy: exit 3
is "insufficient data, *report still written*". So `BANK`, `REIT` and `IPO` must each produce a
complete `FinancialHistory` with the misses named in the coverage report.

**Both payloads are optional and the two absences stay independent.** M1 already returns `None` for
each, `fetch.py` records both as absences, and README § 4 lists a missing `companyfacts` as a normal
outcome. They degrade differently — one costs the series, the other costs the denominator and the
identity — so a single "payload missing" path would tell the reader the wrong half is unreliable.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal
from typing import Any, Final

import pytest

from investo.domain.models import Fact, Metric
from investo.domain.periods import FiscalPeriod, PeriodKind
from investo.domain.provenance import Derivation, Provenance, SourceRef
from investo.ingest.edgar.submissions import CompanyProfile
from investo.normalize.statements import Bucket, SpineOrigin, build_history
from investo.normalize.tags import CHAINS
from tests.conftest import M2_WINDOW, company_facts, filing_rows, history, submissions

EVERY_FIXTURE: Final = [
    "AAPL.trimmed.json",
    "ARXS.json",
    "BADUNIT.trimmed.json",
    "BANK.trimmed.json",
    "IPO.trimmed.json",
    "NCI.trimmed.json",
    "NOPERIODIC.trimmed.json",
    "NOQ4.trimmed.json",
    "REIT.trimmed.json",
    "RESTATER.trimmed.json",
    "STUBYEAR.trimmed.json",
    "TIER2.trimmed.json",
    "YTDONLY.trimmed.json",
]
"""Every `companyfacts` fixture. The provenance walk has to hold on all of them, including the ones
whose interesting property is a miss — a metric that resolved nothing contributes no facts, so a
payload full of absences would satisfy the walk trivially and is why the fact count is asserted too."""

VALUATION_FLOOR_QUARTERS: Final = 12
"""DESIGN.md §5.1's threshold, named so the `IPO` assertion reads as a comparison against the gate
rather than as a bare integer. M2 records `quarters_available` and does not act on it."""


def _refs(source: Provenance) -> tuple[SourceRef, ...]:
    """Every leaf `SourceRef` behind a provenance, for either arm of the union.

    `Derivation.refs()` exists precisely so no consumer re-implements the walk, and `SourceRef` is
    already a leaf — so the dispatch lives here rather than in each assertion below.
    """
    return source.refs() if isinstance(source, Derivation) else (source,)


def _profile(*, cik: int, name: str, sic: int, sic_description: str) -> CompanyProfile:
    """A synthesized profile, for the fixtures whose SIC lives in `PROVENANCE.md` and not a payload.

    `docs/m2/05-testing.md` §8 records the gap: `BANK` and `REIT` have no submissions fixture, so the
    SIC half of §6.10 is not testable end to end in M2. Synthesizing the profile tests the half that
    *is* M2's — that `FinancialHistory` carries the SIC a gate will read — and leaves the join from a
    real payload to the fixture work already recorded as outstanding.
    """
    return CompanyProfile(
        cik=cik,
        name=name,
        sic=sic,
        sic_description=sic_description,
        fiscal_year_end="1231",
        tickers=("EXBK",),
        exchanges=("Nasdaq",),
    )


def _assign(target: object, name: str, value: object) -> None:
    """Set an attribute without the type checker or the linter objecting.

    The same helper `tests/test_config.py` carries, for the same reason: whether pyright rejects a
    literal `history.cik = 1` on a frozen dataclass is a property of the checker's version, and a
    `# pyright: ignore` that stops being necessary becomes its own lint failure. `setattr` with a
    variable name sidesteps both, and ruff's B010 only fires on a constant name.
    """
    setattr(target, name, value)


def _fact_without(**fields: Any) -> Fact:
    """Construct a `Fact` from a dict, so the omission under test survives to runtime.

    Spelling the missing argument literally makes basedpyright reject the file — which is the
    guarantee working at the type level, and is also why the *runtime* half needs a spelling the
    checker cannot pre-empt. `Any` rather than `object` because this boundary is genuinely dynamic;
    `test_documentation.py` takes the same escape for the same reason.
    """
    return Fact(**fields)


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize("fixture", EVERY_FIXTURE)
def test_every_fact_traces_to_a_ref(fixture: str) -> None:
    """§3.2: every figure names the accession, tag and fetch timestamp it came from.

    Walked over both buckets and every metric of every fixture, because the paths that could produce an
    untraceable fact are the *derived* ones — a Q4 from subtraction, a summed SG&A, liabilities from
    two balance-sheet tags — and each appears in only one or two payloads. A ref whose accession or
    URL were blank would still be a ref, so the fields the appendix prints are checked rather than the
    object's presence.

    The fact count is asserted too: a walk over a payload that resolved nothing passes vacuously, and
    several of these fixtures exist because of what they *miss*.
    """
    built = history(fixture)
    counted = 0

    for bucket in Bucket:
        for metric in CHAINS:
            for fact in built.series(metric, bucket):
                counted += 1
                refs = _refs(fact.source)
                assert refs, f"{metric} {fact.period.end} has no traceable source"
                for ref in refs:
                    assert isinstance(ref, SourceRef)
                    assert ref.accession.value
                    assert ref.url
                    assert ref.fetched_at.tzinfo is not None
                    assert ref.filed <= built.as_of

    assert counted > 0, "a fixture that resolves nothing cannot demonstrate the guarantee"


@pytest.mark.spec
def test_a_fact_cannot_be_constructed_without_a_provenance() -> None:
    """The guarantee stated as the violation it forbids.

    §3.2's rule is that an untraceable number is not printed, and the enforcement is that `Fact.source`
    has no default — so the omission is a `TypeError` at the construction site rather than a `None` that
    reaches the renderer and prints an em dash where a filing should be. The walk above passes either
    way, which is why this test exists beside it.
    """
    with pytest.raises(TypeError, match="source"):
        _ = _fact_without(
            metric=Metric.REVENUE,
            value=Decimal("1"),
            period=FiscalPeriod(end=date(2023, 12, 31), kind=PeriodKind.INSTANT),
            unit="USD",
        )

    source_field = next(field for field in dataclasses.fields(Fact) if field.name == "source")
    assert source_field.default is dataclasses.MISSING
    assert source_field.default_factory is dataclasses.MISSING


@pytest.mark.spec
def test_a_derived_fact_names_every_filing_behind_it() -> None:
    """A derived figure traces to *all* its inputs, not to one of them.

    §3.2's spec question 2: printing a computed number with one input's ref "would be worse than
    printing nothing, because it would look traced". `NCI`'s liabilities come from two balance-sheet
    tags and `NOQ4`'s fourth quarter from a year and three quarters, so the two shapes are asserted
    against the arithmetic that produced them rather than against a count written down here.
    """
    liabilities = history("NCI.trimmed.json").series(Metric.LIABILITIES, Bucket.ANNUAL)
    for fact in liabilities:
        assert isinstance(fact.source, Derivation)
        assert len(fact.source.refs()) == 2
        assert len({ref.tag for ref in fact.source.refs()}) == 2

    quarters = history("NOQ4.trimmed.json").series(Metric.REVENUE, Bucket.QUARTERLY)
    derived = [fact for fact in quarters if isinstance(fact.source, Derivation)]
    assert derived
    for fact in derived:
        assert len(_refs(fact.source)) == 4, "the year plus the three quarters subtracted from it"


# ---------------------------------------------------------------------------
# no interpolation
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_gap_stays_a_gap() -> None:
    """No value is ever interpolated or carried forward — asserted on a hole with data on both sides.

    `TIER2`'s long-term debt is the shape that invites the repair: `LongTermDebt` covers FY2021 and
    FY2023, the capital-lease concept covers FY2022 and FY2024, and the `debt_lease_scope` exclusivity
    group keeps only the majority member — so two of four years are genuinely absent while the years
    either side of the first hole are populated. Every tempting fix produces a number: the mean of the
    neighbours, the previous year repeated, or the losing tag's own value.

    All three are asserted against, and the series length is compared to the *winning tag's* fact count
    taken from the payload rather than to a literal, so the test states the rule — the series holds
    exactly what resolution produced and nothing more.
    """
    fixture = "TIER2.trimmed.json"
    filings = filing_rows(
        ("10-K", "2022-02-18", "2021-12-31"),
        ("10-K", "2023-02-17", "2022-12-31"),
        ("10-K", "2024-02-16", "2023-12-31"),
        ("10-K", "2025-02-14", "2024-12-31"),
    )
    built = history(fixture, filings=filings)
    payload = company_facts(fixture)
    winner = {fact.period.end: fact.value for fact in payload.get("us-gaap", "LongTermDebt")}
    loser = {
        fact.period.end: fact.value
        for fact in payload.get("us-gaap", "LongTermDebtAndCapitalLeaseObligations")
    }

    series = built.series(Metric.LONG_TERM_DEBT, Bucket.ANNUAL)
    coverage = built.coverage.annual[Metric.LONG_TERM_DEBT]
    present = [fact.period.end for fact in series]
    missing = sorted(set(built.coverage.spine.annual_ends) - set(present))

    assert present == sorted(winner)
    assert len(series) == len(winner), "no period is invented and none is dropped"
    assert coverage.expected == len(built.coverage.spine.annual_ends)
    assert coverage.filled == len(winner), "the gap is reported, not filled"
    assert missing, "the fixture must leave a hole for the rule to be about"
    assert min(present) < missing[0] < max(present), "a hole with data on both sides of it"

    values = [fact.value for fact in series]
    interpolated = (values[0] + values[-1]) / Decimal(2)
    assert interpolated not in values, "no neighbour mean"
    assert len(set(values)) == len(values), "no value carried forward"
    for end in missing:
        assert end not in {fact.period.end for fact in series}
        assert loser.get(end) not in values, "the losing tag's value is excluded, not substituted"


@pytest.mark.spec
def test_a_dropped_transition_period_leaves_a_hole_rather_than_a_short_year() -> None:
    """The same rule where the gap comes from bucketing rather than from resolution.

    `STUBYEAR`'s transition period is dropped as `OTHER`, which leaves the annual series without a
    figure for the months it covered. Putting it in the annual series would produce one short year that
    every growth rate reads as a collapse; splicing it into a neighbour would invent a period no filing
    describes. So the series holds only the periods that classify, and each retains the start date it
    was filed with.
    """
    built = history("STUBYEAR.trimmed.json")
    series = built.series(Metric.REVENUE, Bucket.ANNUAL)

    assert built.coverage.annual[Metric.REVENUE].dropped_other_bucket == 1
    for fact in series:
        assert fact.period.kind is PeriodKind.ANNUAL
        assert fact.period.days is not None
        assert 350 <= fact.period.days <= 380
    assert len({fact.period.start for fact in series}) == len(series)


# ---------------------------------------------------------------------------
# §6.10 and §5.1 — the gates M2 records and does not enforce
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_bank_yields_a_history_with_operating_income_absent() -> None:
    """§6.10 and §14: a bank produces a report with a blank, not an exception.

    `BANK.trimmed.json` has no `OperatingIncomeLoss` at all — the chain has a single member, so there
    is no fallback and the metric is absent for the whole series. §6.10's argument is that *"a blank
    space with an explanation beats a confident wrong number"*, and the explanation is a rendered
    section: a `normalize/` layer that raised or returned early would produce neither.

    So three things are asserted together — the populated series, the named miss, and the SIC the gate
    will read. Any one alone is satisfied by an implementation that got the other two wrong.
    """
    built = history(
        "BANK.trimmed.json",
        profile=_profile(
            cik=36104,
            name="Example Bancorp Inc.",
            sic=6022,
            sic_description="State Commercial Banks",
        ),
        filings=filing_rows(("10-K", "2019-02-20", "2018-12-31")),
    )

    for metric in (Metric.REVENUE, Metric.NET_INCOME, Metric.ASSETS, Metric.LIABILITIES):
        assert built.series(metric, Bucket.ANNUAL) != (), metric
        assert built.coverage.annual[metric].fill_rate == Decimal(1), metric

    assert not company_facts("BANK.trimmed.json").has("us-gaap", "OperatingIncomeLoss")
    for bucket in Bucket:
        assert built.series(Metric.OPERATING_INCOME, bucket) == ()
        coverage = built.coverage.for_bucket(bucket)[Metric.OPERATING_INCOME]
        assert coverage.filled == 0
        assert coverage.tags_used == (), "nothing resolved, so no tag is credited"
        assert coverage.fill_rate == Decimal(0), "measured and empty, not unmeasurable"

    assert built.sic == 6022
    assert 6000 <= (built.sic or 0) <= 6499, "§6.10's bank band, which M4 reads and M2 does not"
    assert built.sic_description is not None


@pytest.mark.spec
def test_reit_yields_a_history_with_operating_income_and_capex_absent() -> None:
    """The second §6.10 fixture, and the one with two misses rather than one.

    `REIT` has no operating income *and* no member of the four-deep capex chain, so it exercises the
    case where a metric with several fallbacks still resolves nothing — which is the outcome §4.2
    predicts for capex, where "under half of filers tag the preferred concept". A history is still
    produced, the SIC is carried for §6.10's REIT branch, and no finding claims a refusal, because M2
    has no refusal to claim.
    """
    built = history(
        "REIT.trimmed.json",
        profile=_profile(
            cik=1063761,
            name="Example Properties Trust",
            sic=6798,
            sic_description="Real Estate Investment Trusts",
        ),
        filings=filing_rows(("10-K", "2019-02-20", "2018-12-31")),
    )

    assert built.series(Metric.REVENUE, Bucket.ANNUAL) != ()
    assert built.series(Metric.NET_INCOME, Bucket.ANNUAL) != ()
    assert built.series(Metric.ASSETS, Bucket.ANNUAL) != ()
    for metric in (Metric.OPERATING_INCOME, Metric.CAPEX):
        assert built.series(metric, Bucket.ANNUAL) == ()
        assert built.coverage.annual[metric].filled == 0
    assert len(CHAINS[Metric.CAPEX].members) > 1, "the chain has fallbacks and they all missed"

    assert built.sic == 6798
    assert built.coverage.findings != ()


@pytest.mark.spec
def test_ipo_quarters_available_is_six() -> None:
    """§5.1's input, on the wrong side of the 12-quarter boundary, with no refusal attached.

    `IPO.trimmed.json` carries exactly six quarters and an entirely empty annual series. The assertion
    is on the **count** rather than on any downstream consequence, because the consequence does not
    exist yet — §5.1's floor is M5's, and a `normalize/` layer that omitted the valuation would be
    making a call it has no report to attach.

    Six is asserted against the payload's own distinct quarter ends, so it states the rule; the
    comparison against 12 is what records which side of the gate this fixture lands on.
    """
    built = history("IPO.trimmed.json")
    payload = company_facts("IPO.trimmed.json")
    filed_quarters = {
        fact.period.end
        for fact in payload.get("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax")
        if fact.period.kind is PeriodKind.QUARTER
    }

    assert built.quarters_available == len(filed_quarters)
    assert built.quarters_available == 6
    assert built.quarters_available < VALUATION_FLOOR_QUARTERS

    for metric in CHAINS:
        assert built.series(metric, Bucket.ANNUAL) == (), "no annual figure to subtract from"


@pytest.mark.spec
def test_quarters_available_counts_durations_not_instants() -> None:
    """§5.1's gate is quarters *of history*, and an instant is not a quarter of anything.

    An instant lands in the quarterly bucket whenever its date matches no annual calendar entry, and a
    cover-page share count dated three weeks after a quarter end is the common case. `AAPL`'s quarterly
    bucket holds three such instants beside one real quarter, so counting bucket entries would report
    four quarters of history where there is one — and on a longer payload that arithmetic is what pushes
    a filer over the 12-quarter threshold on facts that are not quarters.
    """
    built = history("AAPL.trimmed.json")
    quarterly = [fact for metric in CHAINS for fact in built.series(metric, Bucket.QUARTERLY)]
    instants = [fact for fact in quarterly if fact.period.kind is PeriodKind.INSTANT]
    durations = {fact.period.end for fact in quarterly if fact.period.kind is PeriodKind.QUARTER}

    assert instants, "the fixture must carry instants in the quarterly bucket"
    assert built.quarters_available == len(durations)
    assert built.quarters_available < len({fact.period.end for fact in quarterly})


# ---------------------------------------------------------------------------
# the two absences
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_absent_facts_yields_an_empty_history() -> None:
    """§1: `build_history(None, ...)` returns a history, and the spine is unaffected.

    `fetch.py` records "companyfacts: none published for CIK ..." as an **absence**, the run exits 0, and
    README § 4 lists it as a normal outcome of `facts`. So the object is still built: every metric
    absent, every coverage entry at `filled=0`, a `companyfacts_absent` finding, and — because the
    filings are a separate payload — a real `FILINGS` spine with a non-zero denominator.

    That last part is the discriminating one. `fill_rate == 0` rather than `None` is what says "this
    filer reported four periods and we tagged none of them", which is a different statement from
    `ARXS`'s "nothing was expected", and an implementation that short-circuited on `facts is None`
    would produce the second.
    """
    profile, filings = submissions("AAPL.json")
    built = build_history(
        None,
        ticker="AAPL",
        cik=320193,
        name="Apple Inc.",
        profile=profile,
        filings=filings,
        window=M2_WINDOW,
    )

    assert built.cik == 320193
    assert built.ticker == "AAPL"
    assert set(built.annual) == set(CHAINS), "a full table of dashes, not a missing table"
    assert set(built.quarterly) == set(CHAINS)
    for bucket in Bucket:
        for metric in CHAINS:
            assert built.series(metric, bucket) == ()
            assert built.coverage.for_bucket(bucket)[metric].filled == 0
            assert built.coverage.for_bucket(bucket)[metric].tags_used == ()

    assert built.coverage.spine.origin is SpineOrigin.FILINGS
    assert built.coverage.spine.annual_ends != ()
    assert built.coverage.annual[Metric.REVENUE].fill_rate == Decimal(0)
    assert len(built.coverage.findings_for("companyfacts_absent")) == 1
    assert built.quarters_available == 0
    assert built.restatements == ()
    assert built.market_cap is None


@pytest.mark.spec
def test_both_payloads_absent_yields_an_observed_spine_over_nothing() -> None:
    """§1's third row: the degenerate case still returns a history and still exits 0.

    With no facts and no filings there is nothing to measure and nothing to measure it against, so
    every fill rate is `None` — the one honest answer — and the spine is `OBSERVED` and empty. Both
    absences are reported separately, because a reader needs to know which payload was missing.
    """
    built = build_history(
        None,
        ticker="TEST",
        cik=1000000,
        name="Test Corp",
        profile=None,
        window=M2_WINDOW,
    )

    assert built.coverage.spine.is_empty
    assert built.coverage.spine.origin is SpineOrigin.OBSERVED
    assert {coverage.fill_rate for coverage in built.coverage.annual.values()} == {None}
    codes = {finding.code for finding in built.coverage.findings}
    assert {"companyfacts_absent", "submissions_absent", "spine_observed"} <= codes


@pytest.mark.spec
def test_absent_profile_keeps_cik_and_name() -> None:
    """§1: a 404 on submissions costs the metadata, never the company's identity.

    `cik` and `name` are separate arguments rather than read off `profile` precisely so this path
    works: a ticker that did not resolve exited 2 before `build_history` was reachable, and `TickerRow`
    carries a mixed-case name. Printing the CIK where a name belongs would be the alternative, on a
    cover page.

    The three metadata fields widen to `None` rather than taking an invented value — there is no honest
    fiscal year end for a filer that never stated one — and the spine falls back to `OBSERVED`, because
    the filings came from the payload that 404'd.
    """
    built = history("AAPL.trimmed.json", ticker="AAPL", cik=320193, name="Apple Inc.")

    assert built.cik == 320193
    assert built.name == "Apple Inc."
    assert built.ticker == "AAPL"
    assert built.sic is None
    assert built.sic_description is None
    assert built.fiscal_year_end is None
    assert built.coverage.spine.origin is SpineOrigin.OBSERVED
    assert len(built.coverage.findings_for("submissions_absent")) == 1
    assert built.series(Metric.REVENUE, Bucket.ANNUAL) != (), "the series are unaffected"


@pytest.mark.spec
def test_the_profile_name_wins_when_there_is_one() -> None:
    """§1: submissions is the display name's source, and the ticker file is the fallback.

    M1's rule is about `companyfacts.entityName` being EDGAR-conformed uppercase; the ticker file is
    not, so when the profile is absent its name is the best available. When the profile is present its
    name wins — otherwise the cover page's casing would depend on which argument the caller filled in.
    A blank profile name falls back rather than printing an empty cover page.
    """
    profile, filings = submissions("AAPL.json")

    from_profile = history(
        "AAPL.trimmed.json", profile=profile, filings=filings, name="WRONG FROM TICKER FILE"
    )
    assert from_profile.name == profile.name

    blank = history(
        "AAPL.trimmed.json", profile=dataclasses.replace(profile, name=""), name="Ticker Row Name"
    )
    assert blank.name == "Ticker Row Name"

    assert history("AAPL.trimmed.json", name="Ticker Row Name").name == "Ticker Row Name"


# ---------------------------------------------------------------------------
# what the object carries
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_as_of_defaults_to_the_window_end_rather_than_a_clock() -> None:
    """§1: `as_of=None` means "no filtering", and the recorded date comes from the window.

    Nothing under `normalize/` reads a clock — `test_layering` enforces that structurally, and this is
    the behavioural half: the resolved `as_of` is `window[1]`, which *is* the as-of date the command
    computed at its boundary. A `date.today()` here would make two runs either side of midnight differ
    and §11's determinism gate would report it as nondeterminism rather than as the design mistake it is.
    """
    built = history("RESTATER.trimmed.json")

    assert built.as_of == M2_WINDOW[1]
    assert built.window == M2_WINDOW


@pytest.mark.spec
def test_as_of_is_recorded_and_filters_the_series() -> None:
    """The point-in-time cut is both applied and reported, and one without the other is worse than
    neither.

    `RESTATER` carries four generations of one period. Applied without being recorded, the report shows
    a figure nobody can date; recorded without being applied, it shows today's number under a
    historical label — which is the lookahead leak §4.2(b) exists to prevent, printed as though it were
    point-in-time. Asserted through the surviving value's own `filed` date rather than against a
    literal.
    """
    cut = date(2021, 6, 30)
    built = history("RESTATER.trimmed.json", as_of=cut)
    series = built.series(Metric.REVENUE, Bucket.ANNUAL)

    assert built.as_of == cut
    assert len(series) == 1
    for fact in series:
        for ref in _refs(fact.source):
            assert ref.filed <= cut

    current = history("RESTATER.trimmed.json").series(Metric.REVENUE, Bucket.ANNUAL)
    assert current[0].value != series[0].value, "the cut has to change the answer"


def test_market_cap_is_carried_through_rather_than_recomputed() -> None:
    """§1: M1's figure, threaded through unchanged.

    It is a company-level number with no series and nowhere else to live that M3 can reach without also
    reaching into `FetchResult`. Asserted by identity: a `FinancialHistory` that rebuilt it would need a
    price and a share count, and normalization has neither — so the only way to produce one here would
    be to invent it.
    """
    computed = (Decimal("1234567890"), Derivation(rule="market_cap", inputs=()))
    built = history("AAPL.trimmed.json", market_cap=computed)

    assert built.market_cap is computed
    assert history("AAPL.trimmed.json").market_cap is None


@pytest.mark.spec
def test_the_history_is_frozen_and_its_series_are_tuples() -> None:
    """§1: a frozen dataclass over mutable dicts of mutable lists is frozen in name only.

    `report.json` and the determinism gate both need a series to be unable to change between being
    built and being serialized, and M4 receives this object and computes over it. The `tuple` half is
    the one a `dict[Metric, list[Fact]]` annotation would quietly lose, since nothing else in the suite
    would notice a list.
    """
    built = history("TIER2.trimmed.json")

    with pytest.raises(dataclasses.FrozenInstanceError):
        _assign(built, "cik", 1)

    for bucket in Bucket:
        for metric in CHAINS:
            assert isinstance(built.series(metric, bucket), tuple)
    assert isinstance(built.restatements, tuple)
    assert isinstance(built.coverage.findings, tuple)
    assert isinstance(built.coverage.spine.annual_ends, tuple)


def test_series_returns_an_empty_tuple_for_a_metric_nothing_resolved() -> None:
    """`series()` answers "no facts" rather than raising, because absence is the common case.

    DESIGN.md §4.2's whole argument is that most filers miss most of the long tail of tags. A `KeyError`
    would make every consumer guard the lookup, and the first one that forgot would crash the render on
    a filer that is merely thin.
    """
    built = history("AAPL.trimmed.json")

    assert built.series(Metric.CAPEX, Bucket.ANNUAL) == ()
    assert built.series(Metric.SBC, Bucket.QUARTERLY) == ()


@pytest.mark.spec
@pytest.mark.parametrize("fixture", EVERY_FIXTURE)
def test_every_metric_has_a_series_entry_in_both_buckets(fixture: str) -> None:
    """The series maps are complete over `CHAINS`, so `facts` prints a full table.

    A map holding only the metrics that resolved would make the printed table's height depend on the
    filer, and a reader comparing two companies would not see that the second one is missing rows. The
    absence is the information.
    """
    built = history(fixture)

    assert set(built.annual) == set(CHAINS)
    assert set(built.quarterly) == set(CHAINS)


def test_restatements_are_retained_across_metrics() -> None:
    """ROADMAP open question 10 stays answerable without a re-parse.

    Whether a restated series displays both versions is a *display* question M2 does not answer, and
    cannot be answered later at all if the superseded values were discarded. So the record is carried on
    the history rather than rebuilt, and it holds every generation regardless of whether the value moved
    — which is also the evidence that `as_of` filtering happened before dedup rather than after.
    """
    built = history("AAPL.trimmed.json")

    assert built.restatements != ()
    for record in built.restatements:
        assert record.metric in CHAINS
        assert record.superseded != ()
        assert all(filed <= built.as_of for filed, _, _ in record.superseded)


@pytest.mark.spec
@pytest.mark.parametrize("fixture", ["AAPL.trimmed.json", "TIER2.trimmed.json", "NCI.trimmed.json"])
def test_two_runs_over_one_payload_produce_an_equal_history(fixture: str) -> None:
    """§11 at M2's scope: the history is a function of its inputs.

    `report.json`'s byte-identical gate is `test_serialize`'s; this is the object-level precondition for
    it. Every value in the tree is a frozen dataclass, so equality is structural — and the failure this
    would catch is a set or dict iteration order leaking into a series, which is exactly what the
    partial-sort-key rule exists to prevent and what a single run cannot show.
    """
    assert history(fixture) == history(fixture)

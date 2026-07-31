"""`companyfacts` -> `RawFact` rows: the `Decimal` violation test, and the six live surprises.

`docs/m1/04-parsers.md` §2 lists six things a live payload contradicted. Five of them are absences
or nulls, which means the wrong implementation raises `KeyError` on a real filer and passes every
test written against a tidy fixture. `companyfacts/ARXS.json` is the untidy one, so it carries most
of this file.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from investo.domain.models import RawFact, cover_share_facts
from investo.domain.periods import PeriodKind
from investo.errors import ExitCode, UpstreamFetchError
from investo.ingest.edgar.companyfacts import CompanyFacts, parse_companyfacts
from investo.ingest.edgar.submissions import parse_submissions
from tests.conftest import context, fixture_json

REVENUE_TAG = "RevenueFromContractWithCustomerExcludingAssessedTax"
DECIMAL_FIXTURE_VALUE = "391035000000.01"
"""The value chosen because `Decimal(float(...))` renders it `391035000000.010009765625`."""


def _one(facts: CompanyFacts, taxonomy: str, tag: str) -> RawFact:
    rows = facts.get(taxonomy, tag)
    assert len(rows) == 1, f"{taxonomy}:{tag} should carry exactly one fact, got {len(rows)}"
    return rows[0]


# ---------------------------------------------------------------------------
# CLAUDE.md convention 8 — no money is ever a float
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_decimal_from_source_text(aapl_companyfacts: bytes) -> None:
    """`json.loads(..., parse_float=Decimal)` is called with the number's *source text*.

    Three assertions, and they fail under three different mistakes:

    - The exact round-trip fails if the hook is replaced by `Decimal(row["val"])`, because that
      converts a `float` that has already lost precision.
    - `not isinstance(value, float)` is the one that holds for **any** value, including one that is
      exactly representable in binary — where the round-trip passes and proves nothing. It is
      therefore the assertion that survives someone "simplifying" the hook away and choosing a
      tidier fixture value.
    - `facts_dropped == 0` closes the third door: `_to_decimal` rejects a `float` rather than
      converting it, so a removed hook drops the row instead of mangling it, and a test that only
      looked at the surviving facts would find nothing to complain about.
    """
    assert DECIMAL_FIXTURE_VALUE.encode() in aapl_companyfacts, "the fixture must carry the literal"

    facts = parse_companyfacts(aapl_companyfacts, source=context())
    annual = [
        fact for fact in facts.get("us-gaap", REVENUE_TAG) if fact.period.end == date(2019, 9, 28)
    ]
    assert len(annual) == 1
    value = annual[0].value

    assert value == Decimal(DECIMAL_FIXTURE_VALUE)
    assert str(value) == DECIMAL_FIXTURE_VALUE
    assert not isinstance(value, float)
    assert facts.facts_dropped == 0


# ---------------------------------------------------------------------------
# The taxonomy set is not hardcoded
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_unknown_taxonomy_preserved(arxs_companyfacts: bytes) -> None:
    """`ffd` was not anticipated, sorts *first*, and survives parsing.

    An allowlist of `dei`/`us-gaap`/`srt` would have dropped it silently — and would drop the next
    taxonomy SEC adds. Asserted on `taxonomies_present` as well as on the facts, because a parser
    could keep the rows and still report a fixed set, which is what M2's coverage report reads.
    """
    facts = parse_companyfacts(arxs_companyfacts, source=context())

    assert "ffd" in facts.taxonomies_present
    assert facts.has("ffd", "NetFeeAmt")
    assert _one(facts, "ffd", "NetFeeAmt").taxonomy == "ffd"


# ---------------------------------------------------------------------------
# The display name does not come from here
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_entity_name_not_used_as_display_name(
    arxs_companyfacts: bytes, arxs_submissions: bytes
) -> None:
    """`docs/m1/04-parsers.md` §2: the report's company name comes from `submissions.name`.

    The two endpoints disagree on casing for the same company, so without a stated rule the cover
    page's casing depends on which parser ran last. `entity_name` is retained for provenance only,
    and this test exists to record that the EDGAR-conformed form is *not* interchangeable with the
    display name — a fact that is easy to miss precisely because both strings look like a name.
    """
    facts = parse_companyfacts(arxs_companyfacts, source=context())
    profile, _rows, _files = parse_submissions(arxs_submissions, source=context())

    assert facts.entity_name == "ARXIS, INC."
    assert facts.entity_name == facts.entity_name.upper()
    assert profile.name == "Arxis, Inc."
    assert facts.entity_name != profile.name


@pytest.mark.spec
def test_identity_check_tolerates_name_casing(
    arxs_companyfacts: bytes, arxs_submissions: bytes
) -> None:
    """Company identity is checked on `cik`, never on name.

    The observation above is the proof that a name comparison cannot be the check: punctuation and
    casing differ legitimately between the two endpoints for filers whose CIK matches perfectly, so
    a name check would raise on correct data. Both payloads describe CIK 2093536 under two
    spellings of the name, and parsing them together must not raise — which is what this asserts by
    running the pair through both parsers and comparing the normalized integers.
    """
    facts = parse_companyfacts(arxs_companyfacts, source=context())
    profile, _rows, _files = parse_submissions(arxs_submissions, source=context())

    assert facts.cik == profile.cik == 2093536

    # And the same holds when `companyfacts` uses the mixed-case spelling instead: the CIK is what
    # decides, so flipping the name must change nothing.
    payload: Any = json.loads(arxs_companyfacts)
    payload["entityName"] = "Arxis, Inc."
    reversed_casing = parse_companyfacts(json.dumps(payload).encode(), source=context())
    assert reversed_casing.cik == profile.cik


# ---------------------------------------------------------------------------
# Instant facts, and the absent `start` key
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_instant_fact_has_no_start_key_and_classifies_as_instant(
    arxs_companyfacts: bytes,
) -> None:
    """`start` is *absent* on instant facts — the key is missing, not `null`.

    So `row.get("start")` is the only correct read, and `PeriodKind.INSTANT` is detected by key
    absence rather than a sentinel. The first assertion is on the fixture's own bytes: a fixture
    written with `"start": null` would exercise a different code path and this test would pass
    without proving anything.
    """
    raw: Any = json.loads(arxs_companyfacts)
    row = raw["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]
    assert "start" not in row

    fact = _one(parse_companyfacts(arxs_companyfacts, source=context()), "us-gaap", "Assets")
    assert fact.period.kind is PeriodKind.INSTANT
    assert fact.period.start is None
    assert fact.period.days is None


# ---------------------------------------------------------------------------
# fy / fp are carried and never grouped by
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_registration_statement_fact_carries_null_fy_and_fp(arxs_companyfacts: bytes) -> None:
    """`fy`/`fp` are `null` on registration-statement facts, and `form` is not a periodic report.

    Both matter because they invite filters: `int(row["fy"])` raises on this row, and any code that
    keeps only `10-K`/`10-Q` drops an `S-1/A` fact that SEC published deliberately.
    """
    fact = _one(parse_companyfacts(arxs_companyfacts, source=context()), "ffd", "NetFeeAmt")

    assert fact.filing_fy is None
    assert fact.filing_fp is None
    assert fact.source.form == "S-1/A"
    assert fact.frame == "CY2026Q1I", "`frame` is carried, and restricted to peer use"


@pytest.mark.spec
def test_period_is_derived_from_start_and_end_not_from_filing_fy(arxs_companyfacts: bytes) -> None:
    """DESIGN.md §4.2(a): `fy`/`fp` are the fiscal year of the containing *filing*, not the fact's.

    The fixture carries the trap at minimum size: a period spanning 2025-01-01..2025-03-31 tagged
    `fy: 2026, fp: "Q1"`, because it was reported in a filing made in the issuer's fiscal 2026.
    Grouping by `fy` puts a calendar-2025 quarter in 2026.

    The assertions are on the *derivation*: the period's own dates come from `start`/`end`, its kind
    comes from the day count, and `filing_fy` disagrees with the period's year — which is what makes
    this fixture an argument rather than a coincidence.
    """
    fact = _one(
        parse_companyfacts(arxs_companyfacts, source=context()), "us-gaap", "AccountsPayableCurrent"
    )

    assert fact.period.start == date(2025, 1, 1)
    assert fact.period.end == date(2025, 3, 31)
    assert fact.period.days == 90
    assert fact.period.kind is PeriodKind.QUARTER
    assert fact.filing_fy == 2026
    assert fact.filing_fp == "Q1"
    assert fact.period.end.year != fact.filing_fy


# ---------------------------------------------------------------------------
# Absences are not errors
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_missing_tag_is_not_an_error(aapl_companyfacts: bytes) -> None:
    """DESIGN.md §4.2: a missing tag is a coverage fact, not a failure.

    Hardcoding one tag per metric silently produces sparse data and a confidently wrong report, so
    the absence has to be reportable rather than fatal — `has()` says no and `get()` gives an empty
    tuple that a caller can iterate without a guard.
    """
    facts = parse_companyfacts(aapl_companyfacts, source=context())

    assert facts.has("us-gaap", "OperatingIncomeLoss") is False
    assert facts.get("us-gaap", "OperatingIncomeLoss") == ()
    assert ("us-gaap", "OperatingIncomeLoss") not in facts.tags_present


@pytest.mark.spec
def test_no_dei_section_means_no_cover_share_facts(arxs_companyfacts: bytes) -> None:
    """A NASDAQ filer can have no `dei` section at all, confirmed live.

    `dei:EntityCommonStockSharesOutstanding` is the only source for market cap, so this is the path
    that has to end in an absence rather than a `KeyError` — and emphatically not a zero. Asserted
    here at the parser boundary; `test_market_cap` asserts the arithmetic half.
    """
    facts = parse_companyfacts(arxs_companyfacts, source=context())

    assert "dei" not in facts.taxonomies_present
    assert cover_share_facts(facts.all_facts()) == ()


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_units_are_carried_verbatim(aapl_companyfacts: bytes, arxs_companyfacts: bytes) -> None:
    """The units-dict key, not normalized and not mapped.

    DESIGN.md §4.2 warns twice that unit differences are value differences, and §4.2(b) dedups by
    `(unit, start, end)` — so a normalized unit makes two different numbers look like a
    restatement. `USD-per-shares` is asserted absent because that spelling belongs to `frames` URLs
    only, and a parser that "helpfully" converted would break the dedup key.
    """
    aapl = parse_companyfacts(aapl_companyfacts, source=context())
    assert {fact.unit for fact in aapl.all_facts()} == {"USD", "shares", "USD/shares"}
    assert "USD-per-shares" not in {fact.unit for fact in aapl.all_facts()}

    arxs = parse_companyfacts(arxs_companyfacts, source=context())
    assert {fact.unit for fact in arxs.all_facts()} == {"USD", "pure"}


@pytest.mark.spec
def test_pure_unit_carries_a_decimal_value(arxs_companyfacts: bytes) -> None:
    """`pure` is confirmed live and carries non-integer values, so the parse hook fires on it
    too."""
    fact = _one(
        parse_companyfacts(arxs_companyfacts, source=context()), "us-gaap", "AccountsPayableCurrent"
    )
    assert fact.unit == "pure"
    assert fact.value == Decimal("0.367")
    assert not isinstance(fact.value, float)


# ---------------------------------------------------------------------------
# Keying and identifiers
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_cik_is_normalized_from_a_padded_string(arxs_companyfacts: bytes) -> None:
    """`"0002093536"` -> `2093536`. This endpoint pads; `company_tickers_exchange.json` does not.

    A cast rather than a normalization would leave a string where a URL builder re-pads it, and the
    resulting 404 reads as a delisted company.
    """
    assert b'"cik": "0002093536"' in arxs_companyfacts
    assert parse_companyfacts(arxs_companyfacts, source=context()).cik == 2093536


@pytest.mark.spec
def test_facts_are_keyed_by_taxonomy_and_tag(arxs_companyfacts: bytes) -> None:
    """`Assets` exists in more than one taxonomy, so a tag alone cannot be the key.

    M2's chains name `dei:` and `us-gaap:` tags side by side; a tag-keyed map would let one
    overwrite the other and the loser would be whichever taxonomy iterated last.
    """
    facts = parse_companyfacts(arxs_companyfacts, source=context())
    assert ("us-gaap", "Assets") in facts.tags_present
    assert all(isinstance(key, tuple) and len(key) == 2 for key in facts.tags_present)


def test_malformed_payload_is_exit_4() -> None:
    """A payload with no `facts` is a shape change, and a shape change is not an absence."""
    with pytest.raises(UpstreamFetchError) as caught:
        _ = parse_companyfacts(b'{"cik": "0000320193"}', source=context())
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE


def test_fixture_json_helper_reads_the_same_bytes(arxs_companyfacts: bytes) -> None:
    """Guards the two tests above that read the fixture's raw bytes rather than its parsed form.

    If `fixture_json` and the `arxs_companyfacts` fixture ever pointed at different files, those
    byte-level assertions would be checking a payload the parser never saw.
    """
    assert fixture_json("edgar", "companyfacts", "ARXS.json") == json.loads(arxs_companyfacts)

"""Forms 4, 13D/G and 13F: the signal filter, the amendment key, and two silent-empty traps.

DESIGN.md §6.8 gives the rules; `docs/m1/04-parsers.md` §7 gives the reasons. Three of the four
guarantees here fail *quietly* when broken — a derivative row summed with common shares, an
amendment counted twice, a namespaced 13F read as an empty portfolio — so each has a test that
produces the wrong answer rather than an exception.

No ownership fixtures have been collected (`tests/fixtures/edgar/PROVENANCE.md`), so the documents
are built here. Their shapes come from the element paths `ownership.py` reads.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from investo.domain.provenance import Accession
from investo.errors import ExitCode, UpstreamFetchError
from investo.ingest.edgar.ownership import (
    NOISE_CODES,
    OPEN_MARKET_CODES,
    STRUCTURED_13DG_FROM,
    dedup_amendments,
    parse_13dg,
    parse_13f,
    parse_form4,
)

FILED = date(2026, 5, 4)
ACCESSION = Accession.parse("0001140361-26-025999")
AMENDMENT = Accession.parse("0001140361-26-026500")

PLAN_FOOTNOTE = """  <footnotes>
    <footnote id="F1">Sale effected pursuant to a Rule 10b5-1 trading plan adopted 2025-11-14.
    </footnote>
  </footnotes>"""

DERIVATIVE = """    <derivativeTransaction>
      <securityTitle><value>Restricted Stock Unit</value></securityTitle>
      <transactionDate><value>2026-05-01</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500000</value></transactionShares>
      </transactionAmounts>
    </derivativeTransaction>"""


def _transaction(
    code: str,
    *,
    shares: str = "10000",
    price: str = "212.4400",
    when: str = "2026-05-01",
    disposed: str = "D",
) -> str:
    return f"""    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{when}</value></transactionDate>
      <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{disposed}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>"""


def _form4(
    *transactions: str,
    reporter: str = "COOK TIMOTHY D",
    footnotes: str = "",
    derivative: str = "",
) -> bytes:
    body = "\n".join(transactions)
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-05-01</periodOfReport>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001214156</rptOwnerCik>
      <rptOwnerName>{reporter}</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
{body}
  </nonDerivativeTable>
  <derivativeTable>
{derivative}
  </derivativeTable>
{footnotes}
</ownershipDocument>
""".encode()


THIRTEEN_G = b"""<?xml version="1.0"?>
<edgarSubmission>
  <formData>
    <coverPageHeader>
      <issuerName>Apple Inc.</issuerName>
    </coverPageHeader>
    <reportingPersonInfo>
      <reportingPersonName>THE VANGUARD GROUP</reportingPersonName>
      <aggregateAmountOwned>1,234,567,890</aggregateAmountOwned>
      <percentOfClass>8.4</percentOfClass>
    </reportingPersonInfo>
  </formData>
</edgarSubmission>
"""

THIRTEEN_F = b"""<?xml version="1.0"?>
<ns1:informationTable
    xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <ns1:infoTable>
    <ns1:nameOfIssuer>APPLE INC</ns1:nameOfIssuer>
    <ns1:titleOfClass>COM</ns1:titleOfClass>
    <ns1:cusip>037833100</ns1:cusip>
    <ns1:value>1234567</ns1:value>
    <ns1:shrsOrPrnAmt>
      <ns1:sshPrnamt>5,000</ns1:sshPrnamt>
      <ns1:sshPrnamtType>SH</ns1:sshPrnamtType>
    </ns1:shrsOrPrnAmt>
  </ns1:infoTable>
</ns1:informationTable>
"""
"""A 13F with an explicit namespace **prefix**, which is the point of the fixture.

Filers disagree about the prefix, so a namespace-qualified XPath works for some and silently returns
nothing for others — and "nothing" reads as an institution holding no position at all.
"""


# ---------------------------------------------------------------------------
# Form 4: which rows, and which carry signal
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_only_non_derivative_transactions_are_read() -> None:
    """Derivative rows are option grants and exercises, which §6.8 classifies as noise.

    And worse than noise: a derivative row's `shares` is a *contract* count, so summing it with a
    common-share count produces a number that means nothing — 510,000 shares "sold" where 10,000
    were. The fixture puts a 500,000-unit derivative row alongside a 10,000-share sale so the wrong
    implementation produces a specific wrong total rather than a crash.
    """
    transactions = parse_form4(
        _form4(_transaction("S"), derivative=DERIVATIVE), accession=ACCESSION, filed=FILED
    )

    assert len(transactions) == 1
    assert transactions[0].shares == Decimal("10000")
    assert transactions[0].code == "S"


@pytest.mark.spec
@pytest.mark.parametrize("code", ["P", "S"])
def test_open_market_codes_carry_signal(code: str) -> None:
    """`P` and `S` are the open-market codes, and the only ones §6.8 treats as information."""
    transactions = parse_form4(_form4(_transaction(code)), accession=ACCESSION, filed=FILED)

    assert transactions[0].is_open_market is True
    assert transactions[0].carries_signal is True
    assert code in OPEN_MARKET_CODES


@pytest.mark.spec
@pytest.mark.parametrize("code", ["A", "M", "F", "G"])
def test_grant_exercise_withholding_and_gift_codes_carry_no_signal(code: str) -> None:
    """Grants, exercises, tax withholding and gifts are noise — and the sell-side ones look like
    sales.

    An `F` is shares withheld to pay tax on a vesting; it is a disposition the insider did not
    choose, and counting it as a sale turns every vest date into an insider-selling cluster. That
    is the false positive §6.8's filter exists to prevent, so each code gets its own assertion
    rather than one test over "not P/S".
    """
    transactions = parse_form4(_form4(_transaction(code)), accession=ACCESSION, filed=FILED)

    assert transactions[0].is_open_market is False
    assert transactions[0].carries_signal is False
    assert code in NOISE_CODES


@pytest.mark.spec
def test_a_10b5_1_footnote_flags_the_row_and_removes_it_from_the_signal() -> None:
    """A sale scheduled a year ago says nothing about what the insider thinks today.

    So the row is kept and flagged rather than dropped — the trade happened and the position
    changed — but `carries_signal` goes false. Both halves are asserted, because an implementation
    that dropped the row would satisfy "excluded from the signal" and lose the transaction.
    """
    planned = parse_form4(
        _form4(_transaction("S"), footnotes=PLAN_FOOTNOTE), accession=ACCESSION, filed=FILED
    )
    unplanned = parse_form4(_form4(_transaction("S")), accession=ACCESSION, filed=FILED)

    assert planned[0].planned_10b5_1 is True
    assert planned[0].is_open_market is True
    assert planned[0].carries_signal is False
    assert unplanned[0].planned_10b5_1 is False
    assert unplanned[0].carries_signal is True


@pytest.mark.spec
def test_form4_values_are_decimal_and_the_reporter_and_role_survive() -> None:
    """XML is text, so there is no `float` on this path at all — and no excuse for one.

    `acquired` is read from SEC's own flag rather than inferred from the code: an `S` carrying an
    `A` flag is a payload worth seeing rather than one to silently reclassify.
    """
    transactions = parse_form4(
        _form4(_transaction("S", shares="12345", price="212.4400")),
        accession=ACCESSION,
        filed=FILED,
    )
    row = transactions[0]

    assert row.shares == Decimal("12345")
    assert row.price_per_share == Decimal("212.4400")
    assert not isinstance(row.shares, float)
    assert not isinstance(row.price_per_share, float)
    assert row.reporter == "COOK TIMOTHY D"
    assert row.reporter_is_officer is True
    assert row.reporter_is_director is False
    assert row.acquired is False
    assert row.transaction_date == date(2026, 5, 1)


def test_malformed_form4_xml_is_exit_4() -> None:
    """A document that is not well-formed XML is an upstream failure, not an empty filing."""
    with pytest.raises(UpstreamFetchError) as caught:
        _ = parse_form4(b"<ownershipDocument><nonDeriv", accession=ACCESSION, filed=FILED)
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE


# ---------------------------------------------------------------------------
# Amendments
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_dedup_amendments_keys_on_reporter_date_and_code_and_the_newest_wins() -> None:
    """The key is `(reporter, transaction date, code)` — **not** shares or price.

    An amendment exists *because* one of those was wrong. Including them in the key keeps both the
    error and the correction, and the pair double-counts the trade: 22,500 shares sold where 12,500
    were. That is the specific wrong number this test produces under the wrong key, which is why the
    surviving row's `shares` is asserted rather than only the row count.
    """
    original = parse_form4(
        _form4(_transaction("S", shares="10000", price="212.4400")),
        accession=ACCESSION,
        filed=date(2026, 5, 4),
    )
    amended = parse_form4(
        _form4(_transaction("S", shares="12500", price="213.1000")),
        accession=AMENDMENT,
        filed=date(2026, 5, 11),
    )

    deduped = dedup_amendments((*original, *amended))

    assert len(deduped) == 1
    assert deduped[0].shares == Decimal("12500")
    assert deduped[0].price_per_share == Decimal("213.1000")
    assert deduped[0].filed == date(2026, 5, 11)
    assert deduped[0].accession == AMENDMENT


@pytest.mark.spec
def test_dedup_keeps_rows_that_differ_in_any_key_component() -> None:
    """The other direction: the key has to be narrow enough to keep genuinely different trades.

    A purchase and a sale on one day by one person are two trades; two sales on different days are
    two trades; two people selling are two trades. A key that collapsed any of those would
    under-count exactly the clustering §6.8 is looking for.
    """
    same_day_sale = parse_form4(_form4(_transaction("S")), accession=ACCESSION, filed=FILED)
    same_day_buy = parse_form4(_form4(_transaction("P")), accession=ACCESSION, filed=FILED)
    other_day = parse_form4(
        _form4(_transaction("S", when="2026-04-28")), accession=ACCESSION, filed=FILED
    )
    other_person = parse_form4(
        _form4(_transaction("S"), reporter="WILLIAMS JEFFREY E"), accession=ACCESSION, filed=FILED
    )

    combined = (*same_day_sale, *same_day_buy, *other_day, *other_person)
    assert len(dedup_amendments(combined)) == 4


# ---------------------------------------------------------------------------
# 13D/G, and the structured-era boundary
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_13dg_before_the_structured_era_returns_nothing_without_parsing() -> None:
    """Filings before 2024-12-18 are narrative HTML, and are counted rather than parsed.

    Proven by handing the parser a body that is not XML at all: if it attempted a parse it would
    raise, so returning `()` is evidence that the date check ran first. That matters because a
    parse failure on a pre-boundary filing would be indistinguishable from a parser bug on a
    post-boundary one — which is exactly why the date is hardcoded rather than inferred from
    whether parsing worked.
    """
    before = STRUCTURED_13DG_FROM - timedelta(days=1)
    narrative = b"<html><body>not xml at all"

    assert parse_13dg(narrative, accession=ACCESSION, filed=before, form="SC 13G") == ()


@pytest.mark.spec
def test_13dg_on_the_structured_era_boundary_is_parsed() -> None:
    """The boundary is inclusive: a filing dated exactly 2024-12-18 is structured XML.

    A `>` where `>=` belongs silently drops the first day of the structured era, and every test that
    probes 2023 and 2026 passes anyway.
    """
    owners = parse_13dg(THIRTEEN_G, accession=ACCESSION, filed=STRUCTURED_13DG_FROM, form="SC 13G")

    assert len(owners) == 1
    assert owners[0].owner == "THE VANGUARD GROUP"
    assert owners[0].shares == Decimal("1234567890")
    assert owners[0].percent_of_class == Decimal("8.4")
    assert not isinstance(owners[0].shares, float)


@pytest.mark.spec
def test_13dg_on_or_after_the_boundary_really_does_parse() -> None:
    """The complement of the two tests above, and the one that makes them mean something.

    On or after the boundary a malformed body must *raise*. Without this, an implementation that
    returned `()` unconditionally would pass both of the preceding tests.
    """
    with pytest.raises(UpstreamFetchError):
        _ = parse_13dg(
            b"<html><body>not xml at all",
            accession=ACCESSION,
            filed=STRUCTURED_13DG_FROM,
            form="SC 13G",
        )


# ---------------------------------------------------------------------------
# 13F
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_13f_reads_namespaced_info_tables_by_local_name() -> None:
    """A namespace-qualified path returns nothing for some filers, and nothing reads as no holdings.

    So the match is on the element's *local* name. The fixture carries an explicit `ns1:` prefix
    precisely because that is the case a hardcoded namespace or an unqualified `find("infoTable")`
    both miss — and the failure is an empty portfolio for a manager who filed one.
    """
    positions = parse_13f(THIRTEEN_F, accession=ACCESSION, filed=FILED, manager="BERKSHIRE")

    assert len(positions) == 1
    assert positions[0].issuer == "APPLE INC"
    assert positions[0].cusip == "037833100"
    assert positions[0].value == Decimal("1234567")
    assert positions[0].shares == Decimal("5000")
    assert positions[0].manager == "BERKSHIRE"
    assert not isinstance(positions[0].value, float)


def test_malformed_13f_xml_is_exit_4() -> None:
    """A truncated information table is an upstream failure, not a manager who sold everything."""
    with pytest.raises(UpstreamFetchError) as caught:
        _ = parse_13f(b"<informationTable><infoTable>", accession=ACCESSION, filed=FILED)
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE

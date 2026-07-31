"""DEF 14A: one structured numeric source, and a rule about everything else.

`docs/m1/04-parsers.md` §8: Pay Versus Performance is inline-XBRL tagged via the ECD taxonomy and is
**the only numeric extraction in a proxy**. Everything else — the Summary Compensation Table, the
CD&A, the pay ratio, audit fees — is untagged narrative, handed on as text for M6 and never turned
into a number. That is a rule rather than a limitation: a compensation figure read out of a table by
a regex has no provenance a reader could check, which DESIGN.md §3.2 forbids.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from investo.domain.provenance import Accession
from investo.ingest.edgar.documents import normalize_text
from investo.ingest.edgar.proxy import ECD_TAXONOMY_PREFIX, parse_proxy
from tests.conftest import FETCHED_AT, context

ACCESSION = Accession.parse("0000320193-25-000055")
FILED = date(2025, 4, 1)

NARRATIVE_FIGURE = Decimal("12345678")
"""A number that appears only in prose. No fact may be produced from it."""

PROXY = b"""<html><body>
<p>Compensation Discussion and Analysis</p>
<p>Our named executive officers received total compensation of $12,345,678 in fiscal 2025, and
the median employee was paid $68,254.</p>
<h2>Pay Versus Performance</h2>
<table>
<tr>
  <td><ix:nonFraction name="ecd:PeoTotalCompAmt" contextRef="D2025" unitRef="usd"
      decimals="0" scale="0">14,250,000</ix:nonFraction></td>
  <td><ix:nonFraction name="ecd:PeoActuallyPaidCompAmt" contextRef="D2025"
      unitRef="usd">(1,234)</ix:nonFraction></td>
  <td><ix:nonFraction name="ecd:NonPeoNeoAvgTotalCompAmt" contextRef="D2025"
      unitRef="usd">$4,500</ix:nonFraction></td>
  <td><ix:nonFraction name="us-gaap:Revenues" contextRef="D2025"
      unitRef="usd">999,999</ix:nonFraction></td>
</tr>
</table>
</body></html>
"""
"""A proxy carrying four iXBRL facts, three `ecd:` and one not.

The non-`ecd:` fact is what makes "only ECD facts" testable — without it, an implementation that
extracted every iXBRL fact in the document would pass. The parenthesised value and the currency
symbol are the two renderings a compensation table actually uses.
"""

NO_IXBRL = b"""<html><body>
<p>Compensation Discussion and Analysis</p>
<p>Total compensation for our chief executive officer was $12,345,678.</p>
</body></html>
"""

ONLY_OTHER_TAXONOMY = b"""<html><body>
<p><ix:nonFraction name="us-gaap:Revenues" contextRef="D2025">999,999</ix:nonFraction></p>
</body></html>
"""


def _values(body: bytes) -> list[Decimal]:
    document = parse_proxy(body, source=context(), accession=ACCESSION, filed=FILED)
    return [fact.value for fact in document.pvp_facts]


# ---------------------------------------------------------------------------
# Only ECD facts
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_only_ecd_facts_are_extracted() -> None:
    """Item 402(v) is the ECD taxonomy, and the rest of a proxy's iXBRL is not this parser's.

    A proxy carries iXBRL for the cover page and sometimes for other schedules. Taking every fact
    would put numbers with no relationship to compensation into the pay-versus-performance table,
    and they would look tagged and authoritative because they were.
    """
    document = parse_proxy(PROXY, source=context(), accession=ACCESSION, filed=FILED)
    tags = [fact.tag for fact in document.pvp_facts]

    assert all(fact.taxonomy == ECD_TAXONOMY_PREFIX for fact in document.pvp_facts)
    assert tags == ["PeoTotalCompAmt", "PeoActuallyPaidCompAmt", "NonPeoNeoAvgTotalCompAmt"]
    assert Decimal("999999") not in _values(PROXY), "the us-gaap fact must not be collected"


@pytest.mark.spec
def test_no_number_is_produced_from_narrative() -> None:
    """`proxy.py` produces **no numbers from narrative**, and this is the assertion of that rule.

    `$12,345,678` sits in a sentence in the same document as the tagged facts. A regex over the
    text would find it, and the resulting figure would have no accession, no tag and no way for a
    reader to check it — so it would be a number the report could print and not trace, which
    DESIGN.md §3.2 rules out.
    """
    document = parse_proxy(PROXY, source=context(), accession=ACCESSION, filed=FILED)

    assert str(NARRATIVE_FIGURE) in document.text.replace(",", "")
    assert NARRATIVE_FIGURE not in _values(PROXY)
    assert len(document.pvp_facts) == 3


# ---------------------------------------------------------------------------
# Value rendering
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_a_parenthesised_value_is_negative() -> None:
    """`(1,234)` is minus 1,234 in every compensation table ever printed.

    Reading it as positive flips the sign on every negative adjustment in the table — and the
    pay-versus-performance table is *made* of adjustments, so the compensation actually paid comes
    out higher than reported rather than lower. The sign is the whole content of the disclosure.
    """
    document = parse_proxy(PROXY, source=context(), accession=ACCESSION, filed=FILED)
    by_tag = {fact.tag: fact.value for fact in document.pvp_facts}

    assert by_tag["PeoActuallyPaidCompAmt"] == Decimal("-1234")
    assert by_tag["PeoActuallyPaidCompAmt"] < 0


@pytest.mark.spec
def test_commas_and_currency_symbols_are_stripped() -> None:
    """The rendered text carries thousands separators and a currency symbol; the value carries
    neither.

    `Decimal("14,250,000")` raises and `Decimal("$4,500")` raises, so a parser that passed the
    displayed string straight through would drop both facts — as an absence, which is
    indistinguishable from a proxy that predates the tagging requirement.
    """
    document = parse_proxy(PROXY, source=context(), accession=ACCESSION, filed=FILED)
    by_tag = {fact.tag: fact.value for fact in document.pvp_facts}

    assert by_tag["PeoTotalCompAmt"] == Decimal("14250000")
    assert by_tag["NonPeoNeoAvgTotalCompAmt"] == Decimal("4500")
    for fact in document.pvp_facts:
        assert isinstance(fact.value, Decimal)
        assert not isinstance(fact.value, float)


# ---------------------------------------------------------------------------
# ixbrl_present, and the absence it distinguishes
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_ixbrl_present_distinguishes_no_ecd_facts_from_no_ixbrl_at_all() -> None:
    """Two different problems with two different fixes, so they get two different reports.

    "No `ecd` facts in a document that has iXBRL" is a proxy predating the 2022-12-16 requirement,
    or one whose table this parser does not recognise. "A document we could not read as iXBRL" is
    an extraction bug. A single empty result would conflate them, and the fetch summary would say
    "absent" for both.
    """
    with_facts = parse_proxy(PROXY, source=context(), accession=ACCESSION, filed=FILED)
    other_only = parse_proxy(
        ONLY_OTHER_TAXONOMY, source=context(), accession=ACCESSION, filed=FILED
    )
    none_at_all = parse_proxy(NO_IXBRL, source=context(), accession=ACCESSION, filed=FILED)

    assert with_facts.ixbrl_present is True
    assert with_facts.pvp_facts

    assert other_only.ixbrl_present is True
    assert other_only.pvp_facts == ()

    assert none_at_all.ixbrl_present is False
    assert none_at_all.pvp_facts == ()


@pytest.mark.spec
def test_a_proxy_with_no_ecd_facts_is_an_absence_not_a_failure() -> None:
    """A proxy older than the tagging requirement legitimately has none, so an empty tuple is
    correct.

    Raising here would turn a company that filed a perfectly ordinary 2021 proxy into a failed run.
    """
    document = parse_proxy(NO_IXBRL, source=context(), accession=ACCESSION, filed=FILED)

    assert document.pvp_facts == ()
    assert document.text
    assert document.accession == ACCESSION
    assert document.filed == FILED


# ---------------------------------------------------------------------------
# Text and provenance
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_text_goes_through_the_same_normalizer_documents_uses() -> None:
    """§7.3's citation verifier searches this text, so it must be the same normalization.

    Asserted as a fixed point — `normalize_text(text) == text` — rather than by comparing against a
    hand-written expected string, because that is the property that matters: a quote verified under
    `documents.normalize_text` has to be findable here, and any second normalizer would show up as
    text that is not already normalized.
    """
    document = parse_proxy(PROXY, source=context(), accession=ACCESSION, filed=FILED)

    assert normalize_text(document.text) == document.text
    assert "Compensation Discussion and Analysis" in document.text


@pytest.mark.spec
def test_each_fact_carries_provenance_back_to_the_filing() -> None:
    """DESIGN.md §3.2: a figure that cannot be traced is not printed, and these are figures.

    The qualified tag is what §9.1's appendix prints, and `form` is fixed rather than guessed so the
    appendix line reads as a proxy rather than as an untyped document.
    """
    document = parse_proxy(PROXY, source=context(), accession=ACCESSION, filed=FILED)
    fact = document.pvp_facts[0]

    assert fact.source.accession == ACCESSION
    assert fact.source.form == "DEF 14A"
    assert fact.source.filed == FILED
    assert fact.source.qualified_tag == "ecd:PeoTotalCompAmt"
    assert fact.source.fetched_at == FETCHED_AT
    assert fact.context == "D2025"

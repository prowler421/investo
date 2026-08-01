"""Every URL and identifier transform, against a hand-written expected literal.

**This is the one place in the suite where asserting a literal is right**, and
``docs/m1/03-edgar-client.md`` §6 says why: the literal *is* the specification. There is no
derivation to assert instead — a test that rebuilt the expected URL from the same padding rule as
the code would pass under a wrong rule that both sides agreed on, which is the exact failure these
functions exist to prevent.

The failure mode is uniform and quiet. Every one of these transforms, gotten wrong, produces a 404,
and a 404 from EDGAR is indistinguishable from a company that never filed. ROADMAP M1 names it as
one of the milestone's two risks. So the literals below were written by reading SEC's own documented
URL shapes, not by running the code and pasting the output.

The boundary that would otherwise be found in production: **a CIK below 1,000,000 pads to ten digits
on ``data.sec.gov`` and does not pad in ``/Archives/``.** Apple is such a CIK, so the default
fixture exercises it — and CIK 1 and a ten-digit CIK each get their own assertion at the two
extremes.
"""

from __future__ import annotations

import pytest

from investo.domain.provenance import Accession
from investo.ingest.edgar.client import (
    archives_cik,
    archives_doc_url,
    cik_path,
    companyfacts_url,
    frames_unit,
    frames_url,
    ownership_doc,
    submissions_page_url,
    submissions_url,
    tickers_exchange_url,
)

APPLE = 320193
ACCESSION = Accession.parse("0000320193-25-000079")


# ---------------------------------------------------------------------------
# The two CIK spellings
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize(
    ("cik", "padded", "unpadded"),
    [
        (APPLE, "CIK0000320193", "320193"),
        (1, "CIK0000000001", "1"),
        (1234567890, "CIK1234567890", "1234567890"),
        (999999, "CIK0000999999", "999999"),
        (1000000, "CIK0001000000", "1000000"),
    ],
    ids=["apple", "cik-1", "ten-digit", "just-under-a-million", "exactly-a-million"],
)
def test_the_two_cik_spellings(cik: int, padded: str, unpadded: str) -> None:
    """§6: ``data.sec.gov`` wants ``CIK`` plus a ten-digit pad; ``/Archives/`` wants bare decimal.

    Both spellings for each CIK in one assertion, because the mistake is never "I forgot how to pad"
    — it is using the padded form in the path that wants the bare one. The two rows either side of
    1,000,000 are where a "pad only if short" implementation would diverge.
    """
    assert cik_path(cik) == padded
    assert archives_cik(cik) == unpadded


@pytest.mark.spec
def test_a_ten_digit_cik_is_not_padded_further() -> None:
    """The upper boundary: ten digits already, so the pad is a no-op.

    ``f"CIK{cik:011d}"`` would satisfy every other test here and 404 on every filer with a modern
    CIK, which is most of them.
    """
    assert len(cik_path(1234567890)) == len("CIK") + 10


# ---------------------------------------------------------------------------
# The builders
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_companyfacts_url() -> None:
    """All XBRL facts for one company: ``data.sec.gov``, padded CIK, ``.json``."""
    expected = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    assert companyfacts_url(APPLE) == expected


@pytest.mark.spec
def test_submissions_url() -> None:
    """Company metadata plus ``filings.recent`` — note there is no ``/api/`` segment here.

    ``submissions`` sits at the host root while ``companyfacts`` is under ``/api/xbrl/``, which is
    the kind of asymmetry that gets "tidied" into a shared base path and then 404s.
    """
    assert submissions_url(APPLE) == "https://data.sec.gov/submissions/CIK0000320193.json"


@pytest.mark.spec
def test_submissions_page_url_uses_the_name_sec_gave() -> None:
    """An overflow page is addressed by the ``filings.files[].name`` SEC published.

    Composed from a CIK and an index it would be a guess, and it would 404 on the first filer whose
    pages are numbered differently — which is unobservable until it happens, because the missing
    page just means missing history.
    """
    name = "CIK0000320193-submissions-001.json"
    expected = "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json"
    assert submissions_page_url(name) == expected


@pytest.mark.spec
def test_frames_url() -> None:
    """One cross-company frame: taxonomy, tag, unit and period, in that order.

    The unit segment is the ``-per-`` spelling; see :func:`test_frames_unit` for the pair.
    """
    expected = "https://data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2025Q1.json"
    assert frames_url("us-gaap", "Revenues", "USD", "CY2025Q1") == expected


@pytest.mark.spec
def test_archives_doc_url_uses_the_unpadded_cik_and_the_undashed_accession() -> None:
    """Both transforms at once, and each is the opposite of what ``data.sec.gov`` wants.

    §6 calls this the pairing ROADMAP M1 names as a risk. The literal is written out in full because
    the two halves fail independently: a padded CIK 404s, and a dashed accession directory 404s, and
    neither says which one was wrong.
    """
    expected = "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250628.htm"
    assert archives_doc_url(APPLE, ACCESSION, "aapl-20250628.htm") == expected


@pytest.mark.spec
def test_tickers_exchange_url() -> None:
    """The ticker-to-CIK-and-exchange file, on ``www`` rather than ``data``.

    ``company_tickers.json`` is deliberately not used: two lookup paths for the same question is how
    a NASDAQ filter comes to be bypassed, and only this file carries the exchange.
    """
    assert tickers_exchange_url() == "https://www.sec.gov/files/company_tickers_exchange.json"


@pytest.mark.spec
@pytest.mark.parametrize(
    ("companyfacts_spelling", "frames_spelling"),
    [
        ("USD/shares", "USD-per-shares"),
        ("USD", "USD"),
        ("shares", "shares"),
        ("pure", "pure"),
    ],
)
def test_frames_unit(companyfacts_spelling: str, frames_spelling: str) -> None:
    """§6 rows 6 and 7: one unit, two spellings, two endpoints.

    Mixing them up is a 404 in one direction and a ``KeyError`` in the other, which is why the pair
    has one implementation. The three unchanged rows matter as much as the converted one: a blanket
    ``replace("/", "-per-")`` applied to a unit that has no slash must be a no-op.
    """
    assert frames_unit(companyfacts_spelling) == frames_spelling


# ---------------------------------------------------------------------------
# ownership_doc
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize("form", ["3", "4", "5", "3/A", "4/A", "5/A", " 4 ", "4/a"])
def test_ownership_doc_strips_xsl_prefix(form: str) -> None:
    """Forms 3/4/5 never fetch the XSL viewer path (``docs/m1/06-testing.md`` §4).

    Every Form 3 and Form 4 row in the observed ``submissions`` payload carries ``primaryDocument =
    "xslF345X06/ownership.xml"`` — an XSL-*rendered* viewer path, not the raw XML. Using it verbatim
    fetches a styled HTML document where ``ownership.py`` expects Form 4 XML, and the failure then
    looks like a parser bug rather than a URL bug.

    The amendments are here because ``"4/A"`` is a real form value and a naive ``form in
    {"3","4","5"}`` check misses all of them — which would leave every amended Form 4 fetching the
    viewer.
    """
    assert ownership_doc("xslF345X06/ownership.xml", form=form) == "ownership.xml"


@pytest.mark.spec
@pytest.mark.parametrize("form", ["10-K", "10-Q", "8-K", "DEF 14A", "S-1/A", "4-KIDDING"])
def test_ownership_doc_does_not_strip_for_other_forms(form: str) -> None:
    """Restricted to 3/4/5 rather than applied unconditionally.

    An ``xsl``-prefixed path on some other form would be a different thing, and stripping it blindly
    turns a URL we do not understand into a URL that silently 404s. ``"4-KIDDING"`` is here because
    ``form.startswith("4")`` is the tempting shortcut for handling amendments.
    """
    assert ownership_doc("xslSomething/inline.htm", form=form) == "xslSomething/inline.htm"


@pytest.mark.spec
@pytest.mark.parametrize(
    "document",
    ["ownership.xml", "d901234ds1a.htm", "ARXS_8A_Cert_2093536.pdf", "sub/dir/doc.xml"],
)
def test_ownership_doc_leaves_a_path_without_an_xsl_prefix_alone(document: str) -> None:
    """A Form 4 whose ``primaryDocument`` is already the raw XML must not lose its first segment.

    ``partition("/")`` before checking the prefix is the bug this catches: it would turn
    ``sub/dir/doc.xml`` into ``dir/doc.xml`` for every ownership form, and only for ownership forms.
    """
    assert ownership_doc(document, form="4") == document


@pytest.mark.spec
def test_ownership_doc_composes_into_an_archives_url() -> None:
    """The two functions have to agree, because they are always called together.

    Asserted end to end so a change to either one shows up here: the stripped document name lands in
    the accession directory, which is where the machine-readable XML actually lives.
    """
    document = ownership_doc("xslF345X06/ownership.xml", form="4")
    expected = "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/ownership.xml"
    assert archives_doc_url(APPLE, ACCESSION, document) == expected

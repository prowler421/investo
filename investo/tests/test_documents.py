"""Filing bodies -> Item sections, and the normalizer §7.3's citation verifier depends on.

`docs/m1/04-parsers.md` §5 accepts that the heading regex is brittle across filers and says so out
loud: `split_ok` is a field, `unrecognized` is a field, and the parse rate is reported. So the
tests here are about the *properties* that have to hold whatever the regex does — idempotent
normalization, preserved line breaks, nothing discarded, and no exception on a document that will
not split.

`tests/fixtures/edgar/PROVENANCE.md` records that no filing-body fixtures have been collected yet,
so the documents below are constructed. That gap is why these tests assert properties rather than
parse rates against real filers.
"""

from __future__ import annotations

import pytest

from investo.domain.provenance import Accession
from investo.ingest.edgar.documents import TENK_ITEMS, extract_text, normalize_text, split_items

ACCESSION = Accession.parse("0000320193-19-000119")

MESSY = "Item\u00a01A.&nbsp;Risk Factors   \r\n\r\n\r\n\r\nWe rely on&amp;partners.\t\tYes.  \n"
"""One string carrying every transformation the normalizer performs.

A non-breaking space, an `&nbsp;` entity, an `&amp;` entity, a tab run, trailing spaces, CRLF line
endings and four consecutive newlines — all of which EDGAR emits, and each of which is a separate
branch of `normalize_text`.
"""

TENK = """\
Table of Contents

PART I
Item 1. Business 4
Item 1A. Risk Factors 12
Item 5. Market for Registrant's Common Equity 26
Item 7. Management's Discussion and Analysis 30
Item 7A. Quantitative and Qualitative Disclosures 44

PART I

Item 1. Business
We design, manufacture and sell smartphones, personal computers and accessories, and we
sell a variety of related services. Our fiscal year ends in late September.

Item 1A. Risk Factors
Our business faces risks. The Company's operations and performance depend significantly on
global and regional economic conditions, and adverse macroeconomic conditions could
materially adversely affect the Company's business.

Item 5. Market for Registrant's Common Equity
Our common stock is traded on the Nasdaq Global Select Market.

PART II

Item 7. Management's Discussion and Analysis
Revenue increased during the year, driven by higher net sales of services. The following
discussion should be read together with the consolidated financial statements.

Item 7A. Quantitative and Qualitative Disclosures
We are exposed to interest rate risk and foreign currency risk in the ordinary course of
business.

Exhibit Index

Item 1A. Risk Factors
See page 12.
"""
"""A 10-K skeleton with three deliberate traps.

Every wanted heading appears in the table of contents *before* the body, `Item 5` is a real
heading that is not one of §7.4's wanted items, and `Item 1A` appears a third time at the end with
a one-line cross-reference. Those three positions are what make "keeps the longest section" a
testable claim rather than an untested tie-break: a first-wins implementation returns the empty
table-of-contents section, and a last-wins implementation returns "See page 12."
"""


# ---------------------------------------------------------------------------
# The normalizer
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_normalize_text_is_idempotent() -> None:
    """`normalize_text(normalize_text(x)) == normalize_text(x)`, and this is not a nicety.

    DESIGN.md §7.3 requires verbatim quote verification against this text. A quote normalized once
    and searched inside text that has been normalized twice would not match, so the citation
    verifier would reject a true citation — and the failure would look like the LLM having
    hallucinated a quote it copied correctly.
    """
    once = normalize_text(MESSY)
    assert normalize_text(once) == once
    assert normalize_text(TENK) == normalize_text(normalize_text(TENK))


@pytest.mark.spec
def test_normalize_text_preserves_line_breaks() -> None:
    """Line breaks survive, because the Item-heading regex is line-anchored.

    Collapsing all whitespace — the obvious way to normalize — makes every heading unfindable, and
    the symptom is not an error: it is a report with no narrative sections and a parse rate of
    zero. So the assertion is tied to its consequence, by splitting the normalized text and
    finding the items.
    """
    normalized = normalize_text(TENK)

    assert "\n" in normalized
    assert normalized.count("\n") > 10
    assert "1A" in split_items(normalized, form="10-K", accession=ACCESSION).items


@pytest.mark.spec
def test_normalize_text_collapses_nbsp_and_entities() -> None:
    """`&nbsp;` and a literal non-breaking space both become an ordinary space.

    EDGAR emits both, sometimes in one document. A quote copied out of a rendered filing carries the
    ordinary space, so text that kept `\\xa0` would fail every citation check on a filing that used
    it — silently, since the two are indistinguishable on screen.
    """
    assert normalize_text("a\u00a0b") == "a b"
    assert normalize_text("a&nbsp;b") == "a b"
    assert normalize_text("a&#160;b") == "a b"
    assert normalize_text("Ben &amp; Jerry") == "Ben & Jerry"
    assert normalize_text("  padded  \r\n  lines  ") == "padded\nlines"


@pytest.mark.spec
def test_normalize_text_caps_consecutive_blank_lines() -> None:
    """Two blank lines maximum, so a filing's whitespace does not change its own text's hash.

    §7.3 needs the normalization to be *stable*: the same filing fetched twice, or the same text run
    through a different amount of intervening HTML, has to produce one string.
    """
    assert normalize_text("a\n\n\n\n\n\nb") == "a\n\nb"
    assert normalize_text("a\n\nb") == "a\n\nb"


# ---------------------------------------------------------------------------
# extract_text
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_extract_text_on_plain_text_with_no_tags() -> None:
    """A body with no markup goes straight through the normalizer.

    Worth pinning because the tag-stripping path and the plain-text path have to agree: `lxml` is an
    optional import inside this function, and a fixture that is plain text must produce the same
    string whether or not it is installed. Otherwise this suite's results would depend on the
    developer's environment.
    """
    assert extract_text(b"Hello   world\n\n\n\nSecond  line") == "Hello world\n\nSecond line"


def test_extract_text_strips_tags_and_keeps_the_words() -> None:
    """Tag stripping is asserted on content rather than on spacing.

    `lxml` concatenates text nodes and the regex fallback inserts a newline per tag, so the exact
    whitespace between two adjacent elements differs between the two paths by design. Asserting on
    words keeps this test about extraction; the normalizer's tests above are about whitespace.
    """
    text = extract_text(b"<html><body><p>Risk Factors</p>\n<p>and more</p></body></html>")

    assert "Risk Factors" in text
    assert "and more" in text
    assert "<p>" not in text


# ---------------------------------------------------------------------------
# split_items
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_split_items_finds_the_wanted_headings() -> None:
    """§7.4's item set, split from one document. The section text is asserted, not just the key.

    A heading matcher that recorded the heading and lost the body would produce a full item map, a
    parse rate of 1.0, and no narrative for the LLM to read.
    """
    document = split_items(normalize_text(TENK), form="10-K", accession=ACCESSION)

    assert set(document.items) >= {"1", "1A", "7", "7A"}
    assert "smartphones" in document.items["1"]
    assert "macroeconomic" in document.items["1A"]
    assert "net sales of services" in document.items["7"]
    assert "interest rate risk" in document.items["7A"]


@pytest.mark.spec
def test_a_repeated_heading_keeps_the_longer_section() -> None:
    """The table of contents lists every Item before the body does, so every heading repeats.

    Longest-wins is a heuristic and `documents.py` labels it as one: a table-of-contents entry is
    a line and the real section is paragraphs. The third occurrence — a one-line cross-reference
    at the end — is what makes this test discriminating: first-wins yields the empty contents
    section and last-wins yields "See page 12.", so only longest-wins passes.
    """
    document = split_items(normalize_text(TENK), form="10-K", accession=ACCESSION)
    section = document.items["1A"]

    assert normalize_text(TENK).count("Item 1A") == 3
    assert "macroeconomic" in section
    assert "See page 12." not in section
    assert section, "first-wins would have produced the empty table-of-contents section"


@pytest.mark.spec
def test_unrecognized_headings_are_retained() -> None:
    """A parser never discards what it could not interpret.

    `Item 5` is a real heading that is not one of the wanted items. Keeping it means a §7.4 change —
    or an SEC renumbering — shows up as data rather than as a section that quietly disappeared, and
    the dedup keeps the list readable when the heading repeats in the contents.
    """
    document = split_items(normalize_text(TENK), form="10-K", accession=ACCESSION)

    assert "Item 5" in document.unrecognized
    assert "5" not in document.items
    assert len(document.unrecognized) == len(set(document.unrecognized))


@pytest.mark.spec
def test_missing_items_lower_the_parse_rate_without_raising() -> None:
    """A filing that will not split is a coverage fact, not an aborted run.

    DESIGN.md §14's distinction applied to the narrative: a missing section thins the report, and
    raising here would turn it into a failed run instead. `split_ok` and `parse_rate` are what make
    the gap measurable, so both are asserted rather than only the absence of an exception.
    """
    document = split_items(normalize_text(TENK), form="10-K", accession=ACCESSION)
    found = set(document.items) & set(TENK_ITEMS)

    assert document.split_ok is False
    assert set(TENK_ITEMS) - found, "the fixture must be missing at least one wanted item"
    assert document.parse_rate == len(found) / len(TENK_ITEMS)
    assert 0.0 < document.parse_rate < 1.0


@pytest.mark.spec
def test_a_document_with_no_headings_at_all_does_not_raise() -> None:
    """The worst case is still not an exception: no items, `split_ok=False`, parse rate 0.0."""
    document = split_items("There are no item headings in here.", form="10-K", accession=ACCESSION)

    assert document.items == {}
    assert document.unrecognized == ()
    assert document.split_ok is False
    assert document.parse_rate == 0.0
    assert document.text == "There are no item headings in here."

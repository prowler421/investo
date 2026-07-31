"""Provenance: the accession transforms, and what a traced number is allowed to look like.

DESIGN.md §3.2's first property is that every number traces to a source. These types are that
record, so the tests here are mostly about the ways a *plausible-looking* provenance record can be
wrong: an accession that is silently accepted and 404s, a CIK read off the wrong ten digits, a naive
timestamp that means something different on every machine, and a derived figure that cites one of
its inputs as if it were the whole story.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from investo.domain.provenance import Accession, Derivation, SourceContext, SourceRef
from tests.conftest import FETCHED_AT, fixture_json

APPLE_CIK = 320193
DASHED = "0000320193-25-000079"
UNDASHED = "000032019325000079"


def _ref(tag: str) -> SourceRef:
    """A distinguishable ref. ``tag`` is the label the flattening tests assert on."""
    return SourceRef(
        accession=Accession.parse(DASHED),
        taxonomy="us-gaap",
        tag=tag,
        form="10-K",
        filed=date(2025, 10, 31),
        url="https://data.sec.gov/test",
        fetched_at=FETCHED_AT,
    )


# ---------------------------------------------------------------------------
# Accession.parse
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [DASHED, UNDASHED, f"  {DASHED}  ", f"  {UNDASHED}  "])
def test_parse_accepts_both_spellings_and_normalizes_to_dashed(raw: str) -> None:
    """Both spellings occur in real payloads and both have to arrive at one canonical form.

    ``submissions`` gives the dashed form and an ``/Archives/`` directory name gives the undashed
    one. If ``parse`` normalized only sometimes, two ``Accession`` values for the same filing would
    compare unequal, and the dedup that M2 does by accession would silently keep both.
    """
    assert Accession.parse(raw).value == DASHED


@pytest.mark.parametrize(
    "raw",
    [
        "00003201932500007",
        "0000320193250000790",
        "000032019A-25-000079",
        "",
        "   ",
        "000032019-325-000079",
        "00003201932-5-000079",
        "0000320193-25-00079",
        "0000320193-25-0000790",
        "0000320193_25_000079",
        "0000320193 - 25 - 000079",
        "0000320193-25-000079-index.htm",
    ],
    ids=[
        "17-digits",
        "19-digits",
        "letter",
        "empty",
        "blank",
        "dash-at-10-is-at-9",
        "dash-at-13-is-at-11",
        "sequence-too-short",
        "sequence-too-long",
        "underscores",
        "spaces-around-dashes",
        "index-suffix",
    ],
)
def test_parse_rejects_malformed(raw: str) -> None:
    """Eighteen digits grouped 10-2-6, or nothing.

    ROADMAP M1 names this as one of the milestone's two risks, and the reason is that the failure is
    invisible: a silently accepted malformed accession becomes a URL that returns 404, and a 404
    from EDGAR is indistinguishable from a company that never filed. Every row above is a shape a
    lenient regex accepts — a missing digit, an extra one, a dash one position early.
    """
    with pytest.raises(ValueError, match="accession number"):
        _ = Accession.parse(raw)


@pytest.mark.spec
def test_bad_accession_raises() -> None:
    """``docs/m1/06-testing.md`` §4: a malformed accession is rejected, not passed through.

    The input comes from ``malformed/bad_accession.json`` rather than from this file, so the test
    fails if that fixture is ever "corrected" — at which point the fixture would no longer be
    testing anything and nothing else would say so.
    """
    payload = fixture_json("edgar", "malformed", "bad_accession.json")
    raw = str(payload["filings"]["recent"]["accessionNumber"][0])
    assert len(raw.replace("-", "")) != 18, "the fixture is supposed to carry a malformed value"
    with pytest.raises(ValueError):
        _ = Accession.parse(raw)


def test_nodashes_is_the_archives_directory_name() -> None:
    """``/Archives/`` wants the digits with no dashes, and ``data.sec.gov`` wants them dashed.

    Two spellings of one identifier in two paths, so the transform gets one home. Asserted as a
    derivation — the digits are unchanged and only the separators go — rather than against a second
    hand-typed literal that could be wrong in the same way the code is.
    """
    accession = Accession.parse(DASHED)
    assert accession.nodashes == DASHED.replace("-", "")
    assert accession.nodashes.isdigit()
    assert len(accession.nodashes) == 18


def test_index_url_takes_the_cik_as_an_argument() -> None:
    """The filing index page needs a CIK, and the accession is not allowed to supply it.

    Both spellings appear in one URL — the CIK unpadded, the accession undashed in the directory and
    dashed in the filename — which is why this is a method rather than an f-string at the call site.
    """
    accession = Accession.parse(DASHED)
    url = accession.index_url(APPLE_CIK)
    assert url.endswith(f"/{accession.nodashes}/{accession.value}-index.htm")
    assert f"/data/{APPLE_CIK}/" in url


@pytest.mark.spec
def test_accession_exposes_no_cik_at_runtime_either() -> None:
    """No company CIK is derived from an accession (``docs/m1/01-domain-types.md`` §1).

    The leading ten digits identify whoever *submitted* the filing, which for most issuers is a
    filer agent. Apple's history contains both patterns, so the wrong rule is right on some filings
    and produces a nonexistent CIK on others.

    ``tests/fixtures/typing/accession_cik_attribute.py`` is the real violation test — the guarantee
    is type-level. This is its runtime complement, and it exists because ``__getattr__`` or a
    late-added property would satisfy basedpyright's absence check while still returning a number.
    """
    accession = Accession.parse("0001140361-26-025622")
    attribute = "cik"  # via a variable: ruff's B009 only fires on a constant name
    assert not hasattr(accession, attribute)
    with pytest.raises(AttributeError):
        _ = getattr(accession, attribute)


# ---------------------------------------------------------------------------
# SourceRef
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_source_ref_rejects_a_naive_datetime() -> None:
    """``fetched_at`` is timezone-aware UTC, always.

    A naive timestamp in a provenance record means something different on every machine, and the
    cache is meant to be the immutable record of what the model saw. Accepting one would make that
    record unreadable by anyone in another timezone — and it would be unreadable *silently*, which
    is worse than a crash.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        _ = SourceRef(
            accession=Accession.parse(DASHED),
            taxonomy="dei",
            tag="EntityCommonStockSharesOutstanding",
            form="10-K",
            filed=date(2025, 10, 31),
            url="https://data.sec.gov/test",
            fetched_at=datetime(2026, 7, 31, 11, 2, 21),
        )


def test_source_ref_accepts_a_non_utc_offset() -> None:
    """Aware is the requirement; UTC is the convention.

    Written because the obvious implementation of "must be UTC" is ``tzinfo is UTC``, which rejects
    an equivalent instant expressed at another offset. The record is a moment in time, and refusing
    a correct one is a bug that only shows up on a machine configured differently from the author's.
    """
    ref = SourceRef(
        accession=Accession.parse(DASHED),
        taxonomy=None,
        tag=None,
        form="10-K",
        filed=date(2025, 10, 31),
        url="https://data.sec.gov/test",
        fetched_at=FETCHED_AT.astimezone(timezone(timedelta(hours=-4))),
    )
    assert ref.fetched_at == FETCHED_AT, "the same instant, spelled at another offset"


@pytest.mark.parametrize(
    ("taxonomy", "tag", "expected"),
    [
        ("us-gaap", "Assets", "us-gaap:Assets"),
        ("dei", "EntityCommonStockSharesOutstanding", "dei:EntityCommonStockSharesOutstanding"),
        (None, "Assets", "Assets"),
        ("", "Assets", "Assets"),
        ("us-gaap", None, None),
        (None, None, None),
    ],
)
def test_qualified_tag(taxonomy: str | None, tag: str | None, expected: str | None) -> None:
    """``us-gaap:Assets`` is the spelling DESIGN.md §9.1's appendix prints.

    The qualification is not cosmetic: ``Assets`` exists in more than one taxonomy, so an appendix
    that printed the bare tag would claim a provenance it has not established. The ``None`` rows are
    the non-XBRL sources — a price series has a URL and a fetch time but no tag.
    """
    ref = SourceRef(
        accession=Accession.parse(DASHED),
        taxonomy=taxonomy,
        tag=tag,
        form="10-K",
        filed=date(2025, 10, 31),
        url="https://data.sec.gov/test",
        fetched_at=FETCHED_AT,
    )
    assert ref.qualified_tag == expected


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------
def test_refs_returns_the_leaves_of_a_flat_derivation() -> None:
    """The appendix cites filings, not rules, so it needs the leaves.

    A market cap traces to a price fetch plus one ref per share class. Printing the derivation's own
    ``rule`` would tell a reader what was computed and nothing about where the inputs came from.
    """
    price, shares = _ref("price"), _ref("shares")
    derivation = Derivation(rule="market_cap", inputs=(price, shares))
    assert derivation.refs() == (price, shares)


def test_refs_flattens_nested_derivations_in_input_order() -> None:
    """A derived margin over a stitched series is three levels deep, and order is provenance.

    Input order is asserted, not membership, because the appendix lists the filings a figure came
    from and a set-like walk would reorder them run to run — which DESIGN.md §11's byte-identical
    output gate would then catch as a diff nobody introduced.
    """
    revenue_a, revenue_b, cogs, price = _ref("rev-a"), _ref("rev-b"), _ref("cogs"), _ref("price")
    stitched = Derivation(rule="asc_606_stitch", inputs=(revenue_a, revenue_b))
    gross = Derivation(rule="gross_profit", inputs=(stitched, cogs))
    top = Derivation(rule="margin", inputs=(gross, price))
    assert top.refs() == (revenue_a, revenue_b, cogs, price)


def test_refs_preserves_order_when_the_nested_derivation_comes_first() -> None:
    """The converse arrangement, because "flatten in order" is easy to implement as "leaves last".

    An implementation that appended every direct ``SourceRef`` before recursing would pass the test
    above and fail this one, and the report would cite the same filings in the wrong order.
    """
    a, b, c = _ref("a"), _ref("b"), _ref("c")
    inner = Derivation(rule="inner", inputs=(b, c))
    outer = Derivation(rule="outer", inputs=(inner, a))
    assert outer.refs() == (b, c, a)


def test_refs_on_an_empty_derivation_is_empty() -> None:
    """A rule with no inputs traces to nothing, and says so rather than raising.

    §3.2's rule is that an untraceable number is not printed. That decision belongs to the caller
    that has the number; ``refs()`` reporting an empty tuple is what lets the caller make it.
    """
    assert Derivation(rule="nothing", inputs=()).refs() == ()


def test_note_records_what_the_report_has_to_state() -> None:
    """DESIGN.md §5.4 requires the report to name the share classes counted.

    The number alone cannot say whether GOOG was included, so the omission would be invisible.
    ``note`` is where that goes, and it defaults to ``None`` so a rule with nothing to add says
    nothing rather than an empty string.
    """
    assert Derivation(rule="market_cap", inputs=()).note is None
    assert Derivation(rule="market_cap", inputs=(), note="classes: GOOGL, GOOG").note is not None


# ---------------------------------------------------------------------------
# SourceContext
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_source_context_rejects_a_naive_datetime() -> None:
    """Same rule as :class:`SourceRef`, enforced at the point time *enters* ``ingest/``.

    Every parser builds its refs through a context, so checking only ``SourceRef`` would let a naive
    timestamp travel as far as the first ref built — and the error would then name a type the caller
    never constructed.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        _ = SourceContext(url="https://data.sec.gov/test", fetched_at=datetime(2026, 7, 31))


def test_context_ref_fills_in_what_only_the_context_knows() -> None:
    """A parser knows the tag and the filing; it does not know the URL or the fetch time.

    That split is what keeps parsers testable from a file on disk with no client present. If
    ``ref()`` did not carry both fields through, each parser would need them passed separately and
    one of them would eventually be reconstructed from the clock.
    """
    context = SourceContext(url="https://data.sec.gov/x", fetched_at=FETCHED_AT, cik=APPLE_CIK)
    ref = context.ref(
        accession=Accession.parse(DASHED),
        form="10-Q",
        filed=date(2025, 8, 1),
        taxonomy="us-gaap",
        tag="Assets",
    )
    assert ref.url == context.url
    assert ref.fetched_at == context.fetched_at
    assert ref.form == "10-Q"
    assert ref.filed == date(2025, 8, 1)
    assert ref.qualified_tag == "us-gaap:Assets"


def test_context_ref_defaults_to_no_tag() -> None:
    """Not every row is an XBRL fact — a filing index entry has a form and a date and no tag.

    Defaulting to ``None`` rather than requiring the argument means a parser cannot be tempted to
    pass an empty string, which would print as ``dei:`` in the appendix.
    """
    context = SourceContext(url="https://www.sec.gov/x.htm", fetched_at=FETCHED_AT)
    ref = context.ref(accession=Accession.parse(DASHED), form="8-K", filed=date(2026, 3, 30))
    assert ref.taxonomy is None
    assert ref.tag is None
    assert ref.qualified_tag is None
    assert context.cik is None

"""`submissions`: the columnar transform, the awkward scalars, and pagination.

`docs/m1/04-parsers.md` §3 lists five things a live payload contradicted or sharpened, four of which
were wrong in the design's first draft. Each is a value that a parser written against the
documentation handles by raising — on a payload SEC serves every day.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from investo.errors import ExitCode, UpstreamFetchError
from investo.ingest.edgar._fields import as_bool, as_optional_str
from investo.ingest.edgar.submissions import (
    FilesEntry,
    FilingRow,
    merge_pages,
    pages_needed,
    parse_files,
    parse_submissions,
    parse_submissions_page,
)
from tests.conftest import context, fixture_bytes

APPLE_CIK = 320193
OVERFLOW_PAGE = "CIK0000320193-submissions-001.json"
PAGE_FROM = date(2015, 8, 5)
PAGE_TO = date(2019, 10, 30)
"""The `files[0]` range in `submissions/AAPL.json`, restated so the boundary tests read as one."""


def _row(rows: Sequence[FilingRow], accession: str) -> FilingRow:
    found = [row for row in rows if row.accession.value == accession]
    assert len(found) == 1, f"{accession} should appear exactly once, got {len(found)}"
    return found[0]


def _recent(body: bytes) -> dict[str, Any]:
    payload: Any = json.loads(body)
    columns: dict[str, Any] = payload["filings"]["recent"]
    return columns


def _files(body: bytes) -> tuple[FilesEntry, ...]:
    _profile, _rows, files = parse_submissions(body, source=context())
    return files


# ---------------------------------------------------------------------------
# The columnar transform
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_short_column_raises(arxs_submissions: bytes) -> None:
    """A short column is exit 4, not a truncated filing history.

    `malformed/short_column.json` is the ARXS payload with `primaryDocDescription` two entries
    short. Zipping the arrays truncates to the shortest and silently drops the tail — which looks
    exactly like a company that stopped filing, and is undetectable downstream. A malformed payload
    is not an absence, so it fails the run rather than thinning the coverage report.

    The row count from the intact payload is asserted alongside, because "raises" is only meaningful
    if the same parser succeeds on the same payload with the column restored.
    """
    _profile, intact, _files = parse_submissions(arxs_submissions, source=context())
    assert len(intact) == 7

    with pytest.raises(UpstreamFetchError) as caught:
        _ = parse_submissions(
            fixture_bytes("edgar", "malformed", "short_column.json"), source=context()
        )
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE
    assert "disagree in length" in caught.value.message


@pytest.mark.spec
def test_every_column_is_the_same_length_as_the_row_count(arxs_submissions: bytes) -> None:
    """The check is on *all* arrays, not only the ones the parser reads.

    A length check restricted to the four required columns passes on the malformed fixture above,
    because the column that is short there is one nothing reads. That is the version of this check
    that looks correct and is not.
    """
    _profile, rows, _files = parse_submissions(arxs_submissions, source=context())
    lengths = {name: len(column) for name, column in _recent(arxs_submissions).items()}

    assert set(lengths.values()) == {len(rows)}
    assert "primaryDocDescription" in lengths, "the unread column has to be in scope of the check"


# ---------------------------------------------------------------------------
# Absent scalars
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_empty_string_scalars_become_none(arxs_submissions: bytes) -> None:
    """Absence is spelled `""` here, not `null` — on one endpoint, in one document.

    A parser written against `None` reaches `date.fromisoformat("")` and raises `ValueError` on the
    `reportDate` of a Form 3 or an `EFFECT` row, which is every filer with an insider.

    `act` is asserted through the boundary normalizer rather than through `FilingRow`, because the
    row type does not carry it — what is being pinned is that the `""` in the payload maps to
    `None` and that its presence does not break the parse.
    """
    profile, rows, _files = parse_submissions(arxs_submissions, source=context())
    columns = _recent(arxs_submissions)

    registration = _row(rows, "0001193125-26-146309")
    index = columns["accessionNumber"].index(registration.accession.value)
    assert columns["reportDate"][index] == ""
    assert registration.report_date is None

    effect_index = columns["form"].index("EFFECT")
    assert columns["act"][effect_index] == ""
    assert as_optional_str(columns["act"][effect_index]) is None

    assert profile.name == "Arxis, Inc.", 'the parse survived every `""` in the payload'


@pytest.mark.spec
def test_sic_is_an_int_from_a_string_and_none_when_empty(arxs_submissions: bytes) -> None:
    """`"sic":"3728"` -> `3728`, and `""` -> `None`.

    `int(payload["sic"])` raises on a real input: a filer without an SIC code carries the empty
    string. Both sides are asserted because a normalizer that only handled the string case would
    pass on this fixture and fail on the first filer that has no code.
    """
    payload: Any = json.loads(arxs_submissions)
    assert payload["sic"] == "3728"

    profile, _rows, _files = parse_submissions(arxs_submissions, source=context())
    assert profile.sic == 3728
    assert isinstance(profile.sic, int)

    payload["sic"] = ""
    without, _rows2, _files2 = parse_submissions(json.dumps(payload).encode(), source=context())
    assert without.sic is None


@pytest.mark.spec
def test_is_xbrl_numeric_mixes_null_with_zero_and_one(arxs_submissions: bytes) -> None:
    """`isXBRLNumeric` carries genuine `null` mixed with `0`/`1` in one array.

    It is the exception that proves the `""`-means-absent rule, so both spellings of absence occur
    in a single document. A `bool(...)` cast would record "definitely not numeric XBRL" where the
    truth is "not stated" — and the parse must survive the column even though `FilingRow` does not
    expose it, which is what the row-count assertion pins.
    """
    columns = _recent(arxs_submissions)
    column: list[Any] = columns["isXBRLNumeric"]

    assert None in column
    assert 0 in column
    assert 1 in column
    assert [as_bool(value) for value in column] == [
        None if value is None else bool(value) for value in column
    ]

    _profile, rows, _files = parse_submissions(arxs_submissions, source=context())
    assert len(rows) == len(column)


# ---------------------------------------------------------------------------
# `items`
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_items_double_comma_yields_no_codes(arxs_submissions: bytes) -> None:
    """The observed degenerate value is literally `",,"`, on an `EFFECT` filing.

    A naive `split(",")` yields three empty item codes on a real filing, and an events extractor
    downstream would then report three unrecognised codes that were never there. Filtering on
    `^\\d\\.\\d\\d$` discards all three; `items_raw` keeps the original so the parse rate is honest.
    """
    _profile, rows, _files = parse_submissions(arxs_submissions, source=context())
    effect = _row(rows, "9999999997-26-004411")

    assert effect.items_raw == ",,"
    assert effect.items == ()


@pytest.mark.spec
def test_unrecognised_items_kept_in_items_raw(arxs_submissions: bytes) -> None:
    """A parser never discards what it could not interpret.

    Recognised and unrecognised tokens arrive in the same string, so both halves are asserted on one
    row: `2.02` is kept as a code, `9.999` and `FOO` are not, and every one of the three survives in
    `items_raw`. A parser that recognised nothing and dropped the original would have destroyed the
    evidence that it failed, and the fetch summary's parse rate would report success.
    """
    payload: Any = json.loads(arxs_submissions)
    raw = "9.999,FOO,2.02"
    payload["filings"]["recent"]["items"][0] = raw

    _profile, rows, _files = parse_submissions(json.dumps(payload).encode(), source=context())
    row = _row(rows, "0001193125-26-243043")

    assert row.items == ("2.02",)
    assert row.items_raw == raw


# ---------------------------------------------------------------------------
# The two payload shapes are not interchangeable
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_page_parser_rejects_wrapped_payload(aapl_submissions: bytes) -> None:
    """An overflow page is never parsed as a main payload, or the reverse.

    The page is a flat columnar object; the main payload wraps the same arrays in `filings.recent`
    and adds company metadata. One function cannot parse both, and the failure mode of trying is not
    an exception — it is an empty filing list from a payload that was fine.
    """
    with pytest.raises(UpstreamFetchError) as caught:
        _ = parse_submissions_page(aapl_submissions, source=context())
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE


@pytest.mark.spec
def test_main_parser_rejects_a_flat_page(aapl_submissions_page: bytes) -> None:
    """The converse of the test above, which is the half that is easy to leave out.

    Without it, a `parse_submissions` that fell back to reading the top level as `recent` would look
    correct: it would parse the page's rows and return a `CompanyProfile` full of empty strings.
    """
    with pytest.raises(UpstreamFetchError) as caught:
        _ = parse_submissions(aapl_submissions_page, source=context())
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE


def test_page_parser_reads_the_flat_shape(aapl_submissions_page: bytes) -> None:
    """The page's first key is `accessionNumber`, exactly as the observed payload begins."""
    rows = parse_submissions_page(aapl_submissions_page, source=context())
    assert len(rows) == 3
    assert _row(rows, "0001193125-15-177428").filed == date(2015, 5, 8)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_a_window_reaching_into_the_overflow_requests_the_page(aapl_submissions: bytes) -> None:
    """`filings.recent` is not the whole history — Apple's page 001 holds 2015 filings.

    A 10y lookback that reads only `recent` sees an incomplete filing history, and DESIGN.md §6.6
    calls the loudest signal in the system an 8-K item code. Missing four-year-old 8-Ks is not a
    detail.
    """
    files = _files(aapl_submissions)
    assert pages_needed(files, window=(date(2015, 1, 1), date(2026, 7, 31))) == (OVERFLOW_PAGE,)


@pytest.mark.spec
def test_a_window_inside_recent_requests_zero_pages(aapl_submissions: bytes) -> None:
    """Otherwise the optimization is a comment.

    Asserted as the empty tuple rather than as "no error": an implementation that returned every
    page unconditionally would satisfy every other test in this section, cost two requests per
    company, and never be noticed.
    """
    files = _files(aapl_submissions)
    assert pages_needed(files, window=(date(2021, 1, 1), date(2026, 7, 31))) == ()


@pytest.mark.spec
def test_window_starting_on_a_pages_filing_to_includes_that_page(aapl_submissions: bytes) -> None:
    """The boundary is inclusive: a window whose start equals `filingTo` includes the page.

    A `>` where `>=` belongs drops the page that holds the filings from exactly that day, and
    every test that probes 2015 and 2021 passes anyway. The day *after* `filingTo` is asserted
    excluded, so the pair fixes the boundary rather than one side of it.
    """
    files = _files(aapl_submissions)
    end = date(2026, 7, 31)

    assert pages_needed(files, window=(PAGE_TO, end)) == (OVERFLOW_PAGE,)
    assert pages_needed(files, window=(PAGE_TO + timedelta(days=1), end)) == ()


@pytest.mark.spec
def test_files_entry_missing_a_documented_field_raises_naming_the_key() -> None:
    """The `files[]` field names are the one unconfirmed shape in M1a, so the parser is strict.

    `docs/m1/04-parsers.md` § Pagination says to confirm them by fetching, and PROVENANCE.md records
    that the fetch has not happened. The failure message is therefore designed to *be* the finding:
    a silently skipped page would look like a company that stopped filing, so an entry that does not
    carry every documented key raises with the real keys listed.
    """
    with pytest.raises(UpstreamFetchError) as caught:
        _ = parse_files(
            [{"name": OVERFLOW_PAGE, "filingCount": 1000, "filingFrom": PAGE_FROM.isoformat()}]
        )
    assert "filingTo" in caught.value.message
    assert caught.value.exit_code == ExitCode.UPSTREAM_FETCH_FAILURE


def test_no_files_means_no_pages(arxs_submissions: bytes) -> None:
    """`"files":[]` is the observed value for a filer without overflow, so the empty case is
    normal."""
    assert _files(arxs_submissions) == ()
    assert pages_needed((), window=(date(2015, 1, 1), date(2026, 7, 31))) == ()


# ---------------------------------------------------------------------------
# merge_pages
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_merge_pages_dedups_by_accession_and_sorts_filed_descending(
    aapl_submissions: bytes, aapl_submissions_page: bytes
) -> None:
    """Page boundaries are SEC's, and nothing guarantees they do not overlap.

    Three properties in one test because they are one guarantee: every row from the older page is
    present, an accession that appears in both groups appears once, and the order is `filed`
    descending with a total tiebreak — an unstable sort here would make the fetch summary's row
    order differ between runs, which DESIGN.md §11's byte-identical gate would read as a change.
    """
    _profile, recent, _files = parse_submissions(aapl_submissions, source=context())
    page = parse_submissions_page(aapl_submissions_page, source=context())

    merged = merge_pages(recent, page)
    accessions = [row.accession.value for row in merged]

    assert len(merged) == len(recent) + len(page)
    assert len(set(accessions)) == len(accessions)
    assert "0001193125-15-177428" in accessions, "a row from the older page must survive the merge"
    assert [row.filed for row in merged] == sorted((row.filed for row in merged), reverse=True)


@pytest.mark.spec
def test_merge_pages_keeps_the_first_copy_of_a_duplicated_accession(
    aapl_submissions: bytes,
) -> None:
    """`recent` is passed first, so the newest copy of an overlapping row wins.

    Built by mutating a real row rather than by comparing counts, because a dedup that kept the
    *last* occurrence would also produce the right length — and would serve the older page's stale
    view of a filing that has since been superseded.
    """
    _profile, recent, _files = parse_submissions(aapl_submissions, source=context())
    original = _row(recent, "0000320193-19-000119")
    stale = dataclasses.replace(original, form="10-K/A")

    merged = merge_pages(recent, (stale,))

    assert len(merged) == len(recent)
    assert _row(merged, original.accession.value).form == "10-K"


# ---------------------------------------------------------------------------
# primary_url
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_primary_url_strips_the_xsl_prefix_for_a_form_4(arxs_submissions: bytes) -> None:
    """Every Form 3 and 4 row carries `"xslF345X06/ownership.xml"` — an XSL *viewer* path.

    Using it verbatim fetches a styled HTML rendering where `ownership.py` expects Form 4 XML, and
    the failure looks like a parser bug rather than a URL bug.
    """
    _profile, rows, _files = parse_submissions(arxs_submissions, source=context())
    form4 = _row(rows, "0001140361-26-025999")

    assert form4.primary_document == "xslF345X06/ownership.xml"
    assert form4.primary_url(2093536).endswith("/ownership.xml")
    assert "xslF345X06" not in form4.primary_url(2093536)


@pytest.mark.spec
def test_primary_url_leaves_a_ten_k_document_alone(aapl_submissions: bytes) -> None:
    """The strip is restricted to forms 3/4/5, and this is the half that pins the restriction.

    Applying it unconditionally would turn an `xsl`-prefixed path on some other form — a path we do
    not understand — into a URL that silently 404s.
    """
    _profile, rows, _files = parse_submissions(aapl_submissions, source=context())
    tenk = _row(rows, "0000320193-19-000119")

    assert tenk.primary_url(APPLE_CIK).endswith(f"/{tenk.primary_document}")
    assert f"/Archives/edgar/data/{APPLE_CIK}/" in tenk.primary_url(APPLE_CIK)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_profile_normalizes_the_padded_cik_and_keeps_the_fiscal_year_end_as_text(
    arxs_submissions: bytes,
) -> None:
    """`fiscalYearEnd` is `"MMDD"` and stays a string: it has no year.

    Constructing a date from it would invent a fiscal year the filer never stated, and the invented
    one would then be printed on a report cover.
    """
    profile, _rows, _files = parse_submissions(arxs_submissions, source=context())

    assert profile.cik == 2093536
    assert profile.fiscal_year_end == "1231"
    assert profile.tickers == ("ARXS",)
    assert profile.exchanges == ("Nasdaq",)


def test_acceptance_datetime_is_timezone_aware(arxs_submissions: bytes) -> None:
    """SEC writes a `Z` suffix, and a naive value is rejected rather than assumed to be UTC.

    This field orders filings within a day, and guessing a timezone is how an ordering shifts by
    hours across a date boundary.
    """
    _profile, rows, _files = parse_submissions(arxs_submissions, source=context())
    accepted = _row(rows, "0001193125-26-243043").accepted_at

    assert isinstance(accepted, datetime)
    assert accepted.tzinfo is not None

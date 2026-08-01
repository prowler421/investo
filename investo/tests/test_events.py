"""8-K extraction: which rows count, which keep a body URL, and what must not be here.

`docs/m1/04-parsers.md` §6: extraction only. DESIGN.md §6.6's two-stage design maps onto the
M1/M4.5 split, and the whole value of the first stage is that detection needs **no extra request**
— the item codes are already in `submissions`. So the tests below are about selection and
ordering, plus one assertion that the severity table has not crept in.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest

from investo.ingest.edgar import events
from investo.ingest.edgar.events import BODY_REQUIRED_ITEMS, extract_events, item_parse_rate
from investo.ingest.edgar.submissions import FilingRow, parse_submissions, parse_submissions_page
from tests.conftest import context

APPLE_CIK = 320193

_COLUMNS = (
    "accessionNumber",
    "filingDate",
    "reportDate",
    "acceptanceDateTime",
    "act",
    "form",
    "fileNumber",
    "filmNumber",
    "items",
    "core_type",
    "size",
    "isXBRL",
    "isInlineXBRL",
    "isXBRLNumeric",
    "primaryDocument",
    "primaryDocDescription",
)


def _rows(records: Sequence[dict[str, Any]]) -> tuple[FilingRow, ...]:
    """Build `FilingRow`s through the real parser rather than by constructing the dataclass.

    Constructing it directly would let a test invent an `items` tuple that `submissions.py` would
    never have produced — an `items=("4.02",)` with `items_raw=""`, say — and the extractor's
    contract is with the parser's output, not with the dataclass's field list.
    """
    columns: dict[str, list[Any]] = {
        name: [record.get(name, "") for record in records] for name in _COLUMNS
    }
    return parse_submissions_page(json.dumps(columns).encode(), source=context())


def _record(accession: str, form: str, filed: str, items: str = "") -> dict[str, Any]:
    return {
        "accessionNumber": accession,
        "filingDate": filed,
        "form": form,
        "items": items,
        "primaryDocument": "d8k.htm",
    }


# ---------------------------------------------------------------------------
# Which rows count
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_only_eight_k_forms_are_extracted_including_amendments() -> None:
    """Amendments are in, and everything that is not an 8-K is out.

    A 4.02 filed on an 8-K/A is the same signal as one on an 8-K, and dropping amendments would
    miss a restatement announced as a correction — which is the common case, not the edge one. The
    10-Q and Form 4 rows are here so that "only 8-Ks" is tested against something to exclude.
    """
    rows = _rows(
        [
            _record("0000320193-25-000061", "8-K", "2025-05-01", "2.02,9.01"),
            _record("0000320193-25-000079", "8-K/A", "2025-08-01", "4.02,9.01"),
            _record("0000320193-26-000013", "10-Q", "2026-01-30"),
            _record("0001140361-26-025622", "4", "2026-02-04"),
            _record("0000320193-25-000055", "DEF 14A", "2025-04-01"),
        ]
    )

    extracted = extract_events(rows, cik=APPLE_CIK)

    assert {event.form for event in extracted} == {"8-K", "8-K/A"}
    assert len(extracted) == 2


@pytest.mark.spec
def test_rows_whose_items_parsed_to_nothing_are_kept() -> None:
    """An 8-K with no recognised code is the evidence that the format changed.

    Dropping it would make `item_parse_rate` report 100% on a payload it failed to read — a metric
    that goes up when the parser breaks. So the row is kept with empty `items`, `items_raw` intact,
    and the rate says one of two.
    """
    rows = _rows(
        [
            _record("0000320193-25-000061", "8-K", "2025-05-01", "2.02,9.01"),
            _record("9999999997-26-004411", "8-K", "2026-04-14", ",,"),
        ]
    )

    extracted = extract_events(rows, cik=APPLE_CIK)
    unreadable = [event for event in extracted if not event.items]

    assert len(extracted) == 2
    assert len(unreadable) == 1
    assert unreadable[0].items_raw == ",,"
    assert item_parse_rate(extracted) == (1, 2)


@pytest.mark.spec
def test_item_parse_rate_is_zero_of_zero_for_a_filer_with_no_eight_ks() -> None:
    """A filer with no 8-Ks is an absence, and the rate must not divide by anything.

    `(0, 0)` rather than a float, so the caller decides how to render "no data" — a `0.0` here would
    print as a parse failure in the fetch summary for a company that simply filed nothing.
    """
    assert item_parse_rate(()) == (0, 0)


# ---------------------------------------------------------------------------
# body_url
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_body_url_is_built_only_for_the_items_that_need_it() -> None:
    """4.01 and 5.02 get a URL; everything else gets `None`.

    Only those two are ambiguous by item code alone — 5.02 covers a CEO departure *and* a routine
    compensation amendment — so only those two need the body, and only once M6 exists to read it.
    Building a URL for every 8-K would be harmless; *fetching* one would spend the rate budget on a
    question nobody is asking yet, and the URL being `None` is what stops that.

    4.02 is asserted to have **no** body URL even though it is the loudest signal in the system: its
    item code alone is conclusive, so the body adds nothing. That is the assertion that catches
    "important" being confused with "needs a fetch".
    """
    rows = _rows(
        [
            _record("0000320193-25-000079", "8-K", "2025-08-01", "4.02,9.01"),
            _record("0000320193-25-000061", "8-K", "2025-05-01", "4.01"),
            _record("0000320193-25-000055", "8-K", "2025-04-01", "5.02,9.01"),
            _record("0000320193-25-000044", "8-K", "2025-03-01", "2.02,9.01"),
        ]
    )

    by_items = {event.items: event for event in extract_events(rows, cik=APPLE_CIK)}

    assert BODY_REQUIRED_ITEMS == frozenset({"4.01", "5.02"})
    assert by_items[("4.01",)].body_url is not None
    assert by_items[("5.02", "9.01")].body_url is not None
    assert by_items[("4.02", "9.01")].body_url is None
    assert by_items[("2.02", "9.01")].body_url is None
    assert by_items[("4.01",)].needs_body is True
    assert by_items[("2.02", "9.01")].needs_body is False


@pytest.mark.spec
def test_body_url_uses_the_archives_transforms(aapl_submissions_page: bytes) -> None:
    """The URL is the `/Archives/` one: unpadded CIK, undashed accession.

    Built from the overflow page because that is where the fixtures' only 5.02 lives. Getting either
    transform wrong is a 404 that looks like a filing SEC never published.
    """
    rows = parse_submissions_page(aapl_submissions_page, source=context())
    extracted = extract_events(rows, cik=APPLE_CIK)
    needing_body = [event for event in extracted if event.needs_body]

    assert len(needing_body) == 1
    url = needing_body[0].body_url
    assert url is not None
    assert url.startswith(f"https://www.sec.gov/Archives/edgar/data/{APPLE_CIK}/")
    assert "0001193125-15-173308" not in url, "the /Archives/ directory name is undashed"
    assert "000119312515173308" in url


# ---------------------------------------------------------------------------
# Ordering, and the real fixture
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_events_are_newest_first() -> None:
    """Newest first, with a total tie-break on the accession.

    Two 8-Ks on one day is common, and an unstable order would make the fetch summary differ between
    runs on identical inputs — which DESIGN.md §11's byte-identical gate reads as a change.
    """
    rows = _rows(
        [
            _record("0000320193-25-000044", "8-K", "2025-03-01", "2.02"),
            _record("0000320193-25-000079", "8-K", "2025-08-01", "4.02"),
            _record("0000320193-25-000061", "8-K", "2025-05-01", "8.01"),
            _record("0000320193-25-000062", "8-K", "2025-05-01", "8.01"),
        ]
    )

    extracted = extract_events(rows, cik=APPLE_CIK)
    filed = [event.filed for event in extracted]

    assert filed == sorted(filed, reverse=True)
    assert filed[0] == date(2025, 8, 1)
    assert extracted == extract_events(tuple(reversed(rows)), cik=APPLE_CIK)


@pytest.mark.spec
def test_the_aapl_fixture_4_02_is_extracted(aapl_submissions: bytes) -> None:
    """The flagship fixture carries an Item 4.02, so the extractor has a real target.

    Non-reliance on previously issued financials is the highest-severity flag in the system (M4.5),
    and it is detected here as a string. A fixture without one would leave the whole 8-K path tested
    only against synthetic rows.
    """
    _profile, rows, _files = parse_submissions(aapl_submissions, source=context())
    extracted = extract_events(rows, cik=APPLE_CIK)
    non_reliance = [event for event in extracted if "4.02" in event.items]

    assert len(non_reliance) == 1
    assert non_reliance[0].items_raw == "4.02,9.01"
    assert non_reliance[0].filed == date(2025, 8, 1)


# ---------------------------------------------------------------------------
# What must not be here
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_this_module_has_no_severity_table() -> None:
    """Mapping a code to a severity is M4.5's `analysis/events.py`, and the separation is the point.

    It is what keeps `ingest/` replaceable: a severity table here would mean swapping the extraction
    source also swapped the judgment. Asserted as the absence of *any* severity-shaped name rather
    than of one spelling, because the way this rule gets broken is by someone adding
    `_SEVERITY_BY_ITEM` and not thinking of it as a table.
    """
    named = [name for name in vars(events) if "sever" in name.lower()]
    assert named == []
    assert not hasattr(events, "SEVERITY")

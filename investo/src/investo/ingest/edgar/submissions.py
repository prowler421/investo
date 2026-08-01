"""``submissions`` -> company profile plus filing rows, across as many pages as the window needs.

Source: ``https://data.sec.gov/submissions/CIK##########.json``.

**Confirmed against a complete live payload** — CIK 2093536 (Arxis, Inc., ARXS), fetched
2026-07-31. A recent registrant was chosen deliberately: its filing history is short enough that
the whole document is inspectable, so the key list is exhaustive rather than partial.

``filings.recent`` is columnar — sixteen parallel arrays::

    accessionNumber  filingDate  reportDate  acceptanceDateTime  act  form  fileNumber
    filmNumber  items  core_type  size  isXBRL  isInlineXBRL  isXBRLNumeric
    primaryDocument  primaryDocDescription

Five things the live payload contradicted or sharpened, four of which were wrong in the design's
first draft:

1. ``cik`` and ``sic`` are **zero-padded strings** here, not integers. ``sic`` can also be ``""``
   for a filer without one, so ``int(payload["sic"])`` raises on a real input.
2. **Absent values are the empty string, not ``null``** — ``reportDate``, ``act``, ``fileNumber``
   and ``primaryDocDescription`` all carry ``""``. A parser written against ``None`` reaches
   ``date.fromisoformat("")`` and raises. ``isXBRLNumeric`` is the exception that proves the rule:
   it carries genuine ``null`` mixed with ``0``/``1`` in one array, so both spellings of absence
   occur in one document.
3. ``items`` is comma-separated with no spaces, and **the degenerate case is real** — one
   ``EFFECT`` row's ``items`` is literally ``",,"``. A naive ``split(",")`` yields three empty
   codes. Filtering to ``^\\d\\.\\d\\d$`` discards all three and ``items_raw`` preserves the
   original.
4. ``primaryDocument`` can contain a subdirectory, and for ownership forms it points at the wrong
   thing: every Form 3 and 4 row has ``"xslF345X06/ownership.xml"``, an XSL-rendered viewer path.
   Handled by :func:`~investo.ingest.edgar.client.ownership_doc`. It can also be a PDF, so nothing
   may assume an ``.htm`` suffix.
5. ``files`` is confirmed as the key name and is ``[]`` for a filer without overflow, so the key is
   always present and the empty case needs no special handling.

**The one shape still unconfirmed in M1a is the fields inside a populated ``files[]`` entry.** See
:data:`FILES_ENTRY_FIELDS`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from investo.domain.provenance import Accession, SourceContext
from investo.errors import UpstreamFetchError
from investo.ingest.edgar._fields import (
    as_bool,
    as_cik,
    as_date,
    as_datetime,
    as_optional_int,
    as_optional_str,
    require,
)
from investo.ingest.edgar.client import archives_doc_url, ownership_doc

__all__ = [
    "ITEM_CODE",
    "FILES_ENTRY_FIELDS",
    "CompanyProfile",
    "FilingRow",
    "FilesEntry",
    "parse_submissions",
    "parse_submissions_page",
    "parse_files",
    "pages_needed",
    "merge_pages",
]

ITEM_CODE: Final = re.compile(r"^\d\.\d\d$")
"""An 8-K item code: one digit, a dot, two digits.

The codes themselves are stable and documented (SEC's Webmaster FAQ enumerates them for filings
since 2004): 1.01-1.05, 2.01-2.06, 3.01-3.03, 4.01, 4.02, 5.01-5.08, 6.01-6.10, 7.01, 8.01, 9.01.
Mapping a code to a *severity* is M4.5's ``analysis/events.py``, not this parser's.
"""

_REQUIRED_COLUMNS: Final = ("accessionNumber", "filingDate", "form", "primaryDocument")

FILES_ENTRY_FIELDS: Final = ("name", "filingCount", "filingFrom", "filingTo")
"""Field names inside a populated ``filings.files[]`` entry.

**Taken from SEC's prose plus the widely-used page-naming convention, not from an observed
payload.** A payload with overflow necessarily has >=1,000 filings and could not be inspected with
the tooling available when this was designed; the observed small-filer payload confirms only that
the key exists and is ``[]`` when empty.

``docs/m1/04-parsers.md`` calls confirming this *"the first task of implementation"* and warns that
the two payloads which could be fetched reversed nine assumptions between them, so this one should
be expected to surface something too.

The assumption is therefore isolated to this one constant and to :func:`parse_files`, which raises
:class:`~investo.errors.UpstreamFetchError` naming the exact missing key rather than guessing or
silently skipping the page. The failure message is designed to be the finding: run
``investo fetch AAPL``, read the error, and the real field names are in it.
"""


@dataclass(frozen=True, slots=True)
class CompanyProfile:
    """Company metadata from the main ``submissions`` payload.

    ``name`` is the **display name** for the report cover. ``companyfacts.entityName`` is
    EDGAR-conformed uppercase (``"ARXIS, INC."`` against this endpoint's ``"Arxis, Inc."``), and
    without a stated rule the cover page's casing depends on which parser ran last.
    """

    cik: int
    name: str
    sic: int | None
    sic_description: str | None
    fiscal_year_end: str
    """``"MMDD"``, e.g. ``"0928"``. A string, not a date: it has no year, and constructing one
    would invent a fiscal year for a filer that never stated it."""
    tickers: tuple[str, ...]
    exchanges: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FilingRow:
    """One filing, from either the main payload's ``filings.recent`` or an overflow page.

    One row type for both, because they carry the same columns — only the wrapper differs.

    Attributes:
        filed: ``filingDate``. DESIGN.md §4.2(b)'s ``as_of`` key.
        items: 8-K item codes that matched :data:`ITEM_CODE`.
        items_raw: The value as filed, **never discarded**. A parser that recognizes nothing and
            drops the original has destroyed the evidence that it failed. The parse rate is
            reported in the fetch summary, so a format change shows up as a number dropping rather
            than as flags quietly ceasing to fire.
    """

    accession: Accession
    form: str
    filed: date
    report_date: date | None
    accepted_at: datetime | None
    primary_document: str
    items: tuple[str, ...]
    items_raw: str
    is_xbrl: bool | None
    is_inline_xbrl: bool | None
    size: int | None

    def primary_url(self, cik: int) -> str:
        """URL of the machine-readable primary document.

        Applies the ``xsl*/`` strip for forms 3/4/5 — see
        :func:`~investo.ingest.edgar.client.ownership_doc`. Resolving this in M1a even though M1a
        never fetches a filing body is deliberate: it exercises the ``/Archives/`` URL transforms,
        which retires the padding risk ROADMAP M1 names, in the milestone that ships first.
        """
        document = ownership_doc(self.primary_document, form=self.form)
        return archives_doc_url(cik, self.accession, document)


@dataclass(frozen=True, slots=True)
class FilesEntry:
    """One overflow page's descriptor, from ``filings.files[]``."""

    name: str
    filing_count: int | None
    filing_from: date | None
    filing_to: date | None


def parse_submissions(
    body: bytes, *, source: SourceContext
) -> tuple[CompanyProfile, tuple[FilingRow, ...], tuple[FilesEntry, ...]]:
    """Parse the main payload: company metadata, ``filings.recent``, and the overflow descriptors.

    Returns the ``files[]`` entries as a third element rather than leaving the caller to re-read
    the payload. ``docs/m1/04-parsers.md`` sketches a two-tuple, but deciding *which* pages to
    fetch is the caller's job and it cannot decide without the descriptors — and re-parsing a
    multi-megabyte payload to get them would be the alternative. **[extends the signature in
    ``docs/m1/04-parsers.md``.]**

    Raises:
        UpstreamFetchError: on a malformed payload, including columns of unequal length. Exit 4.
    """
    try:
        payload: Any = json.loads(body)
    except json.JSONDecodeError as exc:
        raise UpstreamFetchError(f"submissions payload is not valid JSON: {exc}") from exc

    filings = payload.get("filings") if isinstance(payload, dict) else None
    if not isinstance(filings, dict):
        raise UpstreamFetchError(
            "submissions payload has no `filings` object.",
            hint=(
                "An overflow page is a flat columnar object with no `filings` wrapper — use "
                "parse_submissions_page for those. The two shapes are not interchangeable."
            ),
        )

    try:
        profile = _to_profile(payload)
    except ValueError as exc:
        raise UpstreamFetchError(f"submissions payload is malformed: {exc}") from exc

    recent = filings.get("recent")
    if not isinstance(recent, dict):
        raise UpstreamFetchError("submissions: `filings.recent` must be a JSON object.")
    rows = _from_columns(recent, source=source, where="filings.recent")
    files = parse_files(filings.get("files"))
    return profile, rows, files


def parse_submissions_page(body: bytes, *, source: SourceContext) -> tuple[FilingRow, ...]:
    """Parse one overflow page. **A flat columnar object; no profile to return.**

    Confirmed empirically, and on the flagship fixture:
    ``https://data.sec.gov/submissions/CIK0000320193-submissions-001.json`` returns HTTP 200 and
    its first accessions are 2015 filings — so Apple's own ``filings.recent`` does not reach 2015,
    and a 10y lookback on AAPL reads an incomplete filing history unless the caller paginates.

    The page begins ``{"accessionNumber":[...]`` — no ``filings`` wrapper, no ``recent``, and none
    of the company metadata. One function cannot parse both shapes, which is why there are two.

    Raises:
        UpstreamFetchError: if handed a wrapped main payload, or on unequal column lengths. Exit 4.
    """
    try:
        payload: Any = json.loads(body)
    except json.JSONDecodeError as exc:
        raise UpstreamFetchError(f"submissions page is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpstreamFetchError("submissions page must be a JSON object.")
    if "filings" in payload:
        raise UpstreamFetchError(
            "This is a main submissions payload, not an overflow page.",
            hint="Use parse_submissions for a payload with a `filings` wrapper.",
        )
    return _from_columns(payload, source=source, where="submissions page")


def parse_files(value: object) -> tuple[FilesEntry, ...]:
    """Parse ``filings.files[]``. ``None`` or ``[]`` yields ``()`` and no request is made.

    Every entry must carry all of :data:`FILES_ENTRY_FIELDS`, and a missing key raises with the key
    named. That strictness is the point: the field names are the one unconfirmed shape in M1a, so
    the first real overflow payload should produce an error that *states the divergence* rather than
    a silently skipped page — which would look like a company that stopped filing.

    Raises:
        UpstreamFetchError: if an entry is not an object, or lacks a documented field. Exit 4.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise UpstreamFetchError(
            f"submissions: `filings.files` must be a list, got {type(value).__name__}."
        )
    entries: list[FilesEntry] = []
    for position, item in enumerate(value):
        if not isinstance(item, dict):
            raise UpstreamFetchError(f"submissions: `filings.files[{position}]` is not an object.")
        missing = [field for field in FILES_ENTRY_FIELDS if field not in item]
        if missing:
            raise UpstreamFetchError(
                f"submissions: `filings.files[{position}]` is missing {missing}; it has "
                f"{sorted(item)}.",
                hint=(
                    "These field names were taken from SEC's prose rather than an observed "
                    "payload — see FILES_ENTRY_FIELDS and docs/m1/04-parsers.md § Pagination. The "
                    "keys listed above are the real ones: update FILES_ENTRY_FIELDS and "
                    "parse_files, then write the finding into that document."
                ),
            )
        try:
            entries.append(
                FilesEntry(
                    name=str(item["name"]),
                    filing_count=as_optional_int(item["filingCount"]),
                    filing_from=as_date(item["filingFrom"]),
                    filing_to=as_date(item["filingTo"]),
                )
            )
        except ValueError as exc:
            raise UpstreamFetchError(
                f"submissions: `filings.files[{position}]` has an unreadable value: {exc}"
            ) from exc
    return tuple(entries)


def pages_needed(files: Sequence[FilesEntry], *, window: tuple[date, date]) -> tuple[str, ...]:
    """The overflow page names whose date range intersects ``window``.

    ``filings.recent`` is **not the whole history.** SEC's documentation: the property path holds
    *"at least one year's of filing or to 1,000 (whichever is more) of the most recent filings"*,
    and *"if the entity has additional filings, ``files`` will contain an array of additional JSON
    files and the date range for the filings each one contains."*

    For a filer with heavy Form 4 traffic, 1,000 filings can be under three years — so a 5y window
    puts older 8-Ks in the overflow, and a ``recent``-only filter finds nothing wrong with a company
    that filed an Item 4.02 four years ago. Since DESIGN.md §6.6 calls 4.02 the loudest signal in
    the system, that is not a detail. Recorded as spec question 1; §6.6 gains a sentence.

    Intersection is **inclusive at both ends**, so a window whose start equals a page's
    ``filingTo`` includes that page — the boundary case, and it gets its own test. A page with an
    unreadable range is included rather than skipped: fetching one page too many costs a request,
    and skipping one costs filings.

    Deciding which pages to fetch is the caller's; this function does not fetch. Cost is one to
    three extra requests per company.
    """
    start, end = window
    wanted: list[str] = []
    for entry in files:
        page_from = entry.filing_from
        page_to = entry.filing_to
        if page_from is None or page_to is None:
            wanted.append(entry.name)
            continue
        if page_to >= start and page_from <= end:
            wanted.append(entry.name)
    return tuple(wanted)


def merge_pages(*groups: Sequence[FilingRow]) -> tuple[FilingRow, ...]:
    """Concatenate, dedup by accession, and sort by ``filed`` descending.

    Dedups because the page boundaries are SEC's and nothing guarantees they do not overlap. The
    first occurrence wins, so passing ``recent`` first keeps the newest copy of a row that appears
    twice.

    The sort breaks ties on the accession string so the order is total. Two filings on one day are
    common, and an unstable sort here would make the fetch summary's row order — and any fixture
    asserting on it — differ between runs.
    """
    seen: dict[str, FilingRow] = {}
    for group in groups:
        for row in group:
            seen.setdefault(row.accession.value, row)
    return tuple(sorted(seen.values(), key=lambda r: (r.filed, r.accession.value), reverse=True))


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _to_profile(payload: Mapping[str, Any]) -> CompanyProfile:
    tickers = payload.get("tickers")
    exchanges = payload.get("exchanges")
    return CompanyProfile(
        cik=as_cik(require(payload, "cik", where="submissions")),
        name=str(payload.get("name") or ""),
        # `sic` is a string here and can be "", so as_optional_int rather than int().
        sic=as_optional_int(payload.get("sic")),
        sic_description=as_optional_str(payload.get("sicDescription")),
        fiscal_year_end=str(payload.get("fiscalYearEnd") or ""),
        tickers=tuple(str(item) for item in tickers) if isinstance(tickers, list) else (),
        exchanges=tuple(str(item) for item in exchanges) if isinstance(exchanges, list) else (),
    )


def _from_columns(
    columns: Mapping[str, Any], *, source: SourceContext, where: str
) -> tuple[FilingRow, ...]:
    """Transpose parallel arrays into rows.

    **All arrays must be the same length, and this is checked.** A parser that zips them without
    checking truncates to the shortest — silently losing the tail of the filing history, which
    looks exactly like a company that stopped filing.

    Exit 4 rather than a coverage note: a malformed payload is not an absence, and a partial filing
    history whose extent we cannot determine is worse than no run.
    """
    arrays = {name: value for name, value in columns.items() if isinstance(value, list)}
    missing = [name for name in _REQUIRED_COLUMNS if name not in arrays]
    if missing:
        raise UpstreamFetchError(f"{where}: missing column(s) {missing}; found {sorted(arrays)}.")

    lengths = {name: len(column) for name, column in arrays.items()}
    if len(set(lengths.values())) != 1:
        raise UpstreamFetchError(
            f"{where}: columns disagree in length: {lengths}",
            hint=(
                "Zipping these would truncate to the shortest column and silently drop the tail "
                "of the filing history — which looks like a company that stopped filing."
            ),
        )

    count = next(iter(lengths.values()))
    rows: list[FilingRow] = []
    for position in range(count):

        def cell(name: str, index: int = position) -> object:
            column = arrays.get(name)
            return column[index] if column is not None else None

        try:
            filed = as_date(cell("filingDate"))
            if filed is None:
                continue
            accession = Accession.parse(str(cell("accessionNumber")))
            items_raw = str(cell("items") or "")
            rows.append(
                FilingRow(
                    accession=accession,
                    form=str(cell("form") or ""),
                    filed=filed,
                    report_date=as_date(cell("reportDate")),
                    accepted_at=as_datetime(cell("acceptanceDateTime")),
                    primary_document=str(cell("primaryDocument") or ""),
                    items=_parse_items(items_raw),
                    items_raw=items_raw,
                    is_xbrl=as_bool(cell("isXBRL")),
                    is_inline_xbrl=as_bool(cell("isInlineXBRL")),
                    size=as_optional_int(cell("size")),
                )
            )
        except ValueError as exc:
            raise UpstreamFetchError(f"{where} row {position} is malformed: {exc}") from exc
    del source  # rows carry no SourceRef; filings are located by accession, not by payload URL
    return tuple(rows)


def _parse_items(raw: str) -> tuple[str, ...]:
    """Split ``"2.02,9.01"`` into codes, keeping only well-formed ones.

    The observed degenerate value is ``",,"`` — two commas, three empty tokens, on an ``EFFECT``
    filing. A naive ``split(",")`` yields ``["", "", ""]``; filtering on :data:`ITEM_CODE` discards
    all three, and no code was ever meant to fire on an ``EFFECT``.
    """
    return tuple(
        token for token in (part.strip() for part in raw.split(",")) if ITEM_CODE.match(token)
    )

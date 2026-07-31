"""Filing bodies -> Item sections (M1b).

Primary document URL: ``/Archives/edgar/data/{cik_unpadded}/{accession_nodashes}/{primaryDocument}``
— both transforms owned by the client, and the ``xsl*/`` strip for forms 3/4/5 applied by
:func:`~investo.ingest.edgar.client.ownership_doc`.

Items split: 1, 1A, 1C, 3, 7, 7A, 8, 9A (DESIGN.md §7.4). §7.4 also records *why* item-level
chunking: **for cost and precision, not context limits.**

**The heading regex is brittle across filers, and this design accepts that rather than fighting
it.** ROADMAP M1 and §7.4 both say: collect failures as fixtures rather than chasing generality up
front. So :attr:`FilingDocument.split_ok` is a field, :attr:`FilingDocument.unrecognized` is a
field, and the parse rate is reported. A filing that will not split is a filing whose narrative
sections are absent from the report — a **coverage fact** — not an aborted run.

The text normalizer is one named function with its own test, and **M6 must call the same one.**
DESIGN.md §7.3 requires verbatim quote verification against this text, so the normalization has to
be stable and recorded: a quote verified under one normalization and searched under another fails
for no reason, and the citation verifier would then reject true claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from investo.domain.provenance import Accession
from investo.errors import ConfigError

__all__ = [
    "TENK_ITEMS",
    "FilingDocument",
    "normalize_text",
    "extract_text",
    "split_items",
]

TENK_ITEMS: Final = ("1", "1A", "1C", "3", "7", "7A", "8", "9A")
"""The 10-K items DESIGN.md §7.4 names. Order is document order, which is also split order."""

_HEADING = re.compile(
    r"""
    ^[ \t]*
    (?:PART\s+[IVX]+[ \t.—-]*)?     # an optional "PART II" prefix on the same line
    ITEM[ \t]*
    (?P<number>\d{1,2}[A-C]?)            # 1, 1A, 7A, 9A ...
    [ \t]*[.:–—-]*[ \t]*
    (?P<title>[^\n]{0,120})
    $
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

_TAG = re.compile(r"<[^>]+>")
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_WHITESPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class FilingDocument:
    """One filing's narrative, split by Item heading.

    Attributes:
        items: ``"1A" -> text``. Only headings that matched and produced non-empty text.
        unrecognized: Headings found in the document that did not map to a wanted item — kept so a
            format change is visible as data rather than as a section that quietly disappeared.
        split_ok: Whether every item in :data:`TENK_ITEMS` was found. ``False`` is a coverage fact.
        text: The normalized full text, retained because §7.3's citation verifier searches it and
            because a quote may span an item boundary.
    """

    accession: Accession
    form: str
    items: dict[str, str]
    unrecognized: tuple[str, ...]
    split_ok: bool
    text: str

    @property
    def parse_rate(self) -> float:
        """Fraction of :data:`TENK_ITEMS` found. Reported in the fetch summary, so a format change
        shows up as a number dropping rather than as a feature that stopped working."""
        return len(set(self.items) & set(TENK_ITEMS)) / len(TENK_ITEMS)


def normalize_text(text: str) -> str:
    """The single, stable text normalization. **M6 must call this exact function.**

    Collapses runs of horizontal whitespace, normalizes ``&nbsp;`` and the other entities EDGAR
    emits, strips trailing spaces, and caps consecutive blank lines at two. Line breaks are
    *preserved* because the Item-heading regex is line-anchored, and flattening them would make
    every heading unfindable.

    Deterministic and idempotent: ``normalize_text(normalize_text(x)) == normalize_text(x)``, which
    gets its own test. Without idempotence, a quote normalized once and searched in text normalized
    twice would not match, and §7.3's verifier would reject a true citation.
    """
    cleaned = (
        text.replace(" ", " ")
        .replace("&nbsp;", " ")
        .replace("&#160;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&rsquo;", "’")
        .replace("&ldquo;", "“")
        .replace("&rdquo;", "”")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    cleaned = _WHITESPACE.sub(" ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))
    return _BLANK_LINES.sub("\n\n", cleaned).strip()


def extract_text(body: bytes) -> str:
    """HTML (or iXBRL, or plain text) -> normalized text.

    Uses ``lxml`` when it is available and falls back to a regex strip when it is not. The fallback
    exists because ``lxml`` is M1b's one new runtime dependency and the fallback keeps
    :func:`split_items` testable from a plain-text fixture without it — **not** because the two are
    interchangeable. ``lxml`` is what handles the tag soup EDGAR filings actually contain, and the
    difference shows up on real filings rather than on fixtures.
    """
    try:
        # Imported here so `split_items` stays testable from a plain-text fixture without lxml
        # present. See the fallback below for why that is a convenience and not equivalence.
        from lxml import html as lxml_html
    except ImportError:
        stripped = _SCRIPT_OR_STYLE.sub(" ", body.decode("utf-8", errors="replace"))
        return normalize_text(_TAG.sub("\n", stripped))

    text = body.decode("utf-8", errors="replace")
    if "<" not in text:
        return normalize_text(text)
    try:
        tree = lxml_html.fromstring(text)
    # Broad on purpose: lxml raises several unrelated types on tag soup, and enumerating them
    # means the next one it invents becomes an unhandled crash on a real filing.
    except Exception as exc:
        raise ConfigError(
            f"Could not parse a filing document as HTML: {exc}",
            hint="Keep the payload as a fixture — an unparseable filing is a case worth having.",
        ) from exc
    for element in tree.xpath("//script | //style"):
        element.drop_tree()
    return normalize_text(tree.text_content())


def split_items(
    text: str, *, form: str, accession: Accession, wanted: tuple[str, ...] = TENK_ITEMS
) -> FilingDocument:
    """Split normalized filing text into Item sections.

    Each heading match starts a section that runs to the next heading. A repeated heading — the
    table of contents lists every Item before the body does — keeps the **longest** section, which
    is a heuristic and is labelled as one: a table-of-contents entry is a line, and the real section
    is paragraphs. It is right on every filing tried and it will be wrong on some filing, at which
    point that filing becomes a fixture.

    Never raises on a document it cannot split. ``split_ok=False`` and a low
    :attr:`FilingDocument.parse_rate` are the outputs; an exception here would turn a narrative gap
    into a failed run, which DESIGN.md §14's distinction forbids.
    """
    sections: dict[str, list[str]] = {}
    unrecognized: list[str] = []
    matches = list(_HEADING.finditer(text))

    for position, match in enumerate(matches):
        number = match.group("number").upper()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        section = text[match.end() : end].strip()
        if number in wanted:
            sections.setdefault(number, []).append(section)
        else:
            unrecognized.append(f"Item {number}")

    items = {
        number: max(candidates, key=len)
        for number, candidates in sections.items()
        if max(candidates, key=len).strip()
    }
    return FilingDocument(
        accession=accession,
        form=form,
        items=items,
        unrecognized=tuple(dict.fromkeys(unrecognized)),
        split_ok=set(wanted).issubset(items),
        text=text,
    )

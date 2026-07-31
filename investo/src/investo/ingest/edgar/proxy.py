"""DEF 14A -> pay-versus-performance facts, plus narrative text for M6 (M1b).

One structured numeric source and a lot of narrative, and the split matters:

**Pay Versus Performance (Item 402(v)) is inline-XBRL tagged** via the ECD taxonomy, for fiscal years
ending on or after **2022-12-16** (Release 34-95607). Extracted with ``lxml`` by reading the iXBRL
facts. **This is the only numeric extraction in a proxy.**

**Everything else is untagged narrative** — Summary Compensation Table, CD&A, pay ratio, audit fees.
The text is extracted and handed on; it is an M6 LLM target, not a data feed. This module produces
**no numbers from narrative**, and that is a rule rather than a limitation: a compensation figure
read out of a table by a regex has no provenance a reader could check, which DESIGN.md §3.2 forbids.

**Beneficial ownership is not read from the proxy.** §6.8: Form 4 and 13D/G XML are the better
source. Parsing the proxy's narrative table too would create a second answer to the same question,
and the failure mode of two answers is that nobody knows which the report printed.

A company with no DEF 14A in the window is an **absence**, printed in the fetch summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from investo.domain.provenance import Accession, SourceContext, SourceRef
from investo.ingest.edgar.documents import extract_text

__all__ = [
    "ECD_TAXONOMY_PREFIX",
    "PVP_REQUIRED_FROM",
    "PayVersusPerformanceFact",
    "ProxyDocument",
    "parse_proxy",
]

ECD_TAXONOMY_PREFIX: Final = "ecd"
"""The Executive Compensation Disclosure taxonomy prefix used by Item 402(v) iXBRL facts."""

PVP_REQUIRED_FROM: Final = date(2022, 12, 16)
"""Fiscal years ending on or after this date must tag pay-versus-performance (Release 34-95607).

A proxy older than this legitimately has no ``ecd`` facts, so an empty result is an absence rather
than a parse failure — and the two are recorded differently.
"""

_IXBRL_FACT: Final = re.compile(
    r"<ix:(?:non)?[Ff]raction\b[^>]*name=[\"'](?P<name>[^\"']+)[\"'][^>]*>(?P<value>.*?)</ix:",
    re.IGNORECASE | re.DOTALL,
)
_NUMERIC: Final = re.compile(r"-?[\d,]+(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class PayVersusPerformanceFact:
    """One iXBRL fact from the pay-versus-performance table.

    ``tag`` keeps the ECD taxonomy's own name. Mapping ``ecd:PeoTotalCompAmt`` to a meaning is
    analysis, and this module does not do it — the same seam ``companyfacts.py`` observes for
    ``us-gaap``.
    """

    taxonomy: str
    tag: str
    value: Decimal
    context: str | None
    source: SourceRef


@dataclass(frozen=True, slots=True)
class ProxyDocument:
    """One DEF 14A.

    Attributes:
        pvp_facts: Structured Item 402(v) numbers, with provenance. Empty for a proxy predating
            :data:`PVP_REQUIRED_FROM`, and empty is an absence.
        text: Normalized narrative, via the **same** normalizer ``documents.py`` uses — DESIGN.md
            §7.3's citation verifier must search the text it verified against.
        ixbrl_present: Whether any iXBRL fact at all was found. Distinguishes "no ``ecd`` facts in a
            document that has iXBRL" from "a document we could not read as iXBRL", which are
            different problems with different fixes.
    """

    accession: Accession
    filed: date
    pvp_facts: tuple[PayVersusPerformanceFact, ...]
    text: str
    ixbrl_present: bool


def parse_proxy(
    body: bytes, *, source: SourceContext, accession: Accession, filed: date
) -> ProxyDocument:
    """Parse a DEF 14A: the ECD facts, and the narrative as text.

    The iXBRL extraction is a regex over the raw markup rather than an XPath over a parsed tree,
    which is a deliberate trade. EDGAR proxies are tag soup that ``lxml`` will parse but will
    sometimes re-namespace, and ``ix:`` elements are frequently unclosed relative to what an HTML
    parser expects. Reading the attributes off the raw markup is more robust on the documents that
    actually exist, and it is honest about being a heuristic: :attr:`ProxyDocument.ixbrl_present`
    reports whether anything matched, so a total miss is visible rather than silent.
    """
    markup = body.decode("utf-8", errors="replace")
    facts: list[PayVersusPerformanceFact] = []
    matched_any = False

    for match in _IXBRL_FACT.finditer(markup):
        matched_any = True
        qualified = match.group("name")
        taxonomy, _, tag = qualified.partition(":")
        if taxonomy.lower() != ECD_TAXONOMY_PREFIX:
            continue
        # iXBRL spells a negative fact two ways and both occur in compensation tables: parentheses
        # in the rendered text, and a `sign="-"` attribute on the element with unsigned text.
        # Reading only the first flips the sign on every negative adjustment tagged the second way.
        value = _to_decimal(match.group("value"))
        if value is None:
            continue
        if _attribute(match.group(0), "sign") == "-" and value > 0:
            value = -value
        facts.append(
            PayVersusPerformanceFact(
                taxonomy=taxonomy,
                tag=tag,
                value=value,
                context=_attribute(match.group(0), "contextRef"),
                source=source.ref(
                    accession=accession,
                    form="DEF 14A",
                    filed=filed,
                    taxonomy=taxonomy,
                    tag=tag,
                ),
            )
        )

    return ProxyDocument(
        accession=accession,
        filed=filed,
        pvp_facts=tuple(facts),
        text=extract_text(body),
        ixbrl_present=matched_any,
    )


def _attribute(element: str, name: str) -> str | None:
    found = re.search(rf"{name}=[\"']([^\"']+)[\"']", element, re.IGNORECASE)
    return found.group(1) if found else None


def _to_decimal(inner: str) -> Decimal | None:
    """Pull a number out of an iXBRL fact's rendered content.

    The displayed text carries commas, currency symbols and sometimes footnote markers, and
    parentheses mean negative in a compensation table. Handled explicitly, because reading
    ``(1,234)`` as positive 1234 would flip the sign on every negative adjustment in the table.
    """
    text = inner.strip()
    negative = text.startswith("(") and text.rstrip().endswith(")")
    found = _NUMERIC.search(text)
    if found is None:
        return None
    try:
        value = Decimal(found.group(0).replace(",", ""))
    except InvalidOperation:
        return None
    return -value if negative else value

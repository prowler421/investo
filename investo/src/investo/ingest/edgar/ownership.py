"""Forms 4, 13D/G and 13F -> ownership rows (M1b).

All XML; ``xml.etree.ElementTree`` from the stdlib, no dependency. Only the DEF 14A path needs
``lxml``, and that is ``proxy.py``.

Three sources, three different shapes of incompleteness, each recorded rather than smoothed over:

**Form 4** — XML since 2003, two-business-day lag. DESIGN.md §6.8: keep the open-market codes ``P``
and ``S``; drop ``A``, ``M``, ``F`` and ``G`` (grants, exercises, tax withholding, gifts) as noise.
10b5-1 planned sales are flagged and excluded from the signal, because a sale scheduled a year ago
says nothing about what the insider thinks today. Amendments (``4/A``) dedup by
``(reporter, transaction date, code)`` with the newest ``filed`` winning.

**13D/G** — structured XML only since **2024-12-18** (Beneficial Ownership Reporting Modernization).
A 5y window straddles that boundary, so pre-2024 filings are narrative HTML. This module returns
rows for the structured era and reports the pre-boundary filings as
:attr:`OwnershipSummary.unparsed_count` rather than pretending the history is complete.

**13F** — 45-day lag, long-only US equity, "as filed" and may contain inconsistencies per SEC.
Positions may be fully unwound before publication. Parsed, and the lag is printed wherever the
number is.

The P/S filtering rule lives here rather than in ``analyze/`` because it is a property of the
*source format* — which transaction codes mean an open-market trade — not an analysis. The judgment
about what a cluster of sales *means* is M4.5's.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from investo.domain.provenance import Accession
from investo.errors import UpstreamFetchError

__all__ = [
    "OPEN_MARKET_CODES",
    "NOISE_CODES",
    "STRUCTURED_13DG_FROM",
    "THIRTEEN_F_LAG_DAYS",
    "InsiderTransaction",
    "BeneficialOwner",
    "InstitutionalPosition",
    "OwnershipSummary",
    "parse_form4",
    "parse_13dg",
    "parse_13f",
    "dedup_amendments",
]

OPEN_MARKET_CODES: Final = frozenset({"P", "S"})
"""Purchase and sale on the open market. The only codes that carry signal (§6.8)."""

NOISE_CODES: Final = frozenset({"A", "M", "F", "G", "C", "D", "I", "J"})
"""Grants, option exercises, tax withholding, gifts, conversions, dispositions to the issuer.

Enumerated rather than derived as "not P/S", so a code SEC adds shows up as *neither* — visible in
:attr:`OwnershipSummary.other_codes` instead of silently classified as noise.
"""

STRUCTURED_13DG_FROM: Final = date(2024, 12, 18)
"""The Beneficial Ownership Reporting Modernization compliance date.

Filings before this are narrative HTML and are counted, not parsed. Hardcoded as a date rather than
inferred from whether the parse succeeded, so a *parser* bug on a post-boundary filing cannot be
mistaken for a pre-boundary filing.
"""

THIRTEEN_F_LAG_DAYS: Final = 45
"""§6.8: 13F is filed up to 45 days after quarter end, so a position may be fully unwound before it
is published. Printed wherever a 13F number is."""

_10B5_1_FLAGS: Final = ("10b5-1", "10b5_1", "rule 10b5")


@dataclass(frozen=True, slots=True)
class InsiderTransaction:
    """One Form 4 transaction line."""

    accession: Accession
    filed: date
    reporter: str
    reporter_is_officer: bool
    reporter_is_director: bool
    transaction_date: date | None
    code: str
    shares: Decimal | None
    price_per_share: Decimal | None
    acquired: bool | None
    """``True`` for A (acquired), ``False`` for D (disposed). SEC's own flag, not inferred from the
    code — an ``S`` with an ``A`` flag is a payload worth seeing rather than silently reclassifying.
    """
    planned_10b5_1: bool

    @property
    def is_open_market(self) -> bool:
        return self.code.upper() in OPEN_MARKET_CODES

    @property
    def carries_signal(self) -> bool:
        """Open-market and not a scheduled sale. This is the filter §6.8 asks for."""
        return self.is_open_market and not self.planned_10b5_1


@dataclass(frozen=True, slots=True)
class BeneficialOwner:
    """One 13D/G filer's reported stake."""

    accession: Accession
    filed: date
    form: str
    owner: str
    shares: Decimal | None
    percent_of_class: Decimal | None


@dataclass(frozen=True, slots=True)
class InstitutionalPosition:
    """One 13F holding line."""

    accession: Accession
    filed: date
    manager: str
    issuer: str
    cusip: str
    value: Decimal | None
    shares: Decimal | None


@dataclass(frozen=True, slots=True)
class OwnershipSummary:
    """Everything parsed, plus an honest count of what was not.

    ``unparsed_count`` and ``other_codes`` are the fields that keep this module's incompleteness
    visible. §6.8's three sources each have a boundary, and a summary that reported only what parsed
    would look complete on a window that straddles one.
    """

    insider: tuple[InsiderTransaction, ...] = ()
    owners: tuple[BeneficialOwner, ...] = ()
    positions: tuple[InstitutionalPosition, ...] = ()
    unparsed_count: int = 0
    other_codes: frozenset[str] = field(default_factory=frozenset)

    @property
    def signal_transactions(self) -> tuple[InsiderTransaction, ...]:
        return tuple(item for item in self.insider if item.carries_signal)


def parse_form4(
    body: bytes, *, accession: Accession, filed: date
) -> tuple[InsiderTransaction, ...]:
    """Parse one Form 4 (or 3, or 5) XML document.

    Reads non-derivative transactions only. Derivative tables carry option grants and exercises,
    which §6.8 classifies as noise — and a derivative row's ``shares`` is a *contract* count, so
    summing it with a common-share count produces a number that means nothing.
    """
    root = _parse_xml(body, what="Form 4")
    owner = root.find("reportingOwner")
    name = _text(owner, "reportingOwnerId/rptOwnerName") if owner is not None else None
    relationship = owner.find("reportingOwnerRelationship") if owner is not None else None
    is_officer = _flag(relationship, "isOfficer")
    is_director = _flag(relationship, "isDirector")
    footnotes = " ".join(node.text or "" for node in root.iter("footnote")).lower()
    remarks = (_text(root, "remarks") or "").lower()
    planned = any(marker in footnotes or marker in remarks for marker in _10B5_1_FLAGS)

    transactions: list[InsiderTransaction] = []
    for node in root.iter("nonDerivativeTransaction"):
        code = _text(node, "transactionCoding/transactionCode") or ""
        acquired_flag = _text(node, "transactionAmounts/transactionAcquiredDisposedCode/value")
        transactions.append(
            InsiderTransaction(
                accession=accession,
                filed=filed,
                reporter=name or "",
                reporter_is_officer=bool(is_officer),
                reporter_is_director=bool(is_director),
                transaction_date=_date(_text(node, "transactionDate/value")),
                code=code.strip().upper(),
                shares=_decimal(_text(node, "transactionAmounts/transactionShares/value")),
                price_per_share=_decimal(
                    _text(node, "transactionAmounts/transactionPricePerShare/value")
                ),
                acquired=None if acquired_flag is None else acquired_flag.strip().upper() == "A",
                planned_10b5_1=planned,
            )
        )
    return tuple(transactions)


def dedup_amendments(
    transactions: tuple[InsiderTransaction, ...],
) -> tuple[InsiderTransaction, ...]:
    """Collapse ``4/A`` amendments, newest ``filed`` winning.

    Keyed on ``(reporter, transaction date, code)`` per §6.8. Not on shares or price, deliberately:
    an amendment usually exists *because* one of those was wrong, so including them in the key would
    keep both the error and the correction and double-count the trade.
    """
    newest: dict[tuple[str, date | None, str], InsiderTransaction] = {}
    for item in transactions:
        key = (item.reporter, item.transaction_date, item.code)
        current = newest.get(key)
        if current is None or item.filed > current.filed:
            newest[key] = item
    return tuple(
        sorted(newest.values(), key=lambda t: (t.transaction_date or t.filed, t.reporter, t.code))
    )


def parse_13dg(
    body: bytes, *, accession: Accession, filed: date, form: str
) -> tuple[BeneficialOwner, ...]:
    """Parse a structured 13D/G.

    Returns ``()`` for a filing before :data:`STRUCTURED_13DG_FROM` **without attempting a parse** —
    those are narrative HTML and a parse failure there would be indistinguishable from a real one.
    The caller counts them into :attr:`OwnershipSummary.unparsed_count`.
    """
    if filed < STRUCTURED_13DG_FROM:
        return ()
    root = _parse_xml(body, what=form)
    owners: list[BeneficialOwner] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in ("reportingPersonInfo", "reportingPerson"):
            continue
        # `_local`, not `find`, for the children too. Matching the container by local name while
        # reading its children by qualified path is worse than either choice consistently: a
        # namespaced 13D/G would then yield a BeneficialOwner with an empty name and `None`
        # amounts — a holder that looks parsed and carries nothing. The 13F path already reads by
        # local name for exactly this reason.
        owners.append(
            BeneficialOwner(
                accession=accession,
                filed=filed,
                form=form,
                owner=(_local(node, "reportingPersonName") or _local(node, "rptOwnerName") or ""),
                shares=_decimal(
                    _local(node, "aggregateAmountOwned") or _local(node, "aggregateAmount")
                ),
                percent_of_class=_decimal(_local(node, "percentOfClass")),
            )
        )
    return tuple(owners)


def parse_13f(
    body: bytes, *, accession: Accession, filed: date, manager: str = ""
) -> tuple[InstitutionalPosition, ...]:
    """Parse a 13F information table.

    "As filed" per SEC, which means inconsistencies are expected and are **not** corrected here. A
    row whose value will not parse keeps its ``None`` rather than being dropped, so the holding is
    still visible as a position even when the amount is not usable.
    """
    root = _parse_xml(body, what="13F")
    positions: list[InstitutionalPosition] = []
    for node in root.iter():
        if not node.tag.endswith("infoTable"):
            continue
        positions.append(
            InstitutionalPosition(
                accession=accession,
                filed=filed,
                manager=manager,
                issuer=_local(node, "nameOfIssuer") or "",
                cusip=_local(node, "cusip") or "",
                value=_decimal(_local(node, "value")),
                shares=_decimal(_local(node, "sshPrnamt")),
            )
        )
    return tuple(positions)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _parse_xml(body: bytes, *, what: str) -> ET.Element:
    """Parse EDGAR XML with the stdlib parser.

    ``xml.etree`` is used rather than ``defusedxml`` because ``ElementTree`` has not expanded
    external entities since Python 3.8 and does not resolve DTDs, so the billion-laughs and
    external-entity classes do not apply. The remaining exposure is a deeply nested document causing
    deep recursion, which is bounded by SEC's own document size limits and would fail loudly.

    Raises:
        UpstreamFetchError: on malformed XML. Exit 4.
    """
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise UpstreamFetchError(f"{what} document is not well-formed XML: {exc}") from exc


def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _local(node: ET.Element, name: str) -> str | None:
    """Find a child by *local* name, ignoring the namespace.

    13F documents are namespaced and filers disagree about the prefix, so a namespace-qualified path
    works for some filers and silently returns nothing for others — which reads as an empty
    portfolio.
    """
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1] == name and child.text:
            return child.text.strip() or None
    return None


def _flag(node: ET.Element | None, path: str) -> bool | None:
    raw = _text(node, path) or _text(node, f"{path}/value")
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true")


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _decimal(value: str | None) -> Decimal | None:
    """``Decimal`` from XML text. There is no ``float`` on this path at all — XML is text."""
    if not value:
        return None
    text = value.strip().replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None

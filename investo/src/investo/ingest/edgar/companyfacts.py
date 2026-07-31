"""``companyfacts`` -> :class:`~investo.domain.models.RawFact` rows, keyed by XBRL tag.

Source: ``https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json``. 10-40 MB for a large
filer.

**Confirmed against a live payload** — CIK 2093536 (ARXS), fetched 2026-07-31. Verbatim head::

    {"cik":"0002093536","entityName":"ARXIS, INC.","facts":{
      "ffd":{"NetFeeAmt":{"label":"","description":"","units":{"USD":[
          {"end":"2026-04-06","val":153994.53,"accn":"0001193125-26-146309",
           "fy":null,"fp":null,"form":"S-1/A","filed":"2026-04-08","frame":"CY2026Q1I"}, ...]}}},
      "us-gaap":{"AccountsPayableCurrent":{"label": ...
          "units":{"pure":[{"start":"2025-01-01","end":"2025-03-31","val":0.367,
           "accn":"0001193125-26-243043","fy":2026,"fp":"Q1","form":"10-Q",
           "filed":"2026-05-28","frame":"CY2025Q1"}, ...]}}}}}

The nesting is as DESIGN.md §4.2 assumes. Six details are not, and each shapes this module:

1. ``cik`` is a **zero-padded string** here, agreeing with ``submissions`` and disagreeing with
   ``company_tickers_exchange.json``. Normalized through ``_fields.as_cik``.
2. ``entityName`` is EDGAR-conformed uppercase (``"ARXIS, INC."``) where ``submissions.name`` gives
   ``"Arxis, Inc."``. **The display name comes from ``submissions``**; see
   :attr:`CompanyFacts.entity_name`.
3. There is a taxonomy beyond ``dei``/``us-gaap``/``srt``: **``ffd``** (Filing Fee Disclosure), and
   it sorts first so it is the first thing this parser sees. A taxonomy allowlist would have
   dropped it, and would drop the next one SEC adds.
4. ``dei`` was **absent entirely** from that filer's payload. So a NASDAQ filer can have no
   cover-page share count and therefore no market cap — an absence, not a ``KeyError`` and
   emphatically not a zero.
5. ``start`` is **absent on instant facts** — the key is missing, not ``null``. So
   ``row.get("start")``, never ``row["start"]``.
6. ``fy`` and ``fp`` are ``null`` on registration-statement facts, ``label``/``description`` can be
   ``""``, and ``form`` is not restricted to periodic reports (``S-1/A`` appears) — so nothing may
   filter facts by assuming ``10-K``/``10-Q``.

The §4.2(a) trap is present in that payload at minimum size: the ``us-gaap`` fact above covers
``2025-01-01``..``2025-03-31`` and carries ``fy: 2026, fp: "Q1"`` — a calendar-Q1-2025 period
labelled fiscal year 2026, because it was reported in a filing made in the issuer's fiscal 2026.

The unit key for per-share values is **``USD/shares``** here; the ``USD-per-shares`` spelling
belongs to ``frames`` URLs only.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from investo.domain.models import RawFact
from investo.domain.periods import FiscalPeriod
from investo.domain.provenance import Accession, SourceContext
from investo.errors import UpstreamFetchError
from investo.ingest.edgar._fields import as_cik, as_date, as_optional_int, as_optional_str, require

__all__ = ["CompanyFacts", "parse_companyfacts"]


@dataclass(frozen=True, slots=True)
class CompanyFacts:
    """Every XBRL fact SEC aggregated for one company.

    Keyed by ``(taxonomy, tag)`` rather than by tag, because ``Assets`` exists in more than one
    taxonomy and M2's chains name ``dei:`` and ``us-gaap:`` tags side by side.

    Attributes:
        cik: Normalized to ``int`` from the payload's padded string.
        entity_name: ``companyfacts.entityName``, retained **for provenance and debugging only**.
            The report's company name comes from ``submissions.name``; without that rule the cover
            page's casing depends on which parser ran last.

            It is also **not** an identity check that two payloads describe the same company —
            observation 2 above is the proof that it cannot be. Punctuation and casing differ
            legitimately between the two endpoints for plenty of real filers whose CIK matches
            perfectly, so a name comparison would raise on correct data. The identity check is on
            ``cik``.
        facts: ``(taxonomy, tag) -> facts``, each tuple ordered by ``(period.end, filed)``.
        tags_present: What was in the payload. A **missing tag is a coverage fact, not a failure** —
            DESIGN.md §4.2's whole argument is that hardcoding one tag per metric silently produces
            sparse data and a confidently wrong report.
        taxonomies_present: A ``frozenset[str]`` rather than a fixed set, because ``ffd`` was not
            anticipated and the next addition will not be either.
        facts_dropped: Rows this parser could not turn into a :class:`RawFact` — a value that is
            not a number, a date that will not parse. Counted rather than discarded silently, so a
            format change surfaces as a number rather than as sparse data.
    """

    cik: int
    entity_name: str
    facts: Mapping[tuple[str, str], tuple[RawFact, ...]]
    tags_present: frozenset[tuple[str, str]]
    taxonomies_present: frozenset[str]
    facts_dropped: int = 0

    @property
    def fact_count(self) -> int:
        return sum(len(rows) for rows in self.facts.values())

    def has(self, taxonomy: str, tag: str) -> bool:
        return (taxonomy, tag) in self.tags_present

    def get(self, taxonomy: str, tag: str) -> tuple[RawFact, ...]:
        """Facts for one tag, or an empty tuple. Absence is not an error."""
        return self.facts.get((taxonomy, tag), ())

    def all_facts(self) -> tuple[RawFact, ...]:
        """Every fact, ordered by ``(taxonomy, tag, period.end)``. Stable across runs."""
        ordered: list[RawFact] = []
        for key in sorted(self.facts):
            ordered.extend(self.facts[key])
        return tuple(ordered)


def parse_companyfacts(body: bytes, *, source: SourceContext) -> CompanyFacts:
    """Parse a ``companyfacts`` payload into typed rows. Assigns no metric.

    ``json.loads(body, parse_float=Decimal, parse_int=int)``. The hook matters more than it looks:
    ``json.loads`` materializes ``391035000000.01`` as a ``float`` before any of our code sees it,
    so ``Decimal(row["val"])`` is already too late — it converts a value that has already lost
    precision, and ``Decimal(0.1)`` is exact and wrong. ``parse_float`` is called with the *source
    text*, so no ``float`` is ever constructed::

        parse_float=Decimal : 391035000000.01
        Decimal(float)      : 391035000000.010009765625

    ``parse_int=int`` is stated explicitly rather than left default, so the pair reads as a
    deliberate policy about numbers rather than an incantation.

    Memory: 40 MB of JSON parses to a few hundred MB of Python objects. Accepted for M1 — this runs
    on a developer machine against one company at a time, and whole-market work is the nightly bulk
    ZIPs (§4.1), not a streaming parser. This function returns immutable tuples and drops the
    intermediate dict, so peak is at parse time and not sustained.

    Raises:
        UpstreamFetchError: if the payload is not an object, or ``cik``/``facts`` is absent. Exit
            4: a malformed payload is not an absence.
    """
    try:
        payload: Any = json.loads(body, parse_float=Decimal, parse_int=int)
    except json.JSONDecodeError as exc:
        raise UpstreamFetchError(f"companyfacts payload is not valid JSON: {exc}") from exc

    where = "companyfacts"
    try:
        cik = as_cik(require(payload, "cik", where=where))
        facts_section = require(payload, "facts", where=where)
    except ValueError as exc:
        raise UpstreamFetchError(f"companyfacts payload is malformed: {exc}") from exc
    if not isinstance(facts_section, dict):
        raise UpstreamFetchError("companyfacts: `facts` must be a JSON object.")

    entity_name = str(payload.get("entityName") or "")
    collected: defaultdict[tuple[str, str], list[RawFact]] = defaultdict(list)
    dropped = 0

    # Any taxonomy key is accepted and recorded. Selecting `us-gaap` over `ffd` is tag-chain
    # business, which is M2's — see docs/m1/README.md §5.
    for taxonomy, tags in facts_section.items():
        if not isinstance(tags, dict):
            continue
        for tag, definition in tags.items():
            if not isinstance(definition, dict):
                continue
            units = definition.get("units")
            if not isinstance(units, dict):
                continue
            for unit, rows in units.items():
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    fact = _to_fact(
                        row,
                        taxonomy=str(taxonomy),
                        tag=str(tag),
                        unit=str(unit),
                        source=source,
                    )
                    if fact is None:
                        dropped += 1
                    else:
                        collected[(str(taxonomy), str(tag))].append(fact)

    facts = {
        key: tuple(sorted(rows, key=lambda f: (f.period.end, f.source.filed)))
        for key, rows in collected.items()
    }
    return CompanyFacts(
        cik=cik,
        entity_name=entity_name,
        facts=facts,
        tags_present=frozenset(facts),
        taxonomies_present=frozenset(str(name) for name in facts_section),
        facts_dropped=dropped,
    )


def _to_fact(
    row: object,
    *,
    taxonomy: str,
    tag: str,
    unit: str,
    source: SourceContext,
) -> RawFact | None:
    """One fact row, or ``None`` if it cannot be interpreted.

    ``None`` rather than an exception, and counted by the caller. A single unparseable row in a
    40 MB payload is a coverage fact; aborting the run for it would make one bad row look like a
    network failure.
    """
    if not isinstance(row, dict):
        return None
    try:
        end = as_date(row.get("end"))
        filed = as_date(row.get("filed"))
        if end is None or filed is None:
            return None
        # `.get`, never `["start"]`: the key is *absent* on instant facts, which is what makes
        # classify(start=None, ...) -> INSTANT correct.
        start = as_date(row.get("start"))
        accession = Accession.parse(str(row["accn"]))
        value = _to_decimal(row.get("val"))
    except (KeyError, ValueError, InvalidOperation):
        return None
    if value is None:
        return None

    return RawFact(
        taxonomy=taxonomy,
        tag=tag,
        # Verbatim, not normalized and not mapped: "USD", "USD/shares", "shares", "pure".
        # DESIGN.md §4.2 twice warns that unit differences are value differences — revenue
        # excluding versus including assessed tax, and EPS arriving under USD/shares.
        unit=unit,
        value=value,
        period=FiscalPeriod.of(start, end),
        source=source.ref(
            accession=accession,
            form=str(row.get("form") or ""),
            filed=filed,
            taxonomy=taxonomy,
            tag=tag,
        ),
        # Carried and never used for grouping (§4.2a). These exist so a fixture can demonstrate
        # the trap: the payload above tags a calendar-Q1-2025 period `fy: 2026`.
        filing_fy=as_optional_int(row.get("fy")),
        filing_fp=as_optional_str(row.get("fp")),
        # Carried, and its use restricted: SEC's frame selection is not point-in-time stable, so
        # it is legitimate for peer cross-sections and illegitimate for the subject company's
        # history. Carrying it and forbidding one use beats dropping it, because M4's peers.py
        # genuinely wants it.
        frame=as_optional_str(row.get("frame")),
    )


def _to_decimal(value: object) -> Decimal | None:
    """A fact value as ``Decimal``, or ``None``.

    ``parse_float=Decimal`` means a non-integer arrives already a ``Decimal``, and an integer
    arrives as ``int``. A ``float`` reaching here means the parse hook was removed, so it is
    **rejected rather than converted** — converting it would launder the precision loss that the
    hook exists to prevent, and the test that asserts ``not isinstance(value, float)`` would still
    pass.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        return Decimal(text) if text else None
    return None

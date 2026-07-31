"""8-K events -> item codes and a body URL. **Extraction only** (M1b).

DESIGN.md §6.6's two-stage design maps cleanly onto the M1/M4.5 split: the codes come from
``submissions.py``'s already-parsed ``items``, so **detection needs no extra request at all** —
which is why §6.6 calls it the highest value per line of code in the system.

**This module has no severity table.** Mapping a code to a severity is M4.5's
``analyze/events.py``, and that separation is what keeps ingest replaceable. In particular Item 4.02
— non-reliance on previously issued financials, the loudest accounting red flag there is — is
detected here as a string and ranked there.

Only 4.01 (auditor change) and 5.02 (officer departure) need the filing body, and only once the LLM
layer exists to refine them: both are ambiguous by item code alone, since 5.02 covers CEO departures
*and* routine compensation amendments. Under ``--llm none`` they fire at a capped severity with
"unclassified, read the filing" (§6.6), so M4.5 does not block on M6.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from investo.domain.provenance import Accession
from investo.ingest.edgar.submissions import FilingRow

__all__ = [
    "EIGHT_K_FORMS",
    "BODY_REQUIRED_ITEMS",
    "EARNINGS_EXHIBIT_PREFIX",
    "FilingEvent",
    "extract_events",
    "item_parse_rate",
]

EIGHT_K_FORMS: Final = frozenset({"8-K", "8-K/A", "8-K12B", "8-K12G3", "8-K15D5"})
"""Forms whose ``items`` column carries 8-K item codes.

Amendments included: a 4.02 filed on an 8-K/A is the same signal as one on an 8-K, and dropping
amendments would miss a restatement announced as a correction — which is the common case.
"""

BODY_REQUIRED_ITEMS: Final = frozenset({"4.01", "5.02"})
"""Items whose code alone does not determine severity, so M4.5 wants the body.

Recorded here rather than in ``analyze/`` because *which* items need a fetch is a property of the
extraction plan, while what the body means is analysis. The severity itself lives in M4.5.
"""

EARNINGS_EXHIBIT_PREFIX: Final = "EX-99"
"""§6.6's parser note is normative: **enumerate all ``EX-99*`` rather than hardcoding ``99.1``**,
because the ``.1`` is filer convention and not rule.

Earnings releases arrive as item 2.02 with the release furnished as an ``EX-99*`` exhibit.
Guidance-only announcements are usually 7.01 (Reg FD) rather than 2.02.
"""


@dataclass(frozen=True, slots=True)
class FilingEvent:
    """One 8-K, reduced to what M4.5 needs.

    ``body_url`` is populated for :data:`BODY_REQUIRED_ITEMS` and ``None`` otherwise — the URL is
    *built*, never fetched. M1 fetching every 8-K body to satisfy a severity rule that does not
    exist yet would spend the rate budget on a question nobody is asking.
    """

    accession: Accession
    filed: date
    form: str
    items: tuple[str, ...]
    items_raw: str
    body_url: str | None

    @property
    def needs_body(self) -> bool:
        return bool(set(self.items) & BODY_REQUIRED_ITEMS)


def extract_events(rows: Sequence[FilingRow], *, cik: int) -> tuple[FilingEvent, ...]:
    """Every 8-K in ``rows``, newest first.

    Rows whose ``items`` parsed to nothing are **kept**, not dropped. An 8-K with no recognized
    code is exactly the evidence that the format changed, and discarding it would make
    :func:`item_parse_rate` report 100% on a payload it failed to read.
    """
    events: list[FilingEvent] = []
    for row in rows:
        if row.form.upper() not in EIGHT_K_FORMS:
            continue
        needs_body = bool(set(row.items) & BODY_REQUIRED_ITEMS)
        events.append(
            FilingEvent(
                accession=row.accession,
                filed=row.filed,
                form=row.form,
                items=row.items,
                items_raw=row.items_raw,
                body_url=row.primary_url(cik) if needs_body else None,
            )
        )
    return tuple(sorted(events, key=lambda e: (e.filed, e.accession.value), reverse=True))


def item_parse_rate(events: Iterable[FilingEvent]) -> tuple[int, int]:
    """``(events with at least one recognized code, total events)``.

    Reported in the fetch summary. A format change then surfaces as a ratio dropping rather than as
    flags quietly ceasing to fire — which is the whole reason ``items_raw`` is retained.
    """
    total = 0
    parsed = 0
    for event in events:
        total += 1
        if event.items:
            parsed += 1
    return parsed, total

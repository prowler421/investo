"""investo — fundamental due-diligence reports for NASDAQ-listed companies, from SEC filings.

Through **M1**: the typer CLI shell and the pydantic-settings config layer (M0), the DESIGN.md §14
exit-code taxonomy (M0), the domain types every later milestone is written against, and the ingest
layer — a content-addressed cache, the single EDGAR client, the parsers, and three price adapters.
``investo fetch TICKER`` works; ``analyze`` and ``facts`` still report the milestone that
implements them.

**Nothing here interprets a financial figure yet.** M1 fetches bytes, records where they came from
and when, and turns them into typed rows keyed by XBRL tag. Choosing which tag answers to "revenue"
is ``normalize/tags.py``, which is M2, and the seam is enforced by an AST test rather than by
convention — see :mod:`investo.ingest`.

``DESIGN.md`` is normative on architecture and ``ROADMAP.md`` on sequencing; on any conflict
between them and a comment in this package, the documents govern. The module tree in
DESIGN.md §3.1 is created per milestone rather than up front — an empty package cannot be
type-checked or tested, and goes stale before it is filled.

Two properties the design treats as non-negotiable, restated here because everything added
later has to preserve them:

- **Every number traces to a source.** If a figure cannot carry its accession number, XBRL tag
  and fetch timestamp, it does not get printed.
- **The LLM cannot touch the numbers.** All figures come from deterministic math; the LLM's
  output schema has no numeric field feeding anything downstream.
"""

from investo import cli, config, domain, errors, ingest

# Dependency order — primitives first, then what is built on them — which reads as documentation of
# the layer. RUF022's alphabetical sort is disabled in pyproject for exactly this reason.
__all__ = ["errors", "config", "domain", "ingest", "cli"]

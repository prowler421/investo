"""investo — fundamental due-diligence reports for NASDAQ-listed companies, from SEC filings.

This is the M0 skeleton: the typer CLI shell, the pydantic-settings config layer, and the
DESIGN.md §14 exit-code taxonomy. Every command declared in :mod:`investo.cli` parses its full
documented flag surface and then reports the milestone that implements it.

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

from investo import cli, config, errors

__all__ = ["errors", "config", "cli"]

"""Domain types: the vocabulary every later milestone is written against (ROADMAP M1).

Three modules, built in dependency order:

``provenance``
    Where a number came from — :class:`~investo.domain.provenance.Accession`,
    :class:`~investo.domain.provenance.SourceRef`,
    :class:`~investo.domain.provenance.Derivation`.

``periods``
    When a number is about — :class:`~investo.domain.periods.FiscalPeriod` and the duration
    arithmetic that classifies it (DESIGN.md §4.2c).

``models``
    What a number is — :class:`~investo.domain.models.Metric`,
    :class:`~investo.domain.models.RawFact`, :class:`~investo.domain.models.Fact`, and
    :func:`~investo.domain.models.market_cap`.

**Zero I/O in this package.** No module here imports ``httpx``, imports ``investo.ingest``, or
reads a file. DESIGN.md §3's dependency flow is one-directional and ``domain/`` is the bottom;
``tests/test_layering.py`` walks the AST and fails if that stops being true.

Nothing here decides what a number *means*. Tag fallback chains, ``as_of`` filtering and Q4
derivation are all ``normalize/``'s, which is M2. See ``docs/m1/01-domain-types.md`` §4 for the
list of things that deliberately do not live here and where each one does.
"""

from __future__ import annotations

__all__: list[str] = []

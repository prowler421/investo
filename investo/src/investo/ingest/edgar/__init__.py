"""EDGAR: one HTTP client and a set of pure parsers (ROADMAP M1).

``client`` is the **single place in the codebase that may talk to sec.gov** (CLAUDE.md convention
6). It owns the token bucket, the mandatory User-Agent, the retry policy and every CIK/accession
URL transform. ``tests/test_layering.py`` walks the AST of every module and fails on a ``sec.gov``
literal or an ``httpx`` import anywhere else.

Everything beside it is a pure ``bytes -> rows`` function. No parser fetches, none caches, none
reads the clock, and none assigns a :class:`~investo.domain.models.Metric`.

M1a: ``tickers``, ``companyfacts``, ``submissions``.
M1b: ``frames``, ``documents``, ``events``, ``ownership``, ``proxy``.

``_fields`` is private to this package and owns the one thing that surprised the design most:
SEC's endpoints disagree with each other about how to spell the same value. See
``docs/m1/04-parsers.md`` §10.1.
"""

from __future__ import annotations

__all__: list[str] = []

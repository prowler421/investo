"""Fetching and parsing raw payloads (ROADMAP M1).

**Nothing in here interprets a financial figure.** M1 fetches bytes, records where they came from
and when, and turns them into typed rows keyed by **XBRL tag**. Choosing which tag answers to
"revenue" is ``normalize/tags.py``, and that is M2.

The seam is enforced rather than intended (``docs/m1/README.md`` §5):

    No module under ``ingest/`` may reference :class:`~investo.domain.models.Metric`, and no
    module under ``ingest/`` may contain a ``us-gaap`` tag literal.

Both halves are checked by an AST walk in ``tests/test_layering.py``. The reason for the second
half is that a tag literal here is the first line of a second, shadow copy of
``normalize/tags.py`` — and the failure mode of two tag tables is that the report and the
appendix disagree about which tag won.

One carve-out, and it is narrow: market cap needs
``dei:EntityCommonStockSharesOutstanding``. That is a cover-page tag with no fallback chain, so
it is not tag *selection* — and it is named in ``domain/models.py``, not here, with
:func:`~investo.domain.models.cover_share_facts` doing the selection so ``ingest/`` names no tag
at all.

Layout:

``cache``
    Content-addressed, append-only, host-agnostic. Shared by EDGAR, the price adapters and
    FINRA.

``edgar/``
    ``client`` is the single place in the codebase that may talk to sec.gov (CLAUDE.md
    convention 6). Everything beside it is a pure ``bytes -> rows`` parser.

``prices/``
    ``base`` declares the provider protocol; three adapters implement it.

``finra``
    Short interest. Its own HTTP client, deliberately — see ``docs/m1/03-edgar-client.md`` §7.
"""

from __future__ import annotations

__all__: list[str] = []

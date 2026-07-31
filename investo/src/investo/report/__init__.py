"""Output: the machine-readable run record now, charts and the PDF in M3.

ROADMAP M2's stated intent for this package is that it *"creates the ``report/`` package that M3 then
fills in"*. It holds one module at M2 — :mod:`~investo.report.serialize`, which writes DESIGN.md
§4.5's ``report.json``.

The value of writing that envelope now rather than in M5 is that every later milestone adds a key to
a document whose shape and determinism rules are already settled and tested. §11's byte-identical
gate applies to ``report.json`` from M2, so M3 adds the PDF to an existing gate rather than building
one.
"""

from __future__ import annotations

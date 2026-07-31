"""Normalization: which tag is which metric, and what a clean series looks like (ROADMAP M2).

M1 fetches bytes and turns them into :class:`~investo.domain.models.RawFact` rows keyed by XBRL
tag. This package decides which tag answers to "revenue" for a given filer in a given period,
reconciles the traps DESIGN.md §4.2 documents, and emits a
:class:`~investo.normalize.statements.FinancialHistory` in which every figure is keyed by
:class:`~investo.domain.models.Metric` and traces to a ``SourceRef`` or a ``Derivation`` over
several.

Three modules, in dependency order:

:mod:`~investo.normalize.tags`
    The chain registry and the resolver. **The only module in the package permitted a ``us-gaap``
    literal** — ``tests/test_layering.py`` enforces the allowlist and its converse.

:mod:`~investo.normalize.facts`
    ``as_of`` filtering, dedup, bucketing, and residual recovery (Q4 and YTD). Pure arithmetic
    over facts already in memory.

:mod:`~investo.normalize.statements`
    Assembly and measurement: the period spine that gives coverage a denominator, the per-metric
    coverage report, and the findings M2 hands to M4.

Four rules hold across the whole package, each enforced by an AST test rather than by convention
(``docs/m2/05-testing.md`` §4):

1. **No I/O and no clock.** ``as_of`` is resolved at the command boundary and threaded down. A
   ``date.today()`` in here makes two runs either side of midnight differ, which DESIGN.md §11's
   determinism gate reports as nondeterminism rather than as the design mistake it is.
2. **No ``float``.** CLAUDE.md convention 8, at the layer where the temptation appears: a fill
   rate looks like a ``float`` and ``json.dumps`` accepts one.
3. **Every sort names a total key.** ``FiscalPeriod`` compares on ``(end, kind)``, so a stable
   sort over a partial key returns payload iteration order for ties — deterministic in practice,
   not a guarantee, and invisible when wrong.
4. **No severity and no refusal.** M2 states what is true about the data; §6.2's severity registry
   and §6.10's bank/REIT gate are M4's and M5's. A refusal reached inside normalization is a
   refusal with no report attached, which is the opposite of what §6.10 asks for.
"""

from __future__ import annotations

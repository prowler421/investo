"""``report.json`` — the run record every later milestone adds a key to (DESIGN.md §4.5).

§4.5 is normative: every run writes a document carrying the full ``FinancialHistory``, all computed
metrics, forecast summary, flags, scores, and the config and prompt versions used. It is what
``--explain`` dumps and what a future ``investo diff`` compares, and it is versioned independently of
the PDF template. *"Without it the PDF is a dead end — nothing downstream can consume a run."*

M2 fills the parts that exist. Four properties of this module are decisions rather than mechanics:

**The empty keys are declared rather than omitted.** A consumer written against M2's output should
break loudly when it reaches for a forecast that is not there, not receive a ``KeyError`` that is
indistinguishable from a typo. It also makes the document's growth visible in a diff: M5 changes
``"forecast": null`` to an object, which is one reviewable line rather than a new top-level key
appearing from nowhere.

**Source refs are interned** into a top-level ``sources`` array and referenced by index, with an
integer meaning a ``SourceRef`` and an object meaning a ``Derivation`` — the discriminator is the JSON
type, which needs no tag field and cannot be ambiguous. §4.5's stated purpose is ``investo diff``, and
with inline refs a single restated value diffs as a changed number *plus* forty lines of identical
provenance moved around. The array is also §9.1's appendix, already deduplicated, so M3 renders it
directly instead of walking every fact to collect distinct refs.

**Values are emitted as JSON strings**, from ``str(value)``, not as JSON numbers. A JSON number is an
IEEE double to most parsers, so ``391035000000.01`` — the value the AAPL fixture carries specifically
to catch this — reads back as ``391035000000.010009765625``. ``str(Decimal)`` is deliberately not
normalized: ``Decimal("1E+2")`` and ``Decimal("100")`` are equal and print differently, and
normalizing would discard the significant figures the filer filed.

**This module returns a string and does not write.** The command owns the file, which keeps the
serializer pure and makes §11's determinism assertion a string comparison rather than a filesystem
fixture. It also does no *reading*: a ``report.json`` reader is what ``investo diff`` needs, ``diff``
is out of v1 scope, and writing one now would fix the deserialization contract before there is a
consumer to test it against.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

from investo.config import Settings
from investo.domain.models import Fact, Metric
from investo.domain.periods import FiscalPeriod
from investo.domain.provenance import Derivation, Provenance, SourceRef
from investo.normalize.facts import Restatement, fact_sort_key
from investo.normalize.statements import (
    Bucket,
    CoverageReport,
    FinancialHistory,
    Finding,
    MetricCoverage,
    PeriodSpine,
)
from investo.normalize.tags import Tier, identity

__all__ = [
    "SCHEMA_VERSION",
    "CONFIG_FIELDS",
    "RunInfo",
    "run_info",
    "serialize",
    "intern_sources",
]

SCHEMA_VERSION: Final = 1
"""An integer, independent of the package version and of the PDF template.

Incremented when a consumer written against the previous value would **misread** the new one. Adding a
key is not a bump; changing a key's type, its units, or the meaning of its value is. The distinction
matters because this document gains a top-level key at four of the next five milestones, and a version
that increments on every addition tells a reader nothing about whether their parser still works.
"""

CONFIG_FIELDS: Final = ("price_provider", "llm_provider", "lookback", "edgar_requests_per_second")
"""The resolved settings that affected the run — **an allowlist, and never a key.**

``Settings`` carries ``tiingo_key``, ``anthropic_key``, ``openai_key`` and ``gemini_key``; §10 says API
keys are *"via env only, never committed, **never logged**"*, and a ``report.json`` in an output
directory is about as logged as a value gets.

Naming the emitted fields explicitly rather than dumping with exclusions is the load-bearing part. The
failure mode of a denylist is that the next field added is emitted **by default**, and the next field
added is as likely to be a key as not. ``test_serialize::test_no_secret_in_document`` populates every
key field and greps the serialized bytes, so the inversion is asserted rather than trusted.
"""


@dataclass(frozen=True, slots=True)
class RunInfo:
    """The envelope's ``run`` block: what this invocation did, as opposed to what it found.

    Separate from :class:`~investo.normalize.statements.FinancialHistory` because two histories built
    from the same facts must compare **equal**, and a cache fingerprint inside one would make them
    differ. Built by :func:`run_info` from ``Settings`` so the allowlist has exactly one home.
    """

    ticker: str
    as_of: date
    window: tuple[date, date]
    lookback_years: int
    manifest_hash: str
    config: Mapping[str, str]
    generated_by: str


def run_info(
    settings: Settings,
    *,
    ticker: str,
    as_of: date,
    window: tuple[date, date],
    lookback_years: int,
    manifest_hash: str,
    version: str,
) -> RunInfo:
    """Build the ``run`` block, emitting only :data:`CONFIG_FIELDS`.

    Args:
        settings: The resolved configuration. **Only the allowlisted fields are read**, so a key
            cannot reach the document by being added to ``Settings`` later.
        ticker: The resolved symbol.
        as_of: The command-boundary date — never a clock read from here.
        window: The lookback window actually applied.
        lookback_years: Whole years, as ``parse_lookback`` resolved them.
        manifest_hash: ``Cache.manifest_hash()`` — the ``sha256`` over the sorted
            ``(key, content_sha256)`` pairs of the entries **this run used**, per §9.1. Not the whole
            manifest file: hashing that would make an AAPL report's hash change when someone fetches
            MSFT.
        version: The package version, for ``generated_by``.
    """
    return RunInfo(
        ticker=ticker,
        as_of=as_of,
        window=window,
        lookback_years=lookback_years,
        manifest_hash=manifest_hash,
        config={name: str(getattr(settings, name)) for name in CONFIG_FIELDS},
        generated_by=f"investo {version}",
    )


def serialize(history: FinancialHistory, *, run: RunInfo) -> str:
    """Render the document. Deterministic, and byte-identical across two runs on one cache.

    ``sort_keys=True`` because ``dict`` insertion order is a property of the code that built the
    document, and refactoring the builder should not change the file. Three further sources of
    nondeterminism are handled upstream and restated here because this is where they would become
    visible:

    - **No clock read.** Nothing in the document comes from ``datetime.now()``. ``run.as_of`` is the
      command-boundary value and ``sources[].fetched_at`` comes from the cache entry, which makes the
      document a function of the cache rather than of the wall clock. A run after ``--refresh``
      produces different bytes because it saw different data — that is the gate working, not failing.
    - **No set or frozenset is serialized.** ``CompanyFacts.tags_present`` and ``taxonomies_present``
      are ``frozenset``, whose iteration order is a function of hash values; nothing in here reaches
      for them, and anything that did would go through ``sorted()`` first.
    - **Index assignment is by sorted key, not encounter order** — see :func:`_intern`.

    Returns:
        The document, UTF-8-encodable, with a trailing newline. **A string, not a file**: the command
        owns the path.
    """
    sources, index_of = _intern(history)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": run.generated_by,
        "run": {
            "ticker": run.ticker,
            "as_of": run.as_of.isoformat(),
            "window": [run.window[0].isoformat(), run.window[1].isoformat()],
            "lookback_years": run.lookback_years,
            "manifest_hash": run.manifest_hash,
            "config": dict(run.config),
        },
        "company": {
            "cik": history.cik,
            "ticker": history.ticker,
            "name": history.name,
            "sic": history.sic,
            "sic_description": history.sic_description,
            "fiscal_year_end": history.fiscal_year_end,
        },
        "sources": [_source(ref) for ref in sources],
        "history": {
            "annual": _series(history.annual, index_of),
            "quarterly": _series(history.quarterly, index_of),
            "quarters_available": history.quarters_available,
        },
        "coverage": _coverage(history.coverage, index_of),
        "restatements": [_restatement(record) for record in history.restatements],
        "market_cap": _market_cap(history, index_of),
        # Declared, not omitted — see the module docstring. `narrative` gains prompt versions and
        # token spend in `run` alongside `config` at M6; the key is not stubbed here, because an empty
        # `prompt_versions` object is indistinguishable from a run where the LLM was on and recorded
        # nothing.
        "analysis": None,
        "forecast": None,
        "narrative": None,
        "backtest": None,
    }
    # `docs/m2/04-serialize.md` §5 writes `separators=(",", ":")` next to `indent=2`, and its own
    # sample document shows `"schema_version": 1` with the space. The two cannot both hold; the space
    # wins, because the sample is what a reader compares against and the key separator has no bearing
    # on determinism. Every property the gate needs — sorted keys, no ASCII escaping, a trailing
    # newline, UTF-8 without a BOM — is unaffected.
    return (
        json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ": "), indent=2)
        + "\n"
    )


# ---------------------------------------------------------------------------
# interning
# ---------------------------------------------------------------------------
def _ref_key(ref: SourceRef) -> tuple[str, str, str, str, str]:
    """``(accession, taxonomy, tag, filed, url)`` — the sort key index assignment uses.

    Encounter order is a function of ``dict`` iteration over the resolved metrics, which is stable but
    is not something §11's byte-identical gate should depend on, and which changes the moment a metric
    is added to the registry. Sorting makes the array a function of its contents.
    """
    return (
        ref.accession.value,
        ref.taxonomy or "",
        ref.tag or "",
        ref.filed.isoformat(),
        ref.url,
    )


def _walk(source: Provenance) -> tuple[SourceRef, ...]:
    """Every leaf ref under ``source``, including through nested derivations."""
    if isinstance(source, Derivation):
        return source.refs()
    return (source,)


def _intern(
    history: FinancialHistory,
) -> tuple[tuple[SourceRef, ...], Mapping[tuple[str, str, str, str, str], int]]:
    """Collect every distinct ref in the document and assign indices by sorted key."""
    collected: dict[tuple[str, str, str, str, str], SourceRef] = {}

    def add(source: Provenance) -> None:
        for ref in _walk(source):
            collected.setdefault(_ref_key(ref), ref)

    for store in (history.annual, history.quarterly):
        for facts in store.values():
            for fact in facts:
                add(fact.source)
    for finding in history.coverage.findings:
        for evidence in finding.evidence:
            add(evidence)
    if history.market_cap is not None:
        add(history.market_cap[1])

    ordered = tuple(collected[key] for key in sorted(collected, key=identity))
    return ordered, {_ref_key(ref): position for position, ref in enumerate(ordered)}


def intern_sources(history: FinancialHistory) -> tuple[SourceRef, ...]:
    """The deduplicated, index-ordered ``sources`` array. **[M3]**

    Exported so ``report/model.py`` renders §9.1's tag-provenance appendix from the *same* walk the
    document uses, rather than collecting distinct refs a second time. This module's docstring
    already anticipated it: the array *"is also §9.1's appendix, already deduplicated, so M3 renders
    it directly instead of walking every fact."*

    Two walks would be two definitions of "distinct" — and the table they disagree about is the one
    whose entire purpose is being checkable against EDGAR. The index a row shows here is therefore
    the index ``report.json`` refers to, which is what makes the two artifacts cross-referenceable
    by hand.
    """
    ordered, _ = _intern(history)
    return ordered


def _provenance(
    source: Provenance, index_of: Mapping[tuple[str, str, str, str, str], int]
) -> int | dict[str, Any]:
    """An integer for a ``SourceRef``, an object for a ``Derivation``, recursively.

    The union is discriminated by JSON type. A ``Derivation``'s ``inputs`` are the same union, so a
    derived Q4 over a stitched series nests rather than flattening — §3.2 requires the ancestry, and
    the appendix prints the leaves.
    """
    if isinstance(source, Derivation):
        return {
            "rule": source.rule,
            "inputs": [_provenance(item, index_of) for item in source.inputs],
            "note": source.note,
        }
    return index_of[_ref_key(source)]


def _source(ref: SourceRef) -> dict[str, Any]:
    """One entry in the ``sources`` array.

    ``fetched_at`` is ISO-8601 with a ``Z`` suffix, always UTC — which ``SourceRef.__post_init__``
    already guarantees is not naive.
    """
    return {
        "accession": ref.accession.value,
        "taxonomy": ref.taxonomy,
        "tag": ref.tag,
        "form": ref.form,
        "filed": ref.filed.isoformat(),
        "url": ref.url,
        "fetched_at": _timestamp(ref.fetched_at),
    }


def _timestamp(moment: datetime) -> str:
    """``2026-07-31T11:02:21Z``.

    Converted to UTC before formatting rather than assumed to be in it. ``SourceRef`` guarantees the
    timestamp is *aware*, not that its offset is zero — and a ``Z`` suffix written onto a
    ``+02:00`` timestamp is a provenance record that is wrong by two hours in a document whose whole
    purpose is being checkable.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# the body
# ---------------------------------------------------------------------------
def _series(
    store: Mapping[Metric, tuple[Fact, ...]],
    index_of: Mapping[tuple[str, str, str, str, str], int],
) -> dict[str, list[dict[str, Any]]]:
    """One bucket's series, keyed by the metric's string value.

    Metrics with no facts are emitted as an empty list rather than omitted, for the same reason the
    top-level milestone keys are: an absent metric and a metric nobody asked for should not look the
    same to a consumer.
    """
    return {
        str(metric): [_fact(fact, index_of) for fact in sorted(store[metric], key=fact_sort_key)]
        for metric in sorted(store, key=str)
    }


def _fact(fact: Fact, index_of: Mapping[tuple[str, str, str, str, str], int]) -> dict[str, Any]:
    return {
        "value": str(fact.value),
        "unit": fact.unit,
        "period": _period(fact.period),
        "source": _provenance(fact.source, index_of),
    }


def _period(period: FiscalPeriod) -> dict[str, Any]:
    return {
        "start": period.start.isoformat() if period.start is not None else None,
        "end": period.end.isoformat(),
        "kind": str(period.kind),
    }


def _coverage(
    report: CoverageReport, index_of: Mapping[tuple[str, str, str, str, str], int]
) -> dict[str, Any]:
    return {
        "spine": _spine(report.spine),
        "annual": _bucket_coverage(report, Bucket.ANNUAL),
        "quarterly": _bucket_coverage(report, Bucket.QUARTERLY),
        "tiers": {
            str(tier): {
                str(bucket): _rate(report.tier_fill_rate(tier, bucket))
                for bucket in (Bucket.ANNUAL, Bucket.QUARTERLY)
            }
            for tier in (Tier.DCF, Tier.QUALITY)
        },
        "findings": [_finding(finding, index_of) for finding in report.findings],
    }


def _spine(spine: PeriodSpine) -> dict[str, Any]:
    """The denominator, **with its origin**.

    ``origin`` is not optional and not inferable from the arrays: a coverage percentage against an
    ``OBSERVED`` spine is close to meaningless, and a consumer that cannot see which one it got will
    eventually print the number without the qualification.
    """
    return {
        "origin": str(spine.origin),
        "annual_ends": [day.isoformat() for day in spine.annual_ends],
        "quarterly_ends": [day.isoformat() for day in spine.quarterly_ends],
    }


def _bucket_coverage(report: CoverageReport, bucket: Bucket) -> dict[str, Any]:
    entries = report.for_bucket(bucket)
    return {str(metric): _metric_coverage(entries[metric]) for metric in sorted(entries, key=str)}


def _metric_coverage(coverage: MetricCoverage) -> dict[str, Any]:
    return {
        "tags_used": list(coverage.tags_used),
        "filled": coverage.filled,
        "expected": coverage.expected,
        "fill_rate": _rate(coverage.fill_rate),
        "derived_periods": coverage.derived_periods,
        "recovered_periods": coverage.recovered_periods,
        "periods_outside_spine": coverage.periods_outside_spine,
        "spine_date_inexact": coverage.spine_date_inexact,
        "dropped_other_bucket": coverage.dropped_other_bucket,
        "dropped_unit_mismatch": coverage.dropped_unit_mismatch,
        "dropped_ytd_redundant": coverage.dropped_ytd_redundant,
        "dropped_ytd_unusable": coverage.dropped_ytd_unusable,
        "sign_anomalies": coverage.sign_anomalies,
    }


def _rate(rate: Decimal | None) -> str | None:
    """A fill rate as a string, or ``null``.

    A string for the same reason every other number here is one, and ``null`` rather than ``0`` when
    nothing was expected — see ``MetricCoverage.fill_rate``. Quantized to four places because the
    exact quotient of two small integers is a repeating decimal 28 digits long, and a coverage figure
    is not a money value: it is a ratio the report prints to one decimal place.
    """
    if rate is None:
        return None
    return str(rate.quantize(Decimal("0.0001")))


def _finding(
    finding: Finding, index_of: Mapping[tuple[str, str, str, str, str], int]
) -> dict[str, Any]:
    return {
        "code": finding.code,
        "metric": str(finding.metric) if finding.metric is not None else None,
        "detail": finding.detail,
        "evidence": [_provenance(item, index_of) for item in finding.evidence],
    }


def _restatement(record: Restatement) -> dict[str, Any]:
    """One restatement record. **Every generation is kept**, per ROADMAP open question 10.

    ``value_changed`` is emitted rather than left to the consumer to derive, because the distinction
    between a restatement and a re-filing is exactly what the ``restated`` finding keys on, and two
    places deriving it is one place too many.
    """
    return {
        "metric": str(record.metric),
        "period": _period(record.period),
        "current": str(record.current),
        "value_changed": record.value_changed,
        "superseded": [
            {"filed": filed.isoformat(), "value": str(value), "accession": accession.value}
            for filed, value, accession in record.superseded
        ],
    }


def _market_cap(
    history: FinancialHistory, index_of: Mapping[tuple[str, str, str, str, str], int]
) -> dict[str, Any] | None:
    if history.market_cap is None:
        return None
    value, derivation = history.market_cap
    return {"value": str(value), "source": _provenance(derivation, index_of)}

"""`report.json`: the schema, the interning, the `Decimal` encoding, and the determinism gate.

`docs/m2/04-serialize.md` is the design; DESIGN.md §4.5 and §11 are normative. This is the first
artifact in the project that §11's byte-identical gate applies to, so the rules settled here are the
ones M3 inherits rather than re-derives.

Two assertions in this file are doing work no obvious version of them would do.

**The quoting is asserted on the serialized bytes, not through a round trip.** The natural test reads
the document back with `json.loads(..., parse_float=Decimal)` and compares — and that passes *even if
the value was emitted as a bare JSON number*, because the hook reconstructs it exactly on the way in.
It would stay green right up until some other consumer, or a reader written in anything but Python,
parsed the same document with default settings and got `391035000000.010009765625`. So there are two
assertions: one that the value survives, and one on the **quotes in the raw text**. The second is what
makes the rule durable — emitting a JSON number is the change a future contributor makes for
readability, it is a one-character diff, and every value-level test in the suite keeps passing.

**Determinism is asserted across a subprocess as well as in-process.** `CompanyFacts` carries two
`frozenset` fields, and a `frozenset`'s iteration order is a function of hash values that are fixed
for the life of one interpreter — so the in-process comparison cannot see the failure it is looking
for. The subprocess run varies `PYTHONHASHSEED`, which is the only way that class of bug shows up
before someone else's machine finds it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from investo.config import Settings
from investo.domain.models import Metric
from investo.ingest.edgar.companyfacts import CompanyFacts
from investo.normalize.statements import (
    Bucket,
    FinancialHistory,
    SpineOrigin,
    build_history,
)
from investo.normalize.tags import Tier
from investo.report.serialize import (
    CONFIG_FIELDS,
    SCHEMA_VERSION,
    RunInfo,
    run_info,
    serialize,
)
from tests.conftest import (
    VALID_USER_AGENT,
    company_facts,
    filing_rows,
    history,
    submissions,
)

AAPL = "AAPL.trimmed.json"
TIER2 = "TIER2.trimmed.json"
NCI = "NCI.trimmed.json"

AS_OF = date(2026, 7, 31)
WINDOW = (date(2015, 1, 1), AS_OF)
MANIFEST = "9f2c1ab4deadbeef0000000000000000000000000000000000000000deadbeef00"

SECRETS = {
    "tiingo_key": "tiingo-SECRET-91b0c4",
    "anthropic_key": "sk-ant-SECRET-4d1f",
    "openai_key": "sk-SECRET-77aa",
    "gemini_key": "gem-SECRET-e3b0",
}
"""Every key field `Settings` carries, populated, so the allowlist is tested by grep rather than by
reading the code that is supposed to implement it."""


def _envelope(settings: Settings | None = None, *, ticker: str = "AAPL") -> RunInfo:
    return run_info(
        settings if settings is not None else Settings(sec_user_agent=VALID_USER_AGENT),
        ticker=ticker,
        as_of=AS_OF,
        window=WINDOW,
        lookback_years=5,
        manifest_hash=MANIFEST,
        version="0.1.0",
    )


def _aapl_history() -> FinancialHistory:
    profile, filings = submissions("AAPL.json", cik=320193)
    return history(
        AAPL,
        ticker="AAPL",
        cik=320193,
        name="Apple Inc.",
        profile=profile,
        filings=filings,
        window=WINDOW,
        as_of=AS_OF,
    )


def _history_from(facts: CompanyFacts) -> FinancialHistory:
    """The AAPL history over a payload the caller has modified.

    Both determinism tests need it: one reorders the fact lists, the other moves `fetched_at`. Keeping
    the rest of the call identical is what makes the comparison a statement about the one thing that
    changed.
    """
    profile, filings = submissions("AAPL.json", cik=320193)
    return build_history(
        facts,
        ticker="AAPL",
        cik=320193,
        name="Apple Inc.",
        profile=profile,
        filings=filings,
        window=WINDOW,
        as_of=AS_OF,
    )


def _document(*, ticker: str = "AAPL") -> tuple[str, dict[str, Any]]:
    raw = serialize(_aapl_history(), run=_envelope(ticker=ticker))
    return raw, json.loads(raw, parse_float=Decimal)


# ---------------------------------------------------------------------------
# the envelope
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_schema_version_is_an_integer_and_is_one() -> None:
    """One version, one producer, and no migration code.

    `schema_version` is incremented when a consumer written against the previous value would
    **misread** the new one — not when a key is added. That distinction matters because this document
    gains a top-level key at four of the next five milestones, and a version that bumps on every
    addition tells a reader nothing about whether their parser still works.
    """
    raw, document = _document()
    assert SCHEMA_VERSION == 1
    assert document["schema_version"] == 1
    assert '"schema_version": 1' in raw, "an integer, not a string — this one is not a Decimal"


@pytest.mark.spec
def test_the_milestone_keys_are_declared_and_null() -> None:
    """Declared rather than omitted, so a consumer breaks loudly rather than getting a `KeyError`.

    A `KeyError` is indistinguishable from a typo in the consumer. It also makes the document's growth
    visible in a diff: M5 changes `"forecast": null` to an object, which is one reviewable line rather
    than a new top-level key appearing from nowhere.
    """
    _, document = _document()
    for key in ("analysis", "forecast", "narrative", "backtest"):
        assert key in document, f"{key} must be declared before the milestone that fills it"
        assert document[key] is None


@pytest.mark.spec
def test_the_run_block_records_the_command_boundary_values() -> None:
    """`as_of`, the window and the lookback come from the command, and the hash from the cache.

    Nothing in the document is a clock read. That is what makes it a function of the cache rather than
    of the wall clock — and what makes a `--refresh` between two runs change the bytes, which is the
    gate working rather than failing.
    """
    _, document = _document()
    run: Any = document["run"]
    assert run["ticker"] == "AAPL"
    assert run["as_of"] == AS_OF.isoformat()
    assert run["window"] == [WINDOW[0].isoformat(), WINDOW[1].isoformat()]
    assert run["lookback_years"] == 5
    assert run["manifest_hash"] == MANIFEST


@pytest.mark.spec
def test_no_secret_in_document() -> None:
    """An API key cannot reach `report.json`. §10: keys are *"never logged"*.

    And a `report.json` in an output directory is about as logged as a value gets. The test constructs
    `Settings` with **every** key field populated and greps the serialized bytes, which is the assertion
    that makes the allowlist load-bearing: the failure mode of a denylist is that the next field added
    is emitted by default, and the next field added is as likely to be a key as not.
    """
    settings = Settings(
        sec_user_agent=VALID_USER_AGENT,
        tiingo_key=SECRETS["tiingo_key"],
        anthropic_key=SECRETS["anthropic_key"],
        openai_key=SECRETS["openai_key"],
        gemini_key=SECRETS["gemini_key"],
    )
    raw = serialize(_aapl_history(), run=_envelope(settings))

    for name, value in SECRETS.items():
        assert value not in raw, f"{name} reached the document"
    assert "SECRET" not in raw
    for field in SECRETS:
        assert field not in raw, f"{field} is named in the document even if its value is not"


@pytest.mark.spec
def test_the_config_block_is_the_allowlist_and_nothing_else() -> None:
    """Named fields, not a dump with exclusions — asserted on the emitted key set.

    `Settings` has a dozen fields and four of them are secrets. An implementation that emitted
    everything except a denylist would pass the grep above today and fail it on the first field someone
    adds, which is exactly the review nobody performs.
    """
    _, document = _document()
    run: Any = document["run"]
    config: Any = run["config"]
    assert set(config) == set(CONFIG_FIELDS)
    assert config["lookback"] == "5y"
    assert config["llm_provider"] == "none"


@pytest.mark.spec
def test_the_company_block_carries_the_identity_and_the_sic() -> None:
    """§6.10's gate, §6.1's Altman variant and §6.5's peer cohort all read `sic` from here.

    Threading it separately is how one of the three ends up reading a different value, so it is in the
    document once, next to the CIK that identifies whose SIC it is.
    """
    _, document = _document()
    company: Any = document["company"]
    assert company == {
        "cik": 320193,
        "ticker": "AAPL",
        "name": "Apple Inc.",
        "sic": 3571,
        "sic_description": "Electronic Computers",
        "fiscal_year_end": "0928",
    }


# ---------------------------------------------------------------------------
# Decimal
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_decimal_round_trip_exact() -> None:
    """`391035000000.01` survives, asserted as `Decimal` equality rather than as a string.

    The guarantee is the value, not the spelling: `str(Decimal)` is deliberately not normalized, so
    `Decimal("1E+2")` and `Decimal("100")` are equal and print differently, and normalizing would
    discard the significant figures the filer filed.
    """
    _, document = _document()
    revenue: Any = document["history"]["annual"]["revenue"]
    values = [Decimal(entry["value"]) for entry in revenue]
    assert Decimal("391035000000.01") in values


@pytest.mark.spec
def test_values_are_quoted_in_the_raw_text() -> None:
    """A value cannot be emitted as a JSON **number** — asserted on the bytes.

    This is the assertion the round trip above cannot make. `parse_float=Decimal` reconstructs a bare
    number exactly, so the round trip is green either way; the quotes are the only observable
    difference, and they are what protects every consumer that is not this test.
    """
    raw, _ = _document()
    assert '"value": "391035000000.01"' in raw
    assert '"value": 391035000000.01' not in raw
    assert "391035000000.010009765625" not in raw, "a float was materialized somewhere"


@pytest.mark.spec
def test_every_value_in_the_document_is_a_string() -> None:
    """The rule over the whole document, not just the one fixture value that catches a `float`.

    An implementation that special-cased the known-awkward number would pass the test above. Walking
    every fact means a value emitted as a number anywhere — a derived figure, a market cap, a
    restatement's superseded generation — fails here instead of on someone's parser.
    """
    _, document = _document()
    history_block: Any = document["history"]
    for bucket in ("annual", "quarterly"):
        series: Any = history_block[bucket]
        for facts in series.values():
            for entry in facts:
                assert isinstance(entry["value"], str), entry
                Decimal(entry["value"])


@pytest.mark.spec
def test_a_fill_rate_is_a_string_or_null_never_a_number() -> None:
    """The number most likely to arrive as a `float`, because a ratio looks like one.

    `json.dumps` accepts a `float` happily, and the AST rule catches the `float(...)` route but not a
    `Decimal` divided into something and formatted. `null` where nothing was expected, per
    `MetricCoverage.fill_rate` — not `0`, which is a different claim.
    """
    profile, filings = submissions("ARXS.json", cik=2093536)
    result = history("ARXS.json", cik=2093536, profile=profile, filings=filings)
    document = json.loads(serialize(result, run=_envelope(ticker="ARXS")), parse_float=Decimal)
    annual: Any = document["coverage"]["annual"]
    assert annual, "ARXS resolves at least one metric entry"
    rates = [entry["fill_rate"] for entry in annual.values()]
    assert all(rate is None or isinstance(rate, str) for rate in rates)
    assert any(rate is None for rate in rates), "ARXS files no 10-K, so annual expects nothing"


# ---------------------------------------------------------------------------
# interning
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_a_filed_fact_points_at_a_source_index() -> None:
    """An integer is a `SourceRef`; the discriminator is the JSON type and cannot be ambiguous.

    Interning is what keeps `investo diff` readable: with inline refs, a single restated value shows up
    as a changed number *plus* forty lines of identical provenance moved around.
    """
    _, document = _document()
    sources: Any = document["sources"]
    revenue: Any = document["history"]["annual"]["revenue"]
    for entry in revenue:
        assert isinstance(entry["source"], int)
        assert 0 <= entry["source"] < len(sources)


@pytest.mark.spec
def test_a_derived_fact_points_at_an_object_whose_inputs_are_the_same_union() -> None:
    """And it nests rather than flattening, because §3.2 requires the ancestry.

    `NCI`'s liabilities are derived from two tags, so the source is an object with a rule and two
    integer inputs. A serializer that flattened to the leaf refs would lose *which arithmetic* produced
    the number — and the number is the one §4.2 warns overstates liabilities by the noncontrolling
    interest when the wrong equity tag is used.
    """
    result = history(NCI, cik=1000049, filings=filing_rows(("10-K", "2024-02-21", "2023-12-31")))
    document = json.loads(serialize(result, run=_envelope(ticker="EXNI")), parse_float=Decimal)
    liabilities: Any = document["history"]["annual"]["liabilities"]
    assert liabilities, "the derivation fired"
    source: Any = liabilities[0]["source"]
    assert source["rule"] == "liabilities_from_lse_minus_equity"
    assert len(source["inputs"]) == 2
    assert all(isinstance(item, int) for item in source["inputs"])


@pytest.mark.spec
def test_source_indices_are_sorted() -> None:
    """Index assignment is by sorted key, **not** encounter order.

    Encounter order is a function of `dict` iteration over the resolved metrics: stable today, not a
    guarantee, and it changes the moment a metric is added to the registry. Asserted by sorting the
    emitted array on the same key and requiring it to already be in that order — which fails for an
    encounter-ordered document without needing a second run to compare against.
    """
    _, document = _document()
    sources: Any = document["sources"]
    keys = [
        (
            entry["accession"],
            entry["taxonomy"] or "",
            entry["tag"] or "",
            entry["filed"],
            entry["url"],
        )
        for entry in sources
    ]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys)), "interned means deduplicated"


@pytest.mark.spec
def test_the_sources_array_is_the_appendix() -> None:
    """§9.1 asks for tag provenance per metric, and this array *is* it, already deduplicated.

    So each entry has to carry everything the appendix prints: the accession, the qualified tag, the
    form, the filing date, the URL, and when we fetched it. A missing field here is a section M3 cannot
    render without walking every fact again.
    """
    _, document = _document()
    sources: Any = document["sources"]
    for entry in sources:
        assert set(entry) == {
            "accession",
            "taxonomy",
            "tag",
            "form",
            "filed",
            "url",
            "fetched_at",
        }
        assert entry["fetched_at"].endswith("Z"), "UTC, always — a naive timestamp means nothing"
        assert "T" in entry["fetched_at"]


# ---------------------------------------------------------------------------
# coverage and findings in the document
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_the_spine_origin_is_emitted() -> None:
    """A percentage against an `OBSERVED` denominator must never appear without saying so.

    Not inferable from the arrays — an observed spine and a filings spine can hold the same dates — so
    `origin` is a field, and a consumer that cannot see which one it got will eventually print the
    number without the qualification.
    """
    _, document = _document()
    spine: Any = document["coverage"]["spine"]
    assert spine["origin"] == str(SpineOrigin.FILINGS)

    profile, filings = submissions("NOPERIODIC.json", cik=1000052)
    observed = history("NOPERIODIC.trimmed.json", cik=1000052, profile=profile, filings=filings)
    other = json.loads(serialize(observed, run=_envelope(ticker="EXNP")), parse_float=Decimal)
    assert other["coverage"]["spine"]["origin"] == str(SpineOrigin.OBSERVED)
    assert any(f["code"] == "spine_observed" for f in other["coverage"]["findings"])


@pytest.mark.spec
def test_the_tier_aggregates_are_emitted_per_bucket() -> None:
    """ROADMAP M2's criterion is stated per tier, so the document reports it that way.

    A single aggregate would hide a tier-2 failure behind tier-1 success — the outcome ROADMAP's
    *"building only the first tier means M4 stalls"* warns about, arriving one milestone later and
    disguised as a passing gate.
    """
    _, document = _document()
    tiers: Any = document["coverage"]["tiers"]
    assert set(tiers) == {str(Tier.DCF), str(Tier.QUALITY)}
    for entry in tiers.values():
        assert set(entry) == {str(Bucket.ANNUAL), str(Bucket.QUARTERLY)}


@pytest.mark.spec
def test_findings_are_emitted_in_full_with_their_codes() -> None:
    """The code is the machine-readable key and the detail is the sentence; both are needed.

    M4 keys flags on the code, and §9.1's caveat section prints the detail. Emitting only a count — the
    obvious size optimization — would leave the report saying "3 findings", which is a number nobody
    acts on.
    """
    result = history(TIER2, cik=1000048)
    document = json.loads(serialize(result, run=_envelope(ticker="EXT2")), parse_float=Decimal)
    findings: Any = document["coverage"]["findings"]
    assert findings
    codes = {entry["code"] for entry in findings}
    assert "series_stitched" in codes
    for entry in findings:
        assert set(entry) == {"code", "metric", "detail", "evidence"}
        assert entry["detail"]


@pytest.mark.spec
def test_a_restatement_carries_every_generation_and_says_whether_the_value_moved() -> None:
    """ROADMAP open question 10 stays answerable, and the re-filing case stays distinguishable.

    `value_changed` is emitted rather than left to the consumer to derive, because the difference
    between a restatement and a re-filing is exactly what the `restated` finding keys on — and two
    places deriving it is one place too many.
    """
    _, document = _document()
    restatements: Any = document["restatements"]
    assert restatements, "the AAPL quarter appears under four accessions"
    for entry in restatements:
        assert set(entry) == {"metric", "period", "current", "value_changed", "superseded"}
        assert isinstance(entry["current"], str)
        for older in entry["superseded"]:
            assert set(older) == {"filed", "value", "accession"}
            assert isinstance(older["value"], str)
    assert any(entry["value_changed"] is False for entry in restatements), (
        "equal values across four accessions is a re-filing, not a restatement"
    )


@pytest.mark.spec
def test_a_metric_with_no_facts_is_an_empty_list_not_a_missing_key() -> None:
    """An absent metric and a metric nobody asked for must not look the same to a consumer.

    The same argument as the milestone keys. `AAPL.trimmed.json` has no capex tag, so the key is there
    and the list is empty — which a chart can render as a gap, where a missing key is a `KeyError`.
    """
    _, document = _document()
    annual: Any = document["history"]["annual"]
    assert str(Metric.CAPEX) in annual
    assert annual[str(Metric.CAPEX)] == []
    assert set(annual) == {str(metric) for metric in Metric}


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_two_runs_produce_identical_bytes() -> None:
    """§11's gate, in-process: the same cache and the same inputs produce the same document.

    Asserting on **bytes** rather than on a parsed comparison is the lesson M1 recorded when `gzip`
    wrote a filename into the blob header while every hash-level assertion passed.
    """
    first = serialize(_aapl_history(), run=_envelope())
    second = serialize(_aapl_history(), run=_envelope())
    assert first.encode("utf-8") == second.encode("utf-8")
    assert first.endswith("\n"), "a trailing newline, so the file is a text file"


@pytest.mark.spec
def test_the_document_is_identical_across_a_subprocess_boundary() -> None:
    """`PYTHONHASHSEED` does not change the output — and the in-process test cannot see this.

    `CompanyFacts` carries two `frozenset` fields whose iteration order is a function of hash values
    that are fixed for the life of one interpreter. So the assertion needs two interpreters with
    different seeds, or the class of bug it is looking for is invisible until someone else runs the
    suite.
    """
    outputs: list[str] = []
    for seed in ("0", "1", "12345"):
        completed = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_SCRIPT],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(Path(__file__).parent.parent),
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": "src"},
        )
        outputs.append(completed.stdout)
    assert len(set(outputs)) == 1, "the document depends on the hash seed"
    assert outputs[0], "the subprocess produced nothing, so the comparison was vacuous"


@pytest.mark.spec
def test_shuffling_the_input_facts_does_not_change_the_document() -> None:
    """Fact ordering within a series is total, so payload iteration order cannot leak into the bytes.

    `FiscalPeriod` compares on `(end, kind)`, and a stable sort over that partial key returns input
    order for ties — where input order descends from `dict` iteration over parsed JSON. Reversing every
    tag's fact list is the cheapest way to make that visible: with a total key the document is
    unchanged, and without one two facts sharing an end and a kind swap places.
    """
    original = company_facts(AAPL, cik=320193)
    reordered = dataclasses.replace(
        original,
        facts={key: tuple(reversed(rows)) for key, rows in original.facts.items()},
    )
    assert serialize(_aapl_history(), run=_envelope()) == serialize(
        _history_from(reordered), run=_envelope()
    )


@pytest.mark.spec
def test_a_different_fetch_timestamp_changes_the_bytes() -> None:
    """The gate working, not failing: the document is a function of the **cache**.

    Two runs against the same cache are identical; a run after `--refresh` differs, because
    `fetched_at` moved and the run saw different data. An implementation that omitted `fetched_at` to
    make the gate easier would pass every other test in this file and would make §4.4's immutable
    record unverifiable.
    """
    original = company_facts(AAPL, cik=320193)
    later = datetime(2026, 8, 2, 9, 0, 0, tzinfo=UTC)
    moved = dataclasses.replace(
        original,
        facts={
            key: tuple(
                dataclasses.replace(fact, source=dataclasses.replace(fact.source, fetched_at=later))
                for fact in rows
            )
            for key, rows in original.facts.items()
        },
    )
    assert serialize(_aapl_history(), run=_envelope()) != serialize(
        _history_from(moved), run=_envelope()
    )


@pytest.mark.spec
def test_the_document_keys_are_sorted() -> None:
    """`sort_keys=True`, because insertion order is a property of the code that built the document.

    Refactoring the builder should not change the file — otherwise every reordering of a dict literal
    is a diff in an artifact that is supposed to change only when the data does.
    """
    raw, document = _document()
    assert list(document) == sorted(document)
    assert raw.index('"analysis"') < raw.index('"backtest"') < raw.index('"company"')


def test_serialize_returns_a_string_and_writes_nothing(tmp_path: Path) -> None:
    """The command owns the file. `serialize` is pure, so the gate is a string comparison.

    A serializer that wrote its own output would need a filesystem fixture for every determinism
    assertion, and `report/` would need a `pathlib` import that the layering rule would then have to
    allow.
    """
    before = set(tmp_path.iterdir())
    raw = serialize(_aapl_history(), run=_envelope())
    assert isinstance(raw, str)
    assert set(tmp_path.iterdir()) == before


_SUBPROCESS_SCRIPT = """
import json
from datetime import UTC, date, datetime
from investo.config import Settings
from investo.domain.provenance import SourceContext
from investo.ingest.edgar.companyfacts import parse_companyfacts
from investo.ingest.edgar.submissions import parse_submissions
from investo.normalize.statements import build_history
from investo.report.serialize import run_info, serialize
from pathlib import Path

root = Path("tests/fixtures/edgar")
ctx = SourceContext(
    url="https://data.sec.gov/test",
    fetched_at=datetime(2026, 7, 31, 11, 2, 21, tzinfo=UTC),
    cik=320193,
)
facts = parse_companyfacts((root / "companyfacts" / "AAPL.trimmed.json").read_bytes(), source=ctx)
profile, recent, _ = parse_submissions((root / "submissions" / "AAPL.json").read_bytes(), source=ctx)
history = build_history(
    facts,
    ticker="AAPL",
    cik=320193,
    name="Apple Inc.",
    profile=profile,
    filings=recent,
    window=(date(2015, 1, 1), date(2026, 7, 31)),
    as_of=date(2026, 7, 31),
)
settings = Settings(sec_user_agent="Investo test suite tests@investo.invalid")
run = run_info(
    settings,
    ticker="AAPL",
    as_of=date(2026, 7, 31),
    window=(date(2015, 1, 1), date(2026, 7, 31)),
    lookback_years=5,
    manifest_hash="9f2c1ab4",
    version="0.1.0",
)
print(serialize(history, run=run), end="")
"""
"""The determinism check, as a program rather than as a call.

Written out here rather than imported so the subprocess shares **nothing** with the parent but the
package and the fixtures on disk — which is the point: a seam that passed objects across would also
pass whatever the parent had already computed, including any dict whose order it had settled.
"""

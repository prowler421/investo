"""Layering rules, enforced by walking the AST of every module in the package.

CLAUDE.md convention 6 and ``docs/m1/03-edgar-client.md`` §7: nothing outside
``ingest/edgar/client.py`` may call sec.gov, ``domain/`` is the bottom of the dependency flow, no
module under ``ingest/`` may name a ``Metric`` or a ``us-gaap`` tag, and no parser may read the
clock. A convention that is only written down is a convention that holds until someone is in a
hurry.

AST rather than grep, and that choice cuts both ways. A literal split across a concatenation or an
f-string is still caught — ``"https://data.sec" + ".gov"`` reaches the same host however it is
spelled — while a *comment* mentioning sec.gov cannot fail the build, because a comment never
reaches the AST at all.

**Docstrings are treated as comments here**, which is a deliberate reading rather than a loophole.
Every module in this package documents the rule it obeys, so prose is where the string "sec.gov" is
*most* likely to appear legitimately. A string that is a whole statement cannot be used as a value,
and that is what makes it documentation by construction.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

import investo
from investo.domain.models import COVER_SHARES_TAG

PACKAGE_ROOT: Final = Path(str(investo.__file__)).parent
"""The installed package, not the working tree — the ``src/`` layout means those can differ."""

CLIENT: Final = "ingest/edgar/client.py"
"""The choke point. Every rule below is stated as an exception to this one file."""

SECGOV_LITERAL_ALLOWED: Final = {
    CLIENT: "the choke point itself: every URL builder lives here",
    "config.py": "prose in a pydantic field description — a sentence, not a URL",
    "domain/provenance.py": "Accession.index_url builds one; a recorded spec conflict",
}
"""Modules permitted a non-docstring ``sec.gov`` literal, and why.

Two of the three entries are discrepancies rather than design.
``docs/m1/01-domain-types.md`` §1 specifies ``Accession.index_url`` in ``domain/provenance.py``,
which necessarily builds an absolute sec.gov URL, while ``docs/m1/03-edgar-client.md`` §7 rule 1
says no module but the client may hold such a literal. Both cannot hold. Recording the exception
keeps the conflict visible instead of deleting the rule that catches the *next* URL builder to land
in the wrong layer.
"""

HTTPX_IMPORT_ALLOWED: Final = {
    CLIENT,
    "ingest/finra.py",
    "ingest/prices/base.py",
    "ingest/prices/stooq.py",
    "ingest/prices/tiingo.py",
    "ingest/prices/yfinance_.py",
}
"""§7 rule 2: prices and FINRA are different hosts with different rate limits, and are entitled to
their own clients. Nothing else is."""

DEI_TAG_ALLOWLIST: Final = frozenset({COVER_SHARES_TAG})
"""The one XBRL tag named outside ``normalize/``, per ``domain/models.py``.

``docs/m1/06-testing.md`` §4 requires this allowlist to be asserted to hold **exactly one** tag. A
second ``dei`` tag needing a name outside ``normalize/`` is the signal that tag selection has begun
leaking upstream, and the assertion is what turns that from a judgement call into a failing test.
"""

CLOCK_READ_ALLOWED: Final = {
    CLIENT,
    "ingest/cache.py",
    "ingest/finra.py",
    "ingest/prices/base.py",
    "ingest/prices/stooq.py",
    "ingest/prices/tiingo.py",
    "ingest/prices/yfinance_.py",
}
"""``docs/m1/04-parsers.md`` §10.2 rule 3: no *parser* reads the clock.

``client.utcnow`` exists so time enters ``ingest/`` in one place; the price adapters and FINRA
fetch, so they stamp their own ``fetched_at``; and the cache records when it wrote an entry, which
is the one timestamp ``prune`` needs. Everything else under ``ingest/`` receives time through a
``SourceContext``.
"""

_CLOCK_ATTRS: Final = frozenset({"now", "today", "utcnow"})
_CLOCK_OWNERS: Final = frozenset({"datetime", "date"})
_DOMAIN_FORBIDDEN: Final = {"httpx", "urllib", "socket", "requests", "sqlite3", "pathlib"}


# ---------------------------------------------------------------------------
# Reading the package
# ---------------------------------------------------------------------------
def _load() -> tuple[tuple[str, ast.Module], ...]:
    """Parse every module once, keyed by its path relative to the package root."""
    return tuple(
        (path.relative_to(PACKAGE_ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
    )


MODULES: Final = _load()


def _documentation_ids(tree: ast.Module) -> set[int]:
    """Ids of the string constants that are bare statements: docstrings, at any nesting."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _text_of(node: ast.AST) -> str | None:
    """The literal text of a string expression, or ``None`` if it is not one.

    Recursive over ``JoinedStr`` and ``+``, so an f-string and a concatenation each yield the text a
    reader of the source would see. This is the part grep cannot do.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = [_text_of(part) for part in node.values]
        return "".join(part for part in parts if part is not None)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _text_of(node.left)
        right = _text_of(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _value_literals(tree: ast.Module) -> list[str]:
    """Every string literal used as a *value*, with docstrings excluded."""
    documentation = _documentation_ids(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) in documentation:
            continue
        text = _text_of(node)
        if text is not None:
            found.append(text)
    return found


def _bound_names(tree: ast.Module) -> set[str]:
    """Every name a module reads, imports, or accesses as an attribute.

    The two ways a ``Metric`` could actually be *used* are as a name and as an attribute, so those
    are what is checked — not the string "Metric", which appears in the prose of two ``__init__``
    docstrings explaining this very rule.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                found.add(alias.asname or alias.name.split(".")[0])
                found.add(alias.name.rsplit(".", 1)[-1])
    return found


def _imported_modules(rel: str, tree: ast.Module) -> set[str]:
    """Absolute dotted module names a module imports, resolving relative imports.

    Nothing in the package uses a relative import today. Resolving them anyway means the rule cannot
    be sidestepped by writing ``from ..ingest import cache``, which is the spelling someone reaches
    for precisely when the absolute one looks wrong.
    """
    package = ["investo", *rel.split("/")[:-1]]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                parts = [node.module] if node.module else []
            else:
                trimmed = package[: len(package) - node.level + 1]
                parts = [*trimmed, node.module] if node.module else trimmed
            base = ".".join(parts)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def _clock_reads(tree: ast.Module) -> list[str]:
    """Every ``datetime.now`` / ``date.today`` / ``utcnow`` reference in a module."""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in _CLOCK_ATTRS:
            continue
        owner = node.value
        if isinstance(owner, ast.Name) and owner.id in _CLOCK_OWNERS:
            found.append(f"{owner.id}.{node.attr}")
        elif isinstance(owner, ast.Attribute) and owner.attr in _CLOCK_OWNERS:
            found.append(f"{owner.attr}.{node.attr}")
    if "utcnow" in _bound_names(tree):
        found.append("utcnow")
    return found


# ---------------------------------------------------------------------------
# The walk itself has to be worth trusting
# ---------------------------------------------------------------------------
def test_every_module_in_the_package_was_parsed() -> None:
    """Guard the guards: every rule below passes vacuously over an empty module list.

    If ``PACKAGE_ROOT`` ever resolves somewhere without Python files — a layout change, a
    namespace-package install, a typo — this whole file goes green while enforcing nothing. So
    discovery is asserted first, against the modules the milestone is actually about.
    """
    names = {rel for rel, _ in MODULES}
    assert len(names) >= 20, f"only found {sorted(names)}"
    for expected in (CLIENT, "domain/models.py", "ingest/cache.py", "ingest/edgar/_fields.py"):
        assert expected in names


def test_docstrings_are_not_treated_as_value_literals() -> None:
    """The exclusion rule is itself load-bearing, so it gets its own test.

    If ``_value_literals`` stopped skipping docstrings, the sec.gov rule would fail on eight modules
    that merely *describe* the choke point — and the likely repair is deleting the rule rather than
    the prose. Parsed from source rather than from the package, so the test names its own input.
    """
    tree = ast.parse('"""A docstring about sec.gov."""\nURL = "https://data.sec.gov"\n')
    assert _value_literals(tree) == ["https://data.sec.gov"]


def test_concatenated_and_interpolated_literals_are_caught() -> None:
    """The reason this file walks the AST instead of grepping.

    A grep for ``"https://data.sec.gov"`` misses both spellings below, and both reach the same host.
    Asserted directly, because the check is only as good as its worst case.
    """
    tree = ast.parse('A = "https://data.sec" + ".gov/api"\nB = f"https://www.sec.gov/{p}"\n')
    hits = [text for text in _value_literals(tree) if "sec.gov" in text]
    assert "https://data.sec.gov/api" in hits
    assert any(text.startswith("https://www.sec.gov/") for text in hits)


# ---------------------------------------------------------------------------
# The choke point
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_no_secgov_literal_outside_client() -> None:
    """CLAUDE.md convention 6: nothing outside the client may call sec.gov.

    The failure this prevents is not a wrong URL. It is a second path to SEC that does not pass the
    token bucket, so the process exceeds a rate limit whose ten-minute penalty is paid by every
    other user of that IP address.
    """
    offenders = {
        rel
        for rel, tree in MODULES
        if any("sec.gov" in text.lower() for text in _value_literals(tree))
    }
    unexpected = offenders - set(SECGOV_LITERAL_ALLOWED)
    assert not unexpected, f"sec.gov literal outside the client: {sorted(unexpected)}"


@pytest.mark.spec
def test_secgov_allowlist_holds_only_the_recorded_exceptions() -> None:
    """An allowlist nobody re-reads is a rule that has quietly stopped applying.

    Pinning the key set means a fourth exception cannot be added without editing this test, which is
    a diff a reviewer looks at. Both non-client entries are discrepancies against
    ``docs/m1/03-edgar-client.md`` §7 rather than intended design.
    """
    assert set(SECGOV_LITERAL_ALLOWED) == {CLIENT, "config.py", "domain/provenance.py"}


@pytest.mark.spec
def test_client_actually_holds_the_hosts() -> None:
    """The converse of the rule above, and not redundant with it.

    Deleting every sec.gov literal from the client would satisfy the choke-point test perfectly.
    This one fails if the URLs move somewhere the AST walk does not police.
    """
    trees = dict(MODULES)
    literals = _value_literals(trees[CLIENT])
    assert any("data.sec.gov" in text for text in literals)
    assert any("www.sec.gov" in text for text in literals)


@pytest.mark.spec
def test_httpx_is_imported_only_where_a_client_is_allowed() -> None:
    """§7 rule 2. An ``httpx`` import in a parser is a parser that can fetch.

    Which would make it untestable from a file on disk, and would put a second traffic source
    outside the limiter — the same failure as a stray URL, arriving from the other direction.
    """
    offenders = {rel for rel, tree in MODULES if "httpx" in _imported_modules(rel, tree)}
    unexpected = offenders - HTTPX_IMPORT_ALLOWED
    assert not unexpected, f"unexpected httpx import: {sorted(unexpected)}"


# ---------------------------------------------------------------------------
# Flow direction
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_domain_imports_nothing_upward() -> None:
    """DESIGN.md §3: the dependency flow is one-directional and ``domain/`` is the bottom.

    An import upward is how ``domain/`` acquires an opinion about HTTP — and once it has one, the
    types stop being testable without a network and the layering argument is over.
    """
    for rel, tree in MODULES:
        if not rel.startswith("domain/"):
            continue
        imported = _imported_modules(rel, tree)
        upward = {name for name in imported if name.startswith("investo.ingest")}
        assert not upward, f"{rel} imports {sorted(upward)}"


@pytest.mark.spec
def test_domain_imports_no_io_at_all() -> None:
    """``docs/m1/01-domain-types.md``: zero I/O in this package.

    Stated separately from the import-direction rule because ``httpx`` and ``pathlib`` reach the
    outside world without going through ``investo.ingest`` — so a module that satisfies the rule
    above can still read a file.
    """
    for rel, tree in MODULES:
        if not rel.startswith("domain/"):
            continue
        roots = {name.split(".")[0] for name in _imported_modules(rel, tree)}
        offending = roots & _DOMAIN_FORBIDDEN
        assert not offending, f"{rel} imports {sorted(offending)}"
        assert "open" not in _bound_names(tree), f"{rel} opens a file"


# ---------------------------------------------------------------------------
# The M1/M2 seam
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_metric_unreferenced_in_ingest() -> None:
    """M1 emits a ``RawFact``; assigning a ``Metric`` is M2's job.

    The seam matters because a parser that names a metric has started a second, undeclared copy of
    ``normalize/tags.py`` — and two tag-selection rules that disagree produce a number that is wrong
    in one report and right in the next.
    """
    for rel, tree in MODULES:
        if not rel.startswith("ingest/"):
            continue
        assert "Metric" not in _bound_names(tree), f"{rel} references Metric"


@pytest.mark.spec
def test_no_usgaap_literal_in_ingest() -> None:
    """No module under ``ingest/`` names a ``us-gaap`` tag.

    The same seam, checked on the other artifact: a tag literal in a parser is tag *selection*,
    which ROADMAP puts in M2. Several docstrings and comments under ``ingest/`` discuss this rule at
    length; the check is about what the code does with a tag, not what it says about one.
    """
    for rel, tree in MODULES:
        if not rel.startswith("ingest/"):
            continue
        offending = [text for text in _value_literals(tree) if "us-gaap" in text.lower()]
        assert not offending, f"{rel} names a us-gaap tag: {offending}"


@pytest.mark.spec
def test_dei_allowlist_holds_exactly_one_tag() -> None:
    """``docs/m1/06-testing.md`` §4: the ``dei`` allowlist holds exactly one tag.

    Market cap is the one place M1 has to name a tag, and it is a cover-page ``dei`` tag with no
    fallback chain — so it is not tag selection. That carve-out stays honest only while it stays
    singular: a second entry means the M2 seam has moved, and this assertion is what says so.
    """
    assert len(DEI_TAG_ALLOWLIST) == 1
    assert DEI_TAG_ALLOWLIST == {COVER_SHARES_TAG}
    assert COVER_SHARES_TAG == "EntityCommonStockSharesOutstanding"


@pytest.mark.spec
def test_the_allowlisted_dei_tag_is_not_named_in_ingest_either() -> None:
    """The allowlist lives in ``domain/``, and the tag literal has to live there with it.

    ``cover_share_facts`` does the selection precisely so that no module under ``ingest/`` names any
    tag at all. Naming it in a parser as well would put the same string in two places, which is how
    the two come to disagree.
    """
    for rel, tree in MODULES:
        if not rel.startswith("ingest/"):
            continue
        named = [text for text in _value_literals(tree) if text in DEI_TAG_ALLOWLIST]
        assert not named, f"{rel} names {named}"


# ---------------------------------------------------------------------------
# No parser reads the clock
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_no_now_call_in_parsers() -> None:
    """``docs/m1/04-parsers.md`` §10.2 rule 3: time enters through a ``SourceContext``.

    A parser that reads the clock cannot be run twice against one fixture with the same result, so
    its output cannot be byte-identical across runs — which DESIGN.md §11 makes a CI gate from M3.
    The failure is invisible at the call site and obvious only in a diff of two report files.
    """
    for rel, tree in MODULES:
        if not rel.startswith("ingest/") or rel in CLOCK_READ_ALLOWED:
            continue
        reads = sorted(set(_clock_reads(tree)))
        assert not reads, f"{rel} reads the clock: {reads}"


@pytest.mark.spec
def test_the_clock_read_allowlist_holds_no_parser() -> None:
    """Every exemption above is a fetcher or the cache — never a ``bytes -> rows`` function.

    Without this, the cheapest way to make the rule above pass is to add the offending parser to the
    allowlist. Pinning the set makes that repair a visible edit to a test that says why the set is
    what it is.
    """
    assert CLOCK_READ_ALLOWED == {
        CLIENT,
        "ingest/cache.py",
        "ingest/finra.py",
        "ingest/prices/base.py",
        "ingest/prices/stooq.py",
        "ingest/prices/tiingo.py",
        "ingest/prices/yfinance_.py",
    }


@pytest.mark.spec
def test_parsers_under_edgar_exist_to_be_checked() -> None:
    """The clock rule is only meaningful if it covers the modules it names.

    ``ingest/edgar/`` minus the client is the parser set. If that set were empty — a rename, a move
    — ``test_no_now_call_in_parsers`` would iterate over nothing and pass.
    """
    parsers = {
        rel
        for rel, _ in MODULES
        if rel.startswith("ingest/edgar/") and rel != CLIENT and not rel.endswith("__init__.py")
    }
    assert {"ingest/edgar/companyfacts.py", "ingest/edgar/submissions.py"} <= parsers
    assert len(parsers) >= 4

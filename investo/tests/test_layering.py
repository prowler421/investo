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

USGAAP_LITERAL_ALLOWED: Final = {
    "normalize/tags.py": "the chain registry: the single home for every us-gaap tag",
}
"""M2 widens M1's ``ingest/`` rule to the **whole package**, with a one-key allowlist.

M1 argued it for ``ingest/`` on the grounds that a tag literal there *"is the first line of a second,
shadow copy of ``normalize/tags.py``."* The argument is stronger for ``report/`` and ``analyze/``,
because those have a plausible reason to want one — a chart label, a hardcoded fallback for a metric
that came back empty — and the disagreement it produces is between the number and its own provenance
line.
"""

_M2_TREES: Final = ("normalize/", "report/")
"""The trees M2 adds, and the scope of five of its six new rules."""

COMMAND_CLOCK_READ_ALLOWED: Final = {
    "cli.py": "`--as-of` is rejected as future against today, and `cache prune` needs a now",
    "fetch.py": "`run_fetch` defaults `as_of` to today when the caller passed none",
    "facts.py": "`run_facts` does the same, once, before threading the result down",
}
"""Modules **outside** ``ingest/`` that may read a clock, and why. Pinned, because the list grows.

``docs/m2/README.md`` §3 said *"``cli.py`` and ``fetch.py`` are the only modules permitted to"* — true
when it was written and false the moment M2 added a third command body. An enumeration in prose that
needs editing every milestone is the thing that just went stale; an enumeration in a test fails the
build instead, which is the difference between a rule and a description.

The **rule** is unchanged and is what the entries above have to satisfy: the clock is read at a command
boundary, once, and the resolved date is threaded down. Every entry here is a command body. Nothing
under ``normalize/`` or ``report/`` may read one at all —
:func:`test_no_clock_read_in_normalize_or_report`, with an empty allowlist.
"""

_CLOCK_ATTRS: Final = frozenset({"now", "today", "utcnow"})
_CLOCK_OWNERS: Final = frozenset({"datetime", "date"})
_DOMAIN_FORBIDDEN: Final = {"httpx", "urllib", "socket", "requests", "sqlite3", "pathlib"}
_NORMALIZE_FORBIDDEN: Final = _DOMAIN_FORBIDDEN - {"pathlib"}
"""``docs/m2/05-testing.md`` §4 lists five imports, not ``domain/``'s six.

``pathlib`` is deliberately absent: nothing under ``normalize/`` or ``report/`` writes a file today —
``serialize`` returns a string and the command owns the path — but M3's renderer will legitimately need
to read a template from disk, and a rule that has to be relaxed in the milestone after the one that
added it was never the rule.
"""


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


def _sorts_without_a_key(tree: ast.Module) -> list[str]:
    """Every ``sorted(...)`` or ``.sort()`` call with no ``key=`` argument.

    A ``min``/``max`` over facts has the same defect, so those are checked too: all four reduce a
    sequence through ``FiscalPeriod``'s partial order if handed one bare.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id in {"sorted", "min", "max"}:
            name = function.id
        elif isinstance(function, ast.Attribute) and function.attr == "sort":
            name = ".sort"
        else:
            continue
        if not any(keyword.arg == "key" for keyword in node.keywords):
            found.append(f"{name}() at line {node.lineno}")
    return found


def _float_constructions(tree: ast.Module) -> list[str]:
    """``float(...)`` calls and float literals — CLAUDE.md convention 8, checked structurally."""
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
        ):
            found.append(f"float() at line {node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            found.append(f"float literal {node.value!r} at line {node.lineno}")
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


# ---------------------------------------------------------------------------
# The M2/M3 seam — six rules over `normalize/` and `report/`
# ---------------------------------------------------------------------------
def _m2_modules() -> tuple[tuple[str, ast.Module], ...]:
    return tuple((rel, tree) for rel, tree in MODULES if rel.startswith(_M2_TREES))


@pytest.mark.spec
def test_m2_trees_exist_to_be_checked() -> None:
    """Guard the five rules below: each iterates a tree, and an empty tree passes vacuously.

    The same argument as ``test_every_module_in_the_package_was_parsed``, one layer down. If
    ``normalize/`` were renamed, every rule in this section would go green while enforcing nothing —
    which is the failure mode an AST test is supposed to be immune to.
    """
    names = {rel for rel, _ in _m2_modules()}
    for expected in (
        "normalize/tags.py",
        "normalize/facts.py",
        "normalize/statements.py",
        "report/serialize.py",
    ):
        assert expected in names, f"{expected} not found among {sorted(names)}"


@pytest.mark.spec
def test_usgaap_literal_only_in_tags() -> None:
    """``normalize/tags.py`` is the **only** module in the package that may name a ``us-gaap`` tag.

    The failure this prevents is two tag tables. A hardcoded fallback in ``report/`` for a metric that
    came back empty produces a number whose provenance line names a different tag than the one that
    produced it — the report disagreeing with its own appendix, which is the one failure mode this
    project's first stated property exists to rule out.
    """
    offenders = {
        rel
        for rel, tree in MODULES
        if any("us-gaap" in text.lower() for text in _value_literals(tree))
    }
    unexpected = offenders - set(USGAAP_LITERAL_ALLOWED)
    assert not unexpected, f"us-gaap literal outside the registry: {sorted(unexpected)}"


@pytest.mark.spec
def test_the_usgaap_allowlist_holds_only_the_registry() -> None:
    """One key, pinned. A second entry is where a shadow tag table starts."""
    assert set(USGAAP_LITERAL_ALLOWED) == {"normalize/tags.py"}


@pytest.mark.spec
def test_the_registry_actually_holds_the_tags() -> None:
    """The converse, and it is not redundant.

    Moving the chains into a data file or a dict comprehension over strings assembled elsewhere would
    satisfy the rule above perfectly. This fails if the registry stops being the registry — the same
    shape as ``test_client_actually_holds_the_hosts``.
    """
    trees = dict(MODULES)
    literals = _value_literals(trees["normalize/tags.py"])
    assert "us-gaap" in literals, "the registry no longer names its taxonomy"
    assert sum(1 for text in literals if text.startswith("Revenue")) >= 2


@pytest.mark.spec
def test_no_clock_read_in_normalize_or_report() -> None:
    """Nothing under ``normalize/`` or ``report/`` reads a clock. **Empty allowlist**, unlike ``ingest/``'s.

    ``as_of`` is resolved at the command boundary and threaded down. A ``date.today()`` in the pipeline
    makes two runs either side of midnight differ, and DESIGN.md §11 would report that as
    nondeterminism rather than as the design mistake it is — the worst kind of bug report, because it
    sends the reader looking at the serializer.
    """
    for rel, tree in _m2_modules():
        reads = sorted(set(_clock_reads(tree)))
        assert not reads, f"{rel} reads the clock: {reads}"


@pytest.mark.spec
def test_only_a_command_body_reads_a_clock() -> None:
    """The other half of the clock rule: *which* modules outside ``ingest/`` are allowed one.

    ``test_no_clock_read_in_normalize_or_report`` forbids it in the two trees the pipeline lives in,
    which is the rule that matters for determinism. This one pins the positive list, because the
    failure it catches is different: a clock read arriving in a *new* module — M3's ``analyze.py`` is
    the next candidate — that is neither under ``normalize/`` nor obviously a command boundary. That
    would pass every other rule in this file and would put a second, later `as_of` into a run that had
    already resolved one.
    """
    offenders = {
        rel
        for rel, tree in MODULES
        if not rel.startswith("ingest/") and not rel.startswith(_M2_TREES) and _clock_reads(tree)
    }
    assert offenders == set(COMMAND_CLOCK_READ_ALLOWED), (
        f"the set of clock-reading command bodies moved: {sorted(offenders)}"
    )


@pytest.mark.spec
def test_every_clock_reading_module_is_a_command_body() -> None:
    """Each exemption is a command body, not a helper that grew one.

    Pinned as a key set for the reason the ``sec.gov`` allowlist is: the cheapest way to make the test
    above pass is to add the offending module, and that repair should be a visible edit to a set that
    says why each member is in it.
    """
    assert set(COMMAND_CLOCK_READ_ALLOWED) == {"cli.py", "fetch.py", "facts.py"}
    names = {rel for rel, _ in MODULES}
    for rel in COMMAND_CLOCK_READ_ALLOWED:
        assert rel in names, f"{rel} no longer exists"
        assert "/" not in rel, "a command body sits at the package root, next to cli.py"


@pytest.mark.spec
def test_normalize_imports_no_io() -> None:
    """A normalization layer that *can* fetch is one that will, and then the warm-run guarantee is gone."""
    for rel, tree in _m2_modules():
        roots = {name.split(".")[0] for name in _imported_modules(rel, tree)}
        offending = roots & _NORMALIZE_FORBIDDEN
        assert not offending, f"{rel} imports {sorted(offending)}"


@pytest.mark.spec
def test_normalize_does_not_import_downstream_layers() -> None:
    """DESIGN.md §3's flow is one-directional.

    ``report/serialize.py`` importing ``normalize`` is correct; the reverse is not. An upward import is
    how a coverage report acquires an opinion about page layout.
    """
    for rel, tree in MODULES:
        if not rel.startswith("normalize/"):
            continue
        imported = _imported_modules(rel, tree)
        upward = {
            name
            for name in imported
            if name.startswith(("investo.report", "investo.analyze", "investo.backtest"))
        }
        assert not upward, f"{rel} imports {sorted(upward)}"


@pytest.mark.spec
def test_every_sort_names_a_key() -> None:
    """No sort under ``normalize/`` or ``report/`` may use a partial key.

    ``FiscalPeriod`` compares on ``(end, kind)`` with ``start`` excluded, so two durations with the same
    end and kind compare **equal** — and Python's stable sort then returns input order for the tie,
    where input order descends from ``dict`` iteration over a parsed JSON payload. Deterministic today,
    not a guarantee, and invisible when wrong.

    An AST rule rather than a convention for exactly that reason: the failure it prevents does not show
    up in any run that happens to agree.
    """
    for rel, tree in _m2_modules():
        offenders = _sorts_without_a_key(tree)
        assert not offenders, f"{rel} sorts without a key: {offenders}"


@pytest.mark.spec
def test_no_float_in_normalize_or_report() -> None:
    """CLAUDE.md convention 8, at the layer where the temptation appears.

    A fill rate looks like a ``float`` and ``json.dumps`` accepts one. The AST check covers the
    ``float(value)`` route; ``test_serialize::test_values_are_quoted_in_the_raw_text`` covers the other
    one, a ``JSONEncoder`` that passes a ``Decimal`` through unquoted.
    """
    for rel, tree in _m2_modules():
        offenders = _float_constructions(tree)
        assert not offenders, f"{rel} constructs a float: {offenders}"


@pytest.mark.spec
def test_the_sort_and_float_detectors_actually_detect() -> None:
    """Guard the two new detectors against their own false negatives.

    Both are new AST predicates rather than reuses of an existing one, so each gets the treatment
    ``test_concatenated_and_interpolated_literals_are_caught`` gives the literal walker: a snippet that
    must trip it, parsed from source so the test names its own input.
    """
    tree = ast.parse(
        "a = sorted(xs)\n"
        "b = sorted(xs, key=f)\n"
        "xs.sort()\n"
        "c = max(xs)\n"
        "d = float(x)\n"
        "e = 0.5\n"
        "g = Decimal('0.5')\n"
    )
    sorts = _sorts_without_a_key(tree)
    assert len(sorts) == 3, sorts
    floats = _float_constructions(tree)
    assert len(floats) == 2, floats


@pytest.mark.spec
def test_metric_still_unreferenced_in_ingest_after_m2_exists() -> None:
    """The M1/M2 seam, checked now that the other side of it is built.

    ``test_metric_unreferenced_in_ingest`` passed in M1 when no consumer of ``Metric`` existed at all,
    which is a weaker fact than it looks. This asserts the same rule holds with ``normalize/`` present
    and importing ``Metric`` freely — and that the layer which *is* allowed to name it does.
    """
    trees = dict(MODULES)
    assert "Metric" in _bound_names(trees["normalize/tags.py"])
    for rel, tree in MODULES:
        if rel.startswith("ingest/"):
            assert "Metric" not in _bound_names(tree), f"{rel} references Metric"


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

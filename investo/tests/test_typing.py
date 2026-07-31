"""The two guarantees the type checker enforces, and the one runtime cannot.

``docs/m1/06-testing.md`` §5: DESIGN.md §5.4 says the cover-page and diluted share counts are
distinguished by *distinct types*, and both are ``Decimal`` at runtime — which is the whole reason
``NewType`` was chosen. A runtime assertion is therefore impossible by construction. CLAUDE.md still
applies: the violation has to be attempted and has to fail. So the attempt lives in
``tests/fixtures/typing/`` and this file runs basedpyright over it.

**This is the most fragile test in the suite, and the fragility is accepted deliberately.** It
asserts on the output of a tool that is free to rephrase its diagnostics. Three things keep the
blast radius small, and the first is a rule:

1. **Assert on the presence of an error and on its line. Never on message text.** Wording is what
   changes across releases; an error on the marked line is what the guarantee is about.
2. basedpyright is pinned ``>=1.39,<2`` and CI runs ``uv sync --frozen``, so the version moves only
   on a deliberate lock update — a commit someone is looking at.
3. The fixture files are a few lines each, so a line-attribution change is obvious rather than
   mysterious.

The expected line is read out of the fixture by looking for its ``# ERROR:`` marker rather than
hard-coded here, so editing a fixture cannot silently point this test at the wrong line.

The fixture directory is excluded from ``[tool.basedpyright] include`` — every file in it is
*supposed* to fail, and ``make typecheck`` would fail on them otherwise. This test therefore writes
its own throwaway config that includes the directory instead of relying on the command line to
override an exclusion.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).parent.parent
FIXTURES: Final = Path(__file__).parent / "fixtures" / "typing"
MARKER: Final = "# ERROR:"
TIMEOUT_SECONDS: Final = 300.0

EXPECTED_FIXTURES: Final = (
    "accession_cik_attribute.py",
    "cover_shares_as_diluted.py",
    "framerow_as_rawfact.py",
)
"""The three violations §5 names. Pinned so a deleted fixture fails rather than a shrinking run."""


def _command() -> list[str]:
    """How to invoke basedpyright, preferring the module over the console script.

    The Makefile runs ``uv run basedpyright``, which resolves the script from the project
    environment. ``sys.executable -m basedpyright`` reaches the same install without depending on
    ``PATH``, which matters because the autouse ``clean_env`` fixture chdirs out of the repo.
    """
    if importlib.util.find_spec("basedpyright") is not None:
        return [sys.executable, "-m", "basedpyright"]
    found = shutil.which("basedpyright")
    return [found] if found else []


def _marked_line(path: Path) -> int:
    """The 1-based line number carrying the ``# ERROR:`` marker.

    Read from the file rather than hard-coded, so adding a line to a fixture cannot make this test
    assert against a line that no longer holds the violation — which would pass for the wrong reason
    if the file happened to error elsewhere.
    """
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if MARKER in line:
            return number
    raise AssertionError(f"{path.name} carries no {MARKER!r} marker")


def _diagnostics(tmp_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Run basedpyright over the fixture directory; return its errors grouped by file name.

    A throwaway ``pyrightconfig.json`` is used rather than the repo's own, for two reasons.
    ``pyproject.toml`` *excludes* this directory, and whether a path on the command line overrides
    an exclusion is an implementation detail of the checker — a detail that, if it changed, would
    make this test find zero diagnostics and pass every assertion phrased as "no unexpected errors".
    ``extraPaths`` points at ``src`` so ``investo`` resolves from the working tree.
    """
    config = tmp_path / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "include": [str(FIXTURES)],
                "extraPaths": [str(REPO_ROOT / "src")],
                "typeCheckingMode": "strict",
                "pythonVersion": "3.13",
                "reportMissingTypeStubs": False,
                "reportAny": False,
                "reportExplicitAny": False,
                "reportUnusedCallResult": False,
                "reportUnusedParameter": False,
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [*_command(), "--outputjson", "--project", str(config)],
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_SECONDS,
    )
    if not completed.stdout.strip():
        pytest.fail(f"basedpyright produced no JSON. stderr:\n{completed.stderr}")

    payload: Any = json.loads(completed.stdout)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for diagnostic in payload["generalDiagnostics"]:
        if diagnostic["severity"] != "error":
            continue
        grouped.setdefault(Path(str(diagnostic["file"])).name, []).append(diagnostic)
    return grouped


@pytest.fixture(scope="module")
def diagnostics(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[dict[str, Any]]]:
    """One basedpyright invocation for the whole module.

    Module-scoped because starting the checker costs a second or two and the three assertions below
    are about one run over one directory. §5 notes that if this ever becomes slow enough to be
    annoying the fix is to fold it into ``make typecheck`` — not to delete it, which would downgrade
    a guarantee to a comment.
    """
    if not _command():
        pytest.skip("basedpyright is not installed; run `uv sync` to install the dev group")
    return _diagnostics(tmp_path_factory.mktemp("typing"))


@pytest.mark.typing
def test_every_typing_fixture_is_present() -> None:
    """Guard the guards: three files, and the run is worthless if one has gone missing.

    A deleted fixture would take its guarantee with it and leave a green suite behind, because the
    remaining assertions only speak about the files they name.
    """
    found = sorted(path.name for path in FIXTURES.glob("*.py"))
    assert found == sorted(EXPECTED_FIXTURES)
    for name in EXPECTED_FIXTURES:
        assert _marked_line(FIXTURES / name) > 0


@pytest.mark.typing
@pytest.mark.spec
@pytest.mark.parametrize("name", EXPECTED_FIXTURES)
def test_each_typing_fixture_errors_on_its_marked_line(
    name: str, diagnostics: dict[str, list[dict[str, Any]]]
) -> None:
    """§5: each fixture performs a forbidden thing, and basedpyright must reject it.

    Two assertions per file, and both are needed. "At least one error" alone would pass if the
    fixture failed for an unrelated reason — a typo, a missing import — which is how a type-level
    guarantee quietly stops being tested. The line number is what ties the error to the violation.

    Nothing here reads ``message``. basedpyright is free to rephrase its diagnostics, and a test
    that asserted on wording would break on an upgrade that changed nothing about the guarantee.
    """
    errors = diagnostics.get(name, [])
    assert errors, f"{name} produced no basedpyright error"

    expected = _marked_line(FIXTURES / name)
    reported = sorted({int(error["range"]["start"]["line"]) + 1 for error in errors})
    assert expected in reported, f"{name}: errors on {reported}, expected one on {expected}"


@pytest.mark.typing
@pytest.mark.spec
def test_cover_shares_rejected_as_diluted(
    diagnostics: dict[str, list[dict[str, Any]]],
) -> None:
    """DESIGN.md §5.4: a cover-page share count cannot be a per-share denominator.

    The named violation test from ``docs/m1/06-testing.md`` §4. Using the cover-page count where
    diluted weighted-average shares belong is a classic error, and §5.4 says the distinction is
    *"enforced by distinct types"* — a claim that is either checked here or is a comment.

    Both counts are ``Decimal`` at runtime, so no runtime assertion is possible: ``CoverShares`` is
    assignable to ``Decimal`` and ``Decimal`` is not assignable to ``CoverShares``, which is a
    property of the annotations and nothing else.
    """
    assert diagnostics.get("cover_shares_as_diluted.py")


@pytest.mark.typing
@pytest.mark.spec
def test_framerow_not_a_rawfact(diagnostics: dict[str, list[dict[str, Any]]]) -> None:
    """DESIGN.md §4.2: a frames value cannot enter the subject company's series.

    SEC's frame selection is not point-in-time stable — a CY2025Q1 frame can resolve to a 2026
    filing — so it is legitimate for peer cross-sections and illegitimate for the subject's history.
    ``FrameRow`` being a distinct type from ``RawFact`` *is* that restriction; a shared type would
    leave it to a code review to notice.
    """
    assert diagnostics.get("framerow_as_rawfact.py")


@pytest.mark.typing
@pytest.mark.spec
def test_accession_has_no_cik_attribute(diagnostics: dict[str, list[dict[str, Any]]]) -> None:
    """``docs/m1/01-domain-types.md`` §1: no company CIK is derived from an accession.

    The leading ten digits identify the *submitter*, which for most issuers is a filer agent.
    Apple's history contains both patterns, so the wrong rule produces correct answers on some
    filings and a nonexistent CIK on others — a 404 that reads as missing data.

    ``test_provenance.py`` asserts the same absence at runtime with ``hasattr``. Both are worth
    having: the runtime check catches a ``__getattr__`` that would satisfy the type checker, and
    this one catches an annotation-only property that ``hasattr`` would not reach.
    """
    assert diagnostics.get("accession_cik_attribute.py")

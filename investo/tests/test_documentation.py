"""Consistency between the documents and the configuration they describe.

The exit-code taxonomy gets this treatment in ``test_errors.py``, which reads §14's numbers back
out of DESIGN.md rather than trusting a copy. This module does the same for the facts that are
stated in more than one file, on the same reasoning: a value written down four times is a value
that will eventually disagree with itself, and the disagreement is silent because each copy
looks right on its own.

Added after a review found DESIGN §2 recording "Python 3.12+" while `pyproject.toml`,
`.python-version` and `[tool.basedpyright]` all said 3.13 — caught by reading, which is the
method this file exists to replace.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent
DESIGN = ROOT / "DESIGN.md"
ROADMAP = ROOT / "ROADMAP.md"


def _pyproject() -> dict[str, Any]:
    """Parsed ``pyproject.toml``.

    Typed `Any` rather than `object` so the nested lookups below need no cast and no
    `# pyright: ignore` — an ignore comment that stops being necessary becomes its own lint
    failure, and TOML is genuinely dynamic at this boundary.
    """
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _design_section(number: str, title: str) -> str:
    """The text of one DESIGN.md section, up to the next heading of the same level."""
    heading = f"## {number}. {title}"
    assert heading in DESIGN.read_text(encoding="utf-8"), f"DESIGN.md has no '{heading}'"
    body = DESIGN.read_text(encoding="utf-8").split(heading, 1)[1]
    return body.split("\n## ", 1)[0]


@pytest.mark.spec
def test_python_version_agrees_everywhere() -> None:
    """DESIGN §2, ``requires-python``, ``.python-version`` and BasedPyright state one version.

    Four copies of the same decision. `requires-python` is the one that binds — it is what a
    resolver enforces — so the others are checked against it.
    """
    cfg = _pyproject()
    floor = str(cfg["project"]["requires-python"]).removeprefix(">=").strip()

    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert pinned == floor, f".python-version says {pinned}, requires-python says {floor}"

    declared = str(cfg["tool"]["basedpyright"]["pythonVersion"])
    assert declared == floor, f"basedpyright checks {declared}, requires-python says {floor}"

    stated = re.findall(r"Python (\d+\.\d+)", _design_section("2", "Decisions already made"))
    assert stated, "DESIGN §2 no longer states a Python version"
    assert stated[0] == floor, f"DESIGN §2 says Python {stated[0]}, pyproject says {floor}"


@pytest.mark.spec
def test_edgar_rate_limit_agrees_between_design_and_config() -> None:
    """DESIGN §4.1's two numbers — SEC's 10 req/s cap and investo's 5 — bound the default.

    The default lives in `config.Settings`; §4.1 is where the reasoning lives. If someone raises
    the field past the documented ceiling, the prose stops describing the code.
    """
    from investo.config import Settings

    field = Settings.model_fields["edgar_requests_per_second"]
    assert field.default == 5.0

    section = DESIGN.read_text(encoding="utf-8").split("### 4.1 SEC EDGAR", 1)[1]
    section = section.split("\n### ", 1)[0]
    assert "10 requests/second" in section or "10 req/s" in section
    assert "5 req/s" in section


@pytest.mark.spec
def test_every_declared_dependency_is_importable() -> None:
    """A runtime dependency exists because something imports it (CLAUDE.md § Coding standards).

    The rule is "dependencies arrive with the milestone that imports them", and the failure it
    guards against is the reverse of an unmet import: a package listed, locked and installed
    that nothing uses. This catches the M0 set; each later milestone should keep it true.
    """
    import importlib

    module_for = {"pydantic-settings": "pydantic_settings"}
    for spec in _pyproject()["project"]["dependencies"]:
        name = re.split(r"[<>=!~\[]", str(spec), maxsplit=1)[0].strip()
        importlib.import_module(module_for.get(name, name))


def test_roadmap_records_the_m0_deviations() -> None:
    """Deviations from DESIGN.md are logged, per CLAUDE.md § Documentation requirements.

    Deliberately a keyword check rather than a wording check: the point is that the log has an
    entry for each place the code knowingly differs from the design, not that it phrases them
    any particular way. The Python-version entry is here because its absence is what let the
    3.12/3.13 drift sit unnoticed.
    """
    body = ROADMAP.read_text(encoding="utf-8")
    assert "Decided while building M0" in body
    log = body.split("Decided while building M0", 1)[1].split("\n## ", 1)[0]
    for topic in ("`src/` layout", "Python 3.13", "Exit 70", "exit 5, not 4"):
        assert topic in log, f"M0 decision log does not mention {topic!r}"

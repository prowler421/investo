"""``report/render.py`` — escaping, URL denial, the version floor, determinism (ROADMAP M3).

DESIGN §9.0 names two CVEs and three required settings. Every one of them gets a test that attempts
the thing it forbids, per CLAUDE.md: *"for any sentence of the form 'X cannot happen', write the
test that attempts X and asserts it fails."* A settings-are-set assertion would pass over an
implementation that set them and then handed WeasyPrint a different object.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from investo.config import Settings
from investo.errors import ExitCode
from investo.report import render
from investo.report.model import ReportModel, build_model
from investo.report.render import (
    RenderSecurityError,
    deterministic_pdf,
    layout,
    render_html,
    render_pdf,
    render_report,
    stylesheet_text,
    templates_dir,
)
from investo.report.serialize import RunInfo, run_info
from tests.conftest import M2_WINDOW, VALID_USER_AGENT, history

HOSTILE_NAME = '<script>alert(1)</script> & "Sons" <img src=x onerror=1>'
"""Two attacks and an ampersand, in a field that really does come from SEC.

M3 has no LLM, so §9.0's *"untrusted text reaches the renderer"* looks like an M6 problem. It is
not: the company name and `sicDescription` come from SEC payloads and `--peers` comes from the user,
and all three are echoed onto the page. An `&` in a company name is not hypothetical at all.
"""

EPOCH = 1_780_000_000


def _run(ticker: str = "AAPL") -> RunInfo:
    settings = Settings(sec_user_agent=VALID_USER_AGENT, tiingo_key="k")
    return run_info(
        settings,
        ticker=ticker,
        as_of=date(2026, 6, 30),
        window=M2_WINDOW,
        lookback_years=5,
        manifest_hash="0" * 64,
        version="0.1.0",
    )


def _model(*, name: str = "Apple Inc.", brief: bool = False) -> ReportModel:
    subject = history("AAPL.trimmed.json", ticker="AAPL", cik=320193, name=name)
    return build_model(subject, _run(), brief=brief)


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_templates_come_from_the_installed_package() -> None:
    """Not ``Path(__file__).parent``, which re-opens the hole the ``src/`` layout closes.

    The failure that prevents is CI green against a template that was never packaged, discovered by
    the first person to install a wheel — so the assertion is that the resolved directory is inside
    the *installed* package rather than inside the working tree.
    """
    import investo

    package_root = Path(str(investo.__file__)).parent
    assert templates_dir().is_relative_to(package_root)
    assert (templates_dir() / "report.css").is_file()


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_a_hostile_company_name_is_escaped() -> None:
    """§9.0: autoescape on. Jinja does not do it by default and the field is genuinely untrusted."""
    html = render_html(_model(name=HOSTILE_NAME))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


@pytest.mark.spec
def test_a_hostile_company_name_still_renders_to_a_pdf() -> None:
    """An escaping bug that *crashes* is a much better outcome than one that does not.

    Stated as its own test because the assertion above passes on a renderer that escapes correctly
    and then throws — and a report that cannot be produced for a company with an ampersand in its
    name is a different bug with the same fixture.
    """
    pdf = render_pdf(_model(name=HOSTILE_NAME), source_date_epoch=EPOCH)
    assert pdf.startswith(b"%PDF")


@pytest.mark.spec
def test_undefined_is_strict() -> None:
    """A typo'd variable must not render as a blank.

    In this document a missing figure would then look exactly like a legitimately absent one — and
    absence has a deliberate rendering (``—`` plus a stated reason) that a blank does not match.
    """
    from jinja2 import UndefinedError

    env = render.environment()
    template = env.from_string("{{ model.nonexistent_attribute }}")
    with pytest.raises(UndefinedError):
        _ = template.render(model=_model())


# ---------------------------------------------------------------------------
# The url_fetcher
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.invalid/x.png",
        "http://evil.invalid/x.png",
        "file:///etc/passwd",
        "//evil.invalid/x.png",
        "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'/>",
    ],
)
def test_the_url_fetcher_denies_every_scheme(url: str) -> None:
    """§9.0's deny-by-default, strengthened at M3 to deny-everything.

    Every chart embeds as a ``data:`` URI and the stylesheet is a separate object, so there is no
    legitimate URL in the document and the fetcher has no allow branch to get wrong. The
    ``data:image/svg+xml`` case is in the list because it is the one someone would be tempted to
    allow when promoting a chart to SVG — and §9.0 says an SVG must be a *file*, not a data URI.
    """
    template = (Path(__file__).parent / "fixtures" / "report" / "hostile_urls.html").read_text(
        encoding="utf-8"
    )
    with pytest.raises(RenderSecurityError):
        _ = layout(template.replace("__URL__", url))


def test_the_denial_is_a_config_error_not_an_upstream_failure() -> None:
    """Exit 4 promises "upstream fetch failure after retries", and nothing was fetched."""
    assert RenderSecurityError("x").exit_code == ExitCode.CONFIG_ERROR


# ---------------------------------------------------------------------------
# The disclaimer
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_disclaimer_matches_readme() -> None:
    """§10 requires the disclaimer on every report. It is duplicated, so it is asserted.

    ``report/model.py`` holds the text rather than parsing README for it — a renderer that read a
    markdown file to find its own legal text would make that text depend on a heading nobody thinks
    of as load-bearing. The cost of the duplication is this test, and it is the right trade: the two
    drifting apart is the failure, and comparing them is cheaper than the alternative mechanism.
    """
    from investo.report.model import DISCLAIMER

    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.replace("**", "").split())
    assert " ".join(DISCLAIMER.split()) in normalized


@pytest.mark.spec
def test_the_disclaimer_is_in_the_rendered_html() -> None:
    """Present in the artifact, not only on the model. §10 says *prominent*."""
    from investo.report.model import DISCLAIMER

    for brief in (False, True):
        html = render_html(_model(brief=brief), brief=brief)
        assert "not investment advice" in html.lower()
        assert DISCLAIMER.split(".")[0] in html


# ---------------------------------------------------------------------------
# The version floor
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_a_pre_69_weasyprint_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """§9.0. CVE-2025-68616 lets a redirect bypass a custom ``url_fetcher`` entirely.

    So on 68.x the mitigation above is worth nothing, and the version is the thing doing the work.
    Checked at render time rather than at import so this test can exist at all — an import-time
    check is untestable without reloading the module.
    """
    import weasyprint

    from investo.errors import ConfigError

    monkeypatch.setattr(weasyprint, "__version__", "68.0", raising=False)
    with pytest.raises(ConfigError, match="WeasyPrint"):
        render.check_weasyprint_version()


# ---------------------------------------------------------------------------
# The stylesheet
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_the_stylesheet_never_reaches_the_html() -> None:
    """No ``<style>`` element, no ``<link>``, no interpolation.

    ``<style>`` is an HTML raw-text element, so entity references are not parsed inside it — an
    autoescaped stylesheet would be a broken one, and the usual repair is ``|safe``, which reopens
    the CSS-injection context. Handing WeasyPrint a separate ``CSS`` object closes it structurally.
    """
    html = render_html(_model())
    assert "<style" not in html
    assert "<link" not in html
    assert stylesheet_text().strip() != ""


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_source_date_epoch_is_restored() -> None:
    """A leaked environment variable makes one suite's results depend on the order it ran in."""
    key = "SOURCE_DATE_EPOCH"
    os.environ.pop(key, None)
    with deterministic_pdf(EPOCH):
        assert os.environ[key] == str(EPOCH)
    assert key not in os.environ


def test_source_date_epoch_is_restored_to_a_previous_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1")
    with deterministic_pdf(EPOCH):
        assert os.environ["SOURCE_DATE_EPOCH"] == str(EPOCH)
    assert os.environ["SOURCE_DATE_EPOCH"] == "1"


@pytest.mark.spec
def test_two_renders_of_one_model_are_byte_identical() -> None:
    """§11's gate at renderer scope: same inputs, same machine.

    Not a reproducible-build claim — fonts and FreeType make the cross-machine version false, and
    DESIGN §12 records that rather than leaving it as folklore.
    """
    model = _model()
    first = render_pdf(model, source_date_epoch=EPOCH)
    second = render_pdf(model, source_date_epoch=EPOCH)
    assert first == second


@pytest.mark.spec
def test_a_different_as_of_produces_different_bytes() -> None:
    """The converse, and it is not redundant.

    A renderer that ignored ``SOURCE_DATE_EPOCH`` entirely — or wrote a constant — would pass the
    test above perfectly. The creation date has to be a *function of the inputs*, which means two
    different inputs must differ.
    """
    model = _model()
    first = render_pdf(model, source_date_epoch=EPOCH)
    later = render_pdf(model, source_date_epoch=EPOCH + 86_400)
    assert first != later


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_the_brief_is_exactly_two_pages() -> None:
    """README promises "a 2-page summary", which is a promise about the artifact.

    An equality, unlike the full report's band: a three-page brief is a broken promise rather than a
    content change.
    """
    rendered = render_report(_model(brief=True), source_date_epoch=EPOCH, brief=True)
    assert rendered.pages == 2


@pytest.mark.spec
def test_the_full_report_is_within_a_page_band() -> None:
    """A band, not an equality. A hard page count fails on any content change, and a gate that
    fires on non-events gets deleted the second time it does."""
    rendered = render_report(_model(), source_date_epoch=EPOCH)
    assert 3 <= rendered.pages <= 30, rendered.pages


@pytest.mark.spec
def test_weasyprint_still_exposes_the_page_box() -> None:
    """**The canary.** One named test for a private-API dependency that will eventually break.

    ``report/render.overflows`` reads ``Page._page_box``, which is private and will be renamed some
    day. ``pyproject.toml``'s ``<70`` ceiling stops that arriving unannounced; it does not remove it.
    What decides the *cost* of the break is the split this test is half of:

    - the product degrades — ``Rendered.overflowing`` becomes ``None``, the summary says the check
      was skipped, and the user still gets their PDF;
    - the suite fails **here**, by name, with one message that says what upstream changed, instead
      of an ``AttributeError`` surfacing under every geometry assertion at once.

    So this test failing is not a bug in investo. It is the notification, and the repair is to
    rewrite the walk against whatever replaced the attribute.
    """
    document = layout("<!DOCTYPE html><html><body><p>canary</p></body></html>")
    assert document.pages, "a one-paragraph document laid out to zero pages"
    for page in document.pages:
        assert getattr(page, "_page_box", None) is not None, (
            "WeasyPrint no longer exposes Page._page_box — report/render.overflows needs "
            "rewriting against the replacement (DESIGN.md §11, §12)."
        )


@pytest.mark.spec
def test_nothing_overflows_the_page_box() -> None:
    """§11: page count alone is weak — a table running off the right edge has the same count.

    Asserted against ``()`` and **not** against falsiness, because ``None`` is also falsy and means
    the opposite thing — "the walk could not run" rather than "the walk ran and found nothing". A
    ``assert not rendered.overflowing`` here would pass silently on every future WeasyPrint that
    renamed the attribute, which is exactly the degradation the canary above exists to catch.
    """
    rendered = render_report(_model(), source_date_epoch=EPOCH)
    assert rendered.geometry_available, "the geometry walk did not run; see the canary test"
    assert rendered.overflowing == (), [item.text for item in rendered.overflowing or ()]


@pytest.mark.spec
def test_a_broken_geometry_walk_still_produces_a_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the split, and the half with no natural trigger today.

    Simulated rather than waited for: patch ``overflows`` to raise what a renamed attribute would
    raise, and assert the PDF still comes back with the check marked unavailable. Without this, the
    degradation path is code that has never executed, and the first time it runs will be on a user's
    machine on a WeasyPrint nobody here has.
    """
    from investo.report.render import GeometryUnavailableError

    def broken(_document: object) -> tuple[object, ...]:
        raise GeometryUnavailableError("simulated upstream rename")

    monkeypatch.setattr(render, "overflows", broken)
    rendered = render_report(_model(), source_date_epoch=EPOCH)
    assert rendered.pdf.startswith(b"%PDF")
    assert rendered.overflowing is None
    assert not rendered.geometry_available

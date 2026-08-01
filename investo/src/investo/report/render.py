"""Jinja2 → HTML → WeasyPrint → PDF (ROADMAP M3, DESIGN.md §9.0).

§9.0 is unusually specific for a design document — it names two CVEs, a minimum version, three
required settings and three sources of nondeterminism — because every one of them was going to be
discovered the hard way otherwise. This module implements them and adds the parts §9.0 leaves open.

**The `url_fetcher` denies everything.** §9.0 asks for deny-by-default; at M3 that is strengthened
to deny, full stop, and the strengthening is bought by the PNG choice. Every chart embeds as a
``data:`` URI and the stylesheet is a separate ``CSS`` object rather than a ``<style>`` element or a
link, so there is no legitimate URL in the document and the fetcher has no allow branch to get
wrong. Promoting a chart to SVG — which §9.0 requires be
referenced as a *file*, not a ``data:`` URI (WeasyPrint #134) — converts this into an allowlist of
absolute paths, which is still safe and is a materially weaker thing to reason about: a path
comparison has edge cases (symlinks, ``..``, case-insensitive filesystems, percent-encoding) that
"raise unconditionally" does not.

**The version floor is enforced in code as well as in the lockfile.** CVE-2025-68616 is an SSRF
where ``urllib`` follows a redirect *without* re-invoking a custom fetcher — so on any pre-68.0
version the fetcher above is worth nothing, and §9.0 says so in terms. A lockfile is a claim about
one environment; someone will eventually install into another.

**`SOURCE_DATE_EPOCH` is applied here and restored here, and its value arrives as a parameter.**
Nothing under ``report/`` may read a clock, and a determinism setting the caller has to remember is
one the next caller forgets — at M7 the next caller is a batch runner over hundreds of tickers.

**The overflow check depends on a private WeasyPrint attribute, and that is handled rather than
noted.** ``Page._page_box`` will eventually be renamed; the ``<70`` pin delays that and does not
remove it. So a break degrades in the product (``Rendered.overflowing`` becomes ``None`` and the
command says the check was skipped) and fails by name in the suite
(``test_render::test_weasyprint_still_exposes_the_page_box``). Losing one line of a summary is not a
reason to refuse someone a PDF; losing the check silently is not acceptable either.

Three intermediate values are exposed rather than hidden inside one call, and all three exist for
tests: HTML is fast and diffable where a PDF is neither, ``Document`` is what §11's geometry walk
needs, and bytes are what the determinism gate compares.
"""

# pyright: reportUnknownMemberType=false
#
# WeasyPrint ships no type information, so `HTML.render` and everything reachable from a `Document`
# is `Unknown`. `reportMissingTypeStubs` is already off project-wide for this reason; this is the
# same fact one level down, where the untyped call actually happens.
#
# The geometry walk over `Page._page_box` is untyped for a second and worse reason — it is a
# *private* attribute, so no amount of upstream typing would cover it. That risk is handled by
# degrading rather than by the type checker: see `Rendered.geometry_available`.

from __future__ import annotations

import importlib.resources
import os
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

import weasyprint
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from investo.errors import ConfigError, InvestoError
from investo.report.model import ReportModel

__all__ = [
    "MINIMUM_WEASYPRINT_MAJOR",
    "STYLESHEET",
    "RenderSecurityError",
    "GeometryUnavailableError",
    "Overflow",
    "Rendered",
    "render_report",
    "templates_dir",
    "environment",
    "stylesheet_text",
    "render_html",
    "layout",
    "render_pdf",
    "overflows",
    "deterministic_pdf",
    "check_weasyprint_version",
]

MINIMUM_WEASYPRINT_MAJOR: Final = 69
"""§9.0. CVE-2025-68616 (fixed 68.0) and CVE-2026-49452 (fixed 69.0), plus ``use``-tag inheritance."""

STYLESHEET: Final = "report.css"
FULL_TEMPLATE: Final = "report.html.j2"
BRIEF_TEMPLATE: Final = "brief.html.j2"


class RenderSecurityError(InvestoError):
    """A resource request the renderer refused. Exit 5 — the run was misconfigured, not upstream.

    A separate class from ``ConfigError`` despite sharing its code, because the two are found by
    different people: a config error is the user's to fix, and this one means a template or a model
    value tried to reach the network. Naming it is what makes that visible in a traceback.
    """

    exit_code = ConfigError.exit_code


class GeometryUnavailableError(RuntimeError):
    """WeasyPrint's private layout attribute moved, so the overflow check cannot run.

    Deliberately **not** an :class:`~investo.errors.InvestoError`: it carries no exit code because it
    never reaches the shell. ``render_report`` catches it and degrades; the suite lets it through.
    A separate class rather than a bare ``RuntimeError`` so that ``except`` narrows to this one
    cause — catching ``RuntimeError`` around a layout call would also swallow a real bug.
    """


@dataclass(frozen=True, slots=True)
class Overflow:
    """A box that ran outside the page box, with enough context to find it."""

    page: int
    edge: str
    excess: str
    text: str


def check_weasyprint_version() -> None:
    """Refuse a WeasyPrint older than :data:`MINIMUM_WEASYPRINT_MAJOR`.

    Called at render time rather than at import, so a test can monkeypatch the version and observe
    the refusal — an import-time check is untestable without reloading the module, and a guarantee
    with no violation test is CLAUDE.md's stated failure mode.
    """
    raw = str(getattr(weasyprint, "__version__", "0"))
    head = raw.split(".", 1)[0]
    major = int(head) if head.isdigit() else 0
    if major < MINIMUM_WEASYPRINT_MAJOR:
        raise ConfigError(
            f"WeasyPrint {raw} is too old; >= {MINIMUM_WEASYPRINT_MAJOR} is required.",
            hint=(
                "DESIGN.md §9.0: CVE-2025-68616 lets a redirect bypass the url_fetcher entirely, "
                "so this cannot be mitigated in investo's code. Run `uv sync`."
            ),
        )


def templates_dir() -> Path:
    """The packaged template directory.

    ``importlib.resources``, never ``Path(__file__).parent / "templates"``. The ``src/`` layout
    exists so tests import the installed package rather than the working tree (ROADMAP § Decided
    during design), and ``__file__`` re-opens exactly that hole: CI green against a template that
    was never packaged, discovered by the first person to install a wheel.
    """
    return Path(str(importlib.resources.files("investo.report") / "templates"))


def environment() -> Environment:
    """The Jinja environment, with the four settings that are decisions rather than defaults.

    ``autoescape=True`` **unconditionally**, not ``select_autoescape``, which chooses by file
    extension — so a template renamed from ``.html.j2`` to ``.j2`` would silently lose escaping, and
    a rename is not a change anyone reviews as a security edit. There is one output format here, so
    the unconditional form is both simpler and stronger.

    ``StrictUndefined`` because a typo'd variable otherwise renders as an empty string, and in this
    document a missing figure would then look exactly like a legitimately absent one — which has a
    *deliberate* rendering (``—`` plus a stated reason) that a blank does not match. ``facts.py``'s
    argument: *"a blank row is indistinguishable from a rendering bug."*

    ``auto_reload=False`` and no bytecode cache: the first stats the template on every render, and
    the second writes files to a location that depends on the environment, which is a determinism
    hazard that only shows up on the second run.
    """
    return Environment(
        loader=FileSystemLoader(str(templates_dir())),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        auto_reload=False,
    )


def render_html(model: ReportModel, *, brief: bool = False) -> str:
    """The document as HTML. **No stylesheet, and no link to one.**

    The CSS is handed to WeasyPrint as a separate ``CSS`` object in :func:`layout` rather than
    written into a ``<style>`` element or linked with an ``<a href>``. Three things fall out, and
    the third is the one worth having:

    - No URL in the document, so :func:`_deny` needs no allow branch.
    - No escaping problem. ``<style>`` is an HTML raw-text element, so entity references are *not*
      parsed inside it — autoescaping a stylesheet through Jinja would turn every ``"`` in a
      ``font-family`` into ``&quot;`` and break the rule. The alternative is ``|safe``, which is
      the one filter this project will not have in a template that renders filing text at M6.
    - **The stylesheet never passes through the template engine at all**, which closes the
      CSS-injection context autoescape does not cover — structurally, rather than by a test that a
      value was not interpolated. ``test_render::test_the_stylesheet_is_never_templated`` asserts
      the file is not reachable from any template's AST.
    """
    env = environment()
    template = env.get_template(BRIEF_TEMPLATE if brief else FULL_TEMPLATE)
    return template.render(model=model)


def stylesheet_text() -> str:
    """The packaged print stylesheet, verbatim."""
    return (templates_dir() / STYLESHEET).read_text(encoding="utf-8")


def _deny(url: str) -> NoReturn:
    """Every URL. See the module docstring.

    The URL is truncated in the message because a ``data:`` URI of a 300 dpi chart is a megabyte of
    base64, and an error that scrolls a terminal is an error nobody reads.
    """
    raise RenderSecurityError(
        f"the renderer resolves no URLs; refused: {url[:120]}",
        hint=(
            "Charts embed as data: URIs and the stylesheet is inlined, so a URL here means a "
            "template or a model value reached for a remote resource. DESIGN.md §9.0."
        ),
    )


def layout(html: str) -> Any:
    """Parse and lay out, stopping before ``write_pdf``.

    Returns WeasyPrint's ``Document``, which is what §11's geometry check needs — page count alone
    is a weak assertion, since a table running off the right edge has the same page count as one
    that does not.

    ``presentational_hints=False`` is the default and is passed explicitly, because CVE-2026-49452
    is scoped precisely to rendering untrusted HTML with them on and "we get this by default" is a
    fact about a version rather than about this code.
    """
    check_weasyprint_version()
    document = weasyprint.HTML(string=html, url_fetcher=_deny, base_url=None)
    sheet = weasyprint.CSS(string=stylesheet_text(), url_fetcher=_deny, base_url=None)
    return document.render(stylesheets=[sheet], presentational_hints=False)


@contextmanager
def deterministic_pdf(epoch: int) -> Generator[None]:
    """Set ``SOURCE_DATE_EPOCH`` for the duration, then put the environment back as it was.

    WeasyPrint derives ``/CreationDate`` and the document ``/ID`` from the clock unless this is set;
    §9.0 makes it one of the three things without which the determinism gate *"fails on day one."*

    Restoring matters as much as setting. A library function that mutates ``os.environ`` and leaves
    it changed has altered the behaviour of everything that runs after it in the process — including
    the next test, which then passes or fails depending on the order the suite ran in.
    """
    key = "SOURCE_DATE_EPOCH"
    previous = os.environ.get(key)
    os.environ[key] = str(epoch)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


@dataclass(frozen=True, slots=True)
class Rendered:
    """The PDF, plus the two things only the laid-out ``Document`` knows.

    Page count and overflow come from the same layout pass that produced the bytes. Laying an A4
    document out twice to print a page number in a summary would double the slowest step in the
    command, and re-laying it out is also how the number and the file come to disagree.

    ``overflowing`` is ``None`` — not ``()`` — when the geometry walk could not run at all, which is
    a different claim from "nothing overflowed" and is the one that has to survive
    :func:`overflows`' private-API dependency. See :attr:`geometry_available`.
    """

    pdf: bytes
    pages: int
    overflowing: tuple[Overflow, ...] | None

    @property
    def geometry_available(self) -> bool:
        """Whether the overflow check ran. **A canary, and it is allowed to go dark in production.**

        :func:`overflows` reads WeasyPrint's private ``Page._page_box``, and a private attribute will
        eventually be renamed. The ``<70`` pin delays that; it does not remove it. So the failure is
        split deliberately:

        - **In the product, a broken geometry walk must not break the report.** A user running
          ``investo analyze`` wants a PDF, and losing one line of a summary is not a reason to refuse
          one. So ``render_report`` degrades to ``None`` and the command says the check was skipped.
        - **In the suite, it fails loudly**, through ``test_render::test_weasyprint_still_exposes_the_page_box`` —
          one named test, so an upstream rename produces a single failure that says what happened
          rather than an ``AttributeError`` under every geometry assertion.

        That split is the whole mitigation, and it is worth more than the version ceiling: the
        ceiling stops the break arriving unannounced, and this decides what the break costs.
        """
        return self.overflowing is not None


def render_report(model: ReportModel, *, source_date_epoch: int, brief: bool = False) -> Rendered:
    """The whole pipeline. Returns bytes and **does not write** — the command owns the path.

    Same rule ``serialize`` follows, for the same reason: §11's assertion is a bytes comparison
    rather than a filesystem fixture, and a renderer that cannot write cannot overwrite.

    Args:
        model: Built by ``report/model.py``. Already holds its charts, so this function imports no
            matplotlib and the escaping tests do not pay for one.
        source_date_epoch: ``as_of`` at midnight UTC, computed at the command boundary. Not ``0``,
            which would date every report to 1970; not the wall clock, which breaks the gate.
        brief: Selects the template. Nothing else differs — see ``report/model.py``.
    """
    html = render_html(model, brief=brief)
    with deterministic_pdf(source_date_epoch):
        document = layout(html)
        payload = document.write_pdf()
    try:
        overflowing = overflows(document)
    except GeometryUnavailableError:
        # Degraded, not fatal — see `Rendered.geometry_available`. A user waiting on a PDF should
        # not lose it because an upstream private attribute was renamed; the suite is where that
        # fails, and it fails by name.
        overflowing = None
    return Rendered(pdf=bytes(payload), pages=len(document.pages), overflowing=overflowing)


def render_pdf(model: ReportModel, *, source_date_epoch: int, brief: bool = False) -> bytes:
    """:func:`render_report`'s bytes. What §11's determinism assertion compares."""
    return render_report(model, source_date_epoch=source_date_epoch, brief=brief).pdf


def overflows(document: Any) -> tuple[Overflow, ...]:
    """Boxes whose right or bottom edge falls outside the page box.

    §11 offers two options for overflow detection and this is the coarser one, deliberately.
    Golden-image diffing needs a rendering backend pinned to a font stack we do not control, and its
    failures are uninterpretable without opening a picture; a geometry walk fails with a page number
    and the offending text.

    It reaches into ``_page_box``, which is private, and that is a liability worth stating rather
    than hiding: this walk will break on some future WeasyPrint. That is what the ``<70`` ceiling in
    ``pyproject.toml`` is for, and the caller asserts a readable message rather than an opaque
    ``AttributeError``.
    """
    found: list[Overflow] = []
    for number, page in enumerate(document.pages, start=1):
        box = getattr(page, "_page_box", None)
        if box is None:  # pragma: no cover - version guard, see the docstring
            raise GeometryUnavailableError(
                f"WeasyPrint {getattr(weasyprint, '__version__', '?')} no longer exposes "
                "Page._page_box, so the overflow check in report/render.py cannot run "
                "(DESIGN.md §11, §12)."
            )
        right = box.position_x + box.width
        bottom = box.position_y + box.height
        for child in _descendants(box):
            found.extend(_edges(number, child, right, bottom))
    return tuple(found)


def _descendants(box: Any) -> Iterator[Any]:
    for child in getattr(box, "children", ()) or ():
        yield child
        yield from _descendants(child)


def _edges(page: int, box: Any, right: float, bottom: float) -> Sequence[Overflow]:
    """Compare one box's edges against the page's content edges.

    A float comparison, in a package whose convention 8 bans constructing one — and no ``float()``
    call appears here, because WeasyPrint's geometry attributes already are floats. That is the
    distinction the convention draws: this module does not turn a *financial value* into a float,
    which is the failure the rule exists to prevent. A page coordinate was never a ``Decimal``.
    """
    hits: list[Overflow] = []
    box_right = getattr(box, "position_x", 0) + getattr(box, "width", 0)
    box_bottom = getattr(box, "position_y", 0) + getattr(box, "height", 0)
    if box_right > right:
        hits.append(
            Overflow(
                page=page, edge="right", excess=f"{box_right - right:.1f}pt", text=_text_of(box)
            )
        )
    if box_bottom > bottom:
        hits.append(
            Overflow(
                page=page, edge="bottom", excess=f"{box_bottom - bottom:.1f}pt", text=_text_of(box)
            )
        )
    return hits


def _text_of(box: Any) -> str:
    return str(getattr(box, "text", "") or getattr(getattr(box, "element", None), "tag", ""))[:60]

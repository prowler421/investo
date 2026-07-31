"""Where a number came from (DESIGN.md §3.2, §4.1).

DESIGN.md §3.2's first property: **every number traces to a source.** Each figure carries the
accession number, XBRL tag and fetch timestamp it came from, and a figure that cannot be traced
is not printed. This module is that record.

Three types, and the third is an extension of §3.2's sketch:

:class:`Accession`
    A value type over the accession number, which appears in three spellings across EDGAR.

:class:`SourceRef`
    One fact, one filing. §3.2's shape plus a ``taxonomy`` field — see the class docstring.

:class:`Derivation`
    A computed figure and the refs it was computed from. §3.2 gives every ``Fact`` a single
    ``SourceRef``, but market cap, Q4, gross profit and the ASC 606 stitch each trace to
    several. Recorded as spec question 2 in ``docs/m1/README.md``; accepted on review.

:class:`SourceContext`
    What a parser cannot know — the URL a payload arrived from, when it was fetched, and whose
    CIK it describes. Passed *into* every parser so no parser has to reach for a clock or a
    network, which is what makes each one testable from a file on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

__all__ = [
    "Accession",
    "SourceRef",
    "Derivation",
    "Provenance",
    "SourceContext",
]

_ACCESSION_DASHED: Final = re.compile(r"^(\d{10})-(\d{2})-(\d{6})$")
_ACCESSION_BARE: Final = re.compile(r"^(\d{10})(\d{2})(\d{6})$")


@dataclass(frozen=True, slots=True, order=True)
class Accession:
    """An EDGAR accession number, canonically dashed: ``"0000320193-25-000079"``.

    A value type rather than a bare ``str`` because the number appears in three spellings —
    dashed on ``data.sec.gov``, undashed as an ``/Archives/`` directory name, and dashed again
    with an ``-index.htm`` suffix for the filing index page. DESIGN.md §4.1 makes the client
    responsible for those transforms; putting them on the value gives them one home and one
    test, and means no caller has to remember which spelling an endpoint wants.

    **The leading ten digits are not the company's CIK.** They identify the entity that
    *submitted* the filing, which for most companies is a filer agent. Apple's own history
    contains both patterns — ``0000320193-26-000013`` (Apple filing for itself) and
    ``0001140361-26-025622`` (an agent filing on Apple's behalf) — so a rule that reads the CIK
    off the accession produces correct answers on some filings and a nonexistent CIK on others,
    which surfaces as a 404 that looks like missing data.

    This class therefore exposes **no** ``cik`` property, and :meth:`index_url` takes the CIK as
    an argument. The absence is the enforcement; ``tests/fixtures/typing`` attempts the
    attribute access and basedpyright rejects it.
    """

    value: str

    @classmethod
    def parse(cls, raw: str) -> Accession:
        """Normalize either spelling to the canonical dashed form.

        Rejects anything that is not eighteen digits, optionally grouped 10-2-6 by dashes. A
        silently accepted malformed accession becomes a 404 that looks like missing data, which
        ROADMAP M1 names as one of the milestone's two risks — so this raises instead.

        Raises:
            ValueError: if ``raw`` is not a well-formed accession number.
        """
        text = raw.strip()
        dashed = _ACCESSION_DASHED.match(text)
        if dashed is not None:
            return cls(text)
        bare = _ACCESSION_BARE.match(text)
        if bare is not None:
            return cls("-".join(bare.groups()))
        raise ValueError(
            f"{raw!r} is not an accession number. Expected eighteen digits, "
            "optionally grouped as 0000000000-00-000000."
        )

    @property
    def nodashes(self) -> str:
        """The ``/Archives/`` directory-name spelling: ``"000032019325000079"``."""
        return self.value.replace("-", "")

    def index_url(self, cik: int) -> str:
        """URL of the filing index page for this accession under ``cik``.

        ``cik`` is a parameter and not a property for the reason in the class docstring: the
        accession's own leading digits are the submitter's CIK, not the subject company's.
        """
        return (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{self.nodashes}/{self.value}-index.htm"
        )


@dataclass(frozen=True, slots=True)
class SourceRef:
    """One figure's provenance: the filing, the tag, and when we fetched it (DESIGN.md §3.2).

    ``taxonomy`` extends §3.2's sketch. §4.2 requires distinguishing
    ``dei:EntityCommonStockSharesOutstanding`` from
    ``us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding``, and a bare tag string cannot:
    ``Assets`` exists in more than one taxonomy. The appendix prints tag provenance per metric
    (§9.1), and ``us-gaap:Assets`` is the useful spelling of that.

    Attributes:
        accession: The filing this figure was reported in.
        taxonomy: ``"us-gaap"``, ``"dei"``, ``"srt"``, ``"ffd"``, … or ``None`` for a non-XBRL
            source such as a price series.
        tag: The XBRL tag, or ``None`` for a non-XBRL source.
        form: ``"10-K"``, ``"10-Q"``, ``"8-K"``, ``"DEF 14A"``, ``"4"``, ``"S-1/A"``. Not
            restricted to periodic reports — ``companyfacts`` carries registration-statement
            facts too, so nothing may filter on this assuming otherwise.
        filed: The filing date. DESIGN.md §4.2(b)'s ``as_of`` key, and the only date that
            answers "was this knowable then".
        url: Where the payload was fetched from.
        fetched_at: Timezone-aware UTC, always. A naive datetime in a provenance record is a
            timestamp whose meaning depends on the machine that wrote it, and the cache is meant
            to be the immutable record of what the model saw.

    Nothing downstream may read ``fetched_at`` arithmetically. It is displayed — in the fetch
    summary and the appendix — and it participates in ``cache prune``. A figure whose *value*
    depended on when it was fetched would make DESIGN.md §11's byte-identical-output gate
    unsatisfiable.
    """

    accession: Accession
    taxonomy: str | None
    tag: str | None
    form: str
    filed: date
    url: str
    fetched_at: datetime

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None:
            raise ValueError(
                "SourceRef.fetched_at must be timezone-aware. A naive timestamp in a "
                "provenance record means something different on every machine."
            )

    @property
    def qualified_tag(self) -> str | None:
        """``"us-gaap:Assets"`` — the form DESIGN.md §9.1's appendix prints."""
        if self.tag is None:
            return None
        return f"{self.taxonomy}:{self.tag}" if self.taxonomy else self.tag


@dataclass(frozen=True, slots=True)
class Derivation:
    """A computed figure's provenance: the rule that produced it, and its inputs.

    DESIGN.md §3.2 gives every ``Fact`` a single :class:`SourceRef`, but several numbers the
    report prints are computed from more than one fact:

    ==========================================  ==============  ============
    Derived value                               Inputs          First needed
    ==========================================  ==============  ============
    ``Q4 = FY - (Q1+Q2+Q3)``                    4 facts         M2 (§4.2c)
    ``gross profit = revenue - COGS``           2 facts         M2 (§4.2 table)
    ``total liabilities = L&SE - equity``       2 facts         M2 (§4.2 table)
    ``market cap = price * sum(shares)``        1 price + *n*   **M1**
    revenue stitched across ASC 606             2 tags, *n*     M2
    ==========================================  ==============  ============

    A single ``SourceRef`` cannot describe any of them, and the tempting fallback — printing the
    derived number with one of its inputs' refs — is worse than printing nothing, because it
    looks traced. Recorded as spec question 2 in ``docs/m1/README.md``.

    Recursive by construction: ``inputs`` holds :data:`Provenance`, so a derived margin over a
    stitched series nests cleanly.

    ``rule`` is a plain string rather than an enum because the set of rules grows in every
    milestone from M2 to M5, and an enum in ``domain/`` would have to be edited by each of them.

    Attributes:
        rule: A stable identifier for the arithmetic, e.g. ``"market_cap"`` or
            ``"q4_from_annual_minus_quarters"``.
        inputs: Every ref this value was computed from, in a deterministic order.
        note: Human-readable detail the report needs, e.g. ``"classes: GOOGL, GOOG"``. DESIGN.md
            §5.4 requires the report to state which share classes were counted, and it cannot if
            the omission is silent.
    """

    rule: str
    inputs: tuple[Provenance, ...]
    note: str | None = None

    def refs(self) -> tuple[SourceRef, ...]:
        """Every leaf :class:`SourceRef`, flattened, in input order.

        The appendix cites filings, not rules, so it needs the leaves. Written here rather than
        at each call site because a derived margin over a stitched series is three levels deep
        and every consumer would otherwise re-implement the walk.
        """
        found: list[SourceRef] = []
        for item in self.inputs:
            if isinstance(item, Derivation):
                found.extend(item.refs())
            else:
                found.append(item)
        return tuple(found)


type Provenance = SourceRef | Derivation
"""Where a figure came from: one filing, or a rule over several.

``Fact.source`` is annotated with this rather than with ``SourceRef`` so that M2's first derived
number does not need a type change in ``domain/``. Settled in M1 for exactly that reason — every
module written between now and then would otherwise be written against the narrower type.
"""


@dataclass(frozen=True, slots=True)
class SourceContext:
    """What a parser cannot know about the bytes it was handed.

    Every parser in ``ingest/`` has the shape ``parse_x(body: bytes, *, source: SourceContext)``.
    The context carries the URL the payload arrived from, when it was fetched, and — where
    relevant — the CIK it describes, so that each row a parser emits can build a
    :class:`SourceRef` without the parser reaching for a clock or a network.

    That is what makes ``ingest/`` testable: every parser runs against a file on disk with no
    client present, and ``tests/test_layering.py`` asserts that no parser calls
    ``datetime.now()`` or ``date.today()``. A parser that reads the clock cannot be run twice
    against one fixture with the same result, and its output cannot be byte-identical across
    runs — which DESIGN.md §11 makes a CI gate from M3 onward.

    It lives in ``domain/`` rather than ``ingest/`` because it holds no I/O and no EDGAR
    vocabulary — it is the same three fields :class:`SourceRef` already carries, minus the ones
    only the payload knows. Keeping it here also means the module tree matches DESIGN.md §3.1
    exactly, with no invented module.
    """

    url: str
    fetched_at: datetime
    cik: int | None = None

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None:
            raise ValueError("SourceContext.fetched_at must be timezone-aware.")

    def ref(
        self,
        *,
        accession: Accession,
        form: str,
        filed: date,
        taxonomy: str | None = None,
        tag: str | None = None,
    ) -> SourceRef:
        """Build a :class:`SourceRef` for one row, filling in what the context knows."""
        return SourceRef(
            accession=accession,
            taxonomy=taxonomy,
            tag=tag,
            form=form,
            filed=filed,
            url=self.url,
            fetched_at=self.fetched_at,
        )

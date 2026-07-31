#!/usr/bin/env python3
"""Generate the *synthetic* fixtures, deterministically. **Checked in, and read PROVENANCE.md.**

``docs/m1/06-testing.md`` §2 asks for real, reduced payloads — six hard cases, each provably
exhibiting one specific trap — and ``docs/m1/README.md`` sizes curating them at three days of
research that is *"unblocked by everything"* and should run in parallel from day one.

That curation has **not** happened. It cannot happen from an environment with no route to sec.gov,
and a fixture whose trap cannot be pointed at is a fixture that is testing nothing. So rather than
commit six files that look real and are not, this script builds them from an explicit statement of
the trap each one carries, and ``tests/fixtures/edgar/PROVENANCE.md`` records which fixtures are
synthetic and exactly which live fetch replaces each.

Why generated rather than hand-written JSON:

- **The trap is stated in code, next to the data that carries it.** A reviewer reads
  ``_aapl()`` and sees "FY2018 revenue appears twice, tagged fy 2019 and fy 2020" as a line of
  Python, not as something to reconstruct from 400 lines of JSON.
- **Regenerating after a DESIGN change is a command.** Same argument as ``reduce_fixture.py``.
- **Structure is faithful even where content is invented.** Padded-string CIKs, absent ``start``
  keys, ``null`` ``fy``, ``""`` scalars, mixed-case ``"Nasdaq"``, the ``",,"`` items value and the
  ``xslF345X06/`` prefix are all reproduced exactly as observed live, because those are the shapes
  the parsers are written against.

What a synthetic fixture **cannot** do is falsify the design's claims about real payloads. When the
real ones land, the tests should not need changing — that is the test of whether these were faithful.

Usage::

    uv run python tests/make_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------
SELF_FILED = "0000320193-19-000119"
"""Apple filing for itself: the accession's leading digits *are* Apple's CIK."""

AGENT_FILED = "0001140361-19-018465"
"""A filer agent filing on Apple's behalf: the leading digits are the agent's CIK, not Apple's.

Both spellings are in one company's history, which is why `Accession` exposes no `cik` property —
a rule that derives the company CIK from the accession is correct on the first and produces a
nonexistent CIK on the second.
"""


def fact(
    *,
    val: Any,
    end: str,
    start: str | None = None,
    accn: str = SELF_FILED,
    fy: int | None = 2019,
    fp: str | None = "FY",
    form: str = "10-K",
    filed: str = "2019-10-31",
    frame: str | None = None,
) -> dict[str, Any]:
    """One fact row in ``companyfacts`` shape.

    ``start`` is omitted entirely when ``None`` — **the key is absent on instant facts, not
    ``null``**, confirmed live. That is what makes ``classify(start=None, ...) -> INSTANT`` correct
    and what a fixture using ``"start": null`` would fail to exercise.
    """
    row: dict[str, Any] = {}
    if start is not None:
        row["start"] = start
    row["end"] = end
    row["val"] = val
    row["accn"] = accn
    row["fy"] = fy
    row["fp"] = fp
    row["form"] = form
    row["filed"] = filed
    if frame is not None:
        row["frame"] = frame
    return row


def tagged(units: dict[str, list[dict[str, Any]]], *, label: str = "", desc: str = "") -> dict[str, Any]:
    return {"label": label, "description": desc, "units": units}


def payload(cik: int, name: str, facts: dict[str, Any]) -> dict[str, Any]:
    """``cik`` as a **zero-padded string**, as both ``companyfacts`` and ``submissions`` write it."""
    return {"cik": f"{cik:010d}", "entityName": name, "facts": facts}


def annual_series(tag_values: list[tuple[str, str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
    return [fact(start=start, end=end, val=val, **kwargs) for start, end, val in tag_values]


# ---------------------------------------------------------------------------
# companyfacts fixtures
# ---------------------------------------------------------------------------
def _arxs_companyfacts() -> dict[str, Any]:
    """The observed small-filer payload, reproduced from the verbatim head in 04-parsers.md §2.

    Carries, live: a padded-string ``cik``; an ``entityName`` whose casing differs from
    ``submissions``; the unanticipated ``ffd`` taxonomy, **sorted first**; **no ``dei`` section at
    all**; instant facts with the ``start`` key *absent*; ``fy``/``fp`` as ``null`` on
    registration-statement facts; a ``pure`` unit with decimal values; and the §4.2(a) trap — a
    ``2025-01-01``..``2025-03-31`` period tagged ``fy: 2026, fp: "Q1"``.

    The two facts quoted verbatim in the design are exact. The rest is the minimum needed to make it
    a usable payload, and is marked synthetic in PROVENANCE.md.
    """
    return {
        "cik": "0002093536",
        "entityName": "ARXIS, INC.",
        "facts": {
            # `ffd` sorts first, so it is the first thing the parser sees. A taxonomy allowlist would
            # have dropped it — and would drop the next one SEC adds.
            "ffd": {
                "NetFeeAmt": tagged(
                    {
                        "USD": [
                            fact(
                                val=153994.53,
                                end="2026-04-06",
                                accn="0001193125-26-146309",
                                fy=None,
                                fp=None,
                                form="S-1/A",
                                filed="2026-04-08",
                                frame="CY2026Q1I",
                            )
                        ]
                    }
                )
            },
            "us-gaap": {
                # The §4.2(a) trap, verbatim: calendar Q1 2025, labelled fiscal year 2026.
                "AccountsPayableCurrent": tagged(
                    {
                        "pure": [
                            fact(
                                start="2025-01-01",
                                end="2025-03-31",
                                val=0.367,
                                accn="0001193125-26-243043",
                                fy=2026,
                                fp="Q1",
                                form="10-Q",
                                filed="2026-05-28",
                                frame="CY2025Q1",
                            )
                        ]
                    }
                ),
                "Assets": tagged(
                    {
                        "USD": [
                            fact(
                                val=4210000,
                                end="2026-03-31",
                                accn="0001193125-26-243043",
                                fy=2026,
                                fp="Q1",
                                form="10-Q",
                                filed="2026-05-28",
                            )
                        ]
                    }
                ),
            },
            # No `dei` key. A newly-listed filer that has not yet filed a 10-K plausibly has no
            # cover-page facts at all — so no market cap, recorded as an absence rather than a zero.
        },
    }


def _aapl() -> dict[str, Any]:
    """The flagship fixture. Four traps in one payload.

    1. **§4.2(a)** — FY2018 revenue appears twice, once tagged ``fy: 2019`` and once ``fy: 2020``.
       Grouping by ``fy`` therefore puts one period in two different years. Grouping by
       ``(start, end)`` is the only correct rule.
    2. **§4.2(b)** — the quarter ending 2019-06-29 appears under **four** accessions with four
       ``filed`` dates. Dedup is by ``(unit, start, end)`` and the winner is decided by ``filed``,
       which is what ``--as-of`` cuts on.
    3. **ASC 606** — revenue before FY2018 is ``SalesRevenueNet``; after, it is
       ``RevenueFromContractWithCustomerExcludingAssessedTax``. Stitching across the boundary is M2's,
       and it needs both tags present in one payload to be testable at all.
    4. **CIK 320193 is under 1,000,000** — pads to ten digits on ``data.sec.gov`` and does not pad in
       ``/Archives/``. And both accession patterns appear: self-filed and filer-agent.

    The revenue value ``391035000000.01`` is the ``parse_float=Decimal`` violation test's fixture. It
    is not Apple's real revenue; it is a number chosen because ``Decimal(float(...))`` renders it as
    ``391035000000.010009765625``, so a test asserting the exact round-trip fails the moment someone
    "simplifies" the parse hook away.
    """
    revenue_new = "RevenueFromContractWithCustomerExcludingAssessedTax"
    return payload(
        320193,
        "Apple Inc.",
        {
            "us-gaap": {
                # ASC 606: the pre-2018 tag.
                "SalesRevenueNet": tagged(
                    {
                        "USD": annual_series(
                            [
                                ("2015-09-27", "2016-09-24", 215639000000),
                                ("2016-09-25", "2017-09-30", 229234000000),
                            ],
                            fy=2017,
                            filed="2017-11-03",
                        )
                    }
                ),
                revenue_new: tagged(
                    {
                        "USD": [
                            # Trap 1: one period, two fiscal-year labels, two accessions.
                            fact(
                                start="2017-10-01",
                                end="2018-09-29",
                                val=265595000000,
                                fy=2019,
                                filed="2019-10-31",
                                accn=SELF_FILED,
                            ),
                            fact(
                                start="2017-10-01",
                                end="2018-09-29",
                                val=265595000000,
                                fy=2020,
                                filed="2020-10-30",
                                accn="0000320193-20-000096",
                            ),
                            # Trap: the Decimal fixture value.
                            fact(
                                start="2018-09-30",
                                end="2019-09-28",
                                val=391035000000.01,
                                fy=2019,
                                filed="2019-10-31",
                            ),
                            # Trap 2: one quarter, four accessions, four filed dates.
                            *[
                                fact(
                                    start="2019-03-31",
                                    end="2019-06-29",
                                    val=53809000000,
                                    fy=year,
                                    fp="Q3",
                                    form="10-Q",
                                    filed=filed,
                                    accn=accn,
                                )
                                for year, filed, accn in (
                                    (2019, "2019-07-31", "0000320193-19-000076"),
                                    (2019, "2019-10-31", SELF_FILED),
                                    (2020, "2020-05-01", "0000320193-20-000052"),
                                    (2020, "2020-07-31", AGENT_FILED),
                                )
                            ],
                        ]
                    }
                ),
                "NetIncomeLoss": tagged(
                    {
                        "USD": annual_series(
                            [
                                ("2017-10-01", "2018-09-29", 59531000000),
                                ("2018-09-30", "2019-09-28", 55256000000),
                            ]
                        )
                    }
                ),
                "Assets": tagged(
                    {"USD": [fact(val=338516000000, end="2019-09-28"), fact(val=365725000000, end="2018-09-29")]}
                ),
                "WeightedAverageNumberOfDilutedSharesOutstanding": tagged(
                    {
                        "shares": [
                            fact(start="2018-09-30", end="2019-09-28", val=18595651000),
                        ]
                    }
                ),
                "EarningsPerShareDiluted": tagged(
                    {"USD/shares": [fact(start="2018-09-30", end="2019-09-28", val=11.89)]}
                ),
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": tagged(
                    {"shares": [fact(val=4443236000, end="2019-10-18")]}
                )
            },
        },
    )


def _bank() -> dict[str, Any]:
    """A NASDAQ bank. **No ``OperatingIncomeLoss`` line at all** — the §6.10 refusal path.

    Banks do not report operating income, so a capex/FCF chain finds nothing and the correct
    behaviour is to omit the valuation rather than to model a bank as if it were an operating
    company. The fixture's job is to make that absence real rather than hypothetical.
    """
    return payload(
        36104,
        "Example Bancorp Inc.",
        {
            "us-gaap": {
                "Revenues": tagged({"USD": annual_series([("2018-01-01", "2018-12-31", 4820000000)])}),
                "NetIncomeLoss": tagged({"USD": annual_series([("2018-01-01", "2018-12-31", 1210000000)])}),
                "Assets": tagged({"USD": [fact(val=94300000000, end="2018-12-31")]}),
                "Liabilities": tagged({"USD": [fact(val=83100000000, end="2018-12-31")]}),
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": tagged(
                    {"shares": [fact(val=412000000, end="2019-02-01")]}
                )
            },
        },
    )


def _reit() -> dict[str, Any]:
    """A REIT: no operating income **and** no capex tag — a second chain miss on the same filer."""
    return payload(
        1063761,
        "Example Properties Trust",
        {
            "us-gaap": {
                "Revenues": tagged({"USD": annual_series([("2018-01-01", "2018-12-31", 5580000000)])}),
                "NetIncomeLoss": tagged({"USD": annual_series([("2018-01-01", "2018-12-31", 1470000000)])}),
                "Assets": tagged({"USD": [fact(val=35600000000, end="2018-12-31")]}),
                # No PaymentsToAcquirePropertyPlantAndEquipment: REITs tag acquisitions differently.
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": tagged(
                    {"shares": [fact(val=347000000, end="2019-02-15")]}
                )
            },
        },
    )


def _ipo() -> dict[str, Any]:
    """A recent IPO: **six quarters of history**, under §5.1's 12-quarter floor.

    Below 12 quarters the valuation is omitted, same as for banks and REITs (DESIGN.md §6.10). The
    boundary needs a fixture on the wrong side of it, or the rule is only ever tested on companies
    that comfortably pass.
    """
    quarters = [
        ("2024-01-01", "2024-03-31", 41000000, "Q1", 2024),
        ("2024-04-01", "2024-06-30", 47500000, "Q2", 2024),
        ("2024-07-01", "2024-09-30", 52200000, "Q3", 2024),
        ("2024-10-01", "2024-12-31", 61800000, "Q4", 2024),
        ("2025-01-01", "2025-03-31", 66400000, "Q1", 2025),
        ("2025-04-01", "2025-06-30", 71900000, "Q2", 2025),
    ]
    return payload(
        1908259,
        "Example Newco Inc.",
        {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": tagged(
                    {
                        "USD": [
                            fact(
                                start=start,
                                end=end,
                                val=val,
                                fy=fy,
                                fp=fp,
                                form="10-Q",
                                filed=f"{fy}-11-05",
                            )
                            for start, end, val, fp, fy in quarters
                        ]
                    }
                )
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": tagged(
                    {"shares": [fact(val=88000000, end="2025-08-01")]}
                )
            },
        },
    )


def _restater() -> dict[str, Any]:
    """One period, **four ``filed`` dates, four different values.** What ``--as-of`` has to cut.

    A run with ``--as-of 2021-06-30`` must see 812,000,000 — the value that was on file then — and
    not the 2023 restatement. A test that only checks "the newest wins" passes on a payload with one
    filing and proves nothing.
    """
    filings = [
        ("2021-02-24", 812000000, "0000000001-21-000001"),
        ("2021-08-05", 806500000, "0000000001-21-000002"),
        ("2022-02-23", 791200000, "0000000001-22-000001"),
        ("2023-02-22", 774900000, "0000000001-23-000001"),
    ]
    return payload(
        1000045,
        "Example Restated Corp",
        {
            "us-gaap": {
                "Revenues": tagged(
                    {
                        "USD": [
                            fact(
                                start="2020-01-01",
                                end="2020-12-31",
                                val=val,
                                accn=accn,
                                fy=int(filed[:4]),
                                filed=filed,
                            )
                            for filed, val, accn in filings
                        ]
                    }
                )
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": tagged(
                    {"shares": [fact(val=210000000, end="2023-02-01")]}
                )
            },
        },
    )


def _noq4() -> dict[str, Any]:
    """Discrete Q4 never tagged — and **inconsistently, within one issuer, across years.**

    §4.2(c) says that is the real behaviour: it varies by issuer *and* by year within the same issuer.
    So FY2022 here has Q1-Q3 and an annual figure with no Q4, while FY2023 has all four. A fixture
    where Q4 is uniformly absent would let a derivation rule that always subtracts pass, and that rule
    double-counts on FY2023.
    """
    rows: list[dict[str, Any]] = []
    for year, quarters in ((2022, ("Q1", "Q2", "Q3")), (2023, ("Q1", "Q2", "Q3", "Q4"))):
        bounds = {
            "Q1": (f"{year}-01-01", f"{year}-03-31", 240000000),
            "Q2": (f"{year}-04-01", f"{year}-06-30", 258000000),
            "Q3": (f"{year}-07-01", f"{year}-09-30", 266000000),
            "Q4": (f"{year}-10-01", f"{year}-12-31", 301000000),
        }
        for period in quarters:
            start, end, val = bounds[period]
            rows.append(
                fact(
                    start=start,
                    end=end,
                    val=val,
                    fy=year,
                    fp=period,
                    form="10-Q",
                    filed=f"{year}-11-04",
                )
            )
        rows.append(
            fact(
                start=f"{year}-01-01",
                end=f"{year}-12-31",
                val=sum(bounds[p][2] for p in ("Q1", "Q2", "Q3", "Q4")),
                fy=year,
                fp="FY",
                filed=f"{year + 1}-02-20",
            )
        )
    return payload(
        1000046,
        "Example NoQ4 Industries",
        {
            "us-gaap": {"Revenues": tagged({"USD": rows})},
            "dei": {
                "EntityCommonStockSharesOutstanding": tagged(
                    {"shares": [fact(val=64000000, end="2024-02-01")]}
                )
            },
        },
    )


# ---------------------------------------------------------------------------
# tickers
# ---------------------------------------------------------------------------
def _tickers() -> dict[str, Any]:
    """The exchange file, trimmed.

    Three properties the parser must survive, all present here:

    - The exchange value is ``"Nasdaq"``, **mixed case**. A ``== "NASDAQ"`` comparison matches
      nothing and the symptom is exit 2 for every ticker in the universe.
    - One CIK, several rows: GOOGL and GOOG share CIK 1652044, which is what "sum all classes" needs.
    - A **non-NASDAQ** row (JPM, NYSE) so the exit-2 violation test has a real target. Without it, an
      implementation that resolves the CIK and forgets the exchange check passes every test.
    """
    return {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [320193, "Apple Inc.", "AAPL", "Nasdaq"],
            [1045810, "NVIDIA CORP", "NVDA", "Nasdaq"],
            [1652044, "Alphabet Inc.", "GOOGL", "Nasdaq"],
            [1652044, "Alphabet Inc.", "GOOG", "Nasdaq"],
            [2093536, "Arxis, Inc.", "ARXS", "Nasdaq"],
            [19617, "JPMORGAN CHASE & CO", "JPM", "NYSE"],
            [36104, "Example Bancorp Inc.", "EXBK", "Nasdaq"],
            [1063761, "Example Properties Trust", "EXPT", "Nasdaq"],
            [1908259, "Example Newco Inc.", "EXNC", "Nasdaq"],
            [1000045, "Example Restated Corp", "EXRS", "Nasdaq"],
            [1000046, "Example NoQ4 Industries", "EXNQ", "Nasdaq"],
        ],
    }


# ---------------------------------------------------------------------------
# submissions
# ---------------------------------------------------------------------------
def _columns(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """Transpose row dicts into ``filings.recent``'s parallel arrays."""
    names = [
        "accessionNumber",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "act",
        "form",
        "fileNumber",
        "filmNumber",
        "items",
        "core_type",
        "size",
        "isXBRL",
        "isInlineXBRL",
        "isXBRLNumeric",
        "primaryDocument",
        "primaryDocDescription",
    ]
    return {name: [row.get(name, "") for row in rows] for name in names}


def _arxs_submissions() -> dict[str, Any]:
    """The complete small-filer payload. **Every awkward value observed live lives here.**

    ``"cik":"0002093536"`` as a padded string; ``"sic":"3728"``; ``reportDate`` and ``act`` as
    ``""``; ``isXBRLNumeric`` carrying real ``null``s mixed with ``0``/``1``; an ``items`` value of
    ``",,"``; ``primaryDocument`` of ``"xslF345X06/ownership.xml"``; a PDF primary document; and
    ``"files":[]``.

    Small enough to commit whole and untrimmed, which is what makes it the fixture that catches a
    normalization bug a reduction script might have smoothed over.
    """
    rows = [
        {
            "accessionNumber": "0001193125-26-243043",
            "filingDate": "2026-05-28",
            "reportDate": "2026-03-31",
            "acceptanceDateTime": "2026-05-28T16:31:02.000Z",
            "act": "34",
            "form": "10-Q",
            "fileNumber": "001-42311",
            "filmNumber": "26901234",
            "items": "",
            "core_type": "10-Q",
            "size": 3412887,
            "isXBRL": 1,
            "isInlineXBRL": 1,
            "isXBRLNumeric": 1,
            "primaryDocument": "arxs-20260331.htm",
            "primaryDocDescription": "10-Q",
        },
        {
            "accessionNumber": "0001193125-26-146309",
            "filingDate": "2026-04-08",
            "reportDate": "",
            "acceptanceDateTime": "2026-04-08T06:02:44.000Z",
            "act": "33",
            "form": "S-1/A",
            "fileNumber": "333-284119",
            "filmNumber": "26788001",
            "items": "",
            "core_type": "S-1/A",
            "size": 8812403,
            "isXBRL": 1,
            "isInlineXBRL": 1,
            "isXBRLNumeric": 0,
            "primaryDocument": "d901234ds1a.htm",
            "primaryDocDescription": "",
        },
        {
            # The degenerate items value, on an EFFECT filing. A naive split(",") yields ["","",""].
            "accessionNumber": "9999999997-26-004411",
            "filingDate": "2026-04-14",
            "reportDate": "",
            "acceptanceDateTime": "2026-04-14T00:03:11.000Z",
            "act": "",
            "form": "EFFECT",
            "fileNumber": "333-284119",
            "filmNumber": "",
            "items": ",,",
            "core_type": "EFFECT",
            "size": 1041,
            "isXBRL": 0,
            "isInlineXBRL": 0,
            # A genuine JSON null, mixed with 0/1 in this same array.
            "isXBRLNumeric": None,
            "primaryDocument": "",
            "primaryDocDescription": "",
        },
        {
            # primaryDocument points at the XSL *viewer*, not the machine-readable XML.
            "accessionNumber": "0001140361-26-025622",
            "filingDate": "2026-04-20",
            "reportDate": "",
            "acceptanceDateTime": "2026-04-20T20:14:55.000Z",
            "act": "",
            "form": "3",
            "fileNumber": "001-42311",
            "filmNumber": "",
            "items": "",
            "core_type": "3",
            "size": 8814,
            "isXBRL": 0,
            "isInlineXBRL": 0,
            "isXBRLNumeric": None,
            "primaryDocument": "xslF345X06/ownership.xml",
            "primaryDocDescription": "",
        },
        {
            "accessionNumber": "0001140361-26-025999",
            "filingDate": "2026-05-04",
            "reportDate": "2026-05-01",
            "acceptanceDateTime": "2026-05-04T18:02:10.000Z",
            "act": "",
            "form": "4",
            "fileNumber": "001-42311",
            "filmNumber": "",
            "items": "",
            "core_type": "4",
            "size": 9210,
            "isXBRL": 0,
            "isInlineXBRL": 0,
            "isXBRLNumeric": None,
            "primaryDocument": "xslF345X06/ownership.xml",
            "primaryDocDescription": "",
        },
        {
            # An 8-K with real item codes, and a PDF primary document elsewhere in the payload —
            # so nothing may assume an .htm suffix.
            "accessionNumber": "0001193125-26-201188",
            "filingDate": "2026-05-12",
            "reportDate": "2026-05-11",
            "acceptanceDateTime": "2026-05-12T12:00:03.000Z",
            "act": "34",
            "form": "8-K",
            "fileNumber": "001-42311",
            "filmNumber": "26884411",
            "items": "1.01,8.01,9.01",
            "core_type": "8-K",
            "size": 121004,
            "isXBRL": 1,
            "isInlineXBRL": 1,
            "isXBRLNumeric": 0,
            "primaryDocument": "d998877d8k.htm",
            "primaryDocDescription": "8-K",
        },
        {
            "accessionNumber": "0001193125-26-118844",
            "filingDate": "2026-03-30",
            "reportDate": "",
            "acceptanceDateTime": "2026-03-30T09:11:41.000Z",
            "act": "34",
            "form": "8-A12B",
            "fileNumber": "001-42311",
            "filmNumber": "26701221",
            "items": "",
            "core_type": "8-A12B",
            "size": 44120,
            "isXBRL": 0,
            "isInlineXBRL": 0,
            "isXBRLNumeric": None,
            "primaryDocument": "ARXS_8A_Cert_2093536.pdf",
            "primaryDocDescription": "",
        },
    ]
    return {
        "cik": "0002093536",
        "entityType": "operating",
        "sic": "3728",
        "sicDescription": "Aircraft Parts & Auxiliary Equipment, NEC",
        "ownerOrg": "06 Technology",
        "insiderTransactionForOwnerExists": 0,
        "insiderTransactionForIssuerExists": 1,
        "name": "Arxis, Inc.",
        "tickers": ["ARXS"],
        "exchanges": ["Nasdaq"],
        "ein": "931234567",
        "lei": "",
        "description": "",
        "website": "",
        "investorWebsite": "",
        "category": "Non-accelerated filer",
        "fiscalYearEnd": "1231",
        "stateOfIncorporation": "DE",
        "stateOfIncorporationDescription": "DE",
        "addresses": {"business": {"street1": "1 Example Way", "city": "Austin", "stateOrCountry": "TX"}},
        "phone": "512-555-0100",
        "flags": "",
        "formerNames": [],
        "filings": {"recent": _columns(rows), "files": []},
    }


def _aapl_submissions() -> dict[str, Any]:
    """A main payload with a **populated ``files[]``** — the pagination case.

    Confirmed real: Apple's overflow page 001 holds 2015 filings, so ``filings.recent`` does not
    reach 2015 and a 10y lookback reads an incomplete history without pagination.

    **The field names inside the entry are the one unconfirmed shape in M1a.** They are SEC's prose
    plus the page-naming convention, not an observation — see ``FILES_ENTRY_FIELDS``. If the real
    payload disagrees, ``parse_files`` raises and names the real keys.
    """
    rows = [
        {
            "accessionNumber": "0000320193-26-000013",
            "filingDate": "2026-01-30",
            "reportDate": "2025-12-27",
            "acceptanceDateTime": "2026-01-30T18:01:14.000Z",
            "act": "34",
            "form": "10-Q",
            "fileNumber": "001-36743",
            "filmNumber": "26551234",
            "items": "",
            "core_type": "10-Q",
            "size": 6612884,
            "isXBRL": 1,
            "isInlineXBRL": 1,
            "isXBRLNumeric": 1,
            "primaryDocument": "aapl-20251227.htm",
            "primaryDocDescription": "10-Q",
        },
        {
            "accessionNumber": "0000320193-19-000119",
            "filingDate": "2019-10-31",
            "reportDate": "2019-09-28",
            "acceptanceDateTime": "2019-10-31T18:12:36.000Z",
            "act": "34",
            "form": "10-K",
            "fileNumber": "001-36743",
            "filmNumber": "191181234",
            "items": "",
            "core_type": "10-K",
            "size": 12881004,
            "isXBRL": 1,
            "isInlineXBRL": 1,
            "isXBRLNumeric": 1,
            "primaryDocument": "a10-k20199282019.htm",
            "primaryDocDescription": "10-K",
        },
        {
            # A filer-agent accession in the same history as the self-filed ones above.
            "accessionNumber": "0001140361-26-025622",
            "filingDate": "2026-02-04",
            "reportDate": "2026-02-02",
            "acceptanceDateTime": "2026-02-04T21:30:02.000Z",
            "act": "",
            "form": "4",
            "fileNumber": "001-36743",
            "filmNumber": "",
            "items": "",
            "core_type": "4",
            "size": 9114,
            "isXBRL": 0,
            "isInlineXBRL": 0,
            "isXBRLNumeric": None,
            "primaryDocument": "xslF345X06/ownership.xml",
            "primaryDocDescription": "",
        },
        {
            # Item 4.02 — non-reliance. The highest-severity flag in the system (M4.5), detected
            # here as a string only. Present so the events extractor has a real target.
            "accessionNumber": "0000320193-25-000079",
            "filingDate": "2025-08-01",
            "reportDate": "2025-07-30",
            "acceptanceDateTime": "2025-08-01T20:05:00.000Z",
            "act": "34",
            "form": "8-K",
            "fileNumber": "001-36743",
            "filmNumber": "251100221",
            "items": "4.02,9.01",
            "core_type": "8-K",
            "size": 88120,
            "isXBRL": 1,
            "isInlineXBRL": 1,
            "isXBRLNumeric": 0,
            "primaryDocument": "aapl-20250730.htm",
            "primaryDocDescription": "8-K",
        },
        {
            "accessionNumber": "0000320193-25-000061",
            "filingDate": "2025-05-01",
            "reportDate": "2025-05-01",
            "acceptanceDateTime": "2025-05-01T20:01:00.000Z",
            "act": "34",
            "form": "8-K",
            "fileNumber": "001-36743",
            "filmNumber": "251090114",
            "items": "2.02,9.01",
            "core_type": "8-K",
            "size": 64110,
            "isXBRL": 1,
            "isInlineXBRL": 1,
            "isXBRLNumeric": 0,
            "primaryDocument": "aapl-20250501.htm",
            "primaryDocDescription": "8-K",
        },
    ]
    return {
        "cik": "0000320193",
        "entityType": "operating",
        "sic": "3571",
        "sicDescription": "Electronic Computers",
        "ownerOrg": "06 Technology",
        "insiderTransactionForOwnerExists": 0,
        "insiderTransactionForIssuerExists": 1,
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "exchanges": ["Nasdaq"],
        "ein": "942404110",
        "lei": "",
        "description": "",
        "website": "",
        "investorWebsite": "",
        "category": "Large accelerated filer",
        "fiscalYearEnd": "0928",
        "stateOfIncorporation": "CA",
        "stateOfIncorporationDescription": "CA",
        "addresses": {"business": {"street1": "One Apple Park Way", "city": "Cupertino", "stateOrCountry": "CA"}},
        "phone": "(408) 996-1010",
        "flags": "",
        "formerNames": [{"name": "APPLE COMPUTER INC", "from": "1994-01-26T00:00:00.000Z", "to": "2007-01-04T00:00:00.000Z"}],
        "filings": {
            "recent": _columns(rows),
            "files": [
                {
                    "name": "CIK0000320193-submissions-001.json",
                    "filingCount": 1000,
                    "filingFrom": "2015-08-05",
                    "filingTo": "2019-10-30",
                }
            ],
        },
    }


def _aapl_page() -> dict[str, Any]:
    """An overflow page: **flat** columnar, no ``filings`` wrapper, no company metadata.

    The observed page begins ``{"accessionNumber":[...]``. One function cannot parse both shapes,
    which is why there are two — and why one test asserts each parser *rejects* the other's payload.
    """
    rows = [
        {
            "accessionNumber": "0001193125-15-177428",
            "filingDate": "2015-05-08",
            "reportDate": "2015-03-28",
            "acceptanceDateTime": "2015-05-08T16:31:02.000Z",
            "act": "34",
            "form": "10-Q",
            "fileNumber": "000-10030",
            "filmNumber": "15845112",
            "items": "",
            "core_type": "10-Q",
            "size": 4412887,
            "isXBRL": 1,
            "isInlineXBRL": 0,
            "isXBRLNumeric": 1,
            "primaryDocument": "a10-q3272015.htm",
            "primaryDocDescription": "10-Q",
        },
        {
            "accessionNumber": "0001193125-15-175208",
            "filingDate": "2015-05-06",
            "reportDate": "",
            "acceptanceDateTime": "2015-05-06T18:02:44.000Z",
            "act": "",
            "form": "4",
            "fileNumber": "000-10030",
            "filmNumber": "",
            "items": "",
            "core_type": "4",
            "size": 8812,
            "isXBRL": 0,
            "isInlineXBRL": 0,
            "isXBRLNumeric": None,
            "primaryDocument": "xslF345X06/ownership.xml",
            "primaryDocDescription": "",
        },
        {
            "accessionNumber": "0001193125-15-173308",
            "filingDate": "2015-08-05",
            "reportDate": "2015-08-04",
            "acceptanceDateTime": "2015-08-05T16:05:11.000Z",
            "act": "34",
            "form": "8-K",
            "fileNumber": "000-10030",
            "filmNumber": "151101884",
            "items": "5.02,9.01",
            "core_type": "8-K",
            "size": 41200,
            "isXBRL": 0,
            "isInlineXBRL": 0,
            "isXBRLNumeric": None,
            "primaryDocument": "d8k.htm",
            "primaryDocDescription": "8-K",
        },
    ]
    return _columns(rows)


# ---------------------------------------------------------------------------
# malformed
# ---------------------------------------------------------------------------
def _short_column() -> dict[str, Any]:
    """``filings.recent`` with one column shorter than the rest.

    Zipping these truncates to the shortest and silently loses the tail of the filing history —
    which looks exactly like a company that stopped filing. The parser raises instead.
    """
    payload_ = _arxs_submissions()
    recent = payload_["filings"]["recent"]
    recent["primaryDocDescription"] = recent["primaryDocDescription"][:-2]
    return payload_


def _bad_accession() -> dict[str, Any]:
    """An accession that is not eighteen digits. A silently accepted one becomes a 404 that looks
    like missing data — ROADMAP M1's named risk."""
    payload_ = _arxs_submissions()
    payload_["filings"]["recent"]["accessionNumber"][0] = "0001193125-26-24304"
    return payload_


UNDECLARED_403 = """\
Your Request Originates from an Undeclared Automated Tool

To allow for equitable access to all users, SEC reserves the right to limit requests
originating from undeclared automated tools. Your request has been identified as part of a
network of automated tools outside of the acceptable policy and will be managed until action
is taken to declare your traffic.

Please declare your traffic by updating your user agent to include company specific
information.

For best practices on efficiently downloading information from SEC.gov, including the latest
EDGAR filings, visit https://www.sec.gov/os/webmaster-faq#developers.
"""
"""SEC's undeclared-automated-tool 403 body.

The classifier matches ``undeclared\\s+automated\\s+tool`` against this, case-insensitively, on
bytes. Its violation test asserts **exactly one request was made** — not merely that the exception
was raised — because the guarantee is "never retried", which is a claim about the request count.
"""

THROTTLED_403 = """\
Request Rate Threshold Exceeded

Your request rate has exceeded the SEC's threshold. Please reduce your request rate and try
again in ten minutes.
"""
"""The *other* 403. Retryable, and the reason the classifier reads the body rather than the status."""


# ---------------------------------------------------------------------------
# prices
# ---------------------------------------------------------------------------
_CLOSES = [
    ("2026-07-24", "212.4400", "211.9100"),
    ("2026-07-27", "214.0100", "213.4700"),
    ("2026-07-28", "213.1200", "212.5900"),
    ("2026-07-29", "216.7700", "216.2300"),
    ("2026-07-30", "218.0500", "217.5000"),
    ("2026-07-31", "217.3100", "216.7600"),
]
"""Six bars. Closes and adjusted closes **differ**, which the Stooq violation test needs: if they
were equal, an implementation that aliased `close` into `adj_close` would be indistinguishable from
one that reported `None`."""


def _tiingo_prices() -> list[dict[str, Any]]:
    return [
        {
            "date": f"{day}T00:00:00.000Z",
            "close": float(close),
            "high": float(close) + 1.1,
            "low": float(close) - 1.4,
            "open": float(close) - 0.6,
            "volume": 41_000_000 + index * 100_000,
            "adjClose": float(adj),
            "adjVolume": 41_000_000 + index * 100_000,
            "divCash": 0.0,
            "splitFactor": 1.0,
        }
        for index, (day, close, adj) in enumerate(_CLOSES)
    ]


def _yfinance_prices() -> list[dict[str, Any]]:
    """Records the yfinance contract test turns into a frame-like object.

    yfinance owns its own HTTP, so there is no cassette to record — the seam is
    ``yfinance.download``, and the test monkeypatches it. This is the data it returns.
    """
    return [
        {
            "Date": day,
            "Open": float(close) - 0.6,
            "High": float(close) + 1.1,
            "Low": float(close) - 1.4,
            "Close": float(close),
            "Adj Close": float(adj),
            "Volume": 41_000_000 + index * 100_000,
        }
        for index, (day, close, adj) in enumerate(_CLOSES)
    ]


def _stooq_csv() -> str:
    """Stooq's CSV. **No ``Adj Close`` column at all** — which is the point of the fixture."""
    lines = ["Date,Open,High,Low,Close,Volume"]
    for index, (day, close, _adj) in enumerate(_CLOSES):
        low = f"{float(close) - 1.4:.4f}"
        high = f"{float(close) + 1.1:.4f}"
        open_ = f"{float(close) - 0.6:.4f}"
        lines.append(f"{day},{open_},{high},{low},{close},{41_000_000 + index * 100_000}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# typing fixtures
# ---------------------------------------------------------------------------
TYPING_FIXTURES: dict[str, str] = {
    "cover_shares_as_diluted.py": '''\
"""DESIGN.md §5.4: the cover-page share count cannot be a per-share denominator.

Both types are `Decimal` at runtime — which is the whole reason `NewType` was chosen — so this
guarantee cannot be tested at runtime and its violation test is this file. basedpyright must report
an error on the marked line. `test_typing.py` asserts the error count and the line number, never the
message text, because wording is what changes across releases.
"""

from decimal import Decimal

from investo.domain.models import CoverShares, DilutedShares


def per_share(total: Decimal, shares: DilutedShares) -> Decimal:
    return total / shares


cover = CoverShares(Decimal("4443236000"))
_ = per_share(Decimal("100000000000"), cover)  # ERROR: CoverShares is not a DilutedShares
''',
    "framerow_as_rawfact.py": '''\
"""DESIGN.md §4.2: a frames value cannot enter the subject company's series.

Frames is not point-in-time stable — a CY2025Q1 frame can resolve to a 2026 filing — so `FrameRow` is
a distinct type from `RawFact`, and that type distinction *is* the enforcement. This file attempts
the mix; basedpyright must reject it.
"""

from investo.domain.models import RawFact
from investo.ingest.edgar.frames import FrameRow


def append(series: list[RawFact], row: FrameRow) -> None:
    series.append(row)  # ERROR: FrameRow is not a RawFact
''',
    "accession_cik_attribute.py": '''\
"""No company CIK is derived from an accession.

The leading ten digits identify the *submitter*, which for most companies is a filer agent. Apple's
own history contains both patterns, so the wrong rule produces correct answers on some filings and a
nonexistent CIK on others. `Accession` therefore exposes no `cik` at all, and the absence is the
enforcement — this file attempts the access and basedpyright must reject it.
"""

from investo.domain.provenance import Accession

accession = Accession.parse("0001140361-26-025622")
_ = accession.cik  # ERROR: Accession has no `cik` attribute, deliberately
''',
}


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"  {path.relative_to(ROOT.parent)}")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text, encoding="utf-8")
    print(f"  {path.relative_to(ROOT.parent)}")


def main() -> int:
    edgar = ROOT / "edgar"
    print("companyfacts:")
    _write_json(edgar / "companyfacts" / "ARXS.json", _arxs_companyfacts())
    for name, builder in (
        ("AAPL", _aapl),
        ("BANK", _bank),
        ("REIT", _reit),
        ("IPO", _ipo),
        ("RESTATER", _restater),
        ("NOQ4", _noq4),
    ):
        _write_json(edgar / "companyfacts" / f"{name}.trimmed.json", builder())

    print("tickers:")
    _write_json(edgar / "company_tickers_exchange.trimmed.json", _tickers())

    print("submissions:")
    _write_json(edgar / "submissions" / "ARXS.json", _arxs_submissions())
    _write_json(edgar / "submissions" / "AAPL.json", _aapl_submissions())
    _write_json(edgar / "submissions" / "AAPL-submissions-001.json", _aapl_page())

    print("malformed:")
    _write_json(edgar / "malformed" / "short_column.json", _short_column())
    _write_json(edgar / "malformed" / "bad_accession.json", _bad_accession())
    _write_text(edgar / "malformed" / "undeclared_403.txt", UNDECLARED_403)
    _write_text(edgar / "malformed" / "throttled_403.txt", THROTTLED_403)

    print("prices:")
    _write_json(ROOT / "prices" / "tiingo" / "AAPL.json", _tiingo_prices())
    _write_json(ROOT / "prices" / "yfinance" / "AAPL.json", _yfinance_prices())
    _write_text(ROOT / "prices" / "stooq" / "AAPL.csv", _stooq_csv())

    print("typing:")
    for name, body in TYPING_FIXTURES.items():
        _write_text(ROOT / "typing" / name, body)

    return 0


if __name__ == "__main__":
    sys.exit(main())

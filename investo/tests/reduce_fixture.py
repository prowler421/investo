#!/usr/bin/env python3
"""Reduce a real ``companyfacts`` payload to a committable fixture. **Checked in on purpose.**

Apple's ``companyfacts`` is roughly 40 MB. Committing it is unreasonable; hand-editing it produces a
fixture nobody can regenerate or justify. So every ``*.trimmed.json`` in ``tests/fixtures`` is the
output of this script, run against a payload fetched to a gitignored path.

The point is not disk space. It is that a reviewer can ask *"why does this fixture contain these
facts"* and get an answer, and that regenerating fixtures after a DESIGN change is a command rather
than an afternoon. DESIGN.md §11 calls for real ``companyfacts`` JSON for ~15 companies with
known-hard cases — real, reduced, reproducibly.

**Structure is preserved exactly.** The nesting, the key names, the ``null``s, the missing ``start``
keys and the unit spellings all survive: this filters facts, it does not rewrite them. A reduction
that tidied the payload would smooth over precisely the quirks the fixtures exist to carry.

Usage::

    # 1. fetch a real payload (needs INVESTO_SEC_USER_AGENT)
    uv run python -c "
    import httpx, os, pathlib
    ua = os.environ['INVESTO_SEC_USER_AGENT']
    body = httpx.get('https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json',
                     headers={'User-Agent': ua, 'Accept-Encoding': 'gzip, deflate'}).content
    pathlib.Path('tests/fixtures/_raw/AAPL.companyfacts.json').write_bytes(body)"

    # 2. reduce it
    uv run python tests/reduce_fixture.py \\
        tests/fixtures/_raw/AAPL.companyfacts.json \\
        tests/fixtures/edgar/companyfacts/AAPL.trimmed.json \\
        --from 2014-01-01

``tests/fixtures/_raw/`` is gitignored: it holds the full payloads, and one of them is what the
``network``-marked parse-time/peak-memory test measures against. A claim about a 40 MB payload tested
only against a 40 KB one is not tested.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

# The tags M1 and M2 name. Kept here rather than imported from `normalize/tags.py` — which does not
# exist until M2 — and deliberately *not* imported from anywhere under `ingest/`, since the layering
# test forbids a us-gaap literal there and this script is a test utility rather than library code.
#
# When M2 lands, this list should be replaced by an import from `normalize.tags`, so a chain added
# there cannot silently be absent from every fixture.
TIER_1 = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "NetIncomeLoss",
    "GrossProfit",
    "OperatingIncomeLoss",
    "Assets",
    "Liabilities",
    "LiabilitiesAndStockholdersEquity",
    "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "LongTermDebtNoncurrent",
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "EarningsPerShareDiluted",
)
TIER_2 = (
    "AssetsCurrent",
    "LiabilitiesCurrent",
    "RetainedEarningsAccumulatedDeficit",
    "AccountsReceivableNetCurrent",
    "CostOfGoodsAndServicesSold",
    "CostOfRevenue",
    "SellingGeneralAndAdministrativeExpense",
    "DepreciationDepletionAndAmortization",
    "InterestExpense",
    "ShareBasedCompensation",
    "OperatingLeaseLiability",
    "ProceedsFromIssuanceOfCommonStock",
)
DEI_TAGS = ("EntityCommonStockSharesOutstanding",)
"""The cover-page share count market cap needs. One tag, and the layering test asserts the `dei`
allowance is exactly this long."""

WANTED = {"us-gaap": TIER_1 + TIER_2, "dei": DEI_TAGS, "srt": (), "ffd": ()}


def reduce_payload(payload: dict[str, Any], *, since: date | None) -> dict[str, Any]:
    """Keep the wanted tags, and every fact in them at or after ``since``.

    Taxonomies not in :data:`WANTED` are kept **whole** when they are small, because ``ffd`` was not
    anticipated by the design and dropping an unknown taxonomy is how the next surprise gets
    smoothed over before anyone sees it.
    """
    facts: dict[str, Any] = {}
    for taxonomy, tags in payload.get("facts", {}).items():
        wanted = WANTED.get(taxonomy)
        kept: dict[str, Any] = {}
        for tag, definition in tags.items():
            if wanted is not None and wanted and tag not in wanted:
                continue
            units = {
                unit: [row for row in rows if _keep(row, since)]
                for unit, rows in definition.get("units", {}).items()
            }
            units = {unit: rows for unit, rows in units.items() if rows}
            if units:
                kept[tag] = {**definition, "units": units}
        if kept:
            facts[taxonomy] = kept
    return {**payload, "facts": facts}


def _keep(row: dict[str, Any], since: date | None) -> bool:
    if since is None:
        return True
    end = row.get("end")
    if not isinstance(end, str):
        return False
    try:
        return date.fromisoformat(end) >= since
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    _ = parser.add_argument("source", type=Path, help="full companyfacts payload")
    _ = parser.add_argument("destination", type=Path, help="where to write the reduced fixture")
    _ = parser.add_argument(
        "--from",
        dest="since",
        type=date.fromisoformat,
        default=None,
        help="drop facts whose `end` is before this date (YYYY-MM-DD)",
    )
    args = parser.parse_args(argv)

    payload: dict[str, Any] = json.loads(Path(args.source).read_bytes())
    reduced = reduce_payload(payload, since=args.since)
    destination = Path(args.destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # `indent=2` and a trailing newline so a fixture diff is readable in review, which is the whole
    # argument for checking this script in.
    _ = destination.write_text(json.dumps(reduced, indent=2) + "\n", encoding="utf-8")

    kept = sum(
        len(rows)
        for tags in reduced["facts"].values()
        for definition in tags.values()
        for rows in definition["units"].values()
    )
    print(f"{destination}: {kept} facts across {len(reduced['facts'])} taxonomies")
    return 0


if __name__ == "__main__":
    sys.exit(main())

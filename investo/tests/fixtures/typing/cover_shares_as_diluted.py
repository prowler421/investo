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

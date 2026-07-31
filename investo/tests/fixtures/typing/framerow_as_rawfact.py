"""DESIGN.md §4.2: a frames value cannot enter the subject company's series.

Frames is not point-in-time stable — a CY2025Q1 frame can resolve to a 2026 filing — so `FrameRow` is
a distinct type from `RawFact`, and that type distinction *is* the enforcement. This file attempts
the mix; basedpyright must reject it.
"""

from investo.domain.models import RawFact
from investo.ingest.edgar.frames import FrameRow


def append(series: list[RawFact], row: FrameRow) -> None:
    series.append(row)  # ERROR: FrameRow is not a RawFact

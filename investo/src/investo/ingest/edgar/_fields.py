"""Boundary normalizers: SEC's endpoints disagree, and this is where that is reconciled.

``docs/m1/04-parsers.md`` §10.1. Every difference below was discovered by fetching a payload
rather than by reading documentation:

- ``cik`` is a zero-padded **string** from ``submissions`` and ``companyfacts``
  (``"0002093536"``) and a bare **int** from ``company_tickers_exchange.json`` (``320193``).
  Two of the three endpoints pad.
- ``sic`` is a string (``"3728"``) and can be the empty string for a filer without one, so
  ``int(payload["sic"])`` raises on a real input.
- Absence is spelled ``""`` on some fields and ``null`` on others — **sometimes within one
  document**. ``reportDate``, ``act``, ``fileNumber`` and ``primaryDocDescription`` carry ``""``;
  ``isXBRLNumeric`` carries genuine ``null`` mixed with ``0``/``1`` in the same array; ``fy`` and
  ``fp`` are ``null`` on registration-statement facts.

**One module, not one per parser.** Duplicating :func:`as_cik` into two parsers is how the two
come to disagree, and the disagreement is silent: one path builds ``CIK0000320193`` and the other
builds ``CIK320193``, and only the second 404s — as a company that looks delisted.

Not in ``domain/``, deliberately. These encode SEC's payload quirks, and ``domain/`` is meant to
be the layer that knows nothing about where data came from; putting them there would make the
domain types' docstrings describe an HTTP API.
"""

from __future__ import annotations

from datetime import date, datetime

__all__ = [
    "as_cik",
    "as_date",
    "as_datetime",
    "as_optional_int",
    "as_optional_str",
    "as_bool",
    "require",
]


def as_cik(value: object) -> int:
    """Normalize a CIK to an ``int``. ``"0002093536"`` -> ``2093536``; ``320193`` -> ``320193``.

    **A CIK is never optional**, so ``""`` and ``None`` raise rather than returning ``None``. This
    is the function two endpoints disagree about, and the failure it prevents is a 404 that looks
    like a delisted company.

    Raises:
        ValueError: on anything that is not a positive integer or a decimal string.
    """
    if isinstance(value, bool):
        # bool is an int subclass; a boolean CIK is a payload bug, not a CIK of 1.
        raise ValueError(f"CIK must be an integer or a decimal string, got {value!r}")
    if isinstance(value, int):
        cik = value
    elif isinstance(value, str) and value.strip().isdigit():
        cik = int(value.strip())
    else:
        raise ValueError(f"CIK must be an integer or a decimal string, got {value!r}")
    if cik <= 0:
        raise ValueError(f"CIK must be positive, got {cik}")
    return cik


def as_date(value: object) -> date | None:
    """``"2026-04-08"`` -> ``date``; ``""`` and ``None`` -> ``None``.

    Both spellings of absence, because both occur in one ``submissions`` document. A parser
    written against ``None`` alone reaches ``date.fromisoformat("")`` and raises ``ValueError`` on
    a real payload — the ``reportDate`` of a Form 3 row.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return date.fromisoformat(text)
    raise ValueError(f"expected an ISO date string, got {value!r}")


def as_datetime(value: object) -> datetime | None:
    """``acceptanceDateTime`` -> aware ``datetime``; ``""`` and ``None`` -> ``None``.

    SEC writes this one with a ``Z`` suffix, which ``datetime.fromisoformat`` accepts from Python
    3.11. A value that parses to a naive datetime is rejected rather than assumed to be UTC: this
    field is used to order filings, and guessing a timezone is how an ordering silently shifts by
    hours across a day boundary.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise ValueError(f"expected a timezone-aware timestamp, got {value!r}")
        return parsed
    raise ValueError(f"expected an ISO timestamp string, got {value!r}")


def as_optional_int(value: object) -> int | None:
    """``"3728"`` -> ``3728``; ``""`` and ``None`` -> ``None``. For ``sic``, ``fy``, ``size``."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"expected an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.lstrip("-").isdigit():
            return int(text)
    raise ValueError(f"expected an integer or a decimal string, got {value!r}")


def as_optional_str(value: object) -> str | None:
    """``""`` -> ``None``. Everything SEC writes as an absent string."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    raise ValueError(f"expected a string, got {value!r}")


def as_bool(value: object) -> bool | None:
    """``1``/``0`` -> ``bool``; ``None`` -> ``None``.

    ``isXBRLNumeric`` carries all three in one array, so the column is not uniformly typed and a
    strict ``bool(...)`` cast would turn ``null`` into ``False`` — a filing recorded as
    "definitely not numeric XBRL" when the truth is "not stated".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise ValueError(f"expected 0 or 1, got {value!r}")
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
    raise ValueError(f"expected a boolean, 0/1, or an empty string, got {value!r}")


def require(payload: object, key: str, *, where: str) -> object:
    """Read a required key, naming the payload in the error.

    Required keys are named explicitly and their absence raises. The full key set is **not**
    asserted as exhaustive — SEC adds fields, and ``core_type`` and ``isXBRLNumeric`` were both
    absent from an earlier draft of the design's own key list. So each parser reads what it needs
    and ignores the rest.

    Raises:
        ValueError: if ``payload`` is not a mapping, or ``key`` is absent.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"{where}: expected a JSON object, got {type(payload).__name__}")
    if key not in payload:
        raise ValueError(f"{where}: required key {key!r} is missing")
    return payload[key]

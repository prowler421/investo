"""The boundary normalizers, against the table in ``docs/m1/04-parsers.md`` §10.1.

Every row in that table came from fetching a payload rather than from reading documentation, and the
reason this module exists at all is that SEC's endpoints disagree with each other about how to spell
the same value. ``cik`` is a zero-padded string from ``submissions`` and a bare int from
``company_tickers_exchange.json``; ``sic`` is a string that can be empty; absence is ``""`` on some
fields and ``null`` on others, sometimes within one document.

The failure each test is written against is the same one: a value that converts *almost* correctly.
``int(payload["sic"])`` raises on a real filer, ``date.fromisoformat("")`` raises on a real Form 3
row, and ``bool(None)`` does not raise at all — it records "definitely not numeric XBRL" where the
truth is "not stated".
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from investo.ingest.edgar._fields import (
    as_bool,
    as_cik,
    as_date,
    as_datetime,
    as_optional_int,
    as_optional_str,
    require,
)


# ---------------------------------------------------------------------------
# as_cik — the function two endpoints disagree about
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0002093536", 2093536),
        (320193, 320193),
        ("320193", 320193),
        (" 320193 ", 320193),
        (1, 1),
        ("0000000001", 1),
        (1234567890, 1234567890),
    ],
    ids=[
        "padded-string-from-submissions",
        "bare-int-from-tickers-exchange",
        "unpadded-string",
        "surrounded-by-whitespace",
        "cik-1",
        "cik-1-padded",
        "ten-digit-cik",
    ],
)
def test_as_cik(value: object, expected: int) -> None:
    """§10.1: every spelling of a CIK arrives as the same ``int``.

    The failure this prevents is a 404 that looks like a delisted company: one code path builds
    ``CIK0000320193`` and the other ``CIK320193``, and only the second one fails. ``CIK 1`` and a
    ten-digit CIK are here because they are the two ends of the padding rule the URL builders apply.
    """
    assert as_cik(value) == expected


@pytest.mark.spec
def test_as_cik_agrees_across_both_endpoint_spellings() -> None:
    """The point of the table, stated as the property rather than as two rows.

    ``submissions`` pads and ``company_tickers_exchange.json`` does not. If those two ever resolved
    differently the same company would have two identities, and nothing downstream would notice —
    the second one would simply have no filings.
    """
    assert as_cik("0000320193") == as_cik(320193)


@pytest.mark.spec
@pytest.mark.parametrize(
    "value",
    ["", "   ", None, "abc", 0, -1, True, False, "0", "3.5", "1e6", "320,193", [320193], 3.5],
    ids=[
        "empty-string",
        "blank-string",
        "null",
        "letters",
        "zero",
        "negative",
        "true",
        "false",
        "zero-as-string",
        "decimal-string",
        "exponent-string",
        "thousands-separator",
        "list",
        "float",
    ],
)
def test_as_cik_raises_on_anything_that_is_not_a_cik(value: object) -> None:
    """**A CIK is never optional**, so absence raises rather than returning ``None``.

    Every other normalizer in this module treats ``""`` and ``null`` as absence. This one must not:
    a CIK is the identity of the company being analysed, and a ``None`` here would travel until it
    reached a URL builder and produce a request for ``CIK0000000None``.

    ``True`` and ``False`` are in the table because ``bool`` is an ``int`` subclass, so the obvious
    ``isinstance(value, int)`` accepts them and turns a payload bug into a CIK of 1.
    """
    with pytest.raises(ValueError, match="CIK"):
        _ = as_cik(value)


# ---------------------------------------------------------------------------
# as_date / as_datetime
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-04-08", date(2026, 4, 8)),
        (" 2026-04-08 ", date(2026, 4, 8)),
        ("", None),
        ("   ", None),
        (None, None),
    ],
    ids=["filing-date", "padded", "empty-string", "blank-string", "null"],
)
def test_as_date(value: object, expected: date | None) -> None:
    """Both spellings of absence, because both occur in one ``submissions`` document.

    A parser written against ``None`` alone reaches ``date.fromisoformat("")`` and raises on a real
    payload — the ``reportDate`` of a Form 3 row, which is empty for every one of them.
    """
    assert as_date(value) == expected


@pytest.mark.parametrize("value", ["2026-13-01", "08/04/2026", "not-a-date", 20260408, 1.0])
def test_as_date_raises_on_something_that_is_not_a_date(value: object) -> None:
    """A value that is present and unparseable is a parse failure, not an absence.

    Returning ``None`` for it would fold "SEC changed the date format" into "this filer has no
    report date", and the report would show thinner coverage rather than an error anybody acts on.
    """
    with pytest.raises(ValueError):
        _ = as_date(value)


@pytest.mark.spec
def test_as_datetime_accepts_secs_z_suffix() -> None:
    """``acceptanceDateTime`` is written with a ``Z``, which ``fromisoformat`` takes from 3.11.

    Asserted as the same instant as the ``+00:00`` spelling rather than against a constructed
    literal, because the property that matters is that the two spellings agree — a parser that
    stripped the ``Z`` and left the value naive would produce a timestamp four hours off in summer.
    """
    parsed = as_datetime("2026-05-28T16:31:02.000Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed == datetime.fromisoformat("2026-05-28T16:31:02+00:00")


@pytest.mark.parametrize("value", ["", "   ", None])
def test_as_datetime_absence(value: object) -> None:
    """Same two spellings of absence as :func:`as_date`, for the same reason."""
    assert as_datetime(value) is None


@pytest.mark.spec
def test_as_datetime_rejects_a_naive_timestamp() -> None:
    """A value that parses to a naive datetime is rejected rather than assumed to be UTC.

    This field orders filings, and guessing a timezone is how an ordering silently shifts by hours
    across a day boundary — which then reorders two filings made on the same day, which is exactly
    the case ``--as-of`` and the restatement logic depend on.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        _ = as_datetime("2026-05-28T16:31:02")


# ---------------------------------------------------------------------------
# as_optional_int / as_optional_str
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3728", 3728),
        (" 3728 ", 3728),
        ("", None),
        ("   ", None),
        (None, None),
        (3728, 3728),
        (0, 0),
        ("-5", -5),
    ],
    ids=[
        "sic-string",
        "padded",
        "empty-sic",
        "blank-sic",
        "null-fy",
        "already-int",
        "zero",
        "negative",
    ],
)
def test_as_optional_int(value: object, expected: int | None) -> None:
    """§10.1: ``sic`` is a string, and it is the empty string for a filer without one.

    So ``int(payload["sic"])`` raises on a real input. ``fy`` carries a genuine ``null`` on
    registration-statement facts, which is the other spelling. ``0`` is here because a falsy-but-
    present value must survive: ``value or None`` would turn it into an absence.
    """
    assert as_optional_int(value) == expected


@pytest.mark.parametrize("value", ["3.5", "abc", True, False, [1], 3.5])
def test_as_optional_int_raises_on_a_non_integer(value: object) -> None:
    """``bool`` again, and a decimal string.

    ``True`` would otherwise become ``1`` and a fiscal year of ``1`` would be printed as if it were
    filed. A decimal string is refused rather than truncated, because silently dropping a fraction
    from a value SEC declared as an integer field means the field is not what we think it is.
    """
    with pytest.raises(ValueError, match="integer"):
        _ = as_optional_int(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", None), ("   ", None), (None, None), ("34", "34"), (" 10-Q ", "10-Q")],
)
def test_as_optional_str(value: object, expected: str | None) -> None:
    """``""`` is how SEC writes an absent string, in ``act``, ``fileNumber`` and others.

    Stripped rather than passed through, because ``" "`` and ``""`` mean the same thing in these
    columns and a whitespace-only value would otherwise print as a blank in the appendix.
    """
    assert as_optional_str(value) == expected


def test_as_optional_str_raises_on_a_non_string() -> None:
    """A number arriving where a string is documented is an upstream change, not a value.

    Coercing it with ``str()`` would hide the change and put ``"3728"`` and ``3728`` into the same
    column depending on the day.
    """
    with pytest.raises(ValueError, match="string"):
        _ = as_optional_str(3728)


# ---------------------------------------------------------------------------
# as_bool
# ---------------------------------------------------------------------------
@pytest.mark.spec
@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, True), (0, False), (None, None), (True, True), (False, False), ("", None)],
    ids=["one", "zero", "null", "true", "false", "empty-string"],
)
def test_as_bool(value: object, expected: bool | None) -> None:
    """``isXBRLNumeric`` carries ``1``, ``0`` and a real ``null`` in one array.

    So the column is not uniformly typed, and a strict ``bool(...)`` cast turns ``null`` into
    ``False`` — a filing recorded as "definitely not numeric XBRL" when the truth is "not stated".
    Asserted with ``is``, because ``0 == False`` and ``None == None`` would both pass under a
    function that returned the wrong one of the three.
    """
    assert as_bool(value) is expected


@pytest.mark.parametrize("value", [2, -1, "maybe", [1], 1.0])
def test_as_bool_raises_on_a_third_value(value: object) -> None:
    """Two states and an absence. Anything else is a column that changed meaning.

    ``2`` is the interesting row: ``bool(2)`` is ``True``, so a cast would accept it and record a
    flag that was never set. Refusing it is what surfaces the change.
    """
    with pytest.raises(ValueError):
        _ = as_bool(value)


# ---------------------------------------------------------------------------
# require
# ---------------------------------------------------------------------------
def test_require_returns_the_value() -> None:
    """The happy path, so the error paths below are known to be the error paths."""
    assert require({"cik": "0000320193"}, "cik", where="submissions") == "0000320193"


@pytest.mark.spec
def test_require_names_the_missing_key() -> None:
    """The error has to name the key and the payload, because the caller is reading a traceback.

    A bare ``KeyError`` from inside a parser says which key and nothing about which of the four
    endpoints produced the payload — and the four disagree, so that is the first thing you need.
    """
    with pytest.raises(ValueError, match="filings") as caught:
        _ = require({"cik": 320193}, "filings", where="submissions payload")
    assert "filings" in str(caught.value)
    assert "submissions payload" in str(caught.value)


def test_require_distinguishes_absent_from_null() -> None:
    """A key that is present and ``null`` is present.

    ``payload.get(key)`` is the obvious implementation and it conflates the two, which matters here
    because ``fy``, ``fp`` and ``isXBRLNumeric`` all carry real ``null`` values — an implementation
    that raised on them would refuse a payload SEC serves every day.
    """
    assert require({"fy": None}, "fy", where="fact") is None


@pytest.mark.parametrize("payload", [[], "text", 42, None])
def test_require_rejects_a_payload_that_is_not_an_object(payload: object) -> None:
    """A JSON array where an object was documented is an upstream change, and it has to say so.

    Indexing a list with a string raises ``TypeError`` several frames deeper, and the message names
    neither the endpoint nor the key — so the report reads as a crash rather than as SEC having
    changed a payload shape.
    """
    with pytest.raises(ValueError, match="JSON object"):
        _ = require(payload, "cik", where="tickers")

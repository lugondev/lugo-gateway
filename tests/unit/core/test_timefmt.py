from datetime import datetime, timedelta, timezone

from app.core.timefmt import iso_utc


def test_naive_datetime_gets_explicit_utc_offset():
    """SQLite drops tzinfo, so utcnow()-produced values read back naive.
    A naive-looking ISO string is ambiguous to JS Date.parse() (interpreted
    as local time) -- the fix must always emit an explicit offset."""
    naive = datetime(2026, 7, 17, 12, 10, 37, 859208)
    out = iso_utc(naive)
    assert out is not None
    assert out.endswith("+00:00") or out.endswith("Z")
    # Round-tripping must yield an AWARE datetime equal to the naive value
    # reinterpreted as UTC (not shifted).
    parsed = datetime.fromisoformat(out)
    assert parsed.tzinfo is not None
    assert parsed == naive.replace(tzinfo=timezone.utc)


def test_aware_non_utc_datetime_preserves_the_instant():
    """A datetime that already carries a zone must not be blindly re-stamped
    as UTC -- that would silently shift the represented instant."""
    ict = timezone(timedelta(hours=7))  # Asia/Ho_Chi_Minh, UTC+7
    aware = datetime(2026, 7, 17, 19, 10, 37, tzinfo=ict)
    out = iso_utc(aware)
    assert out is not None
    parsed = datetime.fromisoformat(out)
    assert parsed.tzinfo is not None
    # Same instant in time, regardless of which offset is printed.
    assert parsed == aware
    assert parsed.astimezone(timezone.utc) == aware.astimezone(timezone.utc)


def test_none_passthrough():
    assert iso_utc(None) is None

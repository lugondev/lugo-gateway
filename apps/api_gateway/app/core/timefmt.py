from __future__ import annotations

from datetime import datetime, timezone


def iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 với múi giờ TƯỜNG MINH.

    SQLite không lưu tzinfo, nên DateTime(timezone=True) là vô tác dụng ở đó và
    ta đọc ra datetime naive. Chuỗi ISO không có múi giờ là dữ liệu NHẬP NHẰNG:
    JS Date.parse() coi nó là giờ ĐỊA PHƯƠNG và lệch đi đúng UTC offset của
    người xem (đã đo: "7 giờ trước" cho việc vừa mới xảy ra, ở UTC+7).

    Mọi giá trị naive trong DB đều do utcnow() ghi ra, nên coi naive = UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

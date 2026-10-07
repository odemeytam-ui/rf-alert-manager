from datetime import UTC, datetime


def to_epoch(dt: datetime) -> float:
    if dt.tzinfo is None:  # SQLite returns naive datetimes; we always store UTC
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def iso(value: datetime | float | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, UTC)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def now_utc() -> datetime:
    return datetime.now(UTC)

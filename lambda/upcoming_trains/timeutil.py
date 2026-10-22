"""Europe/Warsaw clock and GTFS times.

Alexa-hosted skills run Python 3.8, which has no zoneinfo module. Poland uses
EU daylight-saving rules, so the offset is computed directly:

- CET  (UTC+1) in winter
- CEST (UTC+2) from 01:00 UTC on the last Sunday of March
  until 01:00 UTC on the last Sunday of October

GTFS stop times are local clock times and may be 24:00 or later. 24:40 means
00:40 on the next calendar day, still belonging to the previous service date.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc


def last_sunday(year: int, month: int) -> date:
    """Return the last Sunday of a month."""
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() + 1) % 7)


def _dst_bounds_utc(year: int) -> tuple[datetime, datetime]:
    start_day = last_sunday(year, 3)
    end_day = last_sunday(year, 10)
    start = datetime(start_day.year, start_day.month, start_day.day, 1, 0, tzinfo=UTC)
    end = datetime(end_day.year, end_day.month, end_day.day, 1, 0, tzinfo=UTC)
    return start, end


def is_cest(utc_dt: datetime) -> bool:
    """True when this absolute instant is on Central European Summer Time."""
    if utc_dt.tzinfo is None:
        raise ValueError("utc_dt must be timezone-aware")
    instant = utc_dt.astimezone(UTC)
    start, end = _dst_bounds_utc(instant.year)
    return start <= instant < end


def warsaw_offset_hours_at(utc_dt: datetime) -> int:
    return 2 if is_cest(utc_dt) else 1


def utc_to_warsaw_wall(utc_dt: datetime) -> datetime:
    """Convert an absolute instant to a naive Europe/Warsaw wall clock."""
    instant = utc_dt.astimezone(UTC)
    local = instant + timedelta(hours=warsaw_offset_hours_at(instant))
    return local.replace(tzinfo=None)


def warsaw_date(utc_dt: datetime) -> date:
    return utc_to_warsaw_wall(utc_dt).date()


def offset_hours_for_wall(wall: datetime, fold: int = 0) -> int:
    """Offset in force for a naive Warsaw wall time.

    On the October overlap (02:00-03:00 on the last Sunday) fold=0 is the
    first occurrence (still CEST, UTC+2) and fold=1 is the second (CET, UTC+1).
    The March gap (02:00-03:00) does not exist; those clocks are treated as
    CEST so ordering still moves forward.
    """
    day = wall.date()
    march = last_sunday(wall.year, 3)
    october = last_sunday(wall.year, 10)
    if day < march or day > october:
        return 1
    if day == march:
        return 1 if wall.hour < 2 else 2
    if day == october:
        if wall.hour < 2:
            return 2
        if wall.hour < 3:
            return 2 if fold == 0 else 1
        return 1
    return 2


def warsaw_wall_to_utc(wall: datetime, fold: int = 0) -> datetime:
    """Convert a naive Warsaw wall time to an aware UTC instant."""
    offset = offset_hours_for_wall(wall, fold=fold)
    return (wall - timedelta(hours=offset)).replace(tzinfo=UTC)


def gtfs_clock_to_wall(service_day: date, seconds: int) -> datetime:
    """GTFS seconds after local midnight, which may be past 24:00."""
    if seconds < 0:
        raise ValueError("GTFS time is negative")
    return datetime(service_day.year, service_day.month, service_day.day) + timedelta(
        seconds=seconds
    )


def gtfs_clock_to_utc(service_day: date, seconds: int, fold: int = 0) -> datetime:
    return warsaw_wall_to_utc(gtfs_clock_to_wall(service_day, seconds), fold=fold)


def parse_yyyymmdd(text: str) -> date:
    if len(text) != 8 or not text.isdigit():
        raise ValueError("expected YYYYMMDD")
    return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))


def format_yyyymmdd(day: date) -> str:
    return f"{day.year:04d}{day.month:02d}{day.day:02d}"


def yyyymmdd_int(day: date) -> int:
    return day.year * 10000 + day.month * 100 + day.day


def speak_date(day: date) -> str:
    return f"{day.strftime('%B')} {day.day}, {day.year}"


def clock_hhmm(wall: datetime) -> str:
    return f"{wall.hour:02d}:{wall.minute:02d}"

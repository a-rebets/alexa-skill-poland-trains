"""Turn an Alexa time, and an optional date, into a Warsaw window start.

The spoken window is [T, T+60 minutes). A date pins the calendar day. With
no date, T is the next occurrence of that clock in Europe/Warsaw. A clock
that is already up to five minutes in the past still counts as today, so
asking again just after the hour does not jump to tomorrow.

Alexa resolves a bare "8" to 08:00 before the skill sees it. The next
occurrence of 08:00, asked at 23:00, is tomorrow morning. AMAZON.TIME also
sends MO, AF, EV, and NI for morning, afternoon, evening, and night.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from upcoming_trains.timeutil import utc_to_warsaw_wall, warsaw_wall_to_utc

# A request this close behind the clock still means the hour already underway.
PAST_GRACE = timedelta(minutes=5)

# AMAZON.TIME parts of day. Alexa documents these four values.
PARTS_OF_DAY = {
    "MO": (8, 0),
    "AF": (13, 0),
    "EV": (18, 0),
    "NI": (21, 0),
}


class WhenError(ValueError):
    """The time or date slot cannot be turned into one window."""


def resolve_when(now: datetime, time_text: str, date_text: str) -> datetime | None:
    """UTC start of the requested hour, or None when the user named neither.

    `now` is timezone-aware UTC. A given date is used as that Warsaw calendar
    day, including when the clock time is already past. PRESENT_REF is today.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    time_text = (time_text or "").strip()
    date_text = (date_text or "").strip()
    if not time_text and not date_text:
        return None

    wall_now = utc_to_warsaw_wall(now)
    if time_text:
        hour, minute = _parse_time(time_text)
    else:
        hour, minute = wall_now.hour, wall_now.minute

    if date_text:
        day = _parse_date(date_text, wall_now.date())
        wall = datetime(day.year, day.month, day.day, hour, minute)
        return warsaw_wall_to_utc(wall)

    day = wall_now.date()
    wall = datetime(day.year, day.month, day.day, hour, minute)
    target = warsaw_wall_to_utc(wall)
    if target >= now - PAST_GRACE:
        return target
    nxt = day + timedelta(days=1)
    wall = datetime(nxt.year, nxt.month, nxt.day, hour, minute)
    return warsaw_wall_to_utc(wall)


def _parse_time(text: str) -> tuple:
    key = text.strip().upper()
    if key in PARTS_OF_DAY:
        return PARTS_OF_DAY[key]
    pieces = key.split(":")
    if len(pieces) not in (2, 3):
        raise WhenError("I didn't catch that time. Try eight am, or morning.")
    try:
        hour = int(pieces[0])
        minute = int(pieces[1])
        if len(pieces) == 3:
            int(pieces[2])
    except ValueError:
        raise WhenError("I didn't catch that time. Try eight am, or morning.")
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise WhenError("I didn't catch that time. Try eight am, or morning.")
    return hour, minute


def _parse_date(text: str, today) -> datetime.date:
    if text == "PRESENT_REF":
        return today
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise WhenError(
            "I can look up one day at a time. Say tomorrow, or just say the time."
        )
    return day

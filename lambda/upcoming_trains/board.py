"""Choose the trains that are actually running in the next 60 minutes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from upcoming_trains.constants import DEPARTURE_GRACE_SECONDS, WINDOW_MINUTES
from upcoming_trains.realtime import TripUpdate
from upcoming_trains.schedule import Candidate
from upcoming_trains.timeutil import format_yyyymmdd

UTC = timezone.utc


@dataclass
class Train:
    headsign: str
    route: str
    agency: str
    number: str
    bus: bool
    direction: str
    direction_id: str
    scheduled: datetime
    predicted: datetime
    delay_seconds: int | None
    cancelled: bool
    live: bool


@dataclass
class StationBoard:
    station_id: str
    name: str | None
    known: bool
    trains: list[Train]
    extra: int


def build_board(
    stations: list[tuple[str, str | None]],
    candidates: list[Candidate],
    updates: dict[str, TripUpdate] | None,
    now: datetime,
    kind: str = "departure",
    window_start: datetime | None = None,
) -> list[StationBoard]:
    """`updates` is None when live data is missing or stale.

    A stale feed must not invent delays. Trains then stay on the schedule,
    which drops anything whose scheduled time is already in the past.
    `kind` is "departure" or "arrival". Speech, not this list, caps how many
    lines are read out.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    grouped: dict[str, list[Train]] = {stop_id: [] for stop_id, _name in stations}
    for candidate in candidates:
        train = _decide(candidate, updates, now, kind, window_start)
        if train is None or candidate.station_id not in grouped:
            continue
        grouped[candidate.station_id].append(train)
    boards = []
    for stop_id, name in stations:
        trains = grouped.get(stop_id, [])
        trains.sort(key=lambda item: (item.predicted, item.direction, item.route, item.number))
        boards.append(
            StationBoard(
                station_id=stop_id,
                name=name,
                known=name is not None,
                trains=trains,
                extra=0,
            )
        )
    return boards


def _decide(
    candidate: Candidate,
    updates: dict[str, TripUpdate] | None,
    now: datetime,
    kind: str,
    window_start: datetime | None = None,
) -> Train | None:
    # A named hour is [T, T+60). The live board keeps a few seconds of grace
    # and can still show a cancellation from the last five minutes.
    if window_start is None:
        window_begin = now - timedelta(seconds=DEPARTURE_GRACE_SECONDS)
        window_end = now + timedelta(minutes=WINDOW_MINUTES)
        cancel_begin = now - timedelta(minutes=5)
    else:
        window_begin = window_start
        window_end = window_start + timedelta(minutes=WINDOW_MINUTES)
        cancel_begin = window_start
    update = _matching_update(updates, candidate)
    predicted = candidate.scheduled
    delay: int | None = None
    cancelled = False
    live = False

    if update is not None and update.cancelled:
        cancelled = True
        live = True
    elif update is not None:
        stop = update.stops.get(candidate.stop_sequence)
        if stop is not None and stop.skipped:
            return None
        resolved = _resolve_stop(candidate.scheduled, stop, update.delay, kind)
        if resolved is not None:
            predicted, delay = resolved
            live = True

    # The hour is strictly less than 60 minutes. A train due at exactly
    # now + 60 minutes is the next hour.
    if cancelled:
        if not (cancel_begin <= candidate.scheduled < window_end):
            return None
    elif not (window_begin <= predicted < window_end):
        return None

    return Train(
        headsign=candidate.headsign,
        route=candidate.route,
        agency=candidate.agency,
        number=candidate.number,
        bus=candidate.bus,
        direction=candidate.direction,
        direction_id=getattr(candidate, "direction_id", "") or "",
        scheduled=candidate.scheduled,
        predicted=predicted,
        delay_seconds=delay,
        cancelled=cancelled,
        live=live,
    )


def _matching_update(updates, candidate: Candidate):
    if updates is None:
        return None
    update = updates.get(candidate.trip_id)
    if update is None:
        return None
    if update.start_date and update.start_date != format_yyyymmdd(candidate.service_day):
        return None
    return update


def _resolve_stop(
    scheduled: datetime,
    stop,
    trip_delay: int | None,
    kind: str,
) -> tuple[datetime, int] | None:
    if stop is not None:
        if kind == "arrival":
            resolved = _from_unix(scheduled, stop.arrival_unix)
            if resolved is not None:
                return resolved
            resolved = _from_delay(scheduled, stop.arrival_delay)
            if resolved is not None:
                return resolved
            resolved = _from_unix(scheduled, stop.departure_unix)
            if resolved is not None:
                return resolved
            resolved = _from_delay(scheduled, stop.departure_delay)
            if resolved is not None:
                return resolved
        else:
            resolved = _from_unix(scheduled, stop.departure_unix)
            if resolved is not None:
                return resolved
            resolved = _from_delay(scheduled, stop.departure_delay)
            if resolved is not None:
                return resolved
            resolved = _from_unix(scheduled, stop.arrival_unix)
            if resolved is not None:
                return resolved
            resolved = _from_delay(scheduled, stop.arrival_delay)
            if resolved is not None:
                return resolved
    if trip_delay is not None:
        return scheduled + timedelta(seconds=trip_delay), trip_delay
    return None


def _from_unix(scheduled: datetime, unix: int | None) -> tuple[datetime, int] | None:
    if not unix:
        return None
    predicted = datetime.fromtimestamp(unix, UTC)
    return predicted, int((predicted - scheduled).total_seconds())


def _from_delay(scheduled: datetime, delay: int | None) -> tuple[datetime, int] | None:
    if delay is None:
        return None
    return scheduled + timedelta(seconds=delay), delay

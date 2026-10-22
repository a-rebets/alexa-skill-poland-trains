"""Shared path for the skill and the local preview."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from upcoming_trains.board import build_board
from upcoming_trains.constants import (
    HANDLER_BUDGET_SECONDS,
    REALTIME_WAIT_SECONDS,
    WINDOW_MINUTES,
)
from upcoming_trains.realtime import fetch_feed, prefetch, updates_for_board
from upcoming_trains.schedule import Schedule
from upcoming_trains.speech import Spoken, _is_long_distance, render
from upcoming_trains.timing import add as add_ms
from upcoming_trains.token import LONG_DISTANCE, StationChoice

UTC = timezone.utc


def answer(
    schedule: Schedule,
    choices: list,
    now: datetime | None = None,
    fetch: bool = True,
    kind: str = "departure",
    window_start: datetime | None = None,
) -> tuple[Spoken, dict]:
    """`choices` is a list of StationChoice, or station ids for every direction.

    `window_start` is the UTC start of a named hour. Live delays are fetched
    only when that hour begins within 60 minutes of `now`.
    """
    if now is None:
        now = datetime.now(UTC)
    live = fetch and _delays_apply(now, window_start)
    choices = _canonical_choices(schedule, choices)
    stations = [(choice.station_id, schedule.station_name(choice.station_id)) for choice in choices]
    candidates = []
    for stop_id, name in stations:
        if name is None:
            continue
        candidates.extend(schedule.candidates(stop_id, now, kind, when=window_start))
    wanted = {item.trip_id for item in candidates}
    status = {
        "candidates": len(candidates),
        "fresh": False,
        "age_seconds": None,
        "error": None,
        "timestamp": None,
        "note": "",
        "fetched": False,
    }
    if live and wanted:
        # A body already in memory still has to be indexed once. That walk is
        # the cold realtime=cached time. Do not import boto3 here: the import
        # holds the GIL and used to stretch this step past half a second.
        deadline = _feed_deadline()
        prefetch()
        feed = fetch_feed(wanted, deadline=deadline)
        updates, note = updates_for_board(feed, now)
        status.update(
            {
                "fresh": feed.is_fresh(now),
                "age_seconds": feed.age_seconds(now),
                "error": feed.error,
                "timestamp": feed.timestamp,
                "note": note,
                "fetched": True,
                "matched_trips": len(updates or {}),
            }
        )
    else:
        updates, note = {}, ""
    started = time.perf_counter()
    boards = build_board(stations, candidates, updates, now, kind, window_start)
    _filter_directions(schedule, boards, choices)
    spoken = render(boards, note, now, kind, window_start)
    add_ms("board_ms", (time.perf_counter() - started) * 1000.0)
    return spoken, status


def _feed_deadline() -> float:
    """1.5 s after the handler started, covering every phase of the live fetch.

    Outside a request (the local preview, a test) the clock starts now.
    A stale handler clock from a previous invoke is ignored.
    """
    from upcoming_trains.voice import handler_started

    now = time.monotonic()
    origin = handler_started()
    if origin is None or now - origin > HANDLER_BUDGET_SECONDS:
        return now + REALTIME_WAIT_SECONDS
    return origin + REALTIME_WAIT_SECONDS


def _delays_apply(now: datetime, window_start: datetime | None) -> bool:
    """Live rows matter for the coming hour, and for a named hour that starts nearby."""
    if window_start is None:
        return True
    return abs((window_start - now).total_seconds()) <= WINDOW_MINUTES * 60


def _canonical_choices(schedule: Schedule, choices: list) -> list[StationChoice]:
    """Map platform stops onto their parent and drop the duplicate."""
    ordered = []
    seen = set()
    for item in choices:
        if isinstance(item, str):
            choice = StationChoice(item, None)
        elif isinstance(item, (list, tuple)) and not isinstance(item, StationChoice):
            directions = None if item[1] is None else tuple(item[1])
            choice = StationChoice(item[0], directions)
        else:
            choice = item
        canonical = schedule.canonical(choice.station_id)
        if canonical in seen:
            continue
        seen.add(canonical)
        ordered.append(StationChoice(canonical, choice.directions))
    return ordered


def _filter_directions(schedule: Schedule, boards: list, choices: list[StationChoice]) -> None:
    """Drop trains outside the saved directions.

    Unknown neighbour ids are ignored. If none of the saved ids still exist
    and long-distance was not chosen, the station keeps every train.
    """
    for board, choice in zip(boards, choices):
        if choice.directions is None or not board.trains:
            continue
        selected = set(choice.directions)
        known = schedule.local_neighbour_ids(board.station_id)
        known_selected = selected & known
        wants_long = LONG_DISTANCE in selected
        if not known_selected and not wants_long:
            continue
        kept = []
        for train in board.trains:
            if _is_long_distance(train.route, train.agency, train.bus):
                if wants_long:
                    kept.append(train)
                continue
            if train.direction_id in known_selected:
                kept.append(train)
        board.trains = kept

#!/usr/bin/env python3
"""Print what Alexa would say right now for a token or a list of station ids.

    python3 scripts/preview.py --station 33605 --station 33506
    python3 scripts/preview.py --token v1.WyIzMzYwNSIsIjMzNTA2Il0
    python3 scripts/preview.py --station 33605 --no-live
    python3 scripts/preview.py --station 33548 --arrivals --no-live
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambda"))

from upcoming_trains.answer import answer  # noqa: E402
from upcoming_trains.schedule import default_path, describe_window, load  # noqa: E402
from upcoming_trains.timeutil import utc_to_warsaw_wall  # noqa: E402
from upcoming_trains.token import StationChoice, decode_token  # noqa: E402

UTC = timezone.utc


def main() -> None:
    parser = argparse.ArgumentParser(description="Speak the next hour of trains")
    parser.add_argument("--station", action="append", default=[], metavar="STOP_ID")
    parser.add_argument("--token", help="Account-linking token from the picker")
    parser.add_argument("--arrivals", action="store_true", help="Read arrivals instead of departures")
    parser.add_argument("--no-live", action="store_true", help="Skip the realtime feed")
    parser.add_argument("--schedule", default=default_path())
    args = parser.parse_args()
    if args.token:
        choices = decode_token(args.token)
    elif args.station:
        choices = [StationChoice(stop_id, None) for stop_id in args.station]
    else:
        parser.error("pass --station STOP_ID (repeatable) or --token")

    started = time.perf_counter()
    schedule = load(args.schedule)
    now = datetime.now(UTC)
    kind = "arrival" if args.arrivals else "departure"
    spoken, status = answer(schedule, choices, now, fetch=not args.no_live, kind=kind)
    elapsed = time.perf_counter() - started
    wall = utc_to_warsaw_wall(now)
    print("Arrivals" if args.arrivals else "Departures")
    print(f"Warsaw time: {wall.strftime('%Y-%m-%d %H:%M')}")
    print(f"Timetable window: {describe_window(schedule)}")
    names = []
    for stop_id in (choice.station_id for choice in choices):
        name = schedule.station_name(stop_id)
        names.append(f"{name or 'unknown'} ({stop_id})")
    print("Stations: " + "; ".join(names))
    if args.no_live:
        print("Live feed: skipped")
    elif status.get("error"):
        print(f"Live feed: failed ({status['error']})")
    else:
        age = status.get("age_seconds")
        fresh = "fresh" if status.get("fresh") else "stale"
        print(f"Live feed: {fresh}, age {age}s, matched {status.get('matched_trips', 0)} trips")
    print(f"Lookup candidates: {status['candidates']}  elapsed {elapsed:.2f}s")
    print()
    print("Alexa would say:")
    print(spoken.plain)


if __name__ == "__main__":
    main()

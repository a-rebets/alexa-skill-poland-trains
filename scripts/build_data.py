#!/usr/bin/env python3
"""Build the compact timetable and the station list for the picker.

Downloads https://mkuran.pl/gtfs/polish_trains.zip unless --zip is given.
Keeps every parent station, and every boardable stop in the publisher's
feed_info window. Run this again before that window ends, then deploy.

    python3 scripts/build_data.py
    python3 scripts/build_data.py --zip /path/to/polish_trains.zip
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambda"))

from upcoming_trains.constants import GTFS_ZIP_URL  # noqa: E402
from upcoming_trains.schedule import (  # noqa: E402
    FLAG_ARRIVAL,
    FLAG_DEPARTURE,
    NO_STATION,
    dump,
    load,
)
from upcoming_trains.timeutil import (  # noqa: E402
    parse_yyyymmdd,
    warsaw_wall_to_utc,
)

csv.field_size_limit(10**8)
UTC = timezone.utc
_PERON_NAME = re.compile(r"^(?P<base>.+) \(Peron \d+\)$")
NEEDED = {
    "agency.txt",
    "calendar_dates.txt",
    "feed_info.txt",
    "routes.txt",
    "stops.txt",
    "stop_times.txt",
    "trips.txt",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the My Train Schedule timetable index")
    parser.add_argument("--zip", dest="zip_path", help="Existing polish_trains.zip")
    parser.add_argument(
        "--out",
        default=str(ROOT / "lambda" / "data" / "schedule.bin"),
        help="Output index path",
    )
    parser.add_argument(
        "--picker-dir",
        default=str(ROOT / "picker"),
        help="Where stations.js, stations.json and directions.json go",
    )
    parser.add_argument(
        "--directions-only",
        action="store_true",
        help="Write picker/directions.json from the existing index",
    )
    args = parser.parse_args()
    if args.directions_only:
        schedule = load(args.out)
        write_direction_file(schedule, Path(args.picker_dir) / "directions.json")
        return
    zip_path = Path(args.zip_path) if args.zip_path else _download()
    gtfs_dir = ROOT / "build" / "gtfs"
    _unzip(zip_path, gtfs_dir)
    started = time.perf_counter()
    _build(gtfs_dir, Path(args.out), Path(args.picker_dir))
    print(f"build finished in {time.perf_counter() - started:.1f}s")


def _download() -> Path:
    cache = ROOT / "build" / "polish_trains.zip"
    cache.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {GTFS_ZIP_URL}")
    request = urllib.request.Request(GTFS_ZIP_URL, headers={"User-Agent": "upcoming-trains-build/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        cache.write_bytes(response.read())
    print(f"saved {cache} ({cache.stat().st_size} bytes)")
    return cache


def _unzip(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for name in NEEDED:
            archive.extract(name, dest)


def _build(gtfs: Path, out_path: Path, picker: Path) -> None:
    feed = _read_feed_info(gtfs / "feed_info.txt")
    feed_start = parse_yyyymmdd(feed["feed_start_date"])
    feed_end = parse_yyyymmdd(feed["feed_end_date"])
    print(f"feed window {feed_start} through {feed_end} version {feed.get('feed_version')}")

    agencies = _read_agencies(gtfs / "agency.txt")
    routes = _read_routes(gtfs / "routes.txt")
    stations, parent_of, aliases = _read_stops(gtfs / "stops.txt")
    services = _read_services(gtfs / "calendar_dates.txt", feed_start, feed_end)
    trips = _read_trips(gtfs / "trips.txt", services, routes, agencies)
    print(f"stations {len(stations)} services in window {len(services)} trips {len(trips)}")

    events_by_station = _read_events(gtfs / "stop_times.txt", trips, parent_of)
    event_total = sum(len(rows) for rows in events_by_station.values())
    print(f"boardable events {event_total}")

    pool: dict[str, int] = {}

    def intern(text: str) -> int:
        if text not in pool:
            pool[text] = len(pool)
        return pool[text]

    service_ids = sorted({trip["service_id"] for trip in trips.values()})
    service_index = {service_id: index for index, service_id in enumerate(service_ids)}
    ndays = (feed_end - feed_start).days + 1
    service_bytes = []
    for service_id in service_ids:
        raw = bytearray(ndays)
        for day in services.get(service_id, ()):
            raw[(day - feed_start).days] = 1
        service_bytes.append(bytes(raw))

    trip_ids = sorted(trips)
    trip_index = {trip_id: index for index, trip_id in enumerate(trip_ids)}
    trip_rows = []
    for trip_id in trip_ids:
        trip = trips[trip_id]
        trip_rows.append(
            (
                intern(trip_id),
                intern(trip["headsign"]),
                intern(trip["route"]),
                intern(trip["agency"]),
                intern(trip["number"]),
                service_index[trip["service_id"]],
                1 if trip["bus"] else 0,
            )
        )

    id_to_index = {stop_id: index for index, (stop_id, _name) in enumerate(stations)}
    alias_rows = []
    for source_id, parent_id in sorted(aliases.items()):
        alias_rows.append((intern(source_id), intern(parent_id)))
    flat_events = []
    station_rows = []
    for stop_id, name in stations:
        rows = events_by_station.get(stop_id, [])
        rows.sort(key=lambda item: (item[0], item[2], item[4]))
        start = len(flat_events)
        for dep_sec, arr_sec, seq, flags, trip_id, next_id, prev_id in rows:
            flat_events.append(
                (
                    dep_sec,
                    arr_sec,
                    seq,
                    flags,
                    trip_index[trip_id],
                    id_to_index.get(next_id, NO_STATION) if next_id else NO_STATION,
                    id_to_index.get(prev_id, NO_STATION) if prev_id else NO_STATION,
                )
            )
        station_rows.append((intern(stop_id), intern(name), start, len(rows)))

    strings = [None] * len(pool)
    for text, index in pool.items():
        strings[index] = text

    out_path.parent.mkdir(parents=True, exist_ok=True)
    dump(
        str(out_path),
        feed_start=feed_start,
        feed_end=feed_end,
        built_unix=int(time.time()),
        strings=strings,
        services=service_bytes,
        stations=station_rows,
        trips=trip_rows,
        events=flat_events,
        aliases=alias_rows,
    )
    size = out_path.stat().st_size
    station_list = [{"id": stop_id, "name": name} for stop_id, name in stations]
    picker.mkdir(parents=True, exist_ok=True)
    (picker / "stations.json").write_text(
        json.dumps(station_list, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (picker / "stations.js").write_text(
        "window.STATIONS = "
        + json.dumps(station_list, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    meta = {
        "publisher": feed.get("feed_publisher_name", ""),
        "publisher_url": feed.get("feed_publisher_url", ""),
        "attribution": "PKP Polskie Linie Kolejowe S.A. and Mikołaj Kuranowski",
        "feed_version": feed.get("feed_version", ""),
        "feed_start": feed_start.isoformat(),
        "feed_end": feed_end.isoformat(),
        "built_at": datetime.now(UTC).isoformat(),
        "stations": len(stations),
        "trips": len(trip_rows),
        "events": len(flat_events),
        "bytes": size,
    }
    (out_path.parent / "feed.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out_path} ({size} bytes)")
    write_direction_file(load(str(out_path)), picker / "directions.json")
    _benchmark(out_path, feed_start)


def _benchmark(path: Path, feed_start) -> None:
    started = time.perf_counter()
    schedule = load(str(path))
    load_s = time.perf_counter() - started
    # Mid-afternoon on the first feed day, a busy Warsaw station.
    wall = datetime(feed_start.year, feed_start.month, feed_start.day, 16, 0)
    now = warsaw_wall_to_utc(wall)
    station_id = "33548"
    query_started = time.perf_counter()
    repeats = 20
    count = 0
    for _ in range(repeats):
        count = len(schedule.candidates(station_id, now))
    query_s = (time.perf_counter() - query_started) / repeats
    name = schedule.station_name(station_id)
    in_hour = [
        item
        for item in schedule.candidates(station_id, now)
        if now <= item.scheduled <= now + timedelta(hours=1)
    ]
    sample = ""
    if in_hour:
        sample = f"  e.g. {in_hour[0].route} towards {in_hour[0].direction}"
    print(
        f"load {load_s:.3f}s  query {name} x{repeats} avg {query_s * 1000:.1f}ms  "
        f"expanded candidates {count}  scheduled in the next hour {len(in_hour)}{sample}"
    )


def _read_feed_info(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("feed_info.txt is empty")
    return rows[0]


def _read_agencies(path: Path) -> dict:
    agencies = {}
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            agencies[row["agency_id"]] = row["agency_name"]
    return agencies


def _read_routes(path: Path) -> dict:
    routes = {}
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            routes[row["route_id"]] = row
    return routes


def _read_stops(path: Path) -> tuple[list[tuple[str, str]], dict]:
    stations = []
    parent_of = {}
    seen = set()
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            kind = row.get("location_type") or "0"
            stop_id = row["stop_id"]
            if kind == "1":
                parent_of[stop_id] = stop_id
                if stop_id not in seen:
                    stations.append((stop_id, row["stop_name"]))
                    seen.add(stop_id)
            elif kind == "0":
                parent_of[stop_id] = row.get("parent_station") or stop_id
    stations.sort(key=lambda item: (_sort_key(item[1]), item[0]))
    stations, parent_of, aliases = _merge_platform_stations(stations, parent_of)
    return stations, parent_of, aliases


def _merge_platform_stations(stations, parent_of):
    """Fold "X (Peron N)" into the station named X.

    The platform keeps no picker row. Stop times are stored on X, and the
    platform id is recorded as an alias so an old token still resolves.
    """
    by_name = {}
    for stop_id, name in stations:
        by_name[name] = stop_id
    aliases = {}
    drop = set()
    for stop_id, name in stations:
        match = _PERON_NAME.match(name)
        if match is None:
            continue
        parent_id = by_name.get(match.group("base"))
        if not parent_id or parent_id == stop_id:
            continue
        aliases[stop_id] = parent_id
        drop.add(stop_id)
    if not aliases:
        return stations, parent_of, aliases
    for stop_id, parent_id in list(parent_of.items()):
        mapped = aliases.get(parent_id)
        if mapped:
            parent_of[stop_id] = mapped
    kept = [(stop_id, name) for stop_id, name in stations if stop_id not in drop]
    return kept, parent_of, aliases


def _sort_key(name: str) -> str:
    from upcoming_trains.names import normalize_name

    return normalize_name(name)


def _read_services(path: Path, feed_start, feed_end) -> dict:
    added = defaultdict(set)
    removed = defaultdict(set)
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = parse_yyyymmdd(row["date"])
            if day < feed_start or day > feed_end:
                continue
            if row["exception_type"] == "2":
                removed[row["service_id"]].add(day)
            else:
                added[row["service_id"]].add(day)
    services = {}
    for service_id, days in added.items():
        kept = days - removed.get(service_id, set())
        if kept:
            services[service_id] = kept
    return services


def _read_trips(path: Path, services: dict, routes: dict, agencies: dict) -> dict:
    trips = {}
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["service_id"] not in services:
                continue
            route = routes.get(row["route_id"], {})
            agency_id = route.get("agency_id", "")
            number = (row.get("trip_short_name") or "").strip()
            trips[row["trip_id"]] = {
                "service_id": row["service_id"],
                "headsign": (row.get("trip_headsign") or "").strip(),
                "route": (route.get("route_short_name") or "").strip(),
                "agency": agencies.get(agency_id, agency_id),
                "number": number,
                "bus": (route.get("route_type") or "") == "3",
            }
    return trips


def _read_events(path: Path, trips: dict, parent_of: dict) -> dict:
    """One event per parent stop a trip calls at.

    Consecutive platforms of the same parent collapse: earliest arrival,
    latest departure, and pickup or drop-off if any platform allows it.
    A departure points at the next parent. An arrival points at the previous
    one. The terminus is an arrival only; the origin is a departure only.
    """
    by_trip = defaultdict(list)
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            trip = trips.get(row["trip_id"])
            if trip is None:
                continue
            arrival = row.get("arrival_time") or ""
            departure = row.get("departure_time") or ""
            if not arrival and not departure:
                continue
            parent = parent_of.get(row["stop_id"])
            if not parent:
                continue
            by_trip[row["trip_id"]].append(
                (
                    int(row["stop_sequence"] or 0),
                    parent,
                    _clock_seconds(arrival or departure),
                    _clock_seconds(departure or arrival),
                    (row.get("pickup_type") or "0") != "1",
                    (row.get("drop_off_type") or "0") != "1",
                )
            )
    events = defaultdict(list)
    for trip_id, stops in by_trip.items():
        stops.sort(key=lambda item: item[0])
        groups = []
        for sequence, parent, arrival, departure, pickup, dropoff in stops:
            if groups and groups[-1]["parent"] == parent:
                group = groups[-1]
                if arrival < group["arrival"]:
                    group["arrival"] = arrival
                if departure > group["departure"]:
                    group["departure"] = departure
                group["pickup"] = group["pickup"] or pickup
                group["dropoff"] = group["dropoff"] or dropoff
                continue
            groups.append(
                {
                    "sequence": sequence,
                    "parent": parent,
                    "arrival": arrival,
                    "departure": departure,
                    "pickup": pickup,
                    "dropoff": dropoff,
                }
            )
        if len(groups) < 2:
            continue
        last = len(groups) - 1
        for index, group in enumerate(groups):
            flags = 0
            if index < last and group["pickup"]:
                flags |= FLAG_DEPARTURE
            if index > 0 and group["dropoff"]:
                flags |= FLAG_ARRIVAL
            if not flags:
                continue
            next_id = groups[index + 1]["parent"] if index < last else ""
            prev_id = groups[index - 1]["parent"] if index > 0 else ""
            events[group["parent"]].append(
                (
                    group["departure"],
                    group["arrival"],
                    group["sequence"],
                    flags,
                    trip_id,
                    next_id,
                    prev_id,
                )
            )
    return events


def _clock_seconds(clock: str) -> int:
    hour, minute, second = clock.split(":")
    return int(hour) * 3600 + int(minute) * 60 + int(float(second))


def write_direction_file(schedule, path: Path) -> None:
    """One JSON object, fetched when the picker reaches the direction step."""
    catalog = catalog_from_schedule(schedule)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"wrote {path} ({path.stat().st_size} bytes, {len(catalog)} stations)")


def catalog_from_schedule(schedule) -> dict:
    """Neighbour stops for each station, merged across departures and arrivals.

    The display name uses the same city-prefix rule as speech. Long-distance
    brands are one option, id "LD". Neighbour ids are GTFS parent stop ids.
    """
    from upcoming_trains.speech import _is_long_distance, line_label, local_name, station_city

    names = {}
    for stop_id, name in zip(schedule.station_ids, schedule.station_names):
        names[stop_id] = name
    buckets: dict = {}
    long_distance = set()
    for home in schedule.station_ids:
        for neighbor, route, agency, bus in schedule.iter_links(home):
            if _is_long_distance(route, agency, bus):
                long_distance.add(home)
                continue
            if not neighbor or neighbor == home or neighbor not in names:
                continue
            display, _spoken = line_label(route, agency, bus)
            if not display:
                continue
            home_buckets = buckets.setdefault(home, {})
            home_buckets.setdefault(neighbor, set()).add(display)
    catalog = {}
    for home in set(buckets) | long_distance:
        city = station_city(names.get(home, ""))
        rows = []
        for neighbor, chips in buckets.get(home, {}).items():
            raw = names.get(neighbor, neighbor)
            shown = local_name(raw, city) or raw
            rows.append(
                {
                    "id": neighbor,
                    "name": shown,
                    "lines": sorted(chips, key=_line_sort),
                }
            )
        rows.sort(key=lambda row: (row["name"].casefold(), row["id"]))
        if home in long_distance:
            rows.append({"id": "LD", "name": "Long-distance (Intercity)", "lines": []})
        if rows:
            catalog[home] = rows
    ordered = {}
    for key in sorted(catalog, key=_station_sort):
        ordered[key] = catalog[key]
    return ordered


def _line_sort(label: str):
    match = re.match(r"^([A-Za-z]+)(\d+)(.*)$", label)
    if match:
        return (match.group(1).casefold(), int(match.group(2)), match.group(3).casefold())
    return (label.casefold(), 0, "")


def _station_sort(stop_id: str):
    if stop_id.isdigit():
        return (0, int(stop_id))
    return (1, stop_id)


if __name__ == "__main__":
    main()

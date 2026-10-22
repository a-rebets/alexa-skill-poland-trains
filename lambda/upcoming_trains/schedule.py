"""Compact all-stations departure index.

The national GTFS zip is about 30 MB and stop_times.txt is about 48 MB.
An Alexa-hosted function cannot download and parse that inside the 8 second
response budget, and Alexa-hosted skills have no scheduler. This file is built
on a laptop (`scripts/build_data.py`) and deployed with the skill.

Layout, little-endian:

    char[4]  magic b'UTRN'
    uint16   version = 2
    uint32   feed_start YYYYMMDD
    uint32   feed_end   YYYYMMDD
    uint16   ndays      inclusive day count
    uint32   built_unix
    uint32   string_count
    uint32   string_offsets[string_count + 1]
    bytes    string bytes, utf-8
    uint16   service_count
    uint8    service_days[service_count * ndays]   1 if that service runs
    uint32   station_count
    records  station: 4 x uint32
             (id_str, name_str, event_start, event_count)
    uint32   trip_count
    records  trip: 5 x uint32, uint16 service, uint8 bus, uint8 pad
             (trip_id, headsign, route, agency, number, service, bus, pad)
    uint32   event_count
    records  event, 20 bytes, grouped by station:
             uint32 departure seconds
             uint32 arrival seconds
             uint16 stop_sequence
             uint16 flags          bit 0 departure, bit 1 arrival
             uint32 trip
             uint16 next station   index, or 0xFFFF
             uint16 prev station   index, or 0xFFFF

Seconds may be >= 86400. That is a GTFS time past 24:00.
A departure's next station is where the train calls next. An arrival's
previous station is where it called before this one.

After the events:

    uint32   alias_count
    records  alias: 2 x uint32 (platform stop id, parent stop id)

A platform such as "Warszawa Zachodnia (Peron 9)" is not its own station.
Its id still resolves to the parent, so an old token keeps working.
"""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from upcoming_trains.constants import (
    MAX_EARLY_MINUTES,
    MAX_LATE_MINUTES,
    WINDOW_MINUTES,
)
from upcoming_trains.timeutil import (
    format_yyyymmdd,
    gtfs_clock_to_utc,
    parse_yyyymmdd,
    warsaw_date,
    yyyymmdd_int,
)

MAGIC = b"UTRN"
VERSION = 2
HEADER = struct.Struct("<4sHIIHI")
STATION = struct.Struct("<4I")
TRIP = struct.Struct("<5IHBB")
EVENT = struct.Struct("<IIHHIHH")
assert TRIP.size == 24
assert EVENT.size == 20

FLAG_DEPARTURE = 1
FLAG_ARRIVAL = 2
NO_STATION = 0xFFFF

UTC = timezone.utc


@dataclass(frozen=True)
class Candidate:
    station_id: str
    station_name: str
    trip_id: str
    headsign: str
    route: str
    agency: str
    number: str
    bus: bool
    stop_sequence: int
    service_day: date
    scheduled: datetime
    direction: str
    direction_id: str = ""


@dataclass
class _Station:
    stop_id: str
    name: str
    event_start: int
    event_count: int


@dataclass
class _Trip:
    trip_id: str
    headsign: str
    route: str
    agency: str
    number: str
    service: int
    bus: bool


class Schedule:
    def __init__(
        self,
        feed_start: date,
        feed_end: date,
        built_at: datetime,
        strings: list[str],
        services: list[bytes],
        stations: dict[str, _Station],
        trips: list[_Trip],
        events: bytes,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self.feed_start = feed_start
        self.feed_end = feed_end
        self.built_at = built_at
        self.strings = strings
        self.services = services
        self.stations = stations
        self.trips = trips
        self.events = events
        self.aliases = aliases or {}
        self.ndays = (feed_end - feed_start).days + 1
        self.station_ids = [station.stop_id for station in stations.values()]
        self.station_names = [station.name for station in stations.values()]
        self._local_neighbours: dict[str, set[str]] = {}

    def canonical(self, stop_id: str) -> str:
        """Parent station for a platform variant. Other ids stay as they are."""
        return self.aliases.get(stop_id, stop_id)

    def covers(self, day: date) -> bool:
        return self.feed_start <= day <= self.feed_end

    def station_name(self, stop_id: str) -> str | None:
        station = self.stations.get(self.canonical(stop_id))
        return None if station is None else station.name

    def service_runs(self, service_index: int, day: date) -> bool:
        delta = (day - self.feed_start).days
        if delta < 0 or delta >= self.ndays:
            return False
        days = self.services[service_index]
        return days[delta] == 1

    def candidates(
        self,
        station_id: str,
        now: datetime,
        kind: str = "departure",
        when: datetime | None = None,
    ) -> list[Candidate]:
        """Boardable calls whose scheduled time is near `when` or `now`.

        The window is wider than 60 minutes so a later step can apply a delay
        and still keep a train that will actually run inside the hour.
        `kind` is "departure" (time and the next stop) or "arrival" (time and
        the previous stop). `when` is the start of a named hour. The scan
        uses that instant's Warsaw date, plus the day before and the day
        after, so a 24:xx trip from the previous service date is included
        when the hour crosses midnight.
        """
        station = self.stations.get(self.canonical(station_id))
        if station is None or station.event_count == 0:
            return []
        anchor = when or now
        if anchor.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        want = FLAG_ARRIVAL if kind == "arrival" else FLAG_DEPARTURE
        today = warsaw_date(anchor)
        service_days = (today - timedelta(days=1), today, today + timedelta(days=1))
        start = anchor - timedelta(minutes=MAX_LATE_MINUTES)
        end = anchor + timedelta(minutes=WINDOW_MINUTES + MAX_EARLY_MINUTES)
        found: list[Candidate] = []
        base = station.event_start * EVENT.size
        names = self.station_names
        for offset in range(station.event_count):
            dep_sec, arr_sec, seq, flags, trip_index, next_i, prev_i = EVENT.unpack_from(
                self.events, base + offset * EVENT.size
            )
            if not (flags & want):
                continue
            trip = self.trips[trip_index]
            sec = arr_sec if kind == "arrival" else dep_sec
            neighbor = prev_i if kind == "arrival" else next_i
            direction = names[neighbor] if neighbor < len(names) else ""
            direction_id = self.station_ids[neighbor] if neighbor < len(self.station_ids) else ""
            for service_day in service_days:
                if not self.service_runs(trip.service, service_day):
                    continue
                scheduled = gtfs_clock_to_utc(service_day, sec)
                if start <= scheduled <= end:
                    found.append(
                        Candidate(
                            station_id=station.stop_id,
                            station_name=station.name,
                            trip_id=trip.trip_id,
                            headsign=trip.headsign or "",
                            route=trip.route,
                            agency=trip.agency,
                            number=trip.number,
                            bus=trip.bus,
                            stop_sequence=seq,
                            service_day=service_day,
                            scheduled=scheduled,
                            direction=direction,
                            direction_id=direction_id,
                        )
                    )
        return found

    def iter_links(self, station_id: str):
        """Yield (neighbour stop id, route, agency, bus) for next and previous stops.

        Long-distance calls are included. The caller decides which neighbours
        are local directions.
        """
        station = self.stations.get(self.canonical(station_id))
        if station is None or station.event_count == 0:
            return
        ids = self.station_ids
        base = station.event_start * EVENT.size
        for offset in range(station.event_count):
            _dep, _arr, _seq, flags, trip_index, next_i, prev_i = EVENT.unpack_from(
                self.events, base + offset * EVENT.size
            )
            trip = self.trips[trip_index]
            if flags & FLAG_DEPARTURE and next_i < len(ids):
                yield ids[next_i], trip.route, trip.agency, trip.bus
            if flags & FLAG_ARRIVAL and prev_i < len(ids):
                yield ids[prev_i], trip.route, trip.agency, trip.bus

    def local_neighbour_ids(self, station_id: str) -> set[str]:
        """Stop ids that a local train uses as the next or previous stop.

        Cached. Platform variants are already stored on the parent station.
        """
        from upcoming_trains.speech import _is_long_distance

        key = self.canonical(station_id)
        cached = self._local_neighbours.get(key)
        if cached is not None:
            return cached
        found = set()
        for neighbor, route, agency, bus in self.iter_links(key):
            if not neighbor or neighbor == key:
                continue
            if _is_long_distance(route, agency, bus):
                continue
            found.add(neighbor)
        self._local_neighbours[key] = found
        return found


class _LazyStrings:
    """Decode a string the first time a station or a trip asks for it."""

    def __init__(self, blob, offsets) -> None:
        self._blob = blob
        self._offsets = offsets
        self._cache = {}

    def __len__(self) -> int:
        return len(self._offsets) - 1

    def __getitem__(self, index: int) -> str:
        hit = self._cache.get(index)
        if hit is not None:
            return hit
        start = self._offsets[index]
        end = self._offsets[index + 1]
        text = bytes(self._blob[start:end]).decode("utf-8")
        self._cache[index] = text
        return text


class _LazyTrips:
    """Unpack a trip record on first use. The token's stations touch a few dozen."""

    def __init__(self, buf, start: int, count: int, strings) -> None:
        self._buf = buf
        self._start = start
        self._count = count
        self._strings = strings
        self._cache = {}

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, index: int) -> _Trip:
        if index < 0 or index >= self._count:
            raise IndexError(index)
        hit = self._cache.get(index)
        if hit is not None:
            return hit
        offset = self._start + index * TRIP.size
        trip_i, head_i, route_i, agency_i, number_i, service, bus, _pad = TRIP.unpack_from(
            self._buf, offset
        )
        trip = _Trip(
            self._strings[trip_i],
            self._strings[head_i],
            self._strings[route_i],
            self._strings[agency_i],
            self._strings[number_i],
            service,
            bool(bus),
        )
        self._cache[index] = trip
        return trip


def load(path: str) -> Schedule:
    """Map the index and decode trips only when a station's events name them.

    The file already stores each station's event offset. Reading every trip
    into a Python object is most of the parse, and one request uses the
    trips at a handful of stations.
    """
    size = os.path.getsize(path)
    if size < HEADER.size:
        raise ValueError("schedule file is truncated")
    handle = open(path, "rb")
    try:
        buf = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
    finally:
        handle.close()
    try:
        return _load_mapped(buf)
    except Exception:
        buf.close()
        raise


def _load_mapped(buf) -> Schedule:
    if len(buf) < HEADER.size:
        raise ValueError("schedule file is truncated")
    magic, version, start_i, end_i, ndays, built_unix = HEADER.unpack_from(buf, 0)
    if magic != MAGIC or version != VERSION:
        raise ValueError("schedule file is not a UTRN index")
    cursor = HEADER.size
    (string_count,) = struct.unpack_from("<I", buf, cursor)
    cursor += 4
    offsets = struct.unpack_from("<" + "I" * (string_count + 1), buf, cursor)
    cursor += 4 * (string_count + 1)
    blob_end = cursor + offsets[-1]
    if blob_end > len(buf):
        raise ValueError("schedule string pool is truncated")
    strings = _LazyStrings(memoryview(buf)[cursor:blob_end], offsets)
    cursor = blob_end
    (service_count,) = struct.unpack_from("<H", buf, cursor)
    cursor += 2
    services = []
    for _ in range(service_count):
        raw = buf[cursor : cursor + ndays]
        if len(raw) != ndays:
            raise ValueError("schedule calendar is truncated")
        services.append(raw)
        cursor += ndays
    (station_count,) = struct.unpack_from("<I", buf, cursor)
    cursor += 4
    stations: dict[str, _Station] = {}
    for _ in range(station_count):
        id_i, name_i, event_start, event_count = STATION.unpack_from(buf, cursor)
        cursor += STATION.size
        stop_id = strings[id_i]
        stations[stop_id] = _Station(stop_id, strings[name_i], event_start, event_count)
    (trip_count,) = struct.unpack_from("<I", buf, cursor)
    cursor += 4
    trip_bytes = trip_count * TRIP.size
    if cursor + trip_bytes > len(buf):
        raise ValueError("schedule trips are truncated")
    trips = _LazyTrips(buf, cursor, trip_count, strings)
    cursor += trip_bytes
    (event_count,) = struct.unpack_from("<I", buf, cursor)
    cursor += 4
    event_bytes = event_count * EVENT.size
    # The event table stays packed. Callers unpack the stations they need.
    events = memoryview(buf)[cursor : cursor + event_bytes]
    if len(events) != event_bytes:
        raise ValueError("schedule events are truncated")
    cursor += event_bytes
    if cursor + 4 > len(buf):
        raise ValueError("schedule aliases are truncated")
    (alias_count,) = struct.unpack_from("<I", buf, cursor)
    cursor += 4
    aliases = {}
    for _ in range(alias_count):
        if cursor + 8 > len(buf):
            raise ValueError("schedule aliases are truncated")
        src_i, dst_i = struct.unpack_from("<II", buf, cursor)
        cursor += 8
        aliases[strings[src_i]] = strings[dst_i]
    if cursor != len(buf):
        raise ValueError("schedule file has trailing bytes")
    feed_start = parse_yyyymmdd(f"{start_i:08d}")
    feed_end = parse_yyyymmdd(f"{end_i:08d}")
    if (feed_end - feed_start).days + 1 != ndays:
        raise ValueError("schedule day count does not match the feed window")
    built_at = datetime.fromtimestamp(built_unix, UTC)
    schedule = Schedule(
        feed_start, feed_end, built_at, strings, services, stations, trips, events, aliases
    )
    schedule._mapped = buf
    return schedule


def dump(
    path: str,
    *,
    feed_start: date,
    feed_end: date,
    built_unix: int,
    strings: list[str],
    services: list[bytes],
    stations: list[tuple[int, int, int, int]],
    trips: list[tuple[int, int, int, int, int, int, int]],
    events: list[tuple[int, int, int, int, int, int, int]],
    aliases: list | None = None,
) -> None:
    """Write an index. See the module docstring for the field order.

    Each event is (departure seconds, arrival seconds, stop sequence, flags,
    trip index, next station index, previous station index).
    """
    ndays = (feed_end - feed_start).days + 1
    if ndays <= 0 or ndays > 4000:
        raise ValueError("feed window is empty or longer than 4000 days")
    for service in services:
        if len(service) != ndays:
            raise ValueError("service bitmap has the wrong length")
    parts = [HEADER.pack(
        MAGIC,
        VERSION,
        yyyymmdd_int(feed_start),
        yyyymmdd_int(feed_end),
        ndays,
        built_unix,
    )]
    encoded = [text.encode("utf-8") for text in strings]
    offsets = [0]
    for blob in encoded:
        offsets.append(offsets[-1] + len(blob))
    parts.append(struct.pack("<I", len(strings)))
    parts.append(struct.pack("<" + "I" * len(offsets), *offsets))
    parts.extend(encoded)
    parts.append(struct.pack("<H", len(services)))
    parts.extend(services)
    parts.append(struct.pack("<I", len(stations)))
    for station in stations:
        parts.append(STATION.pack(*station))
    parts.append(struct.pack("<I", len(trips)))
    for trip in trips:
        parts.append(TRIP.pack(*trip, 0))
    parts.append(struct.pack("<I", len(events)))
    for dep_sec, arr_sec, seq, flags, trip_index, next_i, prev_i in events:
        if (
            dep_sec < 0
            or arr_sec < 0
            or seq < 0
            or seq > 0xFFFF
            or flags < 0
            or flags > 0xFFFF
            or next_i < 0
            or next_i > 0xFFFF
            or prev_i < 0
            or prev_i > 0xFFFF
            or trip_index < 0
        ):
            raise ValueError("event field does not fit")
        parts.append(EVENT.pack(dep_sec, arr_sec, seq, flags, trip_index, next_i, prev_i))
    if aliases is None:
        aliases = []
    parts.append(struct.pack("<I", len(aliases)))
    for src_i, dst_i in aliases:
        parts.append(struct.pack("<II", src_i, dst_i))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"".join(parts))


def default_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "data", "schedule.bin")


def describe_window(schedule: Schedule) -> str:
    start = format_yyyymmdd(schedule.feed_start)
    end = format_yyyymmdd(schedule.feed_end)
    return (
        f"{start[0:4]}-{start[4:6]}-{start[6:8]} through "
        f"{end[0:4]}-{end[4:6]}-{end[6:8]}"
    )

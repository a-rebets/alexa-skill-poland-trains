"""GTFS-Realtime reader for the Polish passenger feed.

updates.pb is standard GTFS-Realtime, about 6 MB, and every trip id in it
matches the static GTFS. updates.json is about 34 MB and is a custom document,
not GTFS-Realtime JSON. The host does not gzip either file. The protobuf is
the one that fits the response budget.

The parser walks the wire format and fully decodes only the trip ids the
board asked for. It does not import a protobuf library.
"""

from __future__ import annotations

import gzip
import http.client
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone

from upcoming_trains.constants import (
    REALTIME_CACHE_SECONDS,
    REALTIME_FUTURE_SKEW_SECONDS,
    REALTIME_MAX_AGE_SECONDS,
    REALTIME_SOCKET_TIMEOUT_SECONDS,
    REALTIME_URL,
    REALTIME_WAIT_SECONDS,
)
from upcoming_trains.timing import add as add_ms

UTC = timezone.utc
_MAX_BYTES = 20_000_000
_USER_AGENT = "upcoming-trains-skill/1.0 (+https://mkuran.pl/gtfs/)"
_cache_lock = threading.Lock()
_cache_cv = threading.Condition(_cache_lock)
_conn_lock = threading.Lock()
_conn = None
_conn_host = ""
_raw = None
_raw_at = 0.0
_worker = None
_worker_error = ""

# TripDescriptor.schedule_relationship
_CANCELED = 3
# StopTimeUpdate.schedule_relationship
_SKIPPED = 1


@dataclass
class StopUpdate:
    skipped: bool = False
    departure_unix: int | None = None
    arrival_unix: int | None = None
    departure_delay: int | None = None
    arrival_delay: int | None = None


@dataclass
class TripUpdate:
    cancelled: bool = False
    start_date: str = ""
    delay: int | None = None
    stops: dict[int, StopUpdate] = field(default_factory=dict)


@dataclass
class ParsedFeed:
    timestamp: int | None
    trips: dict[str, TripUpdate]
    error: str | None = None

    def is_fresh(self, now: datetime) -> bool:
        if self.timestamp is None or self.error:
            return False
        age = int(now.astimezone(UTC).timestamp()) - self.timestamp
        return -REALTIME_FUTURE_SKEW_SECONDS <= age <= REALTIME_MAX_AGE_SECONDS

    def age_seconds(self, now: datetime) -> int | None:
        if self.timestamp is None:
            return None
        return int(now.astimezone(UTC).timestamp()) - self.timestamp


def updates_for_board(feed: ParsedFeed, now: datetime) -> tuple[dict[str, TripUpdate] | None, str]:
    """Return trip updates when the feed is fresh, else a spoken fallback note."""
    if feed.error:
        return None, "I couldn't fetch live updates, so these are scheduled times."
    if not feed.is_fresh(now):
        return None, "Live updates look frozen, so these are scheduled times."
    return feed.trips, ""


def prefetch(url: str = REALTIME_URL) -> bool:
    """Start a download when the in-memory copy is older than about 30 seconds.

    Returns True when that copy is already fresh, so the caller can skip work
    that only exists to overlap a download.
    """
    global _worker, _worker_error
    with _cache_lock:
        if _cache_fresh():
            return True
        if _worker is not None and _worker.is_alive():
            return False
        _worker_error = ""
        _worker = threading.Thread(target=_fill_cache, args=(url,), daemon=True)
        _worker.start()
        return False


def fetch_feed(wanted_trip_ids: set[str], url: str = REALTIME_URL) -> ParsedFeed:
    """Parsed trips, or an error feed when the file is not here within the wait.

    A slow download keeps running in the background and can satisfy the next
    request. This call does not block longer than REALTIME_WAIT_SECONDS.
    """
    started = time.perf_counter()
    try:
        raw = _wait_for_bytes(url, REALTIME_WAIT_SECONDS)
        if raw is None:
            with _cache_lock:
                error = _worker_error or "timed out"
            return ParsedFeed(timestamp=None, trips={}, error=error)
        return parse_feed(raw, wanted_trip_ids)
    except Exception as exc:
        return ParsedFeed(timestamp=None, trips={}, error=str(exc) or exc.__class__.__name__)
    finally:
        add_ms("realtime_ms", (time.perf_counter() - started) * 1000.0)


def reset_for_tests() -> None:
    """Drop the cached file and release a test thread blocked in the download."""
    global _raw, _raw_at, _worker, _worker_error
    with _cache_lock:
        _raw = None
        _raw_at = 0.0
        _worker_error = ""
        _worker = None
        _cache_cv.notify_all()
    with _conn_lock:
        _drop_connection()


def _cache_fresh() -> bool:
    return _raw is not None and (time.monotonic() - _raw_at) < REALTIME_CACHE_SECONDS


def _fill_cache(url: str) -> None:
    global _raw, _raw_at, _worker_error
    me = threading.current_thread()
    try:
        data = _download(url)
    except Exception as exc:
        message = "timed out" if isinstance(exc, (socket.timeout, TimeoutError)) else "fetch failed"
        with _cache_lock:
            if me is not _worker:
                return
            _worker_error = message
            _cache_cv.notify_all()
        return
    with _cache_lock:
        if me is not _worker:
            return
        _raw = data
        _raw_at = time.monotonic()
        _worker_error = ""
        _cache_cv.notify_all()


def _wait_for_bytes(url: str, timeout: float):
    prefetch(url)
    deadline = time.perf_counter() + timeout
    with _cache_lock:
        while True:
            if _cache_fresh():
                return _raw
            worker = _worker
            failed = bool(_worker_error) and (worker is None or not worker.is_alive())
            if failed:
                return None
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            _cache_cv.wait(remaining)


def decompress_if_needed(raw: bytes, content_encoding: str | None = None) -> bytes:
    if content_encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def _download(url: str) -> bytes:
    """GET the feed on a reused TLS connection. One retry covers a dead socket."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("realtime url must be https")
    path = parts.path or "/"
    if parts.query:
        path = path + "?" + parts.query
    headers = {
        "Accept-Encoding": "gzip",
        "User-Agent": _USER_AGENT,
        "Connection": "keep-alive",
    }
    last = None
    for _attempt in (1, 2):
        try:
            with _conn_lock:
                conn = _connection(parts.hostname, parts.port or 443)
                conn.timeout = REALTIME_SOCKET_TIMEOUT_SECONDS
                conn.request("GET", path, headers=headers)
                response = conn.getresponse()
                raw = response.read(_MAX_BYTES + 1)
                status = response.status
                encoding = response.getheader("Content-Encoding")
                if status != 200:
                    _drop_connection()
                    raise RuntimeError("realtime feed status %s" % status)
                if len(raw) > _MAX_BYTES:
                    _drop_connection()
                    raise RuntimeError("realtime feed is larger than 20 MB")
            return decompress_if_needed(raw, encoding)
        except Exception as exc:
            with _conn_lock:
                _drop_connection()
            last = exc
    raise last


def _connection(host: str, port: int):
    global _conn, _conn_host
    target = host + ":" + str(port)
    if _conn is None or _conn_host != target:
        _drop_connection()
        _conn = http.client.HTTPSConnection(host, port, timeout=REALTIME_SOCKET_TIMEOUT_SECONDS)
        _conn_host = target
    return _conn


def _drop_connection() -> None:
    global _conn, _conn_host
    conn = _conn
    _conn = None
    _conn_host = ""
    if conn is not None:
        try:
            conn.close()
        except Exception:
            return


def parse_feed(data: bytes, wanted_trip_ids: set[str]) -> ParsedFeed:
    """Scan a FeedMessage. `wanted_trip_ids` empty means keep every trip."""
    timestamp = None
    trips: dict[str, TripUpdate] = {}
    view = memoryview(data)
    index = 0
    limit = len(view)
    keep_all = not wanted_trip_ids
    while index < limit:
        tag, index = _varint(view, index, limit)
        field_number = tag >> 3
        wire = tag & 7
        if wire != 2:
            index = _skip(view, index, limit, wire)
            continue
        length, index = _varint(view, index, limit)
        end = index + length
        if end > limit:
            raise ValueError("truncated realtime field")
        if field_number == 1 and timestamp is None:
            timestamp = _header_timestamp(view, index, end)
        elif field_number == 2:
            update = _entity(view, index, end, wanted_trip_ids, keep_all)
            if update is not None:
                trip_id, parsed = update
                trips[trip_id] = parsed
        index = end
    return ParsedFeed(timestamp=timestamp, trips=trips)


def _header_timestamp(view: memoryview, start: int, end: int) -> int | None:
    index = start
    found = None
    while index < end:
        tag, index = _varint(view, index, end)
        field_number = tag >> 3
        wire = tag & 7
        if wire == 0:
            value, index = _varint(view, index, end)
            if field_number == 3:
                found = value
        else:
            index = _skip(view, index, end, wire)
    return found


def _entity(
    view: memoryview,
    start: int,
    end: int,
    wanted: set[str],
    keep_all: bool,
) -> tuple[str, TripUpdate] | None:
    index = start
    trip_blob = None
    deleted = False
    while index < end:
        tag, index = _varint(view, index, end)
        field_number = tag >> 3
        wire = tag & 7
        if wire == 0:
            value, index = _varint(view, index, end)
            if field_number == 2 and value:
                deleted = True
            continue
        if wire != 2:
            index = _skip(view, index, end, wire)
            continue
        length, index = _varint(view, index, end)
        blob_end = index + length
        if field_number == 3:
            trip_blob = (index, blob_end)
        index = blob_end
    if deleted or trip_blob is None:
        return None
    return _trip_update(view, trip_blob[0], trip_blob[1], wanted, keep_all)


def _trip_update(
    view: memoryview,
    start: int,
    end: int,
    wanted: set[str],
    keep_all: bool,
) -> tuple[str, TripUpdate] | None:
    index = start
    descriptor = None
    stop_blobs = []
    trip_delay = None
    while index < end:
        tag, index = _varint(view, index, end)
        field_number = tag >> 3
        wire = tag & 7
        if wire == 0:
            value, index = _varint(view, index, end)
            if field_number == 5:
                trip_delay = _signed(value)
            continue
        if wire != 2:
            index = _skip(view, index, end, wire)
            continue
        length, index = _varint(view, index, end)
        blob_end = index + length
        if field_number == 1:
            descriptor = (index, blob_end)
        elif field_number == 2:
            stop_blobs.append((index, blob_end))
        index = blob_end
    if descriptor is None:
        return None
    trip_id, start_date, cancelled = _descriptor(view, descriptor[0], descriptor[1])
    if not trip_id:
        return None
    if not keep_all and trip_id not in wanted:
        return None
    update = TripUpdate(cancelled=cancelled, start_date=start_date, delay=trip_delay)
    if not cancelled:
        for blob_start, blob_end in stop_blobs:
            seq, stop = _stop_update(view, blob_start, blob_end)
            if seq is not None:
                update.stops[seq] = stop
    return trip_id, update


def _descriptor(view: memoryview, start: int, end: int) -> tuple[str, str, bool]:
    index = start
    trip_id = ""
    start_date = ""
    relationship = 0
    while index < end:
        tag, index = _varint(view, index, end)
        field_number = tag >> 3
        wire = tag & 7
        if wire == 0:
            value, index = _varint(view, index, end)
            if field_number == 4:
                relationship = value
            continue
        if wire != 2:
            index = _skip(view, index, end, wire)
            continue
        length, index = _varint(view, index, end)
        if field_number == 1:
            trip_id = bytes(view[index : index + length]).decode("utf-8", "replace")
        elif field_number == 3:
            start_date = bytes(view[index : index + length]).decode("utf-8", "replace")
        index += length
    return trip_id, start_date, relationship == _CANCELED


def _stop_update(view: memoryview, start: int, end: int) -> tuple[int | None, StopUpdate]:
    index = start
    sequence = None
    relationship = 0
    arrival = None
    departure = None
    while index < end:
        tag, index = _varint(view, index, end)
        field_number = tag >> 3
        wire = tag & 7
        if wire == 0:
            value, index = _varint(view, index, end)
            if field_number == 1:
                sequence = value
            elif field_number == 5:
                relationship = value
            continue
        if wire != 2:
            index = _skip(view, index, end, wire)
            continue
        length, index = _varint(view, index, end)
        if field_number == 2:
            arrival = _time_event(view, index, index + length)
        elif field_number == 3:
            departure = _time_event(view, index, index + length)
        index += length
    stop = StopUpdate(skipped=relationship == _SKIPPED)
    if departure is not None:
        stop.departure_delay, stop.departure_unix = departure
    if arrival is not None:
        stop.arrival_delay, stop.arrival_unix = arrival
    return sequence, stop


def _time_event(view: memoryview, start: int, end: int) -> tuple[int | None, int | None]:
    index = start
    delay = None
    when = None
    while index < end:
        tag, index = _varint(view, index, end)
        field_number = tag >> 3
        wire = tag & 7
        if wire == 0:
            value, index = _varint(view, index, end)
            if field_number == 1:
                delay = _signed(value)
            elif field_number == 2:
                when = value if value > 1_000_000_000 else None
            continue
        index = _skip(view, index, end, wire)
    return delay, when


def _signed(value: int) -> int:
    """Protobuf int32/int64 uses a sign-extended 64-bit varint, not zigzag."""
    if value >= (1 << 63):
        value -= 1 << 64
    return value


def _varint(view: memoryview, index: int, limit: int) -> tuple[int, int]:
    number = 0
    shift = 0
    while index < limit and shift <= 63:
        byte = view[index]
        index += 1
        number |= (byte & 0x7F) << shift
        if byte < 128:
            return number, index
        shift += 7
    raise ValueError("bad varint in realtime feed")


def _skip(view: memoryview, index: int, limit: int, wire: int) -> int:
    if wire == 0:
        _, index = _varint(view, index, limit)
        return index
    if wire == 1:
        return index + 8
    if wire == 5:
        return index + 4
    if wire == 2:
        length, index = _varint(view, index, limit)
        return index + length
    raise ValueError("unsupported protobuf wire type")

"""GTFS-Realtime reader for the Polish passenger feed.

updates.pb is standard GTFS-Realtime, about 5 MB, and every trip id in it
matches the static GTFS. updates.json is about 34 MB and is a custom document,
not GTFS-Realtime JSON. The host accepts a gzip request but currently sends
the protobuf uncompressed. The body is the one that fits the response budget.

The handler never reads the socket itself. One background download owns DNS,
connect, TLS, headers, and the body. The handler polls a deadline and then
speaks scheduled times. A download already running is not started again.
Lambda freezes that thread between invokes; the next thaw resumes it, and
the bytes land in the cache when the read finishes.

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
from upcoming_trains.timing import set_realtime

UTC = timezone.utc
_MAX_BYTES = 20_000_000
_USER_AGENT = "upcoming-trains-skill/1.0 (+https://mkuran.pl/gtfs/)"
_cache_lock = threading.Lock()
_conn_lock = threading.Lock()
_conn = None
_conn_host = ""
_raw = None
_stored_at = 0.0
_etag = ""
_modified = ""
_generation = 0
_parsed = {}
_scanned = set()
_feed_timestamp = None
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
    """Start a download when we have no body yet.

    Returns True when a body is already in memory, including one past the
    TTL. The caller can skip work that only exists to overlap a download.
    A stale body starts a conditional refresh and still counts as present.
    A download that is already running is left alone.
    """
    with _cache_lock:
        if _raw is not None:
            if not _within_ttl():
                _start_worker_locked(url)
            return True
        if _worker is not None and _worker.is_alive():
            return False
        _start_worker_locked(url)
        return False


def fetch_feed(
    wanted_trip_ids: set[str],
    url: str = REALTIME_URL,
    deadline: float | None = None,
) -> ParsedFeed:
    """Parsed trips, or an error feed when no body is in memory by `deadline`.

    `deadline` is a monotonic time. It defaults to 1.5 s from now. The live
    path passes 1.5 s from the start of the handler. This function does not
    open a socket; a slow download keeps running and can satisfy a later call.
    """
    started = time.perf_counter()
    kind = "missed"
    try:
        if deadline is None:
            deadline = time.monotonic() + REALTIME_WAIT_SECONDS
        kind = _acquire_feed(url, deadline)
        if kind is None:
            kind = "missed"
            with _cache_lock:
                error = _worker_error or "timed out"
            return ParsedFeed(timestamp=None, trips={}, error=error)
        return _trips_from_cache(wanted_trip_ids)
    except Exception as exc:
        kind = "missed"
        return ParsedFeed(timestamp=None, trips={}, error=str(exc) or exc.__class__.__name__)
    finally:
        set_realtime(kind)
        add_ms("realtime_ms", (time.perf_counter() - started) * 1000.0)


def reset_for_tests() -> None:
    """Drop the cached file and detach a test thread blocked in the download."""
    global _raw, _stored_at, _etag, _modified, _generation, _parsed, _scanned
    global _feed_timestamp, _worker, _worker_error
    with _cache_lock:
        _raw = None
        _stored_at = 0.0
        _etag = ""
        _modified = ""
        _generation += 1
        _parsed = {}
        _scanned = set()
        _feed_timestamp = None
        _worker_error = ""
        _worker = None
    with _conn_lock:
        _drop_connection()


def _within_ttl() -> bool:
    return _raw is not None and (time.time() - _stored_at) < REALTIME_CACHE_SECONDS


def _start_worker_locked(url: str) -> None:
    """Start the single background download. The caller holds `_cache_lock`."""
    global _worker, _worker_error
    if _worker is not None and _worker.is_alive():
        return
    _worker_error = ""
    _worker = threading.Thread(target=_fill_cache, args=(url,), daemon=True)
    _worker.start()


def _fill_cache(url: str) -> None:
    global _raw, _stored_at, _etag, _modified, _generation, _parsed, _scanned, _feed_timestamp
    global _worker_error
    me = threading.current_thread()
    with _cache_lock:
        etag = _etag
        modified = _modified
    try:
        result = _download(url, etag, modified)
    except TypeError:
        try:
            result = _download(url)
        except Exception as exc:
            _note_failure(me, exc)
            return
    except Exception as exc:
        _note_failure(me, exc)
        return
    kind, data, new_etag, new_modified = _normalize_download(result)
    with _cache_lock:
        if me is not _worker:
            return
        if kind == "not_modified":
            if _raw is not None:
                _stored_at = time.time()
                if new_etag:
                    _etag = new_etag
                if new_modified:
                    _modified = new_modified
        elif data is not None:
            _raw = data
            _stored_at = time.time()
            _etag = new_etag or ""
            _modified = new_modified or ""
            _generation += 1
            _parsed = {}
            _scanned = set()
            _feed_timestamp = None
        _worker_error = ""


def _note_failure(me, exc: Exception) -> None:
    global _worker_error
    message = "timed out" if isinstance(exc, (socket.timeout, TimeoutError)) else "fetch failed"
    with _cache_lock:
        if me is not _worker:
            return
        _worker_error = message


def _normalize_download(result):
    """Bytes from a test double, or the (`kind`, body, etag, modified) tuple."""
    if isinstance(result, (bytes, bytearray)):
        return "ok", bytes(result), "", ""
    if not isinstance(result, tuple) or not result:
        raise RuntimeError("realtime download returned nothing")
    if result[0] == "not_modified":
        etag = result[1] if len(result) > 1 else ""
        modified = result[2] if len(result) > 2 else ""
        return "not_modified", None, etag or "", modified or ""
    if result[0] == "ok":
        return "ok", result[1], result[2] if len(result) > 2 else "", result[3] if len(result) > 3 else ""
    raise RuntimeError("realtime download returned an unknown result")


def _acquire_feed(url: str, deadline: float):
    """`cached`, `fresh`, or None when the deadline passes with no body.

    The loop sleeps instead of waiting on the download's lock. A socket call
    on the worker cannot hold this thread past the deadline.
    """
    with _cache_lock:
        before = _generation
        if _raw is not None:
            if not _within_ttl():
                _start_worker_locked(url)
            return "cached"
        if time.monotonic() >= deadline:
            _start_worker_locked(url)
            return None
        _start_worker_locked(url)
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if _cache_lock.acquire(timeout=min(0.02, remaining)):
            try:
                if _raw is not None:
                    return "fresh" if _generation != before else "cached"
                worker = _worker
                failed = bool(_worker_error) and (worker is None or not worker.is_alive())
                if failed:
                    return None
            finally:
                _cache_lock.release()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.02, remaining))
    if _cache_lock.acquire(timeout=0.02):
        try:
            if _raw is not None:
                return "fresh" if _generation != before else "cached"
        finally:
            _cache_lock.release()
    return None


def _trips_from_cache(wanted: set[str]) -> ParsedFeed:
    """Trips for `wanted`, scanning the cached protobuf only for new ids."""
    global _feed_timestamp
    with _cache_lock:
        raw = _raw
        if raw is None:
            return ParsedFeed(timestamp=None, trips={}, error="timed out")
        if wanted <= _scanned:
            trips = {trip_id: _parsed[trip_id] for trip_id in wanted if trip_id in _parsed}
            return ParsedFeed(timestamp=_feed_timestamp, trips=trips)
        scanned = set(_scanned)
    missing = wanted - scanned
    feed = parse_feed(raw, missing)
    with _cache_lock:
        if _raw is not raw:
            return feed
        _parsed.update(feed.trips)
        _scanned.update(missing)
        if feed.timestamp is not None:
            _feed_timestamp = feed.timestamp
        trips = {trip_id: _parsed[trip_id] for trip_id in wanted if trip_id in _parsed}
        return ParsedFeed(timestamp=_feed_timestamp, trips=trips)


def decompress_if_needed(raw: bytes, content_encoding: str | None = None) -> bytes:
    if content_encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def _download(url: str, etag: str = "", modified: str = ""):
    """GET the feed on a reused TLS connection. One retry covers a dead socket.

    The socket timeout is an inactivity limit on this background thread. It
    is not the handler's 1.5 s cap. A validator turns an unchanged file into
    a 304 with an empty body.
    """
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
    if etag:
        headers["If-None-Match"] = etag
    elif modified:
        headers["If-Modified-Since"] = modified
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
                new_etag = response.getheader("ETag") or ""
                new_modified = response.getheader("Last-Modified") or ""
                if status == 304:
                    return ("not_modified", new_etag or etag, new_modified or modified)
                if status != 200:
                    _drop_connection()
                    raise RuntimeError("realtime feed status %s" % status)
                if len(raw) > _MAX_BYTES:
                    _drop_connection()
                    raise RuntimeError("realtime feed is larger than 20 MB")
            return ("ok", decompress_if_needed(raw, encoding), new_etag, new_modified)
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


def _peek_trip_id(view: memoryview, start: int, end: int) -> str:
    """The trip id in the descriptor, without walking that trip's stops.

    The live feed puts the descriptor first. Unwanted trips are almost the
    entire 5 MB, and their stop list is what made a warm parse take seconds.
    """
    index = start
    while index < end:
        tag, index = _varint(view, index, end)
        field_number = tag >> 3
        wire = tag & 7
        if wire == 0:
            _, index = _varint(view, index, end)
            continue
        if wire != 2:
            index = _skip(view, index, end, wire)
            continue
        length, index = _varint(view, index, end)
        if field_number == 1:
            return _trip_id_only(view, index, index + length)
        index += length
    return ""


def _trip_id_only(view: memoryview, start: int, end: int) -> str:
    index = start
    while index < end:
        tag, index = _varint(view, index, end)
        field_number = tag >> 3
        wire = tag & 7
        if wire == 0:
            _, index = _varint(view, index, end)
            continue
        if wire != 2:
            index = _skip(view, index, end, wire)
            continue
        length, index = _varint(view, index, end)
        if field_number == 1:
            return bytes(view[index : index + length]).decode("utf-8", "replace")
        index += length
    return ""


def _trip_update(
    view: memoryview,
    start: int,
    end: int,
    wanted: set[str],
    keep_all: bool,
) -> tuple[str, TripUpdate] | None:
    if not keep_all and _peek_trip_id(view, start, end) not in wanted:
        return None
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

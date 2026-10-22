import gzip
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from upcoming_trains.realtime import (
    decompress_if_needed,
    fetch_feed,
    parse_feed,
    reset_for_tests,
    updates_for_board,
)
from upcoming_trains.timing import add, begin, emit, mark_fallback, set_chars, set_realtime

UTC = timezone.utc


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _blob(field: int, payload: bytes) -> bytes:
    return _key(field, 2) + _varint(len(payload)) + payload


def _svar(field: int, value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    return _key(field, 0) + _varint(value)


def _text(field: int, text: str) -> bytes:
    return _blob(field, text.encode())


def _feed() -> bytes:
    header = _text(1, "2.0") + _svar(3, 1_700_000_000)
    descriptor = _text(1, "T1") + _text(3, "20261021") + _svar(4, 0)
    departure = _svar(1, -90) + _svar(2, 1_700_000_600)
    stop = _svar(1, 3) + _blob(3, departure)
    skipped = _svar(1, 4) + _svar(5, 1)
    trip = _blob(1, descriptor) + _blob(2, stop) + _blob(2, skipped) + _svar(5, 120)
    entity = _text(1, "e1") + _blob(3, trip)
    other = _text(1, "e2") + _blob(3, _blob(1, _text(1, "T2") + _svar(4, 3)))
    return _blob(1, header) + _blob(2, entity) + _blob(2, other)


def test_parser_keeps_wanted_trips_and_signed_delay():
    feed = parse_feed(_feed(), {"T1"})
    assert feed.timestamp == 1_700_000_000
    assert set(feed.trips) == {"T1"}
    trip = feed.trips["T1"]
    assert trip.start_date == "20261021"
    assert trip.delay == 120
    assert trip.stops[3].departure_delay == -90
    assert trip.stops[3].departure_unix == 1_700_000_600
    assert trip.stops[4].skipped
    canceled = parse_feed(_feed(), {"T2"}).trips["T2"]
    assert canceled.cancelled
    assert canceled.stops == {}


def test_gzip_wrapper_and_freshness():
    raw = gzip.compress(_feed())
    assert decompress_if_needed(raw, "gzip")[:1] == _feed()[:1]
    now = datetime.fromtimestamp(1_700_000_000, UTC)
    feed = parse_feed(_feed(), set())
    assert feed.is_fresh(now)
    assert not feed.is_fresh(now + timedelta(hours=2))
    updates, note = updates_for_board(feed, now + timedelta(hours=2))
    assert updates is None
    assert "frozen" in note
    updates, note = updates_for_board(feed, now)
    assert note == ""
    assert "T1" in updates


def test_a_late_feed_becomes_scheduled_times(monkeypatch):
    reset_for_tests()
    monkeypatch.setattr("upcoming_trains.realtime.REALTIME_WAIT_SECONDS", 0.2)
    release = threading.Event()

    def slow(url):
        release.wait(2)
        raise socket.timeout("timed out")

    monkeypatch.setattr("upcoming_trains.realtime._download", slow)
    started = time.perf_counter()
    try:
        feed = fetch_feed({"T1"})
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0
        assert feed.error == "timed out"
        updates, note = updates_for_board(feed, datetime.now(UTC))
        assert updates is None
        assert "scheduled" in note
    finally:
        release.set()
        reset_for_tests()


def test_a_download_past_the_cap_is_kept_for_the_next_request(monkeypatch):
    """The handler stops at the cap. The same download may finish afterwards."""
    reset_for_tests()
    monkeypatch.setattr("upcoming_trains.realtime.REALTIME_WAIT_SECONDS", 0.15)
    release = threading.Event()
    calls = []

    def slow(url, etag="", modified=""):
        calls.append(1)
        release.wait(2)
        return _feed()

    monkeypatch.setattr("upcoming_trains.realtime._download", slow)
    try:
        started = time.perf_counter()
        first = fetch_feed({"T1"})
        assert time.perf_counter() - started < 0.6
        assert first.error == "timed out"
        assert calls == [1]
        release.set()
        second = None
        gave_up = time.perf_counter() + 2
        while time.perf_counter() < gave_up:
            second = fetch_feed({"T2"})
            if second.error is None:
                break
            time.sleep(0.02)
        assert second is not None and second.error is None
        assert calls == [1]
        assert set(second.trips) == {"T2"}
        assert second.timestamp == 1_700_000_000
    finally:
        release.set()
        reset_for_tests()


def test_an_in_flight_download_is_not_started_again(monkeypatch):
    reset_for_tests()
    monkeypatch.setattr("upcoming_trains.realtime.REALTIME_WAIT_SECONDS", 0.15)
    release = threading.Event()
    calls = []

    def slow(url, etag="", modified=""):
        calls.append(1)
        release.wait(2)
        return _feed()

    monkeypatch.setattr("upcoming_trains.realtime._download", slow)
    try:
        assert fetch_feed({"T1"}).error == "timed out"
        assert fetch_feed({"T2"}).error == "timed out"
        assert calls == [1]
    finally:
        release.set()
        reset_for_tests()


def test_the_cap_is_the_handler_clock_not_the_socket(monkeypatch):
    """A deadline already near expiry does not wait out a blocked connect."""
    reset_for_tests()
    release = threading.Event()

    def slow(url, etag="", modified=""):
        release.wait(2)
        return _feed()

    monkeypatch.setattr("upcoming_trains.realtime._download", slow)
    try:
        started = time.perf_counter()
        feed = fetch_feed({"T1"}, deadline=time.monotonic() + 0.12)
        assert time.perf_counter() - started < 0.5
        assert feed.error == "timed out"
        updates, note = updates_for_board(feed, datetime.now(UTC))
        assert updates is None
        assert "scheduled" in note
    finally:
        release.set()
        reset_for_tests()


def test_a_conditional_refresh_does_not_block_on_the_old_body(monkeypatch):
    reset_for_tests()
    calls = []
    release = threading.Event()

    def download(url, etag="", modified=""):
        calls.append(etag)
        if etag:
            release.wait(2)
            return ("not_modified", etag, modified)
        return ("ok", _feed(), '"feed-etag"', "Thu, 22 Oct 2026 09:56:00 GMT")

    monkeypatch.setattr("upcoming_trains.realtime._download", download)
    try:
        first = fetch_feed({"T1"})
        assert set(first.trips) == {"T1"}
        import upcoming_trains.realtime as realtime

        with realtime._cache_lock:
            realtime._stored_at = time.time() - realtime.REALTIME_CACHE_SECONDS - 5
        started = time.perf_counter()
        second = fetch_feed({"T2"})
        assert time.perf_counter() - started < 0.3
        assert second.error is None
        assert set(second.trips) == {"T2"}
        assert calls[0] == ""
        gave_up = time.perf_counter() + 1
        while time.perf_counter() < gave_up and len(calls) < 2:
            time.sleep(0.02)
        assert len(calls) == 2
        assert calls[1]
    finally:
        release.set()
        reset_for_tests()


def test_cached_feed_bytes_are_reused_for_about_thirty_seconds(monkeypatch):
    reset_for_tests()
    calls = []

    def once(url):
        calls.append(url)
        return _feed()

    monkeypatch.setattr("upcoming_trains.realtime._download", once)
    try:
        first = fetch_feed({"T1"})
        second = fetch_feed({"T2"})
        assert calls == [calls[0]]
        assert set(first.trips) == {"T1"}
        assert set(second.trips) == {"T2"}
        assert first.timestamp == second.timestamp == 1_700_000_000
    finally:
        reset_for_tests()


def test_timing_line_lists_the_stages_without_a_secret(capsys):
    begin(40, 12)
    add("realtime_ms", 80)
    set_realtime("cached")
    add("board_ms", 3)
    mark_fallback()
    emit()
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("timing ")
    for field in (
        "cold=",
        "import_ms=40",
        "schedule_ms=12",
        "realtime_ms=80",
        "realtime=cached",
        "board_ms=3",
        "cache_ms=0",
        "eleven_ms=0",
        "s3_put_ms=0",
        "total_ms=",
        "fallback=1",
        "fb=error",
        "chars=0",
    ):
        assert field in line
    assert "xi-api-key" not in line
    assert "access_token" not in line
    assert " " not in line.split("timing ", 1)[1].split("cold=")[0]
    begin(0, 0)
    mark_fallback("eleven_http_401")
    mark_fallback("no_key")
    set_chars(180)
    emit()
    specific = capsys.readouterr().out.strip().splitlines()[-1]
    assert "fb=eleven_http_401" in specific
    assert "chars=180" in specific
    assert "fallback=1" in specific
    assert " " not in specific.split("timing ", 1)[1].split("cold=")[0]
    begin(0, 0)
    mark_fallback("Bearer sk-secret")
    emit()
    dropped = capsys.readouterr().out.strip().splitlines()[-1]
    assert "fb=error" in dropped
    assert "secret" not in dropped
    assert "Bearer" not in dropped

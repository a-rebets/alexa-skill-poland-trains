from datetime import date, datetime, timedelta

from upcoming_trains.schedule import NO_STATION, dump, load
from upcoming_trains.timeutil import warsaw_wall_to_utc


def _index(tmp_path):
    """One station, two trips, service active only on 2026-10-21.

    T1 leaves at 19:30. T2 leaves at 24:40, which is 00:40 the next morning
    and still belongs to the October 21 service.
    """
    feed_start = date(2026, 10, 21)
    feed_end = date(2026, 10, 22)
    strings = [
        "33605",
        "Warszawa Centralna",
        "T1",
        "Kraków Główny",
        "EIP",
        "PKP Intercity",
        "100",
        "T2",
        "Gdynia Główna",
        "IC",
        "PKP Intercity",
        "",
    ]
    services = [bytes([1, 0])]
    trips = [
        (2, 3, 4, 5, 6, 0, 0),
        (7, 8, 9, 10, 11, 0, 0),
    ]
    events = [
        (19 * 3600 + 30 * 60, 19 * 3600 + 28 * 60, 2, 1, 0, NO_STATION, NO_STATION),
        (24 * 3600 + 40 * 60, 24 * 3600 + 40 * 60, 4, 1, 1, NO_STATION, NO_STATION),
    ]
    stations = [(0, 1, 0, 2)]
    path = tmp_path / "schedule.bin"
    dump(
        str(path),
        feed_start=feed_start,
        feed_end=feed_end,
        built_unix=1_700_000_000,
        strings=strings,
        services=services,
        stations=stations,
        trips=trips,
        events=events,
    )
    return load(str(path))


def test_round_trip_and_hour_window(tmp_path):
    schedule = _index(tmp_path)
    assert schedule.station_name("33605") == "Warszawa Centralna"
    assert schedule.covers(date(2026, 10, 21))
    assert not schedule.covers(date(2026, 10, 23))
    now = warsaw_wall_to_utc(datetime(2026, 10, 21, 19, 0))
    found = schedule.candidates("33605", now)
    in_hour = [item for item in found if now <= item.scheduled <= now + timedelta(hours=1)]
    assert [item.trip_id for item in in_hour] == ["T1"]
    assert in_hour[0].headsign == "Kraków Główny"


def test_gtfs_time_past_midnight_is_in_the_next_hour(tmp_path):
    schedule = _index(tmp_path)
    late = warsaw_wall_to_utc(datetime(2026, 10, 21, 23, 50))
    found = [item.trip_id for item in schedule.candidates("33605", late)]
    assert found == ["T2"]
    # Just after midnight the same train still belongs to yesterday's service.
    after = warsaw_wall_to_utc(datetime(2026, 10, 22, 0, 10))
    again = schedule.candidates("33605", after)
    assert [item.trip_id for item in again] == ["T2"]
    assert again[0].service_day == date(2026, 10, 21)
    assert again[0].scheduled == warsaw_wall_to_utc(datetime(2026, 10, 22, 0, 40))


def test_next_stop_and_previous_stop(tmp_path):
    """A --10:00--> B --10:20--> C. Departures at B point at C, arrivals at A."""
    feed_start = date(2026, 10, 21)
    strings = [
        "A",
        "Alpha",
        "B",
        "Beta",
        "C",
        "Gamma",
        "T",
        "Head",
        "R1",
        "KM",
        "1",
    ]
    services = [bytes([1])]
    trips = [(6, 7, 8, 9, 10, 0, 0)]
    events = [
        (10 * 3600, 10 * 3600, 1, 1, 0, 1, NO_STATION),
        (10 * 3600 + 20 * 60, 10 * 3600 + 18 * 60, 2, 3, 0, 2, 0),
        (10 * 3600 + 40 * 60, 10 * 3600 + 40 * 60, 3, 2, 0, NO_STATION, 1),
    ]
    stations = [(0, 1, 0, 1), (2, 3, 1, 1), (4, 5, 2, 1)]
    path = tmp_path / "neighbors.bin"
    dump(
        str(path),
        feed_start=feed_start,
        feed_end=feed_start,
        built_unix=1_700_000_000,
        strings=strings,
        services=services,
        stations=stations,
        trips=trips,
        events=events,
    )
    schedule = load(str(path))
    now = warsaw_wall_to_utc(datetime(2026, 10, 21, 10, 0))
    departures = schedule.candidates("B", now, "departure")
    assert [item.direction for item in departures] == ["Gamma"]
    assert departures[0].scheduled == warsaw_wall_to_utc(datetime(2026, 10, 21, 10, 20))
    arrivals = schedule.candidates("B", now, "arrival")
    assert [item.direction for item in arrivals] == ["Alpha"]
    assert arrivals[0].scheduled == warsaw_wall_to_utc(datetime(2026, 10, 21, 10, 18))
    assert schedule.candidates("A", now, "arrival") == []
    assert schedule.candidates("C", now, "departure") == []
    assert schedule.candidates("A", now, "departure")[0].direction == "Beta"


def test_platform_alias_uses_the_parent_events(tmp_path):
    feed_start = date(2026, 10, 21)
    strings = ["33506", "Warszawa Zachodnia", "34868", "T", "Head", "S4", "SKM", "1"]
    services = [bytes([1])]
    trips = [(3, 4, 5, 6, 7, 0, 0)]
    events = [(8 * 3600, 8 * 3600, 2, 1, 0, NO_STATION, NO_STATION)]
    stations = [(0, 1, 0, 1)]
    path = tmp_path / "alias.bin"
    dump(
        str(path),
        feed_start=feed_start,
        feed_end=feed_start,
        built_unix=1,
        strings=strings,
        services=services,
        stations=stations,
        trips=trips,
        events=events,
        aliases=[(2, 0)],
    )
    schedule = load(str(path))
    assert schedule.canonical("34868") == "33506"
    assert schedule.station_name("34868") == "Warszawa Zachodnia"
    now = warsaw_wall_to_utc(datetime(2026, 10, 21, 7, 30))
    assert [item.route for item in schedule.candidates("34868", now)] == ["S4"]
    assert [item.station_id for item in schedule.candidates("34868", now)] == ["33506"]


def test_sample_feed_contains_warsaw_central(sample_feed):
    path = sample_feed["schedule"]
    schedule = load(str(path))
    assert schedule.station_name("33605") == "Warszawa Centralna"
    assert schedule.station_name("33506") == "Warszawa Zachodnia"
    assert schedule.canonical("34868") == "33506"
    assert schedule.station_name("34868") == "Warszawa Zachodnia"
    picker = (sample_feed["picker"] / "stations.js").read_text(encoding="utf-8")
    assert '"id":"34868"' not in picker
    assert '"id":"33506"' in picker
    assert schedule.station_name("33548") == "Warszawa Ursus Północny"
    assert schedule.feed_end >= date(2026, 11, 20)
    assert schedule.covers(schedule.feed_start)
    assert path.stat().st_size < 1024 * 1024
    now = warsaw_wall_to_utc(datetime(2026, 10, 21, 16, 0))
    departures = schedule.candidates("33548", now, "departure")
    arrivals = schedule.candidates("33548", now, "arrival")
    assert departures
    assert arrivals
    assert any(item.direction for item in departures)
    assert any(item.direction for item in arrivals)

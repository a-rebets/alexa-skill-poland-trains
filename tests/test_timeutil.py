from datetime import date, datetime, timezone

from upcoming_trains.timeutil import (
    clock_hhmm,
    gtfs_clock_to_utc,
    gtfs_clock_to_wall,
    is_cest,
    last_sunday,
    offset_hours_for_wall,
    utc_to_warsaw_wall,
    warsaw_wall_to_utc,
)

UTC = timezone.utc


def test_last_sunday_of_october_2026():
    assert last_sunday(2026, 10) == date(2026, 10, 25)
    assert last_sunday(2026, 10).weekday() == 6


def test_summer_and_winter_offsets():
    summer_noon = warsaw_wall_to_utc(datetime(2026, 10, 21, 12, 0))
    assert summer_noon == datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    assert is_cest(summer_noon)
    winter_noon = warsaw_wall_to_utc(datetime(2026, 11, 1, 12, 0))
    assert winter_noon == datetime(2026, 11, 1, 11, 0, tzinfo=UTC)
    assert not is_cest(winter_noon)
    assert utc_to_warsaw_wall(summer_noon) == datetime(2026, 10, 21, 12, 0)


def test_october_overlap_fold():
    wall = datetime(2026, 10, 25, 2, 30)
    first = warsaw_wall_to_utc(wall, fold=0)
    second = warsaw_wall_to_utc(wall, fold=1)
    assert offset_hours_for_wall(wall, fold=0) == 2
    assert offset_hours_for_wall(wall, fold=1) == 1
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert second == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert utc_to_warsaw_wall(first) == wall
    assert utc_to_warsaw_wall(second) == wall


def test_gtfs_times_past_24_and_past_48():
    service = date(2026, 10, 21)
    past_midnight = gtfs_clock_to_wall(service, 24 * 3600 + 40 * 60)
    assert past_midnight == datetime(2026, 10, 22, 0, 40)
    as_utc = gtfs_clock_to_utc(service, 24 * 3600 + 40 * 60)
    assert as_utc == datetime(2026, 10, 21, 22, 40, tzinfo=UTC)
    long_trip = gtfs_clock_to_wall(service, 41 * 3600 + 15 * 60)
    assert long_trip == datetime(2026, 10, 22, 17, 15)
    assert clock_hhmm(long_trip) == "17:15"

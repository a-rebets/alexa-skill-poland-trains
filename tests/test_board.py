"""The next-60-minutes decision, including delays and times past midnight."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from upcoming_trains.board import build_board
from upcoming_trains.realtime import StopUpdate, TripUpdate
from upcoming_trains.schedule import Candidate

UTC = timezone.utc
NOW = datetime(2026, 10, 21, 17, 0, tzinfo=UTC)  # 19:00 Warsaw, still CEST


def _candidate(
    minutes_from_now: int,
    trip_id: str = "T1",
    seq: int = 2,
    service=None,
    number: str = "100",
    direction: str = "Kraków Główny",
) -> Candidate:
    return Candidate(
        station_id="33605",
        station_name="Warszawa Centralna",
        trip_id=trip_id,
        headsign="Kraków Główny",
        route="EIP",
        agency="PKP Intercity",
        number=number,
        bus=False,
        stop_sequence=seq,
        service_day=service or date(2026, 10, 21),
        scheduled=NOW + timedelta(minutes=minutes_from_now),
        direction=direction,
    )


def _boards(candidates, updates):
    return build_board([("33605", "Warszawa Centralna")], candidates, updates, NOW)


def test_scheduled_train_inside_and_outside_the_hour():
    boards = _boards([_candidate(30), _candidate(70, "T2")], None)
    assert len(boards[0].trains) == 1
    assert boards[0].trains[0].number == "100"
    assert boards[0].trains[0].delay_seconds is None


def test_delay_pulls_a_past_train_into_the_hour():
    # Scheduled 30 minutes ago, but running 40 minutes late: leaves in 10 minutes.
    cand = _candidate(-30)
    predicted = int((NOW + timedelta(minutes=10)).timestamp())
    updates = {
        "T1": TripUpdate(
            start_date="20261021",
            stops={2: StopUpdate(departure_unix=predicted, departure_delay=0)},
        )
    }
    trains = _boards([cand], updates)[0].trains
    assert len(trains) == 1
    assert trains[0].delay_seconds == 40 * 60
    assert trains[0].live


def test_absolute_time_wins_over_a_conflicting_delay_field():
    cand = _candidate(10)
    predicted = int((NOW + timedelta(minutes=20)).timestamp())
    updates = {
        "T1": TripUpdate(
            start_date="20261021",
            stops={2: StopUpdate(departure_unix=predicted, departure_delay=0)},
        )
    }
    train = _boards([cand], updates)[0].trains[0]
    assert train.delay_seconds == 10 * 60


def test_early_train_already_gone_is_dropped():
    cand = _candidate(20)
    predicted = int((NOW - timedelta(minutes=10)).timestamp())
    updates = {
        "T1": TripUpdate(
            start_date="20261021",
            stops={2: StopUpdate(departure_unix=predicted)},
        )
    }
    assert _boards([cand], updates)[0].trains == []


def test_stale_or_missing_feed_does_not_invent_a_delay():
    past = _candidate(-30)
    assert _boards([past], None)[0].trains == []
    assert _boards([past], {})[0].trains == []


def test_cancelled_and_skipped():
    cancelled = _candidate(20, "C")
    skipped = _candidate(25, "S", seq=5)
    updates = {
        "C": TripUpdate(cancelled=True, start_date="20261021"),
        "S": TripUpdate(
            start_date="20261021",
            stops={5: StopUpdate(skipped=True)},
        ),
    }
    trains = _boards([cancelled, skipped], updates)[0].trains
    assert [train.headsign for train in trains] == ["Kraków Główny"]
    assert trains[0].cancelled


def test_update_for_a_different_service_date_is_ignored():
    cand = _candidate(20)
    predicted = int((NOW + timedelta(minutes=50)).timestamp())
    updates = {
        "T1": TripUpdate(
            start_date="20261020",
            stops={2: StopUpdate(departure_unix=predicted)},
        )
    }
    train = _boards([cand], updates)[0].trains[0]
    assert train.live is False
    assert train.delay_seconds is None


def test_trip_level_delay_when_the_stop_has_no_event():
    cand = _candidate(20)
    updates = {"T1": TripUpdate(start_date="20261021", delay=600, stops={})}
    train = _boards([cand], updates)[0].trains[0]
    assert train.delay_seconds == 600
    assert train.predicted == cand.scheduled + timedelta(minutes=10)


def test_unknown_station_is_reported():
    boards = build_board([("999", None)], [], {}, NOW)
    assert boards[0].known is False


def test_predicted_time_is_what_the_hour_uses():
    # Scheduled inside the hour, but the live delay pushes it past 60 minutes.
    pushed_out = _candidate(50, "L", number="L")
    # Scheduled outside the spoken hour, but running early enough to enter it.
    pulled_in = _candidate(70, "E", number="E")
    updates = {
        "L": TripUpdate(start_date="20261021", delay=20 * 60),
        "E": TripUpdate(start_date="20261021", delay=-20 * 60),
    }
    trains = _boards([pushed_out, pulled_in], updates)[0].trains
    assert [train.number for train in trains] == ["E"]
    assert trains[0].predicted == NOW + timedelta(minutes=50)
    assert trains[0].direction == "Kraków Główny"


def test_arrival_uses_the_arrival_time():
    cand = _candidate(10, direction="Warszawa Włochy")
    arrival = int((NOW + timedelta(minutes=4)).timestamp())
    departure = int((NOW + timedelta(minutes=40)).timestamp())
    updates = {
        "T1": TripUpdate(
            start_date="20261021",
            stops={2: StopUpdate(arrival_unix=arrival, departure_unix=departure)},
        )
    }
    arrived = build_board(
        [("33605", "Warszawa Centralna")],
        [cand],
        updates,
        NOW,
        kind="arrival",
    )[0].trains
    assert len(arrived) == 1
    assert arrived[0].predicted == datetime.fromtimestamp(arrival, UTC)
    assert arrived[0].direction == "Warszawa Włochy"
    left = _boards([cand], updates)[0].trains
    assert left[0].predicted == datetime.fromtimestamp(departure, UTC)


def test_the_hour_is_strictly_under_sixty_minutes():
    assert _boards([_candidate(60)], None)[0].trains == []
    kept = _boards([_candidate(59)], None)[0].trains
    assert len(kept) == 1
    assert kept[0].predicted == NOW + timedelta(minutes=59)


def test_named_hour_is_a_strict_sixty_minutes_without_grace():
    start = NOW + timedelta(hours=10)
    inside = replace(_candidate(0), scheduled=start)
    on_the_hour = replace(_candidate(0, "T2", number="200"), scheduled=start + timedelta(minutes=60))
    just_before = replace(
        _candidate(0, "T3", number="300"), scheduled=start - timedelta(seconds=20)
    )
    boards = build_board(
        [("33605", "Warszawa Centralna")],
        [inside, on_the_hour, just_before],
        None,
        NOW,
        window_start=start,
    )
    assert [train.number for train in boards[0].trains] == ["100"]


def test_the_board_keeps_more_than_four_trains():
    candidates = [_candidate(5 + index, "T%d" % index, number=str(index)) for index in range(6)]
    board = _boards(candidates, None)[0]
    assert len(board.trains) == 6
    assert board.extra == 0
    assert board.trains[0].direction == "Kraków Główny"

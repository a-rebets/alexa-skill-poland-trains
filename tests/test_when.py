"""Named hours, the next occurrence of a clock, and CanFulfill."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import lambda_function
from upcoming_trains.answer import answer
from upcoming_trains.board import StationBoard
from upcoming_trains.schedule import load
from upcoming_trains.speech import (
    SKILL_NAME,
    apl_directive,
    change_message,
    help_message,
    render,
    unavailable_message,
)
from upcoming_trains.timeutil import utc_to_warsaw_wall, warsaw_wall_to_utc
from upcoming_trains.token import encode_choices
from upcoming_trains.when import WhenError, resolve_when

from test_speech import _train, validate_alexa_ssml

UTC = timezone.utc
# 23:00 Warsaw on 22 October 2026 is 21:00 UTC. CEST is still in force.
ASKED_AT = warsaw_wall_to_utc(datetime(2026, 10, 22, 23, 0))
EIGHT_AM_NEXT = warsaw_wall_to_utc(datetime(2026, 10, 23, 8, 0))


def _at(hour, minute, day=22):
    return warsaw_wall_to_utc(datetime(2026, 10, day, hour, minute))


def test_eight_am_asked_at_23_00_is_tomorrow():
    assert resolve_when(ASKED_AT, "08:00", "") == EIGHT_AM_NEXT
    assert resolve_when(ASKED_AT, "08:00:00", "") == EIGHT_AM_NEXT
    assert resolve_when(ASKED_AT, "MO", "") == EIGHT_AM_NEXT


def test_parts_of_day_and_a_clock_within_five_minutes():
    assert resolve_when(_at(10, 0), "AF", "") == _at(13, 0)
    assert resolve_when(_at(10, 0), "EV", "") == _at(18, 0)
    assert resolve_when(_at(20, 0), "NI", "") == _at(21, 0)
    assert resolve_when(_at(21, 4), "21:00", "") == _at(21, 0)
    assert resolve_when(_at(21, 6), "21:00", "") == _at(21, 0, day=23)
    assert resolve_when(_at(8, 3), "08:00", "") == _at(8, 0)
    assert resolve_when(_at(8, 5), "08:00", "") == _at(8, 0)
    assert resolve_when(_at(8, 6), "08:00", "") == _at(8, 0, day=23)


def test_midnight_next_occurrence():
    assert resolve_when(_at(0, 2, day=23), "00:00", "") == _at(0, 0, day=23)
    assert resolve_when(_at(0, 3, day=23), "00:00", "") == _at(0, 0, day=23)
    assert resolve_when(_at(0, 6, day=23), "00:00", "") == _at(0, 0, day=24)
    assert resolve_when(_at(0, 2, day=23), "23:50", "") == _at(23, 50, day=23)
    assert resolve_when(_at(23, 58), "00:10", "") == _at(0, 10, day=23)


def test_a_given_date_is_that_day_even_when_the_clock_has_passed():
    pinned = resolve_when(ASKED_AT, "08:00", "2026-10-22")
    assert pinned == _at(8, 0)
    assert resolve_when(ASKED_AT, "08:00", "PRESENT_REF") == _at(8, 0)
    assert resolve_when(ASKED_AT, "", "2026-10-23") == _at(23, 0, day=23)
    assert resolve_when(ASKED_AT, "", "") is None
    try:
        resolve_when(ASKED_AT, "morning", "")
    except WhenError as exc:
        assert "time" in str(exc).lower()
    else:
        raise AssertionError("expected a time error")
    try:
        resolve_when(ASKED_AT, "08:00", "2026-W43")
    except WhenError as exc:
        assert "day" in str(exc).lower()
    else:
        raise AssertionError("expected a date error")


def test_named_hour_speech_uses_clock_words_and_the_same_cap():
    window = EIGHT_AM_NEXT
    def at(route, minute, number):
        predicted = window + timedelta(minutes=minute)
        return _train(
            route=route,
            direction="Warszawa Włochy",
            predicted=predicted,
            scheduled=predicted,
            number=number,
        )

    crowded = render(
        [
            StationBoard(
                "33548",
                "Warszawa Ursus Północny",
                True,
                [at("R3", 5, "1"), at("S1", 12, "2"), at("RL", 20, "3")],
                0,
            )
        ],
        "",
        ASKED_AT,
        window_start=window,
    )
    assert crowded.plain.startswith("Departures from eight am tomorrow.")
    assert "Ursus Północny station." in crowded.plain
    assert "R three at eight oh five" in crowded.plain
    assert "S one at eight twelve" in crowded.plain
    assert "R L at eight twenty" in crowded.plain
    assert "minutes" not in crowded.plain
    assert crowded.apl_title == "Departures from 8 am tomorrow"
    assert crowded.apl_body.startswith("Departures from 8 am tomorrow.")
    validate_alexa_ssml(crowded.ssml)
    assert "say-as" not in crowded.ssml

    paired = render(
        [
            StationBoard(
                "33548",
                "Warszawa Ursus Północny",
                True,
                [at("R3", 5, "1"), at("R3", 35, "2"), at("S1", 10, "3"), at("S1", 40, "4")],
                0,
            )
        ],
        "",
        ASKED_AT,
        window_start=window,
    )
    assert "R three at eight oh five and at eight thirty-five" in paired.plain
    assert "S one at eight ten and at eight forty" in paired.plain

    today = render(
        [StationBoard("33548", "Warszawa Ursus Północny", True, [at("R3", 5, "1")], 0)],
        "",
        window + timedelta(minutes=1),
        window_start=window,
    )
    assert today.plain.startswith("Departures from eight am today.")

    later = render(
        [StationBoard("1", "Warszawa Centralna", True, [], 0)],
        "",
        ASKED_AT,
        window_start=warsaw_wall_to_utc(datetime(2026, 10, 24, 13, 5)),
    )
    assert later.plain.startswith("Departures from one oh five pm on October twenty-fourth.")
    assert "No trains in that hour." in later.plain

    arrivals = render(
        [StationBoard("33548", "Warszawa Ursus Północny", True, [at("R3", 0, "1")], 0)],
        "",
        ASKED_AT,
        kind="arrival",
        window_start=window,
    )
    assert arrivals.plain.startswith("Arrivals from eight am tomorrow.")
    assert "R three at eight" in arrivals.plain
    assert "am" not in arrivals.plain.split(".", 1)[1]

    relative = render(
        [StationBoard("33548", "Warszawa Ursus Północny", True, [at("R3", 5, "1")], 0)],
        "",
        window,
    )
    assert "in five minutes" in relative.plain
    assert "eight oh five" not in relative.plain


def test_feed_dates_include_midnight_and_stop_after_the_last_day(sample_feed):
    schedule = load(str(sample_feed["schedule"]))
    assert schedule.feed_start == date(2026, 10, 21)
    assert schedule.feed_end == date(2026, 11, 20)
    when = warsaw_wall_to_utc(datetime(2026, 10, 23, 0, 30))
    crossed = []
    for stop_id in ("33506", "33605", "33548", "33530"):
        for item in schedule.candidates(stop_id, when, "departure", when=when):
            wall = utc_to_warsaw_wall(item.scheduled)
            assert schedule.covers(item.service_day)
            if item.service_day < wall.date():
                crossed.append((stop_id, item.service_day, wall))
    assert crossed, "a 00:30 window should include a previous service day's 24:xx trip"
    last = warsaw_wall_to_utc(datetime(2026, 11, 20, 8, 0))
    assert schedule.candidates("33506", last, "departure", when=last)
    beyond = warsaw_wall_to_utc(datetime(2026, 11, 21, 8, 0))
    assert schedule.candidates("33506", beyond, "departure", when=beyond) == []
    text = unavailable_message(schedule.feed_start, schedule.feed_end, date(2026, 11, 21))
    assert "isn't available yet" in text
    assert "November 20, 2026" in text
    before = unavailable_message(schedule.feed_start, schedule.feed_end, date(2026, 10, 1))
    assert "isn't available yet" not in before
    assert "October 1, 2026" in before


def test_direction_filter_still_applies_inside_a_named_hour(sample_feed):
    schedule = load(str(sample_feed["schedule"]))
    full, status = answer(schedule, ["33548"], now=ASKED_AT, fetch=False, window_start=EIGHT_AM_NEXT)
    assert status["fetched"] is False
    neighbours = sorted(schedule.local_neighbour_ids("33548"))
    assert neighbours
    narrow, _status = answer(
        schedule,
        [("33548", (neighbours[0],))],
        now=ASKED_AT,
        fetch=False,
        window_start=EIGHT_AM_NEXT,
    )
    assert full.plain.startswith("Departures from eight am tomorrow.")
    assert narrow.plain.startswith("Departures from eight am tomorrow.")
    assert narrow.plain != full.plain


def test_live_delays_only_when_the_window_starts_within_an_hour(monkeypatch, sample_feed):
    calls = []

    class Feed:
        error = None
        timestamp = None

        def is_fresh(self, now):
            return True

        def age_seconds(self, now):
            return 1

    def fetch(wanted, deadline=None):
        calls.append(len(wanted))
        return Feed()

    def updates(feed, now):
        return {}, ""

    monkeypatch.setattr("upcoming_trains.answer.fetch_feed", fetch)
    monkeypatch.setattr("upcoming_trains.answer.prefetch", lambda: None)
    monkeypatch.setattr("upcoming_trains.answer.updates_for_board", updates)
    schedule = load(str(sample_feed["schedule"]))
    answer(schedule, ["33548"], now=ASKED_AT, fetch=True, window_start=EIGHT_AM_NEXT)
    assert calls == []
    near = EIGHT_AM_NEXT - timedelta(minutes=30)
    answer(schedule, ["33548"], now=near, fetch=True, window_start=EIGHT_AM_NEXT)
    assert calls and calls[0] > 0


def test_can_fulfill_yes_for_the_two_train_intents_and_no_otherwise(monkeypatch):
    def explode():
        raise AssertionError("can-fulfill must not load the timetable")

    monkeypatch.setattr(lambda_function, "get_schedule", explode)

    def ask(name, slots=None):
        intent = {"name": name}
        if slots is not None:
            intent["slots"] = slots
        return lambda_function.handle(
            {"request": {"type": "CanFulfillIntentRequest", "intent": intent}}
        )

    both = {
        "time": {"name": "time", "value": "08:00"},
        "date": {"name": "date", "value": "2026-10-23"},
    }
    for name in ("DeparturesIntent", "ArrivalsIntent"):
        bare = ask(name)
        filled = ask(name, both)
        time_only = ask(name, {"time": {"name": "time", "value": "08:00"}})
        for response in (bare, filled, time_only):
            assert "outputSpeech" not in response["response"]
            assert response["response"]["canFulfillIntent"]["canFulfill"] == "YES"
        assert "slots" not in bare["response"]["canFulfillIntent"]
        slots = filled["response"]["canFulfillIntent"]["slots"]
        assert slots["time"] == {"canUnderstand": "YES", "canFulfill": "YES"}
        assert slots["date"] == {"canUnderstand": "YES", "canFulfill": "YES"}
        assert time_only["response"]["canFulfillIntent"]["slots"] == {
            "time": {"canUnderstand": "YES", "canFulfill": "YES"}
        }
    for name in ("ChangeStationsIntent", "AMAZON.HelpIntent", "UnknownIntent", "UpcomingTrainsIntent"):
        response = ask(name)
        assert response["response"]["canFulfillIntent"] == {"canFulfill": "NO"}
        assert "outputSpeech" not in response["response"]


def _load_sample(monkeypatch, sample_feed):
    monkeypatch.setenv("SCHEDULE_PATH", str(sample_feed["schedule"]))
    monkeypatch.setattr(lambda_function, "_SCHEDULE", None)


def test_a_date_outside_the_feed_and_an_expired_today(monkeypatch, sample_feed):
    _load_sample(monkeypatch, sample_feed)
    token = encode_choices([("33548", None)])

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 21, 8, 0, tzinfo=UTC)

    monkeypatch.setattr(lambda_function, "datetime", Later)
    expired = lambda_function.handle(
        {
            "request": {"type": "LaunchRequest"},
            "context": {"System": {"user": {"accessToken": token}}},
        }
    )
    speech = expired["response"]["outputSpeech"]["ssml"]
    assert "Rebuild" in speech
    assert "isn't available yet" not in speech

    class During(datetime):
        @classmethod
        def now(cls, tz=None):
            return ASKED_AT

    monkeypatch.setattr(lambda_function, "datetime", During)
    future = lambda_function.handle(
        {
            "request": {
                "type": "IntentRequest",
                "intent": {
                    "name": "DeparturesIntent",
                    "slots": {
                        "time": {"name": "time", "value": "08:00"},
                        "date": {"name": "date", "value": "2026-12-01"},
                    },
                },
            },
            "context": {"System": {"user": {"accessToken": token}}},
        }
    )
    text = future["response"]["outputSpeech"]["ssml"]
    assert "isn't available yet" in text
    assert "November 20, 2026" in text
    validate_alexa_ssml(text)


def test_handler_passes_the_next_occurrence_into_the_answer(monkeypatch, sample_feed):
    _load_sample(monkeypatch, sample_feed)
    seen = {}

    def fake_answer(*args, **kwargs):
        seen["window"] = kwargs.get("window_start")
        seen["kind"] = kwargs.get("kind")
        from upcoming_trains.speech import Spoken

        return Spoken("Departures from eight am tomorrow.", "<speak>ok</speak>", "body", "Departures from 8 am tomorrow"), {}

    class During(datetime):
        @classmethod
        def now(cls, tz=None):
            return ASKED_AT

    monkeypatch.setattr(lambda_function, "datetime", During)
    monkeypatch.setattr(lambda_function, "answer", fake_answer)
    token = encode_choices([("33548", None)])
    response = lambda_function.handle(
        {
            "request": {
                "type": "IntentRequest",
                "intent": {
                    "name": "DeparturesIntent",
                    "slots": {"time": {"name": "time", "value": "08:00"}},
                },
            },
            "context": {"System": {"user": {"accessToken": token}}},
        }
    )
    assert seen["window"] == EIGHT_AM_NEXT
    assert seen["kind"] == "departure"
    assert response["response"]["outputSpeech"]["ssml"] == "<speak>ok</speak>"


def test_models_manifest_and_picker_use_the_new_name():
    us = json.loads((ROOT / "skill-package/interactionModels/custom/en-US.json").read_text())
    gb = json.loads((ROOT / "skill-package/interactionModels/custom/en-GB.json").read_text())
    assert us == gb
    language = us["interactionModel"]["languageModel"]
    assert language["invocationName"] == "my train schedule"
    intents = {item["name"]: item for item in language["intents"]}
    assert set(intents) == {
        "AMAZON.CancelIntent",
        "AMAZON.HelpIntent",
        "AMAZON.StopIntent",
        "AMAZON.NavigateHomeIntent",
        "AMAZON.FallbackIntent",
        "DeparturesIntent",
        "ArrivalsIntent",
        "ChangeStationsIntent",
    }
    for name in ("DeparturesIntent", "ArrivalsIntent"):
        slots = {slot["name"]: slot["type"] for slot in intents[name]["slots"]}
        assert slots == {"time": "AMAZON.TIME", "date": "AMAZON.DATE"}
    departures = intents["DeparturesIntent"]["samples"]
    arrivals = intents["ArrivalsIntent"]["samples"]
    for sample in (
        "departures",
        "departing trains",
        "departing trains at {time}",
        "departures at {time}",
        "trains leaving at {time}",
        "departures {date} at {time}",
        "trains at {time}",
    ):
        assert sample in departures
    for sample in (
        "arrivals",
        "arriving trains",
        "arriving trains at {time}",
        "arrivals at {time}",
        "arrivals {date} at {time}",
    ):
        assert sample in arrivals

    manifest = json.loads((ROOT / "skill-package/skill.json").read_text())["manifest"]
    custom = manifest["apis"]["custom"]
    assert custom["endpoint"]["uri"].endswith("HOSTED_ENDPOINT_UNKNOWN")
    assert custom["regions"]["NA"]["endpoint"]["uri"].endswith("HOSTED_ENDPOINT_UNKNOWN")
    assert custom["regions"]["EU"]["endpoint"]["uri"].endswith("HOSTED_ENDPOINT_UNKNOWN")
    assert custom["regions"]["FE"]["endpoint"]["uri"].endswith("HOSTED_ENDPOINT_UNKNOWN")
    assert [item["type"] for item in custom["interfaces"]] == [
        "ALEXA_PRESENTATION_APL",
        "CAN_FULFILL_INTENT_REQUEST",
    ]
    for locale, icon in (("en-US", "en-US"), ("en-GB", "en-GB")):
        publishing = manifest["publishingInformation"]["locales"][locale]
        assert publishing["name"] == "My Train Schedule"
        assert publishing["examplePhrases"] == [
            "Alexa, open my train schedule",
            "Alexa, ask my train schedule for arrivals",
            "Alexa, ask my train schedule for departures at 8 am",
        ]
        assert "currently supports only Poland" in publishing["description"]
        assert publishing["smallIconUri"] == "file://assets/images/%s_smallIcon.png" % icon
        assert publishing["largeIconUri"] == "file://assets/images/%s_largeIcon.png" % icon
    assert "Poland Train Tracker" not in json.dumps(manifest["publishingInformation"])

    page = (ROOT / "picker/index.html").read_text(encoding="utf-8")
    privacy = (ROOT / "picker/privacy.html").read_text(encoding="utf-8")
    script = (ROOT / "picker/picker.js").read_text(encoding="utf-8")
    assert "My Train Schedule" in page and "Poland Train Tracker" not in page
    assert 'aria-label="My Train Schedule"' in page
    assert 'aria-label="My Train Schedule"' in privacy
    assert "?v=8" in page and "?v=7" not in page
    assert "?v=8" in privacy
    terms = (ROOT / "picker/terms.html").read_text(encoding="utf-8")
    assert 'aria-label="My Train Schedule"' in terms and "?v=8" in terms
    assert 'href="terms.html"' in page and 'href="terms.html"' in privacy
    assert 'CACHE = "8"' in script
    assert SKILL_NAME == "My Train Schedule"
    assert "My Train Schedule" in change_message()
    assert "Poland Train Tracker" not in change_message()
    assert "My Train Schedule" in help_message()
    assert "change" not in help_message().lower()
    document = apl_directive("Departures", "Ursus Północny")
    assert document["document"]["mainTemplate"]["items"][0]["items"][0]["text"] == "My Train Schedule"

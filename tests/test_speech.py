import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import lambda_function
import upcoming_trains.speech as speech
from upcoming_trains.board import StationBoard, Train
from upcoming_trains.speech import (
    ALEXA_LANGS,
    ALEXA_VOICES,
    POLISH_LANG,
    POLISH_VOICE,
    change_message,
    expired_message,
    fallback_message,
    help_message,
    line_label,
    link_message,
    english_number,
    local_name,
    station_city,
    plain_ssml,
    render,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 21, 17, 0, tzinfo=UTC)
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _train(**kwargs) -> Train:
    predicted = kwargs.pop("predicted", NOW + timedelta(minutes=12))
    defaults = dict(
        headsign="Kraków Główny",
        route="R3",
        agency="Koleje Mazowieckie",
        number="100",
        bus=False,
        direction="Warszawa Włochy",
        direction_id="",
        scheduled=predicted,
        predicted=predicted,
        delay_seconds=None,
        cancelled=False,
        live=False,
    )
    defaults.update(kwargs)
    return Train(**defaults)


def test_city_prefix_is_stripped_only_for_the_same_city():
    assert station_city("Warszawa Zachodnia") == "Warszawa"
    assert station_city("Gdynia") == "Gdynia"
    assert station_city("Grodzisk Mazowiecki") == ""
    assert local_name("Warszawa Centralna", "Warszawa") == "Centralna"
    assert local_name("Warszawa Ursus Północny", "Warszawa") == "Ursus Północny"
    assert local_name("Warszawa Włochy", "Warszawa") == "Włochy"
    assert local_name("Warszawa Ursus Niedźwiadek", "Warszawa") == "Ursus Niedźwiadek"
    assert local_name("Kraków Główny", "Kraków") == "Główny"
    assert local_name("Kraków Główny", "Warszawa") == "Kraków Główny"
    assert local_name("Poznań Główny", "Warszawa") == "Poznań Główny"
    assert local_name("Poznań Główny", "Poznań") == "Główny"
    assert local_name("Łódź Fabryczna", "Łódź") == "Fabryczna"
    assert local_name("Wrocław Główny", "Wrocław") == "Główny"
    assert local_name("Gdańsk Główny", "Gdańsk") == "Główny"
    assert local_name("Gdynia Główna", "Gdynia") == "Główna"
    assert local_name("Sopot Wyścigi", "Sopot") == "Wyścigi"
    assert local_name("Sopot Wyścigi", "Gdynia") == "Sopot Wyścigi"
    assert local_name("Szczecin Główny", "Szczecin") == "Główny"
    assert local_name("Katowice", "Katowice") == "Katowice"
    assert local_name("Lublin Główny", "Lublin") == "Główny"
    assert local_name("Bydgoszcz Główna", "Bydgoszcz") == "Główna"
    assert local_name("Gdynia", "Gdynia") == "Gdynia"
    assert local_name("Warszawa", "Warszawa") == "Warszawa"
    assert local_name("Stacja Warszawa Centralna", "Warszawa") == "Stacja Warszawa Centralna"


def test_line_codes_and_fallbacks():
    assert english_number(2) == "two"
    assert english_number(33) == "thirty-three"
    assert english_number(40) == "forty"
    assert english_number(25) == "twenty-five"
    assert line_label("R3", "Koleje Mazowieckie", False) == ("R3", "R three")
    assert line_label("R2", "Koleje Mazowieckie", False) == ("R2", "R two")
    assert line_label("RL", "Koleje Mazowieckie", False) == ("RL", "R L")
    assert line_label("S1", "SKM Warszawa", False) == ("S1", "S one")
    assert line_label("S40", "SKM Warszawa", False) == ("S40", "S forty")
    assert line_label("SKA1", "Koleje Małopolskie", False) == ("SKA1", "S K A one")
    assert line_label("RE1", "Koleje Mazowieckie", False) == ("RE1", "R E one")
    assert line_label("K5P", "Koleje Małopolskie", False) == ("K5P", "K five P")
    assert line_label("Ł", "Łódzka Kolej Aglomeracyjna", False) == ("Ł", "L")
    assert line_label("ŁP", "Łódzka Kolej Aglomeracyjna", False) == ("ŁP", "L P")
    assert line_label("KŚ", "Koleje Śląskie", False) == ("KŚ", "K S")
    assert line_label("D01", "Koleje Dolnośląskie", False) == ("D01", "D one")
    for code in ("IC", "EIP", "EIC", "EC", "TLK", "ICN", "MP", "EN"):
        assert line_label(code, "PKP Intercity", False) == ("Intercity", "Intercity")
    assert line_label("REG", "PolRegio", False) == ("regional", "regional")
    assert line_label("REG P", "PolRegio", False) == ("regional", "regional")
    assert line_label("IR", "PolRegio", False) == ("regional", "regional")
    assert line_label("", "PKP Intercity", False) == ("Intercity", "Intercity")
    assert line_label("KD Sprinter", "Koleje Dolnośląskie", False) == ("Sprinter", "Sprinter")
    assert line_label("KW P", "Koleje Wielkopolskie", False) == ("regional", "regional")
    assert line_label("", "Leo Express", False) == ("Leo", "Leo")
    assert line_label("", "RegioJet", False) == ("RegioJet", "RegioJet")
    assert line_label("", "Arriva RP", False) == ("Arriva", "Arriva")
    assert line_label("ZKA R3", "Koleje Mazowieckie", True) == ("R3", "R three bus")
    assert line_label("R3", "Koleje Mazowieckie", True) == ("R3", "R three bus")
    assert line_label("ZKA IC", "PKP Intercity", True) == ("bus", "bus")
    assert line_label("ZKA", "PolRegio", True) == ("bus", "bus")


def test_ursus_polnocny_groups_by_next_stop_and_merges_times():
    def at(route, direction, minutes, number="1"):
        return _train(
            route=route,
            direction="Warszawa " + direction,
            predicted=NOW + timedelta(minutes=minutes),
            number=number,
        )

    north = [
        at("R1", "Włochy", 55),
        at("R3", "Włochy", 5),
        at("R3", "Gołąbki", 20, "2"),
        at("R1", "Gołąbki", 50),
        at("R3", "Gołąbki", 15, "3"),
    ]
    ursus = [
        at("R3", "Włochy", 8, "4"),
        at("R1", "Niedźwiadek", 12, "5"),
    ]
    spoken = render(
        [
            StationBoard("33548", "Warszawa Ursus Północny", True, north, 0),
            StationBoard("33530", "Warszawa Ursus", True, ursus, 0),
        ],
        "",
        NOW,
    )
    assert spoken.plain == (
        "Ursus Północny station. "
        "Towards Włochy: R three in five minutes, R one in fifty-five minutes. "
        "Towards Gołąbki: R three in fifteen and in twenty minutes, R one in fifty minutes. "
        "Ursus station. "
        "Towards Włochy: R three in eight minutes. "
        "Towards Niedźwiadek: R one in twelve minutes."
    )
    assert "19:05" not in spoken.plain
    assert "R3 19:05" in spoken.apl_body
    assert "R1 19:55" in spoken.apl_body
    assert "R3 19:15, 19:20" in spoken.apl_body
    assert "<voice" not in spoken.ssml
    assert "say-as" not in spoken.ssml
    validate_alexa_ssml(spoken.ssml)


def test_zachodnia_keeps_local_lines_and_one_intercity_line():
    def at(route, agency, direction, minutes, number):
        return _train(
            route=route,
            agency=agency,
            direction=direction,
            predicted=NOW + timedelta(minutes=minutes),
            number=number,
        )

    ochota = ["R1", "R2", "R7", "RE2", "S1", "R3", "S2"]
    trains = []
    for index, route in enumerate(ochota):
        agency = "SKM Warszawa" if route.startswith("S") else "Koleje Mazowieckie"
        trains.append(at(route, agency, "Warszawa Ochota", 4 + index, "o" + str(index)))
    trains.append(at("S2", "SKM Warszawa", "Warszawa Aleje Jerozolimskie", 6, "s2b"))
    centralna = ["RL", "RE3", "S3", "R8", "RE1"]
    for index, route in enumerate(centralna):
        agency = "SKM Warszawa" if route.startswith("S") else "Koleje Mazowieckie"
        trains.append(at(route, agency, "Warszawa Centralna", 10 + index, "c" + str(index)))
    trains.append(at("EIP", "PKP Intercity", "Kraków Główny", 5, "ic1"))
    trains.append(at("EIC", "PKP Intercity", "Poznań Główny", 13, "ic2"))
    trains.append(at("IC", "PKP Intercity", "Kielce Główne", 40, "ic3"))
    trains.append(at("IC", "PKP Intercity", "Sochaczew", 50, "ic4"))
    trains.append(at("RJ", "RegioJet", "Kraków Główny", 22, "rj"))
    spoken = render([StationBoard("33506", "Warszawa Zachodnia", True, trains, 0)], "", NOW)
    assert spoken.plain.count("Intercity") == 1
    assert "Towards Główny" not in spoken.plain
    assert "Kraków Główny" not in spoken.plain
    assert "Poznań Główny" not in spoken.plain
    assert "Towards Kraków" not in spoken.plain
    assert "Towards Poznań" not in spoken.plain
    assert "Intercity in five and in thirteen minutes." in spoken.plain
    assert "forty" not in spoken.plain
    assert "fifty" not in spoken.plain
    assert "RegioJet in twenty-two minutes." in spoken.plain
    parts = spoken.plain.split(". ")
    ochota_line = [part for part in parts if part.startswith("Towards Ochota")][0]
    aleje_line = [part for part in parts if part.startswith("Towards Aleje Jerozolimskie")][0]
    central_line = [part for part in parts if part.startswith("Towards Centralna")][0]
    assert "S two" in ochota_line
    assert "S one" in ochota_line
    assert "R three" in ochota_line
    assert "S two" in aleje_line
    assert "R E one" in central_line
    assert spoken.plain.index("Towards") < spoken.plain.index("Intercity")
    assert "Towards Główny" not in spoken.apl_body
    assert "Intercity 19:05, 19:13" in spoken.apl_body
    assert "19:40" not in spoken.apl_body
    assert "S2" in spoken.apl_body
    validate_alexa_ssml(spoken.ssml)

    arriving = [
        at("S2", "SKM Warszawa", "Warszawa Ochota", 4, "a1"),
        at("S2", "SKM Warszawa", "Warszawa Lotnisko Chopina", 9, "a2"),
        at("EIP", "PKP Intercity", "Kraków Główny", 11, "a3"),
    ]
    arrivals = render(
        [StationBoard("33506", "Warszawa Zachodnia", True, arriving, 0)],
        "",
        NOW,
        kind="arrival",
    )
    assert "From Ochota:" in arrivals.plain and "S two" in arrivals.plain.split("From Lotnisko")[0]
    assert "From Lotnisko Chopina:" in arrivals.plain
    assert "Warszawa Lotnisko" not in arrivals.plain
    assert "S two" in arrivals.plain.split("From Lotnisko Chopina:")[1]
    assert arrivals.plain.count("Intercity") == 1
    assert "Towards" not in arrivals.plain
    assert "From Główny" not in arrivals.plain


def test_minutes_are_floored_and_a_crowded_direction_keeps_every_line_once():
    almost = _train(route="S2", predicted=NOW + timedelta(minutes=4, seconds=30))
    spoken = render([StationBoard("1", "Warszawa Zachodnia", True, [almost], 0)], "", NOW)
    assert "in four minutes" in spoken.plain
    assert "five" not in spoken.plain
    trains = []
    for index in range(9):
        trains.append(
            _train(
                route="R%d" % (index + 1),
                direction="Warszawa Ochota",
                predicted=NOW + timedelta(minutes=3 + index),
                number=str(index),
            )
        )
        trains.append(
            _train(
                route="R%d" % (index + 1),
                direction="Warszawa Ochota",
                predicted=NOW + timedelta(minutes=30 + index),
                number="b" + str(index),
            )
        )
    crowded = render([StationBoard("1", "Warszawa Zachodnia", True, trains, 0)], "", NOW)
    assert "R nine" in crowded.plain
    assert "R one in three minutes" in crowded.plain
    ninth = crowded.plain.split("R nine")[1]
    assert "in eleven minutes" in ninth
    assert "thirty" not in crowded.plain
    assert "thirty-eight" not in ninth


def test_two_lines_keep_two_times_and_a_third_line_drops_to_one():
    pair = [
        _train(route="R3", predicted=NOW + timedelta(minutes=5), number="1"),
        _train(route="R3", predicted=NOW + timedelta(minutes=15), number="2"),
        _train(route="R3", predicted=NOW + timedelta(minutes=20), number="3"),
        _train(route="R1", predicted=NOW + timedelta(minutes=8), number="4"),
        _train(route="R1", predicted=NOW + timedelta(minutes=40), number="5"),
    ]
    quiet = render([StationBoard("1", "Warszawa Włochy", True, pair, 0)], "", NOW)
    assert "R three in five and in fifteen minutes" in quiet.plain
    assert "R one in eight and in forty minutes" in quiet.plain
    assert "twenty" not in quiet.plain
    busy = pair + [
        _train(route="S1", predicted=NOW + timedelta(minutes=9), number="6"),
    ]
    crowded = render([StationBoard("1", "Warszawa Włochy", True, busy, 0)], "", NOW)
    assert "R three in five minutes" in crowded.plain
    assert "fifteen" not in crowded.plain
    assert "R one in eight minutes" in crowded.plain
    assert "S one in nine minutes" in crowded.plain
    assert "forty" not in crowded.plain


def test_long_distance_follows_the_same_line_cap():
    def at(route, agency, minutes, number):
        return _train(
            route=route,
            agency=agency,
            direction="Kraków Główny",
            predicted=NOW + timedelta(minutes=minutes),
            number=number,
        )

    one_brand = [
        at("EIP", "PKP Intercity", 5, "1"),
        at("EIC", "PKP Intercity", 13, "2"),
        at("IC", "PKP Intercity", 40, "3"),
    ]
    short = render([StationBoard("1", "Warszawa Zachodnia", True, one_brand, 0)], "", NOW)
    assert short.plain.endswith("Intercity in five and in thirteen minutes.")
    assert "forty" not in short.plain
    brands = one_brand + [
        at("RJ", "RegioJet", 8, "4"),
        at("RJ", "RegioJet", 28, "5"),
        at("", "Leo Express", 11, "6"),
        at("", "Leo Express", 31, "7"),
    ]
    long = render([StationBoard("1", "Warszawa Zachodnia", True, brands, 0)], "", NOW)
    assert "Intercity in five minutes." in long.plain
    assert "RegioJet in eight minutes." in long.plain
    assert "Leo in eleven minutes." in long.plain
    assert "thirteen" not in long.plain
    assert "twenty-eight" not in long.plain
    assert "thirty-one" not in long.plain


def test_three_times_merge_and_caps_and_cancellation():
    trains = [
        _train(route="R3", predicted=NOW + timedelta(minutes=5), number="1"),
        _train(route="R3", predicted=NOW + timedelta(minutes=15), number="2"),
        _train(route="R3", predicted=NOW + timedelta(minutes=20), number="3"),
        _train(route="R3", predicted=NOW + timedelta(minutes=40), number="4"),
        _train(route="R1", predicted=NOW + timedelta(minutes=6), cancelled=True, live=True, number="5"),
        _train(route="S1", predicted=NOW + timedelta(minutes=7), number="6"),
        _train(route="RL", predicted=NOW + timedelta(minutes=8), number="7"),
        _train(route="S2", predicted=NOW + timedelta(minutes=9), number="8"),
        _train(route="RE1", predicted=NOW + timedelta(minutes=30), number="9"),
    ]
    spoken = render([StationBoard("1", "Warszawa Włochy", True, trains, 0)], "", NOW)
    assert "R three in five minutes" in spoken.plain
    assert "fifteen" not in spoken.plain
    assert "twenty" not in spoken.plain
    assert "forty" not in spoken.plain
    assert "R one cancelled" in spoken.plain
    assert "R E one" in spoken.plain
    assert "S two" in spoken.plain
    assert "on time" not in spoken.plain
    assert "late" not in spoken.plain
    assert "And 1 more" not in spoken.plain


def test_delay_is_already_in_the_minute_count():
    delayed = _train(
        route="R1",
        scheduled=NOW - timedelta(minutes=10),
        predicted=NOW + timedelta(minutes=5),
        delay_seconds=15 * 60,
        live=True,
        number="3510",
    )
    spoken = render([StationBoard("1", "Warszawa Centralna", True, [delayed], 0)], "", NOW)
    assert spoken.plain == "Centralna station. Towards Włochy: R one in five minutes."
    assert "3510" not in spoken.plain
    assert "late" not in spoken.plain
    assert "19:05" in spoken.apl_body
    assert "19:05" not in spoken.plain


def test_now_singular_and_arrivals_and_empty():
    soon = _train(predicted=NOW + timedelta(seconds=20), route="S1")
    later = _train(predicted=NOW + timedelta(minutes=5), route="S1", number="2")
    spoken = render([StationBoard("1", "Gdynia", True, [soon, later], 0)], "", NOW)
    assert spoken.plain == (
        "Gdynia station. Towards Warszawa Włochy: S one \u2014 now and in five minutes."
    )
    assert 'S one <break time="150ms"/> now and in five minutes' in spoken.ssml
    assert "\u2014" not in spoken.ssml
    assert "\u2014" not in spoken.apl_body
    assert "now" not in spoken.apl_body
    validate_alexa_ssml(spoken.ssml)
    lone = render(
        [StationBoard("1", "Gdynia", True, [_train(predicted=NOW + timedelta(seconds=20), route="S1")], 0)],
        "",
        NOW,
    )
    assert lone.plain == "Gdynia station. Towards Warszawa Włochy: S one \u2014 now."
    assert 'S one <break time="150ms"/> now.' in lone.ssml
    arriving_now = render(
        [
            StationBoard(
                "33548",
                "Warszawa Ursus Północny",
                True,
                [_train(predicted=NOW + timedelta(seconds=20))],
                0,
            )
        ],
        "",
        NOW,
        kind="arrival",
    )
    assert arriving_now.plain == "Ursus Północny station. From Włochy: R three \u2014 now."
    assert 'R three <break time="150ms"/> now.' in arriving_now.ssml
    intercity = render(
        [
            StationBoard(
                "1",
                "Warszawa Centralna",
                True,
                [
                    _train(
                        route="EIP",
                        agency="PKP Intercity",
                        predicted=NOW + timedelta(seconds=10),
                        number="9",
                    )
                ],
                0,
            )
        ],
        "",
        NOW,
    )
    assert intercity.plain == "Centralna station. Intercity \u2014 now."
    assert 'Intercity <break time="150ms"/> now.' in intercity.ssml
    assert "\u2014" not in intercity.ssml
    named = render(
        [StationBoard("1", "Gdynia", True, [soon, later], 0)],
        "",
        NOW,
        window_start=NOW,
    )
    assert "\u2014" not in named.plain
    assert " now" not in named.plain
    assert "150ms" not in named.ssml
    assert "at seven" in named.plain
    one = render(
        [StationBoard("1", "Gdynia", True, [_train(predicted=NOW + timedelta(seconds=70), route="S1")], 0)],
        "",
        NOW,
    )
    assert "in one minute." in one.plain
    assert "\u2014" not in one.plain
    assert "150ms" not in one.ssml
    arriving = render(
        [StationBoard("33548", "Warszawa Ursus Północny", True, [_train(predicted=NOW + timedelta(minutes=4))], 0)],
        "",
        NOW,
        kind="arrival",
    )
    assert arriving.plain == "Ursus Północny station. From Włochy: R three in four minutes."
    assert "R3 19:04" in arriving.apl_body
    quiet = render([StationBoard("33548", "Warszawa Ursus Północny", True, [], 0)], "", NOW)
    assert quiet.plain == "Ursus Północny station. No trains in the next hour."
    missing = render([StationBoard("999", None, False, [], 0)], "", NOW)
    assert missing.plain == "I don't have that station in this timetable."
    assert "999" not in missing.plain
    nowhere = render(
        [StationBoard("1", "Ursus", True, [_train(direction="")], 0)],
        "",
        NOW,
    )
    assert "Towards the next stop:" in nowhere.plain
    arrived = render(
        [StationBoard("1", "Ursus", True, [_train(direction="")], 0)],
        "",
        NOW,
        kind="arrival",
    )
    assert "From the previous stop:" in arrived.plain


def test_station_order_follows_the_token_not_the_clock():
    later = StationBoard(
        "1",
        "Warszawa Centralna",
        True,
        [_train(predicted=NOW + timedelta(minutes=50), route="EIP", agency="PKP Intercity")],
        0,
    )
    sooner = StationBoard(
        "2",
        "Warszawa Włochy",
        True,
        [_train(predicted=NOW + timedelta(minutes=1))],
        0,
    )
    spoken = render([later, sooner], "", NOW)
    assert spoken.plain.index("Centralna station") < spoken.plain.index("Włochy station")
    assert "Intercity in fifty minutes" in spoken.plain
    assert "Towards Włochy: Intercity" not in spoken.plain
    assert "EIP" not in spoken.plain


def test_polish_voice_stays_off_and_rejects_an_unsupported_voice(monkeypatch):
    assert POLISH_VOICE == ""
    assert "Ewa" not in ALEXA_VOICES
    assert "Maja" not in ALEXA_VOICES
    assert "Jacek" not in ALEXA_VOICES
    assert "Jan" not in ALEXA_VOICES
    assert "pl-PL" not in ALEXA_LANGS
    assert POLISH_LANG == "pl-PL"
    spoken = render([StationBoard("1", "Warszawa Włochy", True, [_train()], 0)], "", NOW)
    assert "<voice" not in spoken.ssml
    monkeypatch.setattr(speech, "POLISH_VOICE", "Ewa")
    monkeypatch.setattr(speech, "POLISH_LANG", "pl-PL")
    blocked = render([StationBoard("1", "Warszawa Włochy", True, [_train()], 0)], "", NOW)
    assert "<voice" not in blocked.ssml
    validate_alexa_ssml(blocked.ssml)
    monkeypatch.setattr(speech, "POLISH_VOICE", "Celine")
    monkeypatch.setattr(speech, "POLISH_LANG", "fr-FR")
    wrapped = render(
        [StationBoard("1", "Warszawa Włochy", True, [_train(direction="")], 0)],
        "",
        NOW,
    )
    assert '<voice name="Celine"><lang xml:lang="fr-FR">Włochy</lang></voice>' in wrapped.ssml
    assert "R three" in wrapped.ssml
    voice = ET.fromstring(wrapped.ssml).find("voice")
    assert voice is not None
    assert "R three" not in "".join(voice.itertext())
    assert "the next stop" in wrapped.ssml
    assert "the next stop" not in "".join(voice.itertext())
    validate_alexa_ssml(wrapped.ssml)


def test_escapes_names():
    spoken = render(
        [StationBoard("1", 'A & B <C>', True, [_train(direction='D "E"')], 1)],
        "",
        NOW,
    )
    assert "A & B <C>" in spoken.plain
    assert "A &amp; B &lt;C&gt;" in spoken.ssml
    assert "D &quot;E&quot;" in spoken.ssml
    assert "And 1 more" not in spoken.plain
    assert "&amp;" in plain_ssml("A & B")
    assert "&quot;" in plain_ssml('say "hello"')
    assert "&lt;" in plain_ssml("1 < 2")
    assert "&gt;" in plain_ssml("2 > 1")


def test_help_mentions_only_the_two_commands():
    text = help_message()
    assert "departures" in text
    assert "arrivals" in text
    assert "change" not in text.lower()
    assert "Kuranowski" not in text
    assert "departures" in fallback_message()
    assert "arrivals" in fallback_message()


def test_every_produced_ssml_is_alexa_valid(monkeypatch):
    tricky = 'Łódź "Główny" & <Wschód>'
    documents = [
        render([StationBoard("1", "Warszawa Centralna", True, [], 0)], "", NOW).ssml,
        render(
            [StationBoard("1", "Warszawa Centralna", True, [_train()], 0)],
            "",
            NOW,
        ).ssml,
        render(
            [
                StationBoard(
                    "1",
                    tricky,
                    True,
                    [
                        _train(cancelled=True, direction=tricky),
                        _train(
                            direction=tricky,
                            predicted=NOW + timedelta(minutes=20),
                            route="EIP",
                            agency="PKP Intercity",
                            number="301",
                        ),
                        _train(bus=True, route="ZKA R1", direction="Płock"),
                    ],
                    0,
                )
            ],
            "Live updates look frozen, so these are scheduled times.",
            NOW,
        ).ssml,
        render([StationBoard("999", None, False, [], 0)], "", NOW).ssml,
        render([], "", NOW).ssml,
        render(
            [StationBoard("1", "Centralna", True, [_train()], 0)],
            "I couldn't fetch live updates, so these are scheduled times.",
            NOW,
            kind="arrival",
        ).ssml,
        plain_ssml(link_message()),
        plain_ssml(change_message()),
        plain_ssml(help_message()),
        plain_ssml(fallback_message()),
        plain_ssml(expired_message(date(2026, 10, 21), date(2026, 11, 20), date(2026, 12, 1))),
        plain_ssml("Sorry, I couldn't read the timetable. Please try again in a moment."),
        plain_ssml("Goodbye."),
        plain_ssml("A & B < C > D \"E\" 'F'\x00"),
    ]
    unlinked = {"request": {"type": "LaunchRequest"}, "context": {}}
    documents.append(lambda_function.handle(unlinked)["response"]["outputSpeech"]["ssml"])
    for name in (
        "AMAZON.HelpIntent",
        "ChangeStationsIntent",
        "AMAZON.StopIntent",
        "AMAZON.FallbackIntent",
        "UnknownIntent",
        "ArrivalsIntent",
        "DeparturesIntent",
    ):
        documents.append(
            lambda_function.handle(
                {"request": {"type": "IntentRequest", "intent": {"name": name}}}
            )["response"]["outputSpeech"]["ssml"]
        )

    def explode(event):
        raise RuntimeError("boom")

    monkeypatch.setattr(lambda_function, "handle", explode)
    documents.append(lambda_function.lambda_handler({}, None)["response"]["outputSpeech"]["ssml"])

    tricky_ssml = documents[2]
    assert "&quot;Główny&quot; &amp; &lt;Wschód&gt;" in tricky_ssml
    assert "Łódź" not in tricky_ssml
    assert "say-as" not in tricky_ssml
    assert documents
    for ssml in documents:
        validate_alexa_ssml(ssml)
        assert "hms24" not in ssml
        assert 'name="Ewa"' not in ssml
        assert "pl-PL" not in ssml


def test_validator_rejects_unsupported_say_as_voice_and_lang():
    rejected = [
        '<speak><say-as interpret-as="time" format="hms24">14:05</say-as></speak>',
        '<speak><say-as interpret-as="time" format="hms12">2:05pm</say-as></speak>',
        '<speak><say-as interpret-as="currency">$5</say-as></speak>',
        '<speak><say-as interpret-as="date" format="hms24">20261021</say-as></speak>',
        "<speak><say-as>14:05</say-as></speak>",
        "<speak><not-a-tag>hello</not-a-tag></speak>",
        "From Warszawa at 14:05.",
        '<speak><voice name="Ewa">Cześć</voice></speak>',
        '<speak><lang xml:lang="pl-PL">Cześć</lang></speak>',
        '<speak><break time="250ms" foo="1"/></speak>',
        '<speak><break time="11s"/></speak>',
        '<speak><voice name="Joanna" lang="en-US">hi</voice></speak>',
    ]
    for ssml in rejected:
        try:
            validate_alexa_ssml(ssml)
        except ValueError:
            continue
        raise AssertionError("accepted invalid SSML: " + ssml)

    validate_alexa_ssml(
        '<speak><say-as interpret-as="date" format="mdy">10/21/2026</say-as></speak>'
    )
    validate_alexa_ssml('<speak><say-as interpret-as="time">1\'21"</say-as></speak>')
    validate_alexa_ssml('<speak>R 3 <break time="250ms"/></speak>')
    validate_alexa_ssml('<speak><voice name="Joanna">hello</voice></speak>')
    validate_alexa_ssml(
        '<speak><voice name="Celine"><lang xml:lang="fr-FR">Bonjour</lang></voice></speak>'
    )


# Alexa SSML reference, updated 28 August 2025.
_SAY_AS = {
    "characters",
    "spell-out",
    "cardinal",
    "number",
    "ordinal",
    "digits",
    "fraction",
    "unit",
    "date",
    "time",
    "telephone",
    "address",
    "interjection",
    "expletive",
}
_DATE_FORMATS = {"mdy", "dmy", "ymd", "md", "dm", "ym", "my", "d", "m", "y"}
_TAGS = {
    "amazon:domain",
    "amazon:effect",
    "amazon:emotion",
    "audio",
    "break",
    "emphasis",
    "lang",
    "p",
    "phoneme",
    "prosody",
    "s",
    "say-as",
    "speak",
    "sub",
    "voice",
    "w",
}
_ATTRS = {
    "amazon:domain": {"name"},
    "amazon:effect": {"name"},
    "amazon:emotion": {"name", "intensity"},
    "audio": {"src"},
    "break": {"time", "strength"},
    "emphasis": {"level"},
    "lang": {"xml:lang"},
    "p": set(),
    "phoneme": {"alphabet", "ph"},
    "prosody": {"rate", "pitch", "volume"},
    "s": set(),
    "say-as": {"interpret-as", "format"},
    "speak": set(),
    "sub": {"alias"},
    "voice": {"name"},
    "w": {"role"},
}
_BREAK_TIME = re.compile(r"^[0-9]+(\.[0-9]+)?(ms|s)$")
_BREAK_STRENGTH = {"none", "x-weak", "weak", "medium", "strong", "x-strong"}


def _attr_names(element) -> dict:
    names = {}
    for key, value in element.attrib.items():
        if key == XML_LANG:
            names["xml:lang"] = value
        else:
            names[key] = value
    return names


def validate_alexa_ssml(ssml: str) -> None:
    try:
        root = ET.fromstring(ssml)
    except ET.ParseError as exc:
        raise ValueError("SSML is not XML: " + ssml) from exc
    if root.tag != "speak":
        raise ValueError("SSML root must be speak")
    for el in root.iter():
        if el.tag not in _TAGS:
            raise ValueError("unsupported SSML tag " + el.tag)
        attrs = _attr_names(el)
        extra = set(attrs) - _ATTRS[el.tag]
        if extra:
            raise ValueError("unsupported attribute " + ",".join(sorted(extra)))
        if el.tag == "say-as":
            interpret = attrs.get("interpret-as")
            if interpret not in _SAY_AS:
                raise ValueError("unsupported say-as interpret-as " + repr(interpret))
            fmt = attrs.get("format")
            if interpret != "date":
                if fmt is not None:
                    raise ValueError("unsupported say-as format " + fmt)
            elif fmt is not None and fmt not in _DATE_FORMATS:
                raise ValueError("unsupported say-as format " + fmt)
        if el.tag == "break":
            if "time" in attrs and not _BREAK_TIME.match(attrs["time"]):
                raise ValueError("unsupported break time " + attrs["time"])
            if "time" in attrs and _break_seconds(attrs["time"]) > 10:
                raise ValueError("break time is longer than 10 seconds")
            if "strength" in attrs and attrs["strength"] not in _BREAK_STRENGTH:
                raise ValueError("unsupported break strength")
        if el.tag == "voice":
            name = attrs.get("name")
            if name not in ALEXA_VOICES:
                raise ValueError("unsupported voice " + repr(name))
        if el.tag == "lang":
            locale = attrs.get("xml:lang")
            if locale not in ALEXA_LANGS:
                raise ValueError("unsupported lang " + repr(locale))


def _break_seconds(value: str) -> float:
    if value.endswith("ms"):
        return float(value[:-2]) / 1000.0
    return float(value[:-1])

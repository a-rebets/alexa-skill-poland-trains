"""Direction choices: the v2 token, and which trains are spoken."""

import base64
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from upcoming_trains.answer import answer
from upcoming_trains.schedule import FLAG_ARRIVAL, FLAG_DEPARTURE, NO_STATION, dump, load
from upcoming_trains.timeutil import warsaw_wall_to_utc
from upcoming_trains.token import TokenError, decode_token, encode_choices, encode_token, read_token

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_data import catalog_from_schedule  # noqa: E402

NOW = warsaw_wall_to_utc(datetime(2026, 10, 21, 7, 30))


def _schedule(tmp_path):
    """Zachodnia calls Ochota (S2, R3), Centralna (arrival), and Kraków (Intercity)."""
    day = date(2026, 10, 21)
    strings = [
        "33506",
        "Warszawa Zachodnia",
        "33530",
        "Warszawa Ochota",
        "33605",
        "Warszawa Centralna",
        "99901",
        "Kraków Główny",
        "t-s2",
        "Head",
        "S2",
        "SKM Warszawa",
        "1",
        "t-r3",
        "R3",
        "Koleje Mazowieckie",
        "2",
        "t-ic",
        "EIP",
        "PKP Intercity",
        "3",
    ]
    services = [bytes([1])]
    trips = [
        (8, 9, 10, 11, 12, 0, 0),
        (13, 9, 14, 15, 16, 0, 0),
        (17, 9, 18, 19, 20, 0, 0),
    ]
    eight = 8 * 3600
    events = [
        (eight, eight, 2, FLAG_DEPARTURE, 0, 1, NO_STATION),
        (eight + 600, eight + 600, 2, FLAG_DEPARTURE, 1, 1, NO_STATION),
        (eight + 300, eight + 300, 3, FLAG_DEPARTURE, 2, 3, NO_STATION),
        (eight + 120, eight + 120, 4, FLAG_ARRIVAL, 0, NO_STATION, 2),
    ]
    stations = [
        (0, 1, 0, len(events)),
        (2, 3, len(events), 0),
        (4, 5, len(events), 0),
        (6, 7, len(events), 0),
    ]
    path = tmp_path / "choices.bin"
    dump(
        str(path),
        feed_start=day,
        feed_end=day,
        built_unix=1,
        strings=strings,
        services=services,
        stations=stations,
        trips=trips,
        events=events,
    )
    return load(str(path))


def _say(schedule, choices, kind="departure"):
    spoken, _status = answer(schedule, choices, now=NOW, fetch=False, kind=kind)
    return spoken.plain


def test_catalog_merges_neighbours_and_collapses_long_distance(tmp_path):
    catalog = catalog_from_schedule(_schedule(tmp_path))
    rows = catalog["33506"]
    by_id = {row["id"]: row for row in rows}
    assert by_id["33530"]["name"] == "Ochota"
    assert by_id["33530"]["lines"] == ["R3", "S2"]
    assert by_id["33605"]["name"] == "Centralna"
    assert "S2" in by_id["33605"]["lines"]
    assert "99901" not in by_id
    assert rows[-1] == {"id": "LD", "name": "Long-distance (Intercity)", "lines": []}


def test_v1_speaks_every_direction_and_v2_can_drop_intercity(tmp_path):
    schedule = _schedule(tmp_path)
    everything = _say(schedule, decode_token(encode_token(["33506"])))
    assert "Towards Ochota:" in everything
    assert "S two" in everything
    assert "R three" in everything
    assert "Intercity" in everything

    local_only = _say(schedule, [("33506", ["33530"])])
    assert "Towards Ochota:" in local_only
    assert "S two" in local_only
    assert "Intercity" not in local_only

    long_only = _say(schedule, [("33506", ["LD"])])
    assert "Intercity" in long_only
    assert "Ochota" not in long_only

    arrivals = _say(schedule, [("33506", ["33605"])], kind="arrival")
    assert "From Centralna:" in arrivals
    assert "S two" in arrivals
    assert "Intercity" not in arrivals


def test_unknown_direction_ids_fall_back_instead_of_silence(tmp_path):
    schedule = _schedule(tmp_path)
    spoken = _say(schedule, [("33506", ["00000", "nope-not-a-real-id"])])
    assert "Towards Ochota:" in spoken
    assert "Intercity" in spoken
    mixed = _say(schedule, [("33506", ["00000", "LD"])])
    assert "Intercity" in mixed
    assert "Ochota" not in mixed


def test_python_and_the_picker_encode_the_same_v2_token():
    choices = [["33506", ["LD", "33530"]], ["33548", None], ["33605", []]]
    script = (
        "const p = require('./picker/picker.js');\n"
        "process.stdout.write(p.encodeChoices(%s));\n" % json.dumps(choices)
    )
    produced = subprocess.check_output(["node", "-e", script], cwd=ROOT, text=True)
    expected = encode_choices(
        [("33506", ["LD", "33530"]), ("33548", None), ("33605", [])]
    )
    assert produced == expected
    assert produced.startswith("v2.")
    assert len(produced) < 2048
    decoded = decode_token(produced)
    assert [choice.station_id for choice in decoded] == ["33506", "33548", "33605"]
    assert decoded[0].directions == ("33530", "LD")
    assert decoded[1].directions is None
    assert decoded[2].directions is None
    with pytest.raises(TokenError):
        decode_token("v2.YQ")
    assert read_token(produced).api_key == ""


def test_python_and_the_picker_encode_a_voice_key_the_same_way():
    choices = [["33506", ["LD", "33530"]], ["33548", None]]
    key = "sk_test_key_123456"
    script = (
        "const p = require('./picker/picker.js');\n"
        "process.stdout.write(p.encodeChoices(%s, %s));\n" % (json.dumps(choices), json.dumps(key))
    )
    produced = subprocess.check_output(["node", "-e", script], cwd=ROOT, text=True)
    expected = encode_choices([("33506", ["LD", "33530"]), ("33548", None)], key)
    assert produced == expected
    account = read_token(produced)
    assert account.api_key == key
    assert [choice.station_id for choice in account.choices] == ["33506", "33548"]
    assert account.choices[0].directions == ("33530", "LD")
    assert read_token(encode_token(["33506"])).api_key == ""
    bare = encode_choices([("33506", None)])
    assert bare == encode_choices([("33506", None)], "  ")
    assert key not in bare
    broken = base64.urlsafe_b64encode(
        b'{"c":[["33506",null]],"k":"sk_test_key_123456"'
    ).decode("ascii").rstrip("=")
    with pytest.raises(TokenError) as caught:
        read_token("v2." + broken)
    assert "sk_test" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_five_stations_with_every_direction_stay_under_two_kilobytes(sample_feed):
    catalog = json.loads((sample_feed["picker"] / "directions.json").read_text(encoding="utf-8"))
    busiest = sorted(catalog, key=lambda stop_id: len(catalog[stop_id]), reverse=True)[:5]
    assert len(busiest) == 5
    all_on = encode_choices([(stop_id, None) for stop_id in busiest])
    assert all_on.startswith("v2.")
    assert len(all_on) < 2048
    assert all(choice.directions is None for choice in decode_token(all_on))
    explicit = encode_choices(
        [(stop_id, [row["id"] for row in catalog[stop_id]]) for stop_id in busiest]
    )
    assert len(explicit) < 2048
    round_trip = decode_token(explicit)
    assert [choice.station_id for choice in round_trip] == busiest
    assert round_trip[0].directions is not None
    assert "LD" in round_trip[0].directions

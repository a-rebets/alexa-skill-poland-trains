import json

from upcoming_trains.names import normalize_name, search_stations

STATIONS = [
    {"id": "1", "name": "Łódź Fabryczna"},
    {"id": "2", "name": "Łódź Widzew"},
    {"id": "3", "name": "Warszawa Centralna"},
    {"id": "4", "name": "Warszawa Zachodnia"},
    {"id": "5", "name": "Kraków Główny"},
]


def test_fold_polish_letters():
    assert normalize_name("Łódź Fabryczna") == "lodz fabryczna"
    assert normalize_name("Kraków Główny") == "krakow glowny"


def test_search_lodz_and_warsaw_alias():
    lodz = [item["name"] for item in search_stations(STATIONS, "lodz")]
    assert lodz[0].startswith("Łódź")
    assert "Łódź Fabryczna" in lodz
    warsaw = search_stations(STATIONS, "warsaw central")
    assert warsaw[0]["id"] == "3"
    assert search_stations(STATIONS, "") == []
    assert search_stations(STATIONS, "zzzznotastation") == []


def test_single_letter_typo():
    hits = search_stations(STATIONS, "krakof")
    assert hits and hits[0]["id"] == "5"


def test_sample_station_list_finds_lodz_and_warsaw(sample_feed):
    stations = json.loads((sample_feed["picker"] / "stations.json").read_text(encoding="utf-8"))
    lodz = search_stations(stations, "lodz", limit=40)
    assert any(item["name"] == "Łódź Fabryczna" for item in lodz)
    warsaw = search_stations(stations, "warsaw central", limit=5)
    assert warsaw[0]["name"] == "Warszawa Centralna"
    assert warsaw[0]["id"] == "33605"

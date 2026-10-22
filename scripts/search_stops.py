#!/usr/bin/env python3
"""Find station ids by name. Diacritics are optional: lodz finds Łódź.

    python3 scripts/search_stops.py warsaw central
    python3 scripts/search_stops.py lodz
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambda"))

from upcoming_trains.names import search_stations  # noqa: E402


def main() -> None:
    query = " ".join(sys.argv[1:]).strip()
    if not query:
        print("Usage: python3 scripts/search_stops.py <name>", file=sys.stderr)
        sys.exit(2)
    path = ROOT / "picker" / "stations.json"
    if not path.exists():
        print("picker/stations.json is missing. Run: python3 scripts/build_data.py", file=sys.stderr)
        sys.exit(1)
    stations = json.loads(path.read_text(encoding="utf-8"))
    hits = search_stations(stations, query, limit=20)
    if not hits:
        print("No matches")
        return
    for station in hits:
        print(f"{station['id']}\t{station['name']}")


if __name__ == "__main__":
    main()

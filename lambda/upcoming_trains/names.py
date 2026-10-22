"""Diacritic-insensitive station search shared with the picker page.

'lodz' matches 'Łódź Fabryczna'. 'warsaw central' matches 'Warszawa Centralna'.
The JavaScript in picker/picker.js follows the same rules; both sides are tested.
"""

from __future__ import annotations

_FOLDS = str.maketrans(
    {
        "ą": "a",
        "ć": "c",
        "ę": "e",
        "ł": "l",
        "ń": "n",
        "ó": "o",
        "ś": "s",
        "ź": "z",
        "ż": "z",
        "Ą": "a",
        "Ć": "c",
        "Ę": "e",
        "Ł": "l",
        "Ń": "n",
        "Ó": "o",
        "Ś": "s",
        "Ź": "z",
        "Ż": "z",
    }
)

# Whole words only. Applied to the query after folding.
_ALIASES = {
    "warsaw": "warszawa",
    "cracow": "krakow",
}


def normalize_name(text: str) -> str:
    if not text:
        return ""
    folded = text.translate(_FOLDS).lower()
    # Drop combining marks left by letters we did not map explicitly.
    decomposed = folded
    try:
        import unicodedata

        decomposed = unicodedata.normalize("NFD", folded)
        decomposed = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    except Exception:
        decomposed = folded
    out = []
    prev_space = True
    for ch in decomposed:
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
            prev_space = False
        elif not prev_space:
            out.append(" ")
            prev_space = True
    return "".join(out).strip()


def _alias_query(normalized: str) -> str:
    return " ".join(_ALIASES.get(part, part) for part in normalized.split(" "))


def _levenshtein(left: str, right: str) -> int:
    if abs(len(left) - len(right)) > 1:
        return 2
    if left == right:
        return 0
    prev = list(range(len(right) + 1))
    for i, ca in enumerate(left, start=1):
        cur = [i]
        for j, cb in enumerate(right, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def score_name(normalized_name: str, normalized_query: str) -> int:
    """Higher is better. Zero means no match."""
    name = normalized_name
    query = normalized_query
    if not query or not name:
        return 0
    if name == query:
        return 10000
    if name.startswith(query):
        return 8000 - len(name)
    index = name.find(query)
    if index >= 0:
        return 6000 - index * 10 - len(name)
    query_tokens = query.split(" ")
    name_tokens = name.split(" ")
    if query_tokens and all(
        any(part.startswith(token) for part in name_tokens) for token in query_tokens
    ):
        return 5000 - len(name)
    if len(query_tokens) == 1 and len(query) >= 4:
        for part in name_tokens:
            if _levenshtein(part, query) <= 1:
                return 3000 - len(name)
    return 0


def search_stations(stations: list[dict], query: str, limit: int = 20) -> list[dict]:
    """stations items need 'id' and 'name'. Returns the best matches, best first."""
    folded = _alias_query(normalize_name(query))
    if not folded:
        return []
    scored = []
    for station in stations:
        name = station.get("name") or ""
        normalized = station.get("norm") or normalize_name(name)
        score = score_name(normalized, folded)
        if score > 0:
            scored.append((score, name, station))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:limit]]

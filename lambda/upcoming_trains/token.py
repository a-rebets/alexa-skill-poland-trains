"""Access token that *is* the station list.

Alexa implicit grant stores one string per Amazon account. This skill puts the
chosen station codes in that string, so it does not need DynamoDB.

The token is not signed. The picker is a static page, so any signing key
shipped with it would be public. Station and direction codes are public.
An optional ElevenLabs key rides in the same token and is not written
anywhere else. Format checks reject garbage.

v1.<base64url JSON array of station-id strings>
    Every direction, including long-distance. No voice key.
    Example: v1.WyIzMzYwNSIsIjMzNTA2Il0

v2.<base64url JSON>
    Without a key, a JSON array of [station id, directions].
    directions is null (every direction) or a list of neighbour stop ids.
    "LD" is the long-distance option. Example:
    [["33506",["33530","LD"]],["33548",null]]
    With a key, an object {"c": <that array>, "k": "<api key>"}.
    A missing or empty key means the Alexa voice.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass

from upcoming_trains.constants import MAX_STATIONS_IN_TOKEN, TOKEN_VERSION

_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,40}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_MAX_TOKEN_LENGTH = 2048
TOKEN_V2 = "v2"
LONG_DISTANCE = "LD"


class TokenError(ValueError):
    """The access token is missing a usable station list."""


@dataclass(frozen=True)
class StationChoice:
    """One saved station.

    `directions` is None when every direction is on. Otherwise it is the
    neighbour stop ids the person kept, plus "LD" when long-distance is on.
    """

    station_id: str
    directions: tuple | None = None


@dataclass(frozen=True)
class Account:
    """Stations saved at linking, and the optional ElevenLabs key."""

    choices: tuple
    api_key: str = ""


def usable_key(value) -> str:
    """A key the token will carry. Anything else means the Alexa voice."""
    if not isinstance(value, str):
        return ""
    key = value.strip()
    if _KEY_RE.match(key):
        return key
    return ""


def encode_token(station_ids: list[str]) -> str:
    """v1 token: those stations, every direction."""
    cleaned = _clean_ids(station_ids)
    return _pack(TOKEN_VERSION, cleaned)


def encode_choices(choices: list, api_key: str = "") -> str:
    """v2 token. A direction list is sorted. An empty list means every direction.

    A usable key is field ``k`` on an object. No key keeps the old array.
    """
    if not isinstance(choices, list) or not choices:
        raise TokenError("bad token")
    if len(choices) > MAX_STATIONS_IN_TOKEN:
        raise TokenError("bad token")
    payload = []
    seen = set()
    for item in choices:
        station_id, directions = _pair(item)
        if station_id in seen:
            continue
        seen.add(station_id)
        if directions is None:
            payload.append([station_id, None])
        else:
            payload.append([station_id, list(directions)])
    if not payload:
        raise TokenError("bad token")
    key = usable_key(api_key)
    body = {"c": payload, "k": key} if key else payload
    return _pack(TOKEN_V2, body)


def read_token(token: str) -> Account:
    """v1 and a v2 array have no key. v2 ``k`` is the ElevenLabs key."""
    if token is None:
        raise TokenError("missing token")
    text = str(token).strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    if not text or len(text) > _MAX_TOKEN_LENGTH:
        raise TokenError("bad token")
    version, body = _split(text)
    data = _unpack(body)
    if version == TOKEN_VERSION:
        if not isinstance(data, list) or not data:
            raise TokenError("bad token")
        return Account(tuple(StationChoice(stop_id, None) for stop_id in _clean_ids(data)))
    if version == TOKEN_V2:
        return _account_v2(data)
    raise TokenError("bad token")


def decode_token(token: str) -> list[StationChoice]:
    """v1 is every direction. v2 keeps the direction list, or all of them."""
    return list(read_token(token).choices)


def _account_v2(data) -> Account:
    if isinstance(data, list):
        return Account(tuple(_clean_choices(data)))
    if isinstance(data, dict):
        choices = data.get("c")
        if not isinstance(choices, list):
            raise TokenError("bad token")
        return Account(tuple(_clean_choices(choices)), usable_key(data.get("k", "")))
    raise TokenError("bad token")


def _pair(item) -> tuple:
    if isinstance(item, StationChoice):
        station_id, directions = item.station_id, item.directions
    elif isinstance(item, (list, tuple)) and len(item) == 2:
        station_id, directions = item
    else:
        raise TokenError("bad token")
    if not isinstance(station_id, str) or not _ID_RE.match(station_id):
        raise TokenError("bad token")
    if directions is None:
        return station_id, None
    if isinstance(directions, (list, tuple)):
        return station_id, _clean_directions(directions)
    raise TokenError("bad token")


def _clean_choices(values: list) -> list[StationChoice]:
    if len(values) > MAX_STATIONS_IN_TOKEN:
        raise TokenError("bad token")
    cleaned = []
    seen = set()
    for item in values:
        station_id, directions = _pair(item)
        if station_id in seen:
            continue
        seen.add(station_id)
        cleaned.append(StationChoice(station_id, directions))
    if not cleaned:
        raise TokenError("bad token")
    return cleaned


def _clean_directions(values) -> tuple | None:
    """Sorted unique ids. An empty selection is stored as every direction."""
    if not isinstance(values, (list, tuple)):
        raise TokenError("bad token")
    cleaned = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not _ID_RE.match(value):
            raise TokenError("bad token")
        if value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    if not cleaned:
        return None
    cleaned.sort()
    return tuple(cleaned)


def _clean_ids(values: list) -> list[str]:
    if not isinstance(values, list) or not values:
        raise TokenError("bad token")
    if len(values) > MAX_STATIONS_IN_TOKEN:
        raise TokenError("bad token")
    cleaned = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not _ID_RE.match(value):
            raise TokenError("bad token")
        if value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    if not cleaned:
        raise TokenError("bad token")
    return cleaned


def _pack(version: str, payload) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    token = version + "." + encoded
    if len(token) > _MAX_TOKEN_LENGTH:
        raise TokenError("bad token")
    return token


def _split(text: str) -> tuple:
    dot = text.find(".")
    if dot < 1:
        raise TokenError("bad token")
    version = text[:dot]
    body = text[dot + 1 :]
    if version not in (TOKEN_VERSION, TOKEN_V2):
        raise TokenError("bad token")
    if not body or re.search(r"[^A-Za-z0-9_-]", body):
        raise TokenError("bad token")
    return version, body


def _unpack(body: str):
    pad = "=" * ((4 - len(body) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(body + pad)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise TokenError("bad token")

import json
import subprocess
from pathlib import Path

import pytest

from upcoming_trains.token import TokenError, decode_token, encode_token

ROOT = Path(__file__).resolve().parents[1]


def _ids(token):
    choices = decode_token(token)
    assert all(choice.directions is None for choice in choices)
    return [choice.station_id for choice in choices]


def test_round_trip_preserves_order_and_drops_duplicates():
    token = encode_token(["33605", "0", "33605", "33506"])
    assert token.startswith("v1.")
    assert "=" not in token
    assert _ids(token) == ["33605", "0", "33506"]
    assert _ids("Bearer " + token) == ["33605", "0", "33506"]


def test_rejects_garbage():
    with pytest.raises(TokenError):
        decode_token("")
    with pytest.raises(TokenError):
        decode_token("nope")
    with pytest.raises(TokenError):
        decode_token("v2.YQ")
    with pytest.raises(TokenError):
        decode_token(encode_token(["33605"]) + "!!!")
    with pytest.raises(TokenError):
        decode_token(encode_token(["has space"]))
    with pytest.raises(TokenError):
        decode_token(encode_token(["1"] * 9))


def test_matches_the_picker_page():
    ids = ["33605", "0", "38653"]
    script = (
        "const p = require('./picker/picker.js');\n"
        f"process.stdout.write(p.encodeToken({json.dumps(ids)}));\n"
    )
    produced = subprocess.check_output(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
    )
    assert produced == encode_token(ids)
    assert _ids(produced) == ids

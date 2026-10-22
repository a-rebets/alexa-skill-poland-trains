"""ElevenLabs narration, with S3 and HTTP replaced by fakes."""

import io
import json
import socket
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lambda_function
from test_speech import validate_alexa_ssml
from upcoming_trains.constants import (
    ELEVENLABS_MAX_AUDIO_BYTES,
    ELEVENLABS_MIN_REQUEST_SECONDS,
    ELEVENLABS_MODEL,
    ELEVENLABS_OUTPUT_FORMAT,
    PROGRESSIVE_TIMEOUT_SECONDS,
    ELEVENLABS_S3_RESERVE_SECONDS,
    ELEVENLABS_TIMEOUT_MARGIN_SECONDS,
    ELEVENLABS_URL_EXPIRY_SECONDS,
    ELEVENLABS_VOICE_ID,
    HANDLER_BUDGET_SECONDS,
)
from upcoming_trains.speech import Spoken
from upcoming_trains.timing import begin, emit
from upcoming_trains.token import Account, encode_choices, encode_token, read_token
from upcoming_trains import voice

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
_REAL_SWEEP = voice._maybe_sweep
FALLBACK = "<speak>R two in five minutes.</speak>"
TEXT = "R two in five minutes."
USER = "amzn1.ask.account.EXAMPLE"
KEY = "sk_test_key_123456"
API_ENDPOINT = "https://api.eu.amazonalexa.com"
API_ACCESS = "Atza|example-access-token"
REQUEST_ID = "amzn1.echo-api.request.example"
PROGRESS = (API_ENDPOINT, API_ACCESS, REQUEST_ID)


class Missing(Exception):
    def __init__(self):
        super().__init__("missing")
        self.response = {
            "Error": {"Code": "404"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }


class FakeBody:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.presigns = []
        self.gets = 0
        self.lists = 0
        self.list_delay = 0

    def head_object(self, Bucket, Key):
        delay = getattr(self, "head_delay", 0)
        if delay:
            time.sleep(delay)
        if Key not in self.objects:
            raise Missing()

    def get_object(self, Bucket, Key):
        self.gets += 1
        if Key not in self.objects:
            raise Missing()
        return {"Body": FakeBody(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        if isinstance(Body, str):
            Body = Body.encode("utf-8")
        self.objects[Key] = bytes(Body)

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None, ContinuationToken=None):
        if self.list_delay:
            time.sleep(self.list_delay)
        self.lists += 1
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        if Delimiter:
            contents = []
            folders = []
            seen = set()
            for key in keys:
                rest = key[len(Prefix) :]
                cut = rest.find(Delimiter)
                if cut == -1:
                    contents.append({"Key": key})
                else:
                    folder = Prefix + rest[: cut + len(Delimiter)]
                    if folder not in seen:
                        seen.add(folder)
                        folders.append({"Prefix": folder})
            return {
                "Contents": contents,
                "CommonPrefixes": folders,
                "IsTruncated": False,
            }
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}

    def delete_objects(self, Bucket, Delete):
        for item in Delete.get("Objects") or []:
            self.objects.pop(item.get("Key"), None)
        return {}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.presigns.append(ExpiresIn)
        return "https://s3.example/%s?X-Amz-Expires=%s&X-Amz-Signature=abc" % (
            Params["Key"],
            ExpiresIn,
        )


class FakeResponse:
    def __init__(self, body, content_type="audio/mpeg"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture(autouse=True)
def _isolated_handler_clock():
    # lambda_handler starts the ElevenLabs budget clock and leaves it set.
    # test_speech invokes the real handler. A later test then sees a budget
    # shortened by however long the suite has been running, which fails the
    # cache-miss timeout check when the runner is busy.
    voice._handler_started = None
    yield
    voice._handler_started = None


@pytest.fixture
def s3(monkeypatch):
    voice._swept_for = ""
    monkeypatch.setattr(voice, "_maybe_sweep", lambda *args, **kwargs: None)
    fake = FakeS3()
    monkeypatch.setenv("S3_PERSISTENCE_BUCKET", "alexa-hosted-bucket")
    monkeypatch.setattr(voice, "s3_client", lambda: fake)
    return fake


def _object_key(text=TEXT, now=NOW):
    return voice.clip_object_key(text, now)


def _usage_keys(fake):
    return [key for key in fake.objects if key.startswith("usage/")]


def test_cache_hit_reuses_the_object(s3, monkeypatch):
    s3.objects[_object_key()] = b"ID3cached"

    def explode(*args, **kwargs):
        raise AssertionError("a cached clip must not call ElevenLabs")

    monkeypatch.setattr(voice.urllib.request, "urlopen", explode)
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    _assert_one_audio(spoken)
    assert s3.gets == 0
    assert _usage_keys(s3) == []
    assert USER not in _object_key()
    assert KEY not in _object_key()
    assert s3.presigns == [ELEVENLABS_URL_EXPIRY_SECONDS]


def test_cache_miss_uploads_the_clip(s3, monkeypatch):
    seen = {}

    def urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["key"] = request.get_header("Xi-api-key")
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(b"ID3fresh")

    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    _assert_one_audio(spoken)
    assert seen["timeout"] == pytest.approx(_full_timeout(), abs=0.05)
    assert seen["key"] == KEY
    assert KEY not in spoken
    assert ELEVENLABS_OUTPUT_FORMAT in seen["url"]
    assert ELEVENLABS_VOICE_ID in seen["url"]
    assert seen["body"] == {
        "text": TEXT,
        "model_id": ELEVENLABS_MODEL,
        "language_code": "en",
    }
    assert s3.objects[_object_key()] == b"ID3fresh"
    assert "Media/tts/2026-10-22/" in spoken
    assert "X-Amz-Expires=%s" % ELEVENLABS_URL_EXPIRY_SECONDS in spoken
    assert s3.gets == 0
    assert _usage_keys(s3) == []
    assert all(KEY not in stored for stored in s3.objects)


def _inline_progress(monkeypatch):
    def inline(target, args):
        target(*args)

    monkeypatch.setattr(voice, "_spawn", inline)


def test_progressive_response_is_sent_on_a_cache_miss(s3, monkeypatch):
    calls = []

    def urlopen(request, timeout=None):
        calls.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "auth": request.get_header("Authorization"),
                "body": request.data,
            }
        )
        return FakeResponse(b"ID3fresh")

    _inline_progress(monkeypatch)
    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY, PROGRESS)
    _assert_one_audio(spoken)
    directives = [call for call in calls if call["url"].endswith("/v1/directives")]
    synthesis = [call for call in calls if "text-to-speech" in call["url"]]
    assert len(directives) == 1
    assert directives[0]["url"] == API_ENDPOINT + "/v1/directives"
    assert directives[0]["timeout"] == PROGRESSIVE_TIMEOUT_SECONDS
    assert directives[0]["auth"] == "Bearer " + API_ACCESS
    body = json.loads(directives[0]["body"].decode("utf-8"))
    assert body["header"]["requestId"] == REQUEST_ID
    assert body["directive"]["type"] == "VoicePlayer.Speak"
    assert body["directive"]["speech"] == "<speak>Checking the timetable</speak>"
    assert len(synthesis) == 1
    assert calls[0]["url"].endswith("/v1/directives")
    assert API_ACCESS not in spoken
    assert KEY not in spoken


def test_progressive_response_is_skipped_on_a_cache_hit(s3, monkeypatch):
    s3.objects[_object_key()] = b"ID3cached"

    def explode(*args, **kwargs):
        raise AssertionError("a cached clip must not send a progressive response")

    monkeypatch.setattr(voice.urllib.request, "urlopen", explode)
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY, PROGRESS)
    _assert_one_audio(spoken)


def test_progressive_failure_does_not_break_the_answer(s3, monkeypatch):
    def urlopen(request, timeout=None):
        if request.full_url.endswith("/v1/directives"):
            raise urllib.error.URLError("directive service down")
        return FakeResponse(b"ID3fresh")

    _inline_progress(monkeypatch)
    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY, PROGRESS)
    _assert_one_audio(spoken)
    assert API_ACCESS not in spoken


def test_progressive_response_is_not_sent_to_a_foreign_endpoint(s3, monkeypatch):
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request.full_url)
        return FakeResponse(b"ID3fresh")

    _inline_progress(monkeypatch)
    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    spoken = voice.narrate(
        TEXT,
        USER,
        FALLBACK,
        NOW,
        KEY,
        ("https://evil.example/steal", API_ACCESS, REQUEST_ID),
    )
    _assert_one_audio(spoken)
    assert calls
    assert all("evil.example" not in url for url in calls)
    assert all(API_ACCESS not in url for url in calls)


def _age_handler(seconds: float) -> None:
    voice.note_handler_start()
    voice._handler_started = time.monotonic() - seconds


def test_deadline_skips_elevenlabs_when_the_handler_budget_is_spent(s3, monkeypatch):
    _age_handler(HANDLER_BUDGET_SECONDS)

    def explode(*args, **kwargs):
        raise AssertionError("no time left for ElevenLabs or S3")

    monkeypatch.setattr(voice, "s3_client", explode)
    monkeypatch.setattr(voice.urllib.request, "urlopen", explode)
    try:
        assert voice.narrate(TEXT, USER, FALLBACK, NOW, KEY) == FALLBACK
    finally:
        voice._handler_started = None


def _full_timeout() -> float:
    return (
        HANDLER_BUDGET_SECONDS
        - ELEVENLABS_S3_RESERVE_SECONDS
        - ELEVENLABS_TIMEOUT_MARGIN_SECONDS
    )


def test_deadline_shortens_the_elevenlabs_request(s3, monkeypatch):
    spent = 1.0
    _age_handler(spent)
    expected = (
        HANDLER_BUDGET_SECONDS
        - spent
        - ELEVENLABS_S3_RESERVE_SECONDS
        - ELEVENLABS_TIMEOUT_MARGIN_SECONDS
    )
    assert expected > 3
    assert expected >= ELEVENLABS_MIN_REQUEST_SECONDS
    seen = {}

    def urlopen(request, timeout=None):
        seen["timeout"] = timeout
        return FakeResponse(b"ID3fresh")

    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    try:
        spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    finally:
        voice._handler_started = None
    _assert_one_audio(spoken)
    assert seen["timeout"] == pytest.approx(expected, abs=0.05)


def test_a_short_remainder_falls_back_without_calling_elevenlabs(s3, monkeypatch):
    _age_handler(
        HANDLER_BUDGET_SECONDS
        - ELEVENLABS_S3_RESERVE_SECONDS
        - ELEVENLABS_TIMEOUT_MARGIN_SECONDS
        - 0.05
    )

    def explode(*args, **kwargs):
        raise AssertionError("a short remainder must not call ElevenLabs")

    monkeypatch.setattr(voice.urllib.request, "urlopen", explode)
    try:
        assert voice.narrate(TEXT, USER, FALLBACK, NOW, KEY) == FALLBACK
    finally:
        voice._handler_started = None
    assert _object_key() not in s3.objects


def test_s3_put_failure_falls_back_after_a_successful_generation(s3, monkeypatch):
    def urlopen(request, timeout=None):
        return FakeResponse(b"ID3fresh")

    def fail_put(Bucket, Key, Body, ContentType=None):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(s3, "put_object", fail_put)
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    assert spoken == FALLBACK
    assert "<audio" not in spoken
    assert KEY not in spoken
    assert _object_key() not in s3.objects
    assert _usage_keys(s3) == []


def test_a_slow_cache_miss_starts_elevenlabs_before_the_head_returns(s3, monkeypatch):
    s3.head_delay = 0.45
    started = {}
    began = time.perf_counter()

    def urlopen(request, timeout=None):
        started["at"] = time.perf_counter()
        started["timeout"] = timeout
        return FakeResponse(b"ID3fresh")

    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    _assert_one_audio(spoken)
    assert started["at"] - began < 0.3
    assert started["timeout"] > 3
    assert s3.objects[_object_key()] == b"ID3fresh"


def test_a_slow_cache_hit_is_played_without_the_checking_line(s3, monkeypatch):
    s3.objects[_object_key()] = b"ID3cached"
    s3.head_delay = 0.35
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request.full_url)
        time.sleep(0.8)
        return FakeResponse(b"ID3fresh")

    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    began = time.perf_counter()
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY, PROGRESS)
    elapsed = time.perf_counter() - began
    _assert_one_audio(spoken)
    assert elapsed < 0.6
    assert calls
    assert all("text-to-speech" in url for url in calls)
    assert s3.objects[_object_key()] == b"ID3cached"
    assert s3.gets == 0
    assert _usage_keys(s3) == []


def _last_timing_line(capsys) -> str:
    emit()
    return capsys.readouterr().out.strip().splitlines()[-1]


def test_fallback_reasons_and_character_count_stay_out_of_the_secret(s3, monkeypatch, capsys):
    begin(1, 1)

    def time_out(request, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(voice.urllib.request, "urlopen", time_out)
    assert voice.narrate(TEXT, USER, FALLBACK, NOW, KEY) == FALLBACK
    line = _last_timing_line(capsys)
    assert "fb=eleven_timeout" in line
    assert "chars=%d" % len(TEXT) in line
    assert "fallback=1" in line
    assert KEY not in line
    assert USER not in line
    assert TEXT not in line

    begin(1, 1)

    def rejected(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.elevenlabs.io/v1/text-to-speech/voice",
            429,
            "rejected",
            {},
            io.BytesIO(b'{"detail":"quota"}'),
        )

    monkeypatch.setattr(voice.urllib.request, "urlopen", rejected)
    assert voice.narrate(TEXT, USER, FALLBACK, NOW, KEY) == FALLBACK
    assert "fb=eleven_http_429" in _last_timing_line(capsys)

    begin(1, 1)
    assert voice.narrate(TEXT, USER, FALLBACK, NOW, "") == FALLBACK
    quiet = _last_timing_line(capsys)
    assert "fb=no_key" in quiet
    assert "chars=%d" % len(TEXT) in quiet
    assert KEY not in quiet

    begin(1, 1)

    def fresh(request, timeout=None):
        return FakeResponse(b"ID3fresh")

    def fail_put(Bucket, Key, Body, ContentType=None):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(voice.urllib.request, "urlopen", fresh)
    monkeypatch.setattr(s3, "put_object", fail_put)
    assert voice.narrate(TEXT, USER, FALLBACK, NOW, KEY) == FALLBACK
    line = _last_timing_line(capsys)
    assert "fb=s3_error" in line
    assert KEY not in line


def test_a_generated_clip_logs_no_fallback(s3, monkeypatch, capsys):
    begin(0, 0)
    monkeypatch.setattr(
        voice.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeResponse(b"ID3fresh"),
    )
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    _assert_one_audio(spoken)
    line = _last_timing_line(capsys)
    assert "fallback=0" in line
    assert "fb=none" in line
    assert "chars=%d" % len(TEXT) in line
    assert KEY not in line


def test_timeout_falls_back_to_alexa_ssml(s3, monkeypatch):
    def urlopen(request, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    assert spoken == FALLBACK
    assert _object_key() not in s3.objects
    assert _usage_keys(s3) == []
    assert "<audio" not in spoken
    assert KEY not in spoken


def test_missing_key_uses_the_alexa_voice_and_skips_s3(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a missing key must not touch S3 or ElevenLabs")

    monkeypatch.setattr(voice, "s3_client", explode)
    monkeypatch.setattr(voice.urllib.request, "urlopen", explode)
    assert voice.narrate(TEXT, USER, FALLBACK, NOW) == FALLBACK


def test_an_oversized_mp3_is_not_stored(s3, monkeypatch, capsys):
    begin(0, 0)
    payload = b"ID3" + b"\x00" * ELEVENLABS_MAX_AUDIO_BYTES
    monkeypatch.setattr(
        voice.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeResponse(payload),
    )
    assert voice.narrate(TEXT, USER, FALLBACK, NOW, KEY) == FALLBACK
    assert _object_key() not in s3.objects
    assert _usage_keys(s3) == []
    assert s3.gets == 0
    line = _last_timing_line(capsys)
    assert "fb=too_long" in line
    assert "fb=cap" not in line


def test_audio_ssml_has_one_tag_and_escapes_the_presigned_url():
    ssml = voice.audio_ssml("https://s3.example/a.mp3?x=1&y=2")
    _assert_one_audio(ssml)
    assert "&amp;" in ssml
    assert "&y=2" not in ssml.replace("&amp;", "")


def test_only_todays_warsaw_prefix_is_a_cache_hit(s3, monkeypatch):
    digest = voice.cache_key(TEXT)
    legacy = "Media/tts/" + digest + ".mp3"
    yesterday = "Media/tts/2026-10-21/" + digest + ".mp3"
    s3.objects[legacy] = b"ID3legacy"
    s3.objects[yesterday] = b"ID3yesterday"

    def urlopen(request, timeout=None):
        return FakeResponse(b"ID3fresh")

    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    _assert_one_audio(spoken)
    assert s3.objects[_object_key()] == b"ID3fresh"
    assert s3.objects[legacy] == b"ID3legacy"
    assert s3.objects[yesterday] == b"ID3yesterday"
    late = datetime(2026, 10, 22, 22, 30, tzinfo=UTC)
    assert voice.clip_object_key(TEXT, late).startswith("Media/tts/2026-10-23/")
    assert voice.clip_object_key(TEXT, NOW).startswith("Media/tts/2026-10-22/")


def test_old_clips_are_deleted_without_delaying_the_answer(s3, monkeypatch):
    monkeypatch.setattr(voice, "_maybe_sweep", _REAL_SWEEP)
    voice._swept_for = ""
    digest = voice.cache_key(TEXT)
    today = _object_key()
    yesterday = "Media/tts/2026-10-21/" + digest + ".mp3"
    legacy = "Media/tts/" + voice.cache_key("older sentence") + ".mp3"
    other = "Media/tts/notes.txt"
    s3.objects[today] = b"ID3today"
    s3.objects[yesterday] = b"ID3old"
    s3.objects[legacy] = b"ID3legacy"
    s3.objects[other] = b"keep"
    s3.list_delay = 0.4

    def explode(*args, **kwargs):
        raise AssertionError("today's clip must not call ElevenLabs")

    monkeypatch.setattr(voice.urllib.request, "urlopen", explode)
    began = time.perf_counter()
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    elapsed = time.perf_counter() - began
    _assert_one_audio(spoken)
    assert elapsed < 0.25
    assert "Media/tts/2026-10-22/" in spoken
    assert "X-Amz-Expires=%s" % ELEVENLABS_URL_EXPIRY_SECONDS in spoken
    deadline = time.perf_counter() + 2
    while time.perf_counter() < deadline and (yesterday in s3.objects or legacy in s3.objects):
        time.sleep(0.02)
    assert yesterday not in s3.objects
    assert legacy not in s3.objects
    assert s3.objects[today] == b"ID3today"
    assert s3.objects[other] == b"keep"
    listed = s3.lists
    voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    time.sleep(0.05)
    assert s3.lists == listed
    tomorrow = datetime(2026, 10, 23, 6, 0, tzinfo=UTC)
    s3.objects[_object_key(now=tomorrow)] = b"ID3next"
    s3.list_delay = 0
    voice.narrate(TEXT, USER, FALLBACK, tomorrow, KEY)
    deadline = time.perf_counter() + 2
    while time.perf_counter() < deadline and s3.lists == listed:
        time.sleep(0.02)
    assert s3.lists > listed
    assert s3.objects[_object_key(now=tomorrow)] == b"ID3next"
    assert today not in s3.objects


def test_a_sweep_failure_is_logged_and_the_clip_still_plays(s3, monkeypatch, capsys):
    monkeypatch.setattr(voice, "_maybe_sweep", _REAL_SWEEP)
    voice._swept_for = ""

    def inline(target, args):
        target(*args)

    def boom(**kwargs):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(voice, "_start_sweep", inline)
    s3.list_objects_v2 = boom
    monkeypatch.setattr(
        voice.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeResponse(b"ID3fresh"),
    )
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    _assert_one_audio(spoken)
    logged = capsys.readouterr().out
    assert "tts sweep failed: RuntimeError" in logged
    assert KEY not in logged
    assert USER not in logged
    assert "AccessDenied" not in logged


def test_a_delete_error_is_logged_without_removing_today(s3, capsys):
    today = _object_key()
    legacy = "Media/tts/" + voice.cache_key("older sentence") + ".mp3"
    s3.objects[today] = b"ID3today"
    s3.objects[legacy] = b"ID3legacy"

    def refuse(Bucket, Delete):
        return {"Errors": [{"Key": legacy, "Code": "AccessDenied"}]}

    s3.delete_objects = refuse
    voice._sweep_old_clips(s3, "alexa-hosted-bucket", "2026-10-22")
    logged = capsys.readouterr().out
    assert "tts sweep failed: delete" in logged
    assert KEY not in logged
    assert s3.objects[today] == b"ID3today"
    assert s3.objects[legacy] == b"ID3legacy"


def test_help_stays_on_the_alexa_voice():
    response = lambda_function.handle(
        {"request": {"type": "IntentRequest", "intent": {"name": "AMAZON.HelpIntent"}}}
    )
    ssml = response["response"]["outputSpeech"]["ssml"]
    assert "<audio" not in ssml
    assert "departures" in ssml
    validate_alexa_ssml(ssml)


@pytest.mark.parametrize(
    "status,body",
    [
        (401, b'{"detail":"invalid_api_key"}'),
        (402, b'{"detail":"payment_required"}'),
        (429, b'{"detail":{"status":"quota_exceeded"}}'),
    ],
)
def test_a_rejected_key_falls_back(s3, monkeypatch, status, body):
    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.elevenlabs.io/v1/text-to-speech/voice",
            status,
            "rejected",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    spoken = voice.narrate(TEXT, USER, FALLBACK, NOW, KEY)
    assert spoken == FALLBACK
    assert "<audio" not in spoken
    assert KEY not in spoken
    assert all(KEY not in stored for stored in s3.objects)
    assert _object_key() not in s3.objects


def test_old_tokens_have_no_key():
    assert read_token(encode_token(["33506"])).api_key == ""
    assert read_token(encode_choices([("33506", None)])).api_key == ""
    keyed = read_token(encode_choices([("33506", None)], KEY))
    assert keyed.api_key == KEY
    assert keyed.choices[0].station_id == "33506"


def _launch(token):
    return {
        "request": {"type": "LaunchRequest"},
        "context": {"System": {"user": {"accessToken": token, "userId": USER}}},
    }


def _linked_request(token, kind="LaunchRequest", name=None):
    request = {"type": kind, "requestId": REQUEST_ID}
    if name:
        request["intent"] = {"name": name}
    return {
        "request": request,
        "context": {
            "System": {
                "user": {"accessToken": token, "userId": USER},
                "apiEndpoint": API_ENDPOINT,
                "apiAccessToken": API_ACCESS,
            }
        },
    }


def _fake_schedule():
    return type("S", (), {"covers": lambda self, day: True})()


def test_launch_with_a_key_speaks_audio_and_an_old_token_stays_ssml(s3, monkeypatch):
    seen = {}

    def urlopen(request, timeout=None):
        seen["key"] = request.get_header("Xi-api-key")
        return FakeResponse(b"ID3fresh")

    def fake_answer(*args, **kwargs):
        return (Spoken(plain=TEXT, ssml=FALLBACK, apl_body="R2 08:05"), {})

    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(lambda_function, "answer", fake_answer)
    monkeypatch.setattr(lambda_function, "get_schedule", _fake_schedule)
    keyed = lambda_function.handle(_launch(encode_choices([("33506", None)], KEY)))
    audio = keyed["response"]["outputSpeech"]["ssml"]
    _assert_one_audio(audio)
    assert seen["key"] == KEY
    assert KEY not in audio
    assert all(KEY not in stored for stored in s3.objects)

    def explode(*args, **kwargs):
        raise AssertionError("an old token must not call ElevenLabs")

    monkeypatch.setattr(voice.urllib.request, "urlopen", explode)
    old = lambda_function.handle(_launch(encode_token(["33506"])))
    ssml = old["response"]["outputSpeech"]["ssml"]
    assert ssml == FALLBACK
    assert "<audio" not in ssml


def test_launch_and_arrivals_announce_on_a_miss_and_help_does_not(s3, monkeypatch):
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request.full_url)
        return FakeResponse(b"ID3fresh")

    def fake_answer(*args, **kwargs):
        return (Spoken(plain=TEXT, ssml=FALLBACK, apl_body="R2 08:05"), {})

    _inline_progress(monkeypatch)
    monkeypatch.setattr(voice.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(lambda_function, "answer", fake_answer)
    monkeypatch.setattr(lambda_function, "get_schedule", _fake_schedule)
    token = encode_choices([("33506", None)], KEY)
    for event in (
        _linked_request(token),
        _linked_request(token, "IntentRequest", "ArrivalsIntent"),
        _linked_request(token, "IntentRequest", "DeparturesIntent"),
    ):
        s3.objects.pop(_object_key(), None)
        response = lambda_function.handle(event)
        _assert_one_audio(response["response"]["outputSpeech"]["ssml"])
    assert calls.count(API_ENDPOINT + "/v1/directives") == 3
    help_response = lambda_function.handle(
        {"request": {"type": "IntentRequest", "intent": {"name": "AMAZON.HelpIntent"}}}
    )
    assert "Checking the timetable" not in help_response["response"]["outputSpeech"]["ssml"]
    link = lambda_function.handle({"request": {"type": "LaunchRequest"}, "context": {}})
    assert link["response"]["card"]["type"] == "LinkAccount"
    assert calls.count(API_ENDPOINT + "/v1/directives") == 3


def test_a_train_answer_keeps_the_screen_text(monkeypatch):
    def fake_answer(*args, **kwargs):
        return (
            Spoken(plain=TEXT, ssml=FALLBACK, apl_body="R2 08:05"),
            {},
        )

    monkeypatch.setattr(lambda_function, "answer", fake_answer)
    monkeypatch.setattr(lambda_function, "get_schedule", lambda: type("S", (), {"covers": lambda self, day: True})())
    event = {
        "request": {"type": "LaunchRequest"},
        "context": {
            "System": {
                "user": {"accessToken": "v1.token", "userId": USER},
                "device": {"supportedInterfaces": {"Alexa.Presentation.APL": {}}},
            }
        },
    }
    monkeypatch.setattr(
        lambda_function,
        "read_token",
        lambda token: Account(choices=("33506",), api_key=""),
    )
    response = lambda_function.handle(event)
    ssml = response["response"]["outputSpeech"]["ssml"]
    assert ssml == FALLBACK
    assert "<audio" not in ssml
    body = response["response"]["directives"][0]["datasources"]["payload"]["body"]
    assert body == "R2 08:05"


def _assert_one_audio(ssml: str) -> None:
    validate_alexa_ssml(ssml)
    assert ssml.count("<audio") == 1
    assert ssml.startswith("<speak><audio ")
    assert ssml.endswith("/></speak>")

"""Speak a train answer with ElevenLabs, and keep Alexa's voice as the fallback.

The MP3 is cached in the Alexa-hosted S3 bucket under today's Europe/Warsaw
date. The hash is the voice, the model, and the text, so a clip can be reused
for anyone that day. Older days are deleted in the background. Help, errors,
and the link prompt never come through here: the handler speaks those with
SSML directly. The API key comes only from the linking token. Any failure,
including no key or a rejected key, returns the SSML the caller already
built. The key is not logged and is not part of an S3 key. There is no
per-user clip quota; a bring-your-own key spends that account's credits.
A cache miss tells Alexa to say "Checking the timetable" through the
Directive Service, in the Alexa voice, without waiting on that call.
The ElevenLabs POST is issued as soon as the sentence is known. The S3
HEAD and that directive run beside it. boto3 is imported on the HEAD
thread, not while the realtime feed is parsed: that import holds the GIL.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from upcoming_trains.constants import (
    ELEVENLABS_LANGUAGE,
    ELEVENLABS_MAX_AUDIO_BYTES,
    ELEVENLABS_MAX_AUDIO_SECONDS,
    ELEVENLABS_MIN_REQUEST_SECONDS,
    ELEVENLABS_MIN_WORDS_PER_MINUTE,
    ELEVENLABS_MODEL,
    ELEVENLABS_OUTPUT_FORMAT,
    PROGRESSIVE_TIMEOUT_SECONDS,
    ELEVENLABS_S3_RESERVE_SECONDS,
    ELEVENLABS_TIMEOUT_MARGIN_SECONDS,
    ELEVENLABS_URL_EXPIRY_SECONDS,
    ELEVENLABS_VOICE_ID,
    HANDLER_BUDGET_SECONDS,
)
from upcoming_trains.speech import _escape
from upcoming_trains.timeutil import warsaw_date
from upcoming_trains.timing import add as add_ms
from upcoming_trains.timing import mark_fallback, set_chars
from upcoming_trains.token import usable_key

UTC = timezone.utc
_CLIP_ROOT = "Media/tts/"
# At most one cleanup per container per Warsaw day. The delete itself runs
# off the request, after this flag is set.
_swept_for = ""
_sweep_lock = threading.Lock()

# Full-file convert, not /stream, and not a pipe into the S3 upload. Alexa
# plays one complete MP3, so the object has to exist before the URL is
# returned. A sentence is about 100 KB. Overlapping that put with generation
# would save only the tail of a small upload. The call's own limit is the
# handler time still left, not a fixed 3 seconds.
# The deprecated optimize_streaming_latency flag only shortens time-to-first-byte,
# and its strongest step turns off the text normalizer.
_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/" + ELEVENLABS_VOICE_ID
# How long the TTS worker waits for a cache HEAD before it opens the socket.
# A HEAD from us-east-1 takes about a third of a second, so this does not
# wait one out. An instant hit must not spend a credit. A later hit is what
# gets played, and that clip is not uploaded.
_HEAD_LEAD_SECONDS = 0.1
_MISSING = ("404", "NoSuchKey", "NotFound")
_handler_started = None
_s3 = None
_s3_lock = threading.Lock()
_warm_started = False
_probe = None
_probe_lock = threading.Lock()
_announce_lock = threading.Lock()


def note_handler_start() -> None:
    """Call at the top of lambda_handler. The ElevenLabs budget starts here."""
    global _handler_started, _probe
    _handler_started = time.monotonic()
    with _probe_lock:
        _probe = None


def handler_started():
    """monotonic time at the start of this invoke, or None outside a request."""
    return _handler_started


def _seconds_left() -> float | None:
    """Seconds left in the handler budget, or None when no request is timed."""
    if _handler_started is None:
        return None
    return HANDLER_BUDGET_SECONDS - (time.monotonic() - _handler_started)


def request_timeout() -> float | None:
    """How long the ElevenLabs HTTP call may block, or None to skip it.

    The limit is the handler time still left, minus the S3 upload reserve
    and a small margin. With no handler clock, the full handler budget is
    used. There is no separate fixed cap. Sample it when the sentence is
    known, not after the cache HEAD: a cold client build is not part of
    this wait.
    """
    left = _seconds_left()
    if left is None:
        left = float(HANDLER_BUDGET_SECONDS)
    available = left - ELEVENLABS_S3_RESERVE_SECONDS - ELEVENLABS_TIMEOUT_MARGIN_SECONDS
    if available < ELEVENLABS_MIN_REQUEST_SECONDS:
        return None
    return available


def narrate(
    text: str,
    user_id: str,
    fallback_ssml: str,
    now: datetime | None = None,
    api_key: str = "",
    progress=None,
) -> str:
    """Return one <audio> tag, or `fallback_ssml` when ElevenLabs is not used.

    `progress` is (apiEndpoint, apiAccessToken, requestId) from the Alexa
    request. It is used only when this call is about to generate a new clip.
    """
    set_chars(len((text or "").strip()))
    try:
        spoken = _narrate(
            text or "",
            user_id or "",
            fallback_ssml,
            now,
            usable_key(api_key),
            progress,
        )
    except Exception:
        mark_fallback("error")
        return fallback_ssml
    if not spoken or spoken == fallback_ssml:
        mark_fallback("error")
        return fallback_ssml
    return spoken


def cache_key(text: str) -> str:
    """sha256 of the voice, the model, and the spoken text."""
    material = ELEVENLABS_VOICE_ID + "\n" + ELEVENLABS_MODEL + "\n" + text
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def clip_object_key(text: str, now: datetime) -> str:
    """Today's object. Yesterday's prefix is never read."""
    day = warsaw_date(now).isoformat()
    return _CLIP_ROOT + day + "/" + cache_key(text) + ".mp3"


def _maybe_sweep(client, bucket: str, now: datetime) -> None:
    """Start one cleanup for this Warsaw day. The request does not wait."""
    global _swept_for
    today = warsaw_date(now).isoformat()
    with _sweep_lock:
        if _swept_for == today:
            return
        _swept_for = today
    _start_sweep(_sweep_old_clips, (client, bucket, today))


def _start_sweep(target, args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def _sweep_old_clips(client, bucket: str, today: str) -> None:
    """Delete earlier Warsaw days and leftover un-prefixed clips.

    Best effort. A failure is logged and does not touch today's prefix.
    """
    try:
        legacy = []
        old_prefixes = []
        for page in _list_pages(client, bucket, _CLIP_ROOT, "/"):
            for item in page.get("Contents") or []:
                key = item.get("Key") or ""
                if _is_legacy_clip(key):
                    legacy.append(key)
            for common in page.get("CommonPrefixes") or []:
                prefix = common.get("Prefix") or ""
                day = _day_from_prefix(prefix)
                if day and day < today:
                    old_prefixes.append(prefix)
        if legacy:
            _delete_keys(client, bucket, legacy)
        for prefix in old_prefixes:
            _delete_prefix(client, bucket, prefix)
    except Exception as exc:
        print("tts sweep failed: %s" % type(exc).__name__, flush=True)


def _delete_prefix(client, bucket: str, prefix: str) -> None:
    pending = []
    for page in _list_pages(client, bucket, prefix, ""):
        for item in page.get("Contents") or []:
            key = item.get("Key") or ""
            if not key:
                continue
            pending.append(key)
            if len(pending) >= 1000:
                _delete_keys(client, bucket, pending)
                pending = []
    if pending:
        _delete_keys(client, bucket, pending)


def _delete_keys(client, bucket: str, keys: list) -> None:
    result = client.delete_objects(
        Bucket=bucket,
        Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
    )
    errors = result.get("Errors") if isinstance(result, dict) else None
    if errors:
        print("tts sweep failed: delete", flush=True)


def _list_pages(client, bucket: str, prefix: str, delimiter: str):
    token = ""
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if delimiter:
            kwargs["Delimiter"] = delimiter
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs) or {}
        yield page
        if not page.get("IsTruncated"):
            return
        token = page.get("NextContinuationToken") or ""
        if not token:
            return


def _day_from_prefix(prefix: str) -> str:
    root = _CLIP_ROOT
    if not prefix.startswith(root) or not prefix.endswith("/"):
        return ""
    day = prefix[len(root) : -1]
    if len(day) != 10 or day[4] != "-" or day[7] != "-" or "/" in day:
        return ""
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return ""
    return day


def _is_legacy_clip(key: str) -> bool:
    """An MP3 stored before clips were grouped by day: Media/tts/<sha256>.mp3."""
    if not key.startswith(_CLIP_ROOT):
        return False
    name = key[len(_CLIP_ROOT) :]
    if "/" in name or not name.endswith(".mp3"):
        return False
    digest = name[: -len(".mp3")]
    if len(digest) != 64:
        return False
    for char in digest:
        if char not in "0123456789abcdef":
            return False
    return True


def audio_ssml(url: str) -> str:
    return '<speak><audio src="' + _escape(url) + '"/></speak>'


def _fail(reason: str, fallback_ssml: str) -> str:
    mark_fallback(reason)
    return fallback_ssml


def _narrate(
    text: str,
    _user_id: str,
    fallback_ssml: str,
    now: datetime | None,
    api_key: str,
    progress,
) -> str:
    text = text.strip()
    if not api_key:
        return _fail("no_key", fallback_ssml)
    if not text:
        return _fail("empty", fallback_ssml)
    bucket = os.environ.get("S3_PERSISTENCE_BUCKET") or ""
    if not bucket:
        return _fail("no_bucket", fallback_ssml)
    if _estimated_seconds(text) >= ELEVENLABS_MAX_AUDIO_SECONDS:
        return _fail("too_long", fallback_ssml)
    if _seconds_left() is not None and _seconds_left() <= 0:
        return _fail("deadline", fallback_ssml)
    moment = now or datetime.now(UTC)
    object_key = clip_object_key(text, moment)
    # The HEAD thread imports boto3 and checks the clip. Neither that import
    # nor the HEAD runs before the POST. The import holds the GIL, so doing
    # it on this thread used to shrink the socket budget.
    slot = _begin_head(text, bucket, object_key, moment)
    if slot["ready"].is_set() and slot.get("hit") is True:
        _finish_head(slot)
        return _play(_client_from(slot), bucket, object_key, fallback_ssml)
    timeout = request_timeout()
    if timeout is None:
        _finish_head(slot)
        if slot.get("hit") is True:
            return _play(_client_from(slot), bucket, object_key, fallback_ssml)
        return _fail("deadline", fallback_ssml)
    audio, reason = _generate_during_head(text, api_key, timeout, slot, progress)
    if slot.get("hit") is True:
        return _play(_client_from(slot), bucket, object_key, fallback_ssml)
    if audio and len(audio) >= ELEVENLABS_MAX_AUDIO_BYTES:
        return _fail("too_long", fallback_ssml)
    if not audio:
        return _fail(reason or "eleven_error", fallback_ssml)
    client = _client_from(slot)
    started = time.perf_counter()
    try:
        client.put_object(Bucket=bucket, Key=object_key, Body=audio, ContentType="audio/mpeg")
    except Exception:
        add_ms("s3_put_ms", (time.perf_counter() - started) * 1000.0)
        return _fail("s3_error", fallback_ssml)
    add_ms("s3_put_ms", (time.perf_counter() - started) * 1000.0)
    return _play(client, bucket, object_key, fallback_ssml)


def _client_from(slot):
    client = slot.get("client")
    if client is not None:
        return client
    return s3_client()


def _generate_during_head(text, api_key, timeout, slot, progress):
    """Start ElevenLabs while the HEAD is still open.

    The worker gives an instant hit `_HEAD_LEAD_SECONDS` to land, then opens
    the socket anyway. A hit that lands during the call is what gets played,
    and the generated bytes are discarded. "Checking the timetable" is sent
    only once the HEAD says the clip is new, and the handler does not wait
    for that POST or for a discarded call.
    """
    box = {
        "audio": None,
        "reason": "eleven_error",
        "ms": 0.0,
        "skipped": False,
        "done": threading.Event(),
    }

    def run() -> None:
        try:
            if not slot["ready"].is_set():
                slot["ready"].wait(_HEAD_LEAD_SECONDS)
            if slot.get("hit") is True:
                box["skipped"] = True
                box["reason"] = ""
                return
            # The head already said this clip is new. Say so before the
            # socket when the caller runs the directive inline (tests).
            # In the skill the directive is its own thread. An unknown
            # result (the HEAD failed) stays quiet and still tries TTS.
            if slot["ready"].is_set() and slot.get("hit") is False:
                _announce_once(slot, progress)
            started = time.perf_counter()
            box["audio"], box["reason"] = _synthesize(text, api_key, timeout)
            box["ms"] = (time.perf_counter() - started) * 1000.0
        except Exception:
            box["audio"], box["reason"] = None, "eleven_error"
        finally:
            box["done"].set()

    threading.Thread(target=run, daemon=True).start()
    _finish_head(slot)
    if slot.get("hit") is True:
        return None, ""
    if slot.get("hit") is False:
        _announce_once(slot, progress)
    box["done"].wait()
    add_ms("eleven_ms", box["ms"])
    return box["audio"], box["reason"]


def _announce_once(slot, progress) -> None:
    with _announce_lock:
        if slot.get("announced"):
            return
        slot["announced"] = True
    _announce(progress)


def _spawn(target, args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def _announce(progress) -> None:
    """Alexa voice, only on the way into a new ElevenLabs clip. Failures are ignored."""
    if not progress:
        return
    try:
        endpoint, access_token, request_id = progress
    except (TypeError, ValueError):
        return
    url = _directive_url(endpoint)
    if not url or not isinstance(access_token, str) or not access_token.strip():
        return
    if not isinstance(request_id, str) or not request_id.strip():
        return
    _spawn(_post_progressive, (url, access_token.strip(), request_id.strip()))


def _directive_url(endpoint) -> str:
    if not isinstance(endpoint, str):
        return ""
    try:
        parts = urllib.parse.urlsplit(endpoint.strip())
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password:
        return ""
    if host != "api.amazonalexa.com" and not host.endswith(".amazonalexa.com"):
        return ""
    return "https://" + parts.netloc + "/v1/directives"


def _post_progressive(url: str, access_token: str, request_id: str) -> None:
    payload = json.dumps(
        {
            "header": {"requestId": request_id},
            "directive": {
                "type": "VoicePlayer.Speak",
                "speech": "<speak>Checking the timetable</speak>",
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=PROGRESSIVE_TIMEOUT_SECONDS) as response:
            response.read()
    except Exception:
        return


def warm_client() -> None:
    """Import boto3 on a side thread. Do not call this during the realtime parse.

    The import is pure Python and holds the GIL. On a cold start it ran
    beside the first walk of a feed that was already in memory, and that
    walk showed up as realtime_ms with realtime=cached. The narrate path
    builds the client on the HEAD thread, which overlaps ElevenLabs instead.
    The client is created once and reused. A failure is retried by the next
    s3_client() call.
    """
    global _warm_started
    if _s3 is not None:
        return
    with _s3_lock:
        if _warm_started or _s3 is not None:
            return
        _warm_started = True
    threading.Thread(target=_warm, daemon=True).start()


def _warm() -> None:
    try:
        s3_client()
    except Exception:
        global _warm_started
        with _s3_lock:
            _warm_started = False


def probe_cache(text: str, now: datetime | None = None) -> None:
    """HEAD the clip for this text. Used when the final sentence is already known.

    No bucket, or an empty sentence, does nothing. The result is used only
    when narrate is asked for this same text. `now` picks the Warsaw day.
    This is not started while the realtime feed is being parsed.
    """
    global _probe
    sentence = (text or "").strip()
    bucket = os.environ.get("S3_PERSISTENCE_BUCKET") or ""
    if not sentence or not bucket:
        return
    moment = now or datetime.now(UTC)
    slot = _new_slot(sentence)
    with _probe_lock:
        _probe = slot
    _start_head(slot, bucket, clip_object_key(sentence, moment), moment)


def _new_slot(text: str) -> dict:
    return {
        "text": text,
        "hit": None,
        "client": None,
        "ms": 0.0,
        "ready": threading.Event(),
        "noted": False,
        "announced": False,
    }


def _begin_head(text: str, bucket: str, object_key: str, moment: datetime):
    """The HEAD already running for this sentence, or a new one.

    The client is built on this thread. Callers start ElevenLabs without
    waiting for it.
    """
    with _probe_lock:
        slot = _probe
    if slot and slot.get("text") == text:
        return slot
    slot = _new_slot(text)
    _start_head(slot, bucket, object_key, moment)
    return slot


def _start_head(slot, bucket: str, object_key: str, moment: datetime) -> None:
    def run() -> None:
        started = time.perf_counter()
        try:
            client = s3_client()
            slot["client"] = client
            _maybe_sweep(client, bucket, moment)
            # cache_ms is the HEAD itself. The client build overlaps TTS and
            # is not part of that figure.
            started = time.perf_counter()
            slot["hit"] = _exists(client, bucket, object_key)
        except Exception:
            slot["hit"] = None
        finally:
            slot["ms"] = (time.perf_counter() - started) * 1000.0
            slot["ready"].set()

    threading.Thread(target=run, daemon=True).start()


def _finish_head(slot, timeout=None) -> bool:
    """Wait for the HEAD. `cache_ms` is the check's own time, counted once."""
    if timeout is None:
        slot["ready"].wait()
        ready = True
    else:
        ready = bool(slot["ready"].wait(timeout))
    if ready and not slot.get("noted"):
        slot["noted"] = True
        add_ms("cache_ms", float(slot.get("ms") or 0.0))
    return ready


def _play(client, bucket: str, object_key: str, fallback_ssml: str) -> str:
    try:
        spoken = _audio_or_fallback(client, bucket, object_key, fallback_ssml)
    except Exception:
        return _fail("s3_error", fallback_ssml)
    if not spoken or spoken == fallback_ssml:
        return _fail("s3_error", fallback_ssml)
    return spoken


def s3_client():
    """The hosted-skill client: SigV4 and path-style, same as the sample util.

    boto3 is imported here, not at module import, and not while the realtime
    feed is parsed. The HEAD thread is the one that calls this, beside the
    ElevenLabs socket.
    """
    global _s3
    if _s3 is not None:
        return _s3
    with _s3_lock:
        if _s3 is None:
            _s3 = _build_s3()
        return _s3


def _build_s3():
    import boto3
    from botocore.config import Config

    kwargs = {
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    }
    region = os.environ.get("S3_PERSISTENCE_REGION") or ""
    if region:
        kwargs["region_name"] = region
    return boto3.client("s3", **kwargs)


def _audio_or_fallback(client, bucket: str, object_key: str, fallback_ssml: str) -> str:
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": object_key},
        ExpiresIn=ELEVENLABS_URL_EXPIRY_SECONDS,
    )
    if not url or not str(url).startswith("https://"):
        return fallback_ssml
    return audio_ssml(str(url))


def _synthesize(text: str, api_key: str, timeout: float):
    """Return (mp3, "") or (None, reason). The body of an error is discarded."""
    if not api_key:
        return None, "no_key"
    query = urllib.parse.urlencode({"output_format": ELEVENLABS_OUTPUT_FORMAT})
    payload = json.dumps(
        {
            "text": text,
            "model_id": ELEVENLABS_MODEL,
            "language_code": ELEVENLABS_LANGUAGE,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _TTS_URL + "?" + query,
        data=payload,
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type") or ""
            body = response.read()
    except urllib.error.HTTPError as exc:
        # 401, 402, and quota errors are a rejected key. Any other HTTP
        # failure also falls back. The body is discarded, not logged.
        _drop_error_body(exc)
        return None, _http_reason(getattr(exc, "code", None))
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
            return None, "eleven_timeout"
        return None, "eleven_error"
    except socket.timeout:
        return None, "eleven_timeout"
    except (OSError, ValueError):
        return None, "eleven_error"
    if not body:
        return None, "eleven_error"
    if "audio" not in content_type.lower() and not _looks_like_audio(body):
        return None, "eleven_error"
    return body, ""


def _http_reason(code) -> str:
    try:
        number = int(code)
    except (TypeError, ValueError):
        return "eleven_http"
    if number < 100 or number > 599:
        return "eleven_http"
    return "eleven_http_%d" % number


def _drop_error_body(exc: urllib.error.HTTPError) -> None:
    try:
        exc.read()
    except Exception:
        return


def _looks_like_audio(body: bytes) -> bool:
    return body.startswith(b"ID3") or body[:2] == b"\xff\xfb" or body[:2] == b"\xff\xf3"


def _estimated_seconds(text: str) -> float:
    words = len(text.split())
    if not words:
        return 0.0
    return words * 60.0 / ELEVENLABS_MIN_WORDS_PER_MINUTE


def _exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _is_missing(exc):
            return False
        raise
    return True


def _is_missing(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str((response.get("Error") or {}).get("Code") or "")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in _MISSING or status == 404

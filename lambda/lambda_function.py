"""Alexa-hosted entry point.

The file name and the handler `lambda_handler` are what the hosted Python
runtime looks for. There are no third-party packages: the station list arrives
in the account-linking token, and the timetable is a file next to this code.
"""

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone

_BOOT = time.perf_counter()

from upcoming_trains.answer import answer
from upcoming_trains.schedule import default_path, load
from upcoming_trains.speech import (
    apl_directive,
    change_message,
    expired_message,
    fallback_message,
    help_message,
    link_message,
    plain_ssml,
    unavailable_message,
)
from upcoming_trains.timeutil import warsaw_date
from upcoming_trains.timing import begin as begin_timing
from upcoming_trains.timing import emit as emit_timing
from upcoming_trains.token import TokenError, read_token, usable_key
from upcoming_trains.voice import narrate, note_handler_start, warm_client
from upcoming_trains.when import WhenError, resolve_when

UTC = timezone.utc
_SCHEDULE = None


def get_schedule():
    """The parsed index. Loaded once per process."""
    global _SCHEDULE
    if _SCHEDULE is None:
        path = os.environ.get("SCHEDULE_PATH") or default_path()
        started = time.perf_counter()
        _SCHEDULE = load(path)
        get_schedule.last_load_seconds = time.perf_counter() - started
    return _SCHEDULE


get_schedule.last_load_seconds = None


def _preload_schedule():
    """Read the index at import, outside the first request's handler body."""
    try:
        get_schedule()
    except Exception:
        traceback.print_exc()


def _warm_lambda() -> None:
    """On a hosted invoke, start the feed and boto3 during the cold import.

    Local tests leave AWS_LAMBDA_FUNCTION_NAME unset, so they do not open
    the feed or import boto3 just by importing this module.
    """
    if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return
    try:
        from upcoming_trains.realtime import prefetch

        prefetch()
        warm_client()
    except Exception:
        traceback.print_exc()


_warm_lambda()
_preload_schedule()
_IMPORT_MS = (time.perf_counter() - _BOOT) * 1000.0


def lambda_handler(event, context):
    note_handler_start()
    begin_timing(_IMPORT_MS, (get_schedule.last_load_seconds or 0) * 1000.0)
    try:
        return handle(event)
    except Exception:
        traceback.print_exc()
        return _response(
            plain_ssml("Sorry, I couldn't read the timetable. Please try again in a moment.")
        )
    finally:
        emit_timing()


def handle(event: dict) -> dict:
    request = event.get("request") or {}
    kind = request.get("type")
    if kind == "CanFulfillIntentRequest":
        return _can_fulfill(request.get("intent") or {})
    if kind == "LaunchRequest":
        return upcoming(event, "departure")
    if kind == "IntentRequest":
        name = (request.get("intent") or {}).get("name")
        # UpcomingTrainsIntent is the previous model name. A device that has
        # not picked up the new interaction model still gets departures.
        if name in ("DeparturesIntent", "UpcomingTrainsIntent"):
            return upcoming(event, "departure")
        if name == "ArrivalsIntent":
            return upcoming(event, "arrival")
        if name == "ChangeStationsIntent":
            return _response(plain_ssml(change_message()), card=True)
        if name == "AMAZON.HelpIntent":
            return _response(plain_ssml(help_message()))
        if name in ("AMAZON.CancelIntent", "AMAZON.StopIntent", "AMAZON.NavigateHomeIntent"):
            return _response(plain_ssml("Goodbye."))
        if name == "AMAZON.FallbackIntent":
            return _response(plain_ssml(fallback_message()))
        return _response(plain_ssml(fallback_message()))
    if kind == "SessionEndedRequest":
        return {"version": "1.0", "response": {}}
    return _response(plain_ssml(fallback_message()))


def upcoming(event: dict, kind: str = "departure") -> dict:
    token = _access_token(event)
    if not token:
        return _response(plain_ssml(link_message()), card=True)
    try:
        linked = read_token(token)
    except TokenError:
        return _response(plain_ssml(link_message()), card=True)

    if usable_key(linked.api_key):
        warm_client()

    now = datetime.now(UTC)
    intent = (event.get("request") or {}).get("intent") or {}
    try:
        window = resolve_when(now, _slot_value(intent, "time"), _slot_value(intent, "date"))
    except WhenError as exc:
        return _response(plain_ssml(str(exc)))

    schedule = get_schedule()
    if window is None:
        today = warsaw_date(now)
        if not schedule.covers(today):
            text = expired_message(schedule.feed_start, schedule.feed_end, today)
            return _response(plain_ssml(text))
    elif not schedule.covers(warsaw_date(window)):
        text = unavailable_message(schedule.feed_start, schedule.feed_end, warsaw_date(window))
        return _response(plain_ssml(text))

    spoken, _status = answer(
        schedule, list(linked.choices), now, fetch=True, kind=kind, window_start=window
    )
    title = spoken.apl_title or ("Arrivals" if kind == "arrival" else "Departures")
    directives = [apl_directive(title, spoken.apl_body)] if _supports_apl(event) else None
    ssml = narrate(
        spoken.plain,
        _user_id(event),
        spoken.ssml,
        now,
        linked.api_key,
        _progress_context(event),
    )
    return _response(ssml, directives=directives)


def _can_fulfill(intent: dict) -> dict:
    """Yes for departures and arrivals, including a time or a date. No otherwise."""
    name = intent.get("name") or ""
    if name not in ("DeparturesIntent", "ArrivalsIntent"):
        return {"version": "1.0", "response": {"canFulfillIntent": {"canFulfill": "NO"}}}
    body = {"canFulfill": "YES"}
    slots = intent.get("slots") or {}
    answered = {}
    if isinstance(slots, dict):
        for slot_name in slots:
            if slot_name:
                answered[slot_name] = {"canUnderstand": "YES", "canFulfill": "YES"}
    if answered:
        body["slots"] = answered
    return {"version": "1.0", "response": {"canFulfillIntent": body}}


def _slot_value(intent: dict, name: str) -> str:
    slots = intent.get("slots") or {}
    slot = slots.get(name) if isinstance(slots, dict) else None
    if not isinstance(slot, dict):
        return ""
    value = slot.get("value")
    if value is None:
        return ""
    return str(value).strip()


def _progress_context(event: dict):
    """Endpoint, token, and request id for one progressive response. Or None."""
    try:
        system = event["context"]["System"]
        request = event.get("request") or {}
        return (
            system.get("apiEndpoint") or "",
            system.get("apiAccessToken") or "",
            request.get("requestId") or "",
        )
    except Exception:
        return None


def _user_id(event: dict) -> str:
    try:
        return event["context"]["System"]["user"].get("userId") or ""
    except Exception:
        return ""


def _access_token(event: dict) -> str:
    try:
        return event["context"]["System"]["user"].get("accessToken") or ""
    except Exception:
        return ""


def _supports_apl(event: dict) -> bool:
    try:
        interfaces = event["context"]["System"]["device"]["supportedInterfaces"]
    except Exception:
        return False
    return isinstance(interfaces, dict) and "Alexa.Presentation.APL" in interfaces


def _response(ssml: str, card: bool = False, directives: list | None = None) -> dict:
    body = {
        "outputSpeech": {"type": "SSML", "ssml": ssml},
        "shouldEndSession": True,
    }
    if card:
        body["card"] = {"type": "LinkAccount"}
    if directives:
        body["directives"] = directives
    return {"version": "1.0", "response": body}

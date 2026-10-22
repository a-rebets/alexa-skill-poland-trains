"""One timing line per request. No token, no key, no station list.

`import_ms` is the cold module import and includes `schedule_ms`.
`total_ms` is the handler only, from the start of `lambda_handler`.
A warm request reports cold=0 and the import figures from this process,
which were already paid before the handler.
`fb` is why the Alexa voice was used (`none` when ElevenLabs spoke).
`chars` is the length of the TTS sentence, not the sentence itself.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_current = None
_seen_request = False


def begin(import_ms: float, schedule_ms: float) -> None:
    """Start a request. The first call in the process is the cold one."""
    global _current, _seen_request
    with _lock:
        cold = 0 if _seen_request else 1
        _seen_request = True
        _current = {
            "cold": cold,
            "import_ms": float(import_ms or 0),
            "schedule_ms": float(schedule_ms or 0),
            "realtime_ms": 0.0,
            "realtime": "off",
            "board_ms": 0.0,
            "cache_ms": 0.0,
            "eleven_ms": 0.0,
            "s3_put_ms": 0.0,
            "fallback": 0,
            "fb": "none",
            "chars": 0,
            "t0": time.perf_counter(),
        }


def add(name: str, milliseconds: float) -> None:
    with _lock:
        if _current is None or name not in _current:
            return
        _current[name] = float(_current[name]) + float(milliseconds)


def set_realtime(kind: str) -> None:
    """`fresh` (new body), `cached` (memory or 304), `missed`, or `off`."""
    if kind not in ("fresh", "cached", "missed", "off"):
        return
    with _lock:
        if _current is not None:
            _current["realtime"] = kind


def mark_fallback(reason: str = "") -> None:
    """Record a fallback. The first reason wins; later calls do not replace it."""
    cleaned = _reason(reason)
    with _lock:
        if _current is None:
            return
        _current["fallback"] = 1
        if _current.get("fb") in (None, "", "none"):
            _current["fb"] = cleaned or "error"


def set_chars(count: int) -> None:
    """Character count of the sentence sent to TTS. The text stays out of the log."""
    with _lock:
        if _current is None:
            return
        try:
            _current["chars"] = max(0, int(count))
        except (TypeError, ValueError):
            return


def _reason(reason: str) -> str:
    text = str(reason or "").strip().lower()
    if not text or len(text) > 32:
        return ""
    for char in text:
        if char not in "abcdefghijklmnopqrstuvwxyz0123456789_":
            return ""
    return text


def emit() -> None:
    """Print the single timing line. Safe to call when no request was started."""
    with _lock:
        current = _current
    if not current:
        return
    total = (time.perf_counter() - current["t0"]) * 1000.0
    print(
        "timing cold=%d import_ms=%.0f schedule_ms=%.0f realtime_ms=%.0f "
        "realtime=%s board_ms=%.0f cache_ms=%.0f eleven_ms=%.0f s3_put_ms=%.0f "
        "total_ms=%.0f fallback=%d fb=%s chars=%d"
        % (
            current["cold"],
            current["import_ms"],
            current["schedule_ms"],
            current["realtime_ms"],
            current["realtime"],
            current["board_ms"],
            current["cache_ms"],
            current["eleven_ms"],
            current["s3_put_ms"],
            total,
            current["fallback"],
            current.get("fb") or "none",
            int(current.get("chars") or 0),
        ),
        flush=True,
    )

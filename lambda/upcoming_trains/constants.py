"""Tunable limits shared by the schedule, the board, and the token."""

# How far ahead the spoken board looks.
WINDOW_MINUTES = 60

# A train scheduled this long ago can still be departing if it is delayed.
MAX_LATE_MINUTES = 120

# A train scheduled this far ahead can already be inside the hour if it is early.
MAX_EARLY_MINUTES = 15

# Include a train that is leaving essentially as we speak.
DEPARTURE_GRACE_SECONDS = 30

# The next train and the one after it, when a direction has one or two lines.
# A direction with more lines speaks each line once. Long-distance brands
# (Intercity, RegioJet, Leo, Arriva) are one group under the same rule, so a
# single brand is still at most two times.
MAX_TIMES_PER_LINE = 2

# The picker stops at 5. The decoder allows a few more so a rebuilt page
# does not need a skill release on the same day.
MAX_STATIONS_IN_TOKEN = 8

# The publisher's realtime file sometimes freezes for about an hour.
REALTIME_MAX_AGE_SECONDS = 75 * 60
REALTIME_FUTURE_SKEW_SECONDS = 5 * 60

REALTIME_URL = "https://mkuran.pl/gtfs/polish_trains/updates.pb"
GTFS_ZIP_URL = "https://mkuran.pl/gtfs/polish_trains.zip"

# Implicit-grant redirect hosts. Authorization-code grant uses a different path
# (/api/skill/link/...) and is intentionally not accepted.
ALEXA_REDIRECT_HOSTS = (
    "pitangui.amazon.com",
    "layla.amazon.com",
    "alexa.amazon.co.jp",
)
ALEXA_REDIRECT_PATH = "/spa/skill/account-linking-status.html"

TOKEN_VERSION = "v1"
CLIENT_ID = "upcoming-trains"
PICKER_MAX_STATIONS = 5

# ElevenLabs Sarah, the premade voice "Sarah - Mature, Reassuring, Confident".
# https://elevenlabs.io/docs/api-reference/text-to-speech/convert
ELEVENLABS_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
ELEVENLABS_MODEL = "eleven_v4_turbo"
ELEVENLABS_LANGUAGE = "en"
ELEVENLABS_OUTPUT_FORMAT = "mp3_24000_48"
# Alexa stops waiting at about 8 seconds, measured from the invoke, which
# includes runtime startup before lambda_handler and the trip back after
# it returns. import_ms on a cold start is about 0.1 s and is outside this
# budget. 7.5 s leaves 0.5 s for that startup, the import, and the response.
# The previous 7.0 s budget reserved a full second. A cold ~230 character
# answer then had about 3.5 s left for ElevenLabs after the 0.6 s upload
# reserve and the 0.2 s margin, and the call needs about 4 s. 7.8 s would
# leave too little for an unmeasured runtime bootstrap on top of the 8 s
# limit. The live feed may block the handler for at most
# REALTIME_WAIT_SECONDS; a late feed becomes scheduled times. ElevenLabs
# may use whatever is still left after the upload reserve and a small
# margin. A fixed 3 second cap was too short for a full hour: from
# us-east-1 the call takes about 2.6 seconds, and a longer sentence was
# cut off at 3 with budget left.
HANDLER_BUDGET_SECONDS = 7.5
# The handler stops waiting this long after it starts. DNS, connect, TLS,
# headers, and the body all count. A late file is scheduled times for this
# request. The download itself keeps running and can fill the cache later.
REALTIME_WAIT_SECONDS = 1.5
# Inactivity limit for the background socket only. It is not the handler cap.
# A steady transfer may take longer than this; a stalled socket may not.
REALTIME_SOCKET_TIMEOUT_SECONDS = 8.0
# Reuse one downloaded protobuf this long, then revalidate with If-None-Match.
# The file usually changes every 30–60 s, so a warm request skips the body.
REALTIME_CACHE_SECONDS = 60
# Held back from the ElevenLabs call so the MP3 can still be stored.
# Observed puts from us-east-1 are about 0.4 seconds.
ELEVENLABS_S3_RESERVE_SECONDS = 0.6
# Slack after the upload for the presigned URL and the response itself.
ELEVENLABS_TIMEOUT_MARGIN_SECONDS = 0.2
ELEVENLABS_MIN_REQUEST_SECONDS = 0.4
# Spoken in the Alexa voice while ElevenLabs generates. Must not block the handler.
PROGRESSIVE_TIMEOUT_SECONDS = 1
ELEVENLABS_URL_EXPIRY_SECONDS = 3600

# Alexa allows one response less than 240 seconds of audio, and one audio tag.
ELEVENLABS_MAX_AUDIO_SECONDS = 240
# mp3_24000_48 is 48 kbps, so this many bytes is exactly 240 seconds.
ELEVENLABS_MAX_AUDIO_BYTES = ELEVENLABS_MAX_AUDIO_SECONDS * 48000 // 8
# Skip the API call when even a slow reading would pass the Alexa cap.
ELEVENLABS_MIN_WORDS_PER_MINUTE = 140

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
# includes a short runtime startup before this process runs. The handler
# budget is 7 seconds from lambda_handler. That leaves about a second for
# the runtime startup and the trip back to Alexa. The live feed may block
# the handler for at most REALTIME_WAIT_SECONDS; a late feed becomes
# scheduled times. ElevenLabs still never waits longer than 3 seconds, and
# the S3 put keeps a small reserve after it.
HANDLER_BUDGET_SECONDS = 7.0
ELEVENLABS_TIMEOUT_SECONDS = 3
# How long the handler waits for updates.pb. The socket itself may run a
# little longer in the background so a late file can still fill the 30 s cache.
REALTIME_WAIT_SECONDS = 1.5
REALTIME_SOCKET_TIMEOUT_SECONDS = 3.0
REALTIME_CACHE_SECONDS = 30
ELEVENLABS_S3_RESERVE_SECONDS = 0.5
ELEVENLABS_MIN_REQUEST_SECONDS = 0.4
# Spoken in the Alexa voice while ElevenLabs generates. Must not block the handler.
PROGRESSIVE_TIMEOUT_SECONDS = 1
ELEVENLABS_URL_EXPIRY_SECONDS = 3600

# New clips only. A cached clip does not count, and it still plays after a limit.
# Each person pays for their own key, so the cap is per user per Warsaw day.
ELEVENLABS_DAILY_GENERATIONS = 10
ELEVENLABS_DAILY_CHARS = 15000

# Alexa allows one response less than 240 seconds of audio, and one audio tag.
ELEVENLABS_MAX_AUDIO_SECONDS = 240
# mp3_24000_48 is 48 kbps, so this many bytes is exactly 240 seconds.
ELEVENLABS_MAX_AUDIO_BYTES = ELEVENLABS_MAX_AUDIO_SECONDS * 48000 // 8
# Skip the API call when even a slow reading would pass the Alexa cap.
ELEVENLABS_MIN_WORDS_PER_MINUTE = 140

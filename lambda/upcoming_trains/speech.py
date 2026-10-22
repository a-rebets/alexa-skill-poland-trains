"""Turn a board into the sentence Alexa speaks, plus a screen copy.

Departures are grouped by the next stop, arrivals by the previous stop, and
then by the line code locals use (R3, S1, RL). Clock times stay on the screen. The coming hour is spoken as "in N
minutes". A train in the current minute is "R one — now": the em dash is a
short pause so the line code does not run into "now". Alexa uses a 150 ms
break in that spot. A named hour is spoken as clock words ("eight oh five"),
and the intro carries the only am or pm.

Alexa SSML, reference updated 28 August 2025, has no pl-PL locale and none of
the Polish Polly voices (Ewa, Maja, Jacek, Jan). POLISH_VOICE therefore stays
empty. The wrapper emits a voice only when both the voice and the locale are
on those allowlists, so setting Ewa still cannot produce invalid SSML.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from upcoming_trains.board import StationBoard, Train
from upcoming_trains.constants import MAX_TIMES_PER_LINE
from upcoming_trains.timeutil import clock_hhmm, speak_date, utc_to_warsaw_wall, warsaw_date

# Empty disables the wrapper. A supported voice name turns it on.
POLISH_VOICE = ""
POLISH_LANG = "pl-PL"

# Supported Amazon Polly voices for Alexa skills (not the full Polly catalog).
ALEXA_VOICES = frozenset(
    {
        "Ivy",
        "Joanna",
        "Joey",
        "Justin",
        "Kendra",
        "Kimberly",
        "Matthew",
        "Salli",
        "Nicole",
        "Russell",
        "Amy",
        "Brian",
        "Emma",
        "Aditi",
        "Raveena",
        "Geraint",
        "Chantal",
        "Celine",
        "Lea",
        "Mathieu",
        "Hans",
        "Marlene",
        "Vicki",
        "Carla",
        "Giorgio",
        "Bianca",
        "Mizuki",
        "Takumi",
        "Vitoria",
        "Camila",
        "Ricardo",
        "Penelope",
        "Lupe",
        "Miguel",
        "Conchita",
        "Enrique",
        "Lucia",
        "Mia",
    }
)

# Locales accepted on <lang xml:lang>. pl-PL is not one of them.
ALEXA_LANGS = frozenset(
    {
        "de-DE",
        "en-AU",
        "en-CA",
        "en-GB",
        "en-IN",
        "en-US",
        "es-ES",
        "es-MX",
        "es-US",
        "fr-CA",
        "fr-FR",
        "hi-IN",
        "it-IT",
        "ja-JP",
        "pt-BR",
    }
)

# Strip "<City> " only when the official name continues after it.
# "Kraków Główny" becomes "Główny". "Gdynia" alone stays "Gdynia".
CITY_PREFIXES = (
    "Warszawa",
    "Kraków",
    "Łódź",
    "Wrocław",
    "Poznań",
    "Gdańsk",
    "Gdynia",
    "Sopot",
    "Szczecin",
    "Katowice",
    "Lublin",
    "Bydgoszcz",
)

# Product brands in route_short_name, not a line a local would quote.
_INTERCITY = frozenset({"IC", "EIP", "EIC", "EC", "TLK", "ICN", "MP", "EN"})
_REGIONAL = frozenset({"REG", "REG P", "IR"})

_FOLD = {
    "Ł": "L",
    "ł": "L",
    "Ś": "S",
    "ś": "S",
    "Ą": "A",
    "ą": "A",
    "Ć": "C",
    "ć": "C",
    "Ę": "E",
    "ę": "E",
    "Ń": "N",
    "ń": "N",
    "Ó": "O",
    "ó": "O",
    "Ź": "Z",
    "ź": "Z",
    "Ż": "Z",
    "ż": "Z",
}


@dataclass(frozen=True)
class Spoken:
    plain: str
    ssml: str
    apl_body: str
    apl_title: str = ""


SKILL_NAME = "My Train Schedule"


@dataclass
class _LineGroup:
    display: str
    spoken: str
    pairs: list
    cancelled: bool
    first: datetime


@dataclass
class _DirectionGroup:
    name: str
    wrap: bool
    first: datetime
    lines: dict


def render(
    boards: list[StationBoard],
    note: str,
    now: datetime,
    kind: str = "departure",
    window_start: datetime | None = None,
) -> Spoken:
    clocks = window_start is not None
    intro_plain = ""
    intro_screen = ""
    apl_title = ""
    if clocks:
        intro_plain, intro_screen = window_phrases(kind, window_start, now)
        apl_title = intro_screen[:-1] if intro_screen.endswith(".") else intro_screen
    plain_parts: list[str] = []
    ssml_parts: list[str] = []
    apl_parts: list[str] = []
    for board in boards:
        plain, markup, screen = _station_block(board, now, kind, clocks)
        plain_parts.append(plain)
        ssml_parts.append(markup)
        apl_parts.append(screen)
    if not plain_parts:
        empty = "You have not chosen any stations yet."
        plain_parts.append(empty)
        ssml_parts.append(empty)
        apl_parts.append(empty)
    if note:
        plain_parts.append(note)
        ssml_parts.append(_escape(note))
        apl_parts.append(note)
    if intro_plain:
        plain_parts.insert(0, intro_plain)
        ssml_parts.insert(0, _escape(intro_plain))
        apl_parts.insert(0, intro_screen)
    plain = " ".join(plain_parts)
    ssml = "<speak>" + ' <break time="400ms"/> '.join(ssml_parts) + "</speak>"
    apl_body = "\n\n".join(apl_parts).replace("${", "{")
    return Spoken(plain=plain, ssml=ssml, apl_body=apl_body, apl_title=apl_title)


def station_city(name: str) -> str:
    """Listed city when the stop is that city or starts with "<City> "."""
    text = (name or "").strip()
    for city in CITY_PREFIXES:
        if text == city or text.startswith(city + " "):
            return city
    return ""


def local_name(name: str, home_city: str = "") -> str:
    """Drop "<home city> " when the stop continues after it.

    A direction in another city keeps its full name, so Poznań Główny and
    Kraków Główny stay distinct. The bare city ("Gdynia") stays as it is.
    """
    text = (name or "").strip()
    if not home_city:
        return text
    prefix = home_city + " "
    if text.startswith(prefix):
        rest = text[len(prefix) :].strip()
        if rest:
            return rest
    return text


def english_number(value: int) -> str:
    """English words for a small integer. 33 is "thirty-three", 2 is "two"."""
    number = int(value)
    if number < 0:
        return "minus " + english_number(-number)
    if number < 20:
        return _NUMBER_ONES[number]
    if number < 100:
        tens, ones = divmod(number, 10)
        if ones == 0:
            return _NUMBER_TENS[tens]
        return _NUMBER_TENS[tens] + "-" + _NUMBER_ONES[ones]
    if number < 1000:
        hundreds, rest = divmod(number, 100)
        if rest == 0:
            return _NUMBER_ONES[hundreds] + " hundred"
        return _NUMBER_ONES[hundreds] + " hundred " + english_number(rest)
    return str(number)


_NUMBER_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_NUMBER_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
)


def line_label(route: str, agency: str, bus: bool) -> tuple:
    """Return (screen label, spoken label).

    R2 is spoken "R two" and RL is spoken "R L". The digits are one English
    number, so S40 is "S forty". Long-distance brands collapse to "Intercity"
    or "regional". A replacement bus keeps a real line code ("R three bus")
    and otherwise says "bus".
    """
    code, marked_bus = _strip_zka(route or "")
    replacement = bool(bus or marked_bus)
    kind, display, spoken = _classify(code, agency)
    if replacement:
        if kind == "line":
            return display, spoken + " bus"
        return "bus", "bus"
    return display, spoken


def expired_message(feed_start, feed_end, today) -> str:
    return (
        f"The timetable built into this skill covers {speak_date(feed_start)} "
        f"through {speak_date(feed_end)}, and today is {speak_date(today)}. "
        "Rebuild the schedule on your computer and deploy the skill again."
    )


def link_message() -> str:
    return (
        "Pick your stations in the Alexa app. I sent a card to open the station list. "
        "Saying the Polish names doesn't work reliably on an English Echo."
    )


def change_message() -> str:
    return (
        "To change stations, disable My Train Schedule in the Alexa app and enable it again. "
        "That opens the station list. I also sent a link account card."
    )


def help_message() -> str:
    return (
        "My Train Schedule reads the next hour. "
        "Say departures for trains leaving, or arrivals for trains coming in. "
        "You can add a time, such as departures at eight am."
    )


def unavailable_message(feed_start, feed_end, day) -> str:
    """A requested day sits outside the built timetable."""
    coverage = f"This skill covers {speak_date(feed_start)} through {speak_date(feed_end)}."
    if day > feed_end:
        return "The timetable isn't available yet. " + coverage
    return f"The timetable doesn't cover {speak_date(day)}. " + coverage


def fallback_message() -> str:
    return "You can ask for departures or for arrivals."


def apl_directive(title: str, body: str) -> dict:
    return {
        "type": "Alexa.Presentation.APL.RenderDocument",
        "token": "upcoming-trains-board",
        "document": {
            "type": "APL",
            "version": "1.8",
            "mainTemplate": {
                "parameters": ["payload"],
                "items": [
                    {
                        "type": "Container",
                        "width": "100%",
                        "height": "100%",
                        "paddingLeft": "24dp",
                        "paddingRight": "24dp",
                        "paddingTop": "16dp",
                        "paddingBottom": "16dp",
                        "backgroundColor": "#14222b",
                        "items": [
                            {
                                "type": "Text",
                                "text": SKILL_NAME,
                                "color": "#f4efe6",
                                "fontSize": "16dp",
                            },
                            {
                                "type": "Text",
                                "text": "${payload.title}",
                                "color": "#f4efe6",
                                "fontSize": "28dp",
                                "fontWeight": "bold",
                            },
                            {
                                "type": "Text",
                                "text": "${payload.body}",
                                "color": "#f4efe6",
                                "fontSize": "20dp",
                                "spacing": "12dp",
                            },
                        ],
                    }
                ],
            },
        },
        "datasources": {"payload": {"title": title, "body": body}},
    }


def plain_ssml(text: str) -> str:
    return "<speak>" + _escape(text) + "</speak>"


def _station_block(board: StationBoard, now: datetime, kind: str, clocks: bool = False) -> tuple:
    if not board.known:
        line = "I don't have that station in this timetable."
        return line, _escape(line), line
    home_city = station_city(board.name or "")
    local = local_name(board.name or "", home_city) or "This"
    empty = "No trains in that hour." if clocks else "No trains in the next hour."
    if not board.trains:
        line = f"{local} station. {empty}"
        return line, _ssml_name(local) + " station. " + empty, line
    groups, long_lines = _directions(board.trains, now, kind, home_city)
    if not groups and not long_lines:
        line = f"{local} station. {empty}"
        return line, _ssml_name(local) + " station. " + empty, line
    plain_bits = [_direction_plain(group, kind, clocks) for group in groups]
    ssml_bits = [_direction_ssml(group, kind, clocks) for group in groups]
    screen_bits = [_direction_screen(group, kind) for group in groups]
    for line in long_lines:
        plain_bits.append(_line_plain(line, clocks))
        ssml_bits.append(_line_ssml(line, clocks))
        screen_bits.append(_line_screen(line))
    plain = f"{local} station. " + ". ".join(plain_bits) + "."
    markup = (
        _ssml_name(local)
        + " station. "
        + '. <break time="200ms"/> '.join(ssml_bits)
        + "."
    )
    screen = local + "\n" + "\n".join(screen_bits)
    return plain, markup, screen


def _directions(trains: list[Train], now: datetime, kind: str, home_city: str) -> tuple:
    grouped: dict = {}
    long_distance: dict = {}
    for train in trains:
        if _is_long_distance(train.route, train.agency, train.bus):
            display, spoken = _long_distance_label(train.route, train.agency)
            _add_train(long_distance, spoken, display, train, now)
            continue
        name, wrap = _direction_name(train.direction, kind, home_city)
        bucket = grouped.get(name)
        if bucket is None:
            bucket = _DirectionGroup(name=name, wrap=wrap, first=train.predicted, lines={})
            grouped[name] = bucket
        elif train.predicted < bucket.first:
            bucket.first = train.predicted
        display, spoken = line_label(train.route, train.agency, train.bus)
        _add_train(bucket.lines, spoken, display, train, now)
        line = bucket.lines[spoken]
        if line.first < bucket.first:
            bucket.first = line.first
    ordered = sorted(grouped.values(), key=lambda item: (item.first, item.name))
    result = []
    for bucket in ordered:
        lines = sorted(bucket.lines.values(), key=lambda item: (item.first, item.spoken))
        bucket.lines = _cap_lines(lines)
        if bucket.lines:
            result.append(bucket)
    long_lines = sorted(long_distance.values(), key=lambda item: (item.first, item.spoken))
    return result, _cap_lines(long_lines)


def _cap_lines(lines: list) -> list:
    """One or two lines keep two times. A longer group keeps the next time only."""
    limit = 1 if len(lines) > 2 else MAX_TIMES_PER_LINE
    kept = []
    for line in lines:
        line.pairs = sorted(line.pairs)[:limit]
        if line.pairs or line.cancelled:
            kept.append(line)
    return kept


def _add_train(lines: dict, spoken: str, display: str, train: Train, now: datetime) -> None:
    line = lines.get(spoken)
    if line is None:
        line = _LineGroup(
            display=display,
            spoken=spoken,
            pairs=[],
            cancelled=False,
            first=train.predicted,
        )
        lines[spoken] = line
    elif train.predicted < line.first:
        line.first = train.predicted
    if train.cancelled:
        line.cancelled = True
        return
    minute = _minutes_away(train.predicted, now)
    if any(item[0] == minute for item in line.pairs):
        return
    line.pairs.append((minute, clock_hhmm(utc_to_warsaw_wall(train.predicted))))


def _direction_name(raw: str, kind: str, home_city: str) -> tuple:
    name = local_name(raw or "", home_city)
    if name:
        return name, True
    if kind == "arrival":
        return "the previous stop", False
    return "the next stop", False


def _is_long_distance(route: str, agency: str, bus: bool) -> bool:
    """Intercity, RegioJet, Leo, and Arriva do not get a direction of their own.

    Arriva in this feed is the AR brand, not a numbered suburban line.
    """
    code, _marked = _strip_zka(route or "")
    _kind, _display, spoken = _classify(code, agency)
    if spoken in ("Intercity", "RegioJet", "Leo", "Arriva"):
        return True
    name = (agency or "").lower()
    if "intercity" in name or "regiojet" in name or "arriva" in name:
        return True
    if "leo" in name:
        return True
    return False


def _long_distance_label(route: str, agency: str) -> tuple:
    code, _marked = _strip_zka(route or "")
    _kind, _display, spoken = _classify(code, agency)
    name = (agency or "").lower()
    if spoken == "Intercity" or "intercity" in name:
        return "Intercity", "Intercity"
    if spoken == "RegioJet" or "regiojet" in name:
        return "RegioJet", "RegioJet"
    if spoken == "Leo" or "leo" in name:
        return "Leo", "Leo"
    return "Arriva", "Arriva"


def _direction_plain(group: _DirectionGroup, kind: str, clocks: bool = False) -> str:
    lead = "From" if kind == "arrival" else "Towards"
    return f"{lead} {group.name}: " + ", ".join(_line_plain(line, clocks) for line in group.lines)


def _direction_ssml(group: _DirectionGroup, kind: str, clocks: bool = False) -> str:
    lead = "From" if kind == "arrival" else "Towards"
    name = _ssml_name(group.name) if group.wrap else _escape(group.name)
    body = ", ".join(_line_ssml(line, clocks) for line in group.lines)
    return f"{lead} {name}: {body}"


def _direction_screen(group: _DirectionGroup, kind: str) -> str:
    lead = "From" if kind == "arrival" else "Towards"
    return f"{lead} {group.name}: " + ", ".join(_line_screen(line) for line in group.lines)


# ElevenLabs runs "R one now" together. An em dash with spaces is a short pause.
# Alexa gets a break of about the same length. The screen keeps clock digits.
_NOW_GAP = " \u2014 "
_NOW_BREAK = ' <break time="150ms"/> '


def _opens_with_now(line: _LineGroup, clocks: bool) -> bool:
    """True when the first spoken chunk is "now". Named hours use clocks instead."""
    if clocks or not line.pairs:
        return False
    return line.pairs[0][0] <= 0


def _line_plain(line: _LineGroup, clocks: bool = False) -> str:
    if line.pairs:
        if clocks:
            return f"{line.spoken} {_join_clocks([item[1] for item in line.pairs])}"
        times = _join_times([item[0] for item in line.pairs])
        if _opens_with_now(line, clocks):
            return f"{line.spoken}{_NOW_GAP}{times}"
        return f"{line.spoken} {times}"
    return f"{line.spoken} cancelled"


def _line_ssml(line: _LineGroup, clocks: bool = False) -> str:
    """Same words as the plain line. "now" is a break, and the tag stays unescaped."""
    if not _opens_with_now(line, clocks):
        return _escape(_line_plain(line, clocks))
    times = _join_times([item[0] for item in line.pairs])
    return _escape(line.spoken) + _NOW_BREAK + _escape(times)


def speak_clock(hour: int, minute: int) -> str:
    """12-hour words with no am or pm. 08:05 is "eight oh five", 00:10 is "twelve ten"."""
    hour12 = hour % 12 or 12
    hour_word = english_number(hour12)
    if minute == 0:
        return hour_word
    if minute < 10:
        return hour_word + " oh " + english_number(minute)
    return hour_word + " " + english_number(minute)


def _join_clocks(clocks: list) -> str:
    chunks = ["at " + speak_clock(int(text[0:2]), int(text[3:5])) for text in clocks]
    if len(chunks) == 1:
        return chunks[0]
    if len(chunks) == 2:
        return chunks[0] + " and " + chunks[1]
    return ", ".join(chunks[:-1]) + " and " + chunks[-1]


_ORDINALS = (
    "",
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
    "eleventh",
    "twelfth",
    "thirteenth",
    "fourteenth",
    "fifteenth",
    "sixteenth",
    "seventeenth",
    "eighteenth",
    "nineteenth",
    "twentieth",
    "twenty-first",
    "twenty-second",
    "twenty-third",
    "twenty-fourth",
    "twenty-fifth",
    "twenty-sixth",
    "twenty-seventh",
    "twenty-eighth",
    "twenty-ninth",
    "thirtieth",
    "thirty-first",
)


def window_phrases(kind: str, window_start: datetime, now: datetime) -> tuple:
    """Spoken intro, then the same sentence with a digit clock for the screen."""
    wall = utc_to_warsaw_wall(window_start)
    label = "Arrivals" if kind == "arrival" else "Departures"
    ampm = "am" if wall.hour < 12 else "pm"
    hour12 = wall.hour % 12 or 12
    if wall.minute == 0:
        spoken_clock = english_number(hour12) + " " + ampm
        screen_clock = f"{hour12} {ampm}"
    elif wall.minute < 10:
        spoken_clock = (
            english_number(hour12) + " oh " + english_number(wall.minute) + " " + ampm
        )
        screen_clock = f"{hour12}:{wall.minute:02d} {ampm}"
    else:
        spoken_clock = english_number(hour12) + " " + english_number(wall.minute) + " " + ampm
        screen_clock = f"{hour12}:{wall.minute:02d} {ampm}"
    day = wall.date()
    today = warsaw_date(now)
    if day == today:
        when = "today"
        when_screen = "today"
    elif day == today + timedelta(days=1):
        when = "tomorrow"
        when_screen = "tomorrow"
    else:
        when = "on " + day.strftime("%B") + " " + _ORDINALS[day.day]
        when_screen = "on " + day.strftime("%B") + " " + str(day.day)
    return (
        f"{label} from {spoken_clock} {when}.",
        f"{label} from {screen_clock} {when_screen}.",
    )


def _line_screen(line: _LineGroup) -> str:
    label = line.display
    if line.spoken.endswith(" bus") and line.display != "bus":
        label = line.display + " bus"
    if line.pairs:
        return label + " " + ", ".join(item[1] for item in line.pairs)
    return label + " cancelled"


def _join_times(minutes: list) -> str:
    chunks = []
    for minute in minutes:
        if minute <= 0:
            chunks.append("now")
        else:
            chunks.append("in " + english_number(minute))
    if len(chunks) == 1:
        if minutes[0] <= 0:
            return "now"
        return chunks[0] + " " + _unit(minutes[0])
    last_number = 0
    for minute in reversed(minutes):
        if minute > 0:
            last_number = minute
            break
    if last_number:
        chunks[-1] = chunks[-1] + " " + _unit(last_number)
    if len(chunks) == 2:
        return chunks[0] + " and " + chunks[-1]
    return ", ".join(chunks[:-1]) + " and " + chunks[-1]


def _unit(minute: int) -> str:
    if minute == 1:
        return "minute"
    return "minutes"


def _minutes_away(predicted: datetime, now: datetime) -> int:
    """Whole minutes remaining, rounded down. Under a minute is "now"."""
    delta = int((predicted - now).total_seconds())
    if delta < 60:
        return 0
    return delta // 60


def _classify(code: str, agency: str) -> tuple:
    key = " ".join((code or "").upper().split())
    if key in _INTERCITY:
        return "brand", "Intercity", "Intercity"
    if key in _REGIONAL:
        return "brand", "regional", "regional"
    if code and "sprinter" in code.lower():
        return "brand", "Sprinter", "Sprinter"
    if _is_line_code(code):
        return "line", code, _spell(code)
    display, spoken = _agency_fallback(agency)
    return "brand", display, spoken


def _is_line_code(code: str) -> bool:
    if not code or len(code) > 6:
        return False
    letter = False
    for ch in code:
        if ch.isdigit():
            continue
        if ch.isalpha():
            letter = True
            continue
        return False
    return letter


def _spell(code: str) -> str:
    parts = []
    index = 0
    while index < len(code):
        if code[index].isdigit():
            end = index + 1
            while end < len(code) and code[end].isdigit():
                end += 1
            parts.append(english_number(int(code[index:end])))
            index = end
            continue
        parts.append(_FOLD.get(code[index], code[index]).upper())
        index += 1
    return " ".join(parts)


def _agency_fallback(agency: str) -> tuple:
    name = (agency or "").lower()
    if "intercity" in name:
        return "Intercity", "Intercity"
    if "leo" in name:
        return "Leo", "Leo"
    if "regiojet" in name:
        return "RegioJet", "RegioJet"
    if "arriva" in name:
        return "Arriva", "Arriva"
    return "regional", "regional"


def _strip_zka(route: str) -> tuple:
    text = route.strip()
    upper = text.upper()
    if upper == "ZKA":
        return "", True
    if upper.startswith("ZKA ") or upper.startswith("ZKA\t"):
        return text[3:].strip(), True
    return text, False


def _polish_voice_active() -> bool:
    return bool(POLISH_VOICE) and POLISH_VOICE in ALEXA_VOICES and POLISH_LANG in ALEXA_LANGS


def _ssml_name(name: str) -> str:
    escaped = _escape(name)
    if not _polish_voice_active():
        return escaped
    return (
        '<voice name="'
        + POLISH_VOICE
        + '"><lang xml:lang="'
        + POLISH_LANG
        + '">'
        + escaped
        + "</lang></voice>"
    )


def _escape(text: str) -> str:
    cleaned = "".join(ch for ch in text if _xml_char(ch))
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _xml_char(ch: str) -> bool:
    code = ord(ch)
    return code in (0x9, 0xA, 0xD) or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD

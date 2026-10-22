# Architecture

My Train Schedule is an Alexa skill for the next hour of trains in Poland. This repository is the source for the skill code, the station picker, the interaction model, and the weekly timetable build. The live skill runs as an Alexa-hosted Lambda. GitHub Pages serves only the account-linking page.

`skill-package/skill.json` is a publishing skeleton. Its endpoint ARNs are `HOSTED_ENDPOINT_UNKNOWN` and its icon URIs are `file://assets/images/...`. That file is not the live manifest. Do not copy it over the manifest in the Alexa-hosted repository.

## How the pieces connect

```
Echo  -->  Alexa  -->  Alexa-hosted Lambda (Python 3.8)
                         |                 |
                         |                 +--> updates.pb (live delays, each request)
                         |
                         +--> schedule.bin (built here, pushed to the hosted repo)

Alexa app enable / relink  -->  GitHub Pages picker
                                 implicit-grant fragment
                                 --> access token = stations, directions, optional key
```

Alexa-hosted skills get a Lambda endpoint in us-east-1, eu-west-1, and us-west-2, an S3 bucket, a DynamoDB table, and a CodeCommit repository. There is no custom schedule and no memory or timeout slider. Alexa stops waiting at about 8 seconds. The handler keeps a 7.5 second budget from the start of `lambda_handler`. A cold start's module import is about 0.1 seconds and is outside that budget, so 7.5 seconds leaves half a second for runtime startup before the handler and the trip back to Alexa.

The static GTFS zip is about 30 MB, and `stop_times.txt` is tens of megabytes. Parsing that on a request does not fit the budget, and a hosted skill cannot rebuild it overnight. [`.github/workflows/refresh-timetable.yml`](.github/workflows/refresh-timetable.yml) does the rebuild:

1. Every Monday at about 07:00 Warsaw time (05:00 UTC), on every push to `main`, on a pull request (build only), and on demand.
2. `scripts/build_data.py` downloads [https://mkuran.pl/gtfs/polish_trains.zip](https://mkuran.pl/gtfs/polish_trains.zip) and writes `schedule.bin`, `feed.json`, and the picker data files. The feed is regenerated daily from PKP PLK data. `feed_info.txt` is the window the index keeps; `calendar_dates.txt` in the zip spans a longer range. There is no `calendar.txt`.
3. The Action assembles a Pages site whose root is `index.html`, `privacy.html`, `terms.html`, `picker.js`, `picker.css`, `stations.js`, `stations.json`, and `directions.json`. Those five source files live in `picker/` in git. The three data files are build output and are not committed. The cache-busting version keeps the hand-set `var CACHE` value and appends an 8-character digest of `stations.js` and `directions.json`.
4. Pages is deployed by GitHub Actions, not from a branch. A pull request runs the build and the site assembly and does not deploy, so it cannot replace the live authorization page. The authorization URL stays `https://a-rebets.github.io/alexa-skill-poland-trains/index.html`. A directory URL that redirects can drop Alexa's query string. The privacy policy is `https://a-rebets.github.io/alexa-skill-poland-trains/privacy.html` and the terms of use are `https://a-rebets.github.io/alexa-skill-poland-trains/terms.html`.
5. On the weekly run and on a manual dispatch, the Action pushes `schedule.bin` and `feed.json` to the Alexa-hosted CodeCommit repository when the timetable changed, waits for that deploy, and smoke-tests departures. A push to `main` and a pull request do not push to the hosted repository. `scripts/data_digest.py` ignores the build timestamp in the header so two builds of the same feed compare equal.
6. The run fails, and GitHub emails the owner, if the feed's calendar ends within 10 days.

Realtime delays come from `https://mkuran.pl/gtfs/polish_trains/updates.pb` (GTFS-Realtime). `updates.json` is a custom document and is not used. The handler stops waiting for the live feed 1.5 seconds after it starts. A late file means scheduled times for this request; a download already running is left alone so it can fill the cache later. A finished file is reused for about 60 seconds, then revalidated with If-None-Match. The first use of a body builds a trip index beside the rest of the request, so a cold start that already has the bytes (`realtime=cached`) does not walk the protobuf on the handler after boto3 has taken the GIL. If the feed header is older than 75 minutes, delays are ignored. A missing trip in a fresh feed is not called on time.

Timetable and live delay data come from the Polish Trains GTFS feed processed by Mikołaj Kuranowski. The schedules are provided by PKP Polskie Linie Kolejowe S.A. The live delay feed is also available under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Stations are not stored in DynamoDB. Alexa keeps the access token on the Amazon account that linked the skill.

## Account linking

The grant is OAuth 2.0 implicit. There is no token endpoint and no client secret. Account linking is not in `skill.json`; it is set in the Alexa console (SMAPI).

| Setting | Value |
| --- | --- |
| Enable without account linking | Off, so enabling the skill opens the picker |
| Authorization URI | `https://a-rebets.github.io/alexa-skill-poland-trains/index.html` |
| Client ID | `upcoming-trains` |
| Scope | `stations` |
| Domain list | Empty. The page loads no fonts or images from other sites |

The page accepts `response_type=token` and `client_id=upcoming-trains` only. The redirect host must be `pitangui.amazon.com`, `layla.amazon.com`, or `alexa.amazon.co.jp`, and the path must be `/spa/skill/account-linking-status.html`. The authorization-code path `/api/skill/link/` is rejected. Done redirects with `#state=...&access_token=...&token_type=Bearer`.

The picker has three steps: up to five stations, the directions to hear, and an optional ElevenLabs key. Search folds Polish letters, so `lodz` finds Łódź Fabryczna and `warsaw central` finds Warszawa Centralna. A platform stop such as `Warszawa Zachodnia (Peron 9)` is folded into the parent. Choosing the parent covers the platform, and an old token that still names the platform id resolves to the parent.

The token is `v1.` or `v2.` plus the base64url of a compact JSON payload. It is not signed. The page is public, so a signing key shipped with it would not be secret. Station codes are public GTFS `stop_id` values, not row numbers, so they survive a rebuild.

- `v1` is a list of parent-station ids. `33605` is Warszawa Centralna. Every direction is on, and the voice is Alexa.
- `v2` without a key is `[["33506",["33530","LD"]],["33548",null]]`. `null` or an empty list means every direction. A list is the neighbour stop ids to keep. `"LD"` is long-distance.
- A key makes the payload `{"c":[...],"k":"<api key>"}`. An older token, or a token with no key, still means the Alexa voice.

Each direction is the neighbouring stop, shared by departures towards it and arrivals from it. Long-distance trains are one option. A direction id the new timetable does not know is ignored. If that leaves a station with no known direction and long-distance was not selected, the skill reads every direction instead of staying silent. Garbage tokens are rejected and the skill sends a Link Account card.

The optional ElevenLabs key lives only in that token. It is not written to a file, a log, an exception, or an S3 key. The skill sends it only to ElevenLabs.

## Interaction model

The invocation name is `my train schedule`. Locales are `en-US` and `en-GB`. The model is `skill-package/interactionModels/custom/`.

- `DeparturesIntent` is the default. Launch, and the old name `UpcomingTrainsIntent`, do the same. Optional slots are `time` (`AMAZON.TIME`) and `date` (`AMAZON.DATE`).
- `ArrivalsIntent` uses the same slots.
- `ChangeStationsIntent` sends a Link Account card.
- Help, stop, cancel, navigate home, and fallback stay on the built-in intents.

The spoken window is `[T, T+60 minutes)` in Europe/Warsaw. A date pins that calendar day. With no date, T is the next occurrence of the clock. Asked at 23:00, 8 am means tomorrow at 08:00. A clock up to five minutes in the past stays on today, so asking again just after the hour does not jump to tomorrow. Alexa resolves "tomorrow" to a date and a bare "8" to `08:00` before the skill sees the request. `MO`, `AF`, `EV`, and `NI` are 08:00, 13:00, 18:00, and 21:00.

A named hour uses the GTFS service calendar for that date, including a trip at 24:xx on the previous service day. GTFS times at or after 24:00 belong to the previous service date (24:40 is 00:40 the next morning). A requested day after the feed's last day is "The timetable isn't available yet". The coming hour, with no time slot, uses the rebuild message when today's Warsaw date is outside the feed.

Live delays apply when the window starts within 60 minutes of now. A later hour is read from the schedule. The candidate scan is wider than the spoken hour: from 120 minutes ago to 75 minutes ahead, then the delay is applied, then the result is cut to the hour.

`CanFulfillIntentRequest` answers YES for departures and arrivals, with or without the time and date slots, and NO for every other intent. The manifest interface is `CAN_FULFILL_INTENT_REQUEST`, next to APL.

## Speech

Departures are grouped by the next stop, arrivals by the previous stop, then by the line code (R3, S1, RL). A direction with one or two lines keeps the next two times on each line. More than two lines means one time per line. Long-distance brands (Intercity, RegioJet, Leo, Arriva) are one group at the end of the station, with no direction, under the same cap, so a single brand is at most two times. At most five stations are saved. The decoder allows eight so a rebuilt picker does not need a skill release the same day.

The coming hour is "in N minutes". A train in the current minute is "R one — now". The em dash is a short pause so the line code does not run into "now". Alexa SSML uses a 150 ms break in that spot. A named hour starts with "Departures from eight am tomorrow" (or "today", or "on October twenty-third"). Line times are clock words without am or pm: "R three at eight oh five and at eight thirty-five". The screen keeps clock digits.

Alexa SSML has no Polish voice. Names stay in the English voice. The last stop of a trip is not announced. Replacement buses (route type 3, short names starting with ZKA) are included and called a replacement bus.

## ElevenLabs voice

Train answers can be spoken by ElevenLabs Sarah, voice id `EXAVITQu4vr4xnSDxMaL`, model `eleven_v4_turbo`, `language_code` `en`, output `mp3_24000_48` (MP3 at 24 kHz, 48 kbps). The sentence is the same plain text, with numbers and line codes already in English words. The Echo Show text is unchanged. Help, the link prompt, errors, and goodbye stay on the Alexa voice.

`eleven_v4_turbo` is the quality model that still returns inside the real-time budget. It is twice the price. Flash is an older model and is not used. The deprecated streaming-latency flag only shortens time to first byte, and its strongest step turns off the text normalizer, so the skill calls the full-file MP3 endpoint. Streaming would deliver the first byte sooner, and the skill still has to upload a finished file before Alexa can play it. One answer is one audio tag.

The key is bring-your-own, read from the account-linking token. The skill does not ship a key.

On a cache miss the handler tells Alexa to say "Checking the timetable" through the Directive Service, in the Alexa voice, and does not wait on that call. The ElevenLabs POST is sent as soon as the sentence is known. The S3 HEAD and that directive run beside the POST; neither is waited out before it. boto3 is imported on the HEAD thread at that moment, not while the realtime file is parsed. That import holds the GIL, and doing it earlier made a cold start report `realtime=cached` with a long `realtime_ms`: the protobuf was already in memory, and the first walk of it was stalled. ElevenLabs then gets whatever handler time is still left, minus about 0.6 seconds reserved for the S3 upload and a 0.2 second margin. There is no separate 3 second cap. A fixed 3 second cap was too short: from us-east-1 a full hour takes about 2.6 seconds, and a longer sentence was cut off with budget left. If the remainder is under 0.4 seconds, the key is rejected (401, 402, or a quota error), or the upload fails, Alexa reads the SSML sentence. A text of 560 words or more is not sent (240 seconds at 140 words per minute). A file of 1,440,000 bytes or larger is 240 seconds at 48 kbps and is discarded.

Clips are stored at `Media/tts/<YYYY-MM-DD>/<sha256>.mp3` in the bucket named by `S3_PERSISTENCE_BUCKET`. The date is Europe/Warsaw. The hash is the voice, the model, and the text, so a clip can be reused by anyone that day and is not tied to one person or one key. The skill looks up only that day's prefix. Once per container per Warsaw day, a background thread deletes older date prefixes and leftover `Media/tts/<sha256>.mp3` objects from before the date prefix. That thread is not on the request path. A list or delete failure is logged and the answer still plays. Listing needs `s3:ListBucket`; the hosted role's documented object permissions are get, put, and delete, so without list permission the cleanup logs a failure and leaves the old objects.

The playback URL is a presigned GET, SigV4, path-style, valid for one hour (`ExpiresIn` 3600). The URL can die sooner, when the Lambda role's temporary credentials die. The bucket is private. S3 checks the role that signed the URL.

## Timing log

One line per request. It contains no token, no key, and no station list.

`import_ms` is the cold module import and includes `schedule_ms`. `total_ms` is the handler only, from the start of `lambda_handler`. A warm request reports `cold=0` and the import figures from this process, which were already paid. `fb` is why the Alexa voice was used (`none` when ElevenLabs spoke). `chars` is the length of the TTS sentence, not the sentence. `realtime` is `fresh` (new body), `cached` (memory or 304), `missed`, or `off`.

```
timing cold= import_ms= schedule_ms= realtime_ms= realtime= board_ms= cache_ms= eleven_ms= s3_put_ms= total_ms= fallback= fb= chars=
```

## Deploying lambda changes

A push to `main` that changes `lambda/` runs [Deploy skill code](.github/workflows/deploy-skill.yml), after pytest. The job clones the Alexa-hosted CodeCommit repository and copies `lambda/` onto the directory that contains `lambda_function.py`. It leaves `schedule.bin` and `feed.json` alone (the weekly refresh owns those) and it does not write `skill.json`. If `requirements.txt` would add or remove a dependency, the job prints the diff and keeps the hosted file. The hosted Python 3.8 runtime already provides boto3. Do not add it. `lambda/requirements.txt` in this repo lists no packages.

The commit is pushed to `master` only when the code changed. That push is the deploy. The handler Amazon calls is `lambda_function.lambda_handler`. The job waits until `ask smapi get-skill-status` reports SUCCEEDED for that commit. A failed deploy is retried with an empty commit, up to twice. If no deploy starts within 15 minutes, the job fails. Open the Alexa developer console, Code tab, and click Deploy. The timetable refresh uses the same wait and the same hosted-push lock, so the two jobs cannot push at the same time.

`workflow_dispatch` with `dry_run` prints that diff and does not push.

Do not copy `skill-package/skill.json` over the live manifest. Replacing the hosted endpoint ARNs with the placeholders in this file, or deleting the endpoints, disconnects the skill from its functions. Leave the hosted ARNs, region endpoints, and icon URIs as Amazon wrote them. Do not add an `accountLinking` block there.

Update the interaction model and the manifest with `ask smapi`, not by overwriting the hosted `skill.json`. The model files in this repo are `skill-package/interactionModels/custom/en-US.json` and `en-GB.json`. Manifest text that belongs in the live skill (name, summary, description, example phrases, keywords, testing instructions, and the `CAN_FULFILL_INTENT_REQUEST` interface) is edited through SMAPI so the hosted endpoints and icons stay in place.

## Tests and local build

`schedule.bin`, `feed.json`, `stations.js`, `stations.json`, and `directions.json` are not in git. Pytest builds a small timetable from `tests/fixtures/sample_gtfs/` (a few Warsaw stations, one suburban trip, one Intercity trip, and the Zachodnia platform alias). That sample is what the tests read. It is not the file the Echo uses.

```bash
python3 scripts/build_data.py
python3 -m pytest -q tests
node --test tests/picker.test.js
```

`python3 scripts/build_data.py` downloads the publisher zip and writes the five generated files under `lambda/data/` and `picker/`. Pass `--zip` to build from a zip you already have. `scripts/preview.py` and `scripts/search_stops.py` read that local build. `make test` runs pytest and the picker unit tests.

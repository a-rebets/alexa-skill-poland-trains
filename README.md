# Upcoming Trains

<!-- After the skill is published, wrap this badge in a link to its Alexa Skills store page:
     <a href="https://www.amazon.com/dp/SKILL_ASIN"><img ...></a> -->
<img src="docs/just-ask-alexa-badge.png" alt="Just Ask Alexa" width="190">

An Alexa skill that tells you the next trains leaving your Polish railway stations.

<img src="docs/picker.png" alt="Station picker with Warszawa Centralna and Kraków Główny selected" width="240">

## Try it

- "Alexa, upcoming trains."
- "Alexa, ask upcoming trains for arrivals."
- "Alexa, ask upcoming trains to change my stations."

Alexa reads the trains in the next 60 minutes at your stations, grouped by direction and line, with live delays when they're available. The skill speaks English (US and UK).

## How it works

1. Enable the skill in the Alexa app and tap **Link Account**.
2. This page opens. Search for up to five stations.
3. Choose which directions to hear at each station. Long-distance trains are one option.
4. Optionally paste your own [ElevenLabs](https://elevenlabs.io) API key for a natural voice. Leave it empty for the standard Alexa voice. Tap **Done**.
5. The page puts your stations, directions and optional key into the access token and returns you to Alexa.
6. When you ask, the skill reads the token and looks up the timetable and live delays.

## What this repository is

This repo is the station picker that the Alexa app opens during account linking, plus the skill's privacy policy. Both are static pages served by GitHub Pages:

- Station picker: https://a-rebets.github.io/alexa-skill-poland-trains/
- Privacy policy: https://a-rebets.github.io/alexa-skill-poland-trains/privacy.html

The skill's own code isn't in this repository.

## Privacy

You don't create an account, and nothing is stored on a server. What you pick (stations, directions and, if you add one, your ElevenLabs key) lives only inside the account-linking token. The skill sends the key only to ElevenLabs and never writes it to a file or log.

## Attribution

Timetable and live delay data comes from the [Polish Trains GTFS](https://mkuran.pl/gtfs/) feed processed by Mikołaj Kuranowski. The schedules are provided by PKP Polskie Linie Kolejowe S.A. and used under the PKP PLK public sector data usage terms. The live delay feed is also available under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Amazon, Alexa, and all related marks are trademarks of Amazon.com, Inc. or its affiliates.

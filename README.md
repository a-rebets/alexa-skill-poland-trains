# Station picker

This is the public page the Alexa app opens when you enable the Upcoming Trains skill. You search Polish railway stations and pick a few favorites. The page is static HTML, CSS, and JavaScript. It does not create an account and it does not store anything on a server.

- Authorization URI: https://a-rebets.github.io/alexa-skill-poland-trains/index.html
- Privacy policy: https://a-rebets.github.io/alexa-skill-poland-trains/privacy.html

## Timetable data

Station names come from the public GTFS feed of **PKP Polskie Linie Kolejowe S.A.**, assembled by **Mikołaj Kuranowski**: https://mkuran.pl/gtfs/

## Skill code

The Alexa skill itself is not in this repository. That code stays in a private repository.

## Publishing

`stations.js` and `stations.json` still need to be copied onto this root from the private skill repo's `picker/` directory. The page, stylesheet, script, and privacy policy are already here. Without the station list, search has nothing to show.

In Settings → Pages, set Source to **Deploy from a branch**, branch `main`, folder `/ (root)`.

`.nojekyll` is present so GitHub Pages does not run Jekyll. A GitHub Actions workflow is not in this repo.

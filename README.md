# Station picker

This is the public page the Alexa app opens when you enable the Upcoming Trains skill. You search Polish railway stations and pick a few favorites. The page is static HTML, CSS, and JavaScript. It does not create an account and it does not store anything on a server.

- Authorization URI: https://a-rebets.github.io/alexa-skill-poland-trains/index.html
- Privacy policy: https://a-rebets.github.io/alexa-skill-poland-trains/privacy.html

## Timetable data

Station names come from the public GTFS feed of **PKP Polskie Linie Kolejowe S.A.**, assembled by **Mikołaj Kuranowski**: https://mkuran.pl/gtfs/

## Skill code

The Alexa skill itself is not in this repository. That code stays in a private repository.

## Publishing

GitHub Actions deploys this branch to GitHub Pages (`.github/workflows/pages.yml`). In the repository settings, set Pages → Source to **GitHub Actions**.

If you would rather deploy the branch directly, set Source to **Deploy from a branch**, branch `main`, folder `/ (root)`. `.nojekyll` is there so GitHub Pages does not run Jekyll.

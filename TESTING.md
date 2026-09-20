# Testing with real credentials and the live site

Everything in this repo's own CHANGELOG/README was verified offline
(`smoke_test.py`, SYNTHETIC fixture replay — see that file's module
docstring) or against 2Captcha's REST API directly. **No engine has ever
been run against the live skyscanner.com, from any environment** — unlike
stockx-scraper, where the free path was measured live before the repo
shipped, this repo's own build environment could not even *read*
skyscanner.com to check the page's structure (WebFetch there returns
ROBOTS_DISALLOWED, and that restriction was not bypassed). This means
`flight_parser.py`'s selectors and JSON-shape heuristics are best-effort
guesses, not confirmed site knowledge — see its module docstring's
`# TODO: verify live` markers. This file is the checklist for closing that
gap for real, on a machine that can reach the site.

Run everything below from a normal terminal on your own machine — wherever
this repo lives for you.

## 1. Basic setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env
```

Leave `.env` blank for the first run — the whole point of "local-first" is
that nothing in it is required. Fill in `TWOCAPTCHA_KEY` /
`SKYSCANNER_PROXY` / `SKYSCANNER_CDP_ENDPOINT` later, only if you want to
test those specifically.

## 2. The most important run you can do: check what the real page looks like

This is the step nothing else in this repo could do for you:

```bash
python3 playwright_scraper.py --origin LHR --destination JFK \
  --depart-date 2026-10-15 --max-results 10 \
  --format json --out /tmp/sky_test.json --dump-html
echo "exit code: $?"
cat /tmp/sky_test.json.meta.json 2>/dev/null || echo "(no sidecar — see below)"
```

Three outcomes, and what each one means:

- **`exit code: 0`, a `.meta.json` with `"status": "complete"` and a
  believable `product_count`**: either `flight_parser.py`'s embedded-JSON
  heuristic or its DOM fallback selectors happened to match the real page.
  Open `/tmp/sky_test.json` and actually look at a few rows — a plausible-
  looking `price`/`brand`/`departure_time` is what "happened to match"
  looks like; a null-filled row is what "matched the wrong shape" looks
  like even when the exit code says 0. This is the difference README's
  own honesty section can't check for you.
- **`exit code: 4` (zero products), no `.meta.json` written** (by design —
  see `output_writer.finish_run`): open `sky_test_debug.html` and compare
  it against `flight_parser.py`'s `_CARD_SELECTORS` / the `_find_itinerary_
  lists()` heuristic. This is the expected first-run outcome if the
  selectors need updating — not evidence the free path is blocked.
- **`exit code: 3` (blocked)**: the same page came back with a
  `captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS` hit — most likely
  `"px-captcha"`. **This is what a plain local-first run actually got,
  live, on 2026-09-17**: a PerimeterX "Are you a person or a robot?"
  challenge page (`px-cloud.net`, `<div id="px-captcha">`), not search
  results. `flight_parser.BOT_CHALLENGE_MARKERS` now has two
  site-specific entries from that exact capture (see its docstring) —
  this exit code on its own is NOT evidence of a bug; it's the expected
  outcome of a request PerimeterX decided to challenge. To get PAST it to
  a genuine results page (still needed — a block page proves detection
  works, not that the parser does):
  - Try `--fingerprint` (works on all three engines now) and/or a
    residential `--proxy` — a plain local Chromium with no proxy and no
    device-identity story is the easiest case for PerimeterX to flag.
  - **Update, 2026-09-20 — `--cdp-endpoint` was tried live and did NOT
    get past it**: a real run over a real 2Captcha Scraping Browser API
    session still got the same `px-captcha` challenge — its bundled
    auto-solve extension did not clear it either, and 2Captcha confirmed
    directly there is no automated task type for PerimeterX at all (see
    `captcha_solver.py`'s module docstring for the full incident). Don't
    expect `--cdp-endpoint` alone to solve this. Two things are still
    worth trying instead, neither of which is a solve:
    - `--cdp-block-retries N` (default 2) reconnects for a fresh Scraping
      Browser session on a blocked result and retries — PerimeterX-style
      defenses are reputation/behavior-based, so a fresh-enough session
      sometimes just isn't challenged. Not guaranteed.
    - `--cookies-file path.json` loads cookies from a session a HUMAN
      solved manually in a real browser once (see its own `--help` text).
      If you ever do get a genuine results page — this way, by luck on a
      fresh `--cdp-endpoint` session, or any other way — that's the
      capture `flight_parser.py`'s selectors actually need, see the next
      bullet, and also worth dropping the cookies (redacted) or at least
      the fact that it worked into `captures/`.

Whatever you find, **updating `flight_parser.py`'s selectors/heuristics to
match what you actually saw — with a saved, scrubbed fixture under
`tests/fixtures/` and a new `smoke_test.py` check against it — is the
single most valuable contribution this repo can receive** (see
`CONTRIBUTING.md`).

## 3. Selenium, for real

```bash
python3 -m venv .venv-selenium   # separate venv — see README "Engines"
source .venv-selenium/bin/activate
pip install -r requirements-selenium.txt
python3 selenium_scraper.py --origin LHR --destination JFK --depart-date 2026-10-15 --out /tmp/sky_selenium.json
```

## 4. Puppeteer (pyppeteer), for real

```bash
python3 -m venv .venv-puppeteer
source .venv-puppeteer/bin/activate
pip install -r requirements-puppeteer.txt
python3 puppeteer_scraper.py --origin LHR --destination JFK --depart-date 2026-10-15 --out /tmp/sky_puppeteer.json
```

## 5. The 2Captcha REST API, with your real key

Confirms the key and hits a real, billed-nothing endpoint first:

```bash
python3 -c "
import env_config
from scraper_api_client import TwoCaptchaClient
args = type('A', (), {'twocaptcha_key': None, 'proxy': None, 'cdp_endpoint': None, 'url': None})()
env_config.apply_env(args)
c = TwoCaptchaClient(args.twocaptcha_key)
print('balance: \$%.2f' % c.get_balance())
"
```

## 6. The Scraping Browser API (`--cdp-endpoint`), for real

`SKYSCANNER_CDP_ENDPOINT` in `.env` is picked up automatically:

```bash
python3 playwright_scraper.py --origin LHR --destination JFK --depart-date 2026-10-15 --out /tmp/sky_cdp.json
```

**Gotcha**, same as the rest of the family: if `.env` has BOTH
`SKYSCANNER_CDP_ENDPOINT` and `SKYSCANNER_PROXY` set, the code ignores
`SKYSCANNER_PROXY` and warns — a CDP session already carries its own exit
IP, stacking a second one on top is a contradiction, not better cover
(same for a fingerprint over `--cdp-endpoint`). Comment out whichever
you're not testing if you want to test them in isolation.

**If this comes back blocked (a real, confirmed possibility — see step 2
above)**, try the two PerimeterX mitigations:

```bash
# reconnect for a fresh Scraping Browser session up to 4 more times on a blocked result
python3 playwright_scraper.py --origin LHR --destination JFK --depart-date 2026-10-15 \
  --cdp-block-retries 4 --out /tmp/sky_cdp.json

# reuse cookies from a session you solved manually yourself in a real browser
python3 playwright_scraper.py --origin LHR --destination JFK --depart-date 2026-10-15 \
  --cookies-file my_cookies.json --out /tmp/sky_cdp.json
```

Neither is a solve — see README "Read this before trusting a run" for
why none exists. `--cookies-file` expects a plain JSON array of cookie
objects (`name`/`value`/`domain`/... — the shape Playwright's own
`context.cookies()` returns, so the easiest way to produce one is to run
a headful `--headful` session yourself, solve the challenge by hand in
the window that opens, and save `await context.cookies()` before it
closes).

## 7. The residential proxy (`--proxy` / `SKYSCANNER_PROXY`), for real

```bash
python3 playwright_scraper.py --origin LHR --destination JFK --depart-date 2026-10-15 --out /tmp/sky_proxy.json
```

## 8. Push to GitHub and let CI do the rest

```bash
git remote add origin git@github.com:2scraper/skyscanner-scraper.git
git push -u origin main
git push --tags
```

Then, in the GitHub repo's Settings:

- **Secrets and variables → Actions**: add `TWOCAPTCHA_KEY`,
  `SKYSCANNER_CDP_ENDPOINT` / `SKYSCANNER_PROXY` (only if you want the
  `canary-cdp` job using them — `canary-local` needs no secrets at all),
  and `CLAUDE_CODE_OAUTH_TOKEN` (for `claude.yml` / `claude-code-review.yml`
  — both silently no-op without it, by design, rather than failing every
  PR check).
- **Actions → canary → Run workflow**: dispatch it manually at least once
  rather than waiting a day for the cron and trusting the badge blind —
  this is this repo's actual FIRST live test, so look at the run's log and
  uploaded artifact, not just the badge color.

## 9. What "done" looks like

- `tests.yml` green on both Python versions and all three `engine-smoke`
  matrix legs.
- At least one manually-dispatched `canary.yml` run, looked at — not just
  the badge — including whichever of the three outcomes in step 2 above it
  landed on.
- If step 2 landed on anything other than a clean `complete` with
  plausible-looking rows: `flight_parser.py` updated to match what you
  actually saw, with a fixture under `tests/fixtures/` and a new
  `smoke_test.py` check, per `CONTRIBUTING.md`.

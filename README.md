# skyscanner-scraper

![release](https://img.shields.io/github/v/release/2scraper/skyscanner-scraper?sort=semver)
![tests](https://github.com/2scraper/skyscanner-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/skyscanner-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![local-first](https://img.shields.io/badge/local--first-yes-success)

skyscanner.com flight-search scraper: origin/destination/dates in, a flat
list of itineraries out. Three engines (Playwright primary, Selenium and
Puppeteer/pyppeteer for parity), JSON or CSV output, an open, documented
`Product` schema. Part of the [2scraper](https://github.com/2scraper)
family — same output contract, exit codes, and family modules as
`stockx-scraper`.

## Read this before trusting a run

Unlike this family's other repos, **this one was built without ever being
able to look at the real site.** `WebFetch` on skyscanner.com's search
pages returns `ROBOTS_DISALLOWED`, and that restriction was not worked
around to build this — no `curl`, no bypass, nothing. That means:

- **Live-verified 2026-09-29:** with `--wait-for-human` over your own
  local Chrome (`--cdp-endpoint http://127.0.0.1:9222`), a person holds
  the PerimeterX button once; later runs in that same Chrome profile got
  no challenge and returned the full result set (385/385 itineraries for
  LHR→JFK in ~10s). Fully automated runs still stop at PerimeterX.
- **Update 2026-09-29:** the flight list actually arrives over the
  `web-unified-search` XHR, not in the HTML. A real response body
  (captured by hand in Chrome, LHR→JFK, 373 itineraries) is now the
  parser's verified source: all 373 rows parse with price, currency,
  airline, times, stops and deeplink (`fixtures/` holds a trimmed copy).
  All three engines capture that XHR and parse it before the HTML. What
  is still unverified: the engines' capture against the live site, since
  every live run so far stops at PerimeterX before the XHR fires.
- `flight_parser.py`'s CSS selectors and `__NEXT_DATA__` heuristics are
  **best-effort guesses**, each marked `# TODO: verify live` in that
  file, not confirmed site knowledge the way stockx-scraper's
  `product_parser.py` is (that one *was* built from real, live captures).
- **Everything else — the architecture — is real and tested**: exit
  codes, the output contract, dedupe, credential redaction, CLI
  validation, all three engines importing cleanly, the crash-safety
  wrapper around parsing. `smoke_test.py` proves all of that against
  SYNTHETIC fixtures (see its own module docstring).
- The gap between those two facts is exactly what `TESTING.md` step 2 and
  the `canary-local` CI job (which runs with **no secrets**, on a
  schedule, against the real site) exist to close. If you run this and
  get zero itineraries back, that is the **expected first-run outcome**
  for a parser nobody has pointed at the real markup yet — not evidence
  the scraper doesn't work. Open the `--dump-html` capture, compare it to
  `flight_parser.py`, fix what doesn't match, and you've turned a guess
  into this repo's first actually-verified fact.
- **Update, 2026-09-17 — the first real fact, and it's about detection,
  not the parser**: a plain local-first run (no proxy, no fingerprint)
  got served a PerimeterX "Are you a person or a robot?" challenge page
  instead of search results — exit `3` (blocked), exactly as designed.
  `flight_parser.BOT_CHALLENGE_MARKERS` now has two real, captured
  markers from that incident (see its docstring), on top of the generic
  detector that already caught it. The parser's own selectors are still
  unverified — a block page isn't a results page — see `TESTING.md`
  step 2 for how to try to get past the challenge (`--fingerprint` / a
  residential proxy / `--cdp-endpoint`) and capture a genuine one.
- **Update, 2026-09-20 — the PerimeterX gap is now CONFIRMED PERMANENT,
  not just unsolved-so-far**: a real run against the real site, over a
  real 2Captcha Scraping Browser API session (`--cdp-endpoint`), still
  got served the same PerimeterX challenge — the Scraping Browser's own
  bundled auto-solve extension did not clear it either. 2Captcha
  confirmed directly: **there is no automated task type for PerimeterX at
  all** (nor for DataDome or a bare Cloudflare managed-challenge
  interstitial, which get the same honest treatment defensively). This
  repo will never pretend to solve it. What it does instead — three
  mitigations, all non-bypass:
  1. **Honest error naming.** `captcha_solver.identify_unsupported_
     vendor()` names the real vendor in the log instead of the old, vague
     "no known widget/sitekey could be extracted" (which read like a
     parser bug, not a real product gap).
  2. **`--cdp-block-retries`** (Playwright/Puppeteer only — see
     "Engines" for Selenium's caveat): on a blocked `--cdp-endpoint` run,
     reconnect for a fresh Scraping Browser session and retry, since
     PerimeterX-style defenses are largely reputation/behavior-based and
     a fresh-enough session sometimes simply isn't challenged at all.
  3. **`--cookies-file`**: load cookies from a session a HUMAN solved
     manually in a real browser once. This is reuse, never an automated
     solve — the scraper never attempts to clear the challenge itself.
- **Update, 2026-09-21 — `--cdp-block-retries` exercised live, for real,
  against the real site**: a full run (`--origin LHR --destination JFK`)
  over a real Scraping Browser session exhausted all three attempts
  (initial connection plus both retries) — each retry correctly
  reconnected for a fresh session (a different exit identity from the
  pool, confirmed in the log), and each fresh session was challenged by
  PerimeterX just as fast as the last. Exit `3`, no `.meta.json` sidecar
  (correct — a blocked run never gets one, see "The output contract").
  The retry *mechanism* works exactly as designed; it just didn't clear
  the block this time, which is the honest, expected outcome for a
  reputation/behavior-based defense that isn't purely session-freshness
  triggered — "sometimes simply isn't challenged" was always a hedge, not
  a promise, and this run is the data point that keeps it a hedge rather
  than quietly becoming an unearned claim of success.
- **Update, 2026-09-22 — `--cookies-file` tried live for the first time,
  and does NOT clear PerimeterX on its own, even from the exact IP that
  solved it.** Roman solved the real "Press & Hold" challenge himself,
  exported all 13 resulting cookies, and ran this repo with
  `--cookies-file`. Challenged immediately over `--cdp-endpoint` (a
  different exit IP/device than the cookies came from, so not
  conclusive) — but ALSO challenged with `--proxy`/`--cdp-endpoint` both
  disabled, a plain local browser on Roman's own real IP, the same
  network the cookies were solved on. Most likely explanation: PerimeterX
  cookies are refreshed continuously by its own JS sensor inside a real,
  ongoing session, and/or its device fingerprint (Playwright's own
  automation tells, e.g. `navigator.webdriver`) is enough on its own to
  re-challenge regardless of which cookies are attached — a static
  snapshot replayed into a different browser process isn't the same
  "device" the cookies were issued to, even at the same IP. Not yet
  tried: a stealth-patched browser combined with `--cookies-file`. Until
  that's tried, the PerimeterX gap stays exactly as CONFIRMED PERMANENT
  as before — this test confirms `--cookies-file` alone isn't the missing
  piece, rather than leaving it merely untested. See `CHANGELOG.md` for
  the full write-up, including a real crash this same test exposed and
  fixed (`page.content()` racing PerimeterX's own self-reloading
  challenge page — now degrades gracefully instead of taking the whole
  run down).

- **Update, 2026-09-22 — `--stealth` added (all three engines), not yet
  live-tested.** The one variable the test above didn't isolate: the
  browser's own automation fingerprint, independent of which cookies are
  attached. `--stealth` (off by default) patches `navigator.webdriver`,
  `window.chrome.runtime`, `navigator.plugins`/`languages`, the
  `permissions.query('notifications')` mismatch, and WebGL vendor/renderer
  strings, before any page loads — the same category of change as
  `puppeteer-extra-plugin-stealth`, explicitly not a captcha solver or
  bypass. Combine with `--cookies-file` and test live before drawing any
  conclusion; until that live test happens, the PerimeterX gap stays
  CONFIRMED PERMANENT exactly as stated above — this entry records what
  was tried, not a result. See `CHANGELOG.md` for the full write-up.

- **Update, 2026-09-22 — `--scraper-api` added and live-tested (all three
  engines): a genuinely different failure shape, still not a bypass.**
  2Captcha's Scraper API (a browserless fetch run entirely on their own
  infrastructure — see `scraper_api_client.py`'s module docstring) never
  showed a single PerimeterX marker in live testing, but the flight-search
  route still never came back usable: sometimes a bare ~700-byte
  un-hydrated app shell, sometimes a real SEO-prerendered page (in a
  non-English locale, no way to pin it) with zero client-rendered result
  cards — skyscanner.com's actual flight data loads via client-side JS/XHR
  calls after the initial page, which a one-shot static fetch never
  triggers. The homepage, by contrast, fetches completely fine. Also
  fixed in passing: this repo's own `.env` `TWOCAPTCHA_KEY` was invalid
  (`ERROR_KEY_DOES_NOT_EXIST`) and has been replaced with a working key —
  worth double-checking it's the one you intend. Full write-up, including
  both observed response shapes, in `CHANGELOG.md`. **The PerimeterX gap
  stays CONFIRMED PERMANENT** — five mitigations tried now (`--cdp-
  endpoint`, `--cookies-file`, `--stealth`, `--scraper-api`, and their
  combinations), none has produced real itinerary data yet.

## Local-first

Like stockx-scraper, this does **not** require 2Captcha's paid Scraping
Browser API to run. The default is an ordinary local headless Chromium,
no proxy, no key, no account. `--proxy` / `--cdp-endpoint` / `--fingerprint`
are opt-in power options for volume, a specific exit country, or a
consistent device identity — the same reasoning stockx-scraper's own
README states, carried over here as an architectural choice even though
(see above) it hasn't been live-measured on *this* site yet the way it was
on that one.

## Install

Pick one engine (installing more than one into the same environment is not
supported — see "Engines" below):

```bash
pip install -r requirements-playwright.txt && playwright install chromium   # primary
pip install -r requirements-selenium.txt                                    # needs a matching chromedriver
pip install -r requirements-puppeteer.txt                                   # pyppeteer — see its own warning below
```

Copy `.env.example` to `.env` — leave it blank for a normal first run (see
"Local-first" above) and fill in what you use later. `python3 env_config.py`
shows what was picked up without ever printing a secret.

## Usage

```bash
# a one-way search
python3 playwright_scraper.py --origin LHR --destination JFK --depart-date 2026-10-15 \
  --format json --out sky_results.json

# round-trip, with filters
python3 playwright_scraper.py --origin LHR --destination JFK \
  --depart-date 2026-10-15 --return-date 2026-10-22 \
  --adults 2 --currency EUR --cabin-class business --stops direct --sort cheapest

# a full search URL directly (escape hatch — bypasses --origin/--destination/date-building)
python3 playwright_scraper.py --url "https://www.skyscanner.com/transport/flights/lhr/jfk/261015/"

# with 2Captcha's Scraping Browser API (opt-in — see "Local-first" above)
python3 playwright_scraper.py --origin LHR --destination JFK --depart-date 2026-10-15 \
  --cdp-endpoint "$SKYSCANNER_CDP_ENDPOINT"
```

### Many searches at once: `batch_scraper.py`

```bash
python3 batch_scraper.py --routes-file routes.example.csv --out skyscanner_batch.json
```

Per route: **1)** your local Chrome on `--local-cdp` (default
`http://127.0.0.1:9222`, started automatically with the persistent
profile `~/.chrome-skyscanner` if nothing listens there), with
`--wait-for-human 180`, so a person clears PerimeterX once and later
routes reuse that session; **2)** if that ends blocked / remote-API error /
crashed, or local Chrome is unavailable, the same search through 2Captcha
as configured in `.env` (Scraping Browser, fresh sessions via
`--cdp-block-retries`). An empty search (exit 4) is not retried.

Output: one merged file for all routes, plus `<out>.batch.json` with each
route's exit code, which path produced it, and every attempt. Exit code:
`0` all routes ok, `6` some, `3` none and at least one blocked.

Verified live 2026-09-29: 3/3 routes, 1690 itineraries via local Chrome with
no challenge (LHR-JFK 385, LHR-BCN round trip 1000+, LON-PAR 305). The
2Captcha fallback runs and reports correctly, but against PerimeterX it came
back blocked (exit 3), as every automated run so far. Round trips: rows
carry the outbound leg's times only.

### When PerimeterX challenges you: `--wait-for-human`

No engine solves PerimeterX's "Press & Hold" (2Captcha has no solver for it
either). The one path that reliably gets real results today is a person
completing the challenge once, with the scraper carrying on in that same
session:

```bash
# a visible local browser (forced --headful); hold the button when asked
python3 playwright_scraper.py --origin LHR --destination JFK --depart-date 2026-11-15 \
  --wait-for-human 120

# or your everyday Chrome, started with a debugging port — best odds, since
# it is your real fingerprint, IP and cookies
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-skyscanner"
python3 playwright_scraper.py --origin LHR --destination JFK --depart-date 2026-11-15 \
  --cdp-endpoint http://127.0.0.1:9222 --wait-for-human 120
```

On a challenge the scraper prints what to do, re-checks every 2s, and once
the challenge is gone collects results (including the `web-unified-search`
XHR). If it is still there after the timeout the run exits `3` (blocked).
With a remote `--cdp-endpoint` (the Scraping Browser) nobody can see the
window, so the flag only warns.

`selenium_scraper.py` and `puppeteer_scraper.py` accept the identical flag
set and produce the identical output contract — see "Engines" for the two
places they genuinely can't behave the same as Playwright.

### Flags

`--url --origin --destination --depart-date --return-date --adults
--currency --cabin-class --stops --sort --max-results --max-scrolls
--stall-rounds --scroll-delay --format --out --retries --retry-delay
--proxy --proxy-file --proxy-shuffle --proxy-block-retries
--twocaptcha-key --captcha-api --solve-captcha --min-score --cdp-endpoint
--cdp-block-retries --cookies-file --fingerprint --fp-tags --fp-country
--stealth --scraper-api --scraper-api-timeout --scraper-api-url
--allow-empty --dump-html --headless/--headful --wait-for-human`

`--cdp-block-retries`, `--cookies-file`, `--stealth`, and `--scraper-api`
are the four non-honest-messaging mitigations for the confirmed
PerimeterX gap (see "Read this before trusting a run" above) — each is a
no-op until, respectively, `--cdp-endpoint`/a block actually happens, a
cookies file is passed, the flag is set, or the flag is set. Present on
all three engines for flag parity (CLAUDE.md §4), but `--cdp-block-
retries` is genuinely weaker on Selenium — see "Engines" below.
`--scraper-api` is the odd one out structurally: every other flag above
still launches a local/`--cdp-endpoint` browser this process drives
itself; `--scraper-api` instead sends the fetch entirely to 2Captcha's
own infrastructure and ignores `--proxy`/`--cdp-endpoint`/`--fingerprint`/
`--cookies-file`/`--max-scrolls`/`--stall-rounds`/`--scroll-delay`
(logged as a warning, not silently dropped).

Identical across all three engines — a smoke_test.py check asserts the
three parsers' flag sets never drift apart. `--fingerprint`/`--fp-tags`/
`--fp-country` apply to Selenium and Puppeteer too (as of this pass —
earlier revisions only wired them into `playwright_scraper.py`, a silent
per-engine gap now closed the same way `stockx-scraper`'s selenium/
puppeteer engines still lack it, unfixed there per the standing
instruction not to touch that repo): each engine sets whatever user agent
the 2Captcha Fingerprint API returns via its own driver's real primitive
(Playwright's `new_context(user_agent=...)`, pyppeteer's
`page.setUserAgent()`, Chrome's own `--user-agent=` switch under
Selenium) — see "What this repo deliberately does NOT apply from a
fingerprint" below. `--captcha-api` overrides the 2Captcha REST base URL
(testing only — this is how the fingerprint/min-score wiring above was
actually verified live, against a local mock, not just unit-tested).
`--min-score` is 2Captcha's own `minScore` field on a `RecaptchaV3Task`
request (0.3 default, matching `stockx-scraper`'s).

### Family flags that don't apply here — and why

The family's flag contract (`--pages --category --concurrency
--proxy-rotate`) has four entries with no equivalent in this repo, each
for a structural reason specific to a single, scroll-based search page
rather than a multi-page listing:

- **`--pages` / `--category`**: skyscanner.com exposes one continuously-
  scrolled results page per search, not `?page=N` listings, and flights
  have no marketplace-style category — `--max-scrolls`/`--stall-rounds`
  and `--origin`/`--destination`/etc. are this repo's actual equivalents
  (see "Pagination" below; already noted in the 1.0.0 CHANGELOG entry).
- **`--concurrency`**: the family's worker-owns-one-exit model (§7)
  parallelizes fetching *independently-addressable pages of the same
  search* — page 2 doesn't need page 1's DOM. A scroll-based page has no
  such unit: there is exactly one page and one DOM, and every scroll
  round depends on the DOM state the previous round left behind, so
  "two workers scrolling the same search" isn't a lighter version of
  concurrency, it's a race on the one thing the run needs to stay
  consistent. `proxy_pool.ProxyPool.worker_view()` — the primitive
  `--concurrency` would dispatch onto — is still ported verbatim (per the
  family's "copy the core, verbatim" rule) and stays tested (see
  `smoke_test.py`), for if a future feature (e.g. running several
  origin/destination/date combinations as a batch, each its own
  independent search) introduces an actual parallelizable unit. Adding a
  `--concurrency` flag that fans out over nothing real would be the same
  "policy nothing reads" defect class as an unused constant — worse,
  since it would *look* like it does something.
- **`--proxy-rotate`**: same root cause — "a fresh proxy for every page"
  presumes there's a next page to rotate before. Rotating mid-scroll would
  mean tearing down the browser context holding every itinerary scrolled
  into view so far, which is strictly worse than not rotating: it throws
  away already-collected results to switch an exit that was working fine.

### What this repo deliberately does NOT apply from a fingerprint

`fingerprint_client.py` only ever extracts and applies the user agent from
a 2Captcha Fingerprint API profile — never a locale or timezone. An older
sibling repo in this family shipped a *fabricated* locale
(`f"en-{country}"`, never anything the API actually returned) and never
applied the API's documented timezone field at all, and both went
unnoticed for months because nothing exercises a credentialed code path in
an offline suite. This session could not get a confirmed field name for
either from 2Captcha's own public reference for this endpoint (it 404s),
so — rather than repeat that exact mistake with a newly-invented key —
this repo omits both until a real API response (or 2Captcha support)
confirms the field name. Omitting a signal honestly beats guessing it.

### Why there's no `page_flow.py` here

The family adds a shared `page_flow.py`/`STATE_POLICY` module once a site
answers a request in more than two ways that each want a different
retry/solve/blocked decision (MediaMarkt: four ways; Amazon: five).
skyscanner.com, as currently understood — entirely from the draft this
repo was rebuilt from, never from a live capture — answers in the same two
ways farfetch.com does (content, or a bot challenge/blocked page), which
is exactly the case the family's own convention says does NOT need
`page_flow.py`: farfetch-scraper doesn't have one either. Revisit this if
a live capture ever turns up a third distinct response shape (e.g. a
locale-specific "no such route" page, or a throttle distinct from a
challenge) that should be retried differently than a plain block.

Credentials belong in `.env` / `SKYSCANNER_PROXY` / `TWOCAPTCHA_KEY` —
never as literal `--proxy`/`--twocaptcha-key` text on a shared or logged
command line if you can avoid it.

## Output contract

`Product` (`output_writer.py`) — family-common columns first, flight-
specific columns after:

```
sku, source, category, title, brand, price, currency, price_source, product_url,
image_url, scraped_at,
origin, destination, depart_date, return_date, adults, cabin_class, stops,
departure_time, arrival_time, duration, sort
```

skyscanner.com has no persistent SKU the way a marketplace does — `sku`
here is a deterministic fingerprint of the itinerary (route, dates,
airline, times, cabin class — everything except price), so the same
itinerary scraped on two different days gets the same `sku` and
`diff_runs.py` reports a real price change instead of one result
disappearing and an unrelated one appearing. `brand` carries the
operating airline. `category` is always `"flights"` — there's no separate
listing category on this site the way there is on a marketplace.
`price_source` is `embedded_json` or `dom`, mirroring which extraction
path actually produced the row (see `flight_parser.py`) — never a
defaulted guess. See `sample_output.json` / `sample_output.csv` — **these
are a clearly fictional illustration of the schema** ("Fictional Air" /
"Sample Airways"), not a real capture, unlike stockx-scraper's own sample
output — see "Read this before trusting a run" above for why no real one
exists yet.

**Exit codes**: `0` complete · `1` crash · `2` bad usage · `3` blocked ·
`4` zero itineraries (and nothing was written) · `5` remote API error ·
`6` partial. Every completed/partial run writes a `<out>.meta.json`
sidecar with `status`, `pages_completed` (scroll rounds, here),
`failed_pages` and `price_confirmed_pct` — **except** a
failed/empty/blocked/remote-API-error run, which writes no sidecar and no
output at all, so it can never overwrite a previous good run
(`--allow-empty` opts out of the "don't write an empty result" half of
that guard only).

## Pagination

skyscanner.com lazy-loads results on scroll rather than exposing numbered
pages, so — unlike stockx-scraper's `?page=N` loop — every engine here
scrolls and re-parses the accumulated page, deduping by `sku`, until
`--max-results` is reached or `--stall-rounds` consecutive scrolls add
nothing new (capped by `--max-scrolls` either way).

## Engines

Playwright is primary; Selenium and pyppeteer are parity copies — all
three agree on exit codes and the `Product` schema via the shared
`output_writer.finish_run()`. Real, stated limits (identical to
stockx-scraper's, since these are properties of the drivers, not the
site):

- **Selenium cannot use an authenticated remote CDP endpoint.**
  chromedriver's `debuggerAddress` takes a bare `host:port`; the Scraping
  Browser API's `ws://login:pass@host:port` shape needs an authenticated
  WebSocket upgrade, which only Playwright's `connect_over_cdp` and
  pyppeteer's `connect` support. `selenium_scraper.py` refuses a
  credentialed `--cdp-endpoint` outright (exit 2).
- **Selenium's `--proxy-server` cannot authenticate at all.** A `--proxy`
  with credentials has them stripped before reaching Chrome, with a loud
  warning — never a silent no-op.
- **Selenium's `--cdp-block-retries` is weaker than Playwright's/
  Puppeteer's.** On those two engines, a retry over `--cdp-endpoint`
  reconnects for a genuinely fresh Scraping Browser session (a new exit
  identity) — the actual PerimeterX mitigation (see "Read this before
  trusting a run"). Selenium's `debugger_address` instead re-attaches to
  the SAME already-running remote browser, so retrying there is just
  another attempt at the scrape, not a fresh identity. Moot in the
  typical case anyway, since a CREDENTIALED `--cdp-endpoint` (the real
  Scraping Browser API shape) is refused outright on Selenium before any
  of this runs (see the point above).
- **pyppeteer is effectively unmaintained** (its own README points at
  Playwright) — shipped for parity, not as a recommendation.
- Install **exactly one** engine per environment — Playwright and
  pyppeteer declare mutually unsatisfiable `pyee` pins, and pyppeteer
  collides with Selenium's `urllib3` pin. Use a venv per engine, same as
  `.github/workflows/tests.yml`'s `engine-smoke` job.

## Known limitations

- **Nothing in `flight_parser.py` has been checked against a live
  response** — see "Read this before trusting a run" above. This is the
  single biggest difference from stockx-scraper and the reason this
  section leads with it instead of burying it.
- **No site-specific block-page marker exists.** `flight_parser.
  BOT_CHALLENGE_MARKERS` is deliberately empty — detection still runs via
  `captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS` (Cloudflare/reCAPTCHA/
  hCaptcha/PerimeterX/DataDome wording), but nothing specific to how
  skyscanner.com's own block page reads has been captured yet. If you hit
  one, `TESTING.md` step 2 explains how to add it, the same way each of
  stockx-scraper's three marker tuples was added after a real captured
  incident.
- **Captcha token injection on a locally-launched browser is not
  implemented**, for the same reason as stockx-scraper: injecting a
  solved token is widget/site-specific, and no real challenge from this
  site was ever available to verify an injector against. Over
  `--cdp-endpoint` (the Scraping Browser API), this doesn't matter —
  2Captcha's own `Captcha.setAutoSolve` CDP domain handles it entirely
  inside their infrastructure (wired up in `playwright_scraper.py` /
  `puppeteer_scraper.py`).
- **`--scraper-api` gets HTML only**, so it never sees the
  `web-unified-search` XHR; it can only fall back to the unverified
  HTML paths. The three browser engines capture the XHR.
- **The embedded-JSON extraction path is a generic heuristic, not a
  pinned query.** stockx-scraper's `getDiscoveryData` query name is
  confirmed; this repo's `_find_itinerary_lists()` instead walks
  `__NEXT_DATA__` looking for any list of dicts that structurally looks
  like flight results (a price-shaped key alongside a legs/segments-
  shaped one). That's a reasonable bet for a Next.js-based site, not a
  verified fact about this one.
- **No round-trip / deep-link has ever been followed past
  skyscanner.com's own domain**, deliberately — the booking partner a
  result's deep link points to is out of scope for this tool.

## Before this repo goes public

This repo is not published yet — it lives on disk, not on GitHub. The
family's own bootstrapping checklist (CLAUDE.md §15 steps 7-8) has three
steps that only make sense once it is, so they're listed here as pending
rather than done:

- **Add the org profile README row**, only once this repo is public and at
  the same bar as its siblings — not before, since a row pointing at a
  private/nonexistent repo is a broken link (measured on the real org,
  2026-09-16: 4 of 17 existing rows were already 404 for exactly that
  reason — see the family standard for the one-line command that checks
  this, and re-run it, don't just trust this sentence, since the count
  changes every time anyone publishes anything).
- **Set the repo's GitHub "About" panel** (description/topics/homepage —
  see CLAUDE.md §2), which is GitHub-side metadata nothing in this repo
  can set for itself.
- **Scan the full git history for credentials before flipping to public**
  (`git rev-list --objects --all`, not just the working tree) — a commit
  on top cannot remove what a tag or a merged PR's refs already hold.
  This matters more than usual here: this session's own testing phase
  used a version-mismatched-then-matched chromedriver and several
  synthetic fixture servers, none of which should have touched any real
  credential, but the scan is what confirms that rather than assumes it.

## Development

```bash
python3 smoke_test.py     # or: pytest tests/test_smoke.py
```

Passes with **no** engine library installed at all (each engine guards its
driver import behind a module-level `try/except ImportError`).

**Testing against the live site**: see [`TESTING.md`](TESTING.md) — a
step-by-step checklist, starting with the one test that actually matters
for this repo: pointing the local-first default at a real search and
checking whether `flight_parser.py`'s guesses hold up.

## License

MIT — see `LICENSE`.

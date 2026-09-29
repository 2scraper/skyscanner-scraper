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

What is measured, as of 2026-09-29. The dated investigation that got here
(2026-09-17 → 2026-09-29) is in `CHANGELOG.md`.

- **Where the data comes from.** A search page's initial HTML carries no
  flights. The page fetches them from the `web-unified-search` XHR and
  re-polls it while its `context.status` is `incomplete`. All three
  engines capture that XHR and parse it first (`price_source:
  search_api`). The parser was built against a real response captured by
  hand in Chrome (LHR→JFK, 373 itineraries; a trimmed copy is in
  `fixtures/`). The `__NEXT_DATA__` and DOM paths are unverified fallbacks
  (`# TODO: verify live` in `flight_parser.py`).
- **What works live.** Playwright over your own local Chrome
  (`--cdp-endpoint http://127.0.0.1:9222`) with `--wait-for-human`. A
  person held PerimeterX's "Press & Hold" once (~10 s); runs in that same
  Chrome profile over the next ~2.5 hours were not challenged and returned
  the full result set: 385–389 itineraries for LHR→JFK in ~10 s, and 1690
  across three routes through `batch_scraper.py`. That is one session;
  how long a cleared session stays unchallenged is not measured beyond it.
  pyppeteer and Selenium are verified on a local stand, not live.
- **What has not worked live.** Every fully automated attempt so far was
  challenged by PerimeterX: local Chromium (plain, and with
  `--fingerprint`); the Scraping Browser API over `--cdp-endpoint`
  (`Captcha.setAutoSolve` on, fresh sessions via `--cdp-block-retries`);
  `--cookies-file` replaying a human-cleared session into another browser.
  `--scraper-api` returned pages without flights, because it cannot see the
  XHR. `--stealth` has not been live-tested.
- **What a captcha solver can and cannot do here — the narrow version.**
  Skyscanner's challenge page (captured 2026-09-17, -22 and -29) carries
  PerimeterX's own "Press & Hold" and no third-party widget: no reCAPTCHA,
  Turnstile or hCaptcha sitekey, so there is nothing for a token-based
  task on that page. 2Captcha's PerimeterX page
  (`2captcha.com/p/perimeterx-solver`, read 2026-09-29) says that solver
  "is currently unavailable or under development" and offers custom
  solutions for large volumes. **This repo does not implement a PerimeterX
  solve** — a TODO if one becomes available, not a property of the site.
- **The rest is tested offline**: exit codes, the output contract, dedupe,
  credential redaction, CLI validation, the XHR capture, `--wait-for-human`
  and re-opening the search after a challenge (all three engines, on a
  local stand). `smoke_test.py` uses the real fixture where it exists and
  says so where a fixture is synthetic.

## Local-first

Like stockx-scraper, this does **not** require 2Captcha's paid Scraping
Browser API to run. The default is an ordinary local headless Chromium,
no proxy, no key, no account. `--proxy` / `--cdp-endpoint` / `--fingerprint`
are opt-in power options for volume, a specific exit country, or a
consistent device identity. On this site, measured 2026-09-29, none of them
got past PerimeterX on its own; what returned real results was your own
local Chrome plus a person clearing the challenge once (see above).

## Install

Pick one engine (installing more than one into the same environment is not
supported — see "Engines" below):

```bash
pip install -r requirements-playwright.txt && playwright install chromium   # primary
pip install -r requirements-selenium.txt                                    # needs a matching chromedriver
pip install -r requirements-puppeteer.txt                                   # pyppeteer — see its own warning below
```

Copy `.env.example` to `.env` **next to the scripts** (it is read from there,
whatever directory you run from) — leave it blank for a normal first run
(see "Local-first" above) and fill in what you use later. An exported
variable still wins over `.env`, and an explicit `--origin/--destination/
--depart-date` wins over a saved `SKYSCANNER_URL`. `python3 env_config.py`
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

Output goes through the same `finish_run()` as a single run: one merged file
(deduped by sku) plus `<out>.meta.json`, whose `failed_pages` are the failed
route numbers and whose `per_route` lists each route's exit code, path and
attempts. A route with a genuinely empty search is not a failure. Exit `0`
all routes ok (or empty), `6` some failed, `3` none produced a row and one
was blocked; a batch with no rows writes nothing unless `--allow-empty`.

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
- **`--delay`**: the family's pause between page fetches. Here there is one
  page per search; the equivalents are `--scroll-delay` (between scroll
  rounds) and `batch_scraper.py --route-delay` (between searches).
- **`--locale`**: not added, because nothing measured says it would work.
  The market is picked by the site's own geo-redirect (measured 2026-09-29:
  `skyscanner.com` → `ru.skyscanner.com` → `skyscanner.net` for one exit),
  and `--currency` only sets the query parameter; the row's `currency` is
  whatever the data states. Whether a browser locale changes the market is
  untested — a TODO, not a decision.

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
disappearing and an unrelated one appearing (segment flight numbers are
part of it, so codeshares do not collide). `brand` is the first leg's
marketing airline. `category` is always `"flights"`. `currency` is the one
the data states (the deeplink's market currency) or `null` — never the
requested `--currency`. `price_source` is `search_api` (the XHR),
`embedded_json` or `dom`, whichever path produced the row. `sample_output.json`
/ `.csv` are cut from the real capture in `fixtures/` (3 itineraries,
LHR→JFK, 2026-09-29) by the same parser and writer a run uses.

**Exit codes**: `0` complete · `1` crash · `2` bad usage · `3` blocked ·
`4` zero itineraries (and nothing was written) · `5` the content was never
obtained (remote API error, navigation failure, dead proxy) · `6` partial.
Every completed/partial run writes a `<out>.meta.json` sidecar with
`status`, `stop_reason`, `pages_completed` (scroll rounds, here),
`failed_pages`, `price_confirmed_pct`, and the site's own arithmetic:
`results_source`, `search_api_status`, `itineraries_available`,
`capped_by_max_results`. So `complete` with `stop_reason: max_results`
means "we took 30 of the 389 the site offered"; a run that stopped while
the search API still said `incomplete` is `partial` (`stop_reason:
search_incomplete`). A failed/empty/blocked/never-obtained run writes no
sidecar and no output at all, so it can never overwrite a previous good
run (`--allow-empty` opts out of the "don't write an empty result" half of
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

- **A dead proxy ends the run (exit 5) instead of rotating within it.**
  The engines recognise `ERR_PROXY_CONNECTION_FAILED` and friends, mark the
  exit dead in the pool and stop retrying it; the next run takes the next
  proxy. Rotating to a fresh browser mid-run is not implemented.
- **Round trips carry the outbound leg only.** A row's `departure_time` /
  `arrival_time` / `duration` / `stops` describe the first leg; the return
  leg is in the XHR but not in the output schema yet.
- **Only the XHR path is verified.** The `__NEXT_DATA__` and DOM fallbacks
  in `flight_parser.py` are guesses marked `# TODO: verify live`.
- **Site-specific block marker: one, measured.** `flight_parser.
  BOT_CHALLENGE_MARKERS` is `("/sttc/px/captcha-v2/",)` — present on every
  captured challenge page, absent from a captured normal page.
  `px-cloud.net` was removed on 2026-09-29 because PerimeterX's background
  sensor loads it on normal pages too.
- **This repo does not implement captcha token injection**, so engines
  never pay for a solve (`allow_paid_solve=False`): a recognised widget is
  logged as `solve_not_attempted`. No reCAPTCHA/Turnstile/hCaptcha widget
  has been observed on this site to build an injector against. Over
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

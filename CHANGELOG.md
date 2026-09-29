# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [Unreleased]

### Fixed — 2026-09-29, template audit: Docker image and batch contract
- The Docker build would have failed: `batch_scraper.py` and `fixtures/`
  were not COPYed (and `.dockerignore` dropped `*.json`). Both added,
  fixtures stripped from the final layer, and a new smoke check compares
  the COPY list with the import graph (with two controls that must fail;
  skipped visibly inside the image, where no Dockerfile exists).
- `batch_scraper.py` now goes through `merge_pages` + `finish_run`: dedupe,
  `<out>.meta.json` with `per_route`, `--allow-empty`, and a batch with no
  rows writes nothing. `<out>.batch.json` is gone. An empty route is no
  longer a failure (ok + empty was exit 6, now 0).
- `pyproject.toml` lists `batch_scraper`; CI's `--help` step covers
  `batch_scraper` and `diff_runs`; the image-clean step checks `fixtures`.
- `finish_run()` takes an optional `stop_reason`.

### Added — 2026-09-29, `batch_scraper.py`: many routes, local Chrome first, 2Captcha fallback
- `--routes-file` CSV (`routes.example.csv`); per route: local Chrome
  (auto-started with a persistent profile) + `--wait-for-human`, then on
  blocked/remote error/crash the `.env` 2Captcha setup (Scraping Browser
  with fresh-session retries). Empty results are not retried.
  `--scraper-api` is not a fallback: it cannot see the results XHR.
- Merged output + `<out>.batch.json` per-route summary; exit 0/6/3.
- Live: 3/3 routes, 1690 itineraries via local Chrome, no challenge.
  Fallback live (local port forced dead): 2 Scraping Browser sessions,
  both PerimeterX → exit 3, reported as such.
- Known gap: round-trip rows carry only the outbound leg's times.

### Verified live, 2026-09-29 — first real skyscanner results, via `--wait-for-human`
- Playwright over `--cdp-endpoint http://127.0.0.1:9222` (Roman's own
  Chrome 154, fresh `--user-data-dir`, no proxy), LHR→JFK 2026-11-15:
  - run 1: PerimeterX "Press & Hold" appeared; Roman held it (~10s); exit 0.
  - run 2, same profile a few minutes later: **no challenge**; the
    `web-unified-search` capture returned `complete` → exit 0.
  - run 3 with `--max-results 1000`: **385/385 itineraries in 9.9s**, all
    with price, airline, times and deeplink, 385 unique skus.
- Two fixes that run 1 exposed:
  - Over a LOCAL `--cdp-endpoint`, Playwright now works in Chrome's own
    persistent profile (`browser.contexts[0]`) and closes only its tab.
    Before, every run got a fresh empty context, so a challenge a person
    cleared never stayed cleared (that is what made run 2 challenge-free).
  - After a geo redirect (`.com` → `ru.skyscanner.com` → `.net`) the
    challenge returned to the HOMEPAGE, not the search, so no more results
    arrived. All three engines now re-open the search if the page is not
    on a search URL after the challenge clears (verified on a local stand
    replaying that redirect, all three engines).

### Added — 2026-09-29, `--wait-for-human SECONDS`, all three engines
- On a bot challenge, print instructions and wait for a PERSON to complete
  it in the browser window (nothing is solved automatically), then carry on
  in that session. Forces `--headful` unless `--cdp-endpoint` points at a
  local Chrome; warns on a remote endpoint nobody can see. Timeout → exit 3.
- Verified on a local stand for all three engines (challenge page that
  clears after 5s → 3 rows, exit 0; one that never clears → exit 3).

### Added — 2026-09-29, all three engines capture the `web-unified-search` XHR
- The flight list is never in the initial HTML; the page fetches it from
  `web-unified-search` and re-polls while `context.status` is
  "incomplete". Playwright and pyppeteer hook `page.on("response")`;
  Selenium reads Chrome's performance log and fetches bodies with
  `Network.getResponseBody`. Shared logic: `flight_parser.SearchApiCapture`
  / `combine_results` — API rows first, HTML rows only for uncovered skus.
- A later poll refreshes the price of rows already collected, and an
  unchanged round is not counted as a stall while the search is still
  incomplete.
- `--dump-html` also writes the last captured body to
  `<out>_search_api_debug.json` (gitignored: it carries session tokens).
- Verified end-to-end for all three engines (Playwright, pyppeteer 2.0.0
  and Selenium 4.49 on system Chrome, each in its own venv) on a local
  stand that polls the real fixture twice: both polls captured, 3 rows,
  price refreshed from the second poll, exit 0. Not yet seen against the
  live site (every live run so far stops at PerimeterX first).
- `--scraper-api` returns HTML only and cannot see the XHR.

### Fixed — 2026-09-29, parser verified against the first REAL skyscanner capture
- Roman captured a real `web-unified-search` XHR body in his own Chrome
  (LHR→JFK, 2026-11-15, 373 itineraries). The generic list walk found the
  right list, but the parser produced **0 rows**: the price is `price.raw`,
  not `price.amount`, so every itinerary was dropped as price-less.
- Field mapping fixed against that capture: `price.raw`; airline from
  `legs[].carriers.marketing[]` (a dict, not a list); deeplink from
  `pricingOptions[0].items[0].url` (site-relative, now absolutised);
  currency taken from the deeplink path (the market currency, e.g. GBP,
  not necessarily the one requested). Old guesses kept as fallbacks.
  Result: 373/373 rows, all with price, airline, times, stops and a link.
- `sku` now includes segment flight numbers when known: 5 codeshare pairs
  (same airline + times, different connecting flight number) collided.
  DOM-path skus are unchanged.
- New `flight_parser.parse_search_json()` for an already-decoded search
  payload. The engines do not intercept this XHR yet — they still only
  parse HTML, whose initial render is an empty shell with no flights.
- `fixtures/web_unified_search_lhr_jfk_trimmed.json` (3 real results,
  tokens stripped) + a smoke check on it — the first non-synthetic fixture.

### Verified — 2026-09-22, `Captcha.setAutoSolve` coverage audited against a real gap found in shein-scraper
- Building a sibling family member (flippa-scraper) around Roman's explicit
  requirement that captcha auto-solve be armed on EVERY page an engine
  touches (not just its main entry point) surfaced a real, live gap in
  shein-scraper's `puppeteer_scraper.py`: a second page-creation path took an
  `autosolve` parameter but never actually called the arming helper with it.
  Auditing this repo for the same class of gap found it does NOT have one —
  `playwright_scraper.py` and `puppeteer_scraper.py` each have exactly ONE
  function that creates a page/browser and takes `autosolve` (this repo has
  no separate search-vs-product-detail split the way shein/flippa do — a
  flight search has no per-listing "detail page" the engines scrape), and
  both call sites are already correctly wired.
- `smoke_test.py` gained the same AST-based regression check ported from
  shein-scraper anyway, as a forward guard: it walks every function in
  `playwright_scraper.py`/`puppeteer_scraper.py` and asserts that any
  function taking an `autosolve` parameter actually calls
  `_enable_scraping_browser_auto_solve()` somewhere in its body, so this
  stays caught automatically if a second entry point (e.g. a per-flight
  detail fetch) is ever added later. `selenium_scraper.py` is correctly
  exempt — it refuses `--cdp-endpoint` outright (CLAUDE.md §6), so there is
  no CDP session to arm this on.

### Added — 2026-09-22, `--scraper-api`: fetch via 2Captcha's Scraper API, all three engines
- Directly prompted by Roman asking us to actually use ALL FIVE of 2Captcha's
  products for this account — captcha solving, the Scraping Browser (CDP),
  the Fingerprint API, proxies, and the Scraper API. The last of these had
  never been wired in at all here, despite `scraper_api_client.py` already
  being the shared HTTP client every other product goes through — same
  addition as shein-scraper's own `--scraper-api` (see that repo's
  CHANGELOG for the shared implementation notes), ported here because this
  repo is the one with the actually-unsolved problem (PerimeterX).
- **Also fixed in passing**: this repo's own `.env`'s `TWOCAPTCHA_KEY` did
  not work — `getBalance` returned `ERROR_KEY_DOES_NOT_EXIST` for it, so
  every captcha-solving/fingerprint call this repo has ever made with the
  default `.env` value was silently failing auth (caught as a WARNING per
  this repo's own error-handling policy, not a crash, which is exactly how
  it went unnoticed). Replaced with the same working key shein-scraper
  uses (confirmed live via `getBalance`, same 2Captcha account) — **Roman,
  worth double-checking `.env` has the key you actually intend for this
  repo.**
- **Live-tested against real skyscanner.com, all three engines, 2026-09-22
  — genuinely different failure shape from every other mitigation tried
  so far, but still not a working bypass.** Unlike `--cdp-endpoint` and
  `--cookies-file` (both of which hit PerimeterX's interactive "Press &
  Hold" challenge directly), the Scraper API NEVER showed a single
  PerimeterX marker in any live test today — no challenge UI, no
  `_pxCaptcha`, nothing. What came back instead, inconsistently across
  repeated identical requests:
  - Sometimes a bare, un-hydrated ~700-byte app shell (`<div id="root"></div>`, no content at all) — a SILENT failure mode, not a visible block.
  - Sometimes a real, full SEO-prerendered page (225-229KB, correct route,
    real `<title>`/meta description for the actual search) but in a
    non-English locale (nl-NL, fi-FI seen) determined by whatever exit IP
    2Captcha's own infrastructure happened to use that request — no
    documented way to pin it — and with ZERO client-rendered
    `[data-testid="result-card"]` elements: skyscanner.com's real flight
    results are fetched and injected by client-side JS/XHR calls AFTER
    the initial page loads, and a single static/one-shot fetch (even with
    `waitFor: {"state": "load"}`, the strongest wait condition this
    endpoint accepts) never waits for or triggers those calls.
  - The skyscanner.com HOMEPAGE, by contrast, rendered completely fine
    (265-267KB, real content) — so whatever produces the two failure
    shapes above is specific to the flight-search route, not a general
    inability to fetch skyscanner.com at all.
  Both failure shapes are handled without crashing: the bare-shell case is
  caught by an explicit shell-length heuristic (no `BOT_CHALLENGE_MARKERS`
  text exists to detect it any other way) and reported `EXIT_BLOCKED`
  (retried via `--cdp-block-retries`, reused as this mode's retry knob);
  the real-page-zero-cards case reports `EXIT_ZERO_PRODUCTS`, not a false
  `EXIT_OK`.
- **Conclusion**: this is genuinely new information (a WAF/CDN-level
  response difference from what a real browser sees), not just another
  way of hitting the same wall — but it does not change this repo's
  overall verdict. PerimeterX (or something upstream of it, on the
  flight-search route specifically) still prevents this repo from getting
  real itinerary data through any path tried so far: local browser,
  `--cdp-endpoint`, `--cookies-file`, `--stealth`, and now `--scraper-api`.
  See README's "Read this before trusting a run" for the standing,
  now five-mitigations-deep, honest status.
- Not implemented/tried: `scrape_url()`'s `cdp_url` parameter (2Captcha's
  `cdpurl` field) would let this mode's fetch run through a caller-
  controlled CDP session — e.g. this repo's own `--cdp-endpoint` — instead
  of 2Captcha's own default pool. Since `--cdp-endpoint` alone already hits
  the interactive challenge directly, this combination seems unlikely to
  help, but it hasn't actually been tried.

### Added — 2026-09-22, `--stealth`: patch common automation tells, across all three engines
- Direct follow-up to the entry above: since a static `--cookies-file`
  snapshot alone didn't clear PerimeterX even from the solving IP, the next
  variable to isolate is the browser's own automation fingerprint. `--stealth`
  (off by default) patches, before any page loads: `navigator.webdriver`
  (hidden instead of `true`), `window.chrome.runtime` (populated instead of
  absent), `navigator.plugins`/`navigator.languages` (fake PDF-viewer
  entries instead of empty), `permissions.query('notifications')` (matched
  to `Notification.permission` instead of the automation-only mismatch), and
  WebGL vendor/renderer (`Intel Inc.` / `Intel Iris OpenGL Engine` instead of
  the CDP-default SwiftShader/Google Inc. strings that are themselves a tell)
  — plus a `toString()` patch on the patched `permissions.query` so the patch
  itself doesn't become the more obvious tell. Same script content in all
  three engines (`playwright_scraper.py` via `context.add_init_script()`,
  `selenium_scraper.py` via the `Page.addScriptToEvaluateOnNewDocument` CDP
  command, `puppeteer_scraper.py` via `page.evaluateOnNewDocument()`) — each
  runs on every document in the context/page before that document's own
  scripts execute, and each failure path is caught and logged, never fatal
  (CLAUDE.md §6). Explicitly **not** a captcha solver or bypass: it changes
  what this browser reveals about itself, nothing about PerimeterX's own
  challenge logic. Unverified as of this entry — combine with
  `--cookies-file` and test live before drawing any conclusion about whether
  it actually helps; `smoke_test.py` confirms only that all three engines
  expose the identical flag (CLAUDE.md §4 parity), not that it works.

### Fixed — 2026-09-22, a real crash: page.content() racing a client-side navigation/reload
- Directly hit live by Roman's own `--cookies-file` test over `--cdp-endpoint`:
  the round loop's `html = await page.content()` is not itself a captcha
  interaction, but PerimeterX's own challenge page apparently keeps
  reloading/redirecting itself client-side, and calling `page.content()`
  at the wrong instant raised `Page.content: Unable to retrieve content
  because the page is navigating and changing the content.` — unhandled,
  so it crashed the ENTIRE run (`EXIT_CRASH`, not `EXIT_BLOCKED`),
  discarding whatever had already been collected. This violated CLAUDE.md
  §6 (a bad round degrades, it never takes the whole run down). Both
  `page.content()` call sites in `scrape_search()` (the round loop, and
  the final `--dump-html` capture) now catch this, wait briefly, retry
  once, and degrade this round/the dump gracefully on a second failure
  rather than raising. Verified live immediately after: the same command
  that crashed now completes cleanly as `EXIT_BLOCKED` (3) instead.

### Verified live, 2026-09-22 — `--cookies-file` alone does NOT clear PerimeterX, even from the exact IP that solved it
- Directly answering the open question from the entry below (`--cookies-
  file` was implemented 2026-09-20 but never actually run live until
  today): Roman solved the PerimeterX "Press & Hold" challenge himself in
  his own real Chrome, exported all 13 resulting cookies (including the
  load-bearing `_px3`, `pxcts`, `_pxvid`, and a signed `__Secure-
  session_id` JWT) via the Cookie-Editor extension, and ran this repo
  with `--cookies-file` pointed at the converted, `_load_cookies_file()`-
  validated result.
  - **First attempt, over `--cdp-endpoint` (2Captcha Scraping Browser)**:
    challenged immediately, on every one of 3 attempts (initial +
    2 `--cdp-block-retries`). Confounded: the cookies came from Roman's
    own machine/IP, but were presented through a completely different
    exit IP and device (the Scraping Browser's own managed session) —
    IP/device mismatch alone could fully explain this result without
    saying anything about whether the cookies themselves are any good.
  - **Second attempt, `SKYSCANNER_PROXY`/`SKYSCANNER_CDP_ENDPOINT` both
    temporarily disabled** (a plain local Playwright-launched Chromium,
    direct connection, Roman's own real IP — the same network the
    cookies were solved on): challenged again, just as fast (the crash
    above happened on THIS run — caught by the fix immediately above).
  - **Conclusion**: even controlling for IP, a static cookie snapshot
    handed to a bare, un-stealth-patched Playwright browser does not
    clear PerimeterX. The most likely explanation, not yet independently
    confirmed: PerimeterX's `_px3`/`pxcts` cookies are refreshed
    continuously by its own JS sensor script running inside a real,
    ongoing browsing session, and/or PerimeterX's device fingerprint
    (Playwright's default `navigator.webdriver = true` among other
    automation tells) is itself sufficient to re-challenge regardless of
  which cookies are attached — a static snapshot replayed into a
  DIFFERENT browser process, even same-IP, is not the same "device" the
  cookies were issued to. This matches etsy-scraper's own README noting
  its DataDome cookie is "bound to the address that solved it" — the
  binding is plausibly broader than IP alone.
  - **Not yet tried**: a stealth-patched browser (e.g. removing
    `navigator.webdriver`, canvas/WebGL noise matching a real profile)
    combined with `--cookies-file`, which is a materially bigger
    engineering investment than this repo's existing three honest,
    non-bypass mitigations and has no guarantee of working against a
    system this well-defended. Until/unless that's tried, the PerimeterX
    gap documented below stands exactly as CONFIRMED PERMANENT as it did
    before this test — `--cookies-file` is now confirmed NOT to be the
    missing piece on its own, not merely still-untested.

### Verified live, 2026-09-21 — `--cdp-block-retries` exercised end-to-end
### Verified live, 2026-09-21 — `--cdp-block-retries` exercised end-to-end
- `python3 playwright_scraper.py --origin LHR --destination JFK
  --depart-date 2026-10-15 --max-results 10 --format json --out
  /tmp/sky_test.json --dump-html` run against the real, live site over a
  real 2Captcha Scraping Browser API session (`SKYSCANNER_CDP_ENDPOINT`
  from `.env`, picked up automatically). All three attempts (the initial
  connection plus both `--cdp-block-retries` retries) hit the PerimeterX
  challenge; each retry reconnected for a fresh session as designed
  (confirmed in the log), and each fresh session was challenged just as
  quickly as the last. Final outcome: exit `3` (blocked), no `.meta.json`
  sidecar written (correct per the output contract). This is the first
  time the retry mechanism itself — not just the base block — has been
  exercised against the real site: it behaves exactly as coded, and the
  underlying PerimeterX gap remains exactly as confirmed-permanent as the
  2026-09-20 entry below already says.

### Added — 2026-09-20, the confirmed-permanent PerimeterX gap and its three non-bypass mitigations
- **Confirmed, not just unsolved-so-far**: a real run against the real
  site, over a real 2Captcha Scraping Browser API session
  (`--cdp-endpoint`), still got served the PerimeterX challenge — the
  Scraping Browser's own bundled auto-solve extension did not clear it.
  2Captcha confirmed directly: there is no automated task type for
  PerimeterX at all. `captcha_solver.CaptchaType` was never going to grow
  a PerimeterX entry that doesn't exist on 2Captcha's side, so this repo
  ships three mitigations instead, none of which attempt to solve or
  bypass the challenge itself:
  1. **`captcha_solver.identify_unsupported_vendor()`** (new function) —
     names the real vendor (`perimeterx` / `datadome` /
     `cloudflare_managed_challenge`) in the log, replacing the old, vague
     "no known widget/sitekey could be extracted" message, which read
     like a parser bug rather than a real, confirmed product gap.
     `solve_when_blocked()` now returns `{"action": "unsupported_vendor",
     "vendor": ...}` instead of falling through to
     `"detected_unidentified_widget"` when a known vendor is present.
  2. **`--cdp-block-retries`** (new flag, all three engines, default 2):
     on a blocked `--cdp-endpoint` run, reconnect for a fresh session and
     retry — genuinely fresh on Playwright/Puppeteer (a new exit
     identity), a documented, weaker no-op-ish retry on Selenium (same
     underlying browser — see README "Engines").
  3. **`--cookies-file`** (new flag, all three engines): load cookies
     (Playwright/CDP cookie-object shape) from a session a HUMAN solved
     manually in a real browser once, before navigating. Reuse of a
     person's own solve, never an automated one.
- All three engines' `_maybe_solve_captcha()` gained an
  `elif action == "unsupported_vendor":` branch (Selenium and Puppeteer
  also gained the pre-existing-but-missing `"detected_unidentified_
  widget"` log branch in the same pass, a small gap found while making
  this change, not a new one it introduced). `smoke_test.py` gained six
  new checks covering the vendor-naming logic, the new action passing
  through all three engines' captcha-handling wiring without crashing,
  the new flags' presence/defaults/parity, `_load_cookies_file`'s JSON
  validation (identical across all three engines), and a malformed
  `--cookies-file` correctly exiting `EXIT_BAD_USAGE` rather than
  crashing.

### Verified live, 2026-09-17 — this repo's first real fact about skyscanner.com
- **`flight_parser.BOT_CHALLENGE_MARKERS`** went from deliberately empty
  to two real entries (`px-cloud.net`, `js.skyscnr.com/sttc/px/`), from an
  actual captured incident: a plain local-first `playwright_scraper.py`
  run (no proxy, no fingerprint) got served PerimeterX's "Are you a
  person or a robot?" challenge page instead of search results —
  correctly reported as exit `3` (blocked), caught even before this
  change by `captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS`'s own
  `"px-captcha"` string. The two new markers are corroboration from the
  same real page, not a replacement for that generic check.
  **What this does NOT verify**: `flight_parser.py`'s selectors/JSON
  heuristics are still unconfirmed — a block page is not a results page.
  See `TESTING.md` step 2 for next steps (this repo has no automated way
  to solve a `px-captcha` challenge locally — `captcha_solver.py`'s
  `CaptchaType` enum has no PerimeterX entry; only a `--cdp-endpoint` run
  can get past one, via the Scraping Browser API's own auto-solve
  extension).

### Added — bringing this repo into compliance with the family's full CLAUDE.md standard
- **`--fingerprint` / `--fp-tags` / `--fp-country` now work on all three
  engines**, not just Playwright. `selenium_scraper.py` applies the
  fetched user agent via Chrome's own `--user-agent=` switch;
  `puppeteer_scraper.py` via pyppeteer's `page.setUserAgent()`. Neither is
  a driver limitation (unlike the CDP-auth/proxy-auth cases elsewhere in
  this file) — it was a silent per-engine gap, confirmed also present,
  unfixed, in `stockx-scraper`'s selenium/puppeteer engines (left there
  per the standing instruction not to touch that repo). Verified live
  against a local mock 2Captcha server on all three engines: the fixture
  page each engine navigated to observed the mock profile's exact user
  agent string in its own request headers.
- **`--captcha-api`** (override the 2Captcha REST base URL) and
  **`--min-score`** (2Captcha's `minScore` field on a `RecaptchaV3Task`)
  on all three engines, matching `stockx-scraper`'s flag names —
  but wired to real behavior here, not copied as dead flags (see Fixed
  below for what copying them verbatim would have shipped).
  `TwoCaptchaClient` now takes an `api_base` constructor argument instead
  of reading the module-level `API_BASE` global directly, which is also
  what let `--captcha-api` be verified live (pointed at a local mock
  server) rather than only unit-tested.
- Five smoke_test.py checks ported from CLAUDE.md §17's own "five checks
  worth stealing" list, adapted to this repo: (1) every engine's
  `_maybe_solve_captcha` binds against `captcha_solver.solve_when_
  blocked`'s real signature via `inspect.signature(...).bind(...)`; (2)
  all three engines expose the identical `--flag` set (nothing existed to
  catch this before — it's how the `--fingerprint` gap on Selenium/
  Puppeteer above was actually found); (3) `--captcha-api`/`--min-score`
  are asserted to be READ by every engine, not just defined, and
  `minScore` is asserted to reach the real `createTask` payload for a
  RecaptchaV3 task only (never Turnstile/RecaptchaV2/HCaptcha, which
  don't document that field); (4) `.env.example` round-trips through the
  real `env_config` loader with every credential reading as unset; (5) a
  regression test locking in the `ProxyParseError` fix below.
- **Documented, not just silently absent**: why `--concurrency` and
  `--proxy-rotate` (family flags) don't exist here, why `--pages`/
  `--category` don't apply, and why this repo has no `page_flow.py` —
  see README "Family flags that don't apply here" and "Why there's no
  `page_flow.py` here". Each is a structural property of a single
  scroll-based search page, not an oversight, and CLAUDE.md §1 asks for
  the reason to be written down rather than left as a silent deviation.
  Also documented (README "Before this repo goes public") the three
  CLAUDE.md §15 steps that only apply once this repo is actually
  published — org profile row, GitHub "About" panel, git-history
  credential scan — as pending, not done, since this repo isn't public.
- `fingerprint_client.py`'s module now states explicitly why it applies
  ONLY the user agent from a Fingerprint API profile, never a locale or
  timezone: 2Captcha's own public reference page for this endpoint 404s,
  and guessing a field name is exactly the mistake an older sibling repo
  made (a fabricated `en-{country}` locale, a timezone field never read)
  — see README "What this repo deliberately does NOT apply from a
  fingerprint".

### Fixed — found by this pass's own audit against the family standard
- **A malformed `--proxy` / `--proxy-file` line crashed with a raw,
  uncaught `ProxyParseError` traceback instead of a clean `EXIT_BAD_USAGE`
  (2)**, in all three engines. `load_proxies()` runs before each engine's
  `run()` even reaches its own try/except, so the exception reached the
  top level unhandled — Python's own default exit code for an uncaught
  exception (1) happened to collide with this scraper's `EXIT_CRASH`
  (also 1), which made the wrong classification easy to miss, but a typo
  in a proxy string is a usage mistake, not a scraper crash, and a
  caller (CI/cron) needs the right code to act on it. Now caught and
  reported as `EXIT_BAD_USAGE` with a clean one-line message, same as a
  bad `--format`. **Confirmed the identical, still-unfixed bug live in
  `stockx-scraper`'s `playwright_scraper.py`** (fixed here only, per the
  standing instruction not to touch that repo).
- **A 2Captcha REST connection failure (API down, DNS failure, a
  misconfigured `--captcha-api`) crashed the run instead of degrading to
  a warning.** `scraper_api_client.TwoCaptchaClient._post()` — the method
  behind `createTask`/`getTaskResult`/`getBalance` — let a bare
  `requests.exceptions.RequestException` propagate uncaught; `captcha_
  solver.solve_when_blocked()`'s `except TwoCaptchaAuthError / except
  TwoCaptchaError` pair never sees it, so it reaches the top level as an
  unhandled crash instead of "captcha solving skipped, continuing" —
  directly contradicting this codebase's own stated policy ("a
  solver-side error is a WARNING, never a crash"). Now wrapped and
  re-raised as `TwoCaptchaError` with credentials redacted, the same
  pattern `random_fingerprint()` already used for its own connection
  errors. **Confirmed the identical, still-unfixed gap live in
  `stockx-scraper`'s `scraper_api_client.py`** (fixed here only).

### Added
- **`SELENIUM_CHROMEDRIVER_PATH` / `CHROMEDRIVER_PATH`** env var override
  for `selenium_scraper.py`. Selenium 4's Selenium Manager resolves/
  downloads a matching chromedriver over the network by default
  (`googlechromelabs.github.io`) whenever `Service()` is called with no
  explicit path — confirmed live: it fails outright in a
  network-restricted environment (no outbound access to that host) even
  when a compatible chromedriver is already installed and on `PATH`.
  Mirrors the escape hatches `playwright_scraper.py`
  (`PLAYWRIGHT_BROWSERS_PATH`) and `puppeteer_scraper.py`
  (`PYPPETEER_EXECUTABLE_PATH`/`PUPPETEER_EXECUTABLE_PATH`) already had,
  so this engine isn't the one that can never run offline/in CI without
  internet access to GitHub.

### Fixed
- **`EXIT_PARTIAL` (6) was unreachable in all three engines.** Every
  engine's scroll loop already had a "scroll failed, stopping pagination
  early" break path (a real fallback for a mid-run failure — e.g. a
  detached frame or a closed context), but it never recorded that as a
  failed page: `finish_run()` was always called with `failed_pages=None`,
  so a run that stopped early because something broke mid-pagination was
  reported as `status: "complete"`, exit `0` — indistinguishable from a
  run that reached `--max-results` or `--stall-rounds` on its own.
  `scrape_search()` in each engine now returns a `scroll_error` flag, and
  `run()` reports `failed_pages` accordingly, so an early-broken scroll
  loop correctly surfaces as `status: "partial"`, exit `6`, with
  whatever itineraries were already collected still written out — never
  silently laundered into "complete".
- **`pages_completed` was off by one in all three engines**, which also
  masked the fix above whenever the scroll loop broke on its very first
  round: the loop's `round_num` is 0-indexed, but round `round_num`'s
  parse always succeeds *before* any break check runs, so the actual
  count of successfully processed rounds is `round_num + 1`. Reporting
  the raw 0-indexed value meant `finish_run`'s `pages_completed > 0`
  partial-gate rejected a same-round scroll failure (`pages_completed`
  read `0` even though round 0's data was genuinely collected). Fixed by
  reporting `rounds + 1`; confirmed live against a local synthetic
  fixture with an injected scroll failure (see `TESTING.md`).
- **A failed `--cdp-endpoint` connection was reported as `EXIT_CRASH` (1)
  instead of `EXIT_REMOTE_API_ERROR` (5)** in `playwright_scraper.py` and
  `puppeteer_scraper.py` — confirmed live against a closed local port
  before the fix (exit `1`) and after (exit `5`). A broken/misconfigured
  remote CDP session (2Captcha Scraping Browser API down, wrong
  endpoint, expired session) is a remote-dependency failure, not a bug
  in this scraper, and a caller (CI/cron) needs to tell those apart to
  act correctly (page an engineer vs. retry/alert on the dependency).
  Both engines now catch the CDP connection failure specifically and
  route it through the same `remote_api_error` outcome the
  permanent-navigation-failure path already used, instead of letting it
  fall through to the generic crash handler. `selenium_scraper.py`
  already refuses `--cdp-endpoint` outright (`EXIT_BAD_USAGE`) and
  needed no change.
  **Known, not fixed here:** `stockx-scraper`'s `playwright_scraper.py`
  and `puppeteer_scraper.py` have the identical pre-existing behaviour
  (confirmed by reading that repo's source) — this fix was applied to
  `skyscanner-scraper` only, per the immediate request to test and fix
  this repo. The two repos' exit-code contracts now diverge on this one
  case; worth fixing in `stockx-scraper` too for family consistency.

## [1.0.0] — 2026-09-16

Initial release — a family-grade rebuild of an earlier single-file draft,
matching the architecture, output contract, and CI shape of this family's
`stockx-scraper`.

### Added
- **Three engines, one contract.** `playwright_scraper.py` (primary,
  **local-first** — see "Known limitations"), `selenium_scraper.py`, and
  `puppeteer_scraper.py` (via pyppeteer) share the same CLI flags, the
  same `Product` output schema, and the same exit codes (`0` complete ·
  `1` crash · `2` bad usage · `3` blocked · `4` zero itineraries · `5`
  remote API error · `6` partial), via a shared `output_writer.
  finish_run()` — ported from stockx-scraper's post-audit version, so the
  outcome-precedence bug fixed there (blocked/remote-API-error being
  silently ignored whenever results were present or `--allow-empty` was
  set) can't recur here either; a smoke_test.py check pins it.
- **Flight search, with scroll-based pagination.** skyscanner.com's search
  results lazy-load on scroll rather than exposing numbered pages, so
  pagination here is a scroll-and-dedupe loop (by a deterministic
  itinerary fingerprint — see `Product.sku`'s docstring in
  `output_writer.py`) instead of stockx-scraper's `?page=N` loop.
- **Embedded-JSON-first parsing, with a DOM fallback** — same family
  principle as stockx-scraper, applied more cautiously here: no confirmed
  query name exists for this site (unlike stockx.com's `getDiscoveryData`),
  so the embedded-JSON path is a generic structural heuristic over
  `__NEXT_DATA__`, not a pinned shape.
- **JSON and CSV export**, a documented `Product` schema, and the same
  `.meta.json` run sidecar contract as the rest of the family.
- **Optional 2Captcha integration**, reusing the family's site-agnostic
  modules near-verbatim: captcha solving, the Scraping Browser API over
  `--cdp-endpoint`, the Fingerprint API, and residential proxies
  (`--proxy` / `SKYSCANNER_PROXY`) — all wired in, **none required to get
  started** (see "Local-first" below).
- `smoke_test.py`: an offline suite against SYNTHETIC fixtures (see its
  module docstring on why — no real capture of this site exists), plus the
  family's reused, still-valid checks (credential redaction, proxy pool,
  CLI validation, diff_runs).
- CI: an offline test matrix across two Python versions, one
  `engine-smoke` job per engine in its own virtualenv, a Docker build
  check, and a `canary` workflow — `canary-local` runs the local-first
  default against the real site on a schedule with **no secrets
  required**, since (unlike stockx-scraper) no live measurement of this
  site's bot-management behaviour exists yet to justify skipping it.
- `README.md` / `TESTING.md` / `CONTRIBUTING.md` documenting the CLI, the
  `Product` schema, the exit-code contract, and — prominently, unlike a
  typical v1.0.0 — exactly what has and hasn't been verified against the
  real site.

### Known limitations (see README for the full list)
- No selector or JSON-shape in `flight_parser.py` has been checked against
  a live response from skyscanner.com. `robots.txt` blocks automated
  fetching of this site's search pages for this project's own tooling, so
  every extraction path is a documented best-effort guess pending a real
  capture — see `TESTING.md` step 2 for the checklist to close that gap.

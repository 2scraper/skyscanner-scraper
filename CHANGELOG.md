# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [Unreleased]

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

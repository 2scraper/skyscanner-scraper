#!/usr/bin/env python3
"""playwright_scraper.py — Playwright engine for the skyscanner-scraper
family member. Playwright is the primary engine (see selenium_scraper.py /
puppeteer_scraper.py for parity copies — all three must agree on exit
codes, run status and whether a run crashes or spends money).

**Local-first, unlike the original draft**: this launches an ordinary
local headless Chromium by default and does NOT require a 2Captcha
Scraping Browser (`--cdp-endpoint`) session to run at all. That mirrors
stockx-scraper's own demonstrated principle — a plain local browser was
measured live to work against stockx.com with no proxy and no CAPTCHA
key, and the paid products are for volume/a specific exit country/a
consistent device identity, not because the free path is blocked. No
equivalent live measurement exists yet for skyscanner.com (see
flight_parser.py's module docstring on why: WebFetch on this site returns
ROBOTS_DISALLOWED, so nothing here has been checked against a live
response) — `--cdp-endpoint` and `--proxy` remain available as opt-in
power options for exactly the same reasons stockx-scraper offers them,
not because local-first is known to fail here.

Example:
    python3 playwright_scraper.py --origin LHR --destination JFK --depart-date 2026-09-15 \\
        --return-date 2026-09-22 --format json --out results.json
    python3 playwright_scraper.py --url "https://www.skyscanner.com/transport/flights/lhr/jfk/260915/" --max-results 20

Pagination model differs from stockx-scraper: skyscanner.com's search
results lazy-load on scroll rather than exposing numbered pages (see
README "Pagination"), so this engine scrolls and re-parses the
accumulated page, deduping by `sku` (output_writer.sku_key — same
"did this round add anything NEW" rule stockx-scraper's pagination loop
uses) until `--max-results` is reached or `--stall-rounds` consecutive
scrolls add nothing new.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    Browser = BrowserContext = Page = None
    async_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

import env_config
import flight_parser as fp
from captcha_solver import detect_from_html, solve_when_blocked
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run, merge_pages, sku_key as _sku_key
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

ENGINE_NAME = "playwright"

# --- the handful of engine constants that vary per site (CLAUDE.md §5) ---
NAV_TIMEOUT_MS = 30_000
READINESS_WAIT_MS = 3_000  # skyscanner.com is a client-rendered SPA — see README
MIN_CARD_MATCHES = fp.MIN_CARD_MATCHES

log = logging.getLogger("playwright_scraper")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def _nonnegative_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return ivalue


def _nonnegative_float(value: str) -> float:
    fvalue = float(value)
    if fvalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return fvalue


def _load_cookies_file(path: Optional[str]) -> Optional[list]:
    """Load a Playwright-shaped cookie list (the same JSON array
    `context.cookies()` produces / `context.add_cookies()` accepts) from a
    file a HUMAN produced by solving a challenge themselves in a real
    browser once. This is the third of the three PerimeterX mitigations
    (see captcha_solver.py's module docstring): 2Captcha has no automated
    solve for PerimeterX, so the only honest way to get PAST an already-
    established block is to reuse a session a person cleared manually —
    never to have this scraper attempt the clearing itself.

    Raises ValueError (caller converts to EXIT_BAD_USAGE) on anything that
    isn't a plain JSON array of cookie dicts — a wrong-shaped file here
    should fail loudly at startup, not surface as a mysterious empty
    context.add_cookies() no-op three steps into a run.
    """
    if not path:
        return None
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"--cookies-file {path!r} could not be read: {exc}") from None
    try:
        cookies = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--cookies-file {path!r} is not valid JSON: {exc}") from None
    if not isinstance(cookies, list) or not all(isinstance(c, dict) for c in cookies):
        raise ValueError(f"--cookies-file {path!r} must be a JSON array of cookie objects (e.g. from context.cookies())")
    return cookies


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="skyscanner.com flight search scraper — Playwright engine",
        epilog="Credentials belong in .env / SKYSCANNER_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None, help="Full skyscanner.com search URL (or set SKYSCANNER_URL) — overrides --origin/--destination/--depart-date/--return-date")
    p.add_argument("--origin", default=None, help="IATA airport code, e.g. LHR")
    p.add_argument("--destination", default=None, help="IATA airport code, e.g. JFK")
    p.add_argument("--depart-date", default=None, help="YYYY-MM-DD")
    p.add_argument("--return-date", default=None, help="YYYY-MM-DD — omit for a one-way search")
    p.add_argument("--adults", type=_positive_int, default=1)
    p.add_argument("--currency", default="USD")
    p.add_argument("--cabin-class", choices=fp.CABIN_CLASS_VALUES, default="economy")
    p.add_argument("--stops", choices=fp.STOPS_VALUES, default="any")
    p.add_argument("--sort", choices=fp.SORT_VALUES, default="best")
    p.add_argument("--max-results", type=_positive_int, default=30, help="Cap on number of itineraries scraped")
    p.add_argument("--max-scrolls", type=_positive_int, default=20, help="Hard cap on scroll rounds, independent of --stall-rounds")
    p.add_argument("--stall-rounds", type=_positive_int, default=4, help="Stop after this many consecutive scrolls add no new itinerary")
    p.add_argument("--scroll-delay", type=_nonnegative_float, default=1.5, help="Delay between scroll rounds, seconds")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None, help="Output path (default: skyscanner_results.<format>)")
    p.add_argument("--retries", type=_nonnegative_int, default=2, help="Retries on initial navigation failure")
    p.add_argument("--retry-delay", type=_nonnegative_float, default=3.0)
    p.add_argument("--proxy", default=None, help="A single proxy, e.g. http://login:pass@host:port (or set SKYSCANNER_PROXY)")
    p.add_argument("--proxy-file", default=None, help="One proxy per line, same formats as --proxy")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None, help="(or set TWOCAPTCHA_KEY)")
    p.add_argument("--captcha-api", default=None, help="Override the 2Captcha API base URL (testing only)")
    p.add_argument("--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--cdp-endpoint", default=None, help="Connect to a remote CDP session (e.g. the 2Captcha Scraping Browser API) instead of launching locally (or set SKYSCANNER_CDP_ENDPOINT) — opt-in, not required for a normal run")
    p.add_argument("--cdp-block-retries", type=_nonnegative_int, default=2, help="On a --cdp-endpoint run that comes back blocked (e.g. a PerimeterX challenge 2Captcha cannot solve — see captcha_solver.py), reconnect for a fresh Scraping Browser session (a different exit identity from the pool) and retry this many times before giving up. Reputation/behavior-based defenses sometimes simply don't challenge a fresh session at all. Has no effect without --cdp-endpoint.")
    p.add_argument("--cookies-file", default=None, help="Path to a JSON array of cookies (Playwright's context.cookies() shape) from a session a HUMAN solved manually — loaded into the browser context before navigating. This is a way to reuse a person's own solve, never a way to solve a challenge automatically.")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument(
        "--stealth", action="store_true",
        help="Patch common automation tells (navigator.webdriver, window.chrome, WebGL vendor/"
             "renderer, plugin list) before any page loads. Experimental, added 2026-09-22 after "
             "a real --cookies-file test still got challenged by PerimeterX from the exact IP that "
             "solved it — see CHANGELOG.md for what this has and hasn't been confirmed to fix. Not "
             "a captcha solver or bypass: it changes what THIS browser reveals about itself, "
             "nothing about the challenge.",
    )
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows,Chrome'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument(
        "--scraper-api", action="store_true",
        help="Fetch via 2Captcha's Scraper API (scraper.2captcha.com) instead of launching any local "
             "or --cdp-endpoint browser — a single browserless HTTP call, run entirely on 2Captcha's "
             "own infrastructure. Requires --twocaptcha-key/TWOCAPTCHA_KEY. A GENUINELY DIFFERENT "
             "product from --cdp-endpoint's Scraping Browser API — see scraper_api_client.py's module "
             "docstring. Live-tested against real skyscanner.com, 2026-09-22: the homepage renders "
             "fine (real, full HTML), but the actual flight-search results page — the one behind "
             "PerimeterX — comes back as a bare, un-hydrated app shell every time (708 bytes, no "
             "results, no visible challenge UI either): a SILENT block, different in kind from the "
             "interactive Press & Hold challenge a real browser hits, but a block nonetheless. NOT a "
             "confirmed bypass — see CHANGELOG.md for the full write-up. --max-scrolls/--stall-rounds/"
             "--scroll-delay/--proxy/--cdp-endpoint/--fingerprint/--cookies-file are all IGNORED in "
             "this mode (a single static fetch has no scroll loop, no live page/DOM to inject cookies "
             "into, and brings its own exit IP/device) — set together, they log a warning rather than "
             "silently doing nothing.",
    )
    p.add_argument("--scraper-api-timeout", type=_positive_int, default=60, help="Seconds 2Captcha itself waits for the target page to finish loading (1-120, their limit)")
    p.add_argument("--scraper-api-url", default=None, help="Override the Scraper API base URL (testing only)")
    p.add_argument("--allow-empty", action="store_true", help="Write output even if zero itineraries were found")
    p.add_argument("--dump-html", action="store_true", help="Save the final accumulated page HTML next to --out, on success too")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"skyscanner_results.{fmt}"


def _resolve_start_url(args: argparse.Namespace) -> Optional[str]:
    if args.url:
        return args.url
    if args.origin and args.destination and args.depart_date:
        return fp.search_url(
            origin=args.origin, destination=args.destination, depart_date=args.depart_date,
            return_date=args.return_date, adults=args.adults, currency=args.currency,
            cabin_class=args.cabin_class, stops=args.stops, sort=args.sort,
        )
    return None


def _dump_path(out_path: str) -> str:
    stem = Path(out_path).with_suffix("")
    return f"{stem}_debug.html"


# --stealth (added 2026-09-22, directly prompted by the --cookies-file
# result below: even from the exact IP that solved a real PerimeterX
# challenge, a bare Playwright-launched Chromium got re-challenged just as
# fast — automation fingerprint tells (navigator.webdriver and friends),
# not IP or cookie possession, are the more likely explanation). This is a
# battery of the standard, publicly-documented evasions used across the
# scraping ecosystem (the same category "puppeteer-extra-plugin-stealth"
# ships) — patch what a headless/automation-controlled Chromium reveals
# about itself, not anything about solving or bypassing PerimeterX's own
# challenge. UNCONFIRMED whether this actually clears PerimeterX — see
# CHANGELOG.md for what's been measured so far, not assumed.
_STEALTH_INIT_SCRIPT = """
(() => {
  // navigator.webdriver: the single most commonly checked automation tell.
  Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined, configurable: true });

  // A real Chrome has a populated window.chrome.runtime; a bare Playwright/
  // CDP session does not.
  if (!window.chrome) { window.chrome = {}; }
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      connect: () => {}, sendMessage: () => {}, id: undefined,
    };
  }

  // navigator.plugins/mimeTypes: empty in a bare automated context; a real
  // Chrome always reports the built-in PDF viewer entries at minimum.
  const fakePlugins = [
    { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
  ];
  Object.defineProperty(Navigator.prototype, 'plugins', { get: () => fakePlugins, configurable: true });
  Object.defineProperty(Navigator.prototype, 'languages', { get: () => ['en-US', 'en'], configurable: true });

  // permissions.query('notifications') returns 'denied' under a bare
  // automated context inconsistently with Notification.permission — a
  // known, specifically-checked mismatch.
  const origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (origQuery) {
    window.navigator.permissions.query = (params) => (
      params && params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission, onchange: null })
        : origQuery(params)
    );
  }

  // WebGL vendor/renderer: SwiftShader/Google Inc. (the CDP-default
  // software renderer) is itself a strong automation signal — report a
  // plausible real-hardware string instead.
  const getParameterPatch = (proto) => {
    const orig = proto.getParameter;
    proto.getParameter = function (parameter) {
      if (parameter === 37445) return 'Intel Inc.';
      if (parameter === 37446) return 'Intel Iris OpenGL Engine';
      return orig.apply(this, arguments);
    };
  };
  try { getParameterPatch(WebGLRenderingContext.prototype); } catch (e) {}
  try { getParameterPatch(WebGL2RenderingContext.prototype); } catch (e) {}

  // A patched native function's own toString() must still look native —
  // otherwise the patch above becomes its own, more obvious tell.
  const nativeToStringPatch = (fn, name) => {
    try {
      fn.toString = () => `function ${name}() { [native code] }`;
    } catch (e) {}
  };
  nativeToStringPatch(window.navigator.permissions.query, 'query');
})();
""".strip()


async def _new_context(
    browser: Browser, proxy: Optional[Proxy], user_agent: Optional[str], cookies: Optional[list] = None,
    stealth: bool = False,
) -> BrowserContext:
    kwargs = {}
    if proxy is not None:
        kwargs["proxy"] = proxy.playwright_proxy_dict()
    if user_agent:
        kwargs["user_agent"] = user_agent
    context = await browser.new_context(**kwargs)
    if stealth:
        # Before any page/navigation exists in this context, so it runs on
        # every document (main frame and any iframe) before that page's own
        # scripts get a chance to observe the unpatched originals.
        await context.add_init_script(_STEALTH_INIT_SCRIPT)
    if cookies:
        # A manually-solved session's cookies (see --cookies-file / captcha_
        # solver.py's module docstring) — added before any navigation so
        # the first request already carries them, same as a real returning
        # visitor's browser would.
        await context.add_cookies(cookies)
    return context


async def _enable_scraping_browser_auto_solve(context: BrowserContext, page: Page) -> None:
    """Only meaningful over --cdp-endpoint — see the identical helper and
    its docstring in stockx-scraper's playwright_scraper.py."""
    try:
        session = await context.new_cdp_session(page)
        session.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        session.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        session.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _maybe_solve_captcha(
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3,
) -> Optional[dict]:
    if policy == "off" or client is None:
        return None
    result = solve_when_blocked(
        client=client, page_url=url, html=html, count_product_links=fp.count_result_cards,
        extra_markers=fp.BOT_CHALLENGE_MARKERS, min_score=min_score,
    )
    action = result.get("action")
    if action == "no_captcha_detected":
        pass
    elif action == "skipped_products_present":
        log.info("Captcha widget present but results already rendered — not solving.")
    elif action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        log.info("Captcha solved via 2Captcha (%s).", result.get("captcha_type"))
    elif action == "detected_unidentified_widget":
        log.warning("A captcha-like marker was detected but no known widget/sitekey could be extracted.")
    elif action == "unsupported_vendor":
        # Confirmed gap, not a bug (captcha_solver.py's module docstring) —
        # 2Captcha has no task type for this vendor at all. Naming it here
        # replaces the misleading "no known widget/sitekey" line with the
        # real reason, so a caller reads this and reaches for
        # --cdp-block-retries / --cookies-file instead of filing a parser
        # bug report.
        log.warning(
            "%s challenge detected — 2Captcha has no automated solve for this "
            "defense (confirmed gap, not a bug). Reporting run as blocked. "
            "See --cdp-block-retries (retry with a fresh session) and "
            "--cookies-file (reuse a session a human solved manually).",
            result.get("vendor"),
        )
    return result


async def _connect_over_cdp(pw, cdp_endpoint: str):
    """See stockx-scraper's playwright_scraper.py for why this wraps the
    connection error rather than letting it propagate: connect_over_cdp
    repeats a failed endpoint's login:password in its own message and
    "Call log" several times over."""
    try:
        return await pw.chromium.connect_over_cdp(cdp_endpoint)
    except Exception as exc:
        raise RuntimeError(f"CDP connection failed: {redact_credentials(str(exc))}") from None


async def scrape_search(
    *, args: argparse.Namespace, start_url: str, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool, user_agent: Optional[str], cookies: Optional[list] = None,
) -> tuple:
    """Returns (products, blocked, remote_api_error, scroll_rounds_done, scroll_error).

    `scroll_error` is True only when the scroll loop itself broke early on
    an exception (not on reaching --max-results/--stall-rounds/--max-scrolls
    normally) — the caller turns that into EXIT_PARTIAL when products were
    already collected, instead of silently reporting "complete" on a run
    that actually stopped short because something failed mid-pagination."""
    blocked = False
    remote_api_error = False
    scroll_error = False

    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    context = await _new_context(browser, proxy, user_agent, cookies=cookies, stealth=args.stealth)
    page = await context.new_page()
    if autosolve:
        await _enable_scraping_browser_auto_solve(context, page)

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            response = await page.goto(start_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(READINESS_WAIT_MS)
            status = response.status if response is not None else None
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
            last_error = str(exc)
            log.warning("Navigation attempt %d/%d failed: %s", attempt + 1, args.retries + 1, last_error)
            if attempt < args.retries:
                await asyncio.sleep(args.retry_delay)

    if last_error is not None:
        await context.close()
        log.error("Search page permanently failed to load: %s", last_error)
        return [], False, True, 0, False

    if status is not None and status >= 400:
        log.warning("Search page returned HTTP %d — treating as blocked, not empty.", status)
        blocked = True
        if proxy_pool is not None and proxy is not None and status in (403, 429):
            proxy_pool.report_failure(proxy, dead=True)
    elif status is not None and proxy_pool is not None and proxy is not None:
        proxy_pool.report_success(proxy)

    seen_skus: set = set()
    merged: List[Product] = []
    stall = 0
    rounds = 0

    for round_num in range(args.max_scrolls + 1):
        rounds = round_num
        try:
            html = await page.content()
        except Exception as exc:  # noqa: BLE001 — page.content() can race a
            # client-side navigation/reload. Confirmed live, 2026-09-22
            # (Roman's own --cookies-file run over --cdp-endpoint): this
            # exact call crashed the WHOLE process with "Unable to retrieve
            # content because the page is navigating and changing the
            # content" — almost certainly PerimeterX's own challenge page
            # reloading itself, not a bug in what this scraper asked the
            # page to do. One short wait-and-retry, then degrade this round
            # instead of taking the whole run down with it (CLAUDE.md §6 —
            # a bad round must never discard already-collected siblings).
            log.warning("page.content() failed mid-navigation (%s) — waiting briefly and retrying once.", exc)
            await asyncio.sleep(1.0)
            try:
                html = await page.content()
            except Exception as exc2:  # noqa: BLE001
                log.warning("page.content() failed again (%s) — ending this round's collection here, not crashing the run.", exc2)
                scroll_error = True
                break
        if detect_from_html(html, fp.BOT_CHALLENGE_MARKERS):
            blocked = True
        captcha_result = await _maybe_solve_captcha(html=html, url=start_url, client=client, policy=args.solve_captcha, min_score=args.min_score)
        if captcha_result and captcha_result.get("action") in ("warning_no_key", "warning_solver_error", "detected_unidentified_widget", "unsupported_vendor"):
            if fp.count_result_cards(html) == 0:
                blocked = True

        result = fp.safe_parse_search_results(
            html, origin=args.origin, destination=args.destination, depart_date=args.depart_date,
            return_date=args.return_date, adults=args.adults, currency=args.currency,
            cabin_class=args.cabin_class, stops=args.stops, sort=args.sort,
            max_results=args.max_results,
        )
        if result.source_used == "none" and round_num == 0 and not blocked:
            log.warning(
                "No itineraries recognised on the first render — either this search "
                "genuinely has no results, or flight_parser.py's selectors need "
                "updating for the current skyscanner.com markup (see its module "
                "docstring). Re-run with --dump-html to inspect the captured page."
            )

        round_skus = {_sku_key(p) for p in result.products}
        new_skus = round_skus - seen_skus
        if new_skus:
            for p in result.products:
                if _sku_key(p) in new_skus:
                    merged.append(p)
            seen_skus |= new_skus
            stall = 0
        else:
            stall += 1

        if len(merged) >= args.max_results:
            merged = merged[: args.max_results]
            break
        if stall >= args.stall_rounds:
            break
        if round_num >= args.max_scrolls:
            break

        try:
            await page.mouse.wheel(0, 4000)
        except Exception as exc:  # noqa: BLE001 — a scroll failure ends the loop, not the run
            log.warning("Scroll failed, stopping pagination early: %s", exc)
            scroll_error = True
            break
        await asyncio.sleep(args.scroll_delay)

    if args.dump_html:
        try:
            final_html = await page.content()
        except Exception as exc:  # noqa: BLE001 — same navigation race as
            # above; a failed debug dump must never crash an otherwise-
            # complete/blocked run.
            log.warning("--dump-html: page.content() failed (%s) — skipping the dump, not crashing.", exc)
            final_html = None
        if final_html is not None:
            Path(_dump_path(args.out)).write_text(final_html, encoding="utf-8")

    await context.close()
    return merged, blocked, remote_api_error, rounds, scroll_error


def _scrape_via_scraper_api(
    *, args: argparse.Namespace, start_url: str, client: TwoCaptchaClient,
) -> tuple:
    """--scraper-api's own fetch path: one browserless HTTP call to
    2Captcha's Scraper API, no local/CDP browser, no scroll loop (a static
    HTML snapshot can't scroll itself), no cookie injection (no live
    page/DOM to add --cookies-file's cookies to before navigation — see
    --scraper-api's help text). Returns the same 5-tuple shape as
    scrape_search() so run() below can treat both the same way; `rounds`
    is always 0 here since there is exactly one fetch, never a round loop.

    Live-tested 2026-09-22: real HTML comes back for skyscanner.com's
    homepage, but the actual flight-search results URL (PerimeterX-
    protected) comes back as a bare ~700-byte app shell every time — no
    BOT_CHALLENGE_MARKERS text (PerimeterX's "Press & Hold" UI is itself
    rendered by client-side JS that never got a chance to run/decide to
    show it), no results either. That shell has NO recognisable bot-
    challenge marker, so `detect_from_html()` alone cannot tell it apart
    from "genuinely zero itineraries" — see the explicit shell-length
    heuristic below, added specifically because of this live finding."""
    try:
        result = client.scrape_url(start_url, timeout=args.scraper_api_timeout)
    except TwoCaptchaAuthError as exc:
        log.error("Scraper API: %s", exc)
        return [], False, True, 0, False
    except TwoCaptchaError as exc:
        log.error("Scraper API request failed — treating as remote_api_error, not a crash: %s", exc)
        return [], False, True, 0, False

    html = result.body
    blocked = False
    if result.target_status is not None and result.target_status >= 400:
        log.warning("Scraper API: target page returned HTTP %d — treating as blocked.", result.target_status)
        blocked = True
    if detect_from_html(html, fp.BOT_CHALLENGE_MARKERS):
        blocked = True
    # See the docstring above: a real, live 2026-09-22 finding — PerimeterX
    # on skyscanner.com's search URL serves a silent, un-hydrated ~700-byte
    # app shell instead of either real content or a visible challenge.
    # `<div id="root"></div>` with nothing rendered into it and no
    # BOT_CHALLENGE_MARKERS text is NOT "genuinely zero itineraries" — a
    # real empty search still returns the full ~250KB+ hydrated app.
    if not blocked and len(html) < 5000 and "root" in html:
        log.warning(
            "Scraper API returned a %d-byte un-hydrated app shell (no results, no visible "
            "challenge marker either) — treating as a silent block, not zero itineraries. See "
            "--scraper-api's help text.",
            len(html),
        )
        blocked = True

    if args.dump_html:
        Path(_dump_path(args.out)).write_text(html, encoding="utf-8")

    parsed = fp.safe_parse_search_results(
        html, origin=args.origin, destination=args.destination, depart_date=args.depart_date,
        return_date=args.return_date, adults=args.adults, currency=args.currency,
        cabin_class=args.cabin_class, stops=args.stops, sort=args.sort,
        max_results=args.max_results,
    )
    if parsed.source_used == "none" and not blocked:
        log.warning(
            "No itineraries recognised in the Scraper API response — either this search genuinely "
            "has no results, or the fetch landed on a page/locale this repo's parser doesn't "
            "recognise (a clean skyscanner.com homepage fetch landed on a non-US locale in live "
            "testing — the same caveat shein-scraper's own --scraper-api documents). Re-run with "
            "--dump-html to inspect what actually came back."
        )
    return parsed.products, blocked, False, 0, False


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    if async_playwright is None:
        print(f"Error: playwright is not installed ({_PLAYWRIGHT_IMPORT_ERROR}). "
              f"pip install -r requirements-playwright.txt && playwright install chromium", file=sys.stderr)
        return EXIT_CRASH
    start_url = _resolve_start_url(args)
    if not start_url:
        print("Error: provide --url, or --origin/--destination/--depart-date", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print(f"Error: unsupported --format {args.format!r}", file=sys.stderr)
        return EXIT_BAD_USAGE
    args.out = args.out or _default_out(args.format)

    if args.scraper_api:
        # A whole separate, browserless code path — no Playwright import is
        # needed at all here (see CLAUDE.md §6), so it's skipped even though
        # this run() already required it above (mirrors shein-scraper's own
        # --scraper-api, which checks async_playwright further down instead
        # — here the check already happened before this branch exists, so
        # this comment just documents that this mode never touches it).
        if not args.twocaptcha_key:
            print("Error: --scraper-api requires --twocaptcha-key/TWOCAPTCHA_KEY", file=sys.stderr)
            return EXIT_BAD_USAGE
        if args.proxy or args.proxy_file or args.cdp_endpoint or args.fingerprint or args.cookies_file:
            log.warning(
                "--scraper-api ignores --proxy/--proxy-file/--cdp-endpoint/--fingerprint/"
                "--cookies-file — this mode brings its own exit IP/device via 2Captcha's own "
                "infrastructure and has no live page/DOM to inject cookies into, see "
                "--scraper-api's help text."
            )
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api, scraper_api_base=args.scraper_api_url)
        blocked = remote_api_error = False
        merged: List[Product] = []
        # Reuses --cdp-block-retries as the retry knob for this mode too —
        # this repo has no separate plain --block-retries (only the CDP-
        # gated one), and "reconnect/retry this many times before giving
        # up on a blocked outcome" is exactly the same idea here.
        for block_attempt in range(args.cdp_block_retries + 1):
            merged, blocked, remote_api_error, rounds, scroll_error = _scrape_via_scraper_api(
                args=args, start_url=start_url, client=client,
            )
            if remote_api_error or not (blocked and not merged):
                break
            if block_attempt < args.cdp_block_retries:
                log.warning(
                    "Blocked with zero itineraries (Scraper API attempt %d/%d) — retrying the "
                    "same fetch before giving up.",
                    block_attempt + 1, args.cdp_block_retries + 1,
                )
                await asyncio.sleep(args.retry_delay)
        price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
        return finish_run(
            products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
            pages_requested=1, pages_completed=0 if remote_api_error else 1, failed_pages=None,
            blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
            started_at=started_at, price_confirmed_pct=price_confirmed_pct,
        )

    try:
        cookies = _load_cookies_file(args.cookies_file)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE

    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        # A malformed --proxy / --proxy-file line is a USAGE mistake, not a
        # crash — this used to reach the generic `except Exception` below
        # unguarded (load_proxies() runs before that try/except even
        # starts), so it surfaced as a raw, uncaught traceback that
        # happened to exit 1 by Python's own default rather than through
        # this scraper's EXIT_CRASH path, and reported the wrong code
        # (1, "crashed") for what is actually EXIT_BAD_USAGE (2) — the
        # same category as a bad --format. Confirmed the identical,
        # unguarded bug in stockx-scraper's playwright_scraper.py (fixed
        # here only, per the standing instruction not to touch that repo).
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None

    client = None
    if args.twocaptcha_key and (args.solve_captcha != "off" or args.fingerprint):
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)

    user_agent = None
    cdp_refused_fingerprint = refuse_if_cdp(args.cdp_endpoint)
    if args.fingerprint and not cdp_refused_fingerprint:
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    merged: List[Product] = []
    blocked = False
    remote_api_error = False
    rounds = 0
    scroll_error = False
    try:
        async with async_playwright() as pw:
            if args.cdp_endpoint:
                if args.proxy or args.proxy_file:
                    log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
                    proxy_pool = None
                autosolve = args.solve_captcha != "off"
                # A blocked result over --cdp-endpoint gets --cdp-block-
                # retries extra attempts, each over a FRESH Scraping Browser
                # session (a new connect_over_cdp call, not the same
                # browser reused) — reconnecting is what actually changes
                # the exit identity the pool hands back next. This is the
                # mitigation this repo takes for a defense like PerimeterX
                # that 2Captcha cannot solve at all (captcha_solver.py):
                # reputation/behavior-based challenges sometimes simply
                # don't fire on a fresh-enough session. It is NOT a retry
                # against transient network errors — a genuine CDP
                # connection failure still reports remote_api_error (below)
                # without burning through these attempts.
                attempts = args.cdp_block_retries + 1
                for attempt in range(attempts):
                    try:
                        browser = await _connect_over_cdp(pw, args.cdp_endpoint)
                    except RuntimeError as exc:
                        # A broken/misconfigured remote CDP session (2Captcha
                        # Scraping Browser API down, wrong endpoint, expired
                        # session, ...) is a remote-API failure, not a bug in
                        # this scraper — letting it fall through to the generic
                        # `except Exception` below would report it as
                        # EXIT_CRASH, which tells a caller (CI/cron) the wrong
                        # thing to act on (page an engineer instead of
                        # retrying/alerting on the remote dependency).
                        log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
                        remote_api_error = True
                        break

                    merged, blocked, remote_api_error, rounds, scroll_error = await scrape_search(
                        args=args, start_url=start_url, browser=browser, proxy_pool=proxy_pool,
                        client=client, autosolve=autosolve, user_agent=user_agent, cookies=cookies,
                    )
                    await browser.close()

                    if remote_api_error or not blocked:
                        break
                    if attempt < attempts - 1:
                        log.warning(
                            "Blocked on attempt %d/%d over --cdp-endpoint — reconnecting for a "
                            "fresh session and retrying (--cdp-block-retries).",
                            attempt + 1, attempts,
                        )
                        await asyncio.sleep(args.retry_delay)
            else:
                browser = await pw.chromium.launch(headless=args.headless)
                autosolve = False
                merged, blocked, remote_api_error, rounds, scroll_error = await scrape_search(
                    args=args, start_url=start_url, browser=browser, proxy_pool=proxy_pool,
                    client=client, autosolve=autosolve, user_agent=user_agent, cookies=cookies,
                )
                await browser.close()
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH

    price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None

    # `rounds` is the scroll loop's 0-indexed round_num at break time, but by
    # that point round_num's own parse already succeeded (the parse always
    # runs before any break check) — so the actual count of successfully
    # processed rounds is `rounds + 1`, not `rounds`. Reporting the raw
    # 0-indexed value here used to make finish_run's `pages_completed > 0`
    # partial-gate silently fail whenever the scroll loop broke on its very
    # first round (rounds == 0) despite that round's data being genuinely
    # collected — a truncated run reported as "complete" instead of
    # "partial". A scroll-loop exception that broke pagination early is a
    # real failure mode, not "we naturally stopped"
    # (max-results/stall-rounds/max-scrolls) — surface it as a failed
    # "page" (the round after the last one we actually completed) so
    # finish_run's partial precedence can catch it instead of silently
    # reporting a truncated run as complete.
    completed_rounds = rounds + 1
    failed_pages = [completed_rounds + 1] if scroll_error else None

    return finish_run(
        products=merged,
        out_path=args.out,
        fmt=args.format,
        engine=ENGINE_NAME,
        url=start_url,
        pages_requested=args.max_scrolls,
        pages_completed=completed_rounds,
        failed_pages=failed_pages,
        blocked=blocked,
        remote_api_error=remote_api_error,
        allow_empty=args.allow_empty,
        started_at=started_at,
        price_confirmed_pct=price_confirmed_pct,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args()
    args = env_config.apply_env(args)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())

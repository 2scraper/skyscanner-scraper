#!/usr/bin/env python3
"""puppeteer_scraper.py — pyppeteer engine, parity copy of
playwright_scraper.py (same flags, same exit codes and status semantics —
see output_writer.finish_run). Not the primary engine (Playwright is); kept
for parity, and because it — like Playwright, unlike Selenium — CAN open an
authenticated `ws://login:pass@host:port` CDP session, so it is the second
engine able to use the Scraping Browser API's own `Captcha.setAutoSolve`.

Named and shaped like stockx-scraper's own `puppeteer_scraper.py` (Python +
pyppeteer, not a separate Node.js file) — this repo follows that family
convention rather than the original draft's Node.js `skyscanner_scraper_
puppeteer.js`, since the point of this rebuild is to match the family's
actual structure, one shared output contract and one set of family modules
across all three engines.

pyppeteer itself is effectively unmaintained (its own README points at
Playwright) — this file exists for parity/completeness, not as a
recommendation to prefer it.

Chromium binary: sourced from `PYPPETEER_EXECUTABLE_PATH` /
`PUPPETEER_EXECUTABLE_PATH` if set (handy for reusing an existing
Playwright/system Chromium instead of pyppeteer's own bundled download),
otherwise pyppeteer's own default.

**PerimeterX gap and its mitigations** (see captcha_solver.py's module
docstring for the full incident, confirmed live against a real
skyscanner.com challenge): 2Captcha has no automated solve for
PerimeterX/DataDome/a bare Cloudflare managed challenge at all.
`--cdp-block-retries` reconnects over `--cdp-endpoint` for a fresh
Scraping Browser session and retries on a blocked result — this engine
(like Playwright, unlike Selenium) opens a genuine new remote session per
`pyppeteer_connect` call, so a retry here really can land a different
exit identity. `--cookies-file` loads a session a human solved manually,
via `page.setCookie()` before navigation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from pyppeteer import connect as pyppeteer_connect
    from pyppeteer import launch as pyppeteer_launch
    from pyppeteer.errors import NetworkError, PageError, TimeoutError as PyppeteerTimeoutError
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    pyppeteer_launch = None
    pyppeteer_connect = None
    NetworkError = PageError = PyppeteerTimeoutError = Exception
    _PYPPETEER_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PYPPETEER_IMPORT_ERROR = None

import env_config
import flight_parser as fp
from captcha_solver import detect_from_html, solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run, sku_key as _sku_key
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

ENGINE_NAME = "puppeteer"
NAV_TIMEOUT_MS = 30_000
READINESS_WAIT_S = 3.0

log = logging.getLogger("puppeteer_scraper")

_CHROMIUM_EXECUTABLE = os.environ.get("PYPPETEER_EXECUTABLE_PATH") or os.environ.get("PUPPETEER_EXECUTABLE_PATH")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="skyscanner.com flight search scraper — pyppeteer (Puppeteer) engine",
        epilog="Credentials belong in .env / SKYSCANNER_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None)
    p.add_argument("--origin", default=None)
    p.add_argument("--destination", default=None)
    p.add_argument("--depart-date", default=None)
    p.add_argument("--return-date", default=None)
    p.add_argument("--adults", type=_positive_int, default=1)
    p.add_argument("--currency", default="USD")
    p.add_argument("--cabin-class", choices=fp.CABIN_CLASS_VALUES, default="economy")
    p.add_argument("--stops", choices=fp.STOPS_VALUES, default="any")
    p.add_argument("--sort", choices=fp.SORT_VALUES, default="best")
    p.add_argument("--max-results", type=_positive_int, default=30)
    p.add_argument("--max-scrolls", type=_positive_int, default=20)
    p.add_argument("--stall-rounds", type=_positive_int, default=4)
    p.add_argument("--scroll-delay", type=float, default=1.5)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-delay", type=float, default=3.0)
    p.add_argument("--proxy", default=None)
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None)
    p.add_argument("--captcha-api", default=None, help="Override the 2Captcha API base URL (testing only)")
    p.add_argument("--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile's user agent (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows'")
    p.add_argument(
        "--stealth", action="store_true",
        help="Patch common automation tells (navigator.webdriver, window.chrome, WebGL vendor/"
             "renderer, plugin list) before any page loads. Experimental, added 2026-09-22 after "
             "a real --cookies-file test still got challenged by PerimeterX from the exact IP that "
             "solved it — see CHANGELOG.md for what this has and hasn't been confirmed to fix. Not "
             "a captcha solver or bypass: it changes what THIS browser reveals about itself, "
             "nothing about the challenge.",
    )
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument(
        "--scraper-api", action="store_true",
        help="Fetch via 2Captcha's Scraper API (scraper.2captcha.com) instead of launching any local "
             "or --cdp-endpoint browser — a single browserless HTTP call, run entirely on 2Captcha's "
             "own infrastructure. Requires --twocaptcha-key/TWOCAPTCHA_KEY. A GENUINELY DIFFERENT "
             "product from --cdp-endpoint's Scraping Browser API — see scraper_api_client.py's module "
             "docstring. Live-tested against real skyscanner.com, 2026-09-22: no PerimeterX challenge "
             "marker was ever seen, but the actual flight-search results never came back hydrated "
             "either — sometimes a bare ~700-byte app shell, sometimes a real, full SEO-prerendered "
             "page (in whatever locale that request happened to land on) with zero client-rendered "
             "result cards. NOT a confirmed bypass — see CHANGELOG.md for the full write-up. "
             "--max-scrolls/--stall-rounds/--scroll-delay/--proxy/--cdp-endpoint/--fingerprint/"
             "--cookies-file are all IGNORED in this mode (a single static fetch has no scroll loop, "
             "no live page/DOM to inject cookies into, and brings its own exit IP/device) — set "
             "together, they log a warning rather than silently doing nothing.",
    )
    p.add_argument("--scraper-api-timeout", type=int, default=60, help="Seconds 2Captcha itself waits for the target page to finish loading (1-120, their limit)")
    p.add_argument("--scraper-api-url", default=None, help="Override the Scraper API base URL (testing only)")
    p.add_argument("--cdp-endpoint", default=None)
    p.add_argument("--cdp-block-retries", type=int, default=2, help="On a --cdp-endpoint run that comes back blocked (e.g. a PerimeterX challenge 2Captcha cannot solve — see captcha_solver.py), reconnect for a fresh Scraping Browser session and retry this many times before giving up. Has no effect without --cdp-endpoint.")
    p.add_argument("--cookies-file", default=None, help="Path to a JSON array of cookies from a session a HUMAN solved manually — loaded before navigating. A way to reuse a person's own solve, never to solve a challenge automatically.")
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--dump-html", action="store_true")
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


def _load_cookies_file(path: Optional[str]) -> Optional[list]:
    """Same JSON-array-of-cookie-dicts contract as playwright_scraper.py's
    identical helper (see that module for the full rationale) — kept as its
    own copy here, not imported, so each engine script stays independently
    importable with no other engine's driver installed (CLAUDE.md §6)."""
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


async def _apply_cookies(page, cookies: Optional[list]) -> None:
    """pyppeteer's page.setCookie() can set cookies before any navigation
    happens (unlike Selenium's add_cookie, which needs a matching-domain
    page already loaded) — its accepted keys are the same CDP
    Network.setCookie shape Playwright's own cookie objects use, so no
    format translation is needed, just a per-cookie filter to the keys
    pyppeteer recognizes. A single bad cookie logs and is skipped rather
    than aborting the whole set or the run (CLAUDE.md §6)."""
    if not cookies:
        return
    for cookie in cookies:
        pp_cookie = {k: v for k, v in cookie.items() if k in
                     ("name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite")}
        if not pp_cookie.get("name") or "value" not in pp_cookie:
            log.warning("Skipping malformed cookie in --cookies-file (missing name/value): %r", cookie)
            continue
        try:
            await page.setCookie(pp_cookie)
        except Exception as exc:  # noqa: BLE001 — one bad cookie must not abort the run
            log.warning("Skipping cookie %r from --cookies-file: %s", pp_cookie.get("name"), exc)


async def _launch(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str]):
    if cdp_endpoint:
        try:
            return await pyppeteer_connect(browserWSEndpoint=cdp_endpoint, defaultViewport=None)
        except Exception as exc:
            raise RuntimeError(f"CDP connection failed: {redact_credentials(str(exc))}") from None
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
    if proxy is not None:
        args.append(proxy.pyppeteer_launch_arg())
    kwargs = dict(headless=headless, args=args)
    if _CHROMIUM_EXECUTABLE:
        kwargs["executablePath"] = _CHROMIUM_EXECUTABLE
    return await pyppeteer_launch(**kwargs)


async def _authenticate_if_needed(page, proxy: Optional[Proxy]) -> None:
    if proxy is not None:
        auth = proxy.pyppeteer_auth_dict()
        if auth:
            await page.authenticate(auth)


async def _enable_scraping_browser_auto_solve(page) -> None:
    try:
        client = await page.target.createCDPSession()
        client.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        client.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        client.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await client.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _maybe_solve_captcha(*, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3) -> Optional[dict]:
    if policy == "off" or client is None:
        return None
    result = solve_when_blocked(
        client=client, page_url=url, html=html, count_product_links=fp.count_result_cards,
        extra_markers=fp.BOT_CHALLENGE_MARKERS, min_score=min_score,
    )
    action = result.get("action")
    if action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        log.info("Captcha solved via 2Captcha (%s).", result.get("captcha_type"))
    elif action == "detected_unidentified_widget":
        log.warning("A captcha-like marker was detected but no known widget/sitekey could be extracted.")
    elif action == "unsupported_vendor":
        # Confirmed gap, not a bug (captcha_solver.py's module docstring) —
        # 2Captcha has no task type for this vendor at all.
        log.warning(
            "%s challenge detected — 2Captcha has no automated solve for this "
            "defense (confirmed gap, not a bug). Reporting run as blocked. "
            "See --cdp-block-retries (retry with a fresh session) and "
            "--cookies-file (reuse a session a human solved manually).",
            result.get("vendor"),
        )
    return result


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


async def scrape_search(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient], autosolve: bool = False,
    user_agent: Optional[str] = None, cookies: Optional[list] = None,
) -> Tuple[List[Product], bool, bool, int, bool]:
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
    try:
        browser = await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint)
    except RuntimeError as exc:
        # A broken/misconfigured remote CDP session (2Captcha Scraping
        # Browser API down, wrong endpoint, expired session, ...) is a
        # remote-API failure, not a bug in this scraper — letting it
        # propagate to run()'s generic `except Exception` would report it
        # as EXIT_CRASH, which tells a caller (CI/cron) the wrong thing to
        # act on (page an engineer instead of retrying/alerting on the
        # remote dependency). Match the same shape the permanent-navigation
        # -failure path below already returns.
        log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
        return [], False, True, 0, False
    page = await browser.newPage()
    if args.stealth:
        # pyppeteer's equivalent of Playwright's context.add_init_script()
        # / Selenium's Page.addScriptToEvaluateOnNewDocument CDP command —
        # runs on every document in this page before that document's own
        # scripts execute. See playwright_scraper.py's own copy of
        # _STEALTH_INIT_SCRIPT and --stealth's help text for why this
        # exists.
        try:
            await page.evaluateOnNewDocument(_STEALTH_INIT_SCRIPT)
        except Exception as exc:  # noqa: BLE001 — optional hardening, never fatal
            log.warning("--stealth: evaluateOnNewDocument failed (%s) — continuing unpatched.", exc)
    if user_agent:
        # The only piece of a 2Captcha Fingerprint API profile this repo
        # applies anywhere — see fingerprint_client.py's module docstring
        # on why locale/timezone are deliberately NOT fabricated from it.
        # pyppeteer's `page.setUserAgent()` is a real, documented API, so
        # (unlike the CDP-auth/proxy-auth limitations below) there is no
        # driver-level reason to leave this one unwired — that would be a
        # silent gap, not a named exception (CLAUDE.md §4/§6).
        await page.setUserAgent(user_agent)
    await _apply_cookies(page, cookies)
    await _authenticate_if_needed(page, proxy)
    if autosolve:
        await _enable_scraping_browser_auto_solve(page)

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            response = await page.goto(start_url, {"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
            await asyncio.sleep(READINESS_WAIT_S)
            status = response.status if response is not None else None
            if proxy_pool is not None and proxy is not None:
                proxy_pool.report_success(proxy)
            last_error = None
            break
        except (NetworkError, PageError, PyppeteerTimeoutError, Exception) as exc:  # noqa: BLE001
            message = str(exc)
            last_error = message
            dead = is_proxy_dead_error(message)
            if proxy_pool is not None and proxy is not None and dead:
                proxy_pool.report_failure(proxy, dead=True)
                log.warning("Proxy reported dead: %s", message)
            else:
                log.warning("Navigation attempt %d/%d failed: %s", attempt + 1, args.retries + 1, message)
            if attempt < args.retries:
                await asyncio.sleep(args.retry_delay)

    if last_error is not None:
        await browser.close()
        log.error("Search page permanently failed to load: %s", last_error)
        return [], False, True, 0, False

    if status is not None and status >= 400:
        log.warning("Search page returned HTTP %d — treating as blocked, not empty.", status)
        blocked = True

    seen_skus: set = set()
    merged: List[Product] = []
    stall = 0
    rounds = 0

    for round_num in range(args.max_scrolls + 1):
        rounds = round_num
        html = await page.content()
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
            await page.evaluate("window.scrollBy(0, 4000);")
        except Exception as exc:  # noqa: BLE001 — a scroll failure ends the loop, not the run
            log.warning("Scroll failed, stopping pagination early: %s", exc)
            scroll_error = True
            break
        await asyncio.sleep(args.scroll_delay)

    if args.dump_html:
        Path(_dump_path(args.out)).write_text(await page.content(), encoding="utf-8")

    await browser.close()
    return merged, blocked, remote_api_error, rounds, scroll_error


def _scrape_via_scraper_api(
    *, args: argparse.Namespace, start_url: str, client: TwoCaptchaClient,
) -> Tuple[List[Product], bool, bool, int, bool]:
    """--scraper-api's own fetch path — see playwright_scraper.py's copy of
    this function for the full rationale and the live 2026-09-22 findings
    (identical logic, duplicated per engine per this family's own
    convention — CLAUDE.md §4). One browserless HTTP call (plain
    `requests`, not pyppeteer — nothing here needs `await`)."""
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
            "recognise, or (the more commonly observed shape in live testing) a real page came "
            "back but skyscanner.com's client-rendered result cards never hydrated in a single "
            "static fetch. Re-run with --dump-html to inspect what actually came back."
        )
    return parsed.products, blocked, False, 0, False


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    start_url = _resolve_start_url(args)
    if not start_url:
        print("Error: provide --url, or --origin/--destination/--depart-date", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print(f"Error: unsupported --format {args.format!r}", file=sys.stderr)
        return EXIT_BAD_USAGE
    args.out = args.out or _default_out(args.format)

    if args.scraper_api:
        # A whole separate, browserless code path — no pyppeteer is needed
        # at all here (see CLAUDE.md §6), so the pyppeteer_launch-is-None
        # check below is skipped entirely.
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

    if pyppeteer_launch is None:
        print(f"Error: pyppeteer is not installed ({_PYPPETEER_IMPORT_ERROR}). "
              f"pip install -r requirements-puppeteer.txt", file=sys.stderr)
        return EXIT_CRASH

    try:
        cookies = _load_cookies_file(args.cookies_file)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE

    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        # A malformed --proxy/--proxy-file line is a usage mistake, not a
        # crash — see playwright_scraper.py's identical fix for the full
        # incident writeup (same unguarded bug, confirmed also present in
        # stockx-scraper and left unfixed there per standing instruction).
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None
    if args.cdp_endpoint and proxy_pool is not None:
        log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
        proxy_pool = None

    client = None
    if args.twocaptcha_key and (args.solve_captcha != "off" or args.fingerprint):
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)
    autosolve = bool(args.cdp_endpoint) and args.solve_captcha != "off"

    user_agent = None
    cdp_refused_fingerprint = refuse_if_cdp(args.cdp_endpoint)
    if args.fingerprint and not cdp_refused_fingerprint:
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    try:
        if args.cdp_endpoint:
            # A blocked result over --cdp-endpoint gets --cdp-block-retries
            # extra attempts, each over a FRESH Scraping Browser session
            # (a new pyppeteer_connect call inside scrape_search's own
            # _launch, not the same browser reused) — see
            # playwright_scraper.py's identical loop for the full
            # rationale: reconnecting is what actually changes the exit
            # identity the pool hands back, and this is the mitigation this
            # repo takes for a defense (PerimeterX) 2Captcha cannot solve
            # at all. A genuine CDP connection failure still reports
            # remote_api_error without burning through these attempts,
            # since scrape_search's own _launch already turns that into
            # (blocked=False, remote_api_error=True) before this loop ever
            # sees it.
            attempts = args.cdp_block_retries + 1
            for attempt in range(attempts):
                merged, blocked, remote_api_error, rounds, scroll_error = await scrape_search(
                    args=args, start_url=start_url, proxy_pool=proxy_pool, client=client, autosolve=autosolve,
                    user_agent=user_agent, cookies=cookies,
                )
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
            merged, blocked, remote_api_error, rounds, scroll_error = await scrape_search(
                args=args, start_url=start_url, proxy_pool=proxy_pool, client=client, autosolve=autosolve,
                user_agent=user_agent, cookies=cookies,
            )
        price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH

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
        products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
        pages_requested=args.max_scrolls, pages_completed=completed_rounds, failed_pages=failed_pages,
        blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
        started_at=started_at, price_confirmed_pct=price_confirmed_pct,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_arg_parser().parse_args()
    args = env_config.apply_env(args)
    try:
        return asyncio.get_event_loop().run_until_complete(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())

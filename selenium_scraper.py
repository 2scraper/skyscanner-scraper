#!/usr/bin/env python3
"""selenium_scraper.py — Selenium engine, parity copy of
playwright_scraper.py (same flags, same exit codes, same status semantics
— see output_writer.finish_run). Selenium is not the primary engine
(Playwright is); it exists for parity, not because it is preferred.

Same hard engine limits as stockx-scraper's selenium_scraper.py (see also
the family CLAUDE.md §6, referenced there):

  - Selenium CANNOT use an AUTHENTICATED remote CDP endpoint.
    chromedriver's `debuggerAddress` takes a bare `host:port`; Playwright's
    `connect_over_cdp` and pyppeteer's `connect` take a full
    `ws://user:pass@host:port` and authenticate on the WebSocket upgrade.
    A `--cdp-endpoint` carrying credentials (the Scraping Browser API
    shape) is refused outright here with EXIT_BAD_USAGE — no half-working
    attempt — with a pointer to playwright_scraper.py / puppeteer_scraper.py.
  - Selenium's `--proxy-server` CANNOT authenticate at all. A `--proxy`
    with a login/password has its credentials stripped before being handed
    to Chrome, and this engine WARNS rather than silently dropping them.

**PerimeterX gap and its mitigations** (see captcha_solver.py's module
docstring): this repo does not implement a solve for PerimeterX (what
skyscanner.com serves) or DataDome / a Cloudflare managed challenge. `--cdp-block-retries` and
`--cookies-file` exist here for CLI/flag parity with playwright_scraper.py
and puppeteer_scraper.py (CLAUDE.md §4), but `--cdp-block-retries` is
weaker on this engine specifically: Selenium's `debugger_address` attaches
to an ALREADY-RUNNING remote browser rather than opening a fresh session
the way Playwright's `connect_over_cdp` / pyppeteer's `connect` do, so
retrying here reconnects to the SAME browser process — no fresh exit
identity, just another attempt at the scrape. A CREDENTIALED `--cdp-
endpoint` (the real Scraping Browser API shape, where the identity-
refresh mitigation would matter most) never reaches that retry loop at
all — it's refused outright as `EXIT_BAD_USAGE` before this module does
anything, per the limitation above. `--cookies-file` (a session a human
solved manually, reused here) works normally on this engine.

Chrome binary: normally auto-detected by Selenium/Selenium Manager from a
regular Chrome/Chromium install. Set `CHROME_BIN` or `SELENIUM_CHROME_BIN`
to point at a specific binary instead.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    webdriver = None
    WebDriverException = Exception
    Options = None
    Service = None
    _SELENIUM_IMPORT_ERROR = _IMPORT_ERROR
else:
    _SELENIUM_IMPORT_ERROR = None

import env_config
import flight_parser as fp
from captcha_solver import detect_from_html, solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run, sku_key as _sku_key
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

ENGINE_NAME = "selenium"
NAV_TIMEOUT_S = 30
READINESS_WAIT_S = 3.0
_CHROME_BINARY = os.environ.get("SELENIUM_CHROME_BIN") or os.environ.get("CHROME_BIN")
# Selenium 4's Selenium Manager resolves/downloads a matching chromedriver
# over the network by default (googlechromelabs.github.io) whenever
# Service() is called with no explicit path — which fails outright in any
# network-restricted environment (a CI runner with no outbound internet,
# an air-gapped box) even when a compatible chromedriver is already
# installed and on PATH. Mirrors playwright's PLAYWRIGHT_BROWSERS_PATH and
# puppeteer's PYPPETEER_EXECUTABLE_PATH/PUPPETEER_EXECUTABLE_PATH escape
# hatches so this engine isn't the one that can never run offline.
_CHROMEDRIVER_PATH = os.environ.get("SELENIUM_CHROMEDRIVER_PATH") or os.environ.get("CHROMEDRIVER_PATH")

# See stockx-scraper's selenium_scraper.py for the full incident writeup:
# Selenium Manager phones home to plausible.io with usage stats by
# default (confirmed live on that repo, 2026-09-14) — opted out the same
# way here, before this module's own SECURITY.md promise is tested.
os.environ.setdefault("SE_AVOID_STATS", "true")

log = logging.getLogger("selenium_scraper")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="skyscanner.com flight search scraper — Selenium engine",
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
    p.add_argument("--solve-captcha", choices=["off", "when-blocked"], default="when-blocked")
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
    p.add_argument("--cdp-endpoint", default=None,
                    help="NOTE: refused if it carries credentials — Selenium cannot authenticate a remote CDP session")
    p.add_argument("--cdp-block-retries", type=int, default=2, help="Retry a blocked run this many times over --cdp-endpoint (parity flag — see this module's docstring: weaker here than on playwright/puppeteer, since Selenium reconnects to the SAME browser session, not a fresh one). Has no effect without --cdp-endpoint.")
    p.add_argument("--cookies-file", default=None, help="Path to a JSON array of cookies from a session a HUMAN solved manually — applied before the real navigation. A way to reuse a person's own solve, never to solve a challenge automatically.")
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--wait-for-human", type=int, default=0, metavar="SECONDS",
                   help="When the page shows a bot challenge (PerimeterX 'Press & Hold'), wait up to SECONDS "
                        "for a PERSON to complete it in the browser window, then carry on in that same session. "
                        "Nothing here solves the challenge; it only waits. Forces --headful unless "
                        "--cdp-endpoint points at your own local Chrome. 0 (default) = off.")
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


def _cdp_endpoint_has_credentials(cdp_endpoint: str) -> bool:
    parts = urlparse(cdp_endpoint)
    return bool(parts.username or parts.password)


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


def _apply_cookies(driver, cookies: Optional[list], start_url: str) -> None:
    """Selenium's add_cookie() requires the browser to already be on a page
    whose domain matches the cookie's own domain — unlike Playwright's
    context.add_cookies()/pyppeteer's page.setCookie(), which can set a
    cookie before any navigation happens at all. So this does one cheap
    real navigation to the target origin first (not about:blank, which
    Chrome refuses to attach any cookie to), adds each cookie, and leaves
    the caller's own real navigation in scrape_search() to follow as
    normal — that second request is the one that actually carries the
    cookies. A single bad cookie (wrong domain, malformed, expired) is a
    warning and skipped, never a crash (CLAUDE.md §6: one bad unit of work
    degrades, it doesn't take the whole run down)."""
    if not cookies:
        return
    parts = urlparse(start_url)
    origin = f"{parts.scheme}://{parts.netloc}/"
    try:
        driver.get(origin)
    except WebDriverException as exc:
        log.warning("Could not navigate to %s to apply --cookies-file cookies: %s", origin, exc)
        return
    for cookie in cookies:
        sel_cookie = {k: v for k, v in cookie.items() if k in
                      ("name", "value", "path", "domain", "secure", "httpOnly", "expiry", "sameSite")}
        if "expiry" not in sel_cookie and isinstance(cookie.get("expires"), (int, float)) and cookie["expires"] > 0:
            # Playwright/CDP cookie shape uses `expires` (float epoch
            # seconds, -1 for a session cookie); Selenium's add_cookie
            # wants an int `expiry` and rejects -1 outright — a session
            # cookie is expressed by simply omitting the field instead.
            sel_cookie["expiry"] = int(cookie["expires"])
        if not sel_cookie.get("name") or "value" not in sel_cookie:
            log.warning("Skipping malformed cookie in --cookies-file (missing name/value): %r", cookie)
            continue
        try:
            driver.add_cookie(sel_cookie)
        except WebDriverException as exc:
            log.warning("Skipping cookie %r from --cookies-file: %s", sel_cookie.get("name"), exc)


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


def _build_driver(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str], user_agent: Optional[str] = None, stealth: bool = False):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    if _CHROME_BINARY:
        options.binary_location = _CHROME_BINARY
    # Selenium has no response hook; Chrome's performance log is how
    # _drain_search_api_responses() sees the search-API XHR.
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    if user_agent:
        # The only piece of a 2Captcha Fingerprint API profile this repo
        # applies anywhere (see fingerprint_client.py's module docstring on
        # why locale/timezone are deliberately NOT fabricated from it).
        # Chrome's own `--user-agent=` switch is a real, documented
        # ChromeOptions argument — unlike the CDP-auth and proxy-auth
        # limitations elsewhere in this file, there is no driver-level
        # reason Selenium couldn't apply this, so (unlike those two)
        # skipping it here would be a silent gap, not a named exception.
        options.add_argument(f"--user-agent={user_agent}")

    if cdp_endpoint:
        options.debugger_address = urlparse(cdp_endpoint).netloc.split("@")[-1]
        driver = webdriver.Chrome(options=options)
    else:
        if proxy is not None:
            if proxy.has_auth:
                log.warning(
                    "Selenium's --proxy-server cannot authenticate — using %s:%s "
                    "with credentials STRIPPED, not silently dropped.",
                    proxy.host, proxy.port,
                )
            options.add_argument(f"--proxy-server={proxy.server_only()}")

        if Service and _CHROMEDRIVER_PATH:
            service = Service(executable_path=_CHROMEDRIVER_PATH)
        elif Service:
            service = Service()  # Selenium Manager: resolves/downloads over the network
        else:
            service = None
        driver = webdriver.Chrome(service=service, options=options) if service else webdriver.Chrome(options=options)

    if stealth:
        # See playwright_scraper.py's own copy of --stealth's help text and
        # _STEALTH_INIT_SCRIPT for why this exists. Selenium's equivalent
        # of Playwright's context.add_init_script(): a CDP command that
        # runs the given source on every new document, before that
        # document's own scripts run.
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _STEALTH_INIT_SCRIPT})
        except Exception as exc:  # noqa: BLE001 — optional hardening, never fatal
            log.warning("--stealth: Page.addScriptToEvaluateOnNewDocument failed (%s) — continuing unpatched.", exc)
    return driver


# See stockx-scraper's selenium_scraper.py for why this reads the
# Navigation Timing API instead of a driver-native Response object.
_STATUS_JS = (
    "try { return performance.getEntriesByType('navigation')[0].responseStatus || 0; } "
    "catch (e) { return 0; }"
)


_WARNED_VENDORS: set = set()


def _maybe_solve_captcha(*, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3, rows_seen: int = 0) -> Optional[dict]:
    if policy == "off" or client is None:
        return None
    result = solve_when_blocked(
        client=client, page_url=url, html=html,
        # Itineraries already collected (mostly from the search-API XHR, which
        # the DOM card count cannot see) mean the page is not gated.
        count_product_links=lambda h: fp.count_result_cards(h) + rows_seen,
        allow_paid_solve=False,
        extra_markers=fp.BOT_CHALLENGE_MARKERS, min_score=min_score,
    )
    action = result.get("action")
    if action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        log.warning(
            "Captcha token obtained but NOT auto-injected on the Selenium "
            "engine (unverified widget-specific step) — use "
            "playwright_scraper.py or puppeteer_scraper.py with "
            "--cdp-endpoint for the Scraping Browser API's built-in solve."
        )
    elif action == "detected_unidentified_widget":
        log.warning("A captcha-like marker was detected but no known widget/sitekey could be extracted.")
    elif action == "unsupported_vendor":
        # Name the vendor instead of the vague "no known widget/sitekey"
        # line. The claim is about THIS page and THIS repo (CLAUDE.md §19),
        # not about what 2Captcha can do. Logged once per process: this
        # runs every scroll round.
        vendor = result.get("vendor")
        if vendor not in _WARNED_VENDORS:
            _WARNED_VENDORS.add(vendor)
            log.warning(
                "%s challenge on the page and no widget this repo can solve (reCAPTCHA/Turnstile/"
                "hCaptcha) was found; this repo does not implement a %s solve. Reported as blocked "
                "only if no itineraries were collected. What has worked live: --wait-for-human over "
                "your own local Chrome (README).",
                vendor, vendor,
            )
    return result


def _drain_search_api_responses(driver, capture: "fp.SearchApiCapture", pending: set) -> None:
    """Feed every finished search-API response since the last call into
    `capture`: responseReceived marks the request, loadingFinished means
    its body can be fetched over CDP. `pending` carries requests whose body
    wasn't finished yet across calls. Never raises."""
    try:
        entries = driver.get_log("performance")
    except Exception as exc:  # noqa: BLE001 — e.g. a driver without the log enabled
        log.debug("Performance log unavailable, search-API capture off: %s", exc)
        return
    for entry in entries:
        try:
            msg = json.loads(entry["message"])["message"]
        except (ValueError, KeyError, TypeError):
            continue
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "Network.responseReceived":
            response = params.get("response") or {}
            if response.get("status") == 200 and fp.is_search_api_url(response.get("url")):
                pending.add(params.get("requestId"))
        elif method == "Network.loadingFinished" and params.get("requestId") in pending:
            pending.discard(params["requestId"])
            try:
                body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": params["requestId"]})
            except WebDriverException as exc:
                log.debug("Could not read a search-API response body: %s", exc)
                continue
            text = body.get("body", "")
            if body.get("base64Encoded"):
                text = base64.b64decode(text)
            if capture.add(text):
                log.info("Captured a search-API response (status %s).", capture.status)


def scrape_search(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    user_agent: Optional[str] = None, cookies: Optional[list] = None, stats: Optional[dict] = None,
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
    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint, user_agent=user_agent, stealth=args.stealth)
    _apply_cookies(driver, cookies, start_url)
    capture = fp.SearchApiCapture()
    pending_requests: set = set()

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            driver.set_page_load_timeout(NAV_TIMEOUT_S)
            driver.get(start_url)
            time.sleep(READINESS_WAIT_S)
            try:
                reported = driver.execute_script(_STATUS_JS)
                status = int(reported) if reported else None
            except WebDriverException:
                pass
            if proxy_pool is not None and proxy is not None:
                proxy_pool.report_success(proxy)
            last_error = None
            break
        except WebDriverException as exc:
            message = str(exc)
            last_error = message
            dead = is_proxy_dead_error(message)
            if proxy_pool is not None and proxy is not None and dead:
                proxy_pool.report_failure(proxy, dead=True)
                log.warning("Proxy %s is dead (%s) — not retrying the same exit.", proxy.masked(), message)
                if stats is not None:
                    stats.setdefault("dead_proxies", []).append(proxy.masked())
                break  # CLAUDE.md §8: a dead proxy wants a different exit (run() rotates), not another try
            else:
                log.warning("Navigation attempt %d/%d failed: %s", attempt + 1, args.retries + 1, message)
            if attempt < args.retries:
                time.sleep(args.retry_delay)

    if last_error is not None:
        driver.quit()
        log.error("Search page permanently failed to load: %s", last_error)
        return [], False, True, 0, False

    if status is not None and status >= 400:
        log.warning("Search page returned HTTP %d — treating as blocked, not empty.", status)
        blocked = True

    if args.wait_for_human:
        def _page_html() -> str:
            try:
                return driver.page_source
            except WebDriverException:
                return ""
        if detect_from_html(_page_html(), fp.BOT_CHALLENGE_MARKERS):
            print("\n>>> Bot challenge in the browser window. Complete it by hand "
                  f"(press and hold the button). Waiting up to {args.wait_for_human}s...\n", file=sys.stderr, flush=True)
            deadline = time.time() + args.wait_for_human
            while time.time() < deadline:
                time.sleep(2)
                html_now = _page_html()
                if html_now and not detect_from_html(html_now, fp.BOT_CHALLENGE_MARKERS):
                    log.info("Challenge cleared by hand — continuing in this session.")
                    time.sleep(READINESS_WAIT_S)
                    # Seen live 2026-09-29: after a geo redirect the challenge
                    # returned to the HOMEPAGE, not the search, so no more
                    # results would ever arrive there.
                    if not fp.is_search_url(driver.current_url):
                        log.info("The challenge returned to %s, not the search: re-opening the search.", driver.current_url)
                        try:
                            driver.get(start_url)
                            time.sleep(READINESS_WAIT_S)
                        except Exception as exc:  # noqa: BLE001 — the round loop still reads whatever loaded
                            log.warning("Re-opening the search after the challenge failed: %s", exc)
                    blocked = False
                    break
            else:
                log.warning("--wait-for-human: challenge still there after %ds.", args.wait_for_human)

    seen_skus: set = set()
    merged: List[Product] = []
    stall = 0
    rounds = 0

    for round_num in range(args.max_scrolls + 1):
        rounds = round_num
        _drain_search_api_responses(driver, capture, pending_requests)
        html = driver.page_source
        if detect_from_html(html, fp.BOT_CHALLENGE_MARKERS):
            blocked = True
        captcha_result = _maybe_solve_captcha(html=html, url=start_url, client=client, policy=args.solve_captcha, min_score=args.min_score, rows_seen=len(merged) + len(capture.payloads))
        if captcha_result and captcha_result.get("action") in ("warning_no_key", "warning_solver_error", "detected_unidentified_widget", "unsupported_vendor", "solve_not_attempted"):
            if fp.count_result_cards(html) == 0:
                blocked = True

        result = fp.safe_parse_search_results(
            html, origin=args.origin, destination=args.destination, depart_date=args.depart_date,
            return_date=args.return_date, adults=args.adults, currency=args.currency,
            cabin_class=args.cabin_class, stops=args.stops, sort=args.sort,
            max_results=args.max_results,
        )
        result = fp.combine_results(capture.parse(
            origin=args.origin, destination=args.destination, depart_date=args.depart_date,
            return_date=args.return_date, adults=args.adults, currency=args.currency,
            cabin_class=args.cabin_class, stops=args.stops, sort=args.sort,
            max_results=args.max_results,
        ), result)
        round_skus = {_sku_key(p) for p in result.products}
        new_skus = round_skus - seen_skus
        if result.source_used == "search_api":
            # A later poll refreshes the price of rows already collected.
            fresh = {_sku_key(p): p for p in result.products}
            merged = [fresh.get(_sku_key(p), p) for p in merged]
        if new_skus:
            for p in result.products:
                if _sku_key(p) in new_skus:
                    merged.append(p)
            seen_skus |= new_skus
            stall = 0
        elif not capture.incomplete:
            # While the page is still polling (context.status "incomplete"),
            # an unchanged round is not a stall — more results are coming.
            stall += 1

        if len(merged) >= args.max_results:
            merged = merged[: args.max_results]
            break
        if stall >= args.stall_rounds:
            break
        if round_num >= args.max_scrolls:
            break

        try:
            driver.execute_script("window.scrollBy(0, 4000);")
        except WebDriverException as exc:
            log.warning("Scroll failed, stopping pagination early: %s", exc)
            scroll_error = True
            break
        time.sleep(args.scroll_delay)

    if args.dump_html and capture.payloads:
        api_dump = _dump_path(args.out).replace("_debug.html", "_search_api_debug.json")
        Path(api_dump).write_text(json.dumps(capture.payloads[-1], ensure_ascii=False), encoding="utf-8")

    if args.dump_html:
        Path(_dump_path(args.out)).write_text(driver.page_source, encoding="utf-8")

    driver.quit()
    if blocked and merged:
        # CLAUDE.md §8: detected ≠ blocking. A challenge marker (or an error
        # status) on a run that collected itineraries guarded nothing.
        log.warning("A challenge/error was seen, but %d itineraries were collected — reporting the data, not a block.", len(merged))
        blocked = False
    if stats is not None:
        dead = stats.get("dead_proxies")
        stats.clear()
        stats.update(fp.search_meta(capture, collected=len(merged), max_results=args.max_results))
        if dead:
            stats["dead_proxies"] = dead
        if stats["search_incomplete"]:
            log.warning("Stopped while the search was still incomplete (%s itineraries offered so far) — reporting partial.",
                        stats["itineraries_available"])
    return merged, blocked, remote_api_error, rounds, scroll_error


def _scrape_via_scraper_api(
    *, args: argparse.Namespace, start_url: str, client: TwoCaptchaClient,
) -> Tuple[List[Product], bool, bool, int, bool]:
    """--scraper-api's own fetch path — see playwright_scraper.py's copy of
    this function for the full rationale and the live 2026-09-22 findings
    (identical logic, duplicated per engine per this family's own
    convention — CLAUDE.md §4)."""
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


def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    search_stats: dict = {}
    start_url = _resolve_start_url(args)
    if not start_url:
        print("Error: provide --url, or --origin/--destination/--depart-date", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print(f"Error: unsupported --format {args.format!r}", file=sys.stderr)
        return EXIT_BAD_USAGE
    args.out = args.out or _default_out(args.format)
    if args.wait_for_human < 0:
        print("Error: --wait-for-human must be >= 0", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.wait_for_human:
        if args.cdp_endpoint:
            if urlparse(args.cdp_endpoint).hostname not in ("localhost", "127.0.0.1", "::1"):
                log.warning("--wait-for-human with a remote --cdp-endpoint: nobody can see that browser's "
                            "window, so a challenge there cannot be completed by hand.")
        elif args.headless:
            log.info("--wait-for-human needs a visible window: switching to --headful.")
            args.headless = False

    if args.scraper_api:
        # A whole separate, browserless code path — no Selenium/chromedriver
        # is needed at all here (see CLAUDE.md §6), so none of the checks
        # below (webdriver import, --cdp-endpoint credential shape) apply.
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
                time.sleep(args.retry_delay)
        price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
        return finish_run(
            products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
            pages_requested=1, pages_completed=0 if remote_api_error else 1, failed_pages=None,
            blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
            started_at=started_at, price_confirmed_pct=price_confirmed_pct,
        )

    if webdriver is None:
        print(f"Error: selenium is not installed ({_SELENIUM_IMPORT_ERROR}). "
              f"pip install -r requirements-selenium.txt", file=sys.stderr)
        return EXIT_CRASH
    if args.cdp_endpoint and _cdp_endpoint_has_credentials(args.cdp_endpoint):
        print(
            "Error: --cdp-endpoint carries credentials — Selenium/chromedriver's "
            "debuggerAddress takes a bare host:port and cannot authenticate a "
            "remote session. Use playwright_scraper.py or puppeteer_scraper.py "
            "for the Scraping Browser API.", file=sys.stderr,
        )
        return EXIT_BAD_USAGE

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

    user_agent = None
    cdp_refused_fingerprint = bool(args.fingerprint) and refuse_if_cdp(args.cdp_endpoint)
    if args.stealth and args.cdp_endpoint:
        # CLAUDE.md §8: never layer our own fingerprint patches over a CDP
        # browser — it brings its own identity (the Scraping Browser's, or
        # your real Chrome's), and a patched half of one is a contradiction.
        log.warning("Ignoring --stealth: a --cdp-endpoint browser already has its own identity.")
        args.stealth = False
    if args.fingerprint and not cdp_refused_fingerprint:
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    try:
        if args.cdp_endpoint:
            # See this module's docstring: unlike playwright_scraper.py /
            # puppeteer_scraper.py, reconnecting here does NOT get a fresh
            # exit identity — Selenium's debugger_address just re-attaches
            # to the SAME already-running browser. Still retries the scrape
            # itself (not nothing — a page that hadn't finished loading,
            # e.g.), and the flag exists for CLI parity (CLAUDE.md §4)
            # either way. A CREDENTIALED endpoint never reaches here at all
            # — refused as EXIT_BAD_USAGE above.
            attempts = args.cdp_block_retries + 1
            for attempt in range(attempts):
                merged, blocked, remote_api_error, rounds, scroll_error = scrape_search(
                    args=args, start_url=start_url, proxy_pool=proxy_pool, client=client,
                    user_agent=user_agent, cookies=cookies, stats=search_stats,
                )
                if remote_api_error or not blocked:
                    break
                if attempt < attempts - 1:
                    log.warning(
                        "Blocked on attempt %d/%d over --cdp-endpoint — retrying (note: "
                        "Selenium reconnects to the SAME browser session, not a fresh "
                        "one — see this module's docstring).",
                        attempt + 1, attempts,
                    )
                    time.sleep(args.retry_delay)
        else:
            # CLAUDE.md §8: a dead proxy wants a DIFFERENT exit, and a rotation
            # is a fresh browser. One attempt per proxy in the pool at most.
            rotations = len(proxy_pool) if proxy_pool else 1
            for rotation in range(rotations):
                dead_before = len(search_stats.get("dead_proxies", []))
                merged, blocked, remote_api_error, rounds, scroll_error = scrape_search(
                    args=args, start_url=start_url, proxy_pool=proxy_pool, client=client,
                    user_agent=user_agent, cookies=cookies, stats=search_stats,
                )
                if not (remote_api_error and len(search_stats.get("dead_proxies", [])) > dead_before):
                    break
                if rotation < rotations - 1:
                    log.warning("Rotating to the next proxy with a fresh browser (%d/%d).", rotation + 2, rotations)
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
        extra_meta=search_stats or None,
        incomplete=bool(search_stats.get("search_incomplete")),
        stop_reason=("search_incomplete" if search_stats.get("search_incomplete")
                     else "max_results" if search_stats.get("capped_by_max_results") else None),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_arg_parser().parse_args()
    args = env_config.apply_env(args)
    try:
        return run(args)
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())

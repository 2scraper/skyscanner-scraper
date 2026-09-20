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
docstring for the full incident): 2Captcha has no automated solve for
PerimeterX/DataDome/a bare Cloudflare managed challenge at all, confirmed
live against a real skyscanner.com challenge. `--cdp-block-retries` and
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
from scraper_api_client import TwoCaptchaClient

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
    p.add_argument("--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile's user agent (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument("--cdp-endpoint", default=None,
                    help="NOTE: refused if it carries credentials — Selenium cannot authenticate a remote CDP session")
    p.add_argument("--cdp-block-retries", type=int, default=2, help="Retry a blocked run this many times over --cdp-endpoint (parity flag — see this module's docstring: weaker here than on playwright/puppeteer, since Selenium reconnects to the SAME browser session, not a fresh one). Has no effect without --cdp-endpoint.")
    p.add_argument("--cookies-file", default=None, help="Path to a JSON array of cookies from a session a HUMAN solved manually — applied before the real navigation. A way to reuse a person's own solve, never to solve a challenge automatically.")
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


def _build_driver(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str], user_agent: Optional[str] = None):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    if _CHROME_BINARY:
        options.binary_location = _CHROME_BINARY
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
        return webdriver.Chrome(options=options)

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
    return webdriver.Chrome(service=service, options=options) if service else webdriver.Chrome(options=options)


# See stockx-scraper's selenium_scraper.py for why this reads the
# Navigation Timing API instead of a driver-native Response object.
_STATUS_JS = (
    "try { return performance.getEntriesByType('navigation')[0].responseStatus || 0; } "
    "catch (e) { return 0; }"
)


def _maybe_solve_captcha(*, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3) -> Optional[dict]:
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
        log.warning(
            "Captcha token obtained but NOT auto-injected on the Selenium "
            "engine (unverified widget-specific step) — use "
            "playwright_scraper.py or puppeteer_scraper.py with "
            "--cdp-endpoint for the Scraping Browser API's built-in solve."
        )
    elif action == "detected_unidentified_widget":
        log.warning("A captcha-like marker was detected but no known widget/sitekey could be extracted.")
    elif action == "unsupported_vendor":
        # Confirmed gap, not a bug (captcha_solver.py's module docstring) —
        # 2Captcha has no task type for this vendor at all.
        log.warning(
            "%s challenge detected — 2Captcha has no automated solve for this "
            "defense (confirmed gap, not a bug). Reporting run as blocked. "
            "See --cdp-block-retries and --cookies-file (this module's "
            "docstring has the Selenium-specific caveat on the former).",
            result.get("vendor"),
        )
    return result


def scrape_search(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
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
    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint, user_agent=user_agent)
    _apply_cookies(driver, cookies, start_url)

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
                log.warning("Proxy reported dead: %s", message)
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

    seen_skus: set = set()
    merged: List[Product] = []
    stall = 0
    rounds = 0

    for round_num in range(args.max_scrolls + 1):
        rounds = round_num
        html = driver.page_source
        if detect_from_html(html, fp.BOT_CHALLENGE_MARKERS):
            blocked = True
        captcha_result = _maybe_solve_captcha(html=html, url=start_url, client=client, policy=args.solve_captcha, min_score=args.min_score)
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
            driver.execute_script("window.scrollBy(0, 4000);")
        except WebDriverException as exc:
            log.warning("Scroll failed, stopping pagination early: %s", exc)
            scroll_error = True
            break
        time.sleep(args.scroll_delay)

    if args.dump_html:
        Path(_dump_path(args.out)).write_text(driver.page_source, encoding="utf-8")

    driver.quit()
    return merged, blocked, remote_api_error, rounds, scroll_error


def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    if webdriver is None:
        print(f"Error: selenium is not installed ({_SELENIUM_IMPORT_ERROR}). "
              f"pip install -r requirements-selenium.txt", file=sys.stderr)
        return EXIT_CRASH

    start_url = _resolve_start_url(args)
    if not start_url:
        print("Error: provide --url, or --origin/--destination/--depart-date", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print(f"Error: unsupported --format {args.format!r}", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.cdp_endpoint and _cdp_endpoint_has_credentials(args.cdp_endpoint):
        print(
            "Error: --cdp-endpoint carries credentials — Selenium/chromedriver's "
            "debuggerAddress takes a bare host:port and cannot authenticate a "
            "remote session. Use playwright_scraper.py or puppeteer_scraper.py "
            "for the Scraping Browser API.", file=sys.stderr,
        )
        return EXIT_BAD_USAGE
    args.out = args.out or _default_out(args.format)

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
                    user_agent=user_agent, cookies=cookies,
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
            merged, blocked, remote_api_error, rounds, scroll_error = scrape_search(
                args=args, start_url=start_url, proxy_pool=proxy_pool, client=client,
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
        return run(args)
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())

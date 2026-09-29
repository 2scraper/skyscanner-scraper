#!/usr/bin/env python3
"""captcha_solver.py — detection + solving policy. Comments may name a site
for context; no site-specific selector or URL lives here. Per-site markers
are passed in by the caller (see BOT_CHALLENGE_MARKERS in product_parser.py)
rather than hardcoded, so this module stays reusable across the family.

Policy, matching the family's hard-won rule that "detected != blocking":

  - Detection stays broad — run it on every page, unconditionally.
  - A captcha WIDGET present on a page whose products are already rendered
    guards nothing and is not solved: `--solve-captcha when-blocked` (the
    default) only pays for a solve when `count_product_links(html) == 0`.
  - A missing key or a solver-side error is a WARNING, never a crash — the
    run continues, and only reports EXIT_BLOCKED if the page really was
    gated (see output_writer.EXIT_BLOCKED).

No JavaScript crosses this module's boundary: it hands back a plain
(captcha_type, sitekey, token) triple. The engine — which already speaks
its own driver's dialect (Playwright/Selenium/pyppeteer) — is the one that
calls page.evaluate / execute_script to inject the token, because that is
exactly the kind of per-engine primitive `page_flow.py`-style code should
own instead of a shared module quietly picking one driver's dialect.

**What this module solves, and what it only names.** `CaptchaType` covers
the widget families this repo builds 2Captcha tasks for (Turnstile,
reCAPTCHA v2/v3, hCaptcha). `identify_unsupported_vendor()` names a
defense this repo does NOT implement a solve for — PerimeterX, DataDome, a
Cloudflare managed-challenge interstitial — so the log states the real
reason instead of "no known widget/sitekey", which reads like a parser
bug. That is a statement about this repo, not about 2Captcha (CLAUDE.md
§19): 2Captcha documents `DataDomeSliderTask` for DataDome
(2captcha.com/api-docs/datadome-slider-captcha, read 2026-09-29), and its
PerimeterX page
(2captcha.com/p/perimeterx-solver, read 2026-09-29) says that solver is
under development / custom for large volumes. On skyscanner.com the
challenge (captured 2026-09-17/22/29) is PerimeterX's own "Press & Hold"
with no third-party widget on the page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence

from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

# Generic signals any family site might show. A per-site BOT_CHALLENGE_MARKERS
# list (product_parser.py) is unioned with this, never a replacement for it.
GENERIC_BOT_CHALLENGE_MARKERS: Sequence[str] = (
    # No "cf-turnstile" (CLAUDE.md §8): the Scraping Browser's extension
    # injects it into good pages, and measured on sibling repos it missed
    # real Cloudflare challenges. `challenges.cloudflare.com` is on every
    # real challenge and on no served page. (identify_widget() still uses
    # the class to read a Turnstile sitekey once something was detected.)
    "challenges.cloudflare.com", "cdn-cgi/challenge-platform",
    "g-recaptcha", "recaptcha/api.js", "grecaptcha",
    "h-captcha", "hcaptcha.com/1/api.js",
    "px-captcha", "perimeterx",
    "datadome",
    "attention required", "just a moment", "verify you are human",
)


class CaptchaType(str, Enum):
    CLOUDFLARE_TURNSTILE = "cloudflare_turnstile"
    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    HCAPTCHA = "hcaptcha"


_SITEKEY_PATTERNS = {
    CaptchaType.CLOUDFLARE_TURNSTILE: re.compile(
        r'class="[^"]*cf-turnstile[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
    CaptchaType.RECAPTCHA_V2: re.compile(
        r'class="[^"]*g-recaptcha[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
    CaptchaType.HCAPTCHA: re.compile(
        r'class="[^"]*h-captcha[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
}
# v3 ships no visible widget — the sitekey rides on the loader's `render=`
# query param instead of a data-sitekey attribute.
_RECAPTCHA_V3_LOADER_RE = re.compile(r"recaptcha/api\.js\?render=([\w-]+)")
_RECAPTCHA_EXPLICIT_LOADER_RE = re.compile(r"recaptcha/api\.js\?render=explicit")


@dataclass
class CaptchaSignal:
    captcha_type: CaptchaType
    sitekey: Optional[str]
    invisible: bool = False


# The Scraping Browser API's managed Chromium ships 2Captcha's OWN
# captcha-solving extension pre-installed (confirmed live, 2026-09-14:
# extension id kjmkgkdkpedkejedfhmfcenooemhbpbo, content scripts named
# .../captcha/{turnstile,amazon_waf,yandex,lemin}/{interceptor,hunter}.js).
# `page.content()` over --cdp-endpoint captures THOSE injected <script>
# tags too, and their src/data attributes contain marker substrings like
# "cf-turnstile" regardless of whether the actual page has a real widget —
# on a real StockX 403 that turned out to be a plain, static Cloudflare
# WAF page with no captcha at all ("Sorry, you have been blocked" — no
# `data-sitekey` anywhere), this alone made GENERIC_BOT_CHALLENGE_MARKERS
# fire and identify_widget() correctly find nothing to solve, which then
# logged as a confusing "captcha-like marker detected but no known
# widget/sitekey" rather than a clean "no captcha present". Stripped here
# so the extension's own always-there code never counts as the page's.
# Still needed after dropping "cf-turnstile": the reCAPTCHA hunter's
# attributes can carry "g-recaptcha", which IS in the marker set.
_EXTENSION_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\bchrome-extension://[^>]*>.*?</script>', re.I | re.S
)


def _strip_extension_noise(html: str) -> str:
    return _EXTENSION_SCRIPT_RE.sub("", html)


def detect_from_html(html: str, extra_markers: Sequence[str] = ()) -> bool:
    """Broad detection: True if ANY known marker string appears. Cheap and
    deliberately over-inclusive — see module docstring on why detection and
    blocking are decided separately."""
    haystack = _strip_extension_noise(html).lower()
    for marker in (*GENERIC_BOT_CHALLENGE_MARKERS, *extra_markers):
        if marker.lower() in haystack:
            return True
    return False


# Markers whose VENDOR is known but for which THIS REPO builds no solve
# task (see the module docstring for what 2Captcha itself documents —
# this is a TODO list, not a statement about the vendor's product). Checked
# ONLY after `identify_widget()` returned None for a marker that DID match,
# so a real Turnstile / reCAPTCHA / hCaptcha widget never lands here.
_UNSUPPORTED_VENDOR_MARKERS = {
    "perimeterx": ("px-captcha", "perimeterx"),
    "datadome": ("datadome",),
    "cloudflare_managed_challenge": ("cdn-cgi/challenge-platform", "just a moment", "attention required"),
}


def identify_unsupported_vendor(html: str) -> Optional[str]:
    """Best-effort: names WHICH known bot-defense vendor is present, for the
    case where a marker matched but `identify_widget()` could not turn it
    into a solvable `(type, sitekey)` pair — because no such pair exists
    for this vendor, not because extraction failed. Returns a short vendor
    name for an honest log message; the caller must not treat this as
    something actionable (see `solve_when_blocked`'s `"unsupported_vendor"`
    action, which reports EXIT_BLOCKED same as any other block, just with
    an accurate reason instead of a vague one)."""
    haystack = _strip_extension_noise(html).lower()
    for vendor, markers in _UNSUPPORTED_VENDOR_MARKERS.items():
        if any(m in haystack for m in markers):
            return vendor
    return None


def identify_widget(html: str) -> Optional[CaptchaSignal]:
    """Best-effort: which widget, and its sitekey. Reconciles the v3-vs-
    v2-invisible ambiguity by trusting the LOADER'S query param over any
    wrapper metadata — a site can label its own wrapper "v3" while shipping
    a `render=explicit` (v2-invisible) loader; sending v3 parameters for a
    v2-invisible widget buys a token the site rejects. `render=<sitekey>`
    means v3; `render=explicit` means v2."""
    html = _strip_extension_noise(html)
    m = _RECAPTCHA_V3_LOADER_RE.search(html)
    if m and not _RECAPTCHA_EXPLICIT_LOADER_RE.search(html):
        return CaptchaSignal(CaptchaType.RECAPTCHA_V3, sitekey=m.group(1))
    for ctype, pattern in _SITEKEY_PATTERNS.items():
        m = pattern.search(html)
        if m:
            return CaptchaSignal(ctype, sitekey=m.group(1))
    return None


def _task_payload(signal: CaptchaSignal, page_url: str, *, proxyless: bool, min_score: float = 0.3) -> dict:
    type_map = {
        CaptchaType.CLOUDFLARE_TURNSTILE: "TurnstileTaskProxyless" if proxyless else "TurnstileTask",
        CaptchaType.RECAPTCHA_V2: "RecaptchaV2TaskProxyless" if proxyless else "RecaptchaV2Task",
        CaptchaType.RECAPTCHA_V3: "RecaptchaV3TaskProxyless",
        CaptchaType.HCAPTCHA: "HCaptchaTaskProxyless" if proxyless else "HCaptchaTask",
    }
    task = {"type": type_map[signal.captcha_type], "websiteURL": page_url, "websiteKey": signal.sitekey}
    if signal.captcha_type == CaptchaType.RECAPTCHA_V3:
        # 2Captcha's own field name for RecaptchaV3TaskProxyless (confirmed
        # against their API reference) — the token 2Captcha hands back is
        # produced to clear THIS threshold; it is a solve-request input, not
        # something to validate against the response afterwards. `--min-
        # score` is the CLI knob; omitting it would leave 2Captcha's own
        # server-side default in effect, silently, which is the same
        # "documented flag nobody reads" shape this family has shipped
        # before (CLAUDE.md §17) — so this module always sends one.
        task["minScore"] = min_score
    return task


def solve_when_blocked(
    *,
    client: TwoCaptchaClient,
    page_url: str,
    html: str,
    count_product_links: Callable[[str], int],
    extra_markers: Sequence[str] = (),
    proxyless: bool = True,
    min_score: float = 0.3,
    allow_paid_solve: bool = True,
) -> dict:
    """The default `--solve-captcha when-blocked` policy. Cheap first: count
    product links on the page AS-IS — no readiness wait, no scroll — because
    a page whose products are already rendered is not blocked, and running
    a 20s readiness wait first would burn it even when solving is what
    actually makes products appear on a page that IS gated.
    """
    if not detect_from_html(html, extra_markers):
        return {"action": "no_captcha_detected"}

    if count_product_links(html) > 0:
        return {"action": "skipped_products_present"}

    signal = identify_widget(html)
    if signal is None:
        vendor = identify_unsupported_vendor(html)
        if vendor:
            return {"action": "unsupported_vendor", "vendor": vendor}
        return {"action": "detected_unidentified_widget"}

    if not allow_paid_solve:
        # A token the caller cannot inject buys nothing (CLAUDE.md §23:
        # "we solved it" and "the token got in" are different claims).
        return {"action": "solve_not_attempted", "captcha_type": signal.captcha_type.value,
                "reason": "no token-injection path for this site"}
    try:
        task = _task_payload(signal, page_url, proxyless=proxyless, min_score=min_score)
        token = client.solve_and_wait(task)
        return {"action": "solved", "captcha_type": signal.captcha_type.value, "token": token}
    except TwoCaptchaAuthError as exc:
        return {"action": "warning_no_key", "detail": str(exc)}
    except TwoCaptchaError as exc:
        return {"action": "warning_solver_error", "detail": str(exc)}

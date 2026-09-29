#!/usr/bin/env python3
"""flight_parser.py — this IS the skyscanner.com site knowledge (the
flight-family analog of stockx-scraper's product_parser.py).

**Honesty note, read before trusting anything below**: stockx-scraper's
product_parser.py was verified against real, live captures of stockx.com.
No equivalent capture exists for skyscanner.com in this repo. WebFetch on
skyscanner.com search URLs returns ROBOTS_DISALLOWED, and bypassing that
restriction is not something this assistant will do — so nothing here has
been checked against the real, current skyscanner.com markup. Every
selector and JSON-shape guess below is marked `# TODO: verify live` and
was carried over from (or extended from) the original draft's own honest
"selectors unverified" disclosure. Do not point this at production
traffic before running `--dump-html` once and confirming these still
match — see README "What's been tested vs. what hasn't".

Parsing strategy, in priority order (same "embedded-JSON-first, DOM
fallback" family principle as stockx-scraper, applied cautiously since the
embedded-JSON shape below is a generic heuristic, not a confirmed query
name like stockx's `getDiscoveryData`):

  1. `__NEXT_DATA__` (skyscanner.com is Next.js-based per public knowledge)
     is parsed, then walked generically for any list of dicts that LOOKS
     like a flight-result list (keys suggesting price + legs/segments) —
     see `_find_itinerary_lists()`. No hardcoded query name: this repo
     doesn't know skyscanner.com's real query name the way stockx-scraper
     knows `getDiscoveryData`.
  2. DOM fallback: `[data-testid="result-card"]` and a handful of sibling
     testid guesses, matching the original draft's TODO-marked selectors.

Both paths feed the same `Product` shape from output_writer.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

from output_writer import Product

log = logging.getLogger("flight_parser")

BASE_URL = "https://www.skyscanner.com"
SOURCE = "skyscanner.com"

STOPS_VALUES = ("any", "direct", "1stop")
SORT_VALUES = ("best", "cheapest", "fastest")
CABIN_CLASS_VALUES = ("economy", "premiumeconomy", "business", "first")

MIN_CARD_MATCHES = 2  # per family invariant (see stockx-scraper's product_parser.py
                       # MIN_CARD_MATCHES): must be >1, or one unrelated link resolves
                       # a "rendered" wait early.

# REAL, live-captured incident (2026-09-17, via Roman's own run — see
# TESTING.md): a plain local-first request (no proxy, no fingerprint) got
# served PerimeterX's "Are you a person or a robot?" challenge page
# (skyscanner.nl locale that day; the Dutch h1 itself is NOT used as a
# marker below — too locale-specific, same lesson MediaMarkt's German-vs-
# Spanish block page taught the rest of this family) instead of search
# results. `captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS` already catches
# this via its generic `"px-captcha"` string (confirmed: that page had
# `<div id="px-captcha">` and DID get classified `blocked`, not silently
# `empty`) — the two markers below are added anyway, as durable,
# locale-independent, skyscanner-specific corroboration of the SAME
# incident, not a replacement for the generic check:
#   - `/sttc/px/captcha-v2/` — skyscanner's OWN asset path serving
#     PerimeterX's captcha artwork (`.../sttc/px/captcha-v2/captcha-01.svg`),
#     which a genuine results page has no reason to reference.
# `px-cloud.net` was a marker here until 2026-09-29 and was REMOVED: it is
# the host of PerimeterX's background sensor (a `js.px-cloud.net` iframe),
# which a real, unchallenged skyscanner page (2026-09-22 capture) also
# loads — so it flagged every normal page as blocked.
# What this incident does NOT confirm: `flight_parser.py`'s selectors
# below (`# TODO: verify live`) — this was a block page, not a results
# page, so the actual parsing logic is still unverified. See TESTING.md
# for what a captured RESULTS page would need to confirm next, and note
# `captcha_solver.py` has no solving path for PerimeterX specifically
# (its `CaptchaType` enum only covers Turnstile/reCAPTCHA/hCaptcha) — a
# `--cdp-endpoint` run relies entirely on the Scraping Browser API's own
# auto-solve extension for this one, not on this repo's manual solver.
BOT_CHALLENGE_MARKERS: tuple = (
    "/sttc/px/captcha-v2/",
)


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def _yymmdd(date_str: str) -> str:
    """'2026-09-15' -> '260915', the path-segment date format used by
    skyscanner.com's public URL scheme (unverified live — see module
    docstring; carried over from the original draft, which itself did not
    claim to have confirmed it against a real response)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%y%m%d")


def search_url(
    *,
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str] = None,
    adults: int = 1,
    currency: str = "USD",
    cabin_class: str = "economy",
    stops: str = "any",
    sort: str = "best",
) -> str:
    """Builds a skyscanner.com flight-search URL.

    # TODO: verify live — path shape `/transport/flights/{o}/{d}/{date}[/{date}]/`
    and query params (`adults`, `currency`, `cabinclass`, `stops`, `sort`)
    match the original draft's own understanding of the site, not a
    confirmed live response (see module docstring)."""
    o, d = origin.strip().upper(), destination.strip().upper()
    path = f"/transport/flights/{o}/{d}/{_yymmdd(depart_date)}/"
    if return_date:
        path += f"{_yymmdd(return_date)}/"
    query = {
        "adults": adults,
        "currency": currency,
        "cabinclass": cabin_class,
    }
    if stops and stops != "any":
        query["stops"] = stops
    if sort and sort != "best":
        query["sort"] = sort
    return f"{BASE_URL}{path}?{urlencode(query)}"


def is_search_url(url: str) -> bool:
    return "/transport/flights/" in urlparse(url).path


# --------------------------------------------------------------------------- #
# sku fingerprint — Skyscanner has no persistent SKU (see output_writer.
# Product's docstring). `sku` here is a deterministic fingerprint of the
# ITINERARY (everything except price), so the same itinerary scraped on
# two different days gets the same sku and diff_runs.py can report a real
# price CHANGE instead of one result vanishing and another appearing.
# --------------------------------------------------------------------------- #
def make_sku(
    *,
    origin: Optional[str], destination: Optional[str],
    depart_date: Optional[str], return_date: Optional[str],
    airline: Optional[str], departure_time: Optional[str],
    arrival_time: Optional[str], cabin_class: Optional[str],
    flight_numbers: Optional[List[str]] = None,
) -> str:
    basis = "|".join(str(x or "") for x in (
        origin, destination, depart_date, return_date, airline,
        departure_time, arrival_time, cabin_class,
    ))
    # Codeshares: two offers can share airline + times but differ in the
    # marketing flight number of a connecting segment (seen live: TP4313 vs
    # B6317 on BOS→JFK). Appended only when known, so DOM-path skus are unchanged.
    if flight_numbers:
        basis += "|" + ",".join(flight_numbers)
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    route = f"{origin or '??'}{destination or '??'}"
    date_part = (depart_date or "").replace("-", "")
    return f"{route}-{date_part}-{digest}"


# --------------------------------------------------------------------------- #
# __NEXT_DATA__ extraction + generic itinerary-shaped-list walk
# --------------------------------------------------------------------------- #
def extract_next_data(html: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        return json.loads(tag.string)
    except (ValueError, TypeError):
        log.debug("__NEXT_DATA__ present but not parseable as JSON")
        return None


# Structural signal for "this dict looks like one flight itinerary result",
# used only to find candidate lists inside an unknown JSON shape — NOT a
# confirmed field-name contract (see module docstring). A dict counts as a
# candidate if it has something price-shaped AND something itinerary-shaped.
_PRICE_KEYS = ("price", "totalPrice", "minPrice", "amount")
_ITINERARY_KEYS = ("legs", "segments", "carriers", "pricingOptions", "deeplink", "deepLink")


def _looks_like_itinerary(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    has_price = any(k in node for k in _PRICE_KEYS)
    has_itinerary_shape = any(k in node for k in _ITINERARY_KEYS)
    return has_price and has_itinerary_shape


def _find_itinerary_lists(node: Any, depth: int = 0, found: Optional[List[List[dict]]] = None) -> List[List[dict]]:
    """Generic best-effort walk (see module docstring — no confirmed query
    name exists for this site yet). Returns every list found whose items
    mostly look like itineraries, so the caller can pick the biggest one."""
    if found is None:
        found = []
    if depth > 14:
        return found
    if isinstance(node, list):
        if node:
            matches = sum(1 for item in node if _looks_like_itinerary(item))
            if matches >= max(1, len(node) // 2):
                found.append([item for item in node if isinstance(item, dict)])
        for item in node:
            _find_itinerary_lists(item, depth + 1, found)
    elif isinstance(node, dict):
        for value in node.values():
            _find_itinerary_lists(value, depth + 1, found)
    return found


def _num(node: Any, *path: str) -> Optional[float]:
    cur = node
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if isinstance(cur, (int, float)):
        return float(cur)
    if isinstance(cur, str):
        try:
            return float(cur.replace(",", ""))
        except ValueError:
            return None
    return None


def _itinerary_node_to_product(
    node: dict, *, origin: str, destination: str, depart_date: str,
    return_date: Optional[str], adults: int, cabin_class: str,
    stops: str, sort: str, currency: str, price_source: str = "embedded_json",
) -> Optional[Product]:
    """# TODO: verify live — field names below (`price`/`legs[0].carriers`/
    `legs[0].departure`/...) are a best-effort guess at a plausible Apollo/
    GraphQL shape for a flight-search result, not a confirmed one. Returns
    None (rather than a half-populated Product) if not even a price can be
    found, so a shape this guess gets wrong degrades to "found nothing
    here" instead of fabricating a row with a fake price."""
    # Verified 2026-09-29 against a real `web-unified-search` response
    # (LHR→JFK, 373 itineraries): the price lives in `price.raw` (float) with
    # `price.formatted` alongside ("£323"); `amount` only appears inside
    # `pricingOptions[].price`. The other paths are kept as fallbacks.
    price = (
        _num(node, "price", "raw") or _num(node, "price", "amount") or _num(node, "price")
        or _num(node, "totalPrice", "amount") or _num(node, "totalPrice")
        or _num(node, "minPrice")
    )
    if price is None:
        return None

    legs = node.get("legs") if isinstance(node.get("legs"), list) else []
    leg0 = legs[0] if legs and isinstance(legs[0], dict) else {}
    carriers = leg0.get("carriers")
    if isinstance(carriers, dict):
        # Real shape: {"marketing": [{"name": "jetBlue", ...}], "operationType": ...}
        carriers = carriers.get("marketing")
    carriers = carriers if isinstance(carriers, list) else None
    airline = None
    if carriers and isinstance(carriers[0], dict):
        airline = carriers[0].get("name")
    elif isinstance(node.get("carriers"), list) and node["carriers"]:
        c0 = node["carriers"][0]
        airline = c0.get("name") if isinstance(c0, dict) else None

    departure_time = leg0.get("departure") or leg0.get("departureTime")
    arrival_time = leg0.get("arrival") or leg0.get("arrivalTime")
    duration = leg0.get("durationInMinutes") or leg0.get("duration")
    if isinstance(duration, (int, float)):
        duration = f"{int(duration) // 60}h {int(duration) % 60}m"
    stop_count = leg0.get("stopCount")
    stops_label = (
        "Direct" if stop_count == 0
        else f"{stop_count} stop{'s' if stop_count != 1 else ''}" if isinstance(stop_count, int)
        else None
    )

    deep_link = node.get("deeplink") or node.get("deepLink") or node.get("bookingUrl")
    if not deep_link:
        # Real shape: pricingOptions[0].items[0].url — a site-relative
        # "/transport_deeplink/4.0/UK/en-GB/GBP/<agent>/..." path.
        opts = node.get("pricingOptions")
        items = opts[0].get("items") if opts and isinstance(opts[0], dict) else None
        if items and isinstance(items[0], dict):
            deep_link = items[0].get("url")
    if isinstance(deep_link, str) and deep_link.startswith("/"):
        deep_link = BASE_URL + deep_link
    # The site prices in the market's currency, not necessarily the one
    # requested — the deeplink path carries the real one (".../en-GB/GBP/...").
    # Not read from the data → null, never the requested --currency
    # (CLAUDE.md §8: "Missing currency is null").
    m = re.search(r"/transport_deeplink/[^/]+/[^/]+/[^/]+/([A-Z]{3})/", deep_link or "")
    currency = m.group(1) if m else None

    flight_numbers = [
        f"{(seg.get('marketingCarrier') or {}).get('displayCode', '')}{seg.get('flightNumber', '')}"
        for leg in legs if isinstance(leg, dict)
        for seg in (leg.get("segments") or []) if isinstance(seg, dict) and seg.get("flightNumber")
    ]
    sku = make_sku(
        origin=origin, destination=destination, depart_date=depart_date,
        return_date=return_date, airline=airline, departure_time=departure_time,
        arrival_time=arrival_time, cabin_class=cabin_class,
        flight_numbers=flight_numbers,
    )
    title = f"{origin} → {destination}" + (f" · {airline}" if airline else "")

    return Product(
        sku=sku,
        source=SOURCE,
        category="flights",
        title=title,
        brand=airline,
        price=price,
        currency=currency,
        price_source=price_source,
        product_url=deep_link or None,
        image_url=None,
        scraped_at=_now_iso(),
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        return_date=return_date,
        adults=adults,
        cabin_class=cabin_class,
        stops=stops_label or stops,
        departure_time=departure_time,
        arrival_time=arrival_time,
        duration=duration,
        sort=sort,
    )


@dataclass
class SearchResult:
    products: List[Product]
    source_used: str  # "embedded_json" | "dom" | "none"


def parse_search_json(
    data: Any, *, origin: str, destination: str, depart_date: str,
    return_date: Optional[str] = None, adults: int = 1, currency: str = "USD",
    cabin_class: str = "economy", stops: str = "any", sort: str = "best",
    max_results: Optional[int] = None,
) -> SearchResult:
    """Parse an already-decoded search JSON payload — e.g. the body of the
    `web-unified-search` XHR (`itineraries.results[]`), which is where the
    real flight list arrives; the initial HTML is only an empty shell (see
    README). Uses the same generic itinerary-list walk as the HTML path."""
    candidate_lists = _find_itinerary_lists(data)
    if not candidate_lists:
        return SearchResult(products=[], source_used="none")
    products = []
    for node in max(candidate_lists, key=len):
        p = _itinerary_node_to_product(
            node, origin=origin, destination=destination, depart_date=depart_date,
            return_date=return_date, adults=adults, cabin_class=cabin_class,
            stops=stops, sort=sort, currency=currency, price_source="search_api",
        )
        if p is not None:
            products.append(p)
        if max_results and len(products) >= max_results:
            break
    return SearchResult(products=products, source_used="search_api" if products else "none")


# --------------------------------------------------------------------------- #
# XHR capture — the real flight list never appears in the initial HTML (an
# empty shell, confirmed 2026-09-22); the page fetches it from
# `web-unified-search` and re-polls it while `context.status` is
# "incomplete" (confirmed on the 2026-09-29 capture). Engines hand every
# matching response body to a SearchApiCapture; each round parses it.
# --------------------------------------------------------------------------- #
SEARCH_API_URL_MARKERS = ("unified-search",)


def is_search_api_url(url: str) -> bool:
    return any(m in (url or "") for m in SEARCH_API_URL_MARKERS)


class SearchApiCapture:
    """Engine-agnostic store of captured search-API payloads."""

    def __init__(self) -> None:
        self.payloads: List[Any] = []
        self.status: Optional[str] = None  # context.status of the latest payload

    def add(self, body: Any) -> bool:
        """Accept a decoded JSON object or its raw text; ignore anything that
        isn't JSON or carries no itinerary-shaped list. Returns True if kept."""
        if isinstance(body, (bytes, str)):
            try:
                body = json.loads(body)
            except (ValueError, TypeError):
                return False
        if not _find_itinerary_lists(body):
            return False
        self.payloads.append(body)
        ctx = body.get("context") if isinstance(body, dict) else None
        self.status = ctx.get("status") if isinstance(ctx, dict) else None
        return True

    @property
    def incomplete(self) -> bool:
        return self.status == "incomplete"

    def parse(self, **kwargs) -> SearchResult:
        """Products from every payload so far; a later poll's row replaces an
        earlier one with the same sku (prices get refreshed while polling)."""
        by_sku: Dict[str, Product] = {}
        for body in self.payloads:
            try:
                res = parse_search_json(body, **{**kwargs, "max_results": None})
            except Exception as exc:  # noqa: BLE001 — same rule as safe_parse_search_results
                log.error("A captured search-API payload failed to parse — skipping it: %s", exc)
                continue
            for p in res.products:
                by_sku[p.sku] = p
        products = list(by_sku.values())
        if kwargs.get("max_results"):
            products = products[: kwargs["max_results"]]
        return SearchResult(products=products, source_used="search_api" if products else "none")


def combine_results(api: SearchResult, html: SearchResult) -> SearchResult:
    """One round's rows: captured search-API rows first (the real source),
    then any HTML-derived rows whose sku the API didn't already cover."""
    if not api.products:
        return html
    skus = {p.sku for p in api.products}
    extra = [p for p in html.products if p.sku not in skus]
    return SearchResult(products=api.products + extra, source_used=api.source_used)


def search_meta(capture: SearchApiCapture, *, collected: int, max_results: int) -> dict:
    """The site's own arithmetic beside `status` (CLAUDE.md §21 "complete and
    exhaustive are different words"): how many itineraries the search API
    offered, whether it had finished, and whether --max-results cut it."""
    ids = set()
    for body in capture.payloads:
        for lst in _find_itinerary_lists(body):
            ids.update(str(n.get("id")) for n in lst if isinstance(n, dict) and n.get("id"))
    available = len(ids) if ids else None
    capped = available is not None and collected >= max_results and available > collected
    return {
        "results_source": "search_api" if capture.payloads else "html",
        "search_api_responses": len(capture.payloads),
        "search_api_status": capture.status,
        "itineraries_available": available,
        "capped_by_max_results": capped,
        # still polling when we stopped, and not because of our own cap
        "search_incomplete": capture.incomplete and not capped,
    }


def parse_search_results(
    html: str, *, origin: str, destination: str, depart_date: str,
    return_date: Optional[str] = None, adults: int = 1, currency: str = "USD",
    cabin_class: str = "economy", stops: str = "any", sort: str = "best",
    max_results: Optional[int] = None,
) -> SearchResult:
    next_data = extract_next_data(html)
    if next_data is not None:
        candidate_lists = _find_itinerary_lists(next_data)
        if candidate_lists:
            best = max(candidate_lists, key=len)
            products = []
            for node in best:
                p = _itinerary_node_to_product(
                    node, origin=origin, destination=destination, depart_date=depart_date,
                    return_date=return_date, adults=adults, cabin_class=cabin_class,
                    stops=stops, sort=sort, currency=currency,
                )
                if p is not None:
                    products.append(p)
                if max_results and len(products) >= max_results:
                    break
            if products:
                return SearchResult(products=products, source_used="embedded_json")

    dom_products = _parse_result_cards_from_dom(
        html, origin=origin, destination=destination, depart_date=depart_date,
        return_date=return_date, adults=adults, cabin_class=cabin_class,
        stops=stops, sort=sort, currency=currency, max_results=max_results,
    )
    if dom_products:
        return SearchResult(products=dom_products, source_used="dom")

    return SearchResult(products=[], source_used="none")


# --------------------------------------------------------------------------- #
# DOM fallback — candidate selectors are best-effort guesses, matching the
# original draft's own `# TODO` disclosure, not confirmed against a live
# page (see module docstring).
# --------------------------------------------------------------------------- #
_CARD_SELECTORS = (
    '[data-testid="result-card"]',
    '[data-testid="itinerary-card"]',
    '[data-testid="fqs-day-of-week-result-item"]',
)
_PRICE_SELECTORS = ('[data-testid="price"]', '[data-testid="itinerary-price"]', ".price")
_AIRLINE_SELECTORS = ('[data-testid="airline-name"]', '[data-testid="carrier-name"]', ".airline-name")
_TIME_SELECTORS = ('[data-testid="departure-time"]', ".departure-time")
_ARRIVAL_SELECTORS = ('[data-testid="arrival-time"]', ".arrival-time")
_DURATION_SELECTORS = ('[data-testid="duration"]', ".duration")
_STOPS_SELECTORS = ('[data-testid="stops"]', ".stops")
_LINK_SELECTORS = ('a[href*="/transport/flights/"]', "a[href]")

_PRICE_TEXT_RE = re.compile(r"[\d][\d.,]*")


def _first_text(card, selectors) -> Optional[str]:
    for sel in selectors:
        found = card.select_one(sel)
        if found:
            text = found.get_text(strip=True)
            if text:
                return text
    return None


def _parse_price_text(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = _PRICE_TEXT_RE.search(text.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def count_result_cards(html: str) -> int:
    """Used by captcha_solver.solve_when_blocked — cheap presence check,
    no readiness wait."""
    soup = BeautifulSoup(html, "html.parser")
    for sel in _CARD_SELECTORS:
        matches = soup.select(sel)
        if matches:
            return len(matches)
    return 0


def _parse_result_cards_from_dom(
    html: str, *, origin: str, destination: str, depart_date: str,
    return_date: Optional[str], adults: int, cabin_class: str,
    stops: str, sort: str, currency: str, max_results: Optional[int],
) -> List[Product]:
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for sel in _CARD_SELECTORS:
        cards = soup.select(sel)
        if cards:
            break
    if not cards:
        return []

    products: List[Product] = []
    for card in cards:
        price = _parse_price_text(_first_text(card, _PRICE_SELECTORS))
        if price is None:
            continue  # no usable price on this card — skip rather than fabricate a row
        airline = _first_text(card, _AIRLINE_SELECTORS)
        departure_time = _first_text(card, _TIME_SELECTORS)
        arrival_time = _first_text(card, _ARRIVAL_SELECTORS)
        duration = _first_text(card, _DURATION_SELECTORS)
        stops_label = _first_text(card, _STOPS_SELECTORS)

        deep_link = None
        for sel in _LINK_SELECTORS:
            a = card.select_one(sel)
            if a and a.get("href"):
                href = a["href"]
                deep_link = href if href.startswith("http") else f"{BASE_URL}{href}"
                break

        sku = make_sku(
            origin=origin, destination=destination, depart_date=depart_date,
            return_date=return_date, airline=airline, departure_time=departure_time,
            arrival_time=arrival_time, cabin_class=cabin_class,
        )
        title = f"{origin} → {destination}" + (f" · {airline}" if airline else "")
        products.append(Product(
            sku=sku, source=SOURCE, category="flights", title=title, brand=airline,
            price=price, currency=None, price_source="dom", product_url=deep_link,
            image_url=None, scraped_at=_now_iso(), origin=origin, destination=destination,
            depart_date=depart_date, return_date=return_date, adults=adults,
            cabin_class=cabin_class, stops=stops_label or stops, departure_time=departure_time,
            arrival_time=arrival_time, duration=duration, sort=sort,
        ))
        if max_results and len(products) >= max_results:
            break
    return products


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_parse_search_results(html: str, **kwargs) -> SearchResult:
    """Wraps `parse_search_results` so an unexpected exception INSIDE
    parsing (a round whose markup doesn't match either extraction path in
    some new way `parse_search_results` itself doesn't already guard
    against) degrades that ONE scroll round to "found nothing new here"
    instead of propagating out of an engine's scroll loop and crashing the
    whole run — which would discard every itinerary already collected in
    earlier rounds. Same family lesson as stockx-scraper's `safe_parse` in
    each of its three engines (see their docstrings for the live
    reproduction that motivated it): one bad round is a reason to log and
    move on, not to lose everything gathered so far. All three of this
    repo's engines call this instead of `parse_search_results` directly."""
    try:
        return parse_search_results(html, **kwargs)
    except Exception as exc:  # noqa: BLE001 — see docstring above
        log.error("A scroll round's HTML failed to parse — treating it as empty, not crashing: %s", exc)
        return SearchResult(products=[], source_used="none")

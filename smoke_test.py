#!/usr/bin/env python3
"""smoke_test.py — one file of plain functions with inline/synthetic-
fixture checks. No pytest, no conftest. `tests/test_smoke.py` wraps this as
a single pytest entry point so `pytest` also works, without a second copy
of the checks.

**Honesty note, read before trusting a green run**: unlike stockx-scraper's
smoke_test.py, none of the HTML fixtures here are real captures of
skyscanner.com — none exist (see flight_parser.py's module docstring: this
site is robots.txt-restricted to WebFetch, and that restriction was not
bypassed). Every fixture below is SYNTHETIC — hand-written to exercise the
parsing code paths, not measured against a live response. A green run
here proves the architecture (exit codes, dedupe, precedence, credential
redaction, CLI validation, engines importing cleanly) is sound, and that
the parser's OWN LOGIC does what it says on markup shaped the way the
draft/this repo GUESSED skyscanner.com looks. It does NOT prove
flight_parser.py's selectors or JSON-shape heuristics match the real,
current site — that still needs a live `--dump-html` capture, same as
README "What's been tested vs. what hasn't" says.

Run directly: `python3 smoke_test.py`
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import captcha_solver
import diff_runs
import env_config
import flight_parser as fp
import output_writer
import proxy_pool
import puppeteer_scraper
import scraper_api_client
import selenium_scraper

try:
    import playwright_scraper
except Exception as exc:  # pragma: no cover — this import itself must never fail
    raise AssertionError(f"playwright_scraper must import cleanly even without playwright installed: {exc}") from exc

ROOT = Path(__file__).parent

RESULTS = []  # (name, ok, detail)


def check(name):
    """Runs the decorated function IMMEDIATELY (at module-load time) and
    records the outcome — see stockx-scraper's smoke_test.py for the same
    pattern and the reason every check function is named `_`: only RESULTS
    is ever read, nothing looks a check up by name."""
    def decorator(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc)))
        except Exception as exc:  # a check that crashes is still a failure, not an uncaught traceback
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        return fn
    return decorator


# --------------------------------------------------------------------------- #
# Engines must import cleanly with NO engine library installed, and each
# import its driver at module level guarded by try/except ImportError —
# same family invariant as stockx-scraper (CLAUDE.md §6 there).
# --------------------------------------------------------------------------- #
@check("engines import cleanly regardless of installed drivers")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        assert hasattr(mod, "build_arg_parser")
        assert hasattr(mod, "run")


@check("each engine imports its driver at MODULE level, guarded by try/except ImportError")
def _():
    for path, marker in (
        ("playwright_scraper.py", "except ImportError as _IMPORT_ERROR"),
        ("selenium_scraper.py", "except ImportError as _IMPORT_ERROR"),
        ("puppeteer_scraper.py", "except ImportError as _IMPORT_ERROR"),
    ):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert marker in src, f"{path}: missing guarded driver import"


@check("no forbidden overclaiming wording in any shipped .py/.md file")
def _():
    # Generic guard (no family CLAUDE.md banned-wording list is available
    # for this repo — see the top-level session notes) against the kind of
    # overclaim this family's own README/docstrings consistently avoid:
    # "guaranteed", "100% undetectable", "bypass all", "never gets
    # blocked". Absence proven here, not lifted from a document this repo
    # doesn't have a copy of.
    banned = ("undetectable", "100% success", "bypass all", "never gets blocked", "guaranteed to work")
    # smoke_test.py itself is excluded — it's the file that DEFINES this
    # banned list, so the list literal above would always self-match
    # (same class of fix as stockx-scraper's tests.yml excluding
    # smoke_test.py's own fixture strings from the credential-scan grep).
    for path in ROOT.glob("*.py"):
        if path.name == "smoke_test.py":
            continue
        text = path.read_text(encoding="utf-8").lower()
        for word in banned:
            assert word not in text, f"{path.name} contains banned wording {word!r}"
    for path in ROOT.glob("*.md"):
        text = path.read_text(encoding="utf-8").lower()
        for word in banned:
            assert word not in text, f"{path.name} contains banned wording {word!r}"


# --------------------------------------------------------------------------- #
# flight_parser — URL building / sku fingerprint (structural, not live)
# --------------------------------------------------------------------------- #
@check("search_url builds the documented path+query shape (structural — see module docstring on why this is unverified live)")
def _():
    url = fp.search_url(origin="lhr", destination="jfk", depart_date="2026-09-15", return_date="2026-09-22",
                         adults=2, currency="EUR", cabin_class="business", stops="direct", sort="fastest")
    assert url.startswith("https://www.skyscanner.com/transport/flights/LHR/JFK/260915/260922/")
    assert "adults=2" in url and "currency=EUR" in url and "cabinclass=business" in url
    assert "stops=direct" in url and "sort=fastest" in url

    one_way = fp.search_url(origin="LHR", destination="JFK", depart_date="2026-09-15")
    assert "/260915/?" in one_way, "one-way search must omit the return-date path segment"
    assert "stops=" not in one_way and "sort=" not in one_way, "default any/best must not add noise query params"


@check("make_sku is deterministic per itinerary and differs for a different itinerary")
def _():
    kwargs = dict(origin="LHR", destination="JFK", depart_date="2026-09-15", return_date="2026-09-22",
                   airline="Example Air", departure_time="08:35", arrival_time="11:20", cabin_class="economy")
    s1 = fp.make_sku(**kwargs)
    s2 = fp.make_sku(**kwargs)
    assert s1 == s2, "same itinerary must produce the same sku across runs, or diff_runs.py can't track price changes"
    s3 = fp.make_sku(**{**kwargs, "airline": "Other Air"})
    assert s1 != s3, "a different airline on the same route/date is a different itinerary"

    # Same itinerary, different PRICE — sku must be price-independent, or a
    # legitimate price change on the same flight would look like one
    # result disappearing and an unrelated one appearing (see Product's
    # docstring in output_writer.py).
    s_same_flight_diff_price_context = fp.make_sku(**kwargs)
    assert s_same_flight_diff_price_context == s1


# --------------------------------------------------------------------------- #
# flight_parser — DOM fallback + embedded-JSON parsing on SYNTHETIC fixtures
# --------------------------------------------------------------------------- #
_SYNTHETIC_DOM_HTML = """
<html><body>
<div data-testid="result-card">
  <span data-testid="price">$412</span>
  <span data-testid="airline-name">Example Airline</span>
  <span data-testid="departure-time">08:35</span>
  <span data-testid="arrival-time">11:20</span>
  <span data-testid="duration">7h 45m</span>
  <span data-testid="stops">1 stop</span>
  <a href="/transport/flights/lhr/jfk/260915/deeplink1">Select</a>
</div>
<div data-testid="result-card">
  <span data-testid="price">$500</span>
  <span data-testid="airline-name">Other Air</span>
  <a href="/transport/flights/lhr/jfk/260915/deeplink2">Select</a>
</div>
</body></html>
"""


@check("DOM fallback parses a synthetic result-card fixture into two priced itineraries")
def _():
    res = fp.parse_search_results(_SYNTHETIC_DOM_HTML, origin="LHR", destination="JFK", depart_date="2026-09-15",
                                   return_date="2026-09-22")
    assert res.source_used == "dom"
    assert len(res.products) == 2
    assert res.products[0].price == 412.0
    assert res.products[1].price == 500.0
    assert res.products[0].brand == "Example Airline"
    assert res.products[0].category == "flights"
    assert res.products[0].sku != res.products[1].sku


@check("count_result_cards matches the synthetic fixture's card count")
def _():
    assert fp.count_result_cards(_SYNTHETIC_DOM_HTML) == 2
    assert fp.count_result_cards("<html><body>no cards here</body></html>") == 0


def _synthetic_next_data_html(n: int = 2) -> str:
    results = []
    for i in range(n):
        results.append({
            "price": {"amount": 300 + i * 50},
            "legs": [{
                "carriers": [{"name": f"Airline {i}"}],
                "departure": f"0{9 + i}:00",
                "arrival": f"1{2 + i}:00",
                "durationInMinutes": 180 + i * 10,
                "stopCount": i,
            }],
            "deeplink": f"https://www.skyscanner.com/deeplink/{i}",
        })
    next_data = {"props": {"pageProps": {"someQuery": {"results": results}}}}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></body></html>'


@check("embedded __NEXT_DATA__ generic heuristic parses a synthetic itinerary-shaped list")
def _():
    res = fp.parse_search_results(_synthetic_next_data_html(2), origin="LHR", destination="JFK", depart_date="2026-09-15")
    assert res.source_used == "embedded_json"
    assert len(res.products) == 2
    assert res.products[0].price == 300.0
    assert res.products[0].stops == "Direct"
    assert res.products[1].stops == "1 stop"


@check("embedded-JSON path is preferred over DOM fallback when both are present")
def _():
    combined = _synthetic_next_data_html(1).replace("</body>", _SYNTHETIC_DOM_HTML + "</body>")
    res = fp.parse_search_results(combined, origin="LHR", destination="JFK", depart_date="2026-09-15")
    assert res.source_used == "embedded_json", "embedded JSON must win when it yields at least one usable row"


@check("a page with neither shape returns zero products, source_used='none' — not a crash")
def _():
    res = fp.parse_search_results("<html><body>Nothing recognisable here.</body></html>",
                                   origin="LHR", destination="JFK", depart_date="2026-09-15")
    assert res.products == []
    assert res.source_used == "none"


@check("parse_search_json maps a REAL captured web-unified-search body (fixtures/, 2026-09-29) into fully populated rows")
def _():
    data = json.loads((Path(__file__).parent / "fixtures" / "web_unified_search_lhr_jfk_trimmed.json").read_text())
    res = fp.parse_search_json(data, origin="LHR", destination="JFK", depart_date="2026-11-15")
    assert res.source_used == "embedded_json" and len(res.products) == 3
    p = res.products[0]
    assert (p.price, p.currency, p.brand, p.stops) == (322.29, "GBP", "jetBlue", "Direct")
    assert (p.departure_time, p.arrival_time, p.duration) == ("2026-11-15T07:45:00", "2026-11-15T11:00:00", "8h 15m")
    assert p.product_url.startswith("https://www.skyscanner.com/transport_deeplink/")
    assert res.products[1].sku != res.products[2].sku, "codeshare pair (same times, different flight numbers) must not collide"


@check("safe_parse_search_results degrades a parse exception to an empty result, never propagates (mirrors the family's per-worker-page crash fix)")
def _():
    import unittest.mock as mock
    with mock.patch.object(fp, "parse_search_results", side_effect=RuntimeError("boom")):
        res = fp.safe_parse_search_results("<html></html>", origin="LHR", destination="JFK", depart_date="2026-09-15")
    assert res.products == [] and res.source_used == "none"


@check("BOT_CHALLENGE_MARKERS matches the real, captured PerimeterX incident (2026-09-17), not a guess")
def _():
    # Updated once a real skyscanner.com block page existed to check
    # against (see flight_parser.py's module docstring for the full
    # incident) — before that, this asserted the tuple was EMPTY, since no
    # site-specific marker could be honestly claimed as verified yet.
    # Every marker here must appear in the actual captured page, not just
    # be plausible-sounding.
    real_block_page_fragment = (
        '<script src="https://client.px-cloud.net/PXrf8vapwA/main.min.js"></script>'
        '<img src="//js.skyscnr.com/sttc/px/captcha-v2/captcha-01.svg">'
        '<div id="px-captcha">'
    )
    assert fp.BOT_CHALLENGE_MARKERS, "the real incident below should have left at least one marker"
    for marker in fp.BOT_CHALLENGE_MARKERS:
        assert marker in real_block_page_fragment, f"{marker!r} does not match the actual captured incident"
    assert len(captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS) > 0
    # The generic detector alone already caught this exact page (via its
    # own "px-captcha" string) — the site-specific markers above are
    # corroboration, not the only thing standing between this repo and a
    # misreported "empty" run.
    assert any(m in real_block_page_fragment for m in captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS)


# --------------------------------------------------------------------------- #
# captcha_solver — reused verbatim from the family, site-agnostic
# --------------------------------------------------------------------------- #
@check("captcha widget identification on synthetic markup (no real captcha was ever observed on this site)")
def _():
    html = '<div class="g-recaptcha" data-sitekey="abc123"></div>'
    signal = captcha_solver.identify_widget(html)
    assert signal is not None and signal.captcha_type == captcha_solver.CaptchaType.RECAPTCHA_V2
    assert signal.sitekey == "abc123"


@check("solve_when_blocked skips solving when results are already rendered")
def _():
    html = '<div class="g-recaptcha" data-sitekey="abc"></div>' + _SYNTHETIC_DOM_HTML

    class _FakeClient:
        api_key = "x"

    result = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url="https://x", html=html, count_product_links=fp.count_result_cards,
    )
    assert result["action"] == "skipped_products_present"


# --------------------------------------------------------------------------- #
# output_writer contract — identical semantics to stockx-scraper's (see
# output_writer.py's module docstring for why this is intentional, not a
# coincidence — this file was ported, not reinvented).
# --------------------------------------------------------------------------- #
def _mk_product(sku, price=100.0, **kw):
    base = dict(
        sku=sku, source="skyscanner.com", category="flights", title=f"Itinerary {sku}", brand="Example Air",
        price=price, currency="USD", price_source="dom",
        product_url=f"https://www.skyscanner.com/{sku}", image_url=None, scraped_at="2026-09-15T00:00:00Z",
    )
    base.update(kw)
    return output_writer.Product(**base)


@check("merge_pages dedupes by sku, keeps round order, last-write-wins per sku")
def _():
    round1 = [_mk_product("a", price=100), _mk_product("b", price=200)]
    round2 = [_mk_product("b", price=210), _mk_product("c", price=300)]
    merged = output_writer.merge_pages([round1, round2])
    assert [p.sku for p in merged] == ["a", "b", "c"]
    assert merged[1].price == 210


@check("a zero-product run writes NOTHING — not the data file, and not the sidecar either — unless --allow-empty")
def _():
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "out.json")
        Path(out).write_text('[{"sku": "previous-good-run"}]', encoding="utf-8")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="https://x",
            pages_requested=1, pages_completed=0, failed_pages=[], blocked=False,
            remote_api_error=False, allow_empty=False, started_at=0,
        )
        assert code == output_writer.EXIT_ZERO_PRODUCTS
        assert json.loads(Path(out).read_text()) == [{"sku": "previous-good-run"}], \
            "an empty run must NEVER overwrite a previous good output"
        assert not Path(out + ".meta.json").exists(), \
            "an empty run must NOT write a sidecar beside a previous good output"


@check("a BLOCKED run reports EXIT_BLOCKED even with products present AND --allow-empty set (the exact precedence bug stockx-scraper's audit found)")
def _():
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "out.json")
        p1 = _mk_product("a")
        code = output_writer.finish_run(
            products=[p1], out_path=out, fmt="json", engine="test", url="https://x",
            pages_requested=5, pages_completed=5, failed_pages=[], blocked=True,
            remote_api_error=False, allow_empty=True, started_at=0,
        )
        assert code == output_writer.EXIT_BLOCKED, (
            "blocked must win even when products are present and --allow-empty is set — "
            "this is the outcome-precedence fix ported from stockx-scraper's finish_run(), "
            "tested here so it can never silently regress in THIS repo either"
        )


@check("a remote-API-error run with zero products also writes nothing — data OR sidecar")
def _():
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "out.json")
        Path(out).write_text('[{"sku": "previous-good-run"}]', encoding="utf-8")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="https://x",
            pages_requested=1, pages_completed=0, failed_pages=[], blocked=False,
            remote_api_error=True, allow_empty=False, started_at=0,
        )
        assert code == output_writer.EXIT_REMOTE_API_ERROR
        assert json.loads(Path(out).read_text()) == [{"sku": "previous-good-run"}]
        assert not Path(out + ".meta.json").exists()


@check("empty CSV still carries its header")
def _():
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "out.csv")
        output_writer.write_csv([], out)
        with open(out, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows == [output_writer.PRODUCT_FIELD_NAMES]


@check("exit codes are distinct integers 0-6")
def _():
    codes = [
        output_writer.EXIT_OK, output_writer.EXIT_CRASH, output_writer.EXIT_BAD_USAGE,
        output_writer.EXIT_BLOCKED, output_writer.EXIT_ZERO_PRODUCTS,
        output_writer.EXIT_REMOTE_API_ERROR, output_writer.EXIT_PARTIAL,
    ]
    assert codes == sorted(set(codes)) == list(range(7))


# --------------------------------------------------------------------------- #
# proxy_pool / redact_credentials — reused verbatim from the family
# --------------------------------------------------------------------------- #
@check("proxy formats parse; credentials never appear in the masked/server-only strings")
def _():
    p1 = proxy_pool.parse_proxy_line("http://alice:s3cr3t@1.2.3.4:8080")
    assert p1.host == "1.2.3.4" and p1.port == 8080 and p1.login == "alice" and p1.password == "s3cr3t"
    p2 = proxy_pool.parse_proxy_line("1.2.3.4:8080:bob:hunter2")
    assert p2.login == "bob" and p2.password == "hunter2"
    p3 = proxy_pool.parse_proxy_line("1.2.3.4:8080")
    assert p3.login is None

    assert "s3cr3t" not in p1.masked()
    assert "s3cr3t" not in p1.server_only()
    assert "hunter2" not in p2.server_only()
    assert "***" in p1.masked()


@check("proxy_pool: dead-error classification, block after N failures, success resets it")
def _():
    p = proxy_pool.parse_proxy_line("1.2.3.4:8080")
    pool = proxy_pool.ProxyPool([p], block_retries=2)
    assert proxy_pool.is_proxy_dead_error("net::ERR_PROXY_CONNECTION_FAILED at https://x") is True
    assert proxy_pool.is_proxy_dead_error("Timeout 30000ms exceeded") is False
    pool.report_failure(p, dead=True)
    assert pool.next() is not None, "one failure must not kill the only proxy in the pool"
    pool.report_failure(p, dead=True)
    assert pool.next() is None, "block_retries=2 consecutive dead-errors should mark it dead"


@check("worker_view rotates each worker to a different starting offset (no shared cursor)")
def _():
    proxies = [proxy_pool.parse_proxy_line(f"{i}.0.0.1:80") for i in range(3)]
    pool = proxy_pool.ProxyPool(proxies)
    v0 = pool.worker_view(0, 3).next().host
    v1 = pool.worker_view(1, 3).next().host
    v2 = pool.worker_view(2, 3).next().host
    assert len({v0, v1, v2}) == 3


@check("redact_credentials scrubs URL userinfo AND key/token params, globally")
def _():
    message = (
        "connect_over_cdp failed: ws://scanner123-zone-scraping_browser:secretpass@cb.2captcha.com:9222 "
        "Call log:\n - ws://scanner123-zone-scraping_browser:secretpass@cb.2captcha.com:9222\n"
        "  clientKey=abcdEF123 also leaked here"
    )
    redacted = proxy_pool.redact_credentials(message)
    assert "secretpass" not in redacted
    assert "abcdEF123" not in redacted
    assert redacted.count("***:***@") == 2, "must scrub EVERY occurrence, not just the first"


@check("a failed --cdp-endpoint connection never leaks its credential into the raised error (playwright, puppeteer)")
def _():
    import asyncio
    import unittest.mock as mock

    bad_endpoint = "ws://scanner123-zone-scraping_browser:secretpass@cb.2captcha.com:9222"

    class _FakePW:
        class chromium:
            @staticmethod
            async def connect_over_cdp(_endpoint):
                raise RuntimeError(f"could not connect: {_endpoint}")

    async def _run_pw():
        try:
            await playwright_scraper._connect_over_cdp(_FakePW(), bad_endpoint)
            raise AssertionError("expected a RuntimeError")
        except RuntimeError as exc:
            assert "secretpass" not in str(exc)

    asyncio.run(_run_pw())

    async def _fake_connect(browserWSEndpoint, **_kw):
        raise RuntimeError(f"could not connect: {browserWSEndpoint}")

    async def _run_pptr():
        with mock.patch.object(puppeteer_scraper, "pyppeteer_connect", _fake_connect):
            try:
                await puppeteer_scraper._launch(headless=True, proxy=None, cdp_endpoint=bad_endpoint)
                raise AssertionError("expected a RuntimeError")
            except RuntimeError as exc:
                assert "secretpass" not in str(exc)

    asyncio.run(_run_pptr())


@check("a failed fingerprint/random request never leaks the key into the raised error")
def _():
    import unittest.mock as mock

    import fingerprint_client
    import scraper_api_client

    import requests as _requests

    client = scraper_api_client.TwoCaptchaClient(api_key="topsecretkey123")
    with mock.patch("scraper_api_client.requests.get",
                     side_effect=_requests.exceptions.RequestException("boom key=topsecretkey123")):
        try:
            client.random_fingerprint()
            raise AssertionError("expected TwoCaptchaError")
        except scraper_api_client.TwoCaptchaError as exc:
            assert "topsecretkey123" not in str(exc)
    profile = fingerprint_client.fetch_fingerprint(client)
    assert profile is None, "a failed fingerprint fetch must degrade to None, never crash the caller"


# --------------------------------------------------------------------------- #
# diff_runs — reused verbatim; operates on the family-common sku/price/
# price_source/title columns that this repo's Product also carries.
# --------------------------------------------------------------------------- #
@check("diff_runs refuses to compare a non-complete run")
def _():
    with tempfile.TemporaryDirectory() as d:
        old = Path(d) / "old.json"
        new = Path(d) / "new.json"
        old.write_text("[]", encoding="utf-8")
        new.write_text("[]", encoding="utf-8")
        (Path(d) / "old.json.meta.json").write_text(json.dumps({"status": "partial"}), encoding="utf-8")
        (Path(d) / "new.json.meta.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
        try:
            diff_runs.diff(str(old), str(new))
            raise AssertionError("expected a refusal for a non-complete run")
        except SystemExit as exc:
            assert "refusing to diff" in str(exc)


@check("diff_runs separates a real price change from a price_source change")
def _():
    with tempfile.TemporaryDirectory() as d:
        old = Path(d) / "old.json"
        new = Path(d) / "new.json"
        old.write_text(json.dumps([
            {"sku": "a", "price": 100, "price_source": "dom", "title": "t"},
            {"sku": "b", "price": 200, "price_source": "dom", "title": "t"},
        ]), encoding="utf-8")
        new.write_text(json.dumps([
            {"sku": "a", "price": 150, "price_source": "dom", "title": "t"},              # real change, same source
            {"sku": "b", "price": 205, "price_source": "embedded_json", "title": "t"},    # price AND source both differ
        ]), encoding="utf-8")
        (Path(d) / "old.json.meta.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
        (Path(d) / "new.json.meta.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
        result = diff_runs.diff(str(old), str(new))
        assert [c["sku"] for c in result["changed"]] == ["a"]
        assert [c["sku"] for c in result["source_changed"]] == ["b"]


# --------------------------------------------------------------------------- #
# env_config
# --------------------------------------------------------------------------- #
@check("ENV_KEYS matches .env.example in both directions")
def _():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", example, re.MULTILINE))
    assert documented == set(env_config.ENV_KEYS), (
        f"drift between .env.example and ENV_KEYS: "
        f"in .env.example only={documented - set(env_config.ENV_KEYS)}, "
        f"in ENV_KEYS only={set(env_config.ENV_KEYS) - documented}"
    )


@check("env_config never maps a variable onto a flag with a non-empty default")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        parser = mod.build_arg_parser()
        defaults = {a.dest: a.default for a in parser._actions}
        for dest in env_config.ENV_KEYS.values():
            if dest in defaults:
                assert defaults[dest] in (None, ""), (
                    f"{mod.__name__}: --{dest} has a non-empty default "
                    f"({defaults[dest]!r}) — env_config.apply_env() would "
                    f"never be able to fill it, making the env var silently inert"
                )


@check("apply_env treats a pasted {...} example fragment as unset, not as a real credential")
def _():
    import argparse

    braced_value = "ws://{login}-zone-scraping_browser-country-{cc}-pid-{profileId}:{password}@cb.2captcha.com:9222"
    prev = os.environ.pop("SKYSCANNER_CDP_ENDPOINT", None)
    os.environ["SKYSCANNER_CDP_ENDPOINT"] = braced_value
    try:
        args = argparse.Namespace(cdp_endpoint=None)
        env_config.apply_env(args, dotenv_path=str(ROOT / "_no_such_file.env"))
        assert args.cdp_endpoint is None
    finally:
        del os.environ["SKYSCANNER_CDP_ENDPOINT"]
        if prev is not None:
            os.environ["SKYSCANNER_CDP_ENDPOINT"] = prev

    prev2 = os.environ.pop("SKYSCANNER_URL", None)
    os.environ["SKYSCANNER_URL"] = "https://www.skyscanner.com/transport/flights/lhr/jfk/260915/"
    try:
        args2 = argparse.Namespace(url=None)
        env_config.apply_env(args2, dotenv_path=str(ROOT / "_no_such_file.env"))
        assert args2.url == "https://www.skyscanner.com/transport/flights/lhr/jfk/260915/"
    finally:
        del os.environ["SKYSCANNER_URL"]
        if prev2 is not None:
            os.environ["SKYSCANNER_URL"] = prev2


# --------------------------------------------------------------------------- #
# CLI validation — the family's --pages-0 audit fix, adapted to this
# repo's own positive-int flags (--adults/--max-results/--max-scrolls/
# --stall-rounds)
# --------------------------------------------------------------------------- #
@check("each engine rejects --max-results 0 / negative --adults with a clean argparse error, not a silent misleading run")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        parser = mod.build_arg_parser()
        for bad_args in (["--max-results", "0"], ["--adults", "-1"], ["--max-scrolls", "0"], ["--stall-rounds", "0"]):
            try:
                parser.parse_args(bad_args)
                raise AssertionError(f"{mod.__name__}: {bad_args} should have been rejected")
            except SystemExit:
                pass  # argparse's own clean rejection — the expected outcome


@check("--url resolves standalone; --origin/--destination/--depart-date builds the same shape search_url() would")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        args = mod.build_arg_parser().parse_args(["--url", "https://example.invalid/x"])
        assert mod._resolve_start_url(args) == "https://example.invalid/x"

        args2 = mod.build_arg_parser().parse_args(["--origin", "LHR", "--destination", "JFK", "--depart-date", "2026-09-15"])
        resolved = mod._resolve_start_url(args2)
        assert resolved == fp.search_url(origin="LHR", destination="JFK", depart_date="2026-09-15",
                                          return_date=None, adults=1, currency="USD", cabin_class="economy",
                                          stops="any", sort="best")

        args3 = mod.build_arg_parser().parse_args([])
        assert mod._resolve_start_url(args3) is None, "no --url and no origin/destination/depart-date must resolve to nothing, not a broken URL"


# --------------------------------------------------------------------------- #
# sample_output.{json,csv} — schema check ONLY. Unlike stockx-scraper's
# sample_output (drawn from a REAL capture), this repo has no live capture
# to draw from (see module docstring) — sample_output here is a clearly
# fictional illustration of the schema, and is documented as such in
# README, not asserted to be "non-fabricated" the way a real capture could be.
# --------------------------------------------------------------------------- #
@check("sample_output.json/csv columns match the Product dataclass")
def _():
    sample_json = json.loads((ROOT / "sample_output.json").read_text(encoding="utf-8"))
    assert sample_json, "sample_output.json must not be empty"
    assert set(sample_json[0].keys()) == set(output_writer.PRODUCT_FIELD_NAMES)

    with (ROOT / "sample_output.csv").open(newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == output_writer.PRODUCT_FIELD_NAMES


# --------------------------------------------------------------------------- #
# CLAUDE.md §17's "five checks worth stealing" — the real family's own
# pre-publication audit checklist, ported here rather than re-copied blind:
# each one found a real defect somewhere in that family before it was
# written down as a standing check. See CHANGELOG for what each one found
# HERE (a real, live bug in one case — the ProxyParseError case below).
# --------------------------------------------------------------------------- #
@check("every engine's _maybe_solve_captcha binds against captcha_solver.solve_when_blocked's real signature (CLAUDE.md §17 check #1)")
def _():
    import inspect
    # Sanity: the callee itself accepts the shape every engine below claims
    # to call it with.
    inspect.signature(captcha_solver.solve_when_blocked).bind(
        client=None, page_url="https://example.invalid", html="<html></html>",
        count_product_links=lambda h: 0, extra_markers=(), proxyless=True, min_score=0.3,
    )
    # `classify(html, status, url)` took `status` positionally while two of
    # three engines called it `classify(html, url=…)` is the exact incident
    # this check is modeled on — both crashed on their FIRST live fetch,
    # invisible to import/--help/compileall and every offline assertion
    # that doesn't actually call the function the way a live run does.
    # Binding against each engine's OWN `_maybe_solve_captcha` signature (not
    # just calling captcha_solver directly) is what would have caught a
    # typo introduced in one engine's copy of the wiring but not the other
    # two's — e.g. the --min-score plumbing added to all three engines in
    # the same change.
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        sig = inspect.signature(mod._maybe_solve_captcha)
        sig.bind(html="<html></html>", url="https://example.invalid", client=None, policy="off", min_score=0.3)


@check("all three engines expose the identical --flag set (CLAUDE.md §17 check #2)")
def _():
    def flag_set(mod):
        return {opt for a in mod.build_arg_parser()._actions for opt in a.option_strings if opt.startswith("--")}

    pw, se, pu = flag_set(playwright_scraper), flag_set(selenium_scraper), flag_set(puppeteer_scraper)
    all_engines = pw | se | pu
    for name, flags in (("playwright_scraper", pw), ("selenium_scraper", se), ("puppeteer_scraper", pu)):
        missing = all_engines - flags
        # selenium_scraper legitimately has no per-site-fingerprint story of
        # its own to diverge on here, so no engine-specific exemption exists
        # in this repo (unlike stockx-scraper's --category, which is N/A to
        # a flight search and was never added to any of the three engines
        # in the first place, so it never shows up in `all_engines`).
        assert not missing, f"{name} is missing {sorted(missing)} that (an)other engine(s) define — flag sets have drifted apart"


@check("--proxy-api/--min-score are actually READ, not just defined — a policy flag with no consumer is a documented feature that does nothing (CLAUDE.md §17 check #2/'policy constant nothing reads')")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        src = (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
        assert "args.captcha_api" in src, f"{mod.__name__}: --captcha-api is defined but never read (dead flag)"
        assert "args.min_score" in src, f"{mod.__name__}: --min-score is defined but never read (dead flag)"
    task = captcha_solver._task_payload(
        captcha_solver.CaptchaSignal(captcha_solver.CaptchaType.RECAPTCHA_V3, sitekey="site_key_123"),
        "https://example.invalid", proxyless=True, min_score=0.7,
    )
    assert task["minScore"] == 0.7, "min_score must reach the actual createTask payload's minScore field"
    # A non-v3 task type must NOT carry minScore — 2Captcha's own
    # TurnstileTask/RecaptchaV2Task/HCaptchaTask payloads don't document
    # that field, and sending an extra, unexpected key to a billed API
    # neither of us can inspect live is exactly the kind of unverified
    # guess CLAUDE.md §8 ("never present a guess as a fact") warns against.
    turnstile_task = captcha_solver._task_payload(
        captcha_solver.CaptchaSignal(captcha_solver.CaptchaType.CLOUDFLARE_TURNSTILE, sitekey="site_key_123"),
        "https://example.invalid", proxyless=True, min_score=0.7,
    )
    assert "minScore" not in turnstile_task


@check("--captcha-api actually redirects TwoCaptchaClient's REST calls (CLAUDE.md §17 check #2/'policy constant nothing reads')")
def _():
    client_default = scraper_api_client.TwoCaptchaClient("fakekey")
    assert client_default.api_base == scraper_api_client.API_BASE
    client_override = scraper_api_client.TwoCaptchaClient("fakekey", api_base="http://127.0.0.1:1/mock")
    assert client_override.api_base == "http://127.0.0.1:1/mock"
    try:
        client_override.get_balance()
        raise AssertionError("a mock base URL with nothing listening must not succeed")
    except scraper_api_client.TwoCaptchaError:
        pass  # expected: connection failure to the OVERRIDDEN host, not the real api.2captcha.com


@check(".env.example round-trips through the real loader with every credential reading as unset (CLAUDE.md §17 check #3)")
def _():
    import argparse
    example_path = ROOT / ".env.example"
    tmp_env = ROOT / "_smoke_roundtrip.env"
    try:
        tmp_env.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
        args = argparse.Namespace(**{dest: None for dest in env_config.ENV_KEYS.values()})
        for env_key in env_config.ENV_KEYS:
            os.environ.pop(env_key, None)  # isolate from whatever this shell already exports
        env_config.apply_env(args, dotenv_path=str(tmp_env))
        for env_key, dest in env_config.ENV_KEYS.items():
            assert getattr(args, dest) is None, (
                f"a freshly copied .env.example must read {env_key} as UNSET, not as a real "
                f"credential — got {getattr(args, dest)!r}"
            )
    finally:
        tmp_env.unlink(missing_ok=True)


def asyncio_run_maybe(mod, args):
    """playwright_scraper.run()/puppeteer_scraper.run() are coroutines;
    selenium_scraper.run() is plain sync. One helper, used only by the
    check below, so the check itself doesn't need to know which engine is
    async — same reasoning as CLAUDE.md §6 on keeping engine-dialect
    differences out of shared/cross-engine code."""
    import asyncio
    import inspect as _inspect
    result = mod.run(args)
    if _inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


@check("a malformed --proxy is EXIT_BAD_USAGE (2), not an uncaught crash (CLAUDE.md §17 check #5 / dead-name audit — found live in this repo AND in stockx-scraper)")
def _():
    # Regression test for a real bug found by this session's own audit:
    # `proxy_pool.ProxyParseError` was raised by `load_proxies()` in every
    # engine's `run()`, before that function's own try/except even starts,
    # so it reached the top level as a raw traceback — Python's default
    # exit code for an uncaught exception (1) happened to collide with this
    # scraper's own EXIT_CRASH (also 1), which made it easy to miss in a
    # quick smoke check, but it never went through the intended
    # EXIT_CRASH path (no log.exception, no clean message) and, more to
    # the point, a malformed proxy STRING is a usage mistake
    # (EXIT_BAD_USAGE=2), not evidence the scraper itself crashed.
    # Confirmed live: the identical, still-unfixed bug exists in
    # stockx-scraper's playwright_scraper.py too (left there per the
    # standing instruction not to touch that repo).
    # Every engine checks "is my driver library even installed" BEFORE any
    # CLI-usage validation (including load_proxies()) — deliberate,
    # consistent across all three, and already covered by "engines import
    # cleanly regardless of installed drivers" above. On a machine with
    # none/some of the three driver packages installed, THIS check can
    # only exercise the ones that are — skipping the rest is honest,
    # rather than asserting something that engine can't reach yet.
    _IMPORT_ERROR_ATTR = {
        "playwright_scraper": "_PLAYWRIGHT_IMPORT_ERROR",
        "selenium_scraper": "_SELENIUM_IMPORT_ERROR",
        "puppeteer_scraper": "_PYPPETEER_IMPORT_ERROR",
    }
    exercised = 0
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        if getattr(mod, _IMPORT_ERROR_ATTR[mod.__name__], None) is not None:
            continue
        exercised += 1
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            args = mod.build_arg_parser().parse_args([
                "--origin", "LHR", "--destination", "JFK", "--depart-date", "2026-10-15",
                "--proxy", "not a proxy!!", "--out", out,
            ])
            code = asyncio_run_maybe(mod, args)
            assert code == output_writer.EXIT_BAD_USAGE, (
                f"{mod.__name__}: a malformed --proxy must exit {output_writer.EXIT_BAD_USAGE} "
                f"(bad usage), got {code}"
            )
            assert not Path(out).exists(), f"{mod.__name__}: a bad-usage run must never write output"
    # If NONE of the three drivers are installed, this check hasn't
    # actually verified anything — that's a real gap in coverage on this
    # machine, not a pass, so say so rather than reporting a silent green.
    assert exercised > 0, "no engine's driver is installed — this check ran against zero of the three engines"


# --------------------------------------------------------------------------- #
# PerimeterX gap mitigations (2026-09-20): honest vendor naming plus
# --cdp-block-retries / --cookies-file, implemented across all three engines
# per CLAUDE.md §4's flag-parity rule. See captcha_solver.py's module
# docstring and CHANGELOG for the real incident this responds to — a live
# skyscanner.com PerimeterX challenge that 2Captcha confirmed it has no
# automated solve for at all, even through a real Scraping Browser session.
# --------------------------------------------------------------------------- #
@check("identify_unsupported_vendor names PerimeterX/DataDome/a bare Cloudflare challenge, and returns None for a solvable widget or a clean page")
def _():
    px_fragment = '<div id="px-captcha">Are you a human?</div>'
    assert captcha_solver.identify_unsupported_vendor(px_fragment) == "perimeterx"
    dd_fragment = '<script src="https://ct.datadome.co/tags.js"></script>'
    assert captcha_solver.identify_unsupported_vendor(dd_fragment) == "datadome"
    cf_fragment = '<title>Just a moment...</title><div class="cf-browser-verification">'
    assert captcha_solver.identify_unsupported_vendor(cf_fragment) == "cloudflare_managed_challenge"
    solvable = '<div class="g-recaptcha" data-sitekey="abc123"></div>'
    assert captcha_solver.identify_unsupported_vendor(solvable) is None
    assert captcha_solver.identify_unsupported_vendor("<html><body>clean page</body></html>") is None


@check("solve_when_blocked reports action='unsupported_vendor' (not the vague 'detected_unidentified_widget') for a real PerimeterX block with zero products")
def _():
    html = '<html><body><div id="px-captcha">Are you a human?</div></body></html>'

    class _FakeClient:
        api_key = "x"

    result = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url="https://x", html=html, count_product_links=fp.count_result_cards,
    )
    assert result == {"action": "unsupported_vendor", "vendor": "perimeterx"}

    # A genuinely unidentifiable marker (no known vendor's substring
    # present) must still fall back to the old, honestly-vague action —
    # this must never regress into claiming a vendor it can't actually name.
    unknown_widget_html = '<html><body><div class="g-recaptcha"></div></body></html>'  # no data-sitekey
    result2 = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url="https://x", html=unknown_widget_html, count_product_links=fp.count_result_cards,
    )
    assert result2["action"] == "detected_unidentified_widget"


@check("all three engines' _maybe_solve_captcha pass the new 'unsupported_vendor' action through without crashing (mirrors CLAUDE.md §17 check #1)")
def _():
    import asyncio
    import inspect as _inspect
    import unittest.mock as mock

    class _FakeClient:
        api_key = "x"

    fake_result = {"action": "unsupported_vendor", "vendor": "perimeterx"}
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        with mock.patch.object(mod, "solve_when_blocked", return_value=fake_result):
            outcome = mod._maybe_solve_captcha(
                html="<html></html>", url="https://example.invalid", client=_FakeClient(),
                policy="always", min_score=0.3,
            )
            if _inspect.iscoroutine(outcome):
                outcome = asyncio.run(outcome)
            assert outcome == fake_result, f"{mod.__name__}: _maybe_solve_captcha must pass the action through unchanged"


@check("EVERY function that takes an 'autosolve' parameter actually calls the Captcha.setAutoSolve helper somewhere in its body — playwright/puppeteer only, selenium is exempt (cannot authenticate --cdp-endpoint at all, CLAUDE.md §6). Ported from shein-scraper 2026-09-22 after a real gap was found there (a product-page scrape function took 'autosolve' but never used it) — this repo's own engines currently have only ONE autosolve-taking function each (no separate product-detail path the way shein/flippa have), so this test is a forward guard against that same class of regression if a second entry point is ever added, not a fix for a bug found here.")
def _():
    import ast as _ast

    HELPER_NAME = "_enable_scraping_browser_auto_solve"
    for path in ("playwright_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        tree = _ast.parse(src, filename=path)
        checked_any = False
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            arg_names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            if "autosolve" not in arg_names:
                continue
            checked_any = True
            calls_helper = any(
                isinstance(n, _ast.Call)
                and (
                    (isinstance(n.func, _ast.Name) and n.func.id == HELPER_NAME)
                    or (isinstance(n.func, _ast.Attribute) and n.func.attr == HELPER_NAME)
                )
                for n in _ast.walk(node)
            )
            assert calls_helper, (
                f"{path}: {node.name}() takes an 'autosolve' parameter but never calls "
                f"{HELPER_NAME}() — captcha auto-solve would silently never be armed on "
                f"this page/navigation path when --cdp-endpoint + --solve-captcha are set."
            )
        assert checked_any, f"{path}: expected at least one function with an 'autosolve' parameter (test itself may be stale)"


@check("--cdp-block-retries / --cookies-file are defined on all three engines with matching defaults, and are actually READ (not dead flags)")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        parser = mod.build_arg_parser()
        actions = {a.dest: a for a in parser._actions}
        assert "cdp_block_retries" in actions, f"{mod.__name__}: missing --cdp-block-retries"
        assert actions["cdp_block_retries"].default == 2
        assert "cookies_file" in actions, f"{mod.__name__}: missing --cookies-file"
        assert actions["cookies_file"].default is None
        src = (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
        assert "args.cdp_block_retries" in src, f"{mod.__name__}: --cdp-block-retries is defined but never read (dead flag)"
        assert "args.cookies_file" in src, f"{mod.__name__}: --cookies-file is defined but never read (dead flag)"


@check("_load_cookies_file validates its JSON shape identically across all three engines (None passthrough, valid array accepted, bad shapes/missing file rejected)")
def _():
    with tempfile.TemporaryDirectory() as d:
        good = Path(d) / "cookies.json"
        good.write_text(json.dumps([{"name": "a", "value": "b", "domain": ".skyscanner.com"}]), encoding="utf-8")
        not_json = Path(d) / "bad.json"
        not_json.write_text("{not valid json", encoding="utf-8")
        not_array = Path(d) / "not_array.json"
        not_array.write_text(json.dumps({"name": "a", "value": "b"}), encoding="utf-8")
        not_dicts = Path(d) / "not_dicts.json"
        not_dicts.write_text(json.dumps(["a", "b"]), encoding="utf-8")

        for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
            assert mod._load_cookies_file(None) is None
            loaded = mod._load_cookies_file(str(good))
            assert loaded == [{"name": "a", "value": "b", "domain": ".skyscanner.com"}]
            for bad_path in (str(not_json), str(not_array), str(not_dicts)):
                try:
                    mod._load_cookies_file(bad_path)
                    raise AssertionError(f"{mod.__name__}: {bad_path} should have been rejected")
                except ValueError:
                    pass
            try:
                mod._load_cookies_file(str(Path(d) / "does_not_exist.json"))
                raise AssertionError(f"{mod.__name__}: a missing --cookies-file path should have been rejected")
            except ValueError:
                pass


@check("a malformed --cookies-file is EXIT_BAD_USAGE (2), not an uncaught crash — same class of fix as the malformed --proxy check above")
def _():
    _IMPORT_ERROR_ATTR = {
        "playwright_scraper": "_PLAYWRIGHT_IMPORT_ERROR",
        "selenium_scraper": "_SELENIUM_IMPORT_ERROR",
        "puppeteer_scraper": "_PYPPETEER_IMPORT_ERROR",
    }
    exercised = 0
    with tempfile.TemporaryDirectory() as d:
        bad_cookies = Path(d) / "bad_cookies.json"
        bad_cookies.write_text("{not valid json", encoding="utf-8")
        for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
            if getattr(mod, _IMPORT_ERROR_ATTR[mod.__name__], None) is not None:
                continue
            exercised += 1
            out = str(Path(d) / f"out_{mod.__name__}.json")
            args = mod.build_arg_parser().parse_args([
                "--origin", "LHR", "--destination", "JFK", "--depart-date", "2026-10-15",
                "--cookies-file", str(bad_cookies), "--out", out,
            ])
            code = asyncio_run_maybe(mod, args)
            assert code == output_writer.EXIT_BAD_USAGE, (
                f"{mod.__name__}: a malformed --cookies-file must exit {output_writer.EXIT_BAD_USAGE}, got {code}"
            )
            assert not Path(out).exists(), f"{mod.__name__}: a bad --cookies-file run must never write output"
    assert exercised > 0, "no engine's driver is installed — this check ran against zero of the three engines"


def run() -> int:
    """All @check-decorated functions above already ran at import time
    (that's the point — see the `check()` docstring) and self-registered
    into RESULTS. This just reports them."""
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"smoke_test: {passed}/{len(RESULTS)} checks passed")
    for name, detail in failed:
        print(f"  FAIL: {name}\n        {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())

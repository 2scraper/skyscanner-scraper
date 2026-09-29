#!/usr/bin/env python3
"""Assert a canary run (CLAUDE.md §11): exit code, status, a floor, price and
currency coverage, and that rows came from the search-API XHR.

Usage: canary_assert.py <out.json> <exit_code>. Exit 0 = pass, 1 = fail, with
a GitHub ::error:: line naming the reason.

Floors are set well under what live runs returned on 2026-09-29 (LHR→JFK:
385–389 itineraries, 100% priced, 100% with a currency), so a normal
day-to-day change in the route does not trip them but a broken parser does.
"""
import json
import sys
from pathlib import Path

MIN_ITINERARIES = 50
MIN_PRICED_SHARE = 0.95
MIN_CURRENCY_SHARE = 0.95

EXIT_MEANING = {1: "crash", 2: "bad usage", 3: "blocked", 4: "zero itineraries",
                5: "content never obtained", 6: "partial"}


def fail(msg: str) -> int:
    print(f"::error::canary: {msg}")
    return 1


def main(out: str, code: str) -> int:
    code = int(code)
    if code != 0:
        return fail(f"exit {code} ({EXIT_MEANING.get(code, 'unknown')}) — see the run log and uploaded artefacts")
    meta_path = Path(out + ".meta.json")
    if not meta_path.exists():
        return fail("exit 0 but no sidecar was written")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    rows = json.loads(Path(out).read_text(encoding="utf-8"))
    print(json.dumps({k: meta.get(k) for k in (
        "status", "stop_reason", "product_count", "itineraries_available",
        "capped_by_max_results", "search_api_status", "results_source")}, indent=2))
    if meta.get("status") != "complete":
        return fail(f"status is {meta.get('status')!r}, not 'complete'")
    if meta.get("results_source") != "search_api":
        return fail(f"rows came from {meta.get('results_source')!r}, not the search-API XHR — capture broke?")
    if len(rows) < MIN_ITINERARIES:
        return fail(f"{len(rows)} itineraries, floor is {MIN_ITINERARIES}")
    priced = sum(r.get("price") is not None for r in rows) / len(rows)
    with_currency = sum(bool(r.get("currency")) for r in rows) / len(rows)
    if priced < MIN_PRICED_SHARE:
        return fail(f"only {priced:.0%} of rows have a price")
    if with_currency < MIN_CURRENCY_SHARE:
        return fail(f"only {with_currency:.0%} of rows have a currency")
    print(f"canary OK: {len(rows)} itineraries, {priced:.0%} priced, {with_currency:.0%} with currency, "
          f"{meta['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))

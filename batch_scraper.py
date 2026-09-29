#!/usr/bin/env python3
"""batch_scraper.py — many skyscanner searches in one go, with a fallback.

Per route (one line of --routes-file), in order:

  1. LOCAL: your own Chrome over `--local-cdp` (default
     http://127.0.0.1:9222), in its persistent profile, with
     `--wait-for-human`. This is the path verified live (2026-09-29): a
     person clears PerimeterX once, later searches in that profile are not
     challenged. If nothing listens on the port, Chrome is started with that
     port and `--chrome-profile` (disable with --no-launch-chrome).
  2. 2CAPTCHA fallback: if the local attempt ends blocked / remote-API error
     / crashed, or local Chrome is unavailable, the same search runs through
     whatever 2Captcha setup `.env` provides (SKYSCANNER_CDP_ENDPOINT = the
     Scraping Browser, with --cdp-block-retries; else SKYSCANNER_PROXY).
     Honest expectation: against PerimeterX this path has been blocked in
     every live test so far — it exists so a run still gets a chance when
     no local browser/person is around, and says clearly when it failed.

An EMPTY result (exit 4) is taken at face value and never retried, and
does not count as a failed route.

Output goes through the family's finish_run(): one merged file (deduped by
sku, in route order) plus `<out>.meta.json`, whose `failed_pages` are the
failed ROUTE numbers and whose `per_route` records each route's exit code,
path and attempts. A batch where no route produced a row writes nothing
(unless --allow-empty), exactly like a failed single run.
`--scraper-api` is not used as a fallback: it returns HTML only, and the
flight list arrives over the `web-unified-search` XHR it cannot see.

Routes file: CSV with a header. Required columns origin, destination,
depart_date; optional return_date, adults, cabin_class, stops, sort,
currency. Blank lines and lines starting with # are ignored.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import env_config
import playwright_scraper
from output_writer import (
    EXIT_BAD_USAGE, EXIT_BLOCKED, EXIT_CRASH, EXIT_OK, EXIT_PARTIAL,
    EXIT_REMOTE_API_ERROR, EXIT_ZERO_PRODUCTS, Product, finish_run, merge_pages,
)

log = logging.getLogger("batch_scraper")

REQUIRED_COLUMNS = ("origin", "destination", "depart_date")
OPTIONAL_COLUMNS = ("return_date", "adults", "cabin_class", "stops", "sort", "currency")
# Outcomes worth a second attempt through 2Captcha. EXIT_ZERO_PRODUCTS is
# deliberately absent: a genuinely empty search stays empty.
FALLBACK_ON = (EXIT_BLOCKED, EXIT_REMOTE_API_ERROR, EXIT_CRASH)

_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)


def load_routes(path: str) -> List[Dict[str, str]]:
    """Parse --routes-file. Raises ValueError with the line number on a bad row."""
    with open(path, encoding="utf-8", newline="") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        raise ValueError(f"{path}: empty routes file")
    header = [h.strip() for h in reader.fieldnames]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ValueError(f"{path}: header is missing required column(s): {', '.join(missing)}")
    unknown = [c for c in header if c not in REQUIRED_COLUMNS + OPTIONAL_COLUMNS]
    if unknown:
        raise ValueError(f"{path}: unknown column(s): {', '.join(unknown)}")
    routes = []
    for n, row in enumerate(reader, start=2):
        route = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        empty = [c for c in REQUIRED_COLUMNS if not route.get(c)]
        if empty:
            raise ValueError(f"{path}: data row {n}: empty {', '.join(empty)}")
        routes.append({k: v for k, v in route.items() if v})
    if not routes:
        raise ValueError(f"{path}: no routes")
    return routes


def route_label(route: Dict[str, str]) -> str:
    label = f"{route['origin']}-{route['destination']} {route['depart_date']}"
    return label + (f" / {route['return_date']}" if route.get("return_date") else "")


def engine_argv(route: Dict[str, str], *, out: str, max_results: int) -> List[str]:
    argv = ["--out", out, "--format", "json", "--max-results", str(max_results)]
    for key, value in route.items():
        argv += ["--" + key.replace("_", "-"), value]
    return argv


def local_cdp_alive(endpoint: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 — anything here just means "not reachable"
        return False


def launch_local_chrome(endpoint: str, profile: str, wait_s: float = 15.0) -> bool:
    """Start Chrome with a debugging port and persistent profile; True once it answers."""
    binary = os.environ.get("CHROME_BIN") or next(
        (c for c in _CHROME_CANDIDATES if Path(c).exists() or shutil.which(c)), None)
    if not binary:
        log.warning("No Chrome/Chromium binary found (set CHROME_BIN) — cannot start local Chrome.")
        return False
    port = endpoint.rstrip("/").rsplit(":", 1)[-1]
    Path(profile).expanduser().mkdir(parents=True, exist_ok=True)
    log.info("Starting local Chrome on port %s with profile %s", port, profile)
    subprocess.Popen(
        [binary, f"--remote-debugging-port={port}", f"--user-data-dir={Path(profile).expanduser()}",
         "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if local_cdp_alive(endpoint):
            return True
        time.sleep(0.5)
    return False


def fallback_configured(dotenv_path: str = ".env") -> bool:
    env = env_config.load_dotenv_values(dotenv_path)
    return any(os.environ.get(k) or env.get(k) for k in ("SKYSCANNER_CDP_ENDPOINT", "SKYSCANNER_PROXY"))


@dataclass
class RouteOutcome:
    route: str
    exit_code: int
    via: str  # "local" | "2captcha" | "none"
    rows: int
    attempts: List[Dict[str, object]] = field(default_factory=list)


def _run_engine(argv: List[str]) -> int:
    args = env_config.apply_env(playwright_scraper.build_arg_parser().parse_args(argv))
    try:
        return asyncio.run(playwright_scraper.run(args))
    except Exception as exc:  # noqa: BLE001 — one route crashing must not end the batch
        log.error("Engine crashed on this route: %s", exc)
        return EXIT_CRASH


def scrape_route(
    route: Dict[str, str], *, args: argparse.Namespace, workdir: str,
    run_engine: Callable[[List[str]], int] = _run_engine,
) -> tuple:
    """Returns (RouteOutcome, products). `run_engine` is injectable for tests."""
    label = route_label(route)
    attempts: List[Dict[str, object]] = []

    def attempt(via: str, extra: List[str]):
        out = str(Path(workdir) / f"route_{len(attempts)}_{via}_{abs(hash(label))}.json")
        code = run_engine(engine_argv(route, out=out, max_results=args.max_results) + extra)
        wrote = code in (EXIT_OK, EXIT_PARTIAL) and Path(out).exists()
        rows = json.loads(Path(out).read_text(encoding="utf-8")) if wrote else []
        attempts.append({"via": via, "exit_code": code, "rows": len(rows)})
        log.info("[%s] %s → exit %d, %d itineraries", label, via, code, len(rows))
        return code, rows

    code, rows, via = EXIT_REMOTE_API_ERROR, [], "none"
    local_ok = local_cdp_alive(args.local_cdp) or (
        args.launch_chrome and launch_local_chrome(args.local_cdp, args.chrome_profile))
    if local_ok:
        code, rows = attempt("local", ["--cdp-endpoint", args.local_cdp,
                                       "--wait-for-human", str(args.wait_for_human)])
        via = "local"
    else:
        log.warning("[%s] local Chrome at %s is not available.", label, args.local_cdp)
        attempts.append({"via": "local", "exit_code": None, "rows": 0, "skipped": "unreachable"})

    if code in FALLBACK_ON and args.fallback:
        if fallback_configured():
            log.info("[%s] falling back to 2Captcha (.env: Scraping Browser / proxy).", label)
            code, rows = attempt("2captcha", ["--cdp-block-retries", str(args.cdp_block_retries)])
            via = "2captcha"
        else:
            log.warning("[%s] no 2Captcha fallback configured (SKYSCANNER_CDP_ENDPOINT / SKYSCANNER_PROXY).", label)

    products = [Product(**r) for r in rows]
    return RouteOutcome(route=label, exit_code=code, via=via if rows else "none",
                        rows=len(products), attempts=attempts), products


def batch_outcome(outcomes: List[RouteOutcome], have_rows: bool) -> dict:
    """finish_run() inputs for the whole batch. A route counts as FAILED
    (by its 1-based number, like a failed page) unless it succeeded or the
    site genuinely had nothing (exit 4). Blocked / remote-error only decide
    the batch's status when no route produced a row; otherwise failed routes
    make it partial (6)."""
    failed = [i for i, o in enumerate(outcomes, start=1) if o.exit_code not in (EXIT_OK, EXIT_ZERO_PRODUCTS)]
    failed_codes = [outcomes[i - 1].exit_code for i in failed]
    return {
        "pages_requested": len(outcomes),
        "pages_completed": len(outcomes) - len(failed),
        "failed_pages": failed,
        "blocked": not have_rows and EXIT_BLOCKED in failed_codes,
        "remote_api_error": not have_rows and bool(failed_codes) and EXIT_BLOCKED not in failed_codes,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--routes-file", required=True, help="CSV of searches (see module docstring)")
    p.add_argument("--out", default="skyscanner_batch.json", help="Merged output for all routes")
    p.add_argument("--format", choices=("json", "csv"), default="json")
    p.add_argument("--max-results", type=int, default=5000, help="Per-route cap (a round trip can exceed 1000 itineraries)")
    p.add_argument("--local-cdp", default="http://127.0.0.1:9222", help="Your local Chrome's debugging endpoint")
    p.add_argument("--chrome-profile", default="~/.chrome-skyscanner",
                   help="Profile dir used when this script has to start Chrome itself")
    p.add_argument("--no-launch-chrome", dest="launch_chrome", action="store_false",
                   help="Do not start Chrome when nothing listens on --local-cdp")
    p.add_argument("--wait-for-human", type=int, default=180, metavar="SECONDS",
                   help="How long to wait for a person to clear a challenge in local Chrome")
    p.add_argument("--no-fallback", dest="fallback", action="store_false",
                   help="Do not retry failed routes through 2Captcha")
    p.add_argument("--cdp-block-retries", type=int, default=2, help="Fresh Scraping Browser sessions to try on the fallback")
    p.add_argument("--route-delay", type=float, default=5.0, help="Seconds between routes (be gentle)")
    p.add_argument("--allow-empty", action="store_true", help="Write the (empty) output even when no route produced a row")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_arg_parser().parse_args(argv)
    if args.max_results < 1 or args.wait_for_human < 0 or args.route_delay < 0 or args.cdp_block_retries < 0:
        print("Error: --max-results must be >= 1; --wait-for-human, --route-delay, --cdp-block-retries >= 0",
              file=sys.stderr)
        return EXIT_BAD_USAGE
    try:
        routes = load_routes(args.routes_file)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE

    started_at = time.time()
    outcomes: List[RouteOutcome] = []
    per_route: List[List[Product]] = []
    with tempfile.TemporaryDirectory(prefix="skyscanner_batch_") as workdir:
        for i, route in enumerate(routes):
            if i:
                time.sleep(args.route_delay)
            outcome, rows = scrape_route(route, args=args, workdir=workdir)
            outcomes.append(outcome)
            per_route.append(rows)

    # Same sku from two routes is the same itinerary; merge in route order.
    products = merge_pages(per_route)
    code = finish_run(
        products=products, out_path=args.out, fmt=args.format, engine="batch:playwright",
        url=args.routes_file, allow_empty=args.allow_empty, started_at=started_at,
        extra_meta={"per_route": [asdict(o) for o in outcomes]},
        **batch_outcome(outcomes, have_rows=bool(products)),
    )
    for o in outcomes:
        print(f"{o.route:32} exit {o.exit_code}  via {o.via:8}  {o.rows} itineraries", file=sys.stderr)
    ok = sum(o.exit_code == EXIT_OK for o in outcomes)
    print(f"{ok}/{len(outcomes)} routes ok, {len(products)} itineraries → "
          f"{args.out if products or args.allow_empty else '(nothing written)'} (exit {code})", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())

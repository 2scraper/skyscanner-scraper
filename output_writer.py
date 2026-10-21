#!/usr/bin/env python3
"""output_writer.py — the row model, JSON/CSV writer, dedupe, exit codes,
and run metadata. Carries (almost) no site knowledge: everything here is
part of the family's shared output CONTRACT — diverging from it needs a
written reason, not a per-site tweak. finish_run()'s outcome precedence
never lets `blocked` / `remote_api_error` be silently ignored because
products were present or --allow-empty was passed.
"""
from __future__ import annotations

import contextlib
import csv
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, List, Optional, Sequence

# --------------------------------------------------------------------------- #
# Exit codes — identical across playwright_scraper / selenium_scraper /
# puppeteer_scraper, and identical to every other 2scraper family repo. A
# caller (CI, a cron job, another program) must be able to tell these apart
# without parsing stdout.
# --------------------------------------------------------------------------- #
EXIT_OK = 0
EXIT_CRASH = 1
EXIT_BAD_USAGE = 2
EXIT_BLOCKED = 3
EXIT_ZERO_PRODUCTS = 4
EXIT_REMOTE_API_ERROR = 5
EXIT_PARTIAL = 6

STATUS_BY_EXIT = {
    EXIT_OK: "complete",
    EXIT_CRASH: "crashed",
    EXIT_BAD_USAGE: "bad_usage",
    EXIT_BLOCKED: "blocked",
    EXIT_ZERO_PRODUCTS: "empty",
    EXIT_REMOTE_API_ERROR: "remote_api_error",
    EXIT_PARTIAL: "partial",
}


# --------------------------------------------------------------------------- #
# Row model
# --------------------------------------------------------------------------- #
@dataclass
class Product:
    """Family-common fields first (same order in JSON and CSV across every
    2scraper repo); skyscanner.com-specific
    (flight) fields at the end.

    Skyscanner has no persistent "SKU" the way a marketplace has a style
    id, so `sku` here is a deterministic fingerprint of the ITINERARY
    (route + dates + airline + departure/arrival time + cabin class —
    everything except price), built by `make_sku()` below. That keeps
    `diff_runs.py` meaningful unmodified: the same itinerary scraped on two
    different days gets the same `sku`, so a price change on it shows up
    as a real `changed` row instead of looking like one result vanishing
    and another appearing.

    `brand` carries the operating airline (the closest family-schema
    analog to a marketplace "brand") rather than adding a redundant
    site-specific `airline` field. `category` is the constant `"flights"`
    — Skyscanner doesn't have a product category the way a marketplace
    does, and cabin class already has its own field below.

    `price` / `currency` / `price_source` are the columns diff_runs.py and
    every cross-site tool key off. `price_source` records where the row
    was read: `"search_api"` for the `web-unified-search` XHR the results
    page polls (the primary source; the initial HTML is an empty shell),
    `"embedded_json"` for hydration state embedded in the page, and
    `"dom"` for a rendered result card — see flight_parser.py.
    """

    # --- family-common ---
    sku: Optional[str]
    source: str
    category: Optional[str]
    title: Optional[str]
    brand: Optional[str]
    price: Optional[float]
    currency: Optional[str]
    price_source: Optional[str]
    product_url: Optional[str]
    image_url: Optional[str]
    scraped_at: str

    # --- skyscanner.com-specific (flight) ---
    origin: Optional[str] = None
    destination: Optional[str] = None
    depart_date: Optional[str] = None
    return_date: Optional[str] = None
    adults: Optional[int] = None
    cabin_class: Optional[str] = None
    stops: Optional[str] = None
    departure_time: Optional[str] = None
    arrival_time: Optional[str] = None
    duration: Optional[str] = None
    sort: Optional[str] = None
    # Round trips: the RETURN leg; null on one-way searches. The fields above
    # (departure_time … stops) describe the outbound leg. Verified on a live
    # LHR⇄BCN web-unified-search response, 2026-09-29.
    return_departure_time: Optional[str] = None
    return_arrival_time: Optional[str] = None
    return_duration: Optional[str] = None
    return_stops: Optional[str] = None
    return_airline: Optional[str] = None


PRODUCT_FIELD_NAMES: List[str] = [f.name for f in fields(Product)]


# --------------------------------------------------------------------------- #
# Dedupe / merge — "merge in page order, not arrival order": the caller
# passes pages already sorted by page number (or, for a concurrent run,
# sorted before calling this), never in whichever-finished-first order.
# --------------------------------------------------------------------------- #
def sku_key(p: Product) -> str:
    """The identity `merge_pages` dedupes on, and the same one an engine's
    scroll/pagination loop should track to decide "did this page add
    anything NEW" — never just "is this page non-empty" (a page repeating
    an already-seen itinerary, e.g. past the real last batch of results,
    isn't empty but isn't new either)."""
    return p.sku or f"__no_sku__:{p.product_url}"


def merge_pages(pages: Sequence[Sequence[Product]]) -> List[Product]:
    seen: dict = {}
    order: List[str] = []
    for page in pages:
        for p in page:
            key = sku_key(p)
            if key not in seen:
                order.append(key)
            seen[key] = p  # last write for a given sku wins, in page order
    return [seen[k] for k in order]


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _atomic(path: str, newline: Optional[str] = None):
    """Write to a temporary file beside `path`, then rename over it.

    `open(path, "w")` truncates before a single byte is written, so a crash,
    a kill or a full disk halfway through `json.dump` leaves a SHORTER file
    where a complete one was — the previous good run destroyed by the
    attempt to replace it. Reproduced here before this writer existed: a
    680-byte good `out.json` came back 0 bytes after a failed rewrite.

    * the temp file goes in the target's OWN directory, because `os.replace`
      is atomic only within one filesystem;
    * `fsync` before the rename makes the bytes durable, not merely visible;
    * `newline` is passed through so the csv module's `newline=""`
      requirement survives the move into the temp file.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline=newline, dir=directory,
        prefix=os.path.basename(path) + ".", suffix=".tmp", delete=False)
    try:
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        # NamedTemporaryFile creates at 0600 and os.replace keeps that mode.
        # An existing target keeps its OWN mode (it may have been tightened
        # on purpose); a new one gets exactly what open() would have given
        # it, `0o666 & ~umask` — never a flat 0644, which narrows 0664 under
        # umask 002.
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        # Including KeyboardInterrupt: never leave `out.json.xxxx.tmp` behind.
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


# A spreadsheet treats a cell beginning with one of these as a FORMULA. Some
# columns here (airline names, deeplink URLs) come from the site, so a CSV
# cell starting with one gets a leading apostrophe, which Excel, Sheets and
# LibreOffice read as "text" and do not display. CSV only: the JSON keeps the
# site's bytes, and `csv_cells_escaped` in the sidecar declares the
# difference. Only STRINGS are touched, so a negative number stays a number.
CSV_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r", "\n")
CSV_FORMULA_ESCAPE = "'"


def _csv_value(v: Any) -> Any:
    # Lists/dicts are serialised first, and the escape runs AFTER, because
    # the joined cell can start with a formula character too.
    if isinstance(v, (list, tuple)):
        return " | ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return v


def _csv_escape(v: Any):
    """Returns (value, was_escaped)."""
    if isinstance(v, str) and v.startswith(CSV_FORMULA_LEADS):
        return CSV_FORMULA_ESCAPE + v, True
    return v, False


def write_json(products: Sequence[Product], out_path: str) -> None:
    with _atomic(out_path) as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)


def write_csv(products: Sequence[Product], out_path: str) -> int:
    """Returns how many cells were formula-escaped."""
    escaped = 0
    with _atomic(out_path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PRODUCT_FIELD_NAMES)
        writer.writeheader()  # written even for zero rows — a consumer reads
        for p in products:    # an empty table, not a zero-byte file.
            row = {}
            for k, v in asdict(p).items():
                value, was = _csv_escape(_csv_value(v))
                row[k] = value
                escaped += was
            writer.writerow(row)
    return escaped


def write_output(products: Sequence[Product], out_path: str, fmt: str) -> int:
    """Returns the number of CSV cells escaped (0 for JSON)."""
    if fmt == "json":
        write_json(products, out_path)
        return 0
    if fmt == "csv":
        return write_csv(products, out_path)
    raise ValueError(f"Unsupported format: {fmt!r} (expected 'json' or 'csv')")


# --------------------------------------------------------------------------- #
# Run metadata sidecar
# --------------------------------------------------------------------------- #
def meta_path_for(out_path: str) -> str:
    return f"{out_path}.meta.json"


def write_meta(
    out_path: str,
    *,
    status: str,
    stop_reason: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]] = None,
    product_count: int,
    price_confirmed_pct: Optional[float] = None,
    started_at: float,
    finished_at: Optional[float] = None,
    extra: Optional[dict] = None,
    path: Optional[str] = None,
) -> None:
    meta = {
        "status": status,
        "stop_reason": stop_reason,
        "engine": engine,
        "url": url,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "failed_pages": failed_pages or [],
        "product_count": product_count,
        "price_confirmed_pct": price_confirmed_pct,
        "started_at": started_at,
        "finished_at": finished_at or time.time(),
    }
    if extra:
        meta.update(extra)
    with _atomic(path or meta_path_for(out_path)) as f:
        f.write(json.dumps(meta, ensure_ascii=False, indent=2))


ATTEMPT_META_SUFFIX = ".latest_attempt.meta.json"


def finish_without_fetch(
    out_path: str, *, stop_reason: str, engine: str, url: str,
    pages_requested: int, started_at: float,
) -> int:
    """End a run stopped before anything was fetched (the proxy pre-check
    got a 407 or 402); return EXIT_REMOTE_API_ERROR (5, content not
    obtained).

    The record goes to `<out>.latest_attempt.meta.json`, NOT
    `<out>.meta.json`: the latter describes the data file beside it, and a
    previous run's good output is still there, untouched, so overwriting
    its metadata with a failure would make the pair contradict each other.
    """
    meta = {
        "status": "failed",
        "stop_reason": stop_reason,
        "engine": engine,
        "url": url,
        "pages_requested": pages_requested,
        "pages_completed": 0,
        "failed_pages": [],
        "product_count": 0,
        "price_confirmed_pct": None,
        "started_at": started_at,
        "finished_at": time.time(),
    }
    path = f"{out_path}{ATTEMPT_META_SUFFIX}"
    with _atomic(path) as f:
        f.write(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"[+] Wrote attempt metadata -> {path} (status=failed, stop_reason={stop_reason})")
    print(f"[!] Failed run: nothing was fetched ({stop_reason}). Any previous "
          f"output and its .meta.json are untouched.")
    return EXIT_REMOTE_API_ERROR


# --------------------------------------------------------------------------- #
# finish_run — the single place every engine calls to decide exit code,
# whether to write output at all, and whether to write a sidecar. Keeping
# this in one shared function is what stops the three engines' exit-code
# mapping from drifting apart.
# --------------------------------------------------------------------------- #
def finish_run(
    *,
    products: List[Product],
    out_path: str,
    fmt: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]],
    blocked: bool,
    remote_api_error: bool,
    allow_empty: bool,
    started_at: float,
    price_confirmed_pct: Optional[float] = None,
    extra_meta: Optional[dict] = None,
    stop_reason: Optional[str] = None,
    incomplete: bool = False,
    rows_stated: Optional[int] = None,
) -> int:
    """Decide status/exit code, write output + sidecar (or neither), return
    the process exit code. NEVER writes a sidecar for a failed run, and
    NEVER overwrites a previous good output with an empty one unless the
    caller explicitly passed --allow-empty.

    `rows_stated`: how many results the SITE said it had (the search API's
    own itinerary ids). Defaults to `extra_meta["itineraries_available"]`,
    which every engine already passes. Zero parsed rows against a positive
    stated count is OUR parser failing on a served search, not an empty
    search, so it is reported as `parser_found_nothing` with exit 5 rather
    than exit 4 — a "no flights" answer would send the reader to check the
    route instead of the parser."""
    failed_pages = failed_pages or []
    if rows_stated is None and extra_meta:
        rows_stated = extra_meta.get("itineraries_available")
    # `incomplete`: the site itself had not finished serving (e.g. a search
    # API still reporting "incomplete") when the run stopped.
    partial = (bool(failed_pages) and pages_completed > 0) or (incomplete and bool(products))
    zero_products = len(products) == 0

    # Outcome precedence — decided ONCE, independent of --allow-empty.
    # `--allow-empty` controls only whether a zero-product result gets
    # WRITTEN as a file (below); it must never launder a blocked or
    # remote-API-error run into a "complete" status just because the
    # caller also passed --allow-empty, and it must never do so just
    # because SOME batches did return results while the run was, in fact,
    # blocked partway through.
    # 5 means "the content was never obtained" — a run that gathered rows
    # and THEN hit a remote error is partial (6), not 5.
    # A run stopped by --proxy-budget-mb outranks everything below: the
    # requests after the cap were refused, so whatever the page looked like
    # then (an error, a challenge, a short list) is not evidence about the
    # site. Rows kept → partial (6); none → content not obtained (5).
    over_budget = bool((extra_meta or {}).get("proxy_budget_exceeded"))
    if over_budget:
        stop_reason = "proxy_budget_exceeded"
        status, exit_code = (("partial", EXIT_PARTIAL) if products
                             else ("failed", EXIT_REMOTE_API_ERROR))
    elif remote_api_error and zero_products:
        status, exit_code = "remote_api_error", EXIT_REMOTE_API_ERROR
    elif remote_api_error:
        status, exit_code = "partial", EXIT_PARTIAL
    elif blocked:
        status, exit_code = "blocked", EXIT_BLOCKED
    elif zero_products and rows_stated:
        # The site served results and said how many; we parsed none.
        status, exit_code = "parser_found_nothing", EXIT_REMOTE_API_ERROR
        stop_reason = "parser_found_nothing"
    elif zero_products:
        status, exit_code = "empty", EXIT_ZERO_PRODUCTS
    elif partial:
        status, exit_code = "partial", EXIT_PARTIAL
    else:
        status, exit_code = "complete", EXIT_OK

    # The "never overwrite good output with empty" rule: a zero-product
    # outcome (whatever its status above — blocked/remote_api_error/empty
    # all zero out `products`) writes NEITHER file NOR sidecar unless the
    # caller explicitly opted in with --allow-empty. NEVER write a sidecar
    # in the not-written case — a PREVIOUS good <out>.json is left in
    # place untouched, and a stale failure sidecar sitting right beside it
    # would contradict that good data rather than describe it. The
    # engine's own logs carry the diagnostic detail — that's what a
    # failed run's output is FOR, not this file.
    meta_kwargs = dict(
        status=status, stop_reason=stop_reason or status, engine=engine, url=url,
        pages_requested=pages_requested, pages_completed=pages_completed,
        failed_pages=failed_pages, product_count=len(products),
        price_confirmed_pct=price_confirmed_pct, started_at=started_at,
    )
    # A metered (proxied) run also records itself in the attempt sidecar,
    # every time: a run that wrote no rows leaves no `<out>.meta.json`, and
    # what it spent through the proxy must still be on disk somewhere.
    metered = bool(extra_meta) and "proxy_bytes" in extra_meta
    if zero_products and not allow_empty:
        if metered:
            write_meta(out_path, path=f"{out_path}{ATTEMPT_META_SUFFIX}",
                       extra=extra_meta, **meta_kwargs)
        return exit_code

    escaped = write_output(products, out_path, fmt)
    if fmt == "csv":
        # Merged UNDER the caller's extra: housekeeping never overwrites
        # site data that happens to share a key.
        extra_meta = {"csv_cells_escaped": escaped, **(extra_meta or {})}
    write_meta(out_path, extra=extra_meta, **meta_kwargs)
    if metered:
        write_meta(out_path, path=f"{out_path}{ATTEMPT_META_SUFFIX}",
                   extra=extra_meta, **meta_kwargs)
    return exit_code

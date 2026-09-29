#!/usr/bin/env python3
"""diff_runs.py — diff two runs by `sku`. No site knowledge.

Refuses to compare two runs that are not both `status == "complete"` in
their `.meta.json` sidecar: a partial run's un-fetched pages would otherwise
read as delisted products, which is a false signal worse than no diff at
all.

A price difference that comes with a `price_source` difference is reported
as `source_changed`, not `changed` — it says something about how much of
each run's data got DOM-confirmed, not about the site. `--fail-on-change`
ignores `source_changed` rows for exactly that reason.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List


def _load_meta(run_path: str) -> dict:
    meta_path = Path(f"{run_path}.meta.json")
    if not meta_path.exists():
        raise SystemExit(f"error: no sidecar found at {meta_path} — was this run produced by finish_run()?")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _load_rows(run_path: str) -> List[dict]:
    path = Path(run_path)
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if path.suffix == ".csv":
        import csv
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    raise SystemExit(f"error: unsupported run file extension: {path.suffix}")


def _index_by_sku(rows: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for row in rows:
        sku = row.get("sku")
        if sku:
            out[sku] = row
    return out


_SEARCH_FIELDS = ("origin", "destination", "depart_date", "return_date", "adults", "cabin_class", "sort")


def diff(old_path: str, new_path: str) -> dict:
    old_meta, new_meta = _load_meta(old_path), _load_meta(new_path)
    for label, meta in (("old", old_meta), ("new", new_meta)):
        if meta.get("status") != "complete":
            raise SystemExit(
                f"error: refusing to diff — {label} run status is "
                f"{meta.get('status')!r}, not 'complete'. A partial run's "
                f"un-fetched pages would read as delisted products."
            )

    old_rows, new_rows = _index_by_sku(_load_rows(old_path)), _index_by_sku(_load_rows(new_path))
    old_skus, new_skus = set(old_rows), set(new_rows)

    # Two different searches (or orderings) share no meaning row-for-row: every
    # itinerary would read as added/removed. Refuse rather than print noise.
    for field in _SEARCH_FIELDS:
        old_vals = {str(r.get(field)) for r in old_rows.values()}
        new_vals = {str(r.get(field)) for r in new_rows.values()}
        if old_rows and new_rows and old_vals != new_vals:
            raise SystemExit(
                f"error: refusing to diff — the runs differ in {field!r} "
                f"({sorted(old_vals)} vs {sorted(new_vals)}); they are not the same search."
            )

    added = sorted(new_skus - old_skus)
    removed = sorted(old_skus - new_skus)
    changed, source_changed = [], []

    for sku in sorted(old_skus & new_skus):
        o, n = old_rows[sku], new_rows[sku]
        old_price, new_price = o.get("price"), n.get("price")
        if old_price == new_price:
            continue
        entry = {"sku": sku, "old_price": old_price, "new_price": new_price, "title": n.get("title")}
        if o.get("price_source") != n.get("price_source"):
            entry["old_price_source"] = o.get("price_source")
            entry["new_price_source"] = n.get("price_source")
            source_changed.append(entry)
        else:
            changed.append(entry)

    capped = [label for label, meta in (("old", old_meta), ("new", new_meta)) if meta.get("capped_by_max_results")]
    return {
        # CLAUDE.md §21: in a capped run "removed" can also mean "outside this
        # run's slice", not delisted.
        "capped_runs": capped,
        "added": added, "removed": removed,
        "changed": changed, "source_changed": source_changed,
        "old_count": len(old_rows), "new_count": len(new_rows),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Diff two skyscanner-scraper runs by sku")
    p.add_argument("old_run")
    p.add_argument("new_run")
    p.add_argument("--fail-on-change", action="store_true",
                    help="Exit 1 if any REAL price change is found (source_changed rows are ignored)")
    p.add_argument("--json", action="store_true", help="Print the diff as JSON instead of a summary")
    args = p.parse_args()

    result = diff(args.old_run, args.new_run)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{args.old_run} ({result['old_count']}) -> {args.new_run} ({result['new_count']})")
        print(f"  added:          {len(result['added'])}")
        print(f"  removed:        {len(result['removed'])}")
        print(f"  price changed:  {len(result['changed'])}")
        print(f"  source_changed: {len(result['source_changed'])} (ignored by --fail-on-change)")
        if result["capped_runs"]:
            print(f"  note: {' and '.join(result['capped_runs'])} run was capped by --max-results — "
                  f"'removed' may be outside that run's slice, not gone from the site")

    if args.fail_on_change and result["changed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

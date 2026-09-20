# captures/

Real, live captures of skyscanner.com go here — the same role
`/home/claude/work/captures/` played while building `stockx-scraper`
(search/category/product HTML + the embedded `__NEXT_DATA__`/JSON pulled
out of them), used to verify `flight_parser.py`'s selectors against the
real page instead of the best-effort guesses it ships with today.

This folder is empty right now because nothing in it has been captured
yet — see the repo's README ("Read this before trusting a run") and
`TESTING.md` step 2 for why, and for the exact command to produce one:

```bash
python3 playwright_scraper.py --origin LHR --destination JFK \
  --depart-date 2026-10-15 --max-results 10 \
  --format json --out /tmp/sky_test.json --dump-html
```

That writes `sky_test_debug.html` next to the output — drop a copy of it
here (e.g. `captures/search_lhr_jfk.html`), and if you can, also save the
`__NEXT_DATA__` JSON blob out of it separately (same idea as `stockx-
scraper`'s `*_next_data.json` files) — that's the piece `flight_parser.
_find_itinerary_lists()` actually needs to stop guessing.

Once a real file is here, `flight_parser.py` gets updated to match it,
the `# TODO: verify live` markers on whatever that file confirms get
removed, and a trimmed/scrubbed copy plus a new `smoke_test.py` check
gets added under `tests/fixtures/` — see `CONTRIBUTING.md`.

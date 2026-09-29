# captures/

Measurement artefacts: the `<out>.meta.json` sidecars (and one batch
summary) of the live runs that README's numbers come from. No row data, no
session tokens — counts, statuses and timestamps only. `smoke_test.py`
checks every itinerary count README states against these files, the
fixture and the sample output, so a figure cannot drift from its source.

All runs: 2026-09-29, Playwright over Roman's own local Chrome
(`--cdp-endpoint http://127.0.0.1:9222`) after a person cleared PerimeterX's
"Press & Hold" once (`--wait-for-human`).

| File | What it measured |
|---|---|
| `2026-09-29_lhr-jfk-1115_local-chrome_max1000.meta.json` | LHR→JFK 15.11, `--max-results 1000`: 385 itineraries (older sidecar format, before `itineraries_available` existed) |
| `2026-09-29_lhr-jfk-1115_local-chrome_default-cap.meta.json` | same route, default `--max-results 30`: `complete`, `stop_reason: max_results`, 30 of 389 |
| `2026-09-29_lhr-jfk-1030_local-chrome_after-rechallenge.meta.json` | LHR→JFK 30.10, re-challenged at 18:11 and cleared by hand: 361 of 361 |
| `2026-09-29_batch_one-route.meta.json` | `batch_scraper.py`, one route: 389 |
| `2026-09-29_batch_three-routes.summary.json` | `batch_scraper.py`, three routes (older summary format): 1690 in total |

The real XHR body the parser was built against is in
`fixtures/web_unified_search_lhr_jfk_trimmed.json` (trimmed to 3 of 373).

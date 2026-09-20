# Skyscanner Scraper by 2scraper

**Open-source flight-search scraper for skyscanner.com — three engines, your own infrastructure by default, 2Captcha's paid products when you actually need them.**

Pull flight search results — airline, price, times, duration, stops, deep link — straight from Skyscanner into JSON or CSV.

[**View source on GitHub →**](https://github.com/2scraper/skyscanner-scraper)

---

## Before you scrape: official channels

Skyscanner offers partner/affiliate travel-search feeds for businesses that need programmatic access at scale — check their current partner documentation if your use case fits within one and you can get approved. This scraper exists for everything outside that: quick lookups, personal price tracking, and use cases a formal partnership doesn't fit.

## Read this before you rely on it

Unlike this family's other scrapers, this one was built **without ever being able to look at the real site** — `skyscanner.com` blocks this project's own automated fetch tooling at the `robots.txt` level, and that restriction was respected rather than worked around. The architecture (exit codes, output schema, dedupe, credential handling, all three engines) is real and tested. The parsing selectors are documented best-effort guesses pending a real capture. Full honesty section in the [repository README](https://github.com/2scraper/skyscanner-scraper#readme) — read it before you point this at anything that matters.

## What you get

- Free, open-source scraper, one script per engine — **Playwright** (primary, local-first), **Selenium**, and **Puppeteer** (via pyppeteer), all producing the identical output schema and exit codes
- Search by origin/destination/dates, with cabin class, stops and sort filters
- Reads Skyscanner's own embedded data payload first, with a DOM fallback — same family principle as this project's sibling scrapers
- JSON and CSV export, with a documented `Product` schema and a `.meta.json` sidecar on every completed/partial run
- Optional 2Captcha integration, wired in but never required to get started

## 2Captcha products, when you want them

| Product | What it's for |
|---|---|
| **Captcha solving — [2captcha.com](https://2captcha.com)** | Detects a challenge, decides whether it's actually blocking you (not just present), solves it |
| **Scraping Browser API — 2captcha.com** | A remote browser session over CDP with its own proxy, fingerprint and captcha auto-solve bundled — `--cdp-endpoint` |
| **Browser fingerprints — 2captcha Fingerprint API** | Pin a specific OS/browser/country fingerprint for a locally-launched browser |
| **Proxies — 2captcha.com/proxy** (2prx.com is the same product, different name) | Drop credentials into `.env`, rotated automatically with per-exit failure tracking |

## Who this is for

Travel-price researchers, fare-tracking tools, and anyone who wants Skyscanner search results in a script rather than a browser tab — and is comfortable that this repo's parsing layer is a documented work in progress (see README).

## Get started

```bash
git clone https://github.com/2scraper/skyscanner-scraper.git
cd skyscanner-scraper
pip install -r requirements-playwright.txt && playwright install chromium
cp .env.example .env   # optional — not required for a normal local-first run

python3 playwright_scraper.py --origin LHR --destination JFK --depart-date 2026-10-15 --format json --out sky_results.json
```

Full setup, CLI reference, and configuration details in the [repository README](https://github.com/2scraper/skyscanner-scraper#readme).

---

**Need it running at scale, with proxies, fingerprints, and captcha solving already configured?**
[Talk to us →](https://2captcha.com/contact) · Proxies by [2captcha.com/proxy](https://2captcha.com/proxy) · Scraping Browser API & captcha solving by [2captcha.com](https://2captcha.com)

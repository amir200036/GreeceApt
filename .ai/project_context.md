# GreeceApt — Project Context

## Purpose
Athens apartment deal finder. Scrapes XE.gr for listings in the €30,000–€70,000 range,
runs a multi-layer AI filter pipeline, and produces a final scored database of properties
priced significantly below the local market baseline.

Target: Studios and 1BR apartments (25–55 sqm) suitable for long-term rental (LTR) in Athens.

---

## Tech Stack

| Layer       | Technology |
|-------------|------------|
| Scraping    | curl_cffi (Chrome impersonation), Playwright (cookie capture) |
| AI Filter   | Moondream2 via Ollama (local inference) |
| Storage     | SQLite (3 databases, see pipeline below) |
| Language    | Python 3.11+ |
| Project     | `src/` layout, no framework |

---

## Source Files

```
src/greeceapt/
├── scraper/scrape_xe.py                  # XE.gr scraper — pagination, detail fetch, cookie refresh
├── cookies/cookie_manager.py             # Cookie capture, loading, expiry detection
├── utils/helpers.py                      # URL normalization, neighborhood resolution
├── utils/url_builder.py                  # Builds XE.gr search URL with query params
├── db_helpers/core.py                    # ingested_listings.db schema and insert logic
├── db_helpers/db_conductor.py            # All pipeline DB operations
├── pipeline/ingest.py                    # scraper_listings.json → ingested_listings.db
├── ai_agent/ai_conductor.py              # Orchestrates Layers 0–2
├── ai_agent/layer_0_cleaner.py           # Metadata filter: neighborhood, age, photos
├── ai_agent/layer_1_quality_audit.py     # Moondream2 visual audit (top-5 average, 1–10)
├── ai_agent/layer_2_aesthetic_filter.py  # Aesthetic gate + neighborhood size filter
├── scoring/market_analytics.py           # Neighborhood baseline (10%-trimmed mean PSQM)
├── scoring/scoring_algorithm.py          # Weighted 0–100 deal ranking
├── scoring/scoring_conductor.py          # Orchestrates scoring pipeline
└── main.py                               # Top-level: runs ai_conductor then scoring_conductor
```

---

## Data Files

| File                        | Purpose |
|-----------------------------|---------|
| `data/cookies.json`         | XE.gr browser session cookies |
| `data/scraper_listings.json`| Raw scraped listings (JSON array) — scraper output |
| `data/ingested_listings.db` | All ingested listings (source of truth) |
| `data/updated_listings.db`  | Working DB — rebuilt each pipeline run by Layer 0, filtered by Layers 1 & 2 |
| `data/final_deals.db`       | Final output — `deals` table sorted by investment score |

---

## Pipeline Flow

```
Stage 1 — Cookie Capture (one-time or on expiry)
  cookie_manager.py → data/cookies.json
  Interactive: user solves CAPTCHA in real browser

Stage 2 — Scraping
  scrape_xe.py → data/scraper_listings.json
  Paginates XE.gr, fetches detail per listing (concurrent, Chrome-impersonated)

Stage 3 — Ingestion
  ingest.py: scraper_listings.json → ingested_listings.db

Stage 4 — AI Filter Pipeline  (ai_conductor.py orchestrates)
  Layer 0: ingested_listings.db → updated_listings.db
           Removes listings missing neighborhood, >180 days old, or <2 photos
  Layer 1: Moondream2 visual audit
           Downloads + resizes images, runs Ollama inference, scores interiors 1–10
           visual_score = average of top-5 interior scores
  Layer 2: updated_listings.db (in-place)
           Removes visual_score < 3.0 and thin neighborhoods (< 10 listings)

Stage 5 — Scoring  (scoring_conductor.py)
  Layer 3: Floor-adjusted 10%-trimmed-mean PSQM per neighborhood
  Final:   Weighted 0–100 score per listing → data/final_deals.db
```

---

## Run Order

```bash
# First run only (or when cookies expire):
python -m greeceapt.cookies.cookie_manager

# Each scrape session:
python -m greeceapt.scraper.scrape_xe
python -m greeceapt.pipeline.ingest

# Full AI pipeline + scoring:
python -m greeceapt.main
```

Set `PYTHONPATH=src` or use `pip install -e .` before running.

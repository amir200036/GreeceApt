# GreeceApt — Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────┐
│                      USER / BROWSER                     │
│  - Solves CAPTCHA manually when prompted                │
└────────────────────────┬────────────────────────────────┘
                         │ (interactive, one-time)
                         ▼
┌─────────────────────────────────────────────────────────┐
│              cookie_manager.py                          │
│  Playwright browser → captures session cookies          │
│  Output: data/cookies.json                              │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              scrape_xe.py                               │
│  1. Load cookies, build curl-cffi impersonated session  │
│  2. Paginate XE.gr map search (up to 50 pages)         │
│  3. Collect listing IDs via map_search API              │
│  4. Fetch detail per ID via single_result API           │
│     (async, max 3 concurrent, 5–10s random delay)       │
│  5. Resolve ad groups (cheapest price + most photos)    │
│  6. Write to scraper_listings.json (dedup by url)       │
│  Output: data/scraper_listings.json                     │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              pipeline/ingest.py                         │
│  1. Load scraper_listings.json                          │
│  2. Normalize URLs, resolve neighborhoods               │
│  3. INSERT OR REPLACE into ingested_listings.db         │
│  Output: data/ingested_listings.db                      │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              ai_conductor.py — Layer 0                  │
│  1. Copy ingested_listings.db → updated_listings.db     │
│  2. Remove: missing neighborhood                        │
│  3. Remove: publication date > 180 days old             │
│  4. Remove: fewer than 2 photos                         │
│  Removed rows → removed_listings table                  │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              ai_conductor.py — Layer 1                  │
│  (Moondream2 via Ollama, ThreadPoolExecutor)            │
│  1. Download all images per listing (8 workers)         │
│  2. Resize to 384×384 LANCZOS → temp JPEG               │
│  3. Run Moondream2: "Briefly describe this room…"       │
│  4. Parse description → interior/exterior + score 1–10  │
│  5. visual_score = avg of top-5 interior scores         │
│  6. Write visual_score, aesthetic_grade to DB           │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              ai_conductor.py — Layer 2                  │
│  1. Remove listings with aesthetic_grade < 3.0          │
│  2. Remove all listings in neighborhoods with < 10 left │
│  Removed rows → removed_listings table                  │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              scoring_conductor.py — Layer 3             │
│  market_analytics.py:                                   │
│  1. For each neighborhood: collect (price/area)*floor_mult│
│  2. 10%-trimmed mean → neighborhood PSQM baseline        │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              scoring_conductor.py — Final Ranking       │
│  scoring_algorithm.py:                                  │
│  score = 45% price_arbitrage                            │
│        + 20% location_tier                              │
│        + 20% visual_quality                             │
│        + 15% structural_index                           │
│  Output: data/final_deals.db  (deals + neighborhoods)   │
└─────────────────────────────────────────────────────────┘
```

---

## Module Relationships

```
main.py
  └── ai_agent/ai_conductor.py
        ├── ai_agent/layer_0_cleaner.py   → db_helpers/db_conductor.py
        ├── ai_agent/layer_1_quality_audit.py  (Ollama HTTP, Pillow)
        └── ai_agent/layer_2_aesthetic_filter.py → db_helpers/db_conductor.py

  └── scoring/scoring_conductor.py
        ├── scoring/market_analytics.py
        ├── scoring/scoring_algorithm.py
        └── db_helpers/db_conductor.py

scrape_xe.py
  ├── cookies/cookie_manager.py   (load_cookies)
  ├── utils/url_builder.py        (build_xe_url, ATHENS_CENTER_ID)
  └── utils/helpers.py            (normalize_listing_url, resolve_neighborhood)

pipeline/ingest.py
  └── db_helpers/core.py          (insert_listings)

db_helpers/core.py
  └── utils/helpers.py            (normalize_listing_url)
```

---

## Database Schema

### `data/ingested_listings.db` — Raw Store

```sql
CREATE TABLE listings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    url              TEXT UNIQUE,
    headline         TEXT,
    price_eur        REAL,
    price_per_sqm    REAL,
    area_sqm         REAL,
    neighborhood     TEXT,
    address_raw      TEXT,
    bedrooms         INTEGER,
    bathrooms        INTEGER,
    floor            INTEGER,
    year_built       INTEGER,
    renovation_year  INTEGER,
    energy_class     TEXT,
    photos_count     INTEGER,
    photo_urls_json  TEXT,
    publication_date TEXT,
    scraped_at       TEXT,
    raw_json         TEXT,
    updated_at       TEXT,
    neighborhood_score INTEGER,
    visual_score     REAL,
    aesthetic_grade  REAL
);
```

### `data/updated_listings.db` — Pipeline Working DB

Same columns as ingested_listings.db (all stored as TEXT — cast explicitly in queries), plus:

```sql
layer_1_processed  INTEGER DEFAULT 0,
visual_score       REAL,
aesthetic_grade    REAL,
layer_1_features   TEXT
```

Also contains `removed_listings` table with all source columns + `removal_reason TEXT, layer_origin TEXT`.

### `data/final_deals.db` — Scored Output

```sql
CREATE TABLE deals (
    score       REAL,
    hood        TEXT,
    price       INTEGER,
    area        INTEGER,
    market_diff TEXT,
    floor       TEXT,
    visual      REAL,
    url         TEXT
);

CREATE TABLE neighborhoods (
    name          TEXT PRIMARY KEY,
    listing_count INTEGER,
    median_psqm   REAL,
    tier          TEXT
);
```

---

## Key Design Decisions

| Decision | Reason |
|----------|--------|
| Three separate DBs | ingested = source of truth; updated = filtered working copy rebuilt each run; final = output only |
| `INSERT OR REPLACE` on url | Idempotent ingestion — re-running ingest is safe |
| updated_listings.db rebuilt from scratch each run | Layer 0 always starts clean; stale filtered rows don't accumulate |
| All columns TEXT in updated_listings.db | Avoids type mismatch when copying from a typed source; queries use explicit CAST |
| Moondream via Ollama (local) | No API costs, no rate limits, private data stays local |
| Top-5 interior images for visual_score | Robust to exterior/floor-plan shots that sneak into listings |
| 10%-trimmed mean for PSQM baseline | Removes extreme outliers without IQR complexity |
| Floor multiplier in baseline computation | Normalizes all listings to "1st-floor equivalent" so ground-floor discounts don't distort the neighborhood average |

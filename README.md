# GreeceApt

An Athens apartment deal finder. Scrapes [XE.gr](https://www.xe.gr) for apartments in the **€30,000–€70,000** range, runs a multi-layer AI filter pipeline, and produces a ranked SQLite database of underpriced properties.

**Target:** Studios and 1BR apartments (25–55 sqm) in strong Athens neighborhoods, suitable for long-term rental (LTR).

---

## Why I Built This

Athens has one of the most compelling small-apartment investment markets in Europe right now — high rental yields, ongoing gentrification, and prices still recovering from the 2010s debt crisis. But sifting through hundreds of listings manually to find real deals is slow. This tool automates the full workflow from scraping to scored output.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Scraping | `curl-cffi` (Chrome impersonation), `playwright` (cookie capture) |
| AI Filter | Moondream2 via Ollama (local inference) |
| Storage | SQLite (3 databases through the pipeline) |
| Language | Python 3.11+ |

---

## Project Structure

```
src/greeceapt/
├── scraper/
│   └── scrape_xe.py              # Async scraper — pagination, detail fetch, cookie refresh
├── cookies/
│   └── cookie_manager.py         # Cookie capture, loading, and expiry detection
├── utils/
│   ├── helpers.py                # URL normalization, neighborhood resolution
│   └── url_builder.py            # Builds XE.gr search URLs
├── db_helpers/
│   ├── core.py                   # ingested_listings.db schema and insert logic
│   └── db_conductor.py           # All pipeline DB operations
├── pipeline/
│   └── ingest.py                 # scraper_listings.json → ingested_listings.db
├── ai_agent/
│   ├── ai_conductor.py           # Orchestrates Layers 0–2
│   ├── layer_0_cleaner.py        # Metadata filter: neighborhood, age, photo count
│   ├── layer_1_quality_audit.py  # Moondream2 visual audit (top-5 average, 1–10 scale)
│   └── layer_2_aesthetic_filter.py  # Aesthetic gate + neighborhood size filter
└── scoring/
    ├── market_analytics.py       # Neighborhood baseline computation (10% trimmed mean)
    ├── scoring_algorithm.py      # Weighted deal ranking (0–100 score)
    └── scoring_conductor.py      # Orchestrates scoring pipeline

data/                             # Runtime artifacts (gitignored)
```

---

## Pipeline Flow

```
Stage 1 — Cookie Capture  (one-time, or when cookies expire)
  cookie_manager.py → data/cookies.json
  Opens a real browser; user completes any CAPTCHA.

Stage 2 — Scraping
  scrape_xe.py → data/scraper_listings.json
  Paginates XE.gr map search, fetches each listing detail concurrently.
  Chrome-impersonated session with automatic cookie refresh on block.

Stage 3 — Ingestion
  pipeline/ingest.py → data/ingested_listings.db
  Normalizes URLs, resolves neighborhood names, inserts rows.

Stage 4 — AI Filter Pipeline  (ai_conductor.py orchestrates)
  Layer 0 — Metadata Filter (SQL only, no AI)
    • Missing neighborhood → removed
    • Publication date > 180 days old → removed
    • Fewer than 2 photos → removed
    ingested_listings.db → updated_listings.db

  Layer 1 — Moondream Visual Audit
    • Downloads all listing images, resizes to 384×384
    • Runs Moondream2 (Ollama) to describe each image
    • Interior images scored 1–10 via keyword matching
    • visual_score = average of top-5 interior scores

  Layer 2 — Quality Gate
    • visual_score < 3.0 → removed
    • Neighborhoods with < 10 surviving listings → removed

Stage 5 — Scoring  (scoring_conductor.py)
  Layer 3 — Market Analytics
    • Floor-adjusted 10%-trimmed-mean PSQM per neighborhood
  Final Ranking
    • Weighted 0–100 investment score per listing
    • Output → data/final_deals.db
```

---

## Setup

**1. Clone and create a virtual environment:**

```bash
git clone https://github.com/your-username/GreeceApt.git
cd GreeceApt
python3 -m venv venv
source venv/bin/activate
```

**2. Install dependencies:**

```bash
pip install -r requirements.txt
playwright install chromium
```

**3. Install the package in editable mode:**

```bash
pip install -e .
```

**4. Install and start Ollama with Moondream:**

```bash
ollama pull moondream
ollama serve
```

---

## Running the Pipeline

```bash
# First run only (or when cookies expire):
python -m greeceapt.cookies.cookie_manager

# Scrape XE.gr → data/scraper_listings.json
python -m greeceapt.scraper.scrape_xe

# Ingest → data/ingested_listings.db
python -m greeceapt.pipeline.ingest

# Full AI pipeline + scoring:
python -m greeceapt.main
```

---

## Scoring Model

Each listing in `final_deals.db` receives a `score` (0–100):

| Factor | Weight | What it measures |
|---|---|---|
| Price Arbitrage | 45% | Floor-adjusted PSQM vs. neighborhood baseline (V-index) |
| Location Tier | 20% | Strategic tier rating (45–98) |
| Visual Quality | 20% | Moondream top-5 average (1–10 scale) |
| Structural Index | 15% | Mean of size, energy class, and build year components |

### Neighborhood Investment Tiers

| Tier | Score | Example Neighborhoods |
|---|---|---|
| 5 — Elite Blue Chip | 98 | Kolonaki, Koukaki, Mets, Plaka |
| 4 — Hip Urban | 88 | Pagkrati, Exarcheia, Metaxourgeio, Petralona |
| 3 — Prime Residential | 82 | Kallithea, Zografou, Nea Smyrni, Kaisariani |
| 2 — Growth Hub | 65 | Kypseli, Peristeri, Galatsi |
| 1 — Budget Central | 45 | Omonia, Kolonos, Kato Patisia |

---

## Data Files

| File | Description |
|---|---|
| `data/cookies.json` | XE.gr browser session cookies |
| `data/scraper_listings.json` | Raw scraped listings (JSON array) |
| `data/ingested_listings.db` | All ingested listings (source of truth) |
| `data/updated_listings.db` | Working DB rebuilt each pipeline run; filtered and scored by Layers 0–2 |
| `data/final_deals.db` | Final output: `deals` table sorted by investment score |

All data files are gitignored and stay local.

---

## Interpreting `final_deals.db`

Open with any SQLite browser (e.g. [DB Browser for SQLite](https://sqlitebrowser.org/)):

```sql
SELECT score, hood, price, area, market_diff, floor, visual, url
FROM deals
ORDER BY score DESC;
```

| Column | Meaning |
|---|---|
| `score` | 0–100 composite investment score |
| `hood` | Canonical neighborhood name |
| `price` | Asking price in EUR |
| `area` | Apartment size in sqm |
| `market_diff` | % premium/discount vs. neighborhood baseline (e.g. `-18%` = 18% below baseline) |
| `floor` | Floor number |
| `visual` | Moondream visual quality score (1–10) |
| `url` | XE.gr listing URL |

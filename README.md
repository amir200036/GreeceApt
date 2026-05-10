# GreeceApt

Athens apartment deal finder: scrapes **XE.gr** and **Spitogatos** (merged in `run_all`), runs an AI + scoring pipeline, and writes ranked deals to SQLite.

**Target:** Studios and 1BR (about 25–55 sqm), €30k–€70k band, Athens neighborhoods, long-term rental focus.

---

## Tech

| Layer | Stack |
|------|--------|
| Scraping | `curl-cffi`, Playwright (cookies / Obscura CDP when present) |
| AI | Moondream2 via Ollama (local) |
| Data | SQLite (`ingested` → `updated` → `final_deals`) |
| Python | 3.11+ |

---

## Layout (`src/greeceapt/`)

| Area | Role |
|------|------|
| `scraper/` | `run_all`, `scrape_xe` / `scrape_spitogatos` (CLI shims), `xe/` (XE modules), `spitogatos/` (Spitogatos modules), `util`, `obscura_helper` |
| `cookies/` | Capture + load `data/cookies.json` |
| `db_helpers/` | `paths`, `util`, `core` (ingested schema + insert), `db_conductor` (connections + layer queries) |
| `pipeline/` | `ingest` → `ingested_listings.db` from `listings.json` |
| `ai_agent/` | Layers 0–2 + `ai_conductor` |
| `scoring/` | Baselines + `rank_deals` + `scoring_conductor` |
| `main.py` | Full pipeline entrypoint |

Runtime files under `data/` are gitignored.

**Agent / developer context:** `.ai/project_context.md`, `.ai/coding_rules.md`, `.ai/architecture.md`.

---

## Pipeline (what `python -m greeceapt.main` does)

1. **Scrape** — `run_all`: XE and Spitogatos **each in their own subprocess**, then merge + Quad-Lock dedupe (pHash + metadata) → `data/listings.json`. **XE** (`scraper/xe/`): map search (with-photos by default), optional `XE_MAPSEARCH_PREVIEW` / `XE_FAST` and other `XE_*` env pacing (see `xe/xe_config.py`). **Spitogatos** (`scraper/spitogatos/`): curl-only JSON APIs — Phase A search + Phase B detail; filters follow `SEARCH_PAYLOAD` / SERP URL in `spitogatos/config.py` (e.g. **with photos** and **last update ~6 months** via `lastUpdateMonths`). Merge may still route parseably **old** publications to `data/stale_listings.json` (`scraper/util.py`).
2. **Ingest** — `listings.json` → `data/ingested_listings.db`.
3. **AI** — Layer 0 (metadata SQL filter) → Layer 1 (Moondream visual score) → Layer 2 (score + neighborhood size gate) on `updated_listings.db`.
4. **Score** — Neighborhood baselines + weighted 0–100 score → `data/final_deals.db`.

Layer 0 drops rows with: missing neighborhood, publication age **> 180 days**, or **≤2** photo URLs (needs at least **3** photos).

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -U pip
pip install -r requirements.txt
pip install -e .
pip install -r requirements-dev.txt   # optional: pytest for tests/
playwright install chromium
```

**Virtualenv:** Use **Python 3.11+** (newer releases e.g. 3.14 may work locally; CI may pin lower). Runtime pulls **numpy / scipy / PyWavelets** via `imagehash` for pHash merge in `run_all` — that bulk is normal. **`rich`** comes from **`curl_cffi`**, not the app directly.

Ollama (Layer 1): `ollama pull moondream` then `ollama serve`.

---

## Run

```bash
python -m greeceapt.cookies.cookie_manager           # refresh cookies when missing or expired
python -m greeceapt.main                             # full pipeline
python -m greeceapt.main --score-only                # Stage 4 only → final_deals.db (after AI)

# Individual stages (same order as ``main``)
python -m greeceapt.scraper.run_all                  # scrape + merge only → data/listings.json
python -m greeceapt.pipeline.ingest                   # listings.json → ingested_listings.db
python -m greeceapt.ai_agent.ai_conductor             # Layers 0–2 → updated_listings.db
python -m greeceapt.scoring.scoring_conductor         # baselines + rank → final_deals.db
```

---

## Scoring weights (`final_deals.db`)

| Factor | Weight |
|--------|--------|
| Price vs baseline (V-index) | 45% |
| Location tier | 20% |
| Visual (Moondream top-5 avg, 1–10) | 20% |
| Structural (size, energy, year) | 15% |

Neighborhood → tier mapping lives in `scoring_algorithm.py` (`NEIGHBORHOOD_CANONICAL`). Price baselines require **≥10** listings per neighborhood after filters; thinner hoods omit the price leg and weights are renormalized (see `market_analytics.compute_neighborhood_baselines`).

---

## Useful SQLite

```sql
SELECT score, hood, price, area, market_diff, floor, visual, url
FROM deals
ORDER BY score DESC;
```

| Column | Meaning |
|--------|---------|
| `score` | 0–100 |
| `hood` | Neighborhood name |
| `market_diff` | % vs baseline (e.g. `-18%` = cheaper) |
| `visual` | Moondream 1–10 |

Use any SQLite browser (e.g. [DB Browser](https://sqlitebrowser.org/)).

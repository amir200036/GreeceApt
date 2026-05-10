# GreeceApt — Project context (for agents)

## Mission

Autonomous pipeline to identify **high-yield, low-risk** real estate opportunities in **Athens**, focused on **small long-term-rental (LTR) apartments** (studios / 1BR in the project’s target band). The system scrapes listings, normalizes and stores them, filters low-quality or stale inventory, scores visuals with local vision AI, and ranks deals against neighborhood economics.

## Neighborhood strategy

- Prioritize neighborhoods that support **reliable baselines**: **≥10** qualifying listings per hood get a PSQM baseline (`market_analytics`); thinner hoods still receive a **tier location score**, but the **price vs baseline leg is omitted** for those rows (`rank_deals` renormalizes weights).
- Canonical neighborhood strings and tier mapping live in code (`NEIGHBORHOOD_CANONICAL` in `scoring/scoring_algorithm.py`); align new hoods with existing naming (e.g. **Agios / Agia** prefixes are not stripped).

## 5-tier safety & demand scale

Baseline **tier anchor scores** (used in scoring hierarchy; hood-level detail in `scoring_algorithm`):

| Tier | Anchor | Profile | Neighborhoods (examples) |
|------|--------|---------|---------------------------|
| **5** | **98.0** | Elite blue-chip — max safety, max demand | Plaka, Kolonaki, Koukaki, Mets |
| **4** | **88.0** | Hip urban — high demand, trendy | Pagkrati, Neos Kosmos, Exarcheia, Petralona |
| **3** | **82.0** | Prime residential — safe, stable | Kallithea, Zografou, Ampelokipoi, Dafni |
| **2** | **65.0** | Growth hub — gentrification potential, medium risk | Kypseli, Amerikis Sq, Peristeri |
| **1** | **45.0** | Budget central — higher social risk; **price-driven** only | Omonia, Victoria, Attiki |

## Scoring weights (deal ranker)

Component weights **sum to 1.0** before the 0–100 display scale:

- **Price vs baseline (V-index):** 45% (`W_PRICE`)
- **Location tier:** 20% (`W_HOOD`)
- **Visual (Moondream):** 20% (`W_VISUAL`)
- **Structure / metadata composite:** 15% (`W_STRUCT`)

Missing visual signal uses the configured fallback in `scoring_algorithm.py` (`_VISUAL_FALLBACK`).

## What the repo does (technical)

Scrapes **XE.gr** + **Spitogatos** (merged, deduped), ingests to SQLite, Layer 0–2 AI pipeline (metadata + Moondream + gates), then baselines + `rank_deals` → `final_deals.db`.

## Stack

Python **3.11+**, `src/` layout, SQLite, **`curl-cffi`** + Playwright where needed, **Moondream** via **Ollama** locally.

**Venv:** `requirements.txt` (runtime); `requirements-dev.txt` adds `pytest`. `imagehash` pulls numpy/scipy stack; prefer `pip check` clean.

## Entrypoints

- **`python -m greeceapt.main`** — scrape/merge → ingest → AI → scoring.
- **`python -m greeceapt.cookies.cookie_manager`** — refresh `data/cookies.json` when needed.

## Notable modules

| Path | Role |
|------|------|
| `scraper/run_all.py` | Parallel scrapers + merge → `data/listings.json` |
| `db_helpers/db_conductor.py` | Single place for pipeline DB reads/writes |
| `db_helpers/core.py` | Ingest schema + `insert_listings` |
| `pipeline/ingest.py` | JSON → `ingested_listings.db` |
| `ai_agent/` | Layers 0–2 + `ai_conductor` |
| `scoring/scoring_algorithm.py` | Tiers + `rank_deals` |

## Local artifacts (gitignored)

Cookies, merged `listings.json`, per-site JSON, `ingested_listings.db`, `updated_listings.db`, `final_deals.db`, merge sidecars (`deleted_listings.json`, `stale_listings.json`).

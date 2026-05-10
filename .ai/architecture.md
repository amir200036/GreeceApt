# GreeceApt — Architecture

## Five-layer pipeline (conceptual → code)

| # | Layer | Responsibility | Primary code |
|---|--------|----------------|--------------|
| **1** | **Ingest** | Scraped JSON → **`ingested_listings.db`** (normalized URLs, floor names, types) | `pipeline/ingest.py`, `db_helpers/core.py` |
| **2** | **Filter** | Drop **basements** (by normalized floor), listings with **≤2** photos, stale publication (**>180d**), missing neighborhood | `ai_agent/layer_0_cleaner.py` (copy ingested → `updated_listings.db` + filters) |
| **3** | **Auditor (AI vision)** | Parallel image audit via **Ollama (Moondream)** → structured JSON → **`visual_score`** (1–10 rubric), interior-gated | `ai_agent/layer_1_quality_audit.py`, `ai_agent/ai_conductor.py`, `db_helpers/db_conductor.py` |
| **4** | **Analyst (baseline)** | Per-neighborhood **floor-adjusted, trimmed-mean normalized PSQM** (`market_analytics`). Neighborhoods with **fewer than 10** qualifying listings **are omitted** from the baseline map — **no** tier-wide or municipality fallback for the price anchor | `scoring/market_analytics.py`, `scoring/scoring_conductor.py` |
| **5** | **Ranker** | Weighted sum of price, location tier, visual, structure → **`final_deals.db`** | `scoring/scoring_algorithm.py` (`rank_deals`), `db_conductor` |

**Layer 2 (aesthetic gate)** after the auditor: `layer_2_aesthetic_filter.py` removes weak visuals / thin neighborhoods before ranking — still part of the “quality path” before baselines consume the surviving set.

**Filter contract:** Layer 0 currently enforces neighborhood, **≥3** photos, and **>180d** staleness. **Basement exclusion** is a required part of this layer’s contract: implement using normalized `floor` / numeric level (consistent with ingest rules and `xe_parse` floor semantics) if not already present in SQL.

## End-to-end data flow

1. **Cookies** — `cookies/cookie_manager.py` → `data/cookies.json`
2. **Scrape** — `scraper/run_all.py`: XE + Spitogatos → `data/listings.json` (+ per-site JSON, merge metadata)
3. **Ingest** — `listings.json` → **`ingested_listings.db`**
4. **Filter (Layer 0)** — copy → **`updated_listings.db`**, apply metadata rules
5. **Auditor (Layer 1)** — Moondream batch/parallel image processing → columns on `updated_listings.db`
6. **Gate (Layer 2)** — visual + neighborhood density rules
7. **Analyst + Ranker** — baselines from `updated_listings` / stats tables → **`final_deals.db`**

**Main orchestrator:** `greeceapt/main.py` runs stages in order.

## Database schema relationships

- **`ingested_listings.db` — `listings`:** durable ingested rows; unique key on **normalized `url`**; retains `raw_json`.
- **`updated_listings.db` — `listings`:** same logical listing rows plus **layer columns** (`visual_score`, flags, audit timestamps). Rebuilt from ingested each run at Layer 0 so filters do not stack incorrectly.
- **`final_deals.db`:**
  - **`deals`:** ranked output rows (scores, components, listing identifiers / URLs as defined in `create_final_schema`).
  - **`neighborhoods` (or equivalent stats table):** aggregated **neighborhood_stats** (median PSQM, counts, tier anchors) consumed by the ranker and UI/analytics.

**Relationship (conceptual):** each **deal** in `final_deals.db` refers to one surviving listing identity (via `listing_id` / `url`) and joins logically to **`neighborhoods`** snapshot rows on **canonical neighborhood** for display (counts + optional baseline PSQM). **Tier** feeds the location score only; thin neighborhoods do not inherit a synthetic PSQM baseline.

**Source of truth for `CREATE TABLE`:** `db_helpers/core.py` (ingested), `db_conductor.setup_layer0_tables` / `_ensure_layer1_columns` (updated), `db_conductor.create_final_schema` (final). Do not duplicate full DDL in this doc.

## Performance — AI throughput

- **Batch / parallel auditor:** Layer 1 should **batch work** (concurrent downloads + bounded concurrency to Ollama) to maximize throughput without saturating VRAM or tripping rate limits — tune semaphores and worker pools in `layer_1_quality_audit.py` / conductor, not one HTTP call per process fork.

## Imports (high level)

- `main` → `run_all`, ingest, `ai_conductor`, `scoring_conductor`
- AI + **`scoring_conductor`** → **`db_helpers/db_conductor`** for connections/writes.
- **`rank_deals`** (pure scoring) consumes `sqlite3.Row` objects **supplied by** `db_conductor` — it must not open its own DB files.

## Design choices

| Topic | Choice |
|-------|--------|
| Three DBs | Ingested = durable input; updated = mutable scratch per run; final = regenerated output |
| Idempotent ingest | Upsert on normalized `url` |
| Ollama | Local Moondream — structured JSON protocol (see coding rules) |
| Baselines | Trimmed mean PSQM; floor-adjusted; **min sample 10 per hood** — excluded hoods have no price anchor (weights renormalized in `rank_deals`) |

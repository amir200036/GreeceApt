# GreeceApt — Coding Rules

## Language and Style

- Python 3.11+. Use `X | Y` unions, `list[T]` / `dict[K,V]` generics — no `from typing import Optional/Dict/List`.
- `from __future__ import annotations` is used in pipeline files — keep it there.
- Private module-level constants use a leading underscore (e.g., `_FLOOR_TEXT_MAP`, `_ORDINAL_RE`).
- No docstrings on trivial functions. Add docstrings only when the logic is non-obvious.
- Add comments on SQL blocks and scoring formulas — these are complex and need explanation.
- No backwards-compatibility shims (renaming unused vars, re-exporting removed names, etc.).

---

## Database Rules

### ingested_listings.db (raw store)
- Schema lives in `db_helpers/core.py` — `create_tables()` creates it with `CREATE TABLE IF NOT EXISTS`.
- Inserts use `INSERT OR REPLACE` keyed on `url TEXT UNIQUE` (normalized URL).
- `normalize_xe_item()` maps raw scraper dicts to DB row format — all field mapping happens here.
- Never skip URL normalization. Call `normalize_listing_url()` from `utils/helpers.py` on every URL before insert.
- `raw_json` column stores the full original dict as JSON — preserve it for debugging.

### updated_listings.db (pipeline working DB)
- Built fresh every run (`UPDATED_DB.unlink()` first in `copy_ingested_to_updated`) — never accumulate.
- Source DB is opened read-only (`uri=True`, `mode=ro`).
- All columns are stored as TEXT — use explicit `CAST(col AS REAL)` / `CAST(col AS INTEGER)` in queries.
- Column presence is always checked via `PRAGMA table_info` before use — no assumptions.
- Filtered rows go to `removed_listings` table with `removal_reason` and `layer_origin` columns.

### final_deals.db (scored output)
- Always deleted and recreated by `scoring_conductor.py` via `reset_final_db()`.
- Contains `deals` table (one row per scored listing) and `neighborhoods` table.

---

## Neighborhood / Location Rules

- **neighborhood**: canonical name only — no prefixes, no junk.
- `resolve_neighborhood()` in `utils/helpers.py` handles compound names split across API fields (e.g., municipality="Nea", area="Smyrni" → "Nea Smyrni").
- **Agia / Agios / Agioi are NOT prefixes** — never strip them from the neighborhood name.
- All neighborhood tier mappings live in `NEIGHBORHOOD_CANONICAL` in `scoring/scoring_algorithm.py`.

---

## Scraper Rules

- Never change the `impersonate="chrome124"` setting on the curl-cffi session.
- Concurrent detail fetches are limited to 3 via `asyncio.Semaphore` — do not increase.
- On 403/405/429: refresh cookies and back off. On 405: use a 45s×attempt backoff (server flagged the session).
- `photos_count` falls back to `len(photo_urls)` if the API field is missing.
- URL deduplication is done by normalized path (query params stripped by `normalize_listing_url`).

---

## Scoring Rules

- Weights must sum to 1.00: `W_PRICE=0.45`, `W_HOOD=0.20`, `W_VISUAL=0.20`, `W_STRUCT=0.15`.
- `_VISUAL_FALLBACK = 5.0` is used when a listing has no visual_score (not yet audited).
- `visual_quality` is read directly from the `visual_score` column of the row — no separate DB lookup.
- All score components return 0.0–1.0; multiply by 100 at the end.

---

## Market Computation Rules

- Uses 10%-trimmed mean (`TRIM_PCT = 0.10`) — top and bottom 10% of PSQM values per neighborhood are excluded.
- Floor multipliers normalize listings to a "1st-floor equivalent" before computing the baseline.
- Neighborhoods with no listings are simply absent from the baselines dict — listings in those hoods are skipped in scoring.

---

## What Not to Do

- Do not add error handling for internal invariants — if a key is missing it should crash loudly.
- Do not add features not requested — no extra columns, no new export formats, no optional flags.
- Do not create new files for one-time logic — extend an existing module.
- Do not remove `raw_json` from the listings table — it is the audit trail.
- Do not call sqlite3 directly from pipeline layer files — use `db_conductor.py` instead.

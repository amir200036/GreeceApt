"""Filesystem locations for all pipeline SQLite databases and the data directory."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"

INGESTED_DB = DATA_DIR / "ingested_listings.db"
UPDATED_DB = DATA_DIR / "updated_listings.db"
FINAL_DB = DATA_DIR / "final_deals.db"

# Merge + scraper JSON / cookies (single source for subprocesses and pipeline)
LISTINGS_JSON = DATA_DIR / "listings.json"
DELETED_LISTINGS_JSON = DATA_DIR / "deleted_listings.json"
STALE_LISTINGS_JSON = DATA_DIR / "stale_listings.json"
XE_LISTINGS_JSON = DATA_DIR / "xe_listings.json"
SPITOGATOS_LISTINGS_JSON = DATA_DIR / "spitogatos_listings.json"
COOKIES_JSON = DATA_DIR / "cookies.json"
SPITOGATOS_COOKIES_JSON = DATA_DIR / "spito_cookies.json"
OBSCURA_PATH = PROJECT_ROOT / "obscura"


def ensure_data_dir() -> None:
    """Create ``data/`` under the project root if missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

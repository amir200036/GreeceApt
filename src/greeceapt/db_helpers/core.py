"""
Listings DB: schema, connection, and insert logic.
Stores scraped XE.gr listings in data/ingested_listings.db.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from greeceapt.utils.helpers import normalize_listing_url

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "ingested_listings.db"


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def create_tables() -> None:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            headline TEXT,
            price_eur REAL,
            price_per_sqm REAL,
            area_sqm REAL,
            neighborhood TEXT,
            address_raw TEXT,
            bedrooms INTEGER,
            bathrooms INTEGER,
            floor INTEGER,
            year_built INTEGER,
            renovation_year INTEGER,
            energy_class TEXT,
            photos_count INTEGER,
            photo_urls_json TEXT,
            publication_date TEXT,
            scraped_at TEXT,
            raw_json TEXT,
            updated_at TEXT,
            neighborhood_score INTEGER,
            visual_score INTEGER,
            aesthetic_grade REAL
        )
        """)

        existing = {row[1] for row in c.execute("PRAGMA table_info(listings)")}
        for col, coltype in [
            ("headline", "TEXT"),
            ("price_per_sqm", "REAL"),
            ("visual_score", "REAL"),
            ("aesthetic_grade", "REAL"),
        ]:
            if col not in existing:
                c.execute(f"ALTER TABLE listings ADD COLUMN {col} {coltype}")

        # Migration: drop legacy area column (superseded by full neighborhood name)
        if "area" in existing:
            c.execute("ALTER TABLE listings DROP COLUMN area")

        c.execute("""
        CREATE TABLE IF NOT EXISTS neighborhoods (
            name          TEXT PRIMARY KEY,
            listing_count INTEGER NOT NULL DEFAULT 0,
            first_seen    TEXT,
            last_seen     TEXT
        )
        """)

        conn.commit()
    finally:
        conn.close()
    logger.info("listings table ready.")


def normalize_xe_item(raw: dict) -> dict:
    """Convert raw scraper dict to DB row format."""
    url = normalize_listing_url(raw.get("url"))
    photo_urls = raw.get("photo_urls") or []
    if not isinstance(photo_urls, list):
        photo_urls = []
    return {
        "url": url,
        "headline": raw.get("Headline"),
        "price_eur": raw.get("price_eur"),
        "price_per_sqm": raw.get("price_per_sqm"),
        "area_sqm": raw.get("area_sqm"),
        "neighborhood": raw.get("neighborhood"),
        "address_raw": raw.get("address_raw"),
        "bedrooms": raw.get("bedrooms"),
        "bathrooms": raw.get("bathrooms"),
        "floor": raw.get("floor"),
        "year_built": raw.get("year_built"),
        "renovation_year": raw.get("renovation_year"),
        "energy_class": raw.get("energy_class"),
        "photos_count": raw.get("photos_count"),
        "photo_urls_json": json.dumps(photo_urls, ensure_ascii=False),
        "publication_date": raw.get("publication_date"),
        "scraped_at": raw.get("scraped_at"),
        "raw_json": json.dumps(raw, ensure_ascii=False),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "neighborhood_score": None,
    }


def refresh_neighborhoods(conn: sqlite3.Connection) -> int:
    """Rebuild the neighborhoods summary from scratch using the full listings table."""
    conn.execute("DELETE FROM neighborhoods")
    conn.execute("""
        INSERT INTO neighborhoods (name, listing_count, first_seen, last_seen)
        SELECT
            neighborhood,
            COUNT(*),
            MIN(scraped_at),
            MAX(scraped_at)
        FROM listings
        WHERE neighborhood IS NOT NULL AND TRIM(neighborhood) != ''
        GROUP BY neighborhood
    """)
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM neighborhoods").fetchone()[0]


def insert_listings(items: list[dict]) -> None:
    """Insert or replace a batch of listings in a single transaction."""
    create_tables()
    inserted = skipped = 0
    conn = get_connection()
    try:
        c = conn.cursor()
        for raw in items:
            item = normalize_xe_item(raw)
            if not item.get("url"):
                skipped += 1
                continue
            c.execute("""
                INSERT OR REPLACE INTO listings (
                    url, headline, price_eur, price_per_sqm, area_sqm,
                    neighborhood, address_raw,
                    bedrooms, bathrooms, floor, year_built, renovation_year, energy_class,
                    photos_count, photo_urls_json, publication_date, scraped_at,
                    raw_json, updated_at, neighborhood_score
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                item["url"], item.get("headline"), item["price_eur"], item.get("price_per_sqm"),
                item["area_sqm"], item["neighborhood"], item["address_raw"],
                item["bedrooms"], item["bathrooms"], item["floor"], item["year_built"],
                item["renovation_year"], item["energy_class"], item["photos_count"],
                item["photo_urls_json"], item["publication_date"], item["scraped_at"],
                item["raw_json"], item["updated_at"], item["neighborhood_score"],
            ))
            inserted += 1
        conn.commit()
        hood_count = refresh_neighborhoods(conn)
        logger.info("neighborhoods table: %s distinct neighborhoods.", hood_count)
    finally:
        conn.close()
    logger.info("Inserted %s listings (skipped %s with missing url).", inserted, skipped)

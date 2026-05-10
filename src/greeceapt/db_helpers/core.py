"""
Listings DB: schema, connection, and insert logic.
Stores scraped XE.gr listings in data/ingested_listings.db.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone

from greeceapt.db_helpers import paths
from greeceapt.db_helpers.util import normalize_listing_url, resolve_neighborhood

logger = logging.getLogger(__name__)

paths.ensure_data_dir()

DATA_DIR = paths.DATA_DIR
DB_PATH = paths.INGESTED_DB


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
            title TEXT,
            price_eur REAL,
            price_per_sqm REAL,
            area_sqm REAL,
            municipality TEXT,
            neighborhood TEXT,
            address_raw TEXT,
            bedrooms INTEGER,
            bathrooms INTEGER,
            floor INTEGER,
            year_built INTEGER,
            renovation_year INTEGER,
            energy_class TEXT,
            heating_type TEXT,
            photos_count INTEGER,
            photo_urls_json TEXT,
            publication_date TEXT,
            scraped_at TEXT,
            raw_json TEXT,
            updated_at TEXT,
            neighborhood_score INTEGER,
            visual_score REAL,
            source TEXT
        )
        """)

        existing = {row[1] for row in c.execute("PRAGMA table_info(listings)")}
        for col, coltype in [
            ("title",              "TEXT"),
            ("price_per_sqm",      "REAL"),
            ("municipality",       "TEXT"),
            ("heating_type",       "TEXT"),
            ("visual_score",       "REAL"),
            ("source",             "TEXT"),
        ]:
            if col not in existing:
                c.execute(f"ALTER TABLE listings ADD COLUMN {col} {coltype}")

        # Migration: drop legacy area column (requires SQLite 3.35+)
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

        c.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL,
            old_price_eur REAL,
            new_price_eur REAL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (listing_id) REFERENCES listings(id)
        )
        """)

        conn.commit()
    finally:
        conn.close()
    logger.info("listings table ready.")


def _price_changed(old: object, new: object, *, eps: float = 0.5) -> bool:
    """True when ``new`` should be recorded as a price update vs ``old``."""
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    try:
        return abs(float(old) - float(new)) > eps
    except (TypeError, ValueError):
        return str(old) != str(new)


def _recover_neighborhood(raw: dict) -> tuple[str | None, str | None]:
    """
    Recover neighborhood/municipality from address_raw when scraper left them blank.
    Handles the XE pattern "City (Neighborhood)" stored in address_raw.
    Returns (municipality, neighborhood) — may both be None if not recoverable.
    """
    municipality = raw.get("municipality") or None
    neighborhood = raw.get("neighborhood") or None
    if neighborhood:
        return municipality, neighborhood
    addr_raw = raw.get("address_raw") or ""
    m = re.search(r"\(([^)]+)\)", str(addr_raw).strip())
    if not m:
        return municipality, neighborhood
    area = m.group(1).strip()
    before = addr_raw[: addr_raw.index("(")].strip() or None
    resolved = resolve_neighborhood(before or municipality, area)
    if resolved:
        neighborhood = resolved
        if not municipality and before:
            municipality = before
    return municipality, neighborhood


def normalize_listing(raw: dict) -> dict:
    """Convert a raw scraper dict (XE or Spitogatos) to DB row format."""
    url = normalize_listing_url(raw.get("url"))
    photo_urls = raw.get("photo_urls") or []
    if not isinstance(photo_urls, list):
        photo_urls = []
    municipality, neighborhood = _recover_neighborhood(raw)
    return {
        "url": url,
        "title": raw.get("title"),
        "price_eur": raw.get("price_eur"),
        "price_per_sqm": raw.get("price_per_sqm"),
        "area_sqm": raw.get("area_sqm"),
        "municipality": municipality,
        "neighborhood": neighborhood,
        "address_raw": raw.get("address_raw"),
        "bedrooms": raw.get("bedrooms"),
        "bathrooms": raw.get("bathrooms"),
        "floor": raw.get("floor"),
        "year_built": raw.get("year_built"),
        "renovation_year": raw.get("renovation_year"),
        "energy_class": raw.get("energy_class"),
        "heating_type": raw.get("heating_type"),
        "photos_count": raw.get("photos_count"),
        "photo_urls_json": json.dumps(photo_urls, ensure_ascii=False),
        "publication_date": raw.get("publication_date"),
        "scraped_at": raw.get("scraped_at"),
        "raw_json": json.dumps(raw, ensure_ascii=False),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "neighborhood_score": None,
        "source": raw.get("source"),
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
    """
    Upsert listings by canonical ``url``.

    New rows are inserted. Existing rows are updated in place. If ``price_eur``
    changed, a row is appended to ``price_history`` before the update.
    """
    create_tables()
    inserted = updated = skipped = price_events = 0
    conn = get_connection()
    try:
        c = conn.cursor()
        for raw in items:
            item = normalize_listing(raw)
            if not item.get("url"):
                skipped += 1
                continue
            url = item["url"]
            c.execute("SELECT id, price_eur FROM listings WHERE url = ?", (url,))
            existing = c.fetchone()

            row_vals = (
                item.get("title"), item["price_eur"], item.get("price_per_sqm"),
                item["area_sqm"], item.get("municipality"), item["neighborhood"],
                item["address_raw"], item["bedrooms"], item["bathrooms"], item["floor"],
                item["year_built"], item["renovation_year"], item["energy_class"],
                item.get("heating_type"), item["photos_count"], item["photo_urls_json"],
                item["publication_date"], item["scraped_at"], item["raw_json"],
                item["updated_at"], item["neighborhood_score"], item.get("source"),
            )

            if existing is None:
                c.execute("""
                    INSERT INTO listings (
                        url, title, price_eur, price_per_sqm, area_sqm,
                        municipality, neighborhood, address_raw,
                        bedrooms, bathrooms, floor, year_built, renovation_year,
                        energy_class, heating_type,
                        photos_count, photo_urls_json, publication_date, scraped_at,
                        raw_json, updated_at, neighborhood_score, source
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (url,) + row_vals)
                inserted += 1
                continue

            listing_id, old_price = existing[0], existing[1]
            new_price = item["price_eur"]
            if _price_changed(old_price, new_price):
                def _sql_price(p: object) -> float | None:
                    if p is None:
                        return None
                    try:
                        return float(p)
                    except (TypeError, ValueError):
                        return None

                c.execute(
                    """
                    INSERT INTO price_history (listing_id, old_price_eur, new_price_eur, recorded_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (listing_id, _sql_price(old_price), _sql_price(new_price), item["updated_at"]),
                )
                price_events += 1
                logger.info(
                    "Price change for listing_id=%s url=%s: %s → %s EUR",
                    listing_id, url, old_price, new_price,
                )

            c.execute(
                """
                UPDATE listings SET
                    title = ?, price_eur = ?, price_per_sqm = ?, area_sqm = ?,
                    municipality = ?, neighborhood = ?, address_raw = ?,
                    bedrooms = ?, bathrooms = ?, floor = ?, year_built = ?, renovation_year = ?,
                    energy_class = ?, heating_type = ?,
                    photos_count = ?, photo_urls_json = ?, publication_date = ?, scraped_at = ?,
                    raw_json = ?, updated_at = ?, neighborhood_score = ?, source = ?
                WHERE id = ?
                """,
                row_vals + (listing_id,),
            )
            updated += 1
        conn.commit()
        hood_count = refresh_neighborhoods(conn)
        logger.info("neighborhoods table: %s distinct neighborhoods.", hood_count)
    finally:
        conn.close()
    logger.info(
        "Listings upsert: inserted=%s updated=%s price_history_events=%s (skipped missing url=%s).",
        inserted, updated, price_events, skipped,
    )

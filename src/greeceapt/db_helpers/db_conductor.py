"""
db_conductor.py — Single entry point for all pipeline database interactions.

Covers updated_listings.db (Layer 0, Layer 1 operations) and
final_deals.db (scoring output). Never call sqlite3 directly from
pipeline layers — use this module instead.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"

INGESTED_DB = DATA_DIR / "ingested_listings.db"
UPDATED_DB  = DATA_DIR / "updated_listings.db"
FINAL_DB    = DATA_DIR / "final_deals.db"


# ── Connection factories ──────────────────────────────────────────────────────

def connect_updated() -> sqlite3.Connection:
    return sqlite3.connect(UPDATED_DB)


def connect_updated_readonly() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{UPDATED_DB}?mode=ro", uri=True)


def connect_final_write() -> sqlite3.Connection:
    return sqlite3.connect(FINAL_DB)


# ── DB lifecycle ──────────────────────────────────────────────────────────────

def copy_ingested_to_updated() -> int:
    """Delete updated_listings.db if present and copy listings from ingested_listings.db."""
    if not INGESTED_DB.exists():
        raise FileNotFoundError(f"Source DB not found: {INGESTED_DB}")
    if UPDATED_DB.exists():
        UPDATED_DB.unlink()

    src = sqlite3.connect(f"file:{INGESTED_DB}?mode=ro", uri=True)
    try:
        cols = _table_columns(src, "listings")
        rows = src.execute("SELECT * FROM listings").fetchall()
    finally:
        src.close()

    dst = sqlite3.connect(UPDATED_DB)
    try:
        col_defs = ", ".join(f"{c} TEXT" for c in cols)
        dst.execute(f"CREATE TABLE IF NOT EXISTS listings ({col_defs})")
        placeholders = ", ".join("?" * len(cols))
        col_str = ", ".join(cols)
        dst.executemany(
            f"INSERT OR IGNORE INTO listings ({col_str}) VALUES ({placeholders})",
            rows,
        )
        dst.commit()
    finally:
        dst.close()

    return len(rows)


def reset_final_db() -> None:
    if FINAL_DB.exists():
        FINAL_DB.unlink()


# ── Layer 0: table setup ──────────────────────────────────────────────────────

def setup_layer0_tables(conn: sqlite3.Connection) -> dict[str, bool]:
    """
    Ensure Layer 1 processing columns and removed_listings table exist.
    Returns a dict of which optional source columns are present in listings.
    """
    _ensure_layer1_columns(conn)
    _ensure_removed_table(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    return {
        "has_pub_date":     "publication_date" in cols,
        "has_scraped_at":   "scraped_at"        in cols,
        "has_neighborhood": "neighborhood"      in cols,
    }


def _ensure_layer1_columns(conn: sqlite3.Connection) -> None:
    """Add Layer 1 tracking columns to listings if not already present."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    for col, typedef in [
        ("layer_1_processed", "INTEGER DEFAULT 0"),
        ("visual_score",      "REAL"),
        ("aesthetic_grade",   "REAL"),
        ("layer_1_features",  "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {typedef}")
    conn.commit()


def _ensure_removed_table(conn: sqlite3.Connection) -> None:
    src_cols   = _table_columns(conn, "listings")
    audit_cols = ["removal_reason", "layer_origin"]
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='removed_listings'"
    )
    if not cur.fetchone():
        col_defs = ", ".join(f"{c} TEXT" for c in src_cols + audit_cols)
        cur.execute(f"CREATE TABLE removed_listings ({col_defs})")
    else:
        existing = {row[1] for row in cur.execute("PRAGMA table_info(removed_listings)")}
        for col in src_cols + audit_cols:
            if col not in existing:
                cur.execute(f"ALTER TABLE removed_listings ADD COLUMN {col} TEXT")
    conn.commit()


# ── Layer 0: data access ──────────────────────────────────────────────────────

def get_listings_for_layer0(
    conn: sqlite3.Connection,
    col_info: dict[str, bool],
) -> list[tuple]:
    pub_date_col     = "publication_date" if col_info["has_pub_date"]     else "NULL"
    scraped_at_col   = "scraped_at"       if col_info["has_scraped_at"]   else "NULL"
    neighborhood_col = "neighborhood"     if col_info["has_neighborhood"] else "NULL"
    return conn.execute(
        f"SELECT id, photo_urls_json, {pub_date_col}, {scraped_at_col}, {neighborhood_col} "
        "FROM listings"
    ).fetchall()


def move_listing_to_removed(
    conn: sqlite3.Connection,
    row_id: int | str,
    layer_origin: str,
    reason: str,
) -> bool:
    """Move one listing from listings → removed_listings. Returns True on success."""
    cur = conn.cursor()
    cur.execute("SELECT * FROM listings WHERE id = ?", (row_id,))
    row = cur.fetchone()
    if row is None:
        return False

    cols = _table_columns(conn, "listings")
    all_cols = cols + ["removal_reason", "layer_origin"]
    placeholders = ", ".join("?" * len(all_cols))
    cur.execute(
        f"INSERT INTO removed_listings ({', '.join(all_cols)}) VALUES ({placeholders})",
        list(row) + [reason, layer_origin],
    )
    cur.execute("DELETE FROM listings WHERE id = ?", (row_id,))
    conn.commit()
    return True


def get_surviving_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]


# ── Layer 1: data access ──────────────────────────────────────────────────────

def get_listings_for_layer1(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    """Return (listing_id, url_list) for all listings not yet processed by Layer 1."""
    rows = conn.execute(
        """
        SELECT id, photo_urls_json FROM listings
        WHERE layer_1_processed IS NULL OR CAST(layer_1_processed AS INTEGER) = 0
        """
    ).fetchall()
    result = []
    for lid, photo_urls_json in rows:
        try:
            urls = json.loads(photo_urls_json) if photo_urls_json else []
        except (ValueError, TypeError):
            urls = []
        if urls:
            result.append((str(lid), urls))
    return result


def save_visual_audit_results(
    conn: sqlite3.Connection,
    listing_id: str | int,
    visual_score: float | None,
    features: dict,
) -> None:
    """Persist Layer 1 Moondream results for a listing."""
    conn.execute(
        """
        UPDATE listings
        SET    visual_score       = ?,
               aesthetic_grade   = ?,
               layer_1_processed = 1,
               layer_1_features  = ?
        WHERE  id = ?
        """,
        (
            visual_score,
            visual_score,
            json.dumps(features) if features else None,
            str(listing_id),
        ),
    )
    conn.commit()


def remove_low_aesthetic_listings(  # called by layer_2_aesthetic_filter
    conn: sqlite3.Connection,
    threshold: float,
    layer_name: str = "Layer 2",
) -> int:
    """Move all listings with aesthetic_grade below threshold to removed_listings. Returns count."""
    rows = conn.execute(
        """
        SELECT id, aesthetic_grade FROM listings
        WHERE aesthetic_grade IS NULL
           OR CAST(aesthetic_grade AS REAL) < ?
        """,
        (threshold,),
    ).fetchall()
    removed = 0
    for lid, grade in rows:
        reason = (
            f"Visual score ({float(grade):.2f}) below threshold ({threshold:.1f})"
            if grade is not None
            else "No visual score from Layer 1"
        )
        move_listing_to_removed(conn, lid, layer_name, reason)
        removed += 1
    return removed


def remove_thin_neighborhood_listings(
    conn: sqlite3.Connection,
    min_count: int = 10,
) -> int:
    """Move all listings in neighborhoods with fewer than min_count entries to removed_listings."""
    thin_hoods = conn.execute(
        """
        SELECT neighborhood, COUNT(*) AS cnt
        FROM listings
        WHERE neighborhood IS NOT NULL AND TRIM(neighborhood) != ''
        GROUP BY neighborhood
        HAVING cnt < ?
        """,
        (min_count,),
    ).fetchall()
    removed = 0
    for hood, cnt in thin_hoods:
        rows = conn.execute(
            "SELECT id FROM listings WHERE neighborhood = ?", (hood,)
        ).fetchall()
        for (lid,) in rows:
            move_listing_to_removed(
                conn, lid, "Market Analytics",
                f"Thin neighborhood '{hood}' — only {cnt} listings (min={min_count})",
            )
            removed += 1
    return removed


def get_all_visual_scores(conn: sqlite3.Connection) -> dict[str, float]:
    """Return {listing_id: visual_score} (1.0–10.0 scale) for all listings with a visual score."""
    rows = conn.execute(
        "SELECT id, visual_score FROM listings WHERE visual_score IS NOT NULL"
    ).fetchall()
    return {str(r[0]): float(r[1]) for r in rows}


# ── Scoring: data access ──────────────────────────────────────────────────────

def get_listings_for_baseline(
    conn: sqlite3.Connection,
    min_aesthetic_grade: float,
) -> list[dict]:
    """Return listing dicts for neighborhood baseline computation."""
    rows = conn.execute(
        """
        SELECT neighborhood, price_eur, area_sqm, floor
        FROM listings
        WHERE price_eur      IS NOT NULL
          AND area_sqm       IS NOT NULL
          AND CAST(area_sqm AS REAL) > 0
          AND neighborhood   IS NOT NULL
          AND TRIM(neighborhood) != ''
          AND aesthetic_grade IS NOT NULL
          AND CAST(aesthetic_grade AS REAL) >= ?
        """,
        (min_aesthetic_grade,),
    ).fetchall()
    return [
        {"neighborhood": r[0], "price_eur": r[1], "area_sqm": r[2], "floor": r[3]}
        for r in rows
    ]


def get_all_listings_for_scoring(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM listings").fetchall()


def get_neighborhood_counts(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        """
        SELECT neighborhood, COUNT(*) AS cnt
        FROM listings
        WHERE neighborhood IS NOT NULL AND TRIM(neighborhood) != ''
        GROUP BY neighborhood
        """
    ).fetchall()


# ── Scoring: write ────────────────────────────────────────────────────────────

def create_final_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE deals (
            score       REAL,
            hood        TEXT,
            price       INTEGER,
            area        INTEGER,
            market_diff TEXT,
            floor       TEXT,
            visual      REAL,
            url         TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE neighborhoods (
            name          TEXT PRIMARY KEY,
            listing_count INTEGER,
            median_psqm   REAL,
            tier          TEXT
        )
        """
    )
    conn.commit()


def write_deals(conn: sqlite3.Connection, deals: list[tuple]) -> None:
    conn.executemany("INSERT INTO deals VALUES (?,?,?,?,?,?,?,?)", deals)
    conn.commit()


def write_neighborhoods(conn: sqlite3.Connection, hood_rows: list[tuple]) -> None:
    conn.executemany("INSERT INTO neighborhoods VALUES (?,?,?,?)", hood_rows)
    conn.commit()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _table_columns(conn: sqlite3.Connection, table: str = "listings") -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]

"""
Layer 0 — Metadata Filter (SQL only, no AI).

Removes listings from updated_listings that fail any of three hard filters:
  - Missing neighborhood
  - Fewer than 2 photos
  - Publication date > 180 days ago
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

from greeceapt.db_helpers import db_conductor

MAX_AGE_DAYS = 180
LAYER_NAME   = "Layer 0"
TODAY        = date.today()

logger = logging.getLogger(__name__)


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def run() -> None:
    logger.info("Layer 0: copying ingested_listings.db → updated_listings.db …")
    total = db_conductor.copy_ingested_to_updated()
    logger.info("Layer 0: %s rows copied.", total)

    conn = db_conductor.connect_updated()
    try:
        col_info = db_conductor.setup_layer0_tables(conn)
        rows     = db_conductor.get_listings_for_layer0(conn, col_info)

        removed = 0
        for row_id, photo_urls_json, pub_date, scraped_at, neighborhood in rows:

            # Filter 1: missing neighborhood
            if not neighborhood or not str(neighborhood).strip():
                db_conductor.move_listing_to_removed(
                    conn, row_id, LAYER_NAME, "Missing neighborhood",
                )
                removed += 1
                continue

            # Filter 2: stale listing
            d = _parse_date(pub_date) or _parse_date(scraped_at)
            if d is not None:
                age = (TODAY - d).days
                if age > MAX_AGE_DAYS:
                    db_conductor.move_listing_to_removed(
                        conn, row_id, LAYER_NAME,
                        f"Stale listing (age={age} days, max={MAX_AGE_DAYS})",
                    )
                    removed += 1
                    continue

            # Filter 3: too few photos
            try:
                urls = json.loads(photo_urls_json) if photo_urls_json else []
            except (ValueError, TypeError):
                urls = []
            if len(urls) < 2:
                db_conductor.move_listing_to_removed(
                    conn, row_id, LAYER_NAME, "Insufficient photos (<2)",
                )
                removed += 1
                continue

    finally:
        conn.close()

    logger.info(
        "Layer 0 done. total=%s  removed=%s  passed=%s",
        total, removed, total - removed,
    )


if __name__ == "__main__":
    run()

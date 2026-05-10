"""
Layer 0 — Metadata Filter (SQL only, no AI).

Removes listings from updated_listings that fail any of three hard filters:
  - Missing neighborhood
  - Fewer than 3 photos (2 or fewer URLs after parse)
  - Publication date > 180 days ago
"""

from __future__ import annotations

import logging
from datetime import date

from greeceapt.ai_agent import util as ai_util
from greeceapt.db_helpers import db_conductor
from greeceapt.db_helpers.util import parse_photo_urls_json

MAX_AGE_DAYS = 180
LAYER_NAME   = "Layer 0"
TODAY        = date.today()

logger = logging.getLogger(__name__)


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
            d = ai_util.parse_yyyy_mm_dd_prefix(pub_date) or ai_util.parse_yyyy_mm_dd_prefix(scraped_at)
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
            urls = parse_photo_urls_json(photo_urls_json)
            if len(urls) <= 2:
                db_conductor.move_listing_to_removed(
                    conn, row_id, LAYER_NAME, "Insufficient photos (need at least 3)",
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

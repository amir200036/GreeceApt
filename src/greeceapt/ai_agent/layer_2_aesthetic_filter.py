"""
Layer 2 — Quality & Neighborhood Gate.

Step 1 — Aesthetic filter: remove listings whose visual_score < MIN_AESTHETIC_GRADE.
Step 2 — Neighborhood size filter: remove all listings in any neighborhood that
          has fewer than MIN_NEIGHBORHOOD_SIZE surviving entries. A neighborhood
          with too few data points produces an unreliable price baseline, so every
          listing in it is moved to removed_listings before scoring runs.
"""

from __future__ import annotations

import logging

from greeceapt.db_helpers import db_conductor

MIN_AESTHETIC_GRADE   = 3.0
MIN_NEIGHBORHOOD_SIZE = 10
LAYER_NAME            = "Layer 2"

logger = logging.getLogger(__name__)


def run() -> None:
    conn = db_conductor.connect_updated()
    try:
        removed_aesthetic = db_conductor.remove_low_aesthetic_listings(
            conn, MIN_AESTHETIC_GRADE, LAYER_NAME
        )
        removed_thin = db_conductor.remove_thin_neighborhood_listings(
            conn, MIN_NEIGHBORHOOD_SIZE
        )
    finally:
        conn.close()

    logger.info(
        "Layer 2 done. aesthetic_purge=%s (score < %.1f)  neighborhood_purge=%s (< %d listings).",
        removed_aesthetic, MIN_AESTHETIC_GRADE, removed_thin, MIN_NEIGHBORHOOD_SIZE,
    )


if __name__ == "__main__":
    run()

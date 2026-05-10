"""
scoring_conductor.py — Manages the scoring pipeline.

Flow:
  1. Layer 3 (Market Analytics) — compute neighborhood baselines
  2. Final Deal Ranking         — apply weighted formula, write final_deals.db
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run() -> None:
    from greeceapt.db_helpers import db_conductor
    from greeceapt.scoring.market_analytics import compute_neighborhood_baselines
    from greeceapt.scoring.scoring_algorithm import rank_deals, NEIGHBORHOOD_CANONICAL
    from greeceapt.ai_agent.layer_2_aesthetic_filter import MIN_AESTHETIC_GRADE

    logger.info("=== Scoring Conductor: Layer 3 — Market Analytics ===")
    src_conn = db_conductor.connect_updated_readonly()
    try:
        listing_data = db_conductor.get_listings_for_baseline(src_conn, MIN_AESTHETIC_GRADE)
        baselines, excluded_hoods, min_n = compute_neighborhood_baselines(listing_data)
        for hood in sorted(baselines):
            logger.info("  Neighborhood baseline %s: €%.0f/sqm (normalized)", hood, baselines[hood])
        logger.info(
            "Layer 3: %s neighborhood baselines (strict, n >= %s); "
            "%s neighborhoods excluded (insufficient data: n < %s).",
            len(baselines),
            min_n,
            excluded_hoods,
            min_n,
        )
    finally:
        src_conn.close()

    logger.info("=== Scoring Conductor: Final Deal Ranking ===")
    db_conductor.reset_final_db()

    src_conn = db_conductor.connect_updated_readonly()
    dst_conn = db_conductor.connect_final_write()
    try:
        db_conductor.create_final_schema(dst_conn)

        listings = db_conductor.get_all_listings_for_scoring(src_conn)
        deals    = rank_deals(listings, baselines)
        db_conductor.write_deals(dst_conn, deals)

        hood_counts = db_conductor.get_neighborhood_counts(src_conn)
        hood_rows = []
        for name, cnt in hood_counts:
            name = str(name).strip()
            tier = NEIGHBORHOOD_CANONICAL.get(name)
            avg = baselines.get(name)
            hood_rows.append((name, cnt, round(avg, 1) if avg else None, tier))
        hood_rows.sort(key=lambda r: (r[3] is not None, -r[1]))
        db_conductor.write_neighborhoods(dst_conn, hood_rows)

        unmapped = sum(1 for r in hood_rows if r[3] is None)
        logger.info("%s deals written to final_deals.db", len(deals))
        logger.info(
            "Neighborhoods: %s total (%s unmapped — add to NEIGHBORHOOD_CANONICAL to score them)",
            len(hood_rows), unmapped,
        )
        if deals:
            logger.info("Score range: top=%.1f  bottom=%.1f", deals[0][0], deals[-1][0])
    finally:
        src_conn.close()
        dst_conn.close()


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    run()

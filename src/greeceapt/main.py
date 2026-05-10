"""
main.py — Top-level pipeline orchestrator.

  1. Scrape — ``run_all``: XE + Spitogatos → ``data/listings.json`` (merged).
  2. Ingest — ``listings.json`` → ``data/ingested_listings.db``.
  3. AI — Layer 0 (SQL metadata), Layer 1 (Moondream), Layer 2 (visual + thin-hood gate).
  4. Scoring — baselines + ``rank_deals`` → ``data/final_deals.db``.
"""

from __future__ import annotations

import asyncio
import logging

from greeceapt.logging_config import configure_root_logging

configure_root_logging()
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("=== GreeceApt Pipeline Start ===")

    logger.info("--- Stage 1: Scrape (XE + Spitogatos, deduplicated) ---")
    from greeceapt.scraper.run_all import main as scrape_all
    asyncio.run(scrape_all())

    logger.info("--- Stage 2: Ingest ---")
    from greeceapt.pipeline.ingest import load_json
    from greeceapt.db_helpers import insert_listings
    listings = load_json()
    insert_listings(listings)

    logger.info("--- Stage 3: AI ---")
    from greeceapt.ai_agent.ai_conductor import run as ai_run
    ai_run()

    logger.info("--- Stage 4: Score ---")
    from greeceapt.scoring.scoring_conductor import run as scoring_run
    scoring_run()

    logger.info("=== GreeceApt Pipeline Complete ===")


if __name__ == "__main__":
    main()

"""
Live integration test — 1 page per scraper → merge → ingest → AI (real Moondream) → scoring.

Run explicitly:
    pytest tests/test_live_pipeline.py -v -m live

Requires:
  - Network access to xe.gr and spitogatos.gr
  - Valid cookies at data/cookies.json (for XE)
  - Ollama running locally with moondream model loaded
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import pytest

from greeceapt.ai_agent import ai_conductor
from greeceapt.db_helpers import core as db_core
from greeceapt.db_helpers import db_conductor
from greeceapt.db_helpers import insert_listings
from greeceapt.pipeline import ingest
from greeceapt.scraper import run_all
from greeceapt.scraper.spitogatos import config as spit_cfg
from greeceapt.scraper.spitogatos.scrape import run as spit_run
from greeceapt.scraper.xe import run_scrape_cycle
from greeceapt.scraper.xe import xe_config as xe_cfg
from greeceapt.scoring import scoring_conductor

logger = logging.getLogger(__name__)


@pytest.fixture
def live_tmp(monkeypatch, tmp_path):
    """Route all pipeline outputs to tmp_path; keep real cookies path for XE auth."""
    # XE scraper output
    monkeypatch.setattr(xe_cfg, "LISTINGS_JSON", tmp_path / "xe_listings.json")
    # Spitogatos scraper output + 1-page cap
    monkeypatch.setattr(spit_cfg, "LISTINGS_JSON", tmp_path / "spitogatos_listings.json")
    monkeypatch.setattr(spit_cfg, "MAX_OFFSET", 30)

    # run_all merge I/O
    monkeypatch.setattr(run_all, "XE_JSON", tmp_path / "xe_listings.json")
    monkeypatch.setattr(run_all, "SPITOGATOS_JSON", tmp_path / "spitogatos_listings.json")
    monkeypatch.setattr(run_all, "OUTPUT_JSON", tmp_path / "listings.json")
    monkeypatch.setattr(run_all, "DELETED_JSON", tmp_path / "deleted_listings.json")
    monkeypatch.setattr(run_all, "STALE_JSON", tmp_path / "stale_listings.json")

    # DB paths
    monkeypatch.setattr(db_core, "DB_PATH", tmp_path / "ingested_listings.db")
    monkeypatch.setattr(db_core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_conductor, "INGESTED_DB", tmp_path / "ingested_listings.db")
    monkeypatch.setattr(db_conductor, "UPDATED_DB", tmp_path / "updated_listings.db")
    monkeypatch.setattr(db_conductor, "FINAL_DB", tmp_path / "final_deals.db")
    monkeypatch.setattr(db_conductor, "DATA_DIR", tmp_path)

    # AI conductor log dir
    monkeypatch.setattr(ai_conductor, "DATA_DIR", tmp_path)

    yield tmp_path


@pytest.mark.live
def test_live_one_page_pipeline(live_tmp, monkeypatch):
    tmp = live_tmp

    # ── Stage 1a: XE scraper (1 page) ────────────────────────────────────────
    logger.info("=== Stage 1a: XE scraper (max_pages=1) ===")
    asyncio.run(run_scrape_cycle(max_pages=1))

    xe_json = tmp / "xe_listings.json"
    assert xe_json.exists(), "XE scraper did not write output file"
    xe_listings = json.loads(xe_json.read_text(encoding="utf-8"))
    assert isinstance(xe_listings, list)
    logger.info("XE: %d listings scraped", len(xe_listings))

    # ── Stage 1b: Spitogatos scraper (1 page = MAX_OFFSET=30) ────────────────
    logger.info("=== Stage 1b: Spitogatos scraper (MAX_OFFSET=30) ===")
    asyncio.run(spit_run())

    spit_json = tmp / "spitogatos_listings.json"
    assert spit_json.exists(), "Spitogatos scraper did not write output file"
    spit_listings = json.loads(spit_json.read_text(encoding="utf-8"))
    assert isinstance(spit_listings, list)
    logger.info("Spitogatos: %d listings scraped", len(spit_listings))

    total_scraped = len(xe_listings) + len(spit_listings)
    logger.info("Total scraped across both portals: %d", total_scraped)

    # ── Stage 2: Merge + deduplicate ─────────────────────────────────────────
    logger.info("=== Stage 2: Merge + deduplicate ===")
    fresh, stale, merged_pairs = asyncio.run(run_all._merge())
    logger.info(
        "Merge: %d fresh, %d stale, %d merged pairs",
        len(fresh), len(stale), len(merged_pairs),
    )

    (tmp / "listings.json").write_text(
        json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (tmp / "deleted_listings.json").write_text(
        json.dumps(merged_pairs, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (tmp / "stale_listings.json").write_text(
        json.dumps(stale, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    assert (tmp / "listings.json").exists()
    assert len(fresh) <= total_scraped  # dedup can only reduce count

    # ── Stage 3: Ingest ───────────────────────────────────────────────────────
    logger.info("=== Stage 3: Ingest → ingested_listings.db ===")
    db_core.create_tables()
    listings = ingest.load_json(str(tmp / "listings.json"))
    assert isinstance(listings, list)
    insert_listings(listings)

    ing = sqlite3.connect(tmp / "ingested_listings.db")
    try:
        n_ingested = ing.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    finally:
        ing.close()
    logger.info("Ingested: %d rows", n_ingested)
    assert n_ingested == len(fresh)

    # ── Stage 4: AI (Layer 0 → 1 → 2) ────────────────────────────────────────
    logger.info("=== Stage 4: AI pipeline (Layers 0, 1, 2) ===")
    ai_conductor.run()

    upd = sqlite3.connect(tmp / "updated_listings.db")
    try:
        n_survived = upd.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        n_removed  = upd.execute("SELECT COUNT(*) FROM removed_listings").fetchone()[0]

        # Listings removed before Layer 1 (no neighborhood / bad date / too few photos)
        n_l0_removed = upd.execute(
            "SELECT COUNT(*) FROM removed_listings WHERE layer_origin = 'Layer 0'"
        ).fetchone()[0]

        # Listings that went through Layer 1 = all ingested minus Layer-0 rejects
        n_l1_eligible = n_ingested - n_l0_removed

        # Scores in the surviving listings table
        n_scored_surviving = upd.execute(
            "SELECT COUNT(*) FROM listings WHERE visual_score IS NOT NULL"
        ).fetchone()[0]

        # Scores preserved in removed_listings for non-Layer-0 removals
        # (Layer 2 moves rows AFTER Layer 1 has already written visual_score)
        n_post_l0_removed = upd.execute(
            "SELECT COUNT(*) FROM removed_listings WHERE layer_origin != 'Layer 0'"
        ).fetchone()[0]
        n_post_l0_removed_scored = upd.execute(
            "SELECT COUNT(*) FROM removed_listings "
            "WHERE layer_origin != 'Layer 0' AND visual_score IS NOT NULL"
        ).fetchone()[0]

        n_total_scored = n_scored_surviving + n_post_l0_removed_scored
    finally:
        upd.close()

    logger.info(
        "AI: %d ingested → %d Layer-0 removed → %d Layer-1 eligible → %d scored → %d survived",
        n_ingested, n_l0_removed, n_l1_eligible, n_total_scored, n_survived,
    )

    assert n_survived + n_removed == n_ingested
    # Every listing that reached Layer 1 must have received a visual score.
    assert n_total_scored == n_l1_eligible, (
        f"Layer-1 scoring incomplete: eligible={n_l1_eligible} scored={n_total_scored} "
        f"(surviving scored={n_scored_surviving}, post-L0-removed scored={n_post_l0_removed_scored})"
    )

    # ── Stage 5: Scoring ──────────────────────────────────────────────────────
    logger.info("=== Stage 5: Scoring → final_deals.db ===")
    scoring_conductor.run()

    assert (tmp / "final_deals.db").exists(), "Scoring did not create final_deals.db"
    fin = sqlite3.connect(tmp / "final_deals.db")
    try:
        n_deals = fin.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        n_hoods = fin.execute("SELECT COUNT(*) FROM neighborhoods").fetchone()[0]
        top_scores = fin.execute(
            "SELECT score, hood, price, url FROM deals ORDER BY score DESC LIMIT 3"
        ).fetchall()
    finally:
        fin.close()

    logger.info("Scoring: %d deals, %d neighborhoods", n_deals, n_hoods)
    for score, hood, price, url in top_scores:
        logger.info("  Top deal: score=%.1f  hood=%s  price=%s  url=%s", score, hood, price, url)

    assert n_deals == n_survived
    assert n_hoods >= 0

    logger.info(
        "=== Live pipeline OK — XE=%d  Spit=%d  fresh=%d  ingested=%d  "
        "survived=%d  removed=%d  deals=%d ===",
        len(xe_listings), len(spit_listings), len(fresh),
        n_ingested, n_survived, n_removed, n_deals,
    )

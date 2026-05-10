"""
End-to-end pipeline test: both portal JSONs → merge → ingest → AI → scoring.

Layer 1 is stubbed (no Ollama); merge pHash step avoids HTTP. Layer 2’s
``MIN_NEIGHBORHOOD_SIZE`` is lowered so two listings in one hood are not purged
before scoring. Layer 3 baseline computation is stubbed (real baselines require
min 10 listings per neighborhood); scoring then runs and writes ``final_deals.db``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from PIL import Image
import imagehash

from greeceapt.ai_agent import ai_conductor
from greeceapt.ai_agent import layer_2_aesthetic_filter as layer2
from greeceapt.db_helpers import core as db_core
from greeceapt.db_helpers import db_conductor
from greeceapt.db_helpers import insert_listings
from greeceapt.pipeline import ingest
from greeceapt.scraper import run_all
from greeceapt.scraper import util as scraper_util
from greeceapt.scoring import market_analytics
from greeceapt.scoring import scoring_conductor


def _stub_neighborhood_baselines(*_args, **_kwargs):
    """Match ``compute_neighborhood_baselines`` return shape; Pagkrati anchor for e2e rows."""
    return ({"Pagkrati": 4500.0}, 0, 10)


def _listing(url: str, source: str, price_eur: int, *, area_sqm: float = 55.0) -> dict:
    """Minimal valid row for merge + Layer 0 (neighborhood, fresh date, ≥3 photo URLs)."""
    today = date.today().isoformat()
    suffix = url.rsplit("/", 1)[-1]
    return {
        "url": url,
        "title": f"e2e {source}",
        "price_eur": price_eur,
        "area_sqm": area_sqm,
        "floor": 2,
        "year_built": 2015,
        "energy_class": "B",
        "neighborhood": "Pagkrati",
        "municipality": "Athens",
        "publication_date": today,
        "scraped_at": f"{today}T12:00:00+00:00",
        "photos_count": 3,
        "photo_urls": [
            f"https://example.invalid/e2e/{suffix}/1.jpg",
            f"https://example.invalid/e2e/{suffix}/2.jpg",
            f"https://example.invalid/e2e/{suffix}/3.jpg",
        ],
        "source": source,
    }


@pytest.fixture
def pipeline_tmp(monkeypatch, tmp_path: Path):
    """Route DBs, merge I/O, and Layer 1 logs to tmp_path."""
    monkeypatch.setattr(db_core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_core, "DB_PATH", tmp_path / "ingested_listings.db")
    monkeypatch.setattr(db_conductor, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_conductor, "INGESTED_DB", tmp_path / "ingested_listings.db")
    monkeypatch.setattr(db_conductor, "UPDATED_DB", tmp_path / "updated_listings.db")
    monkeypatch.setattr(db_conductor, "FINAL_DB", tmp_path / "final_deals.db")

    monkeypatch.setattr(run_all, "DATA_DIR", tmp_path)
    monkeypatch.setattr(run_all, "OUTPUT_JSON", tmp_path / "listings.json")
    monkeypatch.setattr(run_all, "DELETED_JSON", tmp_path / "deleted_listings.json")
    monkeypatch.setattr(run_all, "STALE_JSON", tmp_path / "stale_listings.json")
    monkeypatch.setattr(run_all, "XE_JSON", tmp_path / "xe_listings.json")
    monkeypatch.setattr(run_all, "SPITOGATOS_JSON", tmp_path / "spitogatos_listings.json")

    monkeypatch.setattr(ai_conductor, "DATA_DIR", tmp_path)
    yield tmp_path


async def _fake_compute_listing_photo_hashes(
    photo_urls: list[str],
    url_cache: dict[str, str],
    sem,
    client,
) -> dict[str, str]:
    """Deterministic pHashes without HTTP (merge dedupe stays per-listing)."""
    out: dict[str, str] = {}
    for u in photo_urls:
        if u in url_cache:
            out[u] = url_cache[u]
            continue
        rgb = (abs(hash(u)) % 220 + 20, (abs(hash(u)) >> 8) % 220, (abs(hash(u)) >> 16) % 220)
        im = Image.new("RGB", (32, 32), color=rgb)
        h = str(imagehash.phash(im))
        out[u] = h
        url_cache[u] = h
    return out


def _stub_run_layer1(max_workers: int = 2) -> None:
    """Skip Moondream; assign a passing visual score to every Layer 1 work item."""
    conn = db_conductor.connect_updated()
    try:
        for lid, _urls in db_conductor.get_listings_for_layer1(conn):
            db_conductor.save_visual_audit_results(conn, lid, 8.0, {})
    finally:
        conn.close()


def test_merge_ingest_ai_scoring_full_pipeline(monkeypatch, pipeline_tmp: Path):
    monkeypatch.setattr(scraper_util, "compute_listing_photo_hashes", _fake_compute_listing_photo_hashes)
    monkeypatch.setattr(ai_conductor, "run_layer1", _stub_run_layer1)
    monkeypatch.setattr(market_analytics, "compute_neighborhood_baselines", _stub_neighborhood_baselines)
    # Production MIN_NEIGHBORHOOD_SIZE=10 would purge tiny test hoods before scoring.
    monkeypatch.setattr(layer2, "MIN_NEIGHBORHOOD_SIZE", 1)

    xe_listing = _listing("https://www.xe.gr/property/e2e-xe-1", "xe", 195_000, area_sqm=55.0)
    # Distinct area vs XE so Quad-Lock does not merge if pHashes collide (same metadata + similar images).
    spit_listing = _listing(
        "https://www.spitogatos.gr/en/property/e2e-spit-1", "spitogatos", 210_000, area_sqm=72.0
    )

    pipeline_tmp.mkdir(parents=True, exist_ok=True)
    (pipeline_tmp / "xe_listings.json").write_text(
        json.dumps([xe_listing], ensure_ascii=False),
        encoding="utf-8",
    )
    (pipeline_tmp / "spitogatos_listings.json").write_text(
        json.dumps([spit_listing], ensure_ascii=False),
        encoding="utf-8",
    )

    fresh, stale, merged = asyncio.run(run_all._merge())
    assert len(fresh) == 2
    assert not stale

    with run_all.OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(fresh, f, indent=2, ensure_ascii=False)
    with run_all.DELETED_JSON.open("w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    with run_all.STALE_JSON.open("w", encoding="utf-8") as f:
        json.dump(stale, f, indent=2, ensure_ascii=False)

    listings = ingest.load_json(str(run_all.OUTPUT_JSON))
    assert {row["source"] for row in listings} == {"xe", "spitogatos"}

    db_core.create_tables()
    insert_listings(listings)

    ing = sqlite3.connect(pipeline_tmp / "ingested_listings.db")
    try:
        assert ing.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2
    finally:
        ing.close()

    ai_conductor.run()

    upd = sqlite3.connect(pipeline_tmp / "updated_listings.db")
    try:
        n = upd.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        assert n == 2
        scored = upd.execute(
            "SELECT COUNT(*) FROM listings WHERE visual_score IS NOT NULL AND layer_1_processed = 1"
        ).fetchone()[0]
        assert scored == 2
    finally:
        upd.close()

    scoring_conductor.run()

    assert db_conductor.FINAL_DB.exists()
    fin = sqlite3.connect(pipeline_tmp / "final_deals.db")
    try:
        deal_n = fin.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        hood_n = fin.execute("SELECT COUNT(*) FROM neighborhoods").fetchone()[0]
        assert deal_n == 2
        assert hood_n >= 1
        urls = {row[0] for row in fin.execute("SELECT url FROM deals")}
        assert "https://www.xe.gr/property/e2e-xe-1" in urls
        assert "https://www.spitogatos.gr/en/property/e2e-spit-1" in urls
    finally:
        fin.close()

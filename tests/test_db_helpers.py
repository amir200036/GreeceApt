"""Tests for greeceapt.db_helpers (core + db_conductor + util)."""

import json
import sqlite3

import pytest

from greeceapt.db_helpers import core as db_core
from greeceapt.db_helpers import db_conductor
from greeceapt.db_helpers.util import (
    coerce_sql_float,
    coerce_sql_int,
    normalize_listing_url,
    parse_photo_urls_json,
    resolve_neighborhood,
    row_get,
)


def test_normalize_listing_url_strips_query():
    url = "https://www.xe.gr/property/123?utm=1#frag"
    assert normalize_listing_url(url) == "https://www.xe.gr/property/123"


def test_normalize_listing_url_empty():
    assert normalize_listing_url(None) is None
    assert normalize_listing_url("") is None


def test_resolve_neighborhood_athens_prefers_area():
    assert resolve_neighborhood("Athens", "Pagkrati") == "Pagkrati"


def test_resolve_neighborhood_compound_nea_smyrni():
    assert resolve_neighborhood("Nea", "Smyrni") == "Nea Smyrni"


def test_parse_photo_urls_json():
    assert parse_photo_urls_json(None) == []
    assert parse_photo_urls_json("[]") == []
    assert parse_photo_urls_json('["https://a"]') == ["https://a"]
    assert parse_photo_urls_json("{") == []


def test_row_get_and_coerce():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (price_eur TEXT, year_built TEXT)")
    conn.execute("INSERT INTO t VALUES ('120000', '2010')")
    row = conn.execute("SELECT * FROM t").fetchone()
    assert coerce_sql_float(row_get(row, "price_eur")) == 120_000.0
    assert coerce_sql_int(row_get(row, "year_built")) == 2010
    assert row_get(row, "missing") is None
    conn.close()


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """Route all pipeline DB paths to a temporary directory."""
    monkeypatch.setattr(db_core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_core, "DB_PATH", tmp_path / "ingested_listings.db")
    monkeypatch.setattr(db_conductor, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_conductor, "INGESTED_DB", tmp_path / "ingested_listings.db")
    monkeypatch.setattr(db_conductor, "UPDATED_DB", tmp_path / "updated_listings.db")
    monkeypatch.setattr(db_conductor, "FINAL_DB", tmp_path / "final_deals.db")
    yield tmp_path


def test_normalize_listing_json_roundtrip():
    raw = {
        "url": "https://example.com/l/1?q=1",
        "title": "Test",
        "price_eur": 100_000,
        "area_sqm": 50.0,
        "neighborhood": "Pagkrati",
        "photo_urls": ["https://img/a.jpg", "https://img/b.jpg"],
        "photos_count": 2,
        "source": "xe",
    }
    row = db_core.normalize_listing(raw)
    assert row["url"] == "https://example.com/l/1"
    assert "photo_urls" not in row
    urls = json.loads(row["photo_urls_json"])
    assert len(urls) == 2
    assert json.loads(row["raw_json"])["title"] == "Test"


def test_insert_listings_skips_missing_url(isolated_db):
    db_core.create_tables()
    db_core.insert_listings([{"url": None, "price_eur": 1}, {"url": "", "price_eur": 2}])
    conn = sqlite3.connect(db_core.DB_PATH)
    try:
        n = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_insert_listings_persists(isolated_db):
    db_core.insert_listings(
        [
            {
                "url": "https://xe.gr/x/1",
                "title": "A",
                "price_eur": 200_000,
                "area_sqm": 55,
                "neighborhood": "Pagkrati",
                "photos_count": 3,
                "photo_urls": ["a", "b", "c"],
                "scraped_at": "2026-01-01",
            }
        ]
    )
    conn = sqlite3.connect(db_core.DB_PATH)
    try:
        row = conn.execute("SELECT url, neighborhood FROM listings").fetchone()
    finally:
        conn.close()
    assert row[0] == "https://xe.gr/x/1"
    assert row[1] == "Pagkrati"


def test_insert_listings_price_history_on_change(isolated_db):
    base = {
        "url": "https://xe.gr/x/price",
        "title": "P",
        "price_eur": 100_000,
        "area_sqm": 40,
        "neighborhood": "Pagkrati",
        "photos_count": 0,
        "photo_urls": [],
        "scraped_at": "2026-01-01",
    }
    db_core.insert_listings([base])
    db_core.insert_listings([{**base, "price_eur": 105_000, "scraped_at": "2026-01-02"}])
    conn = sqlite3.connect(db_core.DB_PATH)
    try:
        n_hist = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        row = conn.execute("SELECT price_eur FROM listings WHERE url = ?", (base["url"],)).fetchone()
    finally:
        conn.close()
    assert n_hist == 1
    assert row[0] == 105_000


def test_copy_ingested_to_updated(isolated_db):
    db_core.insert_listings(
        [
            {
                "url": "https://xe.gr/x/2",
                "title": "B",
                "price_eur": 150_000,
                "area_sqm": 40,
                "neighborhood": "Pagkrati",
                "photos_count": 1,
                "photo_urls": ["x"],
                "scraped_at": "2026-01-02",
            }
        ]
    )
    n = db_conductor.copy_ingested_to_updated()
    assert n == 1
    conn = sqlite3.connect(db_conductor.UPDATED_DB)
    try:
        assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    finally:
        conn.close()

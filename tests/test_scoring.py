"""Tests for greeceapt.scoring."""

import sqlite3

import pytest

from greeceapt.scoring.market_analytics import compute_neighborhood_baselines, floor_multiplier
from greeceapt.scoring.scoring_algorithm import rank_deals
from greeceapt.scoring.util import trimmed_mean


def test_trimmed_mean_symmetric():
    assert trimmed_mean([1.0, 2.0, 3.0, 100.0], 0.25) == 2.5


def test_floor_multiplier_bands():
    assert floor_multiplier(None) == 1.0
    assert floor_multiplier(-1) == 1.45
    assert floor_multiplier(0) == 1.10
    assert floor_multiplier(1) == 1.0
    assert floor_multiplier(5) == 0.90


def test_compute_neighborhood_baselines():
    listings = [
        {
            "neighborhood": "HoodA",
            "price_eur": 100_000 + i * 500,
            "area_sqm": 50,
            "floor": 1,
        }
        for i in range(10)
    ]
    hood_bs, excluded, min_n = compute_neighborhood_baselines(listings, trim_pct=0.0)
    assert "HoodA" in hood_bs
    assert hood_bs["HoodA"] > 0
    assert excluded == 0
    assert min_n == 10


def test_compute_neighborhood_baselines_skips_small_hood():
    listings = [
        {
            "neighborhood": "Tiny",
            "price_eur": 100_000,
            "area_sqm": 50,
            "floor": 1,
        }
        for _ in range(5)
    ]
    listings += [
        {
            "neighborhood": f"Other{i}",
            "price_eur": 110_000 + i * 100,
            "area_sqm": 50,
            "floor": 1,
        }
        for i in range(5)
    ]
    hood_bs, excluded, min_n = compute_neighborhood_baselines(listings, trim_pct=0.0)
    assert not hood_bs
    assert excluded == 6
    assert min_n == 10


def test_rank_deals_returns_sorted_rows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE listings (
            price_eur REAL, area_sqm REAL, neighborhood TEXT, municipality TEXT, floor TEXT,
            energy_class TEXT, year_built INTEGER, visual_score REAL, layer_1_features TEXT, url TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO listings VALUES
        (200000, 50, 'Pagkrati', NULL, '1', 'B', 2010, 7.0, NULL, 'https://xe/1'),
        (250000, 50, 'Pagkrati', NULL, '1', 'B', 2010, 7.0, NULL, 'https://xe/2')
        """
    )
    rows = conn.execute("SELECT * FROM listings").fetchall()
    baselines = {"Pagkrati": 4000.0}
    deals = rank_deals(rows, baselines)
    assert len(deals) == 2
    assert deals[0][0] >= deals[1][0]
    conn.close()


def test_rank_deals_without_baseline_omits_price_anchor():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE listings (
            price_eur REAL, area_sqm REAL, neighborhood TEXT, municipality TEXT, floor TEXT,
            energy_class TEXT, year_built INTEGER, visual_score REAL, layer_1_features TEXT, url TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO listings VALUES
        (200000, 50, 'RareHood', 'Metro', '1', 'B', 2010, 8.0,
         '{"interior_quality_avg": 8.0, "is_renovated": true}', 'https://xe/r1')
        """
    )
    rows = conn.execute("SELECT * FROM listings").fetchall()
    deals = rank_deals(rows, {})
    assert len(deals) == 1
    assert deals[0][4] == "n/a"
    assert deals[0][6] == pytest.approx(8.3)  # renovated bump in _effective_visual_quality
    conn.close()

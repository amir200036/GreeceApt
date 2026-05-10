"""
scoring_algorithm.py — Final Deal Ranking.

Applies the weighted formula to rank listings by investment score (0–100):

  45%  Price Arbitrage  — V_index vs strict neighborhood baseline only; if the hood has
      no baseline (Layer 3 sample n < 10), this **location_price** leg is omitted and
      the remaining weights are renormalized (no municipality or broader fallback).
  20%  Location Tier    — N_score: strategic tier rating
  20%  Visual Quality   — Layer 1 structured interior score (JSON), else ``visual_score`` fallback
  15%  Structural Index — mean of Size, Energy class, and Year components

Receives normalized baselines from market_analytics (Layer 3).
Receives visual quality from Layer 1 (structured JSON + ``visual_score`` fallback).
Does NOT compute market averages — that is Layer 3's responsibility.

Greek construction milestones (year component):
  Post-2015=1.0, 2006–2015=0.9, 1982–2005=0.7, 1970–1981=0.5, Pre-1970=0.3
"""

from __future__ import annotations

import json
import logging
import sqlite3

from greeceapt.db_helpers.util import coerce_sql_float, coerce_sql_int, row_get
from greeceapt.scoring.market_analytics import floor_multiplier

logger = logging.getLogger(__name__)

# ── Neighborhood tier scores ──────────────────────────────────────────────────

NEIGHBORHOOD_SCORES: dict[str, float] = {
    "Tier_5_Elite_BlueChip":    98.0,  
    "Tier_4_Hip_Urban":         88.0,  
    "Tier_3_Prime_Residential": 82.0, 
    "Tier_2_Growth_Hub":        65.0,  
    "Tier_1_Budget_Central":    45.0,  
}
DEFAULT_HOOD_SCORE = 60.0

NEIGHBORHOOD_CANONICAL: dict[str, str | None] = {
    # --- Tier 5: Elite & Maximum Safety (98.0) ---
    "Plaka":             "Tier_5_Elite_BlueChip",  # tourist core, peak safety/demand
    "Kolonaki":          "Tier_5_Elite_BlueChip",
    "Lycabettus":        "Tier_5_Elite_BlueChip",
    "Koukaki":           "Tier_5_Elite_BlueChip",
    "Kynosargous":       "Tier_5_Elite_BlueChip",  # next to Koukaki, strong demand
    "Mets":              "Tier_5_Elite_BlueChip",
    "First Cemetery":    "Tier_5_Elite_BlueChip",  # Mets-adjacent, premium & quiet
    "Hilton":            "Tier_5_Elite_BlueChip",

    # --- Tier 4: Hip & High Demand (88.0) ---
    "Pagkrati":          "Tier_4_Hip_Urban",
    "Varnava":           "Tier_4_Hip_Urban",  # Pagkrati core, high demand
    "Ano Petralona":     "Tier_4_Hip_Urban",  # desirable pocket
    "Kato Petralona":    "Tier_4_Hip_Urban",
    "Neos Kosmos":       "Tier_4_Hip_Urban",
    "Agios Ioannis":     "Tier_4_Hip_Urban",  # metro-adjacent, solid demand
    "Exarcheia":         "Tier_4_Hip_Urban",  # strong student demand + transit
    "Metaxourgeio":      "Tier_4_Hip_Urban",
    "Keramikos":         "Tier_4_Hip_Urban",
    "Gouva":             "Tier_4_Hip_Urban",
    "Lambrakis Hill":    "Tier_4_Hip_Urban",  # Neos Kosmos / Pagkrati views
    "Mouseio":           "Tier_4_Hip_Urban",

    # --- Tier 3: Prime & Safe Residential (82.0 anchor) ---
    "Dafni":             "Tier_3_Prime_Residential",  # family-friendly, metro
    "Kallithea":         "Tier_3_Prime_Residential",
    "Zografou":          "Tier_3_Prime_Residential",
    "Ilisia":            "Tier_3_Prime_Residential",
    "Ampelokipoi":       "Tier_3_Prime_Residential",
    "Nea Chalkidona":    "Tier_3_Prime_Residential",  # quiet residential pocket
    "Gyzi":              "Tier_3_Prime_Residential",
    "Vyronas":           "Tier_3_Prime_Residential",
    "Agia Paraskevi":    "Tier_3_Prime_Residential",
    "Nea Smyrni":        "Tier_3_Prime_Residential",
    "Kaisariani":        "Tier_3_Prime_Residential",

    # --- Tier 2: Developing / Growth (65.0) ---
    "Kypseli":           "Tier_2_Growth_Hub",
    "Ano Kypseli":       "Tier_2_Growth_Hub",
    "Nea Kypseli":       "Tier_2_Growth_Hub",
    "Amerikis Square":   "Tier_2_Growth_Hub",
    "Pedion tou Areos":  "Tier_2_Growth_Hub",
    "Peristeri":         "Tier_2_Growth_Hub",
    "Galatsi":           "Tier_2_Growth_Hub",
    "Nirvana":           "Tier_2_Growth_Hub",
    "Ano Patisia":       "Tier_2_Growth_Hub",

    # --- Tier 1: High Risk / Budget (45.0) ---
    "Omonia":            "Tier_1_Budget_Central",
    "Victoria Square":   "Tier_1_Budget_Central",
    "Agios Panteleimonas":"Tier_1_Budget_Central",
    "Attica Square":     "Tier_1_Budget_Central",
    "Larissis station":  "Tier_1_Budget_Central",
    "Stathmos Larissis": "Tier_1_Budget_Central",
    "Kolonos":           "Tier_1_Budget_Central",
    "Akadimia Platonos": "Tier_1_Budget_Central",
    "Kato Patisia":      "Tier_1_Budget_Central",
    "Agios Nikolaos":    "Tier_1_Budget_Central",
    "Ipirou":            "Tier_1_Budget_Central",

    # ── Spitogatos / alternate transliteration variants ──────────────────────
    # Kipseli / Kypseli variants (Greek Κυψέλη)
    "Kipseli":                   "Tier_2_Growth_Hub",
    "Ano Kipseli":               "Tier_2_Growth_Hub",
    "Nea Kipseli":               "Tier_2_Growth_Hub",
    "Plateia Kipselis":          "Tier_2_Growth_Hub",
    "Fokionos Negri":            "Tier_2_Growth_Hub",  # pedestrian street in Kypseli
    "Gkrava":                    "Tier_2_Growth_Hub",  # sub-area of Kypseli

    # Victoria / Viktorias Square area
    "Viktorias Square":          "Tier_1_Budget_Central",
    "Plateia Attikis":           "Tier_1_Budget_Central",
    "Attiki":                    "Tier_1_Budget_Central",
    "Plateia Vathis":            "Tier_1_Budget_Central",  # Vathis Square, central
    "Agiou Meletiou - Victorias Square - Marni": "Tier_1_Budget_Central",
    "Evelpidon - Dikastiria":    "Tier_1_Budget_Central",  # court district near Omonia
    "Leoforos Patision - Leoforos Acharnon": "Tier_1_Budget_Central",
    "Sepolia":                   "Tier_1_Budget_Central",
    "Thimarakia":                "Tier_1_Budget_Central",  # near Ano Patisia
    "Patisia":                   "Tier_1_Budget_Central",
    "Stathmos Larisis":          "Tier_1_Budget_Central",  # variant of Larissis station
    "akadimia Platonos":         "Tier_1_Budget_Central",  # lowercase from Spitogatos

    # Amerikis / Plateia Amerikis
    "Plateia Amerikis":          "Tier_2_Growth_Hub",
    "Plateia Koliatsou":         "Tier_2_Growth_Hub",
    "Koliatsou":                 "Tier_2_Growth_Hub",
    "Kipriadou - Ano Patisia":   "Tier_2_Growth_Hub",

    # Gyzi / Gizi variants
    "Gizi - Pedion Areos":       "Tier_2_Growth_Hub",
    "Plateia Gkizi":             "Tier_3_Prime_Residential",
    "Gkyzi - Arios Pagos":       "Tier_3_Prime_Residential",

    # Ilisia area
    "Kato Ilisia":               "Tier_3_Prime_Residential",
    "Panormou":                  "Tier_3_Prime_Residential",  # Ampelokipoi sub-area

    # Exarcheia variants
    "Exarchia - Neapoli":        "Tier_4_Hip_Urban",
    "Neapoli Exarcheion":        "Tier_4_Hip_Urban",
    "Pagkrati Center":           "Tier_4_Hip_Urban",
    "Agios Eleftherios":         "Tier_4_Hip_Urban",  # near Pagkrati

    # Koukaki compound name
    "Koukaki - Pediki Chara":    "Tier_5_Elite_BlueChip",

    # Exceptions
    "130":               None,
}

# ── Scoring weights (must sum to 1.0) ─────────────────────────────────────────

W_PRICE  = 0.45
W_HOOD   = 0.20
W_VISUAL = 0.20
W_STRUCT = 0.15

_VISUAL_FALLBACK = 5.0


def _effective_visual_quality(row: sqlite3.Row) -> float:
    """
    Prefer Layer 1 structured JSON (interior_quality_avg, is_renovated); else ``visual_score``.
    """
    vs = coerce_sql_float(row_get(row, "visual_score")) or _VISUAL_FALLBACK
    raw = row_get(row, "layer_1_features")
    if not raw:
        return float(vs)
    try:
        feat = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return float(vs)
    if not isinstance(feat, dict):
        return float(vs)
    iq = feat.get("interior_quality_avg")
    if iq is None:
        return float(vs)
    try:
        q = float(iq)
    except (TypeError, ValueError):
        return float(vs)
    q = max(1.0, min(10.0, q))
    if feat.get("is_renovated"):
        q = min(10.0, q + 0.3)
    return round(q, 2)


# ── Scoring components (all return 0.0–1.0) ───────────────────────────────────

def _hood_score(neighborhood: str) -> float:
    tier = NEIGHBORHOOD_CANONICAL.get(neighborhood)
    if tier is None:
        return DEFAULT_HOOD_SCORE / 100.0
    return NEIGHBORHOOD_SCORES.get(tier, DEFAULT_HOOD_SCORE) / 100.0


def _v_index(listing_psqm: float, floor_mult: float, baseline: float) -> float:
    """Price Value Index. >1.0 means listing is underpriced vs floor-adjusted baseline."""
    adjusted = listing_psqm * floor_mult
    if adjusted <= 0:
        return 0.0
    return baseline / adjusted


def _price_score(v_idx: float) -> float:
    """Maps V_index to 0.0–1.0. V=0.5 → 0.0 (50% premium), V=1.0 → 0.5, V=1.5 → 1.0."""
    return min(1.0, max(0.0, v_idx - 0.5))


def _size_component(area_sqm: float) -> float:
    if 35.0 <= area_sqm <= 55.0:
        return 1.0
    if (20.0 <= area_sqm < 35.0) or (55.0 < area_sqm <= 75.0):
        return 0.8
    if 75.0 < area_sqm <= 100.0:
        return 0.6
    return 0.4


def _energy_component(energy_class: str | None) -> float:
    if energy_class:
        ec = str(energy_class).strip().upper().replace("+", "").replace(" ", "")
        if ec in {"A++", "A+", "A"}:
            return 1.0
        if ec == "B":
            return 0.85
        if ec == "C":
            return 0.70
        if ec == "D":
            return 0.55
        if ec in {"E", "Z", "H", "ZH"}:
            return 0.40
    return 0.50


def _year_component(year_built: int | None) -> float:
    if year_built is None:
        return 0.50
    if year_built > 2015:
        return 1.0
    if year_built >= 2006:
        return 0.9
    if year_built >= 1982:
        return 0.7
    if year_built >= 1970:
        return 0.5
    return 0.3


def _structural_index(area_sqm: float, energy_class: str | None, year_built: int | None) -> float:
    return (
        _size_component(area_sqm)
        + _energy_component(energy_class)
        + _year_component(year_built)
    ) / 3.0


def _market_diff(v_idx: float) -> str:
    pct = (v_idx - 1.0) * 100.0
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


# ── Main ranking function ─────────────────────────────────────────────────────

def rank_deals(
    listings: list[sqlite3.Row],
    baselines: dict[str, float],
) -> list[tuple]:
    """
    Rank listings by investment score (Alpha-style composite).

    listings: sqlite3.Row objects from updated_listings.db
    baselines: strict per-neighborhood anchors from market_analytics (hood absent →
        no location_price anchor; that weight is omitted and remaining weights renormalized).

    Returns list of deal tuples sorted by score descending:
      (score, hood, price, area, market_diff, floor, visual_quality, url)
    ``market_diff`` is ``\"n/a\"`` when there is no neighborhood baseline for that row.
    """
    deals: list[tuple] = []
    skip_no_price_area = ranked_without_price_anchor = 0
    w_rest = W_HOOD + W_VISUAL + W_STRUCT

    for row in listings:
        price    = coerce_sql_float(row_get(row, "price_eur"))
        area_sqm = coerce_sql_float(row_get(row, "area_sqm"))
        if price is None or area_sqm is None or area_sqm <= 0:
            skip_no_price_area += 1
            continue

        hood     = str(row_get(row, "neighborhood") or "").strip()
        baseline = baselines.get(hood)
        has_price_anchor = baseline is not None and baseline > 0

        floor_val    = row_get(row, "floor")
        energy_class = row_get(row, "energy_class")
        year_built   = coerce_sql_int(row_get(row, "year_built"))

        listing_psqm   = price / area_sqm
        mult           = floor_multiplier(floor_val)
        visual_quality = _effective_visual_quality(row)

        hood_term   = _hood_score(hood) * W_HOOD
        visual_term = (visual_quality / 10.0) * W_VISUAL
        struct_term = _structural_index(area_sqm, energy_class, year_built) * W_STRUCT

        if has_price_anchor:
            v_idx = _v_index(listing_psqm, mult, baseline)
            raw_score = (
                _price_score(v_idx) * W_PRICE
                + hood_term
                + visual_term
                + struct_term
            )
            market_diff = _market_diff(v_idx)
        else:
            ranked_without_price_anchor += 1
            raw_score = (hood_term + visual_term + struct_term) / w_rest
            market_diff = "n/a"

        score = round(min(100.0, raw_score * 100.0), 1)

        deals.append((
            score,
            hood,
            int(price),
            int(area_sqm),
            market_diff,
            str(floor_val) if floor_val is not None else "",
            visual_quality,
            row_get(row, "url"),
        ))

    deals.sort(key=lambda x: x[0], reverse=True)

    if skip_no_price_area:
        logger.info("Skipped (missing price/area): %s", skip_no_price_area)
    if ranked_without_price_anchor:
        logger.info(
            "Ranked without neighborhood price anchor (location_price leg omitted; "
            "weights renormalized): %s listings",
            ranked_without_price_anchor,
        )

    return deals

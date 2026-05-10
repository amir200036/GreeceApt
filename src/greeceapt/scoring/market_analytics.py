"""
market_analytics.py — Layer 3: Neighborhood Baseline Computation.

Produces stable per-neighborhood price anchors using:
  - Double Normalization: normalized_psqm = (price / area) * floor_multiplier
  - 10% Trimmed Mean to exclude outlier listings at both tails
  - A minimum sample size per neighborhood (default 10): neighborhoods with fewer
    qualifying listings are omitted from the baseline dictionary (no broader fallback).

Floor multipliers convert any floor to a "1st-floor equivalent" so that
ground-floor and basement discounts don't distort the neighborhood average.

  DB numeric → multiplier:  -1 → 1.45,  -0.5 → 1.25,  0/0.5 → 1.10,  1–2 → 1.00,  3+ → 0.90
"""

from __future__ import annotations

from greeceapt.scoring.util import trimmed_mean

TRIM_PCT = 0.10
DEFAULT_MIN_SAMPLE_SIZE = 10


def floor_multiplier(floor_val) -> float:
    """Return the floor-adjustment multiplier for a numeric DB floor value."""
    if floor_val is None or str(floor_val).strip() == "":
        return 1.00
    try:
        f = float(str(floor_val).strip())
    except ValueError:
        return 1.00
    if f <= -1:
        return 1.45   # basement
    if f < 0:
        return 1.25   # semi-basement (-0.5)
    if f < 1:
        return 1.10   # ground (0, 0.5)
    if f < 3:
        return 1.00   # 1st / 2nd floor
    return 0.90       # 3rd floor and above


def _normalized_psqm_row(listing: dict) -> tuple[str, float] | None:
    """Return (neighborhood_key, normalized_psqm) or None if row unusable."""
    hood = listing.get("neighborhood")
    price = listing.get("price_eur")
    area = listing.get("area_sqm")
    floor = listing.get("floor")

    try:
        price = float(price)  # type: ignore[arg-type]
        area = float(area)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

    if area <= 0:
        return None

    hk = str(hood).strip() if hood and str(hood).strip() else ""
    if not hk:
        return None

    normalized_psqm = (price / area) * floor_multiplier(floor)
    return hk, normalized_psqm


def compute_neighborhood_baselines(
    listings: list[dict],
    trim_pct: float = TRIM_PCT,
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
) -> tuple[dict[str, float], int, int]:
    """
    Return floor-adjusted trimmed-mean normalized PSQM per neighborhood.

    A neighborhood appears in the dict only if it has at least ``min_sample_size``
    valid listings after filtering (strict threshold: 9 or fewer → excluded).

    Returns:
        (baselines_by_neighborhood, excluded_neighborhood_count, min_sample_size)
        where ``excluded_neighborhood_count`` is how many distinct neighborhood names
        had at least one qualifying row but fewer than ``min_sample_size`` rows.
    """
    hood_vals: dict[str, list[float]] = {}

    for listing in listings:
        parsed = _normalized_psqm_row(listing)
        if parsed is None:
            continue
        hk, npsqm = parsed
        hood_vals.setdefault(hk, []).append(npsqm)

    hood_out = {
        h: trimmed_mean(vals, trim_pct)
        for h, vals in hood_vals.items()
        if len(vals) >= min_sample_size
    }
    excluded = sum(1 for vals in hood_vals.values() if len(vals) < min_sample_size)
    return hood_out, excluded, min_sample_size

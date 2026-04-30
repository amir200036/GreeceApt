"""
market_analytics.py — Layer 3: Neighborhood Baseline Computation.

Produces stable per-neighborhood price anchors using:
  - Double Normalization: normalized_psqm = (price / area) * floor_multiplier
  - 10% Trimmed Mean to exclude outlier listings at both tails

Floor multipliers convert any floor to a "1st-floor equivalent" so that
ground-floor and basement discounts don't distort the neighborhood average.

  DB numeric → multiplier:  -1 → 1.45,  -0.5 → 1.25,  0/0.5 → 1.10,  1–2 → 1.00,  3+ → 0.90
"""

from __future__ import annotations

TRIM_PCT = 0.10


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


def _trimmed_mean(values: list[float], trim_pct: float = TRIM_PCT) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    if n <= 3:
        return sum(values) / n
    cut = max(1, int(n * trim_pct))
    trimmed = sorted(values)[cut:-cut]
    return sum(trimmed) / len(trimmed)


def compute_neighborhood_baselines(
    listings: list[dict],
    trim_pct: float = TRIM_PCT,
) -> dict[str, float]:
    """
    Return floor-adjusted trimmed-mean price per sqm per neighborhood.

    listings: list of dicts with keys neighborhood, price_eur, area_sqm, floor.
    Returns: {neighborhood_name: normalized_psqm_baseline}
    """
    hood_vals: dict[str, list[float]] = {}

    for listing in listings:
        hood  = listing.get("neighborhood")
        price = listing.get("price_eur")
        area  = listing.get("area_sqm")
        floor = listing.get("floor")

        try:
            price = float(price)  # type: ignore[arg-type]
            area  = float(area)   # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue

        if not hood or area <= 0:
            continue

        normalized_psqm = (price / area) * floor_multiplier(floor)
        hood_vals.setdefault(str(hood).strip(), []).append(normalized_psqm)

    return {h: _trimmed_mean(vals, trim_pct) for h, vals in hood_vals.items() if vals}

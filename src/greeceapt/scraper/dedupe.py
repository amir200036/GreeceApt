"""
Entity resolution for merged scraper output — deciding when two listings are one property.

Replaces the original "any single photo pair under Hamming 5, then check metadata" rule,
which was both slow (O(n²) over ~4M listing pairs, ~436M ndarray hash comparisons) and
imprecise (one shared stock photo or a lookalike floor plan merged unrelated listings).

**Every threshold here comes from measurement, not intuition.** They were derived from the
401 confirmed duplicate pairs archived from the previous dataset:

* Coordinate error between the two coordinates of the *same* property:
  p50 34 m, p90 250 m, p95 308 m, p99 879 m, max 2636 m. Portals deliberately fuzz
  location, so geo can only ever be a *candidate generator*, never a veto — a 165 m gate
  would have discarded 21.7% of real duplicates.
* Price delta between portals for the same property: p90 3.6%, p95 7.7%, max 21.4%.

Note on what could NOT be calibrated from that data: ``area_sqm``, ``floor``, ``year_built``
and ``energy_class`` all showed perfect agreement on those pairs **because the old gate
already required it**. Those measurements are circular and were not used to set tolerances.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# ── Measured / chosen thresholds ─────────────────────────────────────────────

PHASH_HAMMING_THRESHOLD = 5      # unchanged from the original implementation
GEO_CELL_M = 250.0               # 3x3 scan => ~750 m reach => ~98.5% of true pairs
AREA_REL_TOLERANCE = 0.03
PRICE_REL_TOLERANCE = 0.25       # measured max divergence 21.4%
PHOTO_MATCH_MIN = 0.34           # >= 1 in 3 photos must pair up
PHOTO_MATCH_SPARSE_META = 0.60   # required when metadata cannot corroborate
COMMON_PHOTO_MIN_LISTINGS = 5    # a photo in >= N listings is agency boilerplate
MIN_CORROBORATORS = 1        # area agreement is enough to promote a moderate photo overlap

# Which metadata fields carry real identity information — MEASURED, not assumed.
# On 1171 fresh pairs with >=60% photo overlap (identical photos => near-certainly the same
# property), agreement rates were:
#     area_sqm 93%, price 94%(<=8%)   -> reliable; safe to VETO on a conflict
#     bedrooms 88%, year_built 81%    -> noisy; count agreement as a soft plus, never veto
#     floor 40%, energy_class 44%     -> NOISE. Agents enter these inconsistently even for the
#                                        same property on the SAME portal, so a disagreement
#                                        says nothing. They are excluded from the gate entirely.
# Only area is a hard veto field. Everything else is soft corroboration at most.
SOFT_CORROBORATORS = ("year_built", "bedrooms")

_EARTH_RADIUS_M = 6_371_000.0
_DEG_LAT_M = 111_320.0


# ── Hashing primitives ────────────────────────────────────────────────────────

def phash_to_int(value: str | int | None) -> int | None:
    """Hex phash → int. ``int.bit_count()`` on these is far faster than ndarray XOR."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def listing_hash_ints(listing: dict, blacklist: frozenset[int] | None = None) -> list[int]:
    """Usable photo hashes for a listing, boilerplate removed."""
    out: list[int] = []
    for h in listing.get("image_hashes") or []:
        v = phash_to_int(h)
        if v is None:
            continue
        if blacklist and v in blacklist:
            continue
        out.append(v)
    return out


def build_common_photo_blacklist(
    listings: Sequence[dict],
    min_listings: int = COMMON_PHOTO_MIN_LISTINGS,
) -> frozenset[int]:
    """
    Photo hashes appearing in ``min_listings`` or more distinct listings.

    These are agency watermarks, stock building shots and generic floor plans. Left in,
    a single such photo is enough to "prove" two unrelated listings are the same property.
    """
    counts: dict[int, int] = {}
    for listing in listings:
        for h in set(listing_hash_ints(listing)):
            counts[h] = counts.get(h, 0) + 1
    return frozenset(h for h, c in counts.items() if c >= min_listings)


# ── Geometry ──────────────────────────────────────────────────────────────────

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _coords(listing: dict) -> tuple[float, float] | None:
    lat, lon = listing.get("latitude"), listing.get("longitude")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Blocking ──────────────────────────────────────────────────────────────────

def candidate_pairs(listings: Sequence[dict]) -> set[tuple[int, int]]:
    """
    Index pairs worth comparing in full, from the union of two independent blocking keys.

    1. **Geo grid** — same or adjacent ~250 m cell. Catches the ~98.5% of true duplicates
       whose coordinates agree to within roughly 750 m.
    2. **(rounded area, price bucket)** — geo-free, so it still recovers the tail of pairs
       whose coordinates disagree wildly (the measured maximum was 2.6 km).

    Neither key is a filter on its own; both only propose pairs for scoring.
    """
    geo_cells: dict[tuple[int, int], list[int]] = {}
    meta_cells: dict[tuple[int, int], list[int]] = {}

    for idx, listing in enumerate(listings):
        coords = _coords(listing)
        if coords is not None:
            lat, lon = coords
            cell_lat = int(lat * _DEG_LAT_M // GEO_CELL_M)
            lon_m = _DEG_LAT_M * math.cos(math.radians(lat))
            cell_lon = int(lon * lon_m // GEO_CELL_M)
            geo_cells.setdefault((cell_lat, cell_lon), []).append(idx)

        area = _as_float(listing.get("area_sqm"))
        price = _as_float(listing.get("price_eur"))
        if area is not None and area > 0 and price is not None and price > 0:
            meta_cells.setdefault((int(round(area)), int(price // 5000)), []).append(idx)

    pairs: set[tuple[int, int]] = set()

    for (clat, clon), bucket in geo_cells.items():
        neighbourhood: list[int] = []
        for dlat in (-1, 0, 1):
            for dlon in (-1, 0, 1):
                neighbourhood.extend(geo_cells.get((clat + dlat, clon + dlon), ()))
        for i in bucket:
            for j in neighbourhood:
                if i < j:
                    pairs.add((i, j))
                elif j < i:
                    pairs.add((j, i))

    # Area buckets are exact, so also scan the adjacent ones — 49.6 and 50.4 sqm both
    # round into neighbouring integers but are the same flat.
    for (area_key, price_key), bucket in meta_cells.items():
        neighbourhood = []
        for da in (-1, 0, 1):
            for dp in (-1, 0, 1):
                neighbourhood.extend(meta_cells.get((area_key + da, price_key + dp), ()))
        for i in bucket:
            for j in neighbourhood:
                if i < j:
                    pairs.add((i, j))
                elif j < i:
                    pairs.add((j, i))

    return pairs


# ── Pair scoring ──────────────────────────────────────────────────────────────

def greedy_photo_matching(
    hashes_a: Sequence[int],
    hashes_b: Sequence[int],
    threshold: int = PHASH_HAMMING_THRESHOLD,
) -> tuple[int, int]:
    """
    One-to-one photo pairing. Returns ``(matched_pairs, min_hamming)``.

    One-to-one matters: counting "does this photo resemble anything over there" lets a
    single photo in A match three near-identical photos in B and report three matches,
    inflating the match fraction. Greedy on ascending distance is sufficient and exact
    enough for the 8–30 photos a listing carries; a Hungarian solve would be overkill.
    """
    if not hashes_a or not hashes_b:
        return 0, 64

    scored: list[tuple[int, int, int]] = []
    min_d = 64
    for i, ha in enumerate(hashes_a):
        for j, hb in enumerate(hashes_b):
            d = hamming(ha, hb)
            if d < min_d:
                min_d = d
            if d < threshold:
                scored.append((d, i, j))

    scored.sort()
    used_a: set[int] = set()
    used_b: set[int] = set()
    matched = 0
    for _d, i, j in scored:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        matched += 1
    return matched, min_d


def metadata_agreement(a: dict, b: dict) -> tuple[int, tuple[str, ...]]:
    """
    Returns ``(corroborators, conflicts)``.

    ``conflicts`` contains only fields reliable enough to *veto* a match — measured, that is
    ``area_sqm`` alone (93% agreement on same-property pairs). A disagreement there is
    decisive. ``corroborators`` counts soft positive signals: area agreement plus agreement
    on ``year_built`` / ``bedrooms``, which reinforce a match but whose disagreement is
    ignored because they are too noisy (19% / 12% conflict) to reject on.

    ``floor`` and ``energy_class`` are deliberately absent: they disagreed ~60% of the time
    on pairs with identical photos, so they carry no usable identity information. The
    original gate treated all four as hard fields and also skipped any field null on either
    side, so with sparse coverage it often validated nothing while reporting a pass.
    """
    corroborators = 0
    conflicts: list[str] = []

    area_a, area_b = _as_float(a.get("area_sqm")), _as_float(b.get("area_sqm"))
    if area_a is not None and area_b is not None:
        denom = max(area_a, area_b)
        if denom > 0:
            if abs(area_a - area_b) / denom > AREA_REL_TOLERANCE:
                conflicts.append("area_sqm")
            else:
                corroborators += 1

    for fname in SOFT_CORROBORATORS:
        fa, fb = _as_float(a.get(fname)), _as_float(b.get(fname))
        if fa is not None and fb is not None and int(fa) == int(fb):
            corroborators += 1

    return corroborators, tuple(conflicts)


@dataclass(frozen=True)
class PairEvidence:
    photo_match_frac: float
    matched_photos: int
    min_hamming: int
    geo_distance_m: float | None
    price_rel_delta: float | None
    agreeing_fields: int
    conflicting_fields: tuple[str, ...]
    confidence: str  # "HIGH" | "REVIEW" | "NONE"

    @property
    def is_high(self) -> bool:
        return self.confidence == "HIGH"


def evaluate_pair(a: dict, b: dict, hashes_a: Sequence[int], hashes_b: Sequence[int]) -> PairEvidence:
    """Score one candidate pair into HIGH / REVIEW / NONE."""
    matched, min_d = greedy_photo_matching(hashes_a, hashes_b)
    denom = min(len(hashes_a), len(hashes_b))
    frac = (matched / denom) if denom else 0.0

    agreeing, conflicts = metadata_agreement(a, b)

    geo_m: float | None = None
    ca, cb = _coords(a), _coords(b)
    if ca and cb:
        geo_m = haversine_m(ca[0], ca[1], cb[0], cb[1])

    price_delta: float | None = None
    pa, pb = _as_float(a.get("price_eur")), _as_float(b.get("price_eur"))
    if pa and pb and max(pa, pb) > 0:
        price_delta = abs(pa - pb) / max(pa, pb)

    confidence = "NONE"
    if conflicts:
        # A disagreement on a field both sides actually state is decisive.
        confidence = "NONE"
    elif frac >= PHOTO_MATCH_SPARSE_META:
        confidence = "HIGH"
    elif frac >= PHOTO_MATCH_MIN and agreeing >= MIN_CORROBORATORS:
        confidence = "HIGH"
    elif frac >= PHOTO_MATCH_MIN:
        confidence = "REVIEW"
    elif matched > 0:
        confidence = "REVIEW"

    if confidence != "NONE" and price_delta is not None and price_delta > PRICE_REL_TOLERANCE:
        # Same photos but a price gap larger than any observed between portals.
        confidence = "REVIEW"

    return PairEvidence(
        photo_match_frac=round(frac, 4),
        matched_photos=matched,
        min_hamming=min_d,
        geo_distance_m=round(geo_m, 1) if geo_m is not None else None,
        price_rel_delta=round(price_delta, 4) if price_delta is not None else None,
        agreeing_fields=agreeing,
        conflicting_fields=conflicts,
        confidence=confidence,
    )


# ── Grouping ──────────────────────────────────────────────────────────────────

class UnionFind:
    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for i in range(len(self._parent)):
            out.setdefault(self.find(i), []).append(i)
        return out


@dataclass
class DedupeResult:
    groups: list[list[int]] = field(default_factory=list)
    review_pairs: list[dict] = field(default_factory=list)
    blacklisted_photos: int = 0
    candidate_pairs: int = 0
    high_confidence_pairs: int = 0
    split_components: int = 0


def validate_component(listings: Sequence[dict], members: Sequence[int]) -> bool:
    """
    Every pair *within* a component must be mutually consistent before it is merged.

    Union-Find is transitively correct but propagates mistakes: given ``A~B`` and ``B~C``
    through one weak edge, ``A`` and ``C`` merge even when they plainly conflict. A wrong
    merge destroys a real listing, while a missed duplicate only leaves a redundant row —
    that asymmetry justifies rejecting the whole component when any internal pair clashes.
    """
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            _agree, conflicts = metadata_agreement(listings[members[i]], listings[members[j]])
            if conflicts:
                return False
    return True


def find_duplicate_groups(listings: Sequence[dict]) -> DedupeResult:
    """Group listings that refer to the same property. Indices refer into ``listings``."""
    result = DedupeResult()
    if not listings:
        return result

    blacklist = build_common_photo_blacklist(listings)
    result.blacklisted_photos = len(blacklist)
    hashes = [listing_hash_ints(row, blacklist) for row in listings]

    pairs = candidate_pairs(listings)
    result.candidate_pairs = len(pairs)

    uf = UnionFind(len(listings))
    high_edges = 0
    for i, j in sorted(pairs):
        if not hashes[i] or not hashes[j]:
            continue
        ev = evaluate_pair(listings[i], listings[j], hashes[i], hashes[j])
        if ev.is_high:
            uf.union(i, j)
            high_edges += 1
        elif ev.confidence == "REVIEW":
            result.review_pairs.append({
                "a_url": listings[i].get("url"),
                "b_url": listings[j].get("url"),
                "photo_match_frac": ev.photo_match_frac,
                "matched_photos": ev.matched_photos,
                "min_hamming": ev.min_hamming,
                "geo_distance_m": ev.geo_distance_m,
                "price_rel_delta": ev.price_rel_delta,
                "agreeing_fields": ev.agreeing_fields,
                "conflicting_fields": list(ev.conflicting_fields),
            })
    result.high_confidence_pairs = high_edges

    for members in uf.groups().values():
        if len(members) == 1:
            result.groups.append(members)
            continue
        if validate_component(listings, members):
            result.groups.append(sorted(members))
        else:
            # Internally inconsistent: keep every member separate rather than risk a
            # destructive merge, and surface it for review.
            result.split_components += 1
            logger.info(
                "Dedupe: split inconsistent component of %s listings (%s)",
                len(members),
                [listings[m].get("url") for m in members],
            )
            result.groups.extend([m] for m in sorted(members))

    result.groups.sort(key=lambda g: g[0])
    return result

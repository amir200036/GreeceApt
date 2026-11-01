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
from typing import Any, Iterable, Literal, Mapping, Sequence

from greeceapt.scraper.phash_index import PhotoRef, analyze_photo_index

logger = logging.getLogger(__name__)

# ── Measured / chosen thresholds ─────────────────────────────────────────────

PHASH_HAMMING_THRESHOLD = 5      # unchanged from the original implementation
GEO_CELL_M = 250.0               # indexed broadly, then confirmed at the 1,000 m boundary
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

CandidateSource = Literal["photo", "geo", "metadata"]
CandidateMap = dict[tuple[int, int], frozenset[CandidateSource]]


def candidate_pairs(
    listings: Sequence[dict],
    photo_pairs: Iterable[tuple[int, int]] = (),
) -> CandidateMap:
    """
    Index pairs worth comparing in full, with every proposing source retained.

    1. **Photo bands** — confirmed near-pHash pairs supplied by ``analyze_photo_index``.
    2. **Geo grid** — expanded 250 m cells, Haversine-confirmed through 1,000 m.
    3. **Metadata ranges** — the exact 3% area and 25% price tolerances.

    Neither key is a filter on its own; both only propose pairs for scoring.
    """
    geo_cells: dict[tuple[int, int], list[int]] = {}

    for idx, listing in enumerate(listings):
        coords = _coords(listing)
        if coords is not None:
            lat, lon = coords
            cell_lat = int(lat * _DEG_LAT_M // GEO_CELL_M)
            lon_m = _DEG_LAT_M * math.cos(math.radians(lat))
            cell_lon = int(lon * lon_m // GEO_CELL_M)
            geo_cells.setdefault((cell_lat, cell_lon), []).append(idx)

    sources: dict[tuple[int, int], set[CandidateSource]] = {}

    def add_pair(left: int, right: int, source: CandidateSource) -> None:
        if left == right or not (0 <= left < len(listings)) or not (0 <= right < len(listings)):
            return
        sources.setdefault(tuple(sorted((left, right))), set()).add(source)

    for left, right in photo_pairs:
        add_pair(left, right, "photo")

    for (clat, clon), bucket in geo_cells.items():
        neighbourhood: list[int] = []
        cell_radius = math.ceil(1_000.0 / GEO_CELL_M) + 1
        for dlat in range(-cell_radius, cell_radius + 1):
            for dlon in range(-cell_radius, cell_radius + 1):
                neighbourhood.extend(geo_cells.get((clat + dlat, clon + dlon), ()))
        for i in bucket:
            for j in neighbourhood:
                if i == j:
                    continue
                coords_i, coords_j = _coords(listings[i]), _coords(listings[j])
                if coords_i and coords_j and haversine_m(*coords_i, *coords_j) <= 1_000.0:
                    add_pair(i, j, "geo")

    metadata_rows = sorted(
        (area, price, index)
        for index, listing in enumerate(listings)
        if (area := _as_float(listing.get("area_sqm"))) is not None and area > 0
        and (price := _as_float(listing.get("price_eur"))) is not None and price > 0
    )
    for position, (area, price, index) in enumerate(metadata_rows):
        maximum_area = area / (1.0 - AREA_REL_TOLERANCE)
        for other_area, other_price, other_index in metadata_rows[position + 1:]:
            if other_area > maximum_area:
                break
            area_delta = abs(area - other_area) / max(area, other_area)
            price_delta = abs(price - other_price) / max(price, other_price)
            if area_delta <= AREA_REL_TOLERANCE and price_delta <= PRICE_REL_TOLERANCE:
                add_pair(index, other_index, "metadata")

    return {pair: frozenset(pair_sources) for pair, pair_sources in sorted(sources.items())}


# ── Pair scoring ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PhotoMatch:
    a_photo_key: str
    b_photo_key: str
    distance: int
    variant: Literal["full", "center"]


def _photo_edges(
    photos_a: Sequence[PhotoRef],
    photos_b: Sequence[PhotoRef],
    threshold: int,
) -> tuple[list[PhotoMatch], int]:
    edges: list[PhotoMatch] = []
    minimum = 64
    for photo_a in photos_a:
        for photo_b in photos_b:
            for variant in ("full", "center"):
                hash_a = getattr(photo_a.fingerprint, variant)
                hash_b = getattr(photo_b.fingerprint, variant)
                if hash_a is None or hash_b is None:
                    continue
                distance = hamming(hash_a, hash_b)
                minimum = min(minimum, distance)
                if distance < threshold:
                    edges.append(PhotoMatch(
                        photo_a.photo_key,
                        photo_b.photo_key,
                        distance,
                        variant,
                    ))
    edges.sort(key=lambda match: (
        match.distance,
        match.a_photo_key,
        match.b_photo_key,
        match.variant,
    ))
    return edges, minimum


def greedy_photo_matching(
    photos_a: Sequence[PhotoRef],
    photos_b: Sequence[PhotoRef],
    threshold: int = PHASH_HAMMING_THRESHOLD,
) -> tuple[PhotoMatch, ...]:
    """Select deterministic one-to-one matches across like-for-like hash variants."""
    edges, _minimum = _photo_edges(photos_a, photos_b, threshold)
    used_a: set[str] = set()
    used_b: set[str] = set()
    matches: list[PhotoMatch] = []
    for match in edges:
        if match.a_photo_key in used_a or match.b_photo_key in used_b:
            continue
        used_a.add(match.a_photo_key)
        used_b.add(match.b_photo_key)
        matches.append(match)
    return tuple(matches)


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
    candidate_sources: frozenset[str]
    matches: tuple[PhotoMatch, ...]
    full_matches: int
    center_matches: int
    photo_match_frac: float
    matched_photos: int
    min_hamming: int
    geo_distance_m: float | None
    price_rel_delta: float | None
    agreeing_fields: int
    conflicting_fields: tuple[str, ...]
    reason_code: str
    confidence: Literal["HIGH", "REVIEW", "NONE"]

    @property
    def is_high(self) -> bool:
        return self.confidence == "HIGH"


def evaluate_pair(
    a: dict,
    b: dict,
    photos_a: Sequence[PhotoRef],
    photos_b: Sequence[PhotoRef],
    candidate_sources: Iterable[str] = (),
) -> PairEvidence:
    """Score one candidate pair into HIGH / REVIEW / NONE."""
    _edges, min_d = _photo_edges(photos_a, photos_b, PHASH_HAMMING_THRESHOLD)
    matches = greedy_photo_matching(photos_a, photos_b)
    matched = len(matches)
    denom = min(len(photos_a), len(photos_b))
    frac = matched / denom if denom else 0.0

    agreeing, conflicts = metadata_agreement(a, b)

    geo_m: float | None = None
    ca, cb = _coords(a), _coords(b)
    if ca and cb:
        geo_m = haversine_m(ca[0], ca[1], cb[0], cb[1])

    price_delta: float | None = None
    pa, pb = _as_float(a.get("price_eur")), _as_float(b.get("price_eur"))
    if pa and pb and max(pa, pb) > 0:
        price_delta = abs(pa - pb) / max(pa, pb)

    confidence: Literal["HIGH", "REVIEW", "NONE"] = "NONE"
    reason_code = "no_photo_matches"
    if conflicts:
        confidence = "NONE"
        reason_code = "area_conflict"
    elif matched > 0 and price_delta is not None and price_delta > PRICE_REL_TOLERANCE:
        confidence = "REVIEW"
        reason_code = "price_gap"
    elif matched == 0:
        confidence = "NONE"
        reason_code = "no_photo_matches"
    elif matched < 3:
        confidence = "REVIEW"
        reason_code = "insufficient_photo_count"
    elif frac >= PHOTO_MATCH_SPARSE_META:
        confidence = "HIGH"
        reason_code = "strong_photo_overlap"
    elif frac >= PHOTO_MATCH_MIN and agreeing >= MIN_CORROBORATORS:
        confidence = "HIGH"
        reason_code = "corroborated_photo_overlap"
    elif frac >= PHOTO_MATCH_MIN:
        confidence = "REVIEW"
        reason_code = "uncorroborated_photo_overlap"
    else:
        confidence = "REVIEW"
        reason_code = "low_photo_overlap"

    return PairEvidence(
        candidate_sources=frozenset(candidate_sources),
        matches=matches,
        full_matches=sum(match.variant == "full" for match in matches),
        center_matches=sum(match.variant == "center" for match in matches),
        photo_match_frac=round(frac, 4),
        matched_photos=matched,
        min_hamming=min_d,
        geo_distance_m=round(geo_m, 1) if geo_m is not None else None,
        price_rel_delta=round(price_delta, 4) if price_delta is not None else None,
        agreeing_fields=agreeing,
        conflicting_fields=conflicts,
        reason_code=reason_code,
        confidence=confidence,
    )


# ── Grouping ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GroupingResult:
    groups: tuple[tuple[int, ...], ...]
    bridge_reviews: tuple[dict, ...]


def _stable_listing_id(listing: dict, index: int) -> str:
    return str(listing.get("url") or listing.get("id") or f"index-{index:08d}")


def complete_link_groups(
    listings: Sequence[dict],
    evidence: Mapping[tuple[int, int], PairEvidence],
) -> GroupingResult:
    """Agglomerate only clusters whose every cross-pair is directly ``HIGH``."""
    stable_ids = [_stable_listing_id(listing, index) for index, listing in enumerate(listings)]
    high_edges = [
        (pair, pair_evidence)
        for pair, pair_evidence in evidence.items()
        if pair_evidence.confidence == "HIGH"
    ]
    high_edges.sort(key=lambda item: (
        -item[1].matched_photos,
        -item[1].photo_match_frac,
        item[1].min_hamming,
        min(stable_ids[item[0][0]], stable_ids[item[0][1]]),
        max(stable_ids[item[0][0]], stable_ids[item[0][1]]),
    ))

    clusters: dict[int, set[int]] = {index: {index} for index in range(len(listings))}
    member_cluster = list(range(len(listings)))
    bridge_reviews: list[dict] = []
    seen_reviews: set[tuple[str, ...]] = set()

    for (left, right), _edge_evidence in high_edges:
        root_left = member_cluster[left]
        root_right = member_cluster[right]
        if root_left == root_right:
            continue
        members_left = clusters[root_left]
        members_right = clusters[root_right]
        cross_pairs = [
            tuple(sorted((member_left, member_right)))
            for member_left in members_left
            for member_right in members_right
        ]
        if all(
            pair in evidence and evidence[pair].confidence == "HIGH"
            for pair in cross_pairs
        ):
            keep_root, drop_root = min(root_left, root_right), max(root_left, root_right)
            merged = members_left | members_right
            clusters[keep_root] = merged
            del clusters[drop_root]
            for member in merged:
                member_cluster[member] = keep_root
            continue

        component_ids = tuple(sorted(
            stable_ids[member] for member in members_left | members_right
        ))
        if component_ids in seen_reviews:
            continue
        seen_reviews.add(component_ids)
        non_high = [
            [stable_ids[pair[0]], stable_ids[pair[1]]]
            for pair in cross_pairs
            if pair not in evidence or evidence[pair].confidence != "HIGH"
        ]
        bridge_reviews.append({
            "reason_code": "incomplete_high_clique",
            "blocked_edge_ids": sorted((stable_ids[left], stable_ids[right])),
            "component_ids": list(component_ids),
            "non_high_pair_ids": sorted(non_high),
        })

    groups = tuple(sorted(
        (tuple(sorted(members)) for members in clusters.values()),
        key=lambda group: group[0],
    ))
    bridge_reviews.sort(key=lambda review: (
        review["component_ids"], review["blocked_edge_ids"]
    ))
    return GroupingResult(groups=groups, bridge_reviews=tuple(bridge_reviews))

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
    pair_evidence: dict[tuple[int, int], PairEvidence] = field(default_factory=dict)


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

    photo_analysis = analyze_photo_index(listings)
    result.blacklisted_photos = len(photo_analysis.blacklisted_refs)
    photos = photo_analysis.usable_photos_by_listing

    pairs = candidate_pairs(listings, photo_analysis.photo_candidate_pairs)
    result.candidate_pairs = len(pairs)

    high_edges = 0
    for i, j in sorted(pairs):
        if not photos[i] or not photos[j]:
            continue
        ev = evaluate_pair(listings[i], listings[j], photos[i], photos[j], pairs[(i, j)])
        result.pair_evidence[(i, j)] = ev
        if ev.is_high:
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
                "candidate_sources": sorted(ev.candidate_sources),
                "full_matches": ev.full_matches,
                "center_matches": ev.center_matches,
                "reason_code": ev.reason_code,
            })
    result.high_confidence_pairs = high_edges
    grouping = complete_link_groups(listings, result.pair_evidence)
    result.groups = [list(group) for group in grouping.groups]
    result.review_pairs.extend(grouping.bridge_reviews)
    result.split_components = len(grouping.bridge_reviews)
    return result

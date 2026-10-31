"""Tests for greeceapt.scraper.dedupe — entity resolution."""

import hashlib

from greeceapt.scraper import dedupe


def _h(n: int) -> str:
    """
    Deterministic 64-bit phash hex per ``n``.

    Derived from sha256 so distinct ``n`` land ~32 bits apart — well clear of the
    Hamming-5 threshold. An arithmetic generator does not guarantee that separation
    and silently makes "different" photos compare as duplicates.
    """
    return hashlib.sha256(str(n).encode()).hexdigest()[:16]


def _listing(url, hashes, lat=37.9838, lon=23.7275, **over):
    row = {
        "url": url,
        "image_hashes": list(hashes),
        "latitude": lat,
        "longitude": lon,
        "area_sqm": 40.0,
        "price_eur": 50_000.0,
        "floor": 2.0,
        "year_built": 1970,
        "energy_class": "C",
    }
    row.update(over)
    return row


# ── Hashing primitives ────────────────────────────────────────────────────────

def test_phash_to_int_and_hamming():
    assert dedupe.phash_to_int("00") == 0
    assert dedupe.phash_to_int("ff") == 255
    assert dedupe.phash_to_int(None) is None
    assert dedupe.phash_to_int("zz") is None
    assert dedupe.hamming(0b1010, 0b1001) == 2
    assert dedupe.hamming(255, 255) == 0


# ── One-to-one photo matching ────────────────────────────────────────────────

def test_one_photo_cannot_match_many():
    """One photo in A resembling 3 near-identical photos in B must count once, not 3x."""
    a = [dedupe.phash_to_int(_h(1))]
    b = [dedupe.phash_to_int(_h(1))] * 3
    matched, min_d = dedupe.greedy_photo_matching(a, b)
    assert matched == 1
    assert min_d == 0


def test_photo_matching_pairs_distinct_photos():
    ints = [dedupe.phash_to_int(_h(i)) for i in range(3)]
    matched, _ = dedupe.greedy_photo_matching(ints, ints)
    assert matched == 3


def test_photo_matching_empty_sides():
    assert dedupe.greedy_photo_matching([], [1]) == (0, 64)


# ── Common-photo blacklist ───────────────────────────────────────────────────

def test_boilerplate_photo_is_blacklisted():
    shared = _h(99)
    listings = [_listing(f"u{i}", [shared, _h(i)]) for i in range(6)]
    bl = dedupe.build_common_photo_blacklist(listings)
    assert dedupe.phash_to_int(shared) in bl
    assert dedupe.phash_to_int(_h(0)) not in bl


def test_blacklisted_photo_provides_no_evidence():
    shared = _h(99)
    listings = [_listing(f"u{i}", [shared] + [_h(100 + i * 5 + k) for k in range(5)])
                for i in range(6)]
    res = dedupe.find_duplicate_groups(listings)
    assert all(len(g) == 1 for g in res.groups), "stock photo must not merge listings"


def test_near_boilerplate_photos_provide_no_pair_evidence():
    listings = []
    for index, value in enumerate((0, 1, 2, 4, 8)):
        row = _listing(f"u{index}", [])
        row["photo_fingerprints"] = {
            f"p{index}": {
                "version": "phash-v2",
                "full": f"{value:016x}",
                "center": f"{(1 << (20 + index)):016x}",
            }
        }
        listings.append(row)

    result = dedupe.find_duplicate_groups(listings)

    assert result.high_confidence_pairs == 0
    assert result.review_pairs == []
    assert sorted(result.groups) == [[0], [1], [2], [3], [4]]


# ── Pair scoring ─────────────────────────────────────────────────────────────

def _pair(a, b):
    return dedupe.evaluate_pair(a, b, dedupe.listing_hash_ints(a), dedupe.listing_hash_ints(b))


def test_identical_photos_and_metadata_is_high_confidence():
    hs = [_h(i) for i in range(6)]
    ev = _pair(_listing("a", hs), _listing("b", hs))
    assert ev.confidence == "HIGH"
    assert ev.photo_match_frac == 1.0


def test_conflicting_floor_does_not_veto():
    """Measured: floor disagrees ~60% of the time on identical-photo pairs — it is noise."""
    hs = [_h(i) for i in range(6)]
    ev = _pair(_listing("a", hs, floor=1.0), _listing("b", hs, floor=5.0))
    assert ev.confidence == "HIGH"
    assert ev.conflicting_fields == ()


def test_conflicting_area_vetoes_even_with_identical_photos():
    """Area is the one reliable identity field (93% agreement) — a real conflict is decisive."""
    hs = [_h(i) for i in range(6)]
    ev = _pair(_listing("a", hs, area_sqm=40.0), _listing("b", hs, area_sqm=70.0))
    assert ev.confidence == "NONE"
    assert "area_sqm" in ev.conflicting_fields


def test_conflicting_energy_does_not_veto():
    hs = [_h(i) for i in range(6)]
    ev = _pair(_listing("a", hs, energy_class="A"), _listing("b", hs, energy_class="G"))
    assert ev.confidence == "HIGH"


def test_single_shared_photo_out_of_ten_is_not_high():
    a = _listing("a", [_h(i) for i in range(10)])
    b = _listing("b", [_h(0)] + [_h(50 + i) for i in range(9)])
    ev = _pair(a, b)
    assert ev.photo_match_frac == 0.1
    assert ev.confidence != "HIGH"


def test_sparse_metadata_demands_stronger_photo_evidence():
    """With floor/year/energy unknown, a 40% photo overlap is only worth a review."""
    hs = [_h(i) for i in range(10)]
    sparse = dict(floor=None, year_built=None, energy_class=None, bedrooms=None, area_sqm=None)
    a = _listing("a", hs[:10], **sparse)
    b = _listing("b", hs[:4] + [_h(80 + i) for i in range(6)], **sparse)
    ev = _pair(a, b)
    assert ev.photo_match_frac == 0.4
    assert ev.agreeing_fields == 0
    assert ev.confidence == "REVIEW"


def test_sparse_metadata_with_dominant_photo_overlap_is_high():
    hs = [_h(i) for i in range(10)]
    sparse = dict(floor=None, year_built=None, energy_class=None, bedrooms=None, area_sqm=None)
    ev = _pair(_listing("a", hs, **sparse), _listing("b", hs, **sparse))
    assert ev.confidence == "HIGH"


def test_wild_price_gap_downgrades_to_review():
    hs = [_h(i) for i in range(6)]
    ev = _pair(_listing("a", hs, price_eur=50_000.0), _listing("b", hs, price_eur=100_000.0))
    assert ev.confidence == "REVIEW"


# ── Blocking ─────────────────────────────────────────────────────────────────

def test_distant_listings_still_paired_via_area_price_block():
    """Coordinates can be off by kilometres (measured max 2.6 km) — geo must not be a veto."""
    hs = [_h(i) for i in range(6)]
    a = _listing("a", hs, lat=37.9838, lon=23.7275)
    b = _listing("b", hs, lat=38.0300, lon=23.8000)   # ~7 km away
    pairs = dedupe.candidate_pairs([a, b])
    assert (0, 1) in pairs
    res = dedupe.find_duplicate_groups([a, b])
    assert res.groups == [[0, 1]]


def test_geo_block_pairs_nearby_listings():
    hs = [_h(i) for i in range(6)]
    a = _listing("a", hs, area_sqm=40.0, price_eur=50_000.0)
    b = _listing("b", hs, area_sqm=41.0, price_eur=90_000.0, lat=37.9840, lon=23.7277)
    assert (0, 1) in dedupe.candidate_pairs([a, b])


def test_geo_candidates_cover_one_thousand_meters_across_grid_cells():
    a = _listing("a", [], lat=37.98000, lon=23.7275, area_sqm=None, price_eur=None)
    within = _listing("within", [], lat=37.98898, lon=23.7275, area_sqm=None, price_eur=None)
    beyond = _listing("beyond", [], lat=37.98901, lon=23.7275, area_sqm=None, price_eur=None)

    candidates = dedupe.candidate_pairs([a, within, beyond])

    assert candidates[(0, 1)] == frozenset({"geo"})
    assert (0, 2) not in candidates


def test_metadata_block_covers_exact_relative_area_and_price_boundaries():
    base = _listing("base", [], lat=None, lon=None, area_sqm=100.0, price_eur=100_000.0)
    exact = _listing("exact", [], lat=None, lon=None, area_sqm=97.0, price_eur=75_000.0)
    area_outside = _listing(
        "area-outside", [], lat=None, lon=None, area_sqm=96.9, price_eur=100_000.0
    )
    price_outside = _listing(
        "price-outside", [], lat=None, lon=None, area_sqm=100.0, price_eur=74_900.0
    )

    candidates = dedupe.candidate_pairs([base, exact, area_outside, price_outside])

    assert candidates[(0, 1)] == frozenset({"metadata"})
    assert (0, 2) not in candidates
    assert (0, 3) not in candidates


def test_photo_candidate_survives_when_geo_and_metadata_miss():
    a = _listing("a", [], lat=37.9, lon=23.7, area_sqm=40.0, price_eur=50_000.0)
    b = _listing("b", [], lat=38.1, lon=23.9, area_sqm=90.0, price_eur=200_000.0)

    assert dedupe.candidate_pairs([a, b], [(1, 0)]) == {
        (0, 1): frozenset({"photo"})
    }


def test_candidate_sources_are_unioned_without_duplicate_pairs():
    a = _listing("a", [], area_sqm=100.0, price_eur=100_000.0)
    b = _listing("b", [], area_sqm=99.0, price_eur=90_000.0)

    assert dedupe.candidate_pairs([a, b], [(0, 1), (1, 0), (0, 1)]) == {
        (0, 1): frozenset({"photo", "geo", "metadata"})
    }


def test_unrelated_listings_are_not_candidates():
    a = _listing("a", [_h(1)], lat=37.9838, lon=23.7275, area_sqm=40.0, price_eur=50_000.0)
    b = _listing("b", [_h(2)], lat=38.1000, lon=23.9000, area_sqm=95.0, price_eur=250_000.0)
    assert dedupe.candidate_pairs([a, b]) == {}


# ── Grouping and component validation ────────────────────────────────────────

def test_transitive_group_is_merged_when_consistent():
    hs = [_h(i) for i in range(8)]
    a = _listing("a", hs[:6])
    b = _listing("b", hs[2:8])
    c = _listing("c", hs[4:8] + [_h(20), _h(21)])
    res = dedupe.find_duplicate_groups([a, b, c])
    assert res.groups == [[0, 1, 2]]


def test_inconsistent_component_is_split_not_merged():
    """A~B and B~C, but A and C conflict on AREA — merging all three would destroy data."""
    hs = [_h(i) for i in range(8)]
    a = _listing("a", hs[:6], area_sqm=40.0)
    b = _listing("b", hs[:6], area_sqm=None)   # bridges A and C on photos alone
    c = _listing("c", hs[:6], area_sqm=80.0)
    res = dedupe.find_duplicate_groups([a, b, c])
    assert res.split_components == 1
    assert sorted(res.groups) == [[0], [1], [2]]


def test_review_pairs_are_recorded_not_silently_dropped():
    sparse = dict(area_sqm=None, floor=None, year_built=None, energy_class=None, bedrooms=None)
    a = _listing("a", [_h(i) for i in range(10)], **sparse)
    b = _listing("b", [_h(0), _h(1), _h(2), _h(3)] + [_h(70 + i) for i in range(6)], **sparse)
    res = dedupe.find_duplicate_groups([a, b])
    assert res.review_pairs, "borderline pair (40% overlap, no corroboration) must be surfaced"
    assert res.review_pairs[0]["a_url"] == "a"
    assert "photo_match_frac" in res.review_pairs[0]


def test_empty_input_is_safe():
    res = dedupe.find_duplicate_groups([])
    assert res.groups == [] and res.review_pairs == []


def test_listings_without_hashes_are_never_merged():
    a = _listing("a", [])
    b = _listing("b", [])
    res = dedupe.find_duplicate_groups([a, b])
    assert sorted(res.groups) == [[0], [1]]


def test_union_find_basic():
    uf = dedupe.UnionFind(5)
    uf.union(0, 1)
    uf.union(3, 4)
    groups = sorted(sorted(v) for v in uf.groups().values())
    assert groups == [[0, 1], [2], [3, 4]]

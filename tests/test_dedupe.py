"""Tests for greeceapt.scraper.dedupe — entity resolution."""

import hashlib
import itertools

from greeceapt.scraper import dedupe
from greeceapt.scraper.fingerprints import PhotoFingerprint
from greeceapt.scraper.phash_index import PhotoRef, analyze_photo_index


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


def _refs(full_hashes, center_hashes=None):
    centers = center_hashes if center_hashes is not None else [None] * len(full_hashes)
    return tuple(
        PhotoRef(
            f"p{index}",
            PhotoFingerprint(
                "phash-v2" if center is not None else "phash-v1",
                dedupe.phash_to_int(full),
                dedupe.phash_to_int(center),
            ),
        )
        for index, (full, center) in enumerate(zip(full_hashes, centers))
    )


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
    a = _refs([_h(1)])
    b = _refs([_h(1)] * 3)
    matches = dedupe.greedy_photo_matching(a, b)
    assert len(matches) == 1
    assert matches[0].distance == 0


def test_photo_matching_pairs_distinct_photos():
    refs = _refs([_h(i) for i in range(3)])
    assert len(dedupe.greedy_photo_matching(refs, refs)) == 3


def test_photo_matching_empty_sides():
    assert dedupe.greedy_photo_matching((), _refs([_h(1)])) == ()


def test_photo_matching_uses_same_variant_and_records_winner():
    a = (PhotoRef("a", PhotoFingerprint("phash-v2", 0, 100)),)
    b = (PhotoRef("b", PhotoFingerprint("phash-v2", 1, 100)),)
    assert dedupe.greedy_photo_matching(a, b) == (
        dedupe.PhotoMatch("a", "b", 0, "center"),
    )

    cross_a = (PhotoRef("a", PhotoFingerprint("phash-v2", 0, 0xFFFFFFFFFFFFFFFF)),)
    cross_only = (PhotoRef("cross", PhotoFingerprint("phash-v2", 0xAAAAAAAAAAAAAAAA, 0)),)
    assert dedupe.greedy_photo_matching(cross_a, cross_only) == ()


def test_legacy_full_only_fingerprints_match_without_center_variant():
    a = (PhotoRef("a", PhotoFingerprint("phash-v1", 10, None)),)
    b = (PhotoRef("b", PhotoFingerprint("phash-v1", 11, None)),)

    assert dedupe.greedy_photo_matching(a, b) == (
        dedupe.PhotoMatch("a", "b", 1, "full"),
    )


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
    photos = analyze_photo_index([a, b]).usable_photos_by_listing
    return dedupe.evaluate_pair(a, b, photos[0], photos[1], {"photo"})


def test_identical_photos_and_metadata_is_high_confidence():
    hs = [_h(i) for i in range(6)]
    ev = _pair(_listing("a", hs), _listing("b", hs))
    assert ev.confidence == "HIGH"
    assert ev.photo_match_frac == 1.0
    assert ev.reason_code == "strong_photo_overlap"


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
    assert ev.reason_code == "area_conflict"


def test_conflicting_energy_does_not_veto():
    hs = [_h(i) for i in range(6)]
    ev = _pair(_listing("a", hs, energy_class="A"), _listing("b", hs, energy_class="G"))
    assert ev.confidence == "HIGH"


def test_single_shared_photo_out_of_ten_is_not_high():
    a = _listing("a", [_h(i) for i in range(10)])
    b = _listing("b", [_h(0)] + [_h(50 + i) for i in range(9)])
    ev = _pair(a, b)
    assert ev.photo_match_frac == 0.1
    assert ev.confidence == "REVIEW"
    assert ev.reason_code == "insufficient_photo_count"


def test_two_matching_photos_are_always_review_only():
    a = _listing("a", [_h(i) for i in range(2)])
    b = _listing("b", [_h(i) for i in range(2)])

    ev = _pair(a, b)

    assert ev.matched_photos == 2
    assert ev.photo_match_frac == 1.0
    assert ev.confidence == "REVIEW"
    assert ev.reason_code == "insufficient_photo_count"


def test_zero_matching_photos_is_none_with_reason():
    ev = _pair(_listing("a", [_h(1)]), _listing("b", [_h(2)]))

    assert ev.confidence == "NONE"
    assert ev.reason_code == "no_photo_matches"


def test_three_matches_at_sixty_percent_are_high_without_metadata():
    sparse = dict(area_sqm=None, year_built=None, bedrooms=None)
    a = _listing("a", [_h(i) for i in range(5)], **sparse)
    b = _listing("b", [_h(i) for i in range(3)] + [_h(50), _h(51)], **sparse)

    ev = _pair(a, b)

    assert ev.matched_photos == 3
    assert ev.photo_match_frac == 0.6
    assert ev.confidence == "HIGH"
    assert ev.reason_code == "strong_photo_overlap"


def test_three_matches_at_moderate_overlap_need_a_corroborator():
    hashes = [_h(i) for i in range(8)]
    a = _listing("a", hashes, area_sqm=40.0, year_built=None)
    b = _listing("b", hashes[:3] + [_h(50 + i) for i in range(5)], area_sqm=40.0, year_built=None)

    ev = _pair(a, b)

    assert ev.photo_match_frac == 0.375
    assert ev.agreeing_fields == 1
    assert ev.confidence == "HIGH"
    assert ev.reason_code == "corroborated_photo_overlap"


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
    assert ev.reason_code == "uncorroborated_photo_overlap"


def test_sparse_metadata_with_dominant_photo_overlap_is_high():
    hs = [_h(i) for i in range(10)]
    sparse = dict(floor=None, year_built=None, energy_class=None, bedrooms=None, area_sqm=None)
    ev = _pair(_listing("a", hs, **sparse), _listing("b", hs, **sparse))
    assert ev.confidence == "HIGH"


def test_wild_price_gap_downgrades_to_review():
    hs = [_h(i) for i in range(6)]
    ev = _pair(_listing("a", hs, price_eur=50_000.0), _listing("b", hs, price_eur=100_000.0))
    assert ev.confidence == "REVIEW"
    assert ev.reason_code == "price_gap"


def test_pair_evidence_records_sources_variants_counts_and_photo_keys():
    a = _listing("a", [], area_sqm=None)
    b = _listing("b", [], area_sqm=None)
    photos_a = (
        PhotoRef("a-full", PhotoFingerprint("phash-v2", 0, 1000)),
        PhotoRef("a-center", PhotoFingerprint("phash-v2", 2000, 10)),
        PhotoRef("a-both", PhotoFingerprint("phash-v2", 20, 30)),
    )
    photos_b = (
        PhotoRef("b-full", PhotoFingerprint("phash-v2", 1, 3000)),
        PhotoRef("b-center", PhotoFingerprint("phash-v2", 4000, 11)),
        PhotoRef("b-both", PhotoFingerprint("phash-v2", 22, 30)),
    )

    ev = dedupe.evaluate_pair(a, b, photos_a, photos_b, {"photo", "geo"})

    assert ev.candidate_sources == frozenset({"photo", "geo"})
    assert ev.full_matches == 1
    assert ev.center_matches == 2
    assert [(match.a_photo_key, match.b_photo_key, match.variant) for match in ev.matches] == [
        ("a-both", "b-both", "center"),
        ("a-center", "b-center", "center"),
        ("a-full", "b-full", "full"),
    ]


def test_duplicate_photo_content_under_distinct_keys_counts_once():
    repeated = PhotoFingerprint("phash-v2", 123, 456).to_json()
    a = _listing("a", [])
    b = _listing("b", [])
    a["photo_fingerprints"] = {f"a-{index}": repeated for index in range(3)}
    b["photo_fingerprints"] = {f"b-{index}": repeated for index in range(3)}

    result = dedupe.find_duplicate_groups([a, b])

    assert result.groups == [[0], [1]]
    assert result.high_confidence_pairs == 0
    assert result.review_pairs[0]["matched_photos"] == 1


def test_pair_review_json_is_independent_of_listing_order():
    a = _listing("a", [_h(1), _h(2)])
    b = _listing("b", [_h(1), _h(2)])

    forward = dedupe.find_duplicate_groups([a, b]).review_pairs
    reverse = dedupe.find_duplicate_groups([b, a]).review_pairs

    assert forward == reverse


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

def _evidence(confidence, matched, overlap, minimum, reason="test"):
    return dedupe.PairEvidence(
        candidate_sources=frozenset({"photo"}),
        matches=(),
        full_matches=matched,
        center_matches=0,
        photo_match_frac=overlap,
        matched_photos=matched,
        min_hamming=minimum,
        geo_distance_m=None,
        price_rel_delta=None,
        agreeing_fields=0,
        conflicting_fields=(),
        reason_code=reason,
        confidence=confidence,
    )


def test_transitive_photo_bridge_keeps_strong_safe_cluster():
    hs = [_h(i) for i in range(8)]
    a = _listing("a", hs[:6])
    b = _listing("b", hs[2:8])
    c = _listing("c", hs[4:8] + [_h(20), _h(21)])
    res = dedupe.find_duplicate_groups([a, b, c])
    assert res.groups == [[0, 1], [2]]
    assert any(review["reason_code"] == "incomplete_high_clique" for review in res.review_pairs)


def test_area_conflict_bridge_does_not_discard_safe_cluster():
    """A~B and B~C, but A and C conflict on AREA — merging all three would destroy data."""
    hs = [_h(i) for i in range(8)]
    a = _listing("a", hs[:6], area_sqm=40.0)
    b = _listing("b", hs[:6], area_sqm=None)   # bridges A and C on photos alone
    c = _listing("c", hs[:6], area_sqm=80.0)
    res = dedupe.find_duplicate_groups([a, b, c])
    assert res.split_components == 1
    assert sorted(res.groups) == [[0, 1], [2]]


def test_complete_link_merges_a_direct_high_clique():
    listings = [{"url": "a"}, {"url": "b"}, {"url": "c"}]
    evidence = {
        (0, 1): _evidence("HIGH", 6, 0.8, 0),
        (0, 2): _evidence("HIGH", 5, 0.7, 1),
        (1, 2): _evidence("HIGH", 4, 0.6, 2),
    }

    result = dedupe.complete_link_groups(listings, evidence)

    assert result.groups == ((0, 1, 2),)
    assert result.bridge_reviews == ()


def test_complete_link_bridge_prefers_stronger_edge_and_emits_review():
    listings = [{"url": "a"}, {"url": "b"}, {"url": "c"}]
    evidence = {
        (0, 1): _evidence("HIGH", 6, 0.8, 0),
        (1, 2): _evidence("HIGH", 4, 0.7, 1),
        (0, 2): _evidence("NONE", 0, 0.0, 20, "no_photo_matches"),
    }

    result = dedupe.complete_link_groups(listings, evidence)

    assert result.groups == ((0, 1), (2,))
    assert len(result.bridge_reviews) == 1
    assert result.bridge_reviews[0]["reason_code"] == "incomplete_high_clique"
    assert result.bridge_reviews[0]["confidence"] == "REVIEW"
    assert "matches" in result.bridge_reviews[0]
    assert result.bridge_reviews[0]["candidate_sources"] == ["photo"]
    assert result.bridge_reviews[0]["component_ids"] == ["a", "b", "c"]


def test_complete_link_is_deterministic_across_listing_and_edge_order():
    stable_edges = {
        ("a", "b"): _evidence("HIGH", 5, 0.8, 1),
        ("a", "c"): _evidence("HIGH", 5, 0.8, 1),
        ("b", "c"): _evidence("NONE", 0, 0.0, 30, "no_photo_matches"),
    }
    normalized = []
    for order in itertools.permutations(("a", "b", "c")):
        index = {name: position for position, name in enumerate(order)}
        evidence = {
            tuple(sorted((index[left], index[right]))): value
            for (left, right), value in reversed(tuple(stable_edges.items()))
        }
        result = dedupe.complete_link_groups([{"url": name} for name in order], evidence)
        normalized.append((
            sorted(sorted(order[position] for position in group) for group in result.groups),
            [(review["reason_code"], review["component_ids"]) for review in result.bridge_reviews],
        ))

    assert all(value == normalized[0] for value in normalized)
    assert normalized[0][0] == [["a", "b"], ["c"]]


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

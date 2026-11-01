"""Integration coverage for the default v2 scraper merge pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from greeceapt.scraper import run_all
from greeceapt.scraper import util as scraper_util
from greeceapt.scraper.fingerprints import PhotoFingerprint


def _hash(label: str) -> int:
    return int(hashlib.sha256(label.encode()).hexdigest()[:16], 16)


def _listing(name: str, photos: list[str], **overrides) -> dict:
    row = {
        "url": name,
        "source": "fixture",
        "photo_urls": [f"photo:{label}" for label in photos],
        "photos_count": len(photos),
        "latitude": 37.9838,
        "longitude": 23.7275,
        "area_sqm": 50.0,
        "price_eur": 100_000.0,
        "year_built": 1980,
        "bedrooms": 2,
        "publication_date": "2026-10-20",
    }
    row.update(overrides)
    return row


@pytest.fixture
def merge_paths(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(run_all, "DATA_DIR", tmp_path)
    monkeypatch.setattr(run_all, "OUTPUT_JSON", tmp_path / "listings.json")
    monkeypatch.setattr(run_all, "DELETED_JSON", tmp_path / "deleted_listings.json")
    monkeypatch.setattr(run_all, "STALE_JSON", tmp_path / "stale_listings.json")
    monkeypatch.setattr(run_all, "XE_JSON", tmp_path / "xe_listings.json")
    monkeypatch.setattr(run_all, "SPITOGATOS_JSON", tmp_path / "spitogatos_listings.json")
    monkeypatch.setattr(run_all, "REVIEW_JSON", tmp_path / "review_pairs.json")
    monkeypatch.setattr(scraper_util.paths, "PHASH_CACHE_JSON", tmp_path / "phash_cache.json")
    return tmp_path


def _install_fake_fingerprints(monkeypatch) -> None:
    async def fake(photo_urls, url_cache, sem, client):
        result = {}
        for url in photo_urls:
            label = url.removeprefix("photo:")
            value = PhotoFingerprint("phash-v2", _hash(label), _hash(f"center:{label}"))
            result[url] = value
            url_cache[url] = value
        return result

    monkeypatch.setattr(scraper_util, "compute_listing_photo_fingerprints", fake)


def test_v2_merge_writes_fingerprints_compatibility_fields_and_conservative_groups(
    monkeypatch, merge_paths, caplog
):
    caplog.set_level("INFO")
    _install_fake_fingerprints(monkeypatch)
    rows = [
        _listing("high-a", ["h1", "h2", "h3"]),
        _listing("high-b", ["h1", "h2", "h3"]),
        _listing("weak-a", ["w1", "w2"]),
        _listing("weak-b", ["w1", "w2"]),
        _listing("price-a", ["p1", "p2", "p3"], price_eur=100_000.0),
        _listing("price-b", ["p1", "p2", "p3"], price_eur=140_000.0),
        _listing("area-a", ["a1", "a2", "a3"], area_sqm=50.0),
        _listing("area-b", ["a1", "a2", "a3"], area_sqm=80.0),
        _listing("bridge-a", ["ab1", "ab2", "ab3"]),
        _listing("bridge-b", ["ab1", "ab2", "ab3", "bc1", "bc2", "bc3"]),
        _listing("bridge-c", ["bc1", "bc2", "bc3"]),
    ]
    (run_all.XE_JSON).write_text(json.dumps(rows), encoding="utf-8")
    (run_all.SPITOGATOS_JSON).write_text("[]", encoding="utf-8")

    fresh, stale, merged = asyncio.run(run_all._merge())

    assert stale == []
    assert len(merged) == 2
    assert len(fresh) == len(rows) - 2
    assert {record["absorbed"]["url"] for record in merged} == {"high-b", "bridge-b"}
    output_rows = fresh + [record["absorbed"] for record in merged]
    for listing in output_rows:
        assert listing["photo_fingerprints"]
        assert all(value["version"] == "phash-v2" for value in listing["photo_fingerprints"].values())
        assert all(isinstance(value, str) for value in listing["photo_url_hashes"].values())
        assert all(isinstance(value, str) for value in listing["image_hashes"])
    cache = json.loads((merge_paths / "phash_cache.json").read_text(encoding="utf-8"))
    assert cache and all(value["version"] == "phash-v2" for value in cache.values())
    assert "candidates by source" in caplog.text


def test_review_and_merge_audit_include_complete_authorizing_evidence(monkeypatch, merge_paths):
    _install_fake_fingerprints(monkeypatch)
    rows = [
        _listing("high-a", ["h1", "h2", "h3"]),
        _listing("high-b", ["h1", "h2", "h3"]),
        _listing("weak-a", ["w1", "w2"]),
        _listing("weak-b", ["w1", "w2"]),
    ]
    run_all.XE_JSON.write_text(json.dumps(rows), encoding="utf-8")
    run_all.SPITOGATOS_JSON.write_text("[]", encoding="utf-8")

    _fresh, _stale, merged = asyncio.run(run_all._merge())

    reviews = json.loads(run_all.REVIEW_JSON.read_text(encoding="utf-8"))
    weak = next(review for review in reviews if review.get("reason_code") == "insufficient_photo_count")
    required = {
        "a_url", "b_url", "candidate_sources", "full_matches", "center_matches",
        "matches", "photo_match_frac", "min_hamming", "geo_distance_m", "price_rel_delta",
        "agreeing_fields", "conflicting_fields", "reason_code", "confidence",
    }
    assert required <= weak.keys()
    assert len(weak["candidate_sources"]) == len(set(weak["candidate_sources"]))
    assert all({"a_photo_key", "b_photo_key", "distance", "variant"} <= match.keys()
               for match in weak["matches"])

    assert len(merged) == 1
    audit = merged[0]
    assert audit["dedupe_policy_version"] == "phash-v2-conservative"
    assert audit["group_member_urls"] == ["high-a", "high-b"]
    assert len(audit["authorizing_evidence"]) == 1
    assert required <= audit["authorizing_evidence"][0].keys()


def test_zero_review_run_removes_stale_review_file(monkeypatch, merge_paths):
    _install_fake_fingerprints(monkeypatch)
    run_all.XE_JSON.write_text(json.dumps([_listing("one", ["x1", "x2", "x3"])]), encoding="utf-8")
    run_all.SPITOGATOS_JSON.write_text("[]", encoding="utf-8")
    run_all.REVIEW_JSON.write_text('[{"stale": true}]', encoding="utf-8")

    asyncio.run(run_all._merge())

    assert not run_all.REVIEW_JSON.exists()


def test_network_disabled_merge_uses_legacy_full_hashes_without_download(monkeypatch, merge_paths):
    async def unexpected_download(*args, **kwargs):
        raise AssertionError("network-disabled merge attempted fingerprint download")

    monkeypatch.setattr(scraper_util, "compute_listing_photo_fingerprints", unexpected_download)
    shared = ["l1", "l2", "l3"]
    rows = [_listing("legacy-a", shared), _listing("legacy-b", shared)]
    run_all.XE_JSON.write_text(json.dumps(rows), encoding="utf-8")
    run_all.SPITOGATOS_JSON.write_text("[]", encoding="utf-8")
    scraper_util.save_standalone_phash_cache(
        {f"photo:{label}": f"{_hash(label):016x}" for label in shared},
        merge_paths / "phash_cache.json",
    )

    fresh, _stale, merged = asyncio.run(run_all._merge(network_enabled=False))

    assert len(fresh) == 1
    assert len(merged) == 1
    assert all(
        value["version"] == "phash-v1"
        for value in fresh[0]["photo_fingerprints"].values()
    )


def test_survivor_merge_keeps_date_range_and_longer_photo_set():
    kept = {
        "url": "a", "source": "xe", "price_eur": 50_000,
        "publication_date": "2026-03-01", "photo_urls": ["1", "2", "3"],
        "photos_count": 99,
    }
    absorbed = {
        "url": "b", "source": "spitogatos", "price_eur": 60_000,
        "publication_date": "2026-05-20", "photo_urls": ["1"],
    }

    scraper_util.merge_duplicate_listing_into_kept(kept, absorbed)

    assert kept["earliest_publication_date"] == "2026-03-01"
    assert kept["latest_publication_date"] == "2026-05-20"
    assert kept["publication_date"] == "2026-05-20"
    assert kept["photo_urls"] == ["1", "2", "3"]
    assert kept["photos_count"] == 3
